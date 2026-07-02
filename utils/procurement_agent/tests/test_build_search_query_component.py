"""
Fix A + B1 tests — component-aware _build_search_query (the THIRD builder) and
the cache-hit validation parity in _result_from_cached_edges.
"""

from __future__ import annotations

import pytest

from utils.models import AssetSpecs
from utils.sourcing_archieved.tavily_client import _build_search_query
from utils.procurement_agent.component_query import is_bare_parent_query


# ===========================================================================
# Fix A — _build_search_query's Part branch is now component-aware.
# The F1 case: a mechanical-seal request where extraction set manufacturer/model
# to the PARENT ("Goulds" "3196"). The query must be component-led.
# ===========================================================================

def _seal_specs(component_of=None) -> AssetSpecs:
    return AssetSpecs(
        manufacturer="Goulds",
        model="3196",
        part_number="UNKNOWN-PN",
        voltage="N/A",
        category="Part",
        detected_type="mechanical seal",
        component_of=component_of,
    )


def test_fix_a_part_query_contains_component_term_both_flag_states(monkeypatch):
    """The component term (detected_type) must appear in the Part query whether
    or not INTAKE_TYPE_AWARE is set — detected_type is unconditional."""
    # Flag OFF
    monkeypatch.delenv("INTAKE_TYPE_AWARE", raising=False)
    q_off = _build_search_query(_seal_specs(component_of=None), search_mode="exact")
    assert "mechanical seal" in q_off.lower(), f"flag-off query dropped the component: {q_off!r}"
    # Flag ON (component_of populated)
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    q_on = _build_search_query(_seal_specs(component_of="Goulds 3196"), search_mode="exact")
    assert "mechanical seal" in q_on.lower(), f"flag-on query dropped the component: {q_on!r}"


def test_fix_a_part_query_not_bare_parent():
    """The F1 anti-pattern: a query that carries the parent but NOT the component
    (a bare 'Goulds 3196 ...' that sources the pump). Must NOT be produced."""
    specs = _seal_specs(component_of="Goulds 3196")
    q = _build_search_query(specs, search_mode="exact")
    assert "goulds 3196" in q.lower(), f"parent identity should still anchor: {q!r}"
    assert is_bare_parent_query(q, "Goulds 3196") is False, (
        f"query must not be a bare-parent query: {q!r}"
    )
    # And it ends with the canonical tail.
    assert q.endswith("US distributor price buy")


def test_fix_a_component_of_phrase_used_when_set():
    """When component_of is set, the 'mechanical seal for Goulds 3196' phrase
    leads the query (component-led), matching the T5 pattern in the other two
    builders."""
    specs = _seal_specs(component_of="Goulds 3196")
    q = _build_search_query(specs, search_mode="exact")
    assert "mechanical seal for goulds 3196" in q.lower(), (
        f"expected the component-for-parent phrase, got {q!r}"
    )
    # The parent (mfg Goulds / model 3196) is already in the phrase -> not doubled.
    assert q.lower().count("goulds 3196") == 1, f"parent doubled in query: {q!r}"


def test_fix_a_detected_only_no_parent_leads_with_component(monkeypatch):
    """No component_of, but detected_type is set: lead with the component term."""
    monkeypatch.delenv("INTAKE_TYPE_AWARE", raising=False)
    specs = AssetSpecs(
        manufacturer="SKF", model="6205", part_number="6205-2RS1",
        voltage="N/A", category="Part", detected_type="deep groove ball bearing",
    )
    q = _build_search_query(specs, search_mode="exact")
    assert "deep groove ball bearing" in q.lower()
    assert "skf" in q.lower() and "6205" in q.lower()


def test_fix_a_no_detected_type_byte_identical_to_legacy():
    """A Part with NO detected_type and NO component_of falls back to the legacy
    mfg+mdl+pn anchors — byte-identical to the pre-fix query (so rows that
    carry description-only, like the dynamic-discovery samples, are unchanged)."""
    specs = AssetSpecs(
        manufacturer="SKF", model="6206-2RS", part_number="6206-2RS",
        voltage="N/A", category="Part",
        # detected_type intentionally unset (None); description not read by this builder
    )
    q = _build_search_query(specs, search_mode="exact")
    # Legacy form: 'SKF 6206-2RS "6206-2RS" US distributor price buy'
    assert q == 'SKF 6206-2RS "6206-2RS" US distributor price buy', f"legacy query changed: {q!r}"


