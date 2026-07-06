"""
T3 — component-of parent-attribution eval: offline regression guard + T2 proof.

Mirrors the LIVE intake → sourcing query path deterministically so it gates
regressions in the suite (the live before/after is run separately via
scripts/intake_eval.py — a mocked LLM can't prove the T1 prompt change, only
the real LLM can).

Live-faithfulness (the codebase has been bitten twice by evals testing a path
production doesn't take): the specs fed to the query builder here are built by
the REAL SourcingAgent._dict_to_specs — the same promotion path the live
sourcing run uses to turn asset_specs_json (with the gated intake classifier's
`_component_of` internal key) into an AssetSpecs with `component_of` set. The
query is then built by the REAL _build_search_query (Tier 2's builder). No
re-implementation, no parallel path.

Contracts under test:
  - T2 proof (deterministic, fails pre-T2 / passes post-T2): a component-of
    part MISCLASSIFIED as Equipment still gets a component-led query
    ("mechanical seal for Goulds 3196 ..."), because the Equipment branch now
    honors component_of. Pre-T2 the Equipment branch ignored component_of and
    produced a parent-led query.
  - Part-branch defense-in-depth (passes both before and after T1/T2 — honest):
    a component-of part categorized Part gets a component-led query regardless
    of whether mfg/model are populated with the parent's OEM/model. Fix A
    (commit 1363b6f) already landed this; this test documents and locks it.
  - Promotion parity: _dict_to_specs promotes `_component_of` → component_of
    only under INTAKE_TYPE_AWARE; flag-off leaves component_of unset
    (byte-identical legacy behavior).
"""

from __future__ import annotations

import pytest

from utils.models import AssetSpecs
from utils.procurement_agent.agents.sourcing_agent import SourcingAgent
from utils.sourcing_archieved.tavily_client import _build_search_query
from utils.procurement_agent.component_query import is_bare_parent_query


# ---------------------------------------------------------------------------
# Live-faithful specs construction — use the REAL promotion path.
# ---------------------------------------------------------------------------

def _asset_specs_json(*, detected_type, category, component_of, mfg, model,
                     part_number="UNKNOWN-PN"):
    """The asset_specs_json shape the live intake pipeline produces for a
    component-of part: the classifier's `_component_of` internal key rides on
    the dict; SourcingAgent._dict_to_specs promotes it (under the flag)."""
    d = {
        "manufacturer":  mfg,
        "model":         model,
        "part_number":   part_number,
        "voltage":       "N/A",
        "category":      category,
        "detected_type": detected_type,
    }
    if component_of:
        d["_component_of"] = component_of
    return d


def _build_query_from_intake(specs_json: dict, search_mode: str = "exact") -> str:
    """The live Tier 2 query path: _dict_to_specs (promotion) → _build_search_query.

    Uses the REAL SourcingAgent._dict_to_specs so the `_component_of` →
    `component_of` promotion (gated on INTAKE_TYPE_AWARE) is identical to
    production. This is the path production takes — not a re-implementation.
    """
    specs = SourcingAgent()._dict_to_specs(specs_json)
    return _build_search_query(specs, search_mode=search_mode)


# ---------------------------------------------------------------------------
# T2 proof — Equipment branch honors component_of (fails pre-T2 / passes post-T2).
# A component-of part misclassified as Equipment by extraction. The intended
# classifier path only sets _component_of for mechanical_seal (categorized
# Part), so this is reached only via an LLM misclassification — but it's the
# last parent-led hole, and this test proves T2 closes it.
# ---------------------------------------------------------------------------

def test_t2_equipment_misclassified_component_led_query(monkeypatch):
    """Component-of part categorized Equipment → component-led query, not a
    bare-parent query. This FAILS on pre-T2 code (Equipment branch ignored
    component_of → 'industrial equipment Goulds 3196 price buy USA') and PASSES
    after T2. Proves T2 via the live _dict_to_specs → _build_search_query path."""
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    specs_json = _asset_specs_json(
        detected_type="mechanical seal", category="Equipment",
        component_of="Goulds 3196",
        mfg="Goulds", model="3196",  # parent attribution still present (T1 miss case)
    )
    q = _build_query_from_intake(specs_json)
    assert "mechanical seal for goulds 3196" in q.lower(), (
        f"Equipment-misclassified component-of part must lead with the "
        f"component-for-parent phrase, got {q!r}"
    )
    assert is_bare_parent_query(q, "Goulds 3196") is False, (
        f"must not be a bare-parent query: {q!r}"
    )


