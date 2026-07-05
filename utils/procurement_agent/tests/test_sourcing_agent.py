"""
Tests for SourcingAgent — three-tier parallel sourcing, urgency ranking, warranty filtering.

Tier 2 and Tier 3 calls (Tavily + Anthropic) are mocked. Tier 1 uses a temp catalog file.
"""

import json
import os
from unittest.mock import patch, MagicMock

import pytest

from utils.models import SourcingRun
from utils.procurement_agent.agents.sourcing_agent import (
    SourcingAgent,
    _URGENCY_WEIGHTS,
    _WARRANTY_BANNER,
    _dedup_across_tiers,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_CATALOG = {
    "suppliers": [
        {
            "name":            "Test Arkim Supplier",
            "location":        "Test, TX",
            "website":         "https://test-arkim.com",
            "reliability_score": 95.0,
            "contact_email":   "orders@test-arkim.com",
            "inventory": [
                {
                    "part_number": "PN-TEST-001",
                    "manufacturer": "TestMfg",
                    "description": "Test bearing part",
                    "price":      120.00,
                    "lead_days":  2,
                    "in_stock":   True,
                },
                {
                    "part_number": "PN-TEST-002",
                    "manufacturer": "TestMfg",
                    "description": "Test motor",
                    "price":      2000.00,
                    "lead_days":  3,
                    "in_stock":   False,
                },
            ],
        }
    ]
}


def _make_run(
    specs: dict | None = None,
    urgency: float = 0.3,
    warranty: str = "unknown",
) -> SourcingRun:
    return SourcingRun(
        asset_specs_json=specs or {
            "manufacturer": "TestMfg",
            "model":        "TM-001",
            "part_number":  "PN-TEST-001",
            "voltage":      "460V",
            "category":     "Part",
            "detected_type": "bearing",
        },
        urgency_factor=urgency,
        warranty_status=warranty,
    )


def _agent_with_mocked_tiers(tier2=None, tier3=None) -> tuple[SourcingAgent, str]:
    """Return (agent, catalog_path_str) ready for patch."""
    return SourcingAgent(), ""


# ---------------------------------------------------------------------------
# Tier 1 — catalog lookup
# ---------------------------------------------------------------------------

class TestTier1:
    def test_hit_by_exact_part_number(self, tmp_path):
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps(_CATALOG))

        agent = SourcingAgent()
        run   = _make_run()

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH", str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        assert result["tier_1"]["count"] >= 1
        # The exact PN match should be present and ranked first (highest suitability)
        exact_matches = [r for r in result["tier_1"]["results"]
                         if r["match_type"] == "Exact OEM"]
        assert len(exact_matches) >= 1
        assert exact_matches[0]["vendor_name"] == "Test Arkim Supplier"

    def test_hit_by_manufacturer_when_no_pn_match(self, tmp_path):
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps(_CATALOG))

        agent = SourcingAgent()
        # PN doesn't match but manufacturer does
        run = _make_run(specs={
            "manufacturer": "TestMfg",
            "model":        "TM-999",
            "part_number":  "PN-UNKNOWN",
            "voltage":      "460V",
            "category":     "Part",
        })

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH", str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        # Should match both items by manufacturer
        assert result["tier_1"]["count"] == 2
        assert all(r["match_type"] == "Functional Alternative" for r in result["tier_1"]["results"])

    def test_miss_no_match(self, tmp_path):
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps(_CATALOG))

        agent = SourcingAgent()
        run   = _make_run(specs={
            "manufacturer": "OtherMfg",
            "model":        "OM-001",
            "part_number":  "PN-OTHER-999",
            "voltage":      "460V",
            "category":     "Part",
        })

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH", str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        assert result["tier_1"]["count"] == 0

    def test_missing_catalog_file_returns_empty(self, tmp_path):
        agent = SourcingAgent()
        run   = _make_run()

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH",
                   str(tmp_path / "nonexistent.json")):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        assert result["tier_1"]["count"] == 0
        assert result["tier_1"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Tier 3 — warranty gate
# ---------------------------------------------------------------------------

class TestTier3WarrantyGate:
    def test_tier3_returns_empty_when_in_warranty(self):
        agent = SourcingAgent()
        run   = _make_run(warranty="in_warranty")
        specs = agent._dict_to_specs(run.asset_specs_json)
        weights = _URGENCY_WEIGHTS["predictive"]

        result = agent._run_tier3(specs, weights, "in_warranty")
        assert result == []

    def test_tier3_runs_when_not_in_warranty(self):
        agent = SourcingAgent()
        run   = _make_run(warranty="out_of_warranty")
        specs = agent._dict_to_specs(run.asset_specs_json)
        weights = _URGENCY_WEIGHTS["predictive"]

        with patch("utils.sourcing_archieved.enterprise_search._discover_national_specialists",
                   return_value=[]):
            with patch("utils.sourcing_archieved.enterprise_search._discover_aftermarket_specialists",
                       return_value=[]):
                result = agent._run_tier3(specs, weights, "out_of_warranty")

        # Returns empty list but did not short-circuit before importing
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Warranty banner
# ---------------------------------------------------------------------------

class TestWarrantyBanner:
    def test_banner_set_when_in_warranty(self, tmp_path):
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps({"suppliers": []}))

        agent = SourcingAgent()
        run   = _make_run(warranty="in_warranty")

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH", str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                result = agent.run(run)

        assert result["warranty_banner"] is not None
        assert "warranty" in result["warranty_banner"].lower()

    def test_no_banner_when_not_in_warranty(self, tmp_path):
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps({"suppliers": []}))

        agent = SourcingAgent()
        run   = _make_run(warranty="unknown")

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH", str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        assert result["warranty_banner"] is None

    def test_in_warranty_filter_recorded(self, tmp_path):
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps({"suppliers": []}))

        agent = SourcingAgent()
        run   = _make_run(warranty="in_warranty")

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH", str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                result = agent.run(run)

        assert any("in_warranty" in f for f in result["filters_applied"])


