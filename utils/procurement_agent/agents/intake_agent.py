"""
Intake Agent — multimodal data extraction, sufficiency assessment, clarification.

Brief reference: Section 3.1, Section 8.1.

Rectification Sprint additions:
  - Fix 1: classify_by_units() — units-based classification override post-VLM extraction
  - Fix 3: assess_proceed_state() — asymmetric stop condition (mfg<70, pid≥80 proceeds with caveat)
"""

import base64
import json
import re
import requests
from typing import Optional, Tuple

from utils.models import SourcingRun


def _detect_media_type(img_bytes: bytes) -> str:
    """Return Anthropic-accepted media_type string inferred from image magic bytes."""
    if img_bytes[:2] == b"\xff\xd8":
        return "image/jpeg"
    if img_bytes[:4] == b"\x89PNG":
        return "image/png"
    if img_bytes[:4] == b"RIFF" and img_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"  # safe default for unknown formats

# Per-category fields that must be populated before sourcing can start.
# Key: substring of detected_type (lowercased).
CATEGORY_REQUIRED_FIELDS: dict[str, list[str]] = {
    "motor":                    ["detected_type", "hp", "voltage", "frame", "rpm"],
    "vfd":                      ["detected_type", "voltage", "hp"],
    "variable frequency drive": ["detected_type", "voltage", "hp"],
    "mechanical seal":          ["detected_type", "shaft_size", "material_spec"],
    "bearing":                  ["detected_type", "bore_diameter"],
    "pressure sensor":          ["detected_type", "psi"],
    "valve":                    ["detected_type", "connection_size"],
}

SUFFICIENCY_THRESHOLD = 70  # both confidences must reach this in the symmetric case

_NULL_VALUES = {None, "", "null", "N/A", "Unknown", "UNKNOWN-PN", "none", "unknown"}

# ---------------------------------------------------------------------------
# Fix 1 — Units-based classification override
# ---------------------------------------------------------------------------

# Each rule: (required_fields_set, new_detected_type, new_category, priority)
# Higher priority wins when multiple rules match.
UNIT_CLASSIFICATION_RULES: list[tuple] = [
    # Three motor signals → most specific label wins.
    (frozenset({"hp", "rpm", "frame"}), "3-Phase Electric Motor", "Equipment", 11),
    # Two motor signals — NEMA frame or RPM alone with HP is sufficient.
    (frozenset({"hp", "frame"}),        "Electric Motor",         "Equipment", 10),
    (frozenset({"hp", "rpm"}),          "Electric Motor",         "Equipment",  9),
    # hp+voltage alone is too ambiguous (pumps, compressors also carry both) — omitted.
    (frozenset({"gpm", "psi"}),         "Centrifugal Pump",       "Equipment",  8),
    (frozenset({"gpm", "head"}),        "Centrifugal Pump",       "Equipment",  7),
    (frozenset({"bore_diameter"}),      "Bearing",                "Part",       6),
    (frozenset({"shaft_size"}),         "Mechanical Seal",        "Part",       6),
]


def classify_by_units(specs: dict) -> tuple:
    """Return (detected_type, category, override_applied) based on present unit fields.

    Fires only when the current detected_type conflicts with what the units imply.
    Returns (None, None, False) when no override is warranted.
    """
    best_priority = -1
    best_type: Optional[str] = None
    best_cat:  Optional[str] = None

    for required_fields, new_type, new_cat, priority in UNIT_CLASSIFICATION_RULES:
        if priority <= best_priority:
            continue
        if all(specs.get(f) not in _NULL_VALUES for f in required_fields):
            best_priority = priority
            best_type = new_type
            best_cat  = new_cat

    if best_type is None:
        return None, None, False

    current_type = (specs.get("detected_type") or "").lower()
    # Skip if the key equipment word (last word of best_type) is already in the current type.
    best_base = best_type.split()[-1].lower()
    if best_base in current_type:
        return None, None, False

    return best_type, best_cat, True


# ---------------------------------------------------------------------------
# Fix 3 — Asymmetric stop condition
# ---------------------------------------------------------------------------

_PROCEED_CAVEAT = (
    "Manufacturer identity could not be confirmed with high confidence. "
    "The part type appears specific — sourcing will proceed with a broader search, "
    "but verify results against original equipment documentation."
)


def _first_missing_required_field(specs: dict) -> Optional[str]:
    detected = (specs.get("detected_type") or "").lower()
    for key, fields in CATEGORY_REQUIRED_FIELDS.items():
        if key in detected:
            for field_name in fields:
                if specs.get(field_name) in _NULL_VALUES:
                    return field_name
    return None


