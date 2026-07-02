"""
Acceptance tests for the part-type classifier (T2).

The classifier is mock-verified: tests inject an `llm_call` and assert the
parsed Classification, the UNKNOWN fallback on malformed/invalid output, the
component_of capture for the F1 Goulds case, and that NO real network call
occurs (the default transport is never invoked when an llm_call is injected,
and a socket-level probe proves the default path opens no socket when no key is
present).
"""

from __future__ import annotations

import json
import socket
from unittest.mock import patch, MagicMock

import pytest

from utils.procurement_agent.part_type_classifier import (
    Classification,
    classify_part_type,
    _parse_classification,
    _default_llm_call,
)
from utils.procurement_agent.part_type_registry import (
    KNOWN_PART_TYPES,
    UNKNOWN_PART_TYPE,
)


# ---------------------------------------------------------------------------
# Helpers — a fake llm_call that returns canned raw text.
# ---------------------------------------------------------------------------

def _llm_returning(raw: str):
    """An llm_call double that always returns `raw` (and records that it ran)."""
    calls = []

    def _call(system_prompt: str, user_message: str) -> str:
        calls.append((system_prompt, user_message))
        return raw

    _call.calls = calls  # type: ignore[attr-defined]
    return _call


# ---------------------------------------------------------------------------
# Mocked-LLM returns valid JSON -> parsed Classification (regime/sourcing from registry).
# ---------------------------------------------------------------------------

def test_valid_json_parses_to_classification():
    raw = json.dumps({
        "part_type": "valve",
        "component_of": None,
        "confidence": 92,
    })
    result = classify_part_type("2 inch stainless ball valve", llm_call=_llm_returning(raw))
    assert isinstance(result, Classification)
    assert result.part_type == "valve"
    assert result.regime == "DIRECT"
    assert result.sourcing == "STANDARD"
    assert result.component_of is None
    assert result.confidence == 92


@pytest.mark.parametrize("ptype,expected_regime,expected_sourcing", [
    ("mechanical_seal", "ANCHORED", "MIXED"),
    ("pump", "DIRECT", "MIXED"),
    ("valve", "DIRECT", "STANDARD"),
    ("sensor_instrument", "DIRECT", "OEM"),
    ("motor_drive", "DIRECT", "STANDARD"),
])
def test_regime_and_sourcing_resolved_from_registry(ptype, expected_regime, expected_sourcing):
    raw = json.dumps({"part_type": ptype, "component_of": None, "confidence": 90})
    result = classify_part_type("x", llm_call=_llm_returning(raw))
    assert result.part_type == ptype
    assert result.regime == expected_regime
    assert result.sourcing == expected_sourcing


# ---------------------------------------------------------------------------
# Malformed JSON -> UNKNOWN fallback (no exception).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_raw", [
    "not json at all",
    "{",
    "```{bad```",
    "",
    "null",
    "[]",
    "42",
])
def test_malformed_json_yields_unknown_no_exception(bad_raw):
    result = classify_part_type("anything", llm_call=_llm_returning(bad_raw))
    assert result.part_type == UNKNOWN_PART_TYPE
    assert result.component_of is None
    assert result.confidence == 0


def test_invalid_part_type_string_yields_unknown():
    raw = json.dumps({"part_type": "flux_capacitor", "component_of": None, "confidence": 99})
    result = classify_part_type("x", llm_call=_llm_returning(raw))
    assert result.part_type == UNKNOWN_PART_TYPE


def test_missing_part_type_key_yields_unknown():
    raw = json.dumps({"component_of": None, "confidence": 80})
    result = classify_part_type("x", llm_call=_llm_returning(raw))
    assert result.part_type == UNKNOWN_PART_TYPE


def test_explicit_unknown_string_passes_through():
    raw = json.dumps({"part_type": "unknown", "component_of": None, "confidence": 30})
    result = classify_part_type("a hydraulic hose", llm_call=_llm_returning(raw))
    assert result.part_type == UNKNOWN_PART_TYPE


# ---------------------------------------------------------------------------
# component_of populated from a mocked Goulds-style response (the F1 case).
# ---------------------------------------------------------------------------

def test_component_of_captured_from_goulds_response():
    raw = json.dumps({
        "part_type": "mechanical_seal",
        "component_of": "Goulds 3196",
        "confidence": 95,
    })
    result = classify_part_type("Goulds 3196 mechanical seal", llm_call=_llm_returning(raw))
    assert result.part_type == "mechanical_seal"
    assert result.regime == "ANCHORED"
    assert result.component_of == "Goulds 3196"
    assert result.confidence == 95


def test_component_of_null_normalizes_to_none():
    raw = json.dumps({"part_type": "pump", "component_of": None, "confidence": 88})
    result = classify_part_type("Grundfos CR32-5", llm_call=_llm_returning(raw))
    assert result.component_of is None


def test_component_of_empty_string_normalizes_to_none():
    raw = json.dumps({"part_type": "pump", "component_of": "   ", "confidence": 88})
    result = classify_part_type("x", llm_call=_llm_returning(raw))
    assert result.component_of is None


def test_component_of_non_string_yields_none():
    raw = json.dumps({"part_type": "pump", "component_of": 12345, "confidence": 88})
    result = classify_part_type("x", llm_call=_llm_returning(raw))
    assert result.component_of is None


# ---------------------------------------------------------------------------
# Confidence coercion + clamping.
# ---------------------------------------------------------------------------

def test_confidence_clamped_to_100():
    raw = json.dumps({"part_type": "valve", "component_of": None, "confidence": 250})
    result = classify_part_type("x", llm_call=_llm_returning(raw))
    assert result.confidence == 100