# ---------------------------------------------------------------------------
# Urgency
# ---------------------------------------------------------------------------

class TestUrgency:
    def test_emergency_urgency_applied(self, tmp_path):
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps({"suppliers": []}))

        agent = SourcingAgent()
        run   = _make_run(urgency=1.0)

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH", str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        assert result["urgency_applied"] == "emergency"

    def test_stocking_urgency_applied(self, tmp_path):
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps({"suppliers": []}))

        agent = SourcingAgent()
        run   = _make_run(urgency=0.0)

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH", str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        assert result["urgency_applied"] == "stocking"

    def test_predictive_urgency_applied(self, tmp_path):
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps({"suppliers": []}))

        agent = SourcingAgent()
        run   = _make_run(urgency=0.3)

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH", str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        assert result["urgency_applied"] == "predictive"


# ---------------------------------------------------------------------------
# TCA ranking
# ---------------------------------------------------------------------------

class TestRanking:
    def test_emergency_prefers_fastest_vendor(self):
        agent = SourcingAgent()
        options = [
            {"vendor_name": "SlowCheap", "base_price": 50.0, "lead_time_days": 20, "reliability_score": 80.0, "price_tbd": False},
            {"vendor_name": "FastPricey", "base_price": 200.0, "lead_time_days": 1, "reliability_score": 90.0, "price_tbd": False},
        ]
        ranked = agent._rank(options, _URGENCY_WEIGHTS["emergency"])
        assert ranked[0]["vendor_name"] == "FastPricey"

    def test_stocking_prefers_cheapest_vendor(self):
        agent = SourcingAgent()
        options = [
            {"vendor_name": "SlowCheap", "base_price": 50.0, "lead_time_days": 20, "reliability_score": 80.0, "price_tbd": False},
            {"vendor_name": "FastPricey", "base_price": 200.0, "lead_time_days": 1, "reliability_score": 90.0, "price_tbd": False},
        ]
        ranked = agent._rank(options, _URGENCY_WEIGHTS["stocking"])
        assert ranked[0]["vendor_name"] == "SlowCheap"

    def test_price_tbd_gets_neutral_score(self):
        agent = SourcingAgent()
        options = [
            {"vendor_name": "Priced",   "base_price": 100.0, "lead_time_days": 5, "reliability_score": 90.0, "price_tbd": False},
            {"vendor_name": "QuoteReq", "base_price": 0.0,   "lead_time_days": 3, "reliability_score": 85.0, "price_tbd": True},
        ]
        # Just verify it doesn't crash and returns both options ranked
        ranked = agent._rank(options, _URGENCY_WEIGHTS["predictive"])
        assert len(ranked) == 2

    def test_empty_options_returns_empty(self):
        agent  = SourcingAgent()
        result = agent._rank([], _URGENCY_WEIGHTS["predictive"])
        assert result == []


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------

class TestOutputContract:
    def test_all_required_keys_present(self, tmp_path):
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps({"suppliers": []}))

        agent = SourcingAgent()
        run   = _make_run()

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH", str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        required = {"tier_1", "tier_2", "tier_3", "warranty_banner", "urgency_applied", "filters_applied"}
        assert required.issubset(result.keys())

    def test_each_tier_has_results_count_status(self, tmp_path):
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps({"suppliers": []}))

        agent = SourcingAgent()
        run   = _make_run()

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH", str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        for tier_key in ("tier_1", "tier_2", "tier_3"):
            tier = result[tier_key]
            assert "results" in tier
            assert "count"   in tier
            assert "status"  in tier

    def test_tier1_result_has_required_fields(self, tmp_path):
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps(_CATALOG))

        agent = SourcingAgent()
        run   = _make_run()

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH", str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        assert result["tier_1"]["count"] >= 1
        r = result["tier_1"]["results"][0]
        for field in ("vendor_name", "base_price", "lead_time_days", "reliability_score",
                      "merchant_type", "match_type", "suitability_score", "confidence_score"):
            assert field in r, f"Expected field '{field}' in tier_1 result"


