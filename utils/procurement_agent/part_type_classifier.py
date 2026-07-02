"""
Part-type classifier — Phase 1 of the intake redesign.

`classify_part_type(first_message, llm_call=...)` classifies a user's first
intake message into one of the registry's known part types (or UNKNOWN), and
captures the parent-machine identity (`component_of`) when the message names a
component of a machine — the F1 case: "Goulds 3196 mechanical seal" →
`part_type=mechanical_seal, component_of="Goulds 3196"`.

Design (per the overnight build brief T2):
  - **Constrained output contract.** The LLM is prompted to return strict JSON
    with only `part_type` (one of the registry's known types + "unknown"),
    `component_of` (str | null), and `confidence` (0-100). The parser validates
    the part_type against the registry and falls back to UNKNOWN on ANY parse or
    validation failure — never raises.
  - **`regime` / `sourcing` are data-driven**, looked up from the registry by the
    returned part_type. The LLM only classifies the type; the registry remains
    the single source of truth for sourcing regime. UNKNOWN → regime/sourcing
    from the UNKNOWN sentinel.
  - **`llm_call` is injectable** so tests mock it. The default production call
    uses the codebase's raw `requests.post` pattern against api.anthropic.com
    (NOT the SDK, NOT ANTHROPIC_BASE_URL — immune to the proxy-leak class of
    bug), Haiku-class model, temperature 0.
  - **Fail-soft.** No API key / network error / timeout / rate-limit → UNKNOWN,
    logged, never raised into the intake pipeline (mirrors the integration
    pattern in CLAUDE.md §9 — a failure degrades, never crashes the run).

This module is gated by `INTAKE_TYPE_AWARE` at the call site (T4); the classifier
itself is inert until invoked. No live calls occur at import time.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from typing import Callable, Optional

import requests

from utils.procurement_agent.part_type_registry import (
    KNOWN_PART_TYPES,
    UNKNOWN_PART_TYPE,
    get_profile,
)


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Classification:
    """The classifier's result. `part_type` is one of KNOWN_PART_TYPES or 'unknown';
    `regime` / `sourcing` are resolved from the registry; `component_of` captures
    the parent-machine identity for ANCHORED types (None when not applicable)."""
    part_type: str
    regime: str
    sourcing: str
    component_of: Optional[str]
    confidence: int

    def as_dict(self) -> dict:
        return asdict(self)


def _unknown_classification() -> Classification:
    """The safe fallback for any parse/validation/failure path."""
    p = get_profile(UNKNOWN_PART_TYPE)
    return Classification(
        part_type=UNKNOWN_PART_TYPE,
        regime=p.regime,
        sourcing=p.sourcing,
        component_of=None,
        confidence=0,
    )


# ---------------------------------------------------------------------------
# Constrained prompt — the LLM may only emit known types + "unknown".
# ---------------------------------------------------------------------------

_CLASSIFIER_SYSTEM = """You are an industrial procurement part-type classifier.

Given the user's first message describing a maintenance part or equipment need,
classify it into EXACTLY ONE part type and capture any parent-machine identity.

Return ONLY valid JSON — no markdown fences, no prose — with exactly these keys:
{
  "part_type": one of ["mechanical_seal", "pump", "valve", "sensor_instrument", "motor_drive", "unknown"],
  "component_of": string or null,
  "confidence": integer 0-100
}

part_type rules:
  - "mechanical_seal" — a seal for a pump (the seal is a COMPONENT of the pump).
  - "pump" — the pump itself (centrifugal, multistage, positive displacement, etc.).
  - "valve" — ball / butterfly / gate / globe / check / sanitary valve.
  - "sensor_instrument" — pressure transmitter, flow meter, level instrument, pH probe, thermometer.
  - "motor_drive" — electric motor, VFD/drive, gearbox, actuator.
  - "unknown" — anything off this list (hose, fitting, light, belt, coupling, gasket, gibberish, etc.).