def assess_proceed_state(
    specs: dict, mfg_conf: float, part_conf: float
) -> tuple:
    """Return (state, missing_field, caveat_message).

    States:
        proceed_full_confidence          — both confident, required fields present
        proceed_with_manufacturer_caveat — mfg<70, pid≥80; proceed with banner
        needs_clarification              — both confident but a required field is missing
        blocked_need_part_id             — mfg≥70, pid<70
        blocked_need_either              — mfg<70 and pid<80
    """
    if mfg_conf >= SUFFICIENCY_THRESHOLD and part_conf >= SUFFICIENCY_THRESHOLD:
        missing = _first_missing_required_field(specs)
        if missing:
            return "needs_clarification", missing, None
        return "proceed_full_confidence", None, None

    if mfg_conf < SUFFICIENCY_THRESHOLD and part_conf >= 80:
        return "proceed_with_manufacturer_caveat", None, _PROCEED_CAVEAT

    if mfg_conf >= SUFFICIENCY_THRESHOLD and part_conf < SUFFICIENCY_THRESHOLD:
        return "blocked_need_part_id", "part_type", None

    return "blocked_need_either", "manufacturer", None


# ---------------------------------------------------------------------------
# LLM prompts
# ---------------------------------------------------------------------------

_EXTRACTION_SYSTEM = """You are an industrial procurement data extractor.
Given user input (text and/or images of equipment nameplates), extract structured specifications.

Return ONLY valid JSON with exactly these keys — no additional keys, no markdown fences:
{
  "manufacturer": string or null,
  "model": string or null,
  "part_number": string or null,
  "voltage": string or null,
  "category": "Part" or "Equipment",
  "hp": string or null,
  "serial_number": string or null,
  "description": string or null,
  "gpm": string or null,
  "psi": string or null,
  "frame": string or null,
  "phase": string or null,
  "detected_type": string or null,
  "rpm": string or null,
  "shaft_size": string or null,
  "bore_diameter": string or null,
  "seal_face_size": string or null,
  "connection_size": string or null,
  "material_spec": string or null,
  "use_case": string or null,
  "manufacturer_confidence": integer 0-100,
  "part_id_confidence": integer 0-100,
  "confidence_reasoning": string
}

category rules:
  "Equipment" — full assembled units: pump, motor, compressor, blower, conveyor
  "Part"      — replacement components: VFD, bearing, seal, relay, sensor, valve, belt, coupling

detected_type: specific equipment/part type e.g. "centrifugal pump", "induction motor", "mechanical seal"

manufacturer_confidence scoring:
  95+  manufacturer name is explicitly stated or clearly visible in image
  75-94 inferred from recognizable part number prefix or well-known model number
  50-74 guessed from partial information or context clues
  0-29  unknown or unrecognizable — manufacturer not determinable

part_id_confidence scoring:
  90+  exact part number explicitly provided and unambiguous
  70-89 part type clearly identified with key specs present
  50-69 part type inferred but missing key specs
  <50  part type uncertain — cannot reliably source without more info

Rules:
  - Extract ALL visible technical specs; be thorough
  - Set fields to null if not determinable — never invent or estimate values
  - description: one line summarizing what the item is, from the input
  - If prior specs are provided, merge carefully: only update fields with new information

When the user is responding to a specific clarification question from the agent
(indicated by "Agent asked: ..." in the input), treat their reply as an authoritative
direct answer to that question. Score confidence accordingly:
  - User provides a manufacturer name in response to "Who is the manufacturer?" → manufacturer_confidence = 95
  - User provides dimensions in response to a sizing question → high confidence in those fields
  - User provides a generic or evasive answer → moderate confidence
The prior context summary shows what specs were already extracted. Confidence in those
fields persists from the prior turn unless the user explicitly contradicts them."""

_CLARIFICATION_SYSTEM = """You are an industrial procurement assistant.
The user is trying to source a replacement part or piece of equipment.
Given the partial specs and the specific missing field, generate exactly ONE focused question.
The question must be specific and actionable — ask for precisely one piece of information.
Return ONLY the question text. No preamble, no explanation, no numbered list."""