# ---------------------------------------------------------------------------
# dict_to_specs helper
# ---------------------------------------------------------------------------

class TestDictToSpecs:
    def test_converts_full_dict(self):
        agent = SourcingAgent()
        d = {
            "manufacturer": "Grundfos",
            "model": "CR32-5",
            "part_number": "96516888",
            "voltage": "460V",
            "category": "Equipment",
            "hp": "15",
            "gpm": "32",
        }
        specs = agent._dict_to_specs(d)
        assert specs.manufacturer == "Grundfos"
        assert specs.model == "CR32-5"
        assert specs.hp == "15"

    def test_defaults_missing_required_fields(self):
        agent = SourcingAgent()
        specs = agent._dict_to_specs({})
        assert specs.manufacturer == "Unknown"
        assert specs.part_number  == "UNKNOWN-PN"

    def test_ignores_unknown_fields(self):
        agent = SourcingAgent()
        # Should not raise even with extra keys not in AssetSpecs
        specs = agent._dict_to_specs({
            "manufacturer": "Test",
            "model": "M1",
            "part_number": "PN1",
            "voltage": "460V",
            "unknown_field_xyz": "should be ignored",
        })
        assert specs.manufacturer == "Test"


# ---------------------------------------------------------------------------
# Fix 2 — Alphanumeric PN normalization
# ---------------------------------------------------------------------------

class TestPartNumberNormalization:
    def test_normalize_strips_hyphens(self):
        from utils.procurement_agent.agents.sourcing_agent import normalize_part_number
        assert normalize_part_number("MR-1-1375") == "MR11375"

    def test_normalize_strips_slashes_and_dots(self):
        from utils.procurement_agent.agents.sourcing_agent import normalize_part_number
        assert normalize_part_number("22B/D6.P0") == "22BD6P0"

    def test_normalize_is_uppercase(self):
        from utils.procurement_agent.agents.sourcing_agent import normalize_part_number
        assert normalize_part_number("mr-1-1375") == "MR11375"

    def test_normalize_empty_string(self):
        from utils.procurement_agent.agents.sourcing_agent import normalize_part_number
        assert normalize_part_number("") == ""
        assert normalize_part_number(None) == ""

    def test_different_pns_do_not_collide(self):
        from utils.procurement_agent.agents.sourcing_agent import normalize_part_number
        assert normalize_part_number("MR-1-1375") != normalize_part_number("MR-8-1000")

    def test_normalized_pn_matches_in_tier1(self, tmp_path):
        """MR-1-1375 in user request matches MR11375 in catalog."""
        catalog = {
            "suppliers": [{
                "name": "National Seal",
                "reliability_score": 95.0,
                "location": "Chicago, IL",
                "website": "https://nationalseal.com",
                "inventory": [{
                    "part_number": "MR11375",
                    "manufacturer": "John Crane",
                    "price": 285.0,
                    "lead_days": 2,
                    "in_stock": True,
                }]
            }]
        }
        catalog_file = tmp_path / "catalog.json"
        catalog_file.write_text(json.dumps(catalog))

        agent = SourcingAgent()
        run   = _make_run(specs={
            "manufacturer": "John Crane",
            "model":        "MR",
            "part_number":  "MR-1-1375",
            "voltage":      "N/A",
        })

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH", str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        assert result["tier_1"]["count"] >= 1
        assert result["tier_1"]["results"][0]["match_type"] == "Exact OEM"


# ---------------------------------------------------------------------------
# Fix 4 — Manufacturer-aware PN stemming
# ---------------------------------------------------------------------------

class TestPNStemming:
    def test_endress_hauser_pmc11_stems_correctly(self):
        from utils.procurement_agent.agents.sourcing_agent import stem_part_number
        result = stem_part_number("PMC11-AA1U1HBWBJJ", "Endress+Hauser")
        assert result == "PMC11"

    def test_endress_short_form_stems(self):
        from utils.procurement_agent.agents.sourcing_agent import stem_part_number
        result = stem_part_number("FTL20-AA2A", "Endress & Hauser")
        assert result is not None
        assert result.startswith("FTL")

    def test_allen_bradley_stems_to_series(self):
        from utils.procurement_agent.agents.sourcing_agent import stem_part_number
        result = stem_part_number("22B-D6P0N104", "Allen-Bradley")
        assert result == "22B"

    def test_allen_bradley_alternate_spelling(self):
        from utils.procurement_agent.agents.sourcing_agent import stem_part_number
        result = stem_part_number("22C-D010N104", "Allen Bradley")
        assert result == "22C"

    def test_gusher_pumps_returns_none(self):
        from utils.procurement_agent.agents.sourcing_agent import stem_part_number
        assert stem_part_number("3TE-5", "Gusher Pumps") is None

    def test_john_crane_returns_none(self):
        from utils.procurement_agent.agents.sourcing_agent import stem_part_number
        assert stem_part_number("MR-1-1375", "John Crane") is None

    def test_unknown_manufacturer_returns_none(self):
        from utils.procurement_agent.agents.sourcing_agent import stem_part_number
        assert stem_part_number("ABC-123", "Generic Industrial Inc") is None

    def test_empty_pn_returns_none(self):
        from utils.procurement_agent.agents.sourcing_agent import stem_part_number
        assert stem_part_number("", "Endress+Hauser") is None


