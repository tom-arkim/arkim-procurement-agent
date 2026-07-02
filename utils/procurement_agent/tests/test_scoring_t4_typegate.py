"""
T4 — the multiplicative confidence-aware TypeGate (behind SCORING_V2).

The anchor:
  - Zoro PUMP on a Goulds-seal request -> TypeGate ~0.1 -> final WELL below the
    30 floor (FAILS). A wrong-part-from-a-big-vendor can no longer beat a
    right-part-from-a-specialist.
  - Platinum SEAL on a seal request -> TypeGate ~1.0 -> final above the 30
    floor (PASSES).
  - Undetectable result -> 0.45 floor (never zeroed).
  - Flag-off -> byte-identical to pre-T4 (the gate path only runs under
    SCORING_V2).
"""

import pytest

from utils.sourcing_archieved import scoring as scoring_mod
from utils.sourcing_archieved.scoring import (
    _compute_suitability_score,
    _type_gate,
    _TYPE_GATE_MATCH_HIGH,
    _TYPE_GATE_MATCH_LOW,
    _TYPE_GATE_RESULT_UNDETECTABLE,
    _TYPE_GATE_QUERY_UNDETECTABLE,
    _TYPE_GATE_DIFFERENT,
)
from utils.models import AssetSpecs


TIER_FLOOR = 30.0


def _seal_request() -> AssetSpecs:
    return AssetSpecs(
        manufacturer="Goulds", model="3196", part_number="UNKNOWN-PN",
        voltage="N/A", category="Part", detected_type="mechanical seal",
    )


def _seal_result() -> tuple[str, str, str]:
    snippet = ("Platinum Performance Products Goulds 3196 mechanical seal, "
               "Type 1, 1.375 inch. Part ST-1.375-T1. In stock, ships same day. "
               "Cross-reference / interchange for Goulds 3196.")
    url = "https://platinumperformanceproducts.com/mechanical-seals/goulds/3196-st"
    title = "Goulds 3196 Mechanical Seal - Platinum Performance Products"
    return snippet, url, title


def _pump_result() -> tuple[str, str, str]:
    snippet = "Goulds 3196 centrifugal pump, 5HP, 3450 RPM. In stock at Zoro."
    url = "https://zoro.com/pump/centrifugal/goulds-3196/i/"
    title = "Goulds 3196 Centrifugal Pump"
    return snippet, url, title


# ---------------------------------------------------------------------------
# _type_gate unit tests
# ---------------------------------------------------------------------------

class TestTypeGateUnit:
    def test_different_class_low_gate(self):
        assert _type_gate("SEAL", "PUMP", "centrifugal pump", "https://zoro.com/pump/centrifugal", None) == _TYPE_GATE_DIFFERENT

    def test_same_class_high_confidence_url_and_text_agree(self):
        snippet, url, title = _seal_result()
        assert _type_gate("SEAL", "SEAL", snippet, url, title) == _TYPE_GATE_MATCH_HIGH

    def test_same_class_low_confidence_only_url(self):
        # URL says SEAL, but the text has no seal noun (only the URL signal agrees).
        url = "https://vendor.com/mechanical-seals/goulds/3196"
        assert _type_gate("SEAL", "SEAL", "Goulds 3196 replacement part", url, None) == _TYPE_GATE_MATCH_LOW

    def test_result_undetectable_floor(self):
        assert _type_gate("SEAL", None, "generic part", "https://vendor.com/products/sku-1", None) == _TYPE_GATE_RESULT_UNDETECTABLE

    def test_query_undetectable_neutral(self):
        assert _type_gate(None, "PUMP", "centrifugal pump", "https://zoro.com/pump", None) == _TYPE_GATE_QUERY_UNDETECTABLE

    def test_gate_never_zero(self):
        """Every gate value is > 0 (ESCI lesson — never zero a possibly-correct result)."""
        assert _TYPE_GATE_DIFFERENT > 0
        assert _TYPE_GATE_RESULT_UNDETECTABLE > 0
        assert _TYPE_GATE_MATCH_LOW > 0


# ---------------------------------------------------------------------------
# THE ANCHOR — Zoro pump fails, Platinum seal passes
# ---------------------------------------------------------------------------

