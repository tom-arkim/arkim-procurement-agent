"""
Tests for utils/impact.py — the "Your Arkim impact" calculation module.

Built TO the Time-Saved Calculator Methodology. Asserts the three-tier separation
and its invariants:
  1. SAVINGS — MEASURED: only from the customer's own last_paid / this-RFQ quotes;
     no comparator -> None (never a fabricated 0); no external/market baseline input.
  2. ACTION COUNTS — COUNTED: real pass-throughs, not estimates.
  3. TIME SAVED — ESTIMATED: counts x a VERSIONED config; conservative; the v1
     worked example (V-belt RFQ) yields ~1 hr (65 min), not the mockup's ~2 hr.

These are pure-function tests — no DB, no network, no external calls.
"""

import inspect

from utils.impact import (
    ESTIMATE_MODEL_VERSION,
    ESTIMATE_MODELS,
    compute_savings,
    cumulative_impact,
    per_decision_impact,
    time_saved_minutes,
)

# The methodology's V-belt worked example: 1 part identified, 3 suppliers contacted,
# 2 quotes read, 1 comparison, 0 chases.
_VBELT_COUNTS = {
    "parts_identified": 1,
    "suppliers_contacted": 3,
    "quotes_read": 2,
    "comparisons_made": 1,
    "replies_chased": 0,
}


# ---------------------------------------------------------------------------
# Tier 1 — SAVINGS (measured, customer transactions only)
# ---------------------------------------------------------------------------

class TestSavingsMeasured:
    def test_vs_last_paid(self):
        saving, basis, _ = compute_savings(chosen_price=80.0, last_paid_price=100.0, quotes=None)
        assert saving == 20.0 and basis == "vs_last_paid"

    def test_vs_highest_quote_when_no_history(self):
        saving, basis, _ = compute_savings(chosen_price=80.0, last_paid_price=None,
                                           quotes=[80.0, 95.0, 110.0])
        assert saving == 30.0 and basis == "vs_highest_quote"   # 110 - 80

    def test_last_paid_preferred_over_quotes(self):
        # Both comparators exist -> the firmest (own prior purchase) wins, not the quote spread.
        saving, basis, _ = compute_savings(chosen_price=80.0, last_paid_price=100.0,
                                           quotes=[80.0, 200.0])
        assert basis == "vs_last_paid" and saving == 20.0

    def test_no_comparator_returns_none_not_zero(self):
        # No history AND no rival quote -> NO figure (None), never a fabricated 0.
        saving, basis, _ = compute_savings(chosen_price=80.0, last_paid_price=None, quotes=None)
        assert saving is None and basis is None

    def test_single_quote_is_not_a_comparator(self):
        # One quote = nothing to compare against -> None, not 0.
        saving, basis, _ = compute_savings(chosen_price=80.0, last_paid_price=None, quotes=[80.0])
        assert saving is None and basis is None

    def test_saving_can_be_negative_when_paid_more(self):
        # Honest: paying more than last time is a real (negative) measured delta, not hidden.
        saving, basis, _ = compute_savings(chosen_price=120.0, last_paid_price=100.0, quotes=None)
        assert saving == -20.0 and basis == "vs_last_paid"

    def test_no_external_baseline_input_exists(self):
        # Structural guarantee: the ONLY inputs are the customer's own figures. There is no
        # market/baseline/list-price parameter, so no code path can anchor to an external rate.
        params = set(inspect.signature(compute_savings).parameters)
        assert params == {"chosen_price", "last_paid_price", "quotes"}
        for forbidden in ("market", "baseline", "list_price", "msrp", "reference"):
            assert forbidden not in params


# ---------------------------------------------------------------------------
# Tier 3 — TIME SAVED (estimated: counts x versioned config)
# ---------------------------------------------------------------------------

class TestTimeEstimate:
    def test_v1_config_matches_methodology(self):
        cfg = ESTIMATE_MODELS[ESTIMATE_MODEL_VERSION]
        assert cfg == {
            "identify_part": 15, "contact_supplier": 10, "read_quote": 5,
            "compare_quotes": 10, "chase_nonresponder": 5,
        }

    def test_vbelt_example_is_about_one_hour_not_two(self):
        minutes = time_saved_minutes(_VBELT_COUNTS, version="v1")
        assert minutes == 65                     # 15 + 30 + 10 + 10 + 0
        assert minutes < 120                     # conservative — NOT the mockup's ~2 hr

    def test_zero_counts_zero_minutes(self):
        assert time_saved_minutes({}, version="v1") == 0


# ---------------------------------------------------------------------------
# Per-decision impact (the three tiers, separated, in one payload)
# ---------------------------------------------------------------------------

