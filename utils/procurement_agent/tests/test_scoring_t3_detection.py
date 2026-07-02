"""
T3 — noun-class detection wired into the scorer (behind SCORING_V2).

Detection + storage only. No score change: flag-on and flag-off must produce
the SAME suitability float for the same inputs (the noun-class verdict is just
recorded on _last_noun_classes for T4's TypeGate to consume). Flag-off must NOT
invoke detection.
"""

import pytest

from utils.sourcing_archieved import scoring as scoring_mod
from utils.sourcing_archieved.scoring import (
    _compute_suitability_score,
    _detect_noun_classes,
    _query_noun_class,
    _result_noun_class,
    _last_noun_classes,
)
from utils.models import AssetSpecs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _seal_request() -> AssetSpecs:
    return AssetSpecs(
        manufacturer="Goulds",
        model="3196",
        part_number="UNKNOWN-PN",
        voltage="N/A",
        category="Part",
        detected_type="mechanical seal",
    )


def _seal_result_inputs() -> tuple[str, str]:
    snippet = ("Platinum Performance Products Goulds 3196 mechanical seal, "
               "Type 1, 1.375 inch. Part ST-1.375-T1. In stock, ships same day.")
    url = "https://platinumperformanceproducts.com/mechanical-seals/goulds/3196-st"
    return snippet, url


def _pump_result_inputs() -> tuple[str, str]:
    snippet = "Goulds 3196 centrifugal pump, 5HP. In stock at Zoro."
    url = "https://zoro.com/pump/centrifugal/goulds-3196/i/"
    return snippet, url


# ---------------------------------------------------------------------------
# _query_noun_class
# ---------------------------------------------------------------------------

class TestQueryNounClass:
    def test_seal_request_classifies_seal(self):
        assert _query_noun_class(_seal_request()) == "SEAL"

    def test_pump_request_classifies_pump(self):
        specs = AssetSpecs(
            manufacturer="Goulds", model="3196", part_number="G-3196",
            voltage="N/A", category="Equipment", detected_type="centrifugal pump",
        )
        assert _query_noun_class(specs) == "PUMP"

    def test_bearing_request_classifies_bearing(self):
        specs = AssetSpecs(
            manufacturer="SKF", model="6205", part_number="6205-2RS",
            voltage="N/A", category="Part", detected_type="ball bearing",
        )
        assert _query_noun_class(specs) == "BEARING"

    def test_no_detected_type_falls_back_to_description(self):
        specs = AssetSpecs(
            manufacturer="Acme", model="X1", part_number="X1",
            voltage="N/A", category="Part", detected_type=None,
            description="replacement mechanical seal for Acme X1",
        )
        assert _query_noun_class(specs) == "SEAL"

    def test_undetectable_returns_none(self):
        specs = AssetSpecs(
            manufacturer="Acme", model="X1", part_number="X1",
            voltage="N/A", category="Part", detected_type="industrial component",
            description="general purpose part",
        )
        assert _query_noun_class(specs) is None


# ---------------------------------------------------------------------------
# _result_noun_class
# ---------------------------------------------------------------------------

class TestResultNounClass:
    def test_seal_url_classifies_seal(self):
        snippet, url = _seal_result_inputs()
        assert _result_noun_class(snippet, url) == "SEAL"

    def test_pump_url_classifies_pump(self):
        snippet, url = _pump_result_inputs()
        assert _result_noun_class(snippet, url) == "PUMP"

    def test_title_used_when_url_undetectable(self):
        url = "https://vendor.com/products/sku-999"
        assert _result_noun_class("generic content", url, title="Goulds 3196 mechanical seal") == "SEAL"

    def test_ambiguous_thin_returns_none(self):
        url = "https://vendor.com/products/sku-999"
        assert _result_noun_class("Goulds 3196 replacement part", url) is None


# ---------------------------------------------------------------------------
# _detect_noun_classes — combined
# ---------------------------------------------------------------------------