def test_confidence_negative_clamped_to_0():
    raw = json.dumps({"part_type": "valve", "component_of": None, "confidence": -10})
    result = classify_part_type("x", llm_call=_llm_returning(raw))
    assert result.confidence == 0


def test_confidence_string_numeric_coerced():
    raw = json.dumps({"part_type": "valve", "component_of": None, "confidence": "85"})
    result = classify_part_type("x", llm_call=_llm_returning(raw))
    assert result.confidence == 85


def test_confidence_missing_defaults_to_0():
    raw = json.dumps({"part_type": "valve", "component_of": None})
    result = classify_part_type("x", llm_call=_llm_returning(raw))
    assert result.part_type == "valve"
    assert result.confidence == 0


# ---------------------------------------------------------------------------
# Empty message -> UNKNOWN without invoking the LLM.
# ---------------------------------------------------------------------------

def test_empty_message_yields_unknown_without_llm_call():
    called = []

    def _call(s, u):
        called.append(True)
        return json.dumps({"part_type": "valve"})

    result = classify_part_type("", llm_call=_call)
    assert result.part_type == UNKNOWN_PART_TYPE
    assert called == [], "llm_call must not be invoked for an empty message"


# ---------------------------------------------------------------------------
# An llm_call that RAISES must not blow up the pipeline -> UNKNOWN.
# ---------------------------------------------------------------------------

def test_llm_call_raising_yields_unknown_no_exception():
    def _boom(s, u):
        raise RuntimeError("network explosion")
    result = classify_part_type("a valve", llm_call=_boom)
    assert result.part_type == UNKNOWN_PART_TYPE


# ---------------------------------------------------------------------------
# NO real network call: with an injected llm_call, the default transport
# (requests.post) is never touched; and the default path itself opens no socket
# when ANTHROPIC_API_KEY is absent (it short-circuits to "" -> UNKNOWN).
# ---------------------------------------------------------------------------

def test_injected_llm_call_never_touches_requests_post():
    raw = json.dumps({"part_type": "valve", "component_of": None, "confidence": 90})
    with patch("utils.procurement_agent.part_type_classifier.requests.post") as mock_post:
        result = classify_part_type("2 inch ball valve", llm_call=_llm_returning(raw))
        assert result.part_type == "valve"
        assert mock_post.call_count == 0, "requests.post must not be called when llm_call is injected"


def test_default_llm_call_no_key_opens_no_socket(monkeypatch):
    """With ANTHROPIC_API_KEY unset, the default path short-circuits to '' (no socket)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class _ProbedSocket:
        def __init__(self, *a, **k):
            raise AssertionError("default llm_call opened a socket with no API key")

    orig = socket.socket
    socket.socket = _ProbedSocket  # type: ignore[assignment]
    try:
        result = classify_part_type("a valve")  # default llm_call
    finally:
        socket.socket = orig  # type: ignore[assignment]
    assert result.part_type == UNKNOWN_PART_TYPE


def test_default_llm_call_never_reads_anthropic_base_url(monkeypatch):
    """The default call must hit the hardcoded api.anthropic.com — never ANTHROPIC_BASE_URL."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://EVIL-PROXY.example.com")
    captured = {}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": json.dumps({"part_type": "valve", "component_of": None, "confidence": 90})}]}

    def _fake_post(url, **kwargs):
        captured["url"] = url
        return _FakeResp()

    with patch("utils.procurement_agent.part_type_classifier.requests.post", side_effect=_fake_post):
        result = classify_part_type("a valve")
    assert result.part_type == "valve"
    assert "api.anthropic.com" in captured["url"]
    assert "EVIL-PROXY" not in captured["url"], "default call must NOT honor ANTHROPIC_BASE_URL"


# ---------------------------------------------------------------------------
# as_dict serialization for tracing/logging.
# ---------------------------------------------------------------------------

def test_as_dict_roundtrip():
    raw = json.dumps({"part_type": "mechanical_seal", "component_of": "Goulds 3196", "confidence": 95})
    result = classify_part_type("x", llm_call=_llm_returning(raw))
    d = result.as_dict()
    assert d == {
        "part_type": "mechanical_seal",
        "regime": "ANCHORED",
        "sourcing": "MIXED",
        "component_of": "Goulds 3196",
        "confidence": 95,
    }


# ---------------------------------------------------------------------------
# F1 + observed-failure phrasings against a mocked classifier (the live-LLM
# accuracy is T9's job; here we only prove the parser handles each labeled shape).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected_type,expected_component_of", [
    (json.dumps({"part_type": "mechanical_seal", "component_of": "Goulds 3196", "confidence": 95}),
     "mechanical_seal", "Goulds 3196"),
    (json.dumps({"part_type": "valve", "component_of": None, "confidence": 90}),
     "valve", None),
    (json.dumps({"part_type": "mechanical_seal", "component_of": "Grundfos CR32", "confidence": 88}),
     "mechanical_seal", "Grundfos CR32"),
    (json.dumps({"part_type": "sensor_instrument", "component_of": None, "confidence": 91}),
     "sensor_instrument", None),
    (json.dumps({"part_type": "unknown", "component_of": None, "confidence": 20}),
     UNKNOWN_PART_TYPE, None),
])
def test_observed_failure_phrasings_parse(raw, expected_type, expected_component_of):
    result = classify_part_type("x", llm_call=_llm_returning(raw))
    assert result.part_type == expected_type
    assert result.component_of == expected_component_of