def test_fix_a_equivalents_mode_also_component_led():
    """The 'equivalents' search_mode is an Equipment-branch concept, but confirm
    a Part still gets the component-led query in exact mode (the path Tier 2 uses)."""
    specs = _seal_specs(component_of="Goulds 3196")
    q = _build_search_query(specs, search_mode="exact")
    assert "mechanical seal" in q.lower()


# ===========================================================================
# Fix B1 — _result_from_cached_edges applies the suitability floor + rejection
# filter, so a below-floor / rejection_reason cached edge does NOT surface.
# ===========================================================================

def test_fix_b1_cached_edge_below_floor_does_not_surface():
    from api_server import _result_from_cached_edges
    edges = [
        {"display_name": "Zoro Pump", "supplier_id": "zoro.com",
         "source_url": "https://zoro.com/goulds-pump", "price": 10425.99,
         "match_type": "Functional Alternative", "tier": 2,
         "suitability": 0.0, "lead_days": 5, "purchase_channel": "marketplace"},
    ]
    result = _result_from_cached_edges(edges)
    assert result["tier_2"]["results"] == [], (
        f"below-floor cached edge must not surface, got {result['tier_2']['results']!r}"
    )


def test_fix_b1_cached_edge_with_rejection_reason_does_not_surface():
    from api_server import _result_from_cached_edges
    edges = [
        {"display_name": "Bad Vendor", "supplier_id": "bad.com",
         "source_url": "https://bad.com/x", "price": 50.0,
         "match_type": "Functional Alternative", "tier": 2,
         "suitability": 95.0, "lead_days": 3, "purchase_channel": "marketplace",
         "rejection_reason": "category_mismatch_suspected"},
    ]
    result = _result_from_cached_edges(edges)
    assert result["tier_2"]["results"] == [], (
        f"rejection_reason cached edge must not surface, got {result['tier_2']['results']!r}"
    )


def test_fix_b1_above_floor_cached_edge_surfaces_normally():
    from api_server import _result_from_cached_edges
    edges = [
        {"display_name": "Good Seal Co", "supplier_id": "goodseal.com",
         "source_url": "https://goodseal.com/seal", "price": 120.0,
         "match_type": "Exact OEM", "tier": 2,
         "suitability": 72.0, "lead_days": 4, "purchase_channel": "marketplace"},
    ]
    result = _result_from_cached_edges(edges)
    assert len(result["tier_2"]["results"]) == 1
    cand = result["tier_2"]["results"][0]
    assert cand["vendor_name"] == "Good Seal Co"
    assert not cand.get("rejection_reason")


def test_fix_b1_floor_threshold_matches_live_constant():
    """The cache-hit floor must use the SAME threshold as the live path."""
    from api_server import _result_from_cached_edges
    from utils.sourcing_archieved.constants import TIER_SURFACE_MIN_SUITABILITY
    # An edge exactly AT the floor should surface; one just below should not.
    at = [{"display_name": "At Floor", "supplier_id": "at.com", "source_url": "https://at.com",
           "price": 10.0, "match_type": "Functional Alternative", "tier": 3,
           "suitability": float(TIER_SURFACE_MIN_SUITABILITY), "lead_days": 5,
           "purchase_channel": "rfq"}]
    below = [{"display_name": "Below", "supplier_id": "below.com", "source_url": "https://below.com",
              "price": 10.0, "match_type": "Functional Alternative", "tier": 3,
              "suitability": float(TIER_SURFACE_MIN_SUITABILITY) - 1.0, "lead_days": 5,
              "purchase_channel": "rfq"}]
    assert len(_result_from_cached_edges(at)["tier_3"]["results"]) == 1
    assert _result_from_cached_edges(below)["tier_3"]["results"] == []