class TestPerDecisionImpact:
    def test_shape_and_version_surfaced(self):
        out = per_decision_impact(chosen_price=80.0, last_paid_price=100.0,
                                  quotes=[80.0, 110.0], counts=_VBELT_COUNTS)
        assert set(out) >= {"saving", "saving_basis", "counts",
                            "time_estimate_minutes", "estimate_model_version"}
        assert out["saving"] == 20.0 and out["saving_basis"] == "vs_last_paid"
        assert out["time_estimate_minutes"] == 65
        assert out["estimate_model_version"] == "v1"

    def test_counts_are_real_passthrough(self):
        out = per_decision_impact(chosen_price=80.0, last_paid_price=None, quotes=None,
                                  counts=_VBELT_COUNTS)
        assert out["counts"] == _VBELT_COUNTS          # unchanged, not estimated
        assert out["saving"] is None                   # no comparator -> no figure

    def test_config_version_changes_estimate_not_savings(self, monkeypatch):
        # Tiers are independent: a different estimate model changes hours, never savings.
        monkeypatch.setitem(ESTIMATE_MODELS, "vtest", {
            "identify_part": 30, "contact_supplier": 20, "read_quote": 10,
            "compare_quotes": 20, "chase_nonresponder": 10,
        })
        base = per_decision_impact(chosen_price=80.0, last_paid_price=100.0,
                                   quotes=[80.0, 110.0], counts=_VBELT_COUNTS, version="v1")
        alt = per_decision_impact(chosen_price=80.0, last_paid_price=100.0,
                                  quotes=[80.0, 110.0], counts=_VBELT_COUNTS, version="vtest")
        assert alt["time_estimate_minutes"] == 130     # doubled config -> doubled minutes
        assert alt["time_estimate_minutes"] != base["time_estimate_minutes"]
        assert alt["saving"] == base["saving"] == 20.0  # savings untouched by version
        assert alt["estimate_model_version"] == "vtest"


# ---------------------------------------------------------------------------
# Cumulative impact (real months only, drillable ids)
# ---------------------------------------------------------------------------

def _decision(order_id, month, saving, basis, counts=None):
    return {"order_id": order_id, "month": month, "saving": saving,
            "saving_basis": basis, "counts": counts or _VBELT_COUNTS}


class TestCumulativeImpact:
    def test_total_and_contributing_ids(self):
        decisions = [
            _decision("o1", "2026-01", 20.0, "vs_last_paid"),
            _decision("o2", "2026-03", 30.0, "vs_highest_quote"),
            _decision("o3", "2026-03", None, None),       # no comparator -> contributes 0
        ]
        out = cumulative_impact(decisions, version="v1")
        assert out["total_savings"] == 50.0               # 20 + 30 (None contributes nothing)
        assert out["contributing_order_ids"] == ["o1", "o2"]   # only measured ones drillable

    def test_only_real_months_no_interpolation(self):
        decisions = [
            _decision("o1", "2026-01", 20.0, "vs_last_paid"),
            _decision("o2", "2026-03", 30.0, "vs_highest_quote"),
        ]
        out = cumulative_impact(decisions, version="v1")
        months = [m["month"] for m in out["savings_by_month"]]
        assert months == ["2026-01", "2026-03"]           # Feb absent — never interpolated

    def test_zero_month_stays_zero(self):
        # A month with a real order but no realised saving is a REAL 0, kept, not dropped.
        decisions = [_decision("o3", "2026-02", None, None)]
        out = cumulative_impact(decisions, version="v1")
        feb = next(m for m in out["savings_by_month"] if m["month"] == "2026-02")
        assert feb["savings"] == 0.0
        assert feb["order_ids"] == ["o3"]                 # drillable even with 0 saving

    def test_counts_summed_and_time_estimated(self):
        decisions = [
            _decision("o1", "2026-01", 20.0, "vs_last_paid"),
            _decision("o2", "2026-01", 30.0, "vs_highest_quote"),
        ]
        out = cumulative_impact(decisions, version="v1")
        assert out["counts"]["suppliers_contacted"] == 6  # 3 + 3
        assert out["counts"]["quotes_read"] == 4
        assert out["time_estimate_minutes"] == 130        # 65 + 65
        assert out["estimate_model_version"] == "v1"

    def test_month_note_records_basis_mix(self):
        out = cumulative_impact([
            _decision("o1", "2026-01", 20.0, "vs_last_paid"),
            _decision("o2", "2026-01", None, None),
        ], version="v1")
        jan = next(m for m in out["savings_by_month"] if m["month"] == "2026-01")
        assert jan["savings"] == 20.0
        assert set(jan["order_ids"]) == {"o1", "o2"}
        assert "note" in jan