# Default fallback questions when the LLM is unavailable.
_DEFAULT_QUESTIONS: dict[str, str] = {
    "manufacturer":    "Who is the manufacturer of this equipment? (Check the nameplate or documentation.)",
    "part_type":       "What type of part or equipment is this? (e.g., motor, pump, bearing, mechanical seal)",
    "hp":              "What is the horsepower (HP) rating shown on the nameplate?",
    "voltage":         "What is the operating voltage? (e.g., 460V, 230V, 208V)",
    "frame":           "What is the NEMA frame size? (usually a 3-4 digit code like 213T, 256T, 447T — found on the nameplate)",
    "rpm":             "What is the RPM (speed) rating shown on the nameplate? (typically 1200, 1800, or 3600)",
    "shaft_size":      "What is the shaft diameter? (e.g., 1.5 inch, 42mm — critical for seal sizing)",
    "bore_diameter":   "What is the bore diameter of this bearing? (the inner diameter in mm or inches)",
    "material_spec":   "What are the seal face materials? (e.g., Carbon/Silicon Carbide, Viton elastomers)",
    "psi":             "What is the operating pressure rating in PSI?",
    "connection_size": "What is the connection size or pipe diameter? (e.g., 2 inch NPT, DN50)",
    "detected_type":   "What type of equipment or part is this exactly? (e.g., centrifugal pump, induction motor, mechanical seal)",
}


# ---------------------------------------------------------------------------
# IntakeAgent
# ---------------------------------------------------------------------------