class TestTypeGateAnchor:
    def test_zoro_pump_fails_floor_under_scoring_v2(self, monkeypatch):
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        snippet, url, title = _pump_result()
        score = _compute_suitability_score(_seal_request(), snippet, url, found_pn=None, title=title)
        assert score < TIER_FLOOR, (
            f"Zoro pump must FAIL the {TIER_FLOOR} floor under SCORING_V2, got {score}"
        )
        # The gate crushes it well below the floor, not just barely under.
        assert score <= 10.0, f"pump should be crushed by the ~0.1 gate, got {score}"

    def test_platinum_seal_passes_floor_under_scoring_v2(self, monkeypatch):
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        snippet, url, title = _seal_result()
        score = _compute_suitability_score(_seal_request(), snippet, url, found_pn="ST-1.375-T1", title=title)
        assert score >= TIER_FLOOR, (
            f"Platinum seal must PASS the {TIER_FLOOR} floor under SCORING_V2, got {score}"
        )

    def test_seal_beats_pump_under_scoring_v2(self, monkeypatch):
        """The headline invariant: right-part-specialist beats wrong-part-marketplace."""
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        s_snippet, s_url, s_title = _seal_result()
        p_snippet, p_url, p_title = _pump_result()
        seal_score = _compute_suitability_score(_seal_request(), s_snippet, s_url, found_pn="ST-1.375-T1", title=s_title)
        pump_score = _compute_suitability_score(_seal_request(), p_snippet, p_url, found_pn=None, title=p_title)
        assert seal_score > pump_score


# ---------------------------------------------------------------------------
# Undetectable result -> 0.45 floor (never zeroed)
# ---------------------------------------------------------------------------

class TestUndetectableFloor:
    def test_undetectable_result_not_zeroed(self, monkeypatch):
        """A result whose type is undetectable (no URL slug, no title noun) must
        get the 0.45 gate floor, NOT zero — so a possibly-correct specialist
        result with a thin URL/title isn't cut."""
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        # URL has no category slug; snippet has the mfg + stockist but NO noun
        # the classifier can read -> result_cls None -> 0.45 gate.
        snippet = "Goulds 3196 replacement component. In stock, ships same day. Cross-reference."
        url = "https://specialistvendor.com/products/sku-7788"
        score = _compute_suitability_score(_seal_request(), snippet, url, found_pn="ST-1.375-T1", title=None)
        # Without the gate the additive base would be ~45 (pn_pts=0 cap); with the
        # 0.45 floor it lands ~20. Assert it's NOT zeroed and reflects the floor.
        assert score > 0.0
        assert score < TIER_FLOOR  # undetectable should NOT blindly pass either
        # Sanity: the 0.45 gate was actually applied (query SEAL detected, result None).
        assert scoring_mod._last_noun_classes["query"] == "SEAL"
        assert scoring_mod._last_noun_classes["result"] is None


# ---------------------------------------------------------------------------
# Inertness — flag-off byte-identical to pre-T4
# ---------------------------------------------------------------------------

class TestTypeGateInertness:
    def test_flag_off_uses_legacy_additive(self, monkeypatch):
        """Flag-off: the gate path is not entered; the pump scores its legacy
        additive ~40 (ABOVE the floor) — proving the gate is what fails it, and
        that flag-off scoring is unchanged from pre-T4 behavior."""
        monkeypatch.setattr(scoring_mod, "SCORING_V2", False)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        snippet, url, title = _pump_result()
        score = _compute_suitability_score(_seal_request(), snippet, url, found_pn=None, title=title)
        # Legacy: pn_pts=0, type_pts=0, mfg=10, auth=20, url=10 => 40, capped at 45 => 40.
        assert score == 40.0, f"flag-off pump should be the legacy 40, got {score}"

    def test_flag_off_clean_pn_legacy_unchanged(self, monkeypatch):
        """Flag-off clean-PN bearing is the legacy additive score (95) — proving
        the flag-off path is byte-identical to pre-T4. The flag-on path differs
        ONLY by the intended T4 authority cap (auth 20->10, so 95->85) with the
        type-match gate at 1.0; the bearing still passes strongly."""
        specs = AssetSpecs(
            manufacturer="SKF", model="6205", part_number="6205-2RS",
            voltage="N/A", category="Part", detected_type="ball bearing",
        )
        snippet = "SKF 6205-2RS deep groove ball bearing. In stock, ships today."
        url = "https://mrosupply.com/bearings/skf-6205-2rs/"
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        score_on = _compute_suitability_score(specs, snippet, url, found_pn="6205-2RS")
        monkeypatch.setattr(scoring_mod, "SCORING_V2", False)
        score_off = _compute_suitability_score(specs, snippet, url, found_pn="6205-2RS")
        # Flag-off is the unchanged legacy score.
        assert score_off == 95.0, f"flag-off legacy bearing should be 95, got {score_off}"
        # Flag-on: gate is 1.0 (type matches), the only delta is the auth cap (-10).
        assert score_on == 85.0, (
            f"flag-on bearing should be 95-10(auth cap)=85 with gate 1.0, got {score_on}"
        )
        # The bearing still clears the floor comfortably — no regression in the
        # pass/fail verdict, which is what matters for clean-PN cases.
        assert score_on >= TIER_FLOOR
