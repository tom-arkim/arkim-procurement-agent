"""
T5 — graded Fit replaces exact-PN dominance (behind SCORING_V2).

Exact OEM-PN is demoted from a 40-pt dominating factor to a bonus WITHIN Fit.
Fit also credits parent-model tokens, size/type tokens, and interchange /
cross-reference / "replaces" / "fits" language — so a correct aftermarket
component keyed off the parent model isn't penalized for lacking its own OEM PN.
"""

import pytest

from utils.sourcing_archieved import scoring as scoring_mod
from utils.sourcing_archieved.scoring import (
    _compute_suitability_score,
    _fit_signal,
    _size_type_tokens,
    _FIT_EXACT_PN,
    _FIT_PARENT_MODEL,
    _FIT_INTERCHANGE,
    _FIT_MAX,
)
from utils.models import AssetSpecs

TIER_FLOOR = 30.0


def _seal_request() -> AssetSpecs:
    return AssetSpecs(
        manufacturer="Goulds", model="3196", part_number="UNKNOWN-PN",
        voltage="N/A", category="Part", detected_type="mechanical seal",
    )


# ---------------------------------------------------------------------------
# _size_type_tokens
# ---------------------------------------------------------------------------

class TestSizeTypeTokens:
    def test_decimal_size_extracted(self):
        specs = AssetSpecs(
            manufacturer="Goulds", model="3196", part_number="UNKNOWN-PN",
            voltage="N/A", category="Part", detected_type="mechanical seal",
            description="1.375 inch shaft, Type 1",
        )
        toks = _size_type_tokens(specs)
        assert "1.375" in toks
        assert "type 1" in toks

    def test_no_size_returns_empty(self):
        specs = AssetSpecs(
            manufacturer="SKF", model="6205", part_number="6205-2RS",
            voltage="N/A", category="Part", detected_type="ball bearing",
        )
        toks = _size_type_tokens(specs)
        # 6205 has no decimal; "Type N" absent -> empty (model token handled by parent-model, not here)
        assert toks == ()


# ---------------------------------------------------------------------------
# _fit_signal — graded Fit
# ---------------------------------------------------------------------------

class TestFitSignal:
    def test_aftermarket_seal_strong_fit_without_exact_pn(self):
        """The headline T5 case: a seal page with 'replaces Goulds 3196' + 1.375
        + Type 1 + interchange language scores strong Fit even with NO exact PN
        (searched PN is the UNKNOWN-PN placeholder)."""
        snippet = ("Platinum Goulds 3196 mechanical seal, 1.375 inch shaft, Type 1. "
                   "Cross-reference / interchange for Goulds 3196. ST-1.375-T1.")
        # pn_match_level: searched UNKNOWN-PN vs found ST-1.375-T1 -> 'none'.
        fit = _fit_signal(_seal_request(), snippet, pn_match_level="none")
        assert fit >= _FIT_PARENT_MODEL + _FIT_INTERCHANGE  # parent + interchange at minimum
        assert fit >= 20.0, f"aftermarket seal should have strong Fit, got {fit}"

    def test_exact_pn_is_a_bonus_not_the_whole_factor(self):
        """Exact OEM-PN contributes, but is capped within Fit and no longer the
        40-pt cliff — parent-model + interchange can reach a comparable Fit
        without any exact PN."""
        specs = AssetSpecs(
            manufacturer="SKF", model="6205", part_number="6205-2RS",
            voltage="N/A", category="Part", detected_type="ball bearing",
        )
        # Exact PN match, plus parent model '6205' present.
        fit_exact = _fit_signal(specs, "SKF 6205-2RS ball bearing, 6205 series.", "exact")
        # No exact PN, but parent-model + interchange language.
        fit_aftermarket = _fit_signal(specs, "Replacement bearing for SKF 6205, interchange 6205-2RS.", "none")
        # Exact is a bonus so it scores higher, but aftermarket is comparable, not zero.
        assert fit_exact > 0
        assert fit_aftermarket >= _FIT_PARENT_MODEL + _FIT_INTERCHANGE
        # Neither exceeds the cap.
        assert fit_exact <= _FIT_MAX
        assert fit_aftermarket <= _FIT_MAX

    def test_no_fit_evidence_returns_zero(self):
        """A page with none of {exact/stem/substring PN, parent model, size/type,
        interchange language} scores Fit=0 — the 45-cap then applies (no Fit
        evidence at all), mirroring the legacy 'no PN confirmed' guardrail."""
        specs = AssetSpecs(
            manufacturer="Acme", model="X1", part_number="X1-A",
            voltage="N/A", category="Part", detected_type="widget",
        )
        # Snippet has none of the signals (no X1, no size, no interchange lang).
        fit = _fit_signal(specs, "generic industrial supply catalog page", "none")
        assert fit == 0.0

    def test_fit_capped_at_max(self):
        specs = AssetSpecs(
            manufacturer="Goulds", model="3196", part_number="3196-ST",
            voltage="N/A", category="Part", detected_type="mechanical seal 1.375 type 1",
        )
        # Every signal fires: exact PN + parent model + size/type + interchange.
        snippet = "Goulds 3196 mechanical seal 1.375 type 1. Cross-reference, interchange, replaces 3196-ST."
        fit = _fit_signal(specs, snippet, "exact")
        assert fit == _FIT_MAX


