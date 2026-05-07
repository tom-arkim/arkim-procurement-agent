"""
Tests for SourcingAgent — three-tier parallel sourcing, urgency ranking, warranty filtering.

Tier 2 and Tier 3 calls (Tavily + Anthropic) are mocked. Tier 1 uses a temp catalog file.
"""

import json
import os
from unittest.mock import patch, MagicMock

import pytest

from utils.models import ProcurementRun
from utils.procurement_agent.agents.sourcing_agent import (
    SourcingAgent,
    _URGENCY_WEIGHTS,
    _WARRANTY_BANNER,
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
) -> ProcurementRun:
    return ProcurementRun(
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
            assert result[0].get("search_type") == "capability_pivot"

    def test_capability_pivot_tags_results(self):
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