# ---------------------------------------------------------------------------
# Fix 5 — Tier 3 capability pivot
# ---------------------------------------------------------------------------

class TestTier3CapabilityPivot:
    def test_capability_pivot_fires_when_tier2_empty(self):
        agent  = SourcingAgent()
        specs  = agent._dict_to_specs({
            "manufacturer": "Endress+Hauser",
            "model":        "PMC11",
            "part_number":  "PMC11",
            "voltage":      "N/A",
            "detected_type": "pressure sensor",
        })
        weights = _URGENCY_WEIGHTS["predictive"]

        mock_tavily = MagicMock()
        mock_tavily.search.return_value = {"results": [
            {"title": "EH Distributor", "url": "https://eh-dist.com"},
        ]}

        with patch("utils.sourcing_archieved._tavily", mock_tavily):
            result = agent._run_tier3(specs, weights, "unknown", tier2_count=0)

        mock_tavily.search.assert_called_once()
        query_used = mock_tavily.search.call_args[1]["query"]
        assert "Endress" in query_used or "endress" in query_used.lower()

        assert isinstance(result, list)
        if result:
            # Seeded candidates (is_mock=True) are prepended before pivot results
            # and rank first due to higher suitability; verify at least one category present
            pivot_results  = [r for r in result if r.get("search_type") == "capability_pivot"]
            seeded_results = [r for r in result if r.get("is_mock")]
            assert pivot_results or seeded_results

    def test_capability_pivot_tags_results(self, monkeypatch, tmp_path):
        # Isolate from on-disk brand_intelligence state. The assertion below
        # requires _seeded_tier3_candidates to return [] (pure pivot results),
        # but seeding fires when brand_intelligence has a cached row for the
        # manufacturer+equipment_type (e.g. a 'grundfos|pump' row written by a
        # live harness/dev run). conftest does not isolate brand_intelligence,
        # so a polluted dev DB flips this test red regardless of test order.
        # Pointing _DB_PATH at an empty tmp file makes get_brand_relationships
        # return the empty record (no authorized_service_brands) → seeding
        # short-circuits at `if not auth_brands: return []` → pure pivot.
        # Production seeding logic is untouched; only the test's store is reset.
        from utils import brand_intelligence
        monkeypatch.setattr(brand_intelligence, "_DB_PATH", str(tmp_path / "bi_empty.sqlite"))

        agent  = SourcingAgent()
        specs  = agent._dict_to_specs({
            "manufacturer":  "Grundfos",
            "model":         "CR32-5",
            "part_number":   "96516888",
            "voltage":       "460V",
            "detected_type": "centrifugal pump",
        })
        weights = _URGENCY_WEIGHTS["predictive"]

        mock_tavily = MagicMock()
        mock_tavily.search.return_value = {"results": [
            {"title": "Grundfos Authorized", "url": "https://grundfos-auth.com"},
            {"title": "Pump Supply Co",       "url": "https://pumpsupply.com"},
        ]}

        with patch("utils.sourcing_archieved._tavily", mock_tavily):
            result = agent._run_tier3(specs, weights, "unknown", tier2_count=0)

        assert all(r.get("search_type") == "capability_pivot" for r in result)

    def test_no_pivot_when_tier2_has_results(self):
        agent  = SourcingAgent()
        specs  = agent._dict_to_specs({
            "manufacturer": "Grundfos",
            "model":        "CR32-5",
            "part_number":  "96516888",
            "voltage":      "460V",
        })
        weights = _URGENCY_WEIGHTS["predictive"]

        with patch("utils.sourcing_archieved.enterprise_search._discover_national_specialists",
                   return_value=[]):
            with patch("utils.sourcing_archieved.enterprise_search._discover_aftermarket_specialists",
                       return_value=[]):
                result = agent._run_tier3(specs, weights, "unknown", tier2_count=3)

        assert isinstance(result, list)

    def test_no_pivot_for_unknown_manufacturer(self):
        agent  = SourcingAgent()
        specs  = agent._dict_to_specs({
            "manufacturer": "Unknown",
            "model":        "UNK",
            "part_number":  "UNK-001",
            "voltage":      "N/A",
        })
        weights = _URGENCY_WEIGHTS["predictive"]

        mock_tavily = MagicMock()
        with patch("utils.sourcing_archieved._tavily", mock_tavily):
            with patch("utils.sourcing_archieved.enterprise_search._discover_national_specialists",
                       return_value=[]):
                with patch("utils.sourcing_archieved.enterprise_search._discover_aftermarket_specialists",
                           return_value=[]):
                    agent._run_tier3(specs, weights, "unknown", tier2_count=0)

        mock_tavily.search.assert_not_called()

    def test_tier3_default_tier2_count_no_pivot(self):
        """Default tier2_count=-1 must not trigger pivot (backward compat)."""
        agent  = SourcingAgent()
        specs  = agent._dict_to_specs({
            "manufacturer": "Grundfos",
            "model":        "CR32-5",
            "part_number":  "96516888",
            "voltage":      "460V",
        })
        weights = _URGENCY_WEIGHTS["predictive"]

        mock_tavily = MagicMock()
        with patch("utils.sourcing_archieved._tavily", mock_tavily):
            with patch("utils.sourcing_archieved.enterprise_search._discover_national_specialists",
                       return_value=[]):
                with patch("utils.sourcing_archieved.enterprise_search._discover_aftermarket_specialists",
                           return_value=[]):
                    agent._run_tier3(specs, weights, "unknown")  # no tier2_count arg

        mock_tavily.search.assert_not_called()

    def test_run_output_includes_tier3_capability_pivot_flag(self, tmp_path):
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps({"suppliers": []}))

        agent = SourcingAgent()
        run   = _make_run()

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH", str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        assert "tier3_capability_pivot" in result
        assert isinstance(result["tier3_capability_pivot"], bool)