# ---------------------------------------------------------------------------
# Anchor re-verification under T5 (gate + Fit together)
# ---------------------------------------------------------------------------

class TestT5Anchor:
    def _seal_result(self):
        snippet = ("Platinum Performance Products Goulds 3196 mechanical seal, "
                   "Type 1, 1.375 inch. Part ST-1.375-T1. In stock, ships same day. "
                   "Cross-reference / interchange for Goulds 3196.")
        url = "https://platinumperformanceproducts.com/mechanical-seals/goulds/3196-st"
        return snippet, url

    def _pump_result(self):
        snippet = "Goulds 3196 centrifugal pump, 5HP, 3450 RPM. In stock at Zoro."
        url = "https://zoro.com/pump/centrifugal/goulds-3196/i/"
        return snippet, url

    def test_seal_passes_floor_with_fit(self, monkeypatch):
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        snippet, url = self._seal_result()
        score = _compute_suitability_score(_seal_request(), snippet, url, found_pn="ST-1.375-T1")
        assert score >= TIER_FLOOR, f"seal must still pass under T5 Fit, got {score}"

    def test_pump_fails_floor_under_fit(self, monkeypatch):
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        snippet, url = self._pump_result()
        score = _compute_suitability_score(_seal_request(), snippet, url, found_pn=None)
        assert score < TIER_FLOOR, f"pump must still fail under T5 Fit, got {score}"

    def test_seal_beats_pump(self, monkeypatch):
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        s_snip, s_url = self._seal_result()
        p_snip, p_url = self._pump_result()
        seal = _compute_suitability_score(_seal_request(), s_snip, s_url, found_pn="ST-1.375-T1")
        pump = _compute_suitability_score(_seal_request(), p_snip, p_url, found_pn=None)
        assert seal > pump

    def test_clean_bearing_still_passes(self, monkeypatch):
        """Clean-PN bearing must still clear the floor strongly under T5 (exact-PN
        demoted 40->20, but parent-model + type keep Fit high; gate 1.0)."""
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        specs = AssetSpecs(
            manufacturer="SKF", model="6205", part_number="6205-2RS",
            voltage="N/A", category="Part", detected_type="ball bearing",
        )
        snippet = "SKF 6205-2RS deep groove ball bearing. In stock, ships today."
        url = "https://mrosupply.com/bearings/skf-6205-2rs/"
        score = _compute_suitability_score(specs, snippet, url, found_pn="6205-2RS")
        assert score >= TIER_FLOOR, f"clean bearing must still pass under T5, got {score}"


# ---------------------------------------------------------------------------
# Inertness — flag-off byte-identical (Fit path only runs under SCORING_V2)
# ---------------------------------------------------------------------------

class TestT5Inertness:
    def test_flag_off_uses_legacy_pn_pts(self, monkeypatch):
        """Flag-off: the Fit path is not entered; the pump scores its legacy
        additive 40 (pn_pts=0, but the legacy path uses pn_pts not fit_pts),
        proving flag-off scoring is unchanged from pre-T5."""
        monkeypatch.setattr(scoring_mod, "SCORING_V2", False)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        snippet = "Goulds 3196 centrifugal pump, 5HP. In stock at Zoro."
        url = "https://zoro.com/pump/centrifugal/goulds-3196/i/"
        score = _compute_suitability_score(_seal_request(), snippet, url, found_pn=None)
        # Legacy: pn_pts=0 (no found_pn), type_pts=0, mfg=10, auth=20, url=10 -> 40, capped 45 -> 40.
        assert score == 40.0, f"flag-off pump should be legacy 40, got {score}"