component_of rules:
  - When the message names a part that is a COMPONENT of a named parent machine,
    capture the parent's identity (brand + model if given) in component_of.
    Example: "Goulds 3196 mechanical seal" -> part_type="mechanical_seal",
    component_of="Goulds 3196".
  - When the message is the machine itself (a pump, a motor), component_of is null.
  - When no parent is named, component_of is null.

confidence:
  - 90+ when the type is unambiguous from the wording.
  - 60-89 when inferred from context but reasonably clear.
  - <60 when uncertain; prefer "unknown" with low confidence over a guess.

Return ONLY the JSON object."""


# The allowed part_type strings, as the LLM must emit them (lowercase).
_ALLOWED_TYPES = set(KNOWN_PART_TYPES) | {UNKNOWN_PART_TYPE}


# ---------------------------------------------------------------------------
# Default llm_call — raw requests.post to api.anthropic.com (Haiku, temp 0).
# Fail-soft: returns "" on any error so the parser falls back to UNKNOWN.
# NEVER reads ANTHROPIC_BASE_URL (immune to the proxy-leak class).
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = os.environ.get("INTAKE_CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")


def _default_llm_call(system_prompt: str, user_message: str) -> str:
    """Production LLM call: raw requests.post to api.anthropic.com.

    Returns the raw response text, or "" on any failure (no key, network,
    timeout, non-2xx). Never raises — the classifier degrades to UNKNOWN.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return ""
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _DEFAULT_MODEL,
                "max_tokens": 256,
                "temperature": 0,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]
    except Exception as exc:
        print(f"[part_type_classifier] default llm_call failed: {exc}")
        return ""


# ---------------------------------------------------------------------------
# JSON parsing + validation — the constrained-output parser.
# ---------------------------------------------------------------------------

def _strip_fences(raw: str) -> str:
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def _coerce_confidence(value) -> int:
    """Coerce a confidence value to a clamped int 0-100; 0 on any failure."""
    try:
        c = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, c))


def _normalize_component_of(value) -> Optional[str]:
    """component_of must be a non-empty trimmed string or None."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v or None


def _parse_classification(raw: str) -> Classification:
    """Parse + validate the LLM's raw JSON into a Classification.

    Falls back to UNKNOWN on ANY failure: malformed JSON, missing keys, an
    unknown part_type string, wrong types. Never raises.
    """
    if not raw:
        return _unknown_classification()
    try:
        obj = json.loads(_strip_fences(raw))
    except Exception:
        return _unknown_classification()
    if not isinstance(obj, dict):
        return _unknown_classification()

    part_type = obj.get("part_type")
    if not isinstance(part_type, str):
        return _unknown_classification()
    part_type = part_type.strip().lower()
    if part_type not in _ALLOWED_TYPES:
        return _unknown_classification()

    profile = get_profile(part_type)
    component_of = _normalize_component_of(obj.get("component_of"))
    confidence = _coerce_confidence(obj.get("confidence"))

    return Classification(
        part_type=part_type,
        regime=profile.regime,
        sourcing=profile.sourcing,
        component_of=component_of,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# The injectable LLM call signature: (system_prompt, user_message) -> raw text.
LLMCall = Callable[[str, str], str]


def classify_part_type(
    first_message: str,
    llm_call: Optional[LLMCall] = None,
) -> Classification:
    """Classify the user's first intake message.

    Args:
        first_message: the user's first chat message (text only).
        llm_call: optional injectable (system_prompt, user_message) -> raw_text.
            When None, the default raw-requests.post call is used. Tests pass a
            mock that returns a canned JSON string.

    Returns:
        A Classification. Never raises — any failure (no key, network error,
        malformed/invalid LLM output) yields an UNKNOWN classification so the
        intake pipeline falls through to the current generic behavior.
    """
    message = (first_message or "").strip()
    if not message:
        return _unknown_classification()

    call: LLMCall = llm_call if llm_call is not None else _default_llm_call
    try:
        raw = call(_CLASSIFIER_SYSTEM, message)
    except Exception as exc:
        # An injectable that raises must not blow up the intake pipeline.
        print(f"[part_type_classifier] llm_call raised: {exc}")
        return _unknown_classification()
    return _parse_classification(raw)