def test_t2_equipment_misclassified_no_component_of_is_legacy(monkeypatch):
    """Flag off (or no _component_of) → Equipment branch byte-identical to
    legacy (no component phrase). Guards against the T2 fix altering the
    non-component-of Equipment path."""
    monkeypatch.delenv("INTAKE_TYPE_AWARE", raising=False)
    specs_json = _asset_specs_json(
        detected_type="mechanical seal", category="Equipment",
        component_of=None, mfg="Goulds", model="3196",
    )
    q = _build_query_from_intake(specs_json)
    assert "mechanical seal for" not in q.lower(), (
        f"non-component-of Equipment query must not carry the component phrase: {q!r}"
    )
    assert "goulds 3196" in q.lower()


# ---------------------------------------------------------------------------
# Part-branch defense-in-depth — passes both before and after T1/T2 (honest).
# Fix A (commit 1363b6f) made the Part branch lead with detected_type /
# component_phrase regardless of mfg/model. This locks that behavior so a
# future regression (e.g. someone reverting Fix A) is caught. It is NOT a T1
# proof — the T1 proof is the live before/after (scripts/intake_eval.py).
# ---------------------------------------------------------------------------

def test_part_branch_component_led_even_when_mfg_model_populated(monkeypatch):
    """The 8-of-10 diagnosis case on main: a component-of Part with the parent's
    OEM/model populated (T1 miss). The Part branch leads with the component
    phrase regardless → component-led, never a bare-parent query. Passes with
    OR without T1 (Fix A landed the Part-branch fix) — honest defense-in-depth."""
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    specs_json = _asset_specs_json(
        detected_type="impeller", category="Part",
        component_of="Goulds 3196",
        mfg="Goulds Pumps", model="3196",  # parent attribution (pre-T1 / T1 miss)
    )
    q = _build_query_from_intake(specs_json)
    # component_of is set but impeller isn't a registry type → the classifier
    # would NOT set _component_of for an impeller (CLEANUP §7.5). This test
    # simulates the hypothetical where it IS set; the Part branch honors it.
    assert "impeller for goulds 3196" in q.lower(), (
        f"Part with component_of set must lead with the component-for-parent "
        f"phrase, got {q!r}"
    )
    assert is_bare_parent_query(q, "Goulds 3196") is False


def test_part_branch_detected_only_still_component_led(monkeypatch):
    """A non-seal component (impeller) with NO _component_of (the real current
    state — CLEANUP §7.5: ANCHORED is seal-only) but detected_type set. The Part
    branch leads with detected_type → component-led ('impeller ...'), never a
    bare-parent query even with the parent's mfg/model populated. This is the
    actual safety net keeping the 8-of-10 non-seal cases clean today."""
    monkeypatch.delenv("INTAKE_TYPE_AWARE", raising=False)  # no _component_of promotion
    specs_json = _asset_specs_json(
        detected_type="impeller", category="Part",
        component_of=None,
        mfg="Goulds Pumps", model="3196",  # parent attribution (pre-T1 / T1 miss)
    )
    q = _build_query_from_intake(specs_json)
    assert "impeller" in q.lower(), f"detected_type must lead the Part query: {q!r}"
    assert is_bare_parent_query(q, "Goulds 3196") is False, (
        f"detected_type-led Part query must not be bare-parent: {q!r}"
    )


# ---------------------------------------------------------------------------
# Promotion parity — _dict_to_specs promotes _component_of only under the flag.
# ---------------------------------------------------------------------------

def test_dict_to_specs_promotes_component_of_under_flag(monkeypatch):
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    specs_json = _asset_specs_json(
        detected_type="mechanical seal", category="Part",
        component_of="Goulds 3196", mfg="", model="",
    )
    specs = SourcingAgent()._dict_to_specs(specs_json)
    assert specs.component_of == "Goulds 3196"


def test_dict_to_specs_does_not_promote_component_of_flag_off(monkeypatch):
    monkeypatch.delenv("INTAKE_TYPE_AWARE", raising=False)
    specs_json = _asset_specs_json(
        detected_type="mechanical seal", category="Part",
        component_of="Goulds 3196", mfg="", model="",
    )
    specs = SourcingAgent()._dict_to_specs(specs_json)
    # Flag off → no promotion (byte-identical legacy sourcing). component_of is
    # either None or absent (AssetSpecs default None).
    assert getattr(specs, "component_of", None) is None