# ---------------------------------------------------------------------------
# Cache-hit suitability (fix: price-DB entries now get a floor-clearing score)
# ---------------------------------------------------------------------------

class TestTier2CacheSuitability:
    """Price-DB cache hits must not enter the pipeline with suitability=0."""

    def _make_cache(self, source: str) -> dict:
        return {
            "CachedVendor": {
                "price":        250.0,
                "lead_days":    3,
                "date_fetched": "2026-05-13T00:00:00",
                "source":       source,
                "url":          "https://cachedvendor.com/product/PN-TEST-001",
            }
        }

    def _run_tier2_with_cache(self, source: str) -> list[dict]:
        agent = SourcingAgent()
        run   = _make_run()
        specs = agent._dict_to_specs(run.asset_specs_json)
        weights = _URGENCY_WEIGHTS["predictive"]

        # get_cached_prices is imported inside _call_enterprise_api, patch at source
        with patch("utils.price_db.get_cached_prices",
                   return_value=self._make_cache(source)):
            with patch("utils.sourcing_archieved.tavily_client._search_vendor_prices",
                       return_value=[]):
                with patch("utils.sourcing_archieved.llm_parsing._llm_parse_results",
                           return_value=[]):
                    return agent._run_tier2(specs, weights)

    def test_live_cache_hit_clears_suitability_floor(self):
        results = self._run_tier2_with_cache("live")
        assert any(r["vendor_name"] == "CachedVendor" for r in results), \
            "CachedVendor should not be rejected by suitability floor"
        vendor = next(r for r in results if r["vendor_name"] == "CachedVendor")
        assert vendor.get("suitability_score", 0) >= 30, \
            f"Expected suitability >= 30, got {vendor.get('suitability_score')}"

    def test_live_cache_hit_suitability_is_50(self):
        results = self._run_tier2_with_cache("live")
        vendor = next((r for r in results if r["vendor_name"] == "CachedVendor"), None)
        assert vendor is not None
        assert vendor.get("suitability_score") == 50.0

    def test_rfq_cache_hit_suitability_is_70(self):
        results = self._run_tier2_with_cache("rfq")
        vendor = next((r for r in results if r["vendor_name"] == "CachedVendor"), None)
        assert vendor is not None
        assert vendor.get("suitability_score") == 70.0

    def test_rfq_cache_hit_is_not_rejected(self):
        results = self._run_tier2_with_cache("rfq")
        vendor = next((r for r in results if r["vendor_name"] == "CachedVendor"), None)
        assert vendor is not None
        assert not vendor.get("rejection_reason"), \
            f"rfq cache hit should not be rejected, got: {vendor.get('rejection_reason')}"


# ---------------------------------------------------------------------------
# Cross-tier dedup — by vendor name OR identical listing URL (sourcing honesty).
# Resolves the URL-identical subset of §5a; NOT the alias root cause (same supplier
# under different names at DIFFERENT urls — e.g. OTC — still needs entity resolution).
# ---------------------------------------------------------------------------

