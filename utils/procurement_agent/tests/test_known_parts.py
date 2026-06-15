"""
Tests for utils/known_parts — the part→supplier graph cache.

Covers the two hard requirements (canonical part-key; domain-keyed edges), the
durable/volatile split (a stale price never drops the durable edge), and the
determinism win (same key → same supplier set across reads once cached).
"""

from datetime import datetime, timedelta

import pytest

from utils import known_parts


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(known_parts, "_DB_PATH", str(tmp_path / "known_parts.json"))
    return known_parts


# ---------------------------------------------------------------------------
# HARD REQ 1 — canonical part-key
# ---------------------------------------------------------------------------

class TestCanonicalPartKey:
    def test_manufacturer_aliases_collapse_to_one_key(self):
        k1 = known_parts.canonical_part_key("Gusher", "84004-28-C238CBC")
        k2 = known_parts.canonical_part_key("Gusher Pumps", "84004-28-C238CBC")
        k3 = known_parts.canonical_part_key("Gusher Pumps Type 21", "84004-28-C238CBC")
        assert k1 == k2 == k3
        assert k1 == "gusher pumps|8400428C238CBC"

    def test_pn_format_variants_collapse(self):
        a = known_parts.canonical_part_key("Gusher Pumps", "84004-28-C238CBC")
        b = known_parts.canonical_part_key("Gusher Pumps", "84004 28 c238cbc")
        assert a == b

    def test_blank_pn_disables_caching(self):
        # PN-less / placeholder PNs are too ambiguous to key — return "" (no caching).
        assert known_parts.canonical_part_key("Gusher Pumps", "") == ""
        assert known_parts.canonical_part_key("Gusher Pumps", "UNKNOWN-PN") == ""
        assert known_parts.canonical_part_key("Gusher Pumps", None) == ""


# ---------------------------------------------------------------------------
# HARD REQ 2 — domain-keyed edges (the "Seal It 123" / "sealit123.com" case)
# ---------------------------------------------------------------------------

class TestDomainKeyedEdges:
    def test_same_domain_different_names_dedupe_to_one_edge(self, isolated):
        key = known_parts.canonical_part_key("Gusher Pumps", "84004-28-C238CBC")
        isolated.upsert_edges(key, [
            {"vendor_name": "Seal It 123", "source_url": "https://sealit123.com/p/84004", "base_price": 53.25,
             "match_type": "Exact OEM", "suitability_score": 75},
            {"vendor_name": "sealit123.com", "source_url": "https://www.sealit123.com/p/84004/", "base_price": 53.25,
             "match_type": "Exact OEM", "suitability_score": 75},
        ])
        edges = isolated.get_edges(key)
        assert len(edges) == 1                       # one supplier, not two
        assert edges[0]["supplier_id"] == "sealit123.com"
        assert edges[0]["purchase_channel"] == "marketplace"  # registered + priced

    def test_url_less_supplier_keyed_by_name_fallback(self, isolated):
        key = known_parts.canonical_part_key("Gusher Pumps", "84004-28-C238CBC")
        isolated.upsert_edges(key, [
            {"vendor_name": "Phoenix Pumps", "price_tbd": True, "requires_rfq": True,
             "suitability_score": 88},  # seeded RFQ-only distributor, no URL
        ])
        edges = isolated.get_edges(key)
        assert len(edges) == 1
        assert edges[0]["supplier_id"] == "name:phoenixpumps"
        assert edges[0]["purchase_channel"] == "rfq"   # no buyable price
        assert edges[0]["price"] is None


# ---------------------------------------------------------------------------
# Durable / volatile split — a stale price must NOT drop the durable edge
# ---------------------------------------------------------------------------

class TestFreshness:
    def test_stale_price_flags_but_keeps_edge(self, isolated):
        key = known_parts.canonical_part_key("Gusher Pumps", "84004-28-C238CBC")
        isolated.upsert_edges(key, [
            {"vendor_name": "Seal It 123", "source_url": "https://sealit123.com/x", "base_price": 53.25,
             "match_type": "Exact OEM", "suitability_score": 75},
        ])
        # Backdate the price beyond the TTL.
        db = isolated._load()
        edge = next(iter(db[key]["edges"].values()))
        edge["price_date"] = (datetime.utcnow() - timedelta(days=known_parts.PRICE_TTL_DAYS + 5)).isoformat()
        isolated._save(db)

        edges = isolated.get_edges(key)
        assert len(edges) == 1                         # durable edge persists
        assert edges[0]["price"] == 53.25              # price retained (not discarded)
        assert edges[0]["price_stale"] is True         # but flagged stale -> caller marks unverified

    def test_fresh_price_not_flagged(self, isolated):
        key = known_parts.canonical_part_key("Gusher Pumps", "84004-28-C238CBC")
        isolated.upsert_edges(key, [
            {"vendor_name": "Seal It 123", "source_url": "https://sealit123.com/x", "base_price": 53.25,
             "suitability_score": 75},
        ])
        assert isolated.get_edges(key)[0]["price_stale"] is False


# ---------------------------------------------------------------------------
# The determinism win — same key returns the same supplier set across reads
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_same_key_same_supplier_set_across_reads(self, isolated):
        key = known_parts.canonical_part_key("Gusher Pumps", "84004-28-C238CBC")
        # A representative gusher candidate set, incl. the Tier-3 vendors that rotated live.
        isolated.upsert_edges(key, [
            {"vendor_name": "Seal It 123", "source_url": "https://sealit123.com/x", "base_price": 53.25, "suitability_score": 75},
            {"vendor_name": "Seals-Direct", "source_url": "https://seals-direct.com/x", "base_price": 85.28, "suitability_score": 45},
            {"vendor_name": "Industrial Pump Parts", "source_url": "https://industrialpumpparts.com/x", "base_price": 173.0, "suitability_score": 45},
            {"vendor_name": "Phoenix Pumps", "price_tbd": True, "suitability_score": 88},
        ])
        first = [e["supplier_id"] for e in isolated.get_edges(key)]
        second = [e["supplier_id"] for e in isolated.get_edges(key)]
        assert first == second                          # identical, in identical order
        assert set(first) == {"sealit123.com", "seals-direct.com", "industrialpumpparts.com", "name:phoenixpumps"}
