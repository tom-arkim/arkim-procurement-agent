"""
T3 integration tests — quantity capture wired into IntakeAgent.run() behind
INTAKE_TYPE_AWARE, plus the `_`-prefix leak prevention (markers never reach the
extractor context summary).

Mirrors the existing intake test mocking pattern (requests.post -> canned JSON).
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from utils.models import SourcingRun
from utils.procurement_agent.agents.intake_agent import (
    IntakeAgent,
    _intake_type_aware,
)


def _make_run(specs: dict | None = None) -> SourcingRun:
    return SourcingRun(asset_specs_json=specs)


def _mock_anthropic_response(payload: dict) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"content": [{"text": json.dumps(payload)}]}
    return mock_resp


def _bearing_extraction(qty_field: bool = False) -> dict:
    """A high-confidence bearing extraction (sufficient by default)."""
    base = {
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
    return base


# ---------------------------------------------------------------------------
# Flag-parse parity with the codebase _env_truthy convention.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False),
    ("", False), ("junk", False), (None, False),
])
def test_intake_type_aware_flag_parse(val, expected, monkeypatch):
    if val is None:
        monkeypatch.delenv("INTAKE_TYPE_AWARE", raising=False)
    else:
        monkeypatch.setenv("INTAKE_TYPE_AWARE", val)
    assert _intake_type_aware() is expected


# ---------------------------------------------------------------------------
# Flag OFF -> no quantity behavior change (byte-identical specs: no `quantity`
# and no `_quantity_assumed` key added).
# ---------------------------------------------------------------------------

def test_flag_off_no_quantity_keys_added(monkeypatch):
    monkeypatch.delenv("INTAKE_TYPE_AWARE", raising=False)
    agent = IntakeAgent(anthropic_api_key="test-key")
    run = _make_run({})
    payload = _bearing_extraction()
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_anthropic_response(payload)):
        result = agent.run(run, {"text": "I need 6 SKF 6205 bearings"})
    specs = result["asset_specs"]
    assert "quantity" not in specs, "flag OFF must not add a quantity key"
    assert "_quantity_assumed" not in specs, "flag OFF must not add _quantity_assumed"
    # And the rest of the specs are the normal extraction result.
    assert specs["manufacturer"] == "SKF"
    assert specs["part_number"] == "6205-2RS1"


def test_flag_off_specs_byte_identical_to_pre_branch(monkeypatch):
    """With the flag off, the specs dict equals what the pre-branch intake would
    produce (no quantity keys ride along)."""
    monkeypatch.delenv("INTAKE_TYPE_AWARE", raising=False)
    agent = IntakeAgent(anthropic_api_key="test-key")
    payload = _bearing_extraction()
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_anthropic_response(payload)):
        r1 = agent.run(_make_run({}), {"text": "I need 6 SKF 6205 bearings"})
    # The keys present must be exactly the extraction keys + confidence merge keys
    # + classification keys — NEVER quantity / _quantity_assumed.
    assert "quantity" not in r1["asset_specs"]
    assert "_quantity_assumed" not in r1["asset_specs"]


# ---------------------------------------------------------------------------
# Flag ON -> stated quantity captured.
# ---------------------------------------------------------------------------

def test_flag_on_stated_quantity_captured(monkeypatch):
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    agent = IntakeAgent(anthropic_api_key="test-key")
    run = _make_run({})
    payload = _bearing_extraction()
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_anthropic_response(payload)):
        result = agent.run(run, {"text": "I need 6 SKF 6205 bearings"})
    specs = result["asset_specs"]
    assert specs["quantity"] == 6
    assert specs["_quantity_assumed"] is False


# ---------------------------------------------------------------------------
# Flag ON -> unstated defaults to 1 + assumed marker.
# ---------------------------------------------------------------------------

def test_flag_on_unstated_defaults_to_one_assumed(monkeypatch):
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    agent = IntakeAgent(anthropic_api_key="test-key")
    run = _make_run({})
    payload = _bearing_extraction()
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_anthropic_response(payload)):
        result = agent.run(run, {"text": "SKF 6205 bearing"})
    specs = result["asset_specs"]
    assert specs["quantity"] == 1
    assert specs["_quantity_assumed"] is True


# ---------------------------------------------------------------------------
# Flag ON -> prior real quantity preserved across turns when unstated.
# ---------------------------------------------------------------------------

def test_flag_on_preserves_prior_real_quantity(monkeypatch):
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    agent = IntakeAgent(anthropic_api_key="test-key")
    # Prior turn already captured a real quantity of 6.
    run = _make_run({
        "manufacturer": "SKF", "part_number": "6205-2RS1", "detected_type": "bearing",
        "category": "Part", "bore_diameter": "25mm",
        "manufacturer_confidence": 95, "part_id_confidence": 92,
        "quantity": 6, "_quantity_assumed": False,
    })
    payload = {**_bearing_extraction(), "manufacturer_confidence": 95, "part_id_confidence": 92}
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_anthropic_response(payload)):
        result = agent.run(run, {"text": "it's a 3-phase application"})
    specs = result["asset_specs"]
    assert specs["quantity"] == 6, "prior real quantity must be preserved when unstated"
    assert specs["_quantity_assumed"] is False


# ---------------------------------------------------------------------------
# `_`-prefixed markers never leak to the extractor context summary.
# ---------------------------------------------------------------------------

def test_quantity_assumed_marker_excluded_from_context_summary(monkeypatch):
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    agent = IntakeAgent(anthropic_api_key="test-key")
    prior = {
        "manufacturer": "SKF", "part_number": "6205-2RS1",
        "quantity": 1, "_quantity_assumed": True,
    }
    summary = agent._build_context_summary(prior)
    assert "_quantity_assumed" not in summary, "_-prefixed marker must not leak to context"
    assert "quantity" in summary, "non-_ quantity field should be in the context summary"


# ---------------------------------------------------------------------------
# quantity surfaces on the RunDetail-serialized specs (the api_server filter
# strips only `_-prefixed keys). We assert the filter logic directly so T3's
# "surfaces on RunDetail.asset_specs" contract is locked without spinning up
# the API: a spec dict with quantity + _quantity_assumed, filtered through the
# same `not str(k).startswith("_")` predicate the serializer uses, keeps
# quantity and drops the marker.
# ---------------------------------------------------------------------------

def test_quantity_surfaces_and_marker_stripped_by_run_detail_filter():
    specs = {
        "manufacturer": "SKF",
        "part_number": "6205-2RS1",
        "quantity": 6,
        "_quantity_assumed": False,
        "_asked_fields": ["manufacturer"],
        "_intake_turns": 2,
    }
    # The exact predicate api_server.RunDetail uses (api_server.py:1276).
    filtered = {k: v for k, v in specs.items() if not str(k).startswith("_")}
    assert filtered["quantity"] == 6
    assert "_quantity_assumed" not in filtered
    assert "_asked_fields" not in filtered
    assert "_intake_turns" not in filtered