class TestDedupByUrl:
    def test_same_url_different_names_collapses_to_one(self):
        # The live sealit123 case: one listing, three name spellings, same URL.
        t2 = {"results": [
            {"vendor_name": "sealit123.com", "source_url": "https://sealit123.com/p/84004-28"},
            {"vendor_name": "Seal It 123",   "source_url": "https://sealit123.com/p/84004-28/"},  # trailing slash
        ]}
        t3 = {"results": [
            {"vendor_name": "sealit123",     "source_url": "HTTPS://sealit123.com/p/84004-28"},   # case variant
        ]}
        n = _dedup_across_tiers({"results": []}, t2, t3)
        survivors = [o for o in (t2["results"] + t3["results"]) if not o.get("rejection_reason")]
        assert len(survivors) == 1
        assert survivors[0]["vendor_name"] == "sealit123.com"   # first (higher-tier) wins
        assert n == 2

    def test_same_domain_different_urls_both_survive(self):
        # Two DIFFERENT products on one marketplace must NOT be merged (URL-level, not domain).
        t2 = {"results": [
            {"vendor_name": "Grainger", "source_url": "https://grainger.com/p/AAA"},
            {"vendor_name": "Grainger Pumps", "source_url": "https://grainger.com/p/BBB"},
        ]}
        n = _dedup_across_tiers({"results": []}, t2, {"results": []})
        survivors = [o for o in t2["results"] if not o.get("rejection_reason")]
        assert len(survivors) == 2 and n == 0

    def test_same_name_still_dedups_without_url(self):
        # Existing name-based dedup is preserved when no URL is present.
        t1 = {"results": [{"vendor_name": "Acme Co"}]}
        t3 = {"results": [{"vendor_name": "Acme Co."}]}   # normalizes to same name
        n = _dedup_across_tiers(t1, {"results": []}, t3)
        assert t1["results"][0].get("rejection_reason") is None
        assert t3["results"][0].get("rejection_reason") == "duplicate_in_higher_tier"
        assert n == 1


# ---------------------------------------------------------------------------
# DEMO_MODE Tier 1 — live-only: the fabricated seed catalog + synthetic
# brand-intelligence Tier 1 fallback are gated off so a public real-sourcing
# demo surfaces only genuinely-discovered (live Tavily Tier 2/3) vendors.
# The real seed is now {"suppliers": []} and the synthetic fallback is
# permanently disabled in ALL modes (see TestTier1SeedPurged below), so the
# DEMO_MODE gate is belt-and-suspenders; these tests still use an INJECTED
# temp catalog (_FAB_CATALOG) to exercise the gate's on/off logic in isolation.
# ---------------------------------------------------------------------------

# A catalog built from the REAL (now-purged) fabricated seed entries, used as an
# INJECTED temp catalog so the DEMO_MODE-on tests prove those exact dead-domain /
# placeholder vendors (industrialcontrolsolutions.com, nationalseal.com, Acme)
# do NOT surface when the gate is on.
_FAB_CATALOG = {
    "suppliers": [
        {
            "name":            "National Seal & Bearing Co.",
            "location":        "Chicago, IL",
            "website":         "https://nationalseal.com",   # dead/parked
            "reliability_score": 95.0,
            "contact_email":   "orders@nationalseal.com",
            "inventory": [
                {"part_number": "6205-2RS-C3", "manufacturer": "SKF",
                 "description": "Deep groove ball bearing 25x52x15mm",
                 "price": 18.50, "lead_days": 1, "in_stock": True},
            ],
        },
        {
            "name":            "Industrial Control Solutions",
            "location":        "Dallas, TX",
            "website":         "https://industrialcontrolsolutions.com",  # GoDaddy parked
            "reliability_score": 92.0,
            "contact_email":   "sales@indcontrolsolutions.com",
            "inventory": [
                {"part_number": "22B-D6P0N104", "manufacturer": "Allen-Bradley",
                 "description": "PowerFlex 40 VFD 3HP 460V 3-phase",
                 "price": 875.00, "lead_days": 3, "in_stock": True},
            ],
        },
        {
            "name":            "Acme Industrial Supply",
            "location":        "Houston, TX",
            "website":         "https://acmeindustrial.com",   # placeholder
            "reliability_score": 93.0,
            "contact_email":   "orders@acmeindustrial.com",
            "inventory": [
                {"part_number": "4Z248", "manufacturer": "Dayton",
                 "description": "Industrial motor 1HP 1800RPM 56 frame",
                 "price": 285.00, "lead_days": 2, "in_stock": True},
            ],
        },
    ]
}

_FAB_VENDORS = {
    "National Seal & Bearing Co.",
    "Industrial Control Solutions",
    "Acme Industrial Supply",
}


