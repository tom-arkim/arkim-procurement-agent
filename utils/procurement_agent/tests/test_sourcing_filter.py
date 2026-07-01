"""
Tests for utils/sourcing_filter.apply_exact_only — the exact-only sourcing filter.
Pure: Tier 2/3 keep only exact OEM matches; aftermarket/equivalent dropped; Tier 1 kept.
"""

from utils.sourcing_filter import apply_exact_only


def _results():
    return {
        "tier_1": {"results": [{"vendor_name": "Network Co", "match_type": "Aftermarket Compatible"}], "count": 1},
        "tier_2": {"results": [
            {"vendor_name": "Exact A", "match_type": "Exact OEM"},
            {"vendor_name": "PN Exact", "pn_match_status": "exact_match"},
            {"vendor_name": "Aftermarket B", "match_type": "Aftermarket Compatible"},
            {"vendor_name": "Partial C", "pn_match_status": "partial_match"},
        ], "count": 4},
        "tier_3": {"results": [
            {"vendor_name": "Exact D", "match_type": "Exact OEM"},
            {"vendor_name": "Equiv E", "match_type": "Aftermarket Compatible"},
        ], "count": 2},
    }


def test_tier2_3_keep_only_exact():
    out = apply_exact_only(_results())
    t2 = [o["vendor_name"] for o in out["tier_2"]["results"]]
    assert t2 == ["Exact A", "PN Exact"]
    assert out["tier_2"]["count"] == 2
    t3 = [o["vendor_name"] for o in out["tier_3"]["results"]]
    assert t3 == ["Exact D"]
    assert out["tier_3"]["count"] == 1


def test_tier1_left_intact():
    # Tier 1 (network) is trusted and never filtered, even if it looks aftermarket.
    out = apply_exact_only(_results())
    assert [o["vendor_name"] for o in out["tier_1"]["results"]] == ["Network Co"]
    assert out["tier_1"]["count"] == 1


def test_safe_on_missing_or_malformed():
    assert apply_exact_only({}) == {}
    assert apply_exact_only({"tier_2": None}) == {"tier_2": None}
    out = apply_exact_only({"tier_2": {"results": []}})
    assert out["tier_2"]["results"] == [] and out["tier_2"]["count"] == 0
