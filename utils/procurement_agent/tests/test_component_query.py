"""
T5 acceptance tests — type-aware Q2 (a) + component-aware sourcing query (b).

(a) Type-aware Q2: a known classified type + no identity -> the next
    clarification question is the registry's q2_template, verbatim, with the
    asked-field ledger recorded and the turn cap still enforced. UNKNOWN -> the
    current generic question.
(b) Component-aware query: the F1 fixture — specs with component_of produce a
    query containing BOTH the component term and the parent identity, and NOT a
    bare parent query. Flag off -> query construction byte-identical to today.
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from utils.models import AssetSpecs, SourcingRun
from utils.procurement_agent.agents.intake_agent import IntakeAgent
from utils.procurement_agent.part_type_registry import get_profile
from utils.procurement_agent.component_query import (
    build_component_aware_query,
    is_bare_parent_query,
)


def _make_run(specs: dict | None = None) -> SourcingRun:
    return SourcingRun(asset_specs_json=specs)


def _mock_resp(payload: dict) -> MagicMock:
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = {"content": [{"text": json.dumps(payload)}]}
    return r


# ===========================================================================
# (a) Type-aware Q2 from the registry
# ===========================================================================

def _valve_specs_no_identity() -> dict:
    """A valve classification with NO identity — insufficient, needs clarification."""
    return {
        "detected_type": "valve",
        "category": "Part",
        "manufacturer_confidence": 0,
        "part_id_confidence": 60,
        "_classified_type": "valve",
        "_classified_regime": "DIRECT",
        "_component_of": None,
        "_classified_confidence": 90,
    }


def test_flag_on_known_type_no_identity_asks_q2_template(monkeypatch):
    """The F1 question-flow case: classified valve type, no identity -> next
    question is the valve q2_template verbatim."""
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    agent = IntakeAgent(anthropic_api_key="test-key")
    # Turn-2 follow-up: prior_specs carry the classification (no identity).
    prior = _valve_specs_no_identity()
    prior["_intake_turns"] = 0
    prior["_asked_fields"] = []
    # The extractor returns the same partial specs (still no identity).
    payload = {
        "detected_type": "valve", "category": "Part",
        "manufacturer_confidence": 0, "part_id_confidence": 60,
        "confidence_reasoning": "type only",
    }
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_resp(payload)):
        result = agent.run(_make_run(prior), {"text": "it's a ball valve"})
    q = result.get("follow_up_question") or ""
    expected = get_profile("valve").q2_template
    assert q == expected, f"expected the valve q2_template verbatim, got {q!r}"
    # The asked-field ledger records the q2 ask so it isn't repeated.
    assert "_q2_asked" in (result["asset_specs"].get("_asked_fields") or [])


def test_flag_on_q2_not_repeated_once_asked(monkeypatch):
    """Respects the _asked_fields de-dup: once q2 is asked, the next turn does
    not re-ask it (falls through to the generic picker)."""
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    agent = IntakeAgent(anthropic_api_key="test-key")
    prior = _valve_specs_no_identity()
    prior["_intake_turns"] = 1
    prior["_asked_fields"] = ["_q2_asked"]
    payload = {
        "detected_type": "valve", "category": "Part",
        "manufacturer_confidence": 0, "part_id_confidence": 60,
        "confidence_reasoning": "type only",
    }
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_resp(payload)):
        result = agent.run(_make_run(prior), {"text": "2 inch"})
    q = result.get("follow_up_question") or ""
    expected = get_profile("valve").q2_template
    assert q != expected, "q2 must not be repeated once already asked"


def test_flag_on_unknown_type_falls_through_to_generic(monkeypatch):
    """UNKNOWN classification -> the q2 path is skipped, current generic behavior."""
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    agent = IntakeAgent(anthropic_api_key="test-key")
    prior = {
        "detected_type": "", "category": "Part",
        "manufacturer_confidence": 0, "part_id_confidence": 40,
        "_classified_type": "unknown",
        "_intake_turns": 0, "_asked_fields": [],
    }
    payload = {
        "detected_type": "", "category": "Part",
        "manufacturer_confidence": 0, "part_id_confidence": 40,
        "confidence_reasoning": "unclear",
    }
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_resp(payload)):
        result = agent.run(_make_run(prior), {"text": "a thing"})
    q = result.get("follow_up_question") or ""
    # Must NOT be any registry q2_template (UNKNOWN has none).
    assert q != get_profile("valve").q2_template
    assert q != get_profile("mechanical_seal").q2_template


def test_flag_off_never_asks_q2_template(monkeypatch):
    """Flag off -> q2 path inactive; the generic identity-first opener / picker runs."""
    monkeypatch.delenv("INTAKE_TYPE_AWARE", raising=False)
    agent = IntakeAgent(anthropic_api_key="test-key")
    prior = {
        "detected_type": "valve", "category": "Part",
        "manufacturer_confidence": 0, "part_id_confidence": 60,
        # No _classified_type when flag off.
        "_intake_turns": 0, "_asked_fields": [],
    }
    payload = {
        "detected_type": "valve", "category": "Part",
        "manufacturer_confidence": 0, "part_id_confidence": 60,
        "confidence_reasoning": "type only",
    }
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_resp(payload)):
        result = agent.run(_make_run(prior), {"text": "a valve"})
    q = result.get("follow_up_question") or ""
    assert q != get_profile("valve").q2_template, "flag OFF must not ask the registry q2"
    assert "_q2_asked" not in (result["asset_specs"].get("_asked_fields") or [])


def test_q2_respects_turn_cap(monkeypatch):
    """When the turn counter is at the cap, the q2 path is bypassed and the
    intake commits (the cap is checked before the q2 branch)."""
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    agent = IntakeAgent(anthropic_api_key="test-key")
    prior = _valve_specs_no_identity()
    prior["_intake_turns"] = 2  # next turn (3) == INTAKE_TURN_CAP
    prior["_asked_fields"] = []
    payload = {
        "detected_type": "valve", "category": "Part",
        "manufacturer_confidence": 0, "part_id_confidence": 60,
        "confidence_reasoning": "type only",
    }
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_resp(payload)):
        result = agent.run(_make_run(prior), {"text": "valve"})
    # Cap reached -> commit, no follow-up question.
    assert result.get("follow_up_question") is None
    assert result.get("commit_message") is not None
    assert result["asset_specs"].get("spec_based_sourcing") is True


# ===========================================================================
# (b) Component-aware sourcing query — the F1 fixture
# ===========================================================================

def test_f1_component_aware_query_contains_component_and_parent():
    """The F1 fixture: mechanical seal for Goulds 3196 -> query has BOTH."""
    specs = AssetSpecs(
        manufacturer="Unknown", model="Unknown", part_number="UNKNOWN-PN",
        voltage="N/A", category="Part", detected_type="mechanical seal",
        component_of="Goulds 3196",
    )
    q = build_component_aware_query(specs)
    assert q is not None
    assert "mechanical seal" in q.lower()
    assert "goulds 3196" in q.lower()
    # And it is NOT a bare-parent query.
    assert is_bare_parent_query(q, "Goulds 3196") is False


def test_f1_bare_parent_query_is_flagged_as_bare():
    """The anti-pattern: a bare 'Goulds 3196' query must be flagged as bare-parent."""
    assert is_bare_parent_query("Goulds 3196", "Goulds 3196") is True
    assert is_bare_parent_query('"Goulds 3196" authorized distributor', "Goulds 3196") is True


def test_component_aware_query_none_when_no_parent():
    specs = AssetSpecs(
        manufacturer="SKF", model="6205", part_number="6205-2RS1",
        voltage="N/A", category="Part", detected_type="bearing",
        component_of=None,
    )
    assert build_component_aware_query(specs) is None


def test_component_aware_query_none_when_no_component_term():
    specs = AssetSpecs(
        manufacturer="Unknown", model="Unknown", part_number="UNKNOWN-PN",
        voltage="N/A", category="Part", detected_type=None,
        component_of="Goulds 3196",
    )
    # Falls back to category "part" -> "part for Goulds 3196" (still not bare parent).
    q = build_component_aware_query(specs)
    assert q is not None
    assert "goulds 3196" in q.lower()


# ---------------------------------------------------------------------------
# _build_tier3_query honors component_of (gated by promotion at the call site).
# Flag-off: specs.component_of is None (not promoted) -> query byte-identical.
# ---------------------------------------------------------------------------

def _specs_to_query_tier3(specs: AssetSpecs) -> str:
    from utils.sourcing_archieved.tavily_client import _build_tier3_query
    return _build_tier3_query(specs)


def test_tier3_query_component_aware_when_component_of_set():
    specs = AssetSpecs(
        manufacturer="Unknown", model="Unknown", part_number="UNKNOWN-PN",
        voltage="N/A", category="Part", detected_type="mechanical seal",
        component_of="Goulds 3196",
    )
    q = _specs_to_query_tier3(specs)
    assert "mechanical seal" in q.lower()
    assert "goulds 3196" in q.lower()
    assert is_bare_parent_query(q, "Goulds 3196") is False, (
        "tier3 query must not be a bare-parent query when component_of is set"
    )


def test_tier3_query_byte_identical_when_component_of_absent():
    """Flag-off equivalent: component_of None -> the query is unchanged from today."""
    specs = AssetSpecs(
        manufacturer="Unknown", model="Unknown", part_number="UNKNOWN-PN",
        voltage="N/A", category="Part", detected_type="mechanical seal",
        component_of=None,
    )
    specs_no_field = AssetSpecs(
        manufacturer="Unknown", model="Unknown", part_number="UNKNOWN-PN",
        voltage="N/A", category="Part", detected_type="mechanical seal",
    )
    assert _specs_to_query_tier3(specs) == _specs_to_query_tier3(specs_no_field)


# ---------------------------------------------------------------------------
# _build_aftermarket_query honors component_of.
# ---------------------------------------------------------------------------

def test_aftermarket_query_component_aware_when_component_of_set():
    from utils.sourcing_archieved.enterprise_search import _build_aftermarket_query
    specs = AssetSpecs(
        manufacturer="Unknown", model="Unknown", part_number="UNKNOWN-PN",
        voltage="N/A", category="Part", detected_type="mechanical seal",
        component_of="Goulds 3196",
    )
    q = _build_aftermarket_query(specs)
    assert "mechanical seal" in q.lower()
    assert "goulds 3196" in q.lower()
    assert is_bare_parent_query(q, "Goulds 3196") is False


def test_aftermarket_query_byte_identical_when_component_of_absent():
    from utils.sourcing_archieved.enterprise_search import _build_aftermarket_query
    base = dict(manufacturer="Unknown", model="Unknown", part_number="UNKNOWN-PN",
                voltage="N/A", category="Part", detected_type="mechanical seal")
    a = AssetSpecs(**base, component_of=None)
    b = AssetSpecs(**base)
    assert _build_aftermarket_query(a) == _build_aftermarket_query(b)


# ---------------------------------------------------------------------------
# SourcingAgent._dict_to_specs promotes _component_of under the flag.
# ---------------------------------------------------------------------------

def test_dict_to_specs_promotes_component_of_under_flag(monkeypatch):
    from utils.procurement_agent.agents.sourcing_agent import SourcingAgent
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    d = {
        "manufacturer": "Goulds", "model": "3196", "part_number": "UNKNOWN-PN",
        "voltage": "N/A", "detected_type": "mechanical seal",
        "_component_of": "Goulds 3196",
    }
    specs = SourcingAgent._dict_to_specs(d)
    assert specs.component_of == "Goulds 3196"


def test_dict_to_specs_does_not_promote_component_of_when_flag_off(monkeypatch):
    from utils.procurement_agent.agents.sourcing_agent import SourcingAgent
    monkeypatch.delenv("INTAKE_TYPE_AWARE", raising=False)
    d = {
        "manufacturer": "Goulds", "model": "3196", "part_number": "UNKNOWN-PN",
        "voltage": "N/A", "detected_type": "mechanical seal",
        "_component_of": "Goulds 3196",
    }
    specs = SourcingAgent._dict_to_specs(d)
    assert specs.component_of is None