class IntakeAgent:
    """Extracts structured AssetSpecs from user input and assesses sufficiency.

    Supports multi-turn clarification: on each call, prior specs from
    run.asset_specs_json are merged with newly extracted fields (new values win).
    """

    def __init__(self, anthropic_api_key: Optional[str] = None):
        self._api_key = anthropic_api_key

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, run: SourcingRun, user_input: dict) -> dict:
        """Extract AssetSpecs from user_input and assess sufficiency.

        Args:
            run: current SourcingRun (provides prior context on follow-up turns)
            user_input: dict with keys:
                - "text": str — the user's chat message
                - "images": list[bytes] — uploaded image data (optional)
                - "force_proceed": bool — bypass sufficiency gate if True

        Returns:
            dict with:
                - "asset_specs": dict — merged AssetSpecs fields
                - "manufacturer_confidence": float 0-100
                - "part_id_confidence": float 0-100
                - "sufficient": bool
                - "follow_up_question": str | None
                - "manufacturer_caveat": str | None
                - "confidence_summary": dict
        """
        text           = user_input.get("text", "") or ""
        images         = user_input.get("images") or []
        force_proceed  = bool(user_input.get("force_proceed", False))
        prior_specs    = run.asset_specs_json or {}
        prior_question = user_input.get("prior_question")

        # Extract from this turn's input
        if images:
            extracted = self._extract_multimodal(text, images, prior_specs)
        else:
            extracted = self._extract_text(text, prior_specs, prior_question=prior_question)

        # Merge with prior specs — new non-null values win
        merged = dict(prior_specs)
        for k, v in extracted.items():
            if not isinstance(v, (list, dict)) and v not in _NULL_VALUES:
                merged[k] = v

        # Fix 1: units-based classification override (runs after VLM merge)
        new_type, new_cat, override_applied = classify_by_units(merged)
        if override_applied:
            old_type = merged.get("detected_type")
            merged["detected_type"]          = new_type
            merged["category"]               = new_cat
            merged["classification_override"] = True
            print(f"[IntakeAgent] Units classification override: {old_type!r} → {new_type!r}")

        prior_mfg_conf      = float(prior_specs.get("manufacturer_confidence") or 0)
        prior_part_conf     = float(prior_specs.get("part_id_confidence") or 0)
        extracted_mfg_conf  = float(extracted.get("manufacturer_confidence") or 0)
        extracted_part_conf = float(extracted.get("part_id_confidence") or 0)
        # Confidence is monotonic: answering a clarification question can only add information,
        # not remove previously-established confidence.
        mfg_conf  = max(prior_mfg_conf, extracted_mfg_conf)
        part_conf = max(prior_part_conf, extracted_part_conf)
        # Write floor-corrected values back so next turn's prior_specs has correct baseline
        merged["manufacturer_confidence"] = mfg_conf
        merged["part_id_confidence"]      = part_conf

        if force_proceed:
            return {
                "asset_specs":             merged,
                "manufacturer_confidence": mfg_conf,
                "part_id_confidence":      part_conf,
                "sufficient":              True,
                "follow_up_question":      None,
                "manufacturer_caveat":     None,
                "confidence_summary": {
                    "manufacturer_confidence": mfg_conf,
                    "part_id_confidence":      part_conf,
                    "forced":                  True,
                    "reasoning":               extracted.get("confidence_reasoning"),
                    "proceed_state":           "forced",
                    "caveat":                  None,
                },
            }

        # Fix 3: asymmetric stop condition
        state, missing_field, caveat = assess_proceed_state(merged, mfg_conf, part_conf)
        sufficient = state in ("proceed_full_confidence", "proceed_with_manufacturer_caveat")

        follow_up = None
        if not sufficient:
            follow_up = self._generate_clarification(merged, missing_field or "detected_type")

        return {
            "asset_specs":             merged,
            "manufacturer_confidence": mfg_conf,
            "part_id_confidence":      part_conf,
            "sufficient":              sufficient,
            "follow_up_question":      follow_up,
            "manufacturer_caveat":     caveat,
            "confidence_summary": {
                "manufacturer_confidence": mfg_conf,
                "part_id_confidence":      part_conf,
                "missing_field":           missing_field,
                "reasoning":               extracted.get("confidence_reasoning"),
                "proceed_state":           state,
                "caveat":                  caveat,
            },
        }

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _build_context_summary(self, prior_specs: dict) -> dict:
        """Return all populated prior spec fields including confidence scores.

        Confidence scores are intentionally included so the LLM knows what's already
        established and can score new information relative to that baseline.
        """
        return {
            k: v for k, v in prior_specs.items()
            if not isinstance(v, (list, dict)) and v not in _NULL_VALUES
        }

    def _pn_prefix_hint(self, text: str, prior_specs: dict) -> Optional[tuple]:
        """Scan user text and prior specs for a known PN prefix.

        Returns (manufacturer, confidence) if a match is found, None otherwise.
        Checks prior_specs.part_number first (most reliable), then raw text tokens.
        """
        from utils.brand_intelligence import lookup_manufacturer_from_pn

        existing_pn = prior_specs.get("part_number")
        if existing_pn and existing_pn not in _NULL_VALUES:
            mfg = lookup_manufacturer_from_pn(existing_pn)
            if mfg:
                return (mfg, 92)

        tokens = re.findall(r'\b[A-Z0-9][A-Z0-9\-\.]{3,}\b', text.upper())
        for token in tokens:
            mfg = lookup_manufacturer_from_pn(token)
            if mfg:
                return (mfg, 92)

        return None

    def _extract_text(self, text: str, prior_specs: dict, prior_question: str | None = None) -> dict:
        if not self._api_key:
            return self._fallback_extract(prior_specs)

        pn_hint = self._pn_prefix_hint(text, prior_specs)

        context = ""
        if prior_specs:
            summary = self._build_context_summary(prior_specs)
            if prior_question:
                context = (
                    f"Previously extracted specs:\n{json.dumps(summary)}\n\n"
                    f"Agent asked: \"{prior_question}\"\n"
                    f"User replied: "
                )
            else:
                context = f"Previously extracted specs:\n{json.dumps(summary)}\n\nUser input: "

        if pn_hint:
            mfg_name, mfg_conf = pn_hint
            print(f"[IntakeAgent] PN prefix match → manufacturer={mfg_name!r} conf={mfg_conf}")
            hint_prefix = (
                f"SYSTEM NOTE: The part number prefix matches our records. "
                f"This part is manufactured by {mfg_name}. "
                f"Set manufacturer={mfg_name!r} and manufacturer_confidence={mfg_conf}.\n\n"
            )
            context = hint_prefix + context

        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":           self._api_key,
                    "anthropic-version":   "2023-06-01",
                    "content-type":        "application/json",
                },
                json={
                    "model":      "claude-sonnet-4-6",
                    "max_tokens": 1024,
                    "system":     _EXTRACTION_SYSTEM,
                    "messages":   [{"role": "user", "content": f"{context}{text}"}],
                },
                timeout=30,
            )
            resp.raise_for_status()
            extracted = self._parse_llm_json(resp.json()["content"][0]["text"])
            # If we have a high-confidence prefix match and the LLM returned a different
            # or absent manufacturer, override — prefix lookup is more reliable than
            # LLM inference for known product families.
            if pn_hint:
                mfg_name, mfg_conf = pn_hint
                llm_mfg = extracted.get("manufacturer") or ""
                llm_conf = float(extracted.get("manufacturer_confidence") or 0)
                if llm_mfg in _NULL_VALUES or llm_conf < mfg_conf:
                    extracted["manufacturer"] = mfg_name
                    extracted["manufacturer_confidence"] = mfg_conf
            return extracted
        except Exception as exc:
            print(f"[IntakeAgent] Text extraction failed: {exc}")
            return self._fallback_extract(prior_specs)

    def _extract_multimodal(self, text: str, images: list, prior_specs: dict) -> dict:
        if not self._api_key:
            return self._fallback_extract(prior_specs)

        pn_hint = self._pn_prefix_hint(text, prior_specs)

        content: list = []

        for img_bytes in images[:4]:
            media_type = _detect_media_type(img_bytes)
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            content.append({
                "type":   "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            })

        context = ""
        if prior_specs:
            summary = {k: v for k, v in prior_specs.items()
                       if k not in ("manufacturer_confidence", "part_id_confidence",
                                    "confidence_reasoning") and v not in _NULL_VALUES}
            context = f"Previously extracted specs:\n{json.dumps(summary)}\n\n"

        hint_prefix = ""
        if pn_hint:
            mfg_name, mfg_conf = pn_hint
            print(f"[IntakeAgent] PN prefix match (multimodal) → manufacturer={mfg_name!r} conf={mfg_conf}")
            hint_prefix = (
                f"SYSTEM NOTE: The part number prefix matches our records. "
                f"This part is manufactured by {mfg_name}. "
                f"Set manufacturer={mfg_name!r} and manufacturer_confidence={mfg_conf}.\n\n"
            )

        content.append({
            "type": "text",
            "text": (
                f"{hint_prefix}{context}Extract all equipment/part specifications visible in the "
                f"image(s) and from the text below.\n\n{text or '(no additional text)'}"
            ),
        })

        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":           self._api_key,
                    "anthropic-version":   "2023-06-01",
                    "content-type":        "application/json",
                },
                json={
                    "model":      "claude-sonnet-4-6",
                    "max_tokens": 1024,
                    "system":     _EXTRACTION_SYSTEM,
                    "messages":   [{"role": "user", "content": content}],
                },
                timeout=30,
            )
            resp.raise_for_status()
            extracted = self._parse_llm_json(resp.json()["content"][0]["text"])
            if pn_hint:
                mfg_name, mfg_conf = pn_hint
                llm_mfg = extracted.get("manufacturer") or ""
                llm_conf = float(extracted.get("manufacturer_confidence") or 0)
                if llm_mfg in _NULL_VALUES or llm_conf < mfg_conf:
                    extracted["manufacturer"] = mfg_name
                    extracted["manufacturer_confidence"] = mfg_conf
            return extracted
        except Exception as exc:
            detail = ""
            if hasattr(exc, "response") and exc.response is not None:
                detail = f" — API response: {exc.response.text[:300]}"
            print(f"[IntakeAgent] Multimodal extraction failed: {exc}{detail}")
            return self._fallback_extract(prior_specs)

    def _fallback_extract(self, prior_specs: dict) -> dict:
        return {
            **(prior_specs or {}),
            "manufacturer_confidence": 0,
            "part_id_confidence":      0,
            "confidence_reasoning":    "No API key — extraction skipped",
        }

    @staticmethod
    def _parse_llm_json(raw: str) -> dict:
        raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw)
        try:
            return json.loads(raw)
        except Exception:
            return {"manufacturer_confidence": 0, "part_id_confidence": 0,
                    "confidence_reasoning": "JSON parse failed"}

    # ------------------------------------------------------------------
    # Clarification generation
    # ------------------------------------------------------------------

    def _generate_clarification(self, specs: dict, missing_field: str) -> str:
        if not self._api_key:
            return _DEFAULT_QUESTIONS.get(missing_field,
                                          f"Can you provide the {missing_field.replace('_', ' ')}?")

        specs_summary = {k: v for k, v in specs.items()
                         if not isinstance(v, (list, dict)) and v not in _NULL_VALUES
                         and k not in ("manufacturer_confidence", "part_id_confidence",
                                       "confidence_reasoning")}
        user_msg = (
            f"Current specs: {json.dumps(specs_summary)}\n"
            f"Missing field: {missing_field}\n"
            f"Equipment type: {specs.get('detected_type', 'unknown')}\n\n"
            f"Ask one focused question to obtain the {missing_field.replace('_', ' ')} value."
        )

        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":           self._api_key,
                    "anthropic-version":   "2023-06-01",
                    "content-type":        "application/json",
                },
                json={
                    "model":      "claude-haiku-4-5-20251001",
                    "max_tokens": 150,
                    "system":     _CLARIFICATION_SYSTEM,
                    "messages":   [{"role": "user", "content": user_msg}],
                },
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"].strip()
        except Exception as exc:
            print(f"[IntakeAgent] Clarification generation failed: {exc}")
            return _DEFAULT_QUESTIONS.get(missing_field,
                                          f"Can you provide the {missing_field.replace('_', ' ')}?")
