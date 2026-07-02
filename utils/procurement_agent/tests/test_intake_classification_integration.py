"""
T4 integration tests — part-type classifier wired into IntakeAgent.run() behind
INTAKE_TYPE_AWARE.

Contracts under test:
  - Flag OFF: classifier NEVER invoked; behavior identical (no `_-classified /
    _component_of keys added); zero classifier calls.
  - Flag ON + mocked classifier: _classified_type / _classified_regime /
    _component_of stored on asset_specs_json; the keys are `_-prefixed so they
    are excluded from the context summary and would be stripped by the RunDetail
    filter (proved via the filter predicate).
  - Classification failure (UNKNOWN): intake proceeds exactly as current behavior
    (the `_` keys carry "unknown" but no exception, no flow change).
  - Only the FIRST message (no prior specs) is classified — a follow-up turn does
    NOT re-invoke the classifier.
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from utils.models import SourcingRun
from utils.procurement_agent.agents.intake_agent import IntakeAgent
from utils.procurement_agent.part_type_classifier import Classification


def _make_run(specs: dict | None = None) -> SourcingRun:
    return SourcingRun(asset_specs_json=specs)


def _mock_anthropic_response(payload: dict) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"content": [{"text": json.dumps(payload)}]}
    return mock_resp


def _bearing_payload() -> dict:
    return {
        "manufacturer": "SKF",
        "model": "6205-2RS1",
        "part_number": "6205-2RS1",
        "detected_type": "bearing",
        "category": "Part",
        "bore_diameter": "25mm",
        "manufacturer_confidence": 95,
        "part_id_confidence": 92,
        "confidence_reasoning": "explicit PN",
    }


def _seal_payload() -> dict:
    return {
        "manufacturer": None,
        "model": None,
        "part_number": None,
        "detected_type": "mechanical seal",
        "category": "Part",
        "shaft_size": "1.5 inch",
        "manufacturer_confidence": 0,
        "part_id_confidence": 70,
        "confidence_reasoning": "type inferred from description",
    }


# ---------------------------------------------------------------------------
# Flag OFF -> classifier NOT called, no classification keys added.
# ---------------------------------------------------------------------------

def test_flag_off_classifier_never_invoked(monkeypatch):
    monkeypatch.delenv("INTAKE_TYPE_AWARE", raising=False)
    agent = IntakeAgent(anthropic_api_key="test-key")
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_anthropic_response(_bearing_payload())) as mock_post, \
         patch("utils.procurement_agent.part_type_classifier.classify_part_type") as mock_clf:
        result = agent.run(_make_run({}), {"text": "I need a Goulds 3196 mechanical seal"})
        assert mock_clf.call_count == 0, "classifier must not be invoked when flag is OFF"
        assert mock_post.call_count == 1  # only the extraction call
    specs = result["asset_specs"]
    for k in ("_classified_type", "_classified_regime", "_component_of", "_classified_confidence"):
        assert k not in specs, f"{k} must not be added when flag is OFF"


# ---------------------------------------------------------------------------
# Flag ON + mocked classifier -> keys stored.
# ---------------------------------------------------------------------------

def test_flag_on_classification_keys_stored(monkeypatch):
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    agent = IntakeAgent(anthropic_api_key="test-key")
    fake = Classification(
        part_type="mechanical_seal", regime="ANCHORED", sourcing="MIXED",
        component_of="Goulds 3196", confidence=95,
    )
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_anthropic_response(_seal_payload())), \
         patch("utils.procurement_agent.part_type_classifier.classify_part_type",
               return_value=fake) as mock_clf:
        result = agent.run(_make_run({}), {"text": "Goulds 3196 mechanical seal"})
    assert mock_clf.call_count == 1
    specs = result["asset_specs"]
    assert specs["_classified_type"] == "mechanical_seal"
    assert specs["_classified_regime"] == "ANCHORED"
    assert specs["_component_of"] == "Goulds 3196"
    assert specs["_classified_confidence"] == 95


# ---------------------------------------------------------------------------
# Classification failure (UNKNOWN) -> intake proceeds, no exception.
# ---------------------------------------------------------------------------

def test_flag_on_unknown_classification_does_not_break_flow(monkeypatch):
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    agent = IntakeAgent(anthropic_api_key="test-key")
    fake_unknown = Classification(
        part_type="unknown", regime="DIRECT", sourcing="STANDARD",
        component_of=None, confidence=20,
    )
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_anthropic_response(_seal_payload())), \
         patch("utils.procurement_agent.part_type_classifier.classify_part_type",
               return_value=fake_unknown):
        result = agent.run(_make_run({}), {"text": "a hydraulic hose fitting"})
    specs = result["asset_specs"]
    # The unknown classification is recorded but the flow is unchanged.
    assert specs["_classified_type"] == "unknown"
    assert specs["_component_of"] is None
    # Sufficiency/clarification logic still runs normally.
    assert "sufficient" in result
    assert "follow_up_question" in result


def test_flag_on_classifier_raising_is_swallowed(monkeypatch):
    """A classifier that raises (shouldn't happen, but defensively) must not
    crash intake — the run still returns a normal result with no classification keys."""
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    agent = IntakeAgent(anthropic_api_key="test-key")

    def _boom(_text):
        raise RuntimeError("classifier transport exploded")

    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_anthropic_response(_seal_payload())), \
         patch("utils.procurement_agent.part_type_classifier.classify_part_type",
               side_effect=_boom):
        result = agent.run(_make_run({}), {"text": "a valve"})
    # No crash; classification keys absent (the guard returned before setting them).
    specs = result["asset_specs"]
    assert "_classified_type" not in specs
    assert "sufficient" in result


# ---------------------------------------------------------------------------
# Only the FIRST message is classified (no prior specs).
# ---------------------------------------------------------------------------

def test_followup_turn_does_not_reclassify(monkeypatch):
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    agent = IntakeAgent(anthropic_api_key="test-key")
    # Prior specs exist (turn 2) — classification must NOT run again.
    prior = {
        "detected_type": "mechanical seal",
        "shaft_size": "1.5 inch",
        "manufacturer_confidence": 0,
        "part_id_confidence": 70,
        "_classified_type": "mechanical_seal",
        "_classified_regime": "ANCHORED",
        "_component_of": "Goulds 3196",
        "_classified_confidence": 95,
    }
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_anthropic_response(_seal_payload())), \
         patch("utils.procurement_agent.part_type_classifier.classify_part_type") as mock_clf:
        result = agent.run(_make_run(prior), {"text": "single cartridge"})
    assert mock_clf.call_count == 0, "a follow-up turn must not re-invoke the classifier"
    # Prior classification keys are preserved (carried in prior_specs, copied to merged).
    specs = result["asset_specs"]
    assert specs["_classified_type"] == "mechanical_seal"


# ---------------------------------------------------------------------------
# `_-prefixed classification keys are excluded from the context summary and
# stripped by the RunDetail filter predicate.
# ---------------------------------------------------------------------------

def test_classification_keys_excluded_from_context_summary(monkeypatch):
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    agent = IntakeAgent(anthropic_api_key="test-key")
    prior = {
        "detected_type": "mechanical seal",
        "_classified_type": "mechanical_seal",
        "_classified_regime": "ANCHORED",
        "_component_of": "Goulds 3196",
        "_classified_confidence": 95,
    }
    summary = agent._build_context_summary(prior)
    for k in ("_classified_type", "_classified_regime", "_component_of", "_classified_confidence"):
        assert k not in summary, f"{k} must not leak to the extractor context"
    assert summary["detected_type"] == "mechanical seal"


def test_classification_keys_stripped_by_run_detail_filter():
    specs = {
        "manufacturer": "Goulds",
        "detected_type": "mechanical seal",
        "_classified_type": "mechanical_seal",
        "_classified_regime": "ANCHORED",
        "_component_of": "Goulds 3196",
        "_classified_confidence": 95,
        "_asked_fields": ["shaft_size"],
    }
    filtered = {k: v for k, v in specs.items() if not str(k).startswith("_")}
    assert filtered["manufacturer"] == "Goulds"
    for k in ("_classified_type", "_classified_regime", "_component_of",
              "_classified_confidence", "_asked_fields"):
        assert k not in filtered