class TestTier1DemoModeLiveOnly:
    """DEMO_MODE ON -> Tier 1 surfaces NO fabricated/seed vendor (no dead
    domain, no is_mock priced Tier 1 card) even for a part that TODAY hits the
    catalog. Tier 2/3 (mocked here) are untouched."""

    def test_skf_part_surfaces_no_fabricated_tier1_under_demo(self, tmp_path, monkeypatch):
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps(_FAB_CATALOG))
        monkeypatch.setenv("DEMO_MODE", "1")

        agent = SourcingAgent()
        run = _make_run(specs={
            "manufacturer": "SKF", "model": "6205-2RS", "part_number": "6205-2RS-C3",
            "voltage": "N/A", "category": "Part", "detected_type": "bearing",
        })

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH",
                   str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        t1 = result["tier_1"]
        assert t1["count"] == 0, f"DEMO_MODE Tier 1 must be empty, got: {t1['results']}"
        assert t1["results"] == []
        # Belt-and-suspenders: no fabricated vendor name, no dead-domain URL,
        # no is_mock priced Tier 1 card anywhere in Tier 1.
        for r in t1["results"]:
            assert r.get("vendor_name") not in _FAB_VENDORS
            assert "industrialcontrolsolutions.com" not in (r.get("source_url") or "")
            assert "nationalseal.com" not in (r.get("source_url") or "")
            assert not (r.get("is_mock") and not r.get("price_tbd", True))

    def test_allen_bradley_part_surfaces_no_fabricated_tier1_under_demo(self, tmp_path, monkeypatch):
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps(_FAB_CATALOG))
        monkeypatch.setenv("DEMO_MODE", "true")

        agent = SourcingAgent()
        run = _make_run(specs={
            "manufacturer": "Allen-Bradley", "model": "PowerFlex 40",
            "part_number": "22B-D6P0N104", "voltage": "460V",
            "category": "Part", "detected_type": "VFD",
        })

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH",
                   str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        assert result["tier_1"]["count"] == 0
        assert result["tier_1"]["results"] == []

    def test_seeded_tier1_fallback_gated_off_under_demo(self, monkeypatch):
        """The synthetic _seeded_tier1_candidates fallback is now PERMANENTLY
        disabled (returns [] in ALL modes), and the DEMO_MODE gate in
        _run_tier1 short-circuits before it anyway. With DEMO_MODE on and a
        manufacturer (Endress+Hauser) that has brand-intelligence
        authorized_service_brands but no catalog match, Tier 1 must still be []
        — no fabricated-but-URL-less 'Arkim Network' vendor either way."""
        monkeypatch.setenv("DEMO_MODE", "1")

        agent = SourcingAgent()
        # No catalog patch needed -> real catalog file (now empty); Endress+Hauser
        # is not in it, so the only path to a Tier 1 result would be
        # _seeded_tier1_candidates (now unconditionally []).
        run = _make_run(specs={
            "manufacturer": "Endress+Hauser", "model": "Cerabar M PMC11",
            "part_number": "PMC11-AA1V1HFVXJA", "voltage": "N/A",
            "category": "Part", "detected_type": "pressure transmitter",
        })

        with patch.object(agent, "_run_tier2", return_value=[]):
            with patch.object(agent, "_run_tier3", return_value=[]):
                result = agent.run(run)

        t1 = result["tier_1"]
        assert t1["count"] == 0, f"DEMO_MODE Tier 1 fallback must be gated, got: {t1['results']}"
        # No synthetic is_mock 'Arkim Network' Tier 1 card.
        assert not any(r.get("is_mock") for r in t1["results"])

    def test_demo_mode_off_is_inert_catalog_still_works(self, tmp_path, monkeypatch):
        """DEMO_MODE OFF -> the gate does NOT activate, so an INJECTED catalog
        loads and matches (regression guard for the gate's off-state). The real
        seed is now empty (see TestTier1SeedPurged); this injects a temp catalog
        to exercise gate inertness in isolation."""
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps(_FAB_CATALOG))
        monkeypatch.delenv("DEMO_MODE", raising=False)

        agent = SourcingAgent()
        run = _make_run(specs={
            "manufacturer": "SKF", "model": "6205-2RS", "part_number": "6205-2RS-C3",
            "voltage": "N/A", "category": "Part", "detected_type": "bearing",
        })

        with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH",
                   str(catalog_file)):
            with patch.object(agent, "_run_tier2", return_value=[]):
                with patch.object(agent, "_run_tier3", return_value=[]):
                    result = agent.run(run)

        # The injected temp catalog's SKF-by-manufacturer match surfaces (proving
        # the DEMO_MODE gate stayed inert when off). The real seed is empty, but
        # this test deliberately injects a populated catalog to test the gate.
        assert result["tier_1"]["count"] >= 1
        names = {r["vendor_name"] for r in result["tier_1"]["results"]}
        assert "National Seal & Bearing Co." in names

    def test_demo_mode_falsy_tokens_are_inert(self, tmp_path, monkeypatch):
        """Non-truthy DEMO_MODE tokens (0/false/no/empty) must NOT activate the
        gate — fails safe to today's catalog behaviour."""
        catalog_file = tmp_path / "mock_tier1_suppliers.json"
        catalog_file.write_text(json.dumps(_FAB_CATALOG))

        agent = SourcingAgent()
        run = _make_run(specs={
            "manufacturer": "SKF", "model": "6205-2RS", "part_number": "6205-2RS-C3",
            "voltage": "N/A", "category": "Part", "detected_type": "bearing",
        })

        for off in ("0", "false", "no", "", "junk"):
            monkeypatch.setenv("DEMO_MODE", off)
            with patch("utils.procurement_agent.agents.sourcing_agent._TIER1_CATALOG_PATH",
                       str(catalog_file)):
                with patch.object(agent, "_run_tier2", return_value=[]):
                    with patch.object(agent, "_run_tier3", return_value=[]):
                        result = agent.run(run)
            assert result["tier_1"]["count"] >= 1, \
                f"DEMO_MODE={off!r} must be inert (catalog still works)"


