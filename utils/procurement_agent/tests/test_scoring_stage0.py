"""
T2 — Stage 0 placeholder-penalty fix tests.

Stage 0 is the bug where a real found_pn gets the -30 mismatch penalty for
"mismatching" a placeholder searched PN (UNKNOWN-PN / N/A / Unknown / empty).
The placeholder was never a real PN, so there is nothing to mismatch against.

The fix has a one-line toggle, ``STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL`` in
scoring.py:
  - False (default, GATED)        -> fix applies only under SCORING_V2=1.
                                     Flag-off preserves the legacy -30 behavior
                                     (launch demo byte-identical).
  - True  (UNCONDITIONAL)         -> fix applies always (flag-on OR off).

These tests cover BOTH paths so the toggle can be flipped either way without
breaking the suite, and so the flag-off stress-test Tom runs is meaningful.
"""

import pytest

from utils.sourcing_archieved import scoring as scoring_mod
from utils.sourcing_archieved.scoring import _compute_suitability_score, _is_placeholder_pn
from utils.models import AssetSpecs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _goulds_seal_specs() -> AssetSpecs:
    """Component request where we know the PARENT model (Goulds 3196) but not the
    part's own PN — specs.part_number carries the UNKNOWN-PN placeholder."""
    return AssetSpecs(
        manufacturer="Goulds",
        model="3196",
        part_number="UNKNOWN-PN",
        voltage="N/A",
        category="Part",
        detected_type="mechanical seal",
    )


def _seal_snippet_with_real_pn() -> str:
    """A legitimate specialist seal page that carries a REAL part number."""
    return (
        "Platinum Performance Products Goulds 3196 mechanical seal, "
        "Type 1, 1.375 inch shaft. Part number ST-1.375-T1. "
        "In stock, ships same day. Cross-reference / interchange for Goulds 3196."
    )


def _seal_url() -> str:
    return "https://platinumperformanceproducts.com/mechanical-seals/goulds/3196-st"


# ---------------------------------------------------------------------------
# _is_placeholder_pn helper
# ---------------------------------------------------------------------------

class TestIsPlaceholderPn:
    @pytest.mark.parametrize("pn", ["UNKNOWN-PN", "unknown-pn", "Unknown", "N/A", "n/a", "", "   ", None, "null", "none"])
    def test_placeholders_detected(self, pn):
        assert _is_placeholder_pn(pn) is True

    @pytest.mark.parametrize("pn", ["ST-1.375-T1", "6205-2RS", "HHI-150-12-447T", "PMC11-AA1U1HBWBJJ"])
    def test_real_pns_not_placeholder(self, pn):
        assert _is_placeholder_pn(pn) is False


# ---------------------------------------------------------------------------
# Stage 0 — the anchor case: real PN should NOT be penalized vs UNKNOWN-PN
# ---------------------------------------------------------------------------

class TestStage0PlaceholderPenaltyFix:
    def _seal_score(self) -> float:
        return _compute_suitability_score(
            _goulds_seal_specs(), _seal_snippet_with_real_pn(), _seal_url(),
            found_pn="ST-1.375-T1",
        )

    def test_seal_clears_floor_under_scoring_v2(self, monkeypatch):
        """SCORING_V2=1 -> no -30 penalty against the placeholder -> seal
        scores ~55 (above the 30 floor), not 25 (below floor)."""
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        score = self._seal_score()
        assert score >= 30.0, f"seal should clear the 30 floor under SCORING_V2, got {score}"
        # Sanity: the legacy penalized score was 25; confirm we're well clear.
        assert score > 30.0

    def test_seal_penalized_flag_off_default_gated(self, monkeypatch):
        """Flag-OFF + GATED (default) -> the legacy -30 penalty STILL fires
        against the placeholder -> seal scores ~25 (below floor). This is the
        byte-identical launch-demo behavior; the fix is dormant until flipped."""
        monkeypatch.setattr(scoring_mod, "SCORING_V2", False)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        score = self._seal_score()
        assert score < 30.0, (
            f"GATED flag-off must preserve legacy penalty (seal <30), got {score}; "
            "if this regressed the launch demo scoring changed unintentionally"
        )

    def test_seal_clears_floor_flag_off_unconditional(self, monkeypatch):
        """Flag-OFF + UNCONDITIONAL (toggle flipped) -> fix applies even with
        SCORING_V2 off -> seal clears the floor. This is the launch-demo-fix path
        Tom can elect after the stress test."""
        monkeypatch.setattr(scoring_mod, "SCORING_V2", False)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", True)
        score = self._seal_score()
        assert score >= 30.0, f"UNCONDITIONAL flag-off should fix the seal, got {score}"

    def test_real_mismatch_still_penalized_under_scoring_v2(self, monkeypatch):
        """A GENUINE mismatch (real searched PN vs a different real found_pn)
        must still get -30 under SCORING_V2 — Stage 0 only suppresses the penalty
        for placeholder searched PNs, it does not disable mismatch detection."""
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        specs = AssetSpecs(
            manufacturer="US Motors",
            model="HHI-150-12-447T",
            part_number="HHI-150-12-447T",
            voltage="460V",
            category="Equipment",
            detected_type="Electric Motor",
            hp="150",
        )
        # found_pn is a real, genuinely different PN; snippet doesn't rescue it.
        score_mismatch = _compute_suitability_score(
            specs, "unrelated motor content", "https://vendor.com/motors/sku-999",
            found_pn="WEG-200-14-449T",
        )
        # Compare to the no-found-pn baseline (no penalty) on the same inputs.
        score_no_found = _compute_suitability_score(
            specs, "unrelated motor content", "https://vendor.com/motors/sku-999",
            found_pn=None,
        )
        assert score_mismatch < score_no_found, (
            "genuine mismatch must still incur a penalty under SCORING_V2; "
            f"mismatch={score_mismatch} vs no_found={score_no_found}"
        )

    def test_clean_pn_case_unchanged_flag_off(self, monkeypatch):
        """Clean-PN case (real searched PN, no placeholder involved) must be
        unchanged flag-off — Stage 0 only touches placeholder searched PNs."""
        monkeypatch.setattr(scoring_mod, "SCORING_V2", False)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        specs = AssetSpecs(
            manufacturer="SKF",
            model="6205",
            part_number="6205-2RS",
            voltage="N/A",
            category="Part",
            detected_type="bearing",
        )
        snippet = "SKF 6205-2RS deep groove ball bearing. In stock, ships today."
        url = "https://mrosupply.com/bearings/skf-6205-2rs/"
        score = _compute_suitability_score(specs, snippet, url, found_pn="6205-2RS")
        assert score > 0.0
        # Exact-PN match -> no mismatch penalty regardless of Stage 0.
        assert score >= 40.0


# ---------------------------------------------------------------------------
# Toggle default sanity
# ---------------------------------------------------------------------------

class TestStage0ToggleDefault:
    def test_toggle_defaults_to_gated_false(self):
        """Guardrail: the Stage 0 toggle must default to False (GATED) so the
        launch demo is unaffected until Tom flips it."""
        # Re-import to read the module-level default (tests above monkeypatch it).
        import importlib
        fresh = importlib.reload(importlib.import_module("utils.sourcing_archieved.scoring"))
        assert fresh.STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL is False