class TestDetectNounClasses:
    def test_seal_query_seal_result(self):
        snippet, url = _seal_result_inputs()
        q, r = _detect_noun_classes(_seal_request(), snippet, url)
        assert q == "SEAL"
        assert r == "SEAL"

    def test_seal_query_pump_result(self):
        snippet, url = _pump_result_inputs()
        q, r = _detect_noun_classes(_seal_request(), snippet, url)
        assert q == "SEAL"
        assert r == "PUMP"


# ---------------------------------------------------------------------------
# Wiring: detection runs under SCORING_V2, NOT flag-off; score unchanged
# ---------------------------------------------------------------------------

class TestDetectionWiring:
    def test_detection_runs_flag_on(self, monkeypatch):
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        snippet, url = _seal_result_inputs()
        _compute_suitability_score(_seal_request(), snippet, url, found_pn="ST-1.375-T1")
        assert scoring_mod._last_noun_classes["query"] == "SEAL"
        assert scoring_mod._last_noun_classes["result"] == "SEAL"

    def test_detection_not_invoked_flag_off(self, monkeypatch):
        monkeypatch.setattr(scoring_mod, "SCORING_V2", False)
        snippet, url = _seal_result_inputs()
        # Pre-seed with a non-None value so we can prove flag-off clears it.
        scoring_mod._last_noun_classes["query"] = "STALE"
        scoring_mod._last_noun_classes["result"] = "STALE"
        _compute_suitability_score(_seal_request(), snippet, url, found_pn="ST-1.375-T1")
        assert scoring_mod._last_noun_classes["query"] is None
        assert scoring_mod._last_noun_classes["result"] is None

    def test_pump_result_detected_flag_on(self, monkeypatch):
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        snippet, url = _pump_result_inputs()
        _compute_suitability_score(_seal_request(), snippet, url, found_pn=None)
        assert scoring_mod._last_noun_classes["query"] == "SEAL"
        assert scoring_mod._last_noun_classes["result"] == "PUMP"

    def test_score_unchanged_flag_on_vs_off(self, monkeypatch):
        """T3 is detection-only. The cross-task invariant is: flag-off is
        byte-identical to the pre-redesign legacy score (no SCORING_V2 behavior
        runs). Flag-on scoring legitimately drifts as T4 (auth cap) and T5
        (exact-PN demotion) land — that is intended, not a T3 regression — so we
        do NOT assert flag-on == flag-off here (that equality only held before
        T4/T5 existed). We assert: (a) flag-off equals the legacy additive score,
        and (b) detection actually ran flag-on (the T3 contract)."""
        specs = AssetSpecs(
            manufacturer="SKF", model="6205", part_number="6205-2RS",
            voltage="N/A", category="Part", detected_type="ball bearing",
        )
        snippet = "SKF 6205-2RS deep groove ball bearing. In stock, ships today."
        url = "https://mrosupply.com/bearings/skf-6205-2rs/"
        # (a) flag-off is the unchanged legacy additive score.
        monkeypatch.setattr(scoring_mod, "SCORING_V2", False)
        score_off = _compute_suitability_score(specs, snippet, url, found_pn="6205-2RS")
        assert score_off == 95.0, f"flag-off legacy bearing should be 95, got {score_off}"
        # (b) flag-on runs detection (the T3 contract) and still clears the floor.
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        score_on = _compute_suitability_score(specs, snippet, url, found_pn="6205-2RS")
        assert scoring_mod._last_noun_classes["query"] == "BEARING"
        assert scoring_mod._last_noun_classes["result"] == "BEARING"
        assert score_on >= 30.0

    def test_title_param_improves_result_detection(self, monkeypatch):
        """When the URL is undetectable but a title is supplied, the title drives
        the result noun-class (the snippet alone might be ambiguous)."""
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        url = "https://vendor.com/products/sku-999"
        _compute_suitability_score(
            _seal_request(), "generic replacement content", url,
            found_pn=None, title="Goulds 3196 mechanical seal",
        )
        assert scoring_mod._last_noun_classes["result"] == "SEAL"