# ---------------------------------------------------------------------------
# PERMANENT source-clean guard — the fabricated Tier 1 seed vendors are gone
# at the source and the synthetic fallback is disabled in ALL modes, so no
# fabricated Tier 1 vendor can be sourced or cached in demo OR non-demo.
# ---------------------------------------------------------------------------
class TestTier1SeedPurgedNoFabricatedVendors:
    """The real seed (data/mock_tier1_suppliers.json) is permanently empty of
    fabricated vendors, and _seeded_tier1_candidates returns [] unconditionally,
    so Tier 1 is honestly empty in ALL modes until real onboarded suppliers
    exist. These guard that state at the source and through the agent in
    non-demo (the demo path is covered by TestTier1DemoModeLiveOnly)."""

    _REAL_CATALOG_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))),
        "data", "mock_tier1_suppliers.json",
    )

    # The fabricated vendors/domains that must NEVER surface again, in any mode.
    _FAB_VENDORS = {
        "National Seal & Bearing Co.",
        "Industrial Control Solutions",
        "Acme Industrial Supply",
        "Gulf Coast Electric Motor Service",
        "Southern Pump & Equipment",
    }
    _FAB_DOMAINS = {
        "nationalseal.com", "industrialcontrolsolutions.com",
        "acmeindustrial.com", "gulfcoastmotor.com", "southernpump.com",
        "indcontrolsolutions.com",
    }

    def test_real_seed_catalog_is_empty(self):
        """The seed file must be a valid catalog with NO suppliers — the
        permanent source-level purge of all fabricated vendors."""
        with open(self._REAL_CATALOG_PATH, "r") as fh:
            catalog = json.load(fh)
        assert "suppliers" in catalog, "seed must keep valid {\"suppliers\": []} structure"
        assert catalog["suppliers"] == [], (
            f"seed must be empty of fabricated vendors, got: "
            f"{[s.get('name') for s in catalog['suppliers']]}"
        )

    def test_no_fabricated_tier1_vendor_in_non_demo(self, monkeypatch):
        """DEMO_MODE OFF + the real (empty) seed + a manufacturer that WOULD
        have triggered the synthetic _seeded_tier1_candidates fallback (it has
        brand-intelligence authorized_service_brands but no catalog match).
        Tier 1 must be [] — no fabricated 'Arkim Network' is_mock card and no
        fabricated seed vendor, even with the gate off. This is the non-demo
        half of 'no fabricated vendor in EITHER mode'."""
        monkeypatch.delenv("DEMO_MODE", raising=False)

        agent = SourcingAgent()
        run = _make_run(specs={
            "manufacturer": "Endress+Hauser", "model": "Cerabar M PMC11",
            "part_number": "PMC11-AA1V1HFVXJA", "voltage": "N/A",
            "category": "Part", "detected_type": "pressure transmitter",
        })

        # No catalog patch -> reads the real (now-empty) seed file.
        with patch.object(agent, "_run_tier2", return_value=[]):
            with patch.object(agent, "_run_tier3", return_value=[]):
                result = agent.run(run)

        t1 = result["tier_1"]
        assert t1["count"] == 0, (
            f"non-demo Tier 1 must be empty (no fabricated vendor), got: {t1['results']}"
        )
        assert t1["results"] == []
        # Belt-and-suspenders: no fabricated vendor name, no dead-domain URL,
        # no synthetic is_mock 'Arkim Network' card anywhere in Tier 1.
        for r in t1["results"]:
            assert r.get("vendor_name") not in self._FAB_VENDORS
            assert not any(d in (r.get("source_url") or "") for d in self._FAB_DOMAINS)
            assert not (r.get("is_mock") and not r.get("price_tbd", True))

    def test_seeded_tier1_fallback_unconditionally_disabled(self, monkeypatch):
        """_seeded_tier1_candidates must return [] regardless of DEMO_MODE —
        called directly with a manufacturer that has authorized_service_brands,
        in BOTH demo-on and demo-off states."""
        from utils.models import AssetSpecs
        agent = SourcingAgent()
        specs = AssetSpecs(
            manufacturer="Endress+Hauser", model="Cerabar M PMC11",
            part_number="PMC11-AA1V1HFVXJA", voltage="N/A", category="Part",
            detected_type="pressure transmitter",
        )
        for mode in ("1", None):  # demo ON, then demo OFF (env unset)
            if mode is None:
                monkeypatch.delenv("DEMO_MODE", raising=False)
            else:
                monkeypatch.setenv("DEMO_MODE", mode)
            assert agent._seeded_tier1_candidates(specs) == [], (
                f"_seeded_tier1_candidates must be unconditionally [] (DEMO_MODE={mode!r})"
            )
