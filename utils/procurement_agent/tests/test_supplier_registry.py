"""
Tests for utils/supplier_registry.py — Apollo cache schema extension.

Covers the idempotent migration, the domain-keyed upsert_apollo_data (including
JSON round-trip for the contact-resolution fields), and the needs_reenrichment
staleness logic. The DB path is isolated to a tmp file via monkeypatch (mirrors
test_price_db.py), so the real data/supplier_registry.sqlite is never touched.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from utils import supplier_registry


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point the registry at a throwaway sqlite file under tmp_path."""
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    return supplier_registry


def _table_columns(sr) -> set:
    conn = sr._get_conn()
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(suppliers)").fetchall()}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

class TestMigration:
    def test_apollo_columns_added(self, isolated_db):
        cols = _table_columns(isolated_db)
        for col in isolated_db._APOLLO_COLUMNS:
            assert col in cols, f"missing migrated column: {col}"
        # original columns still present
        for col in ("id", "domain", "name", "onboarding_status", "created_at"):
            assert col in cols

    def test_idempotent_no_error_no_dataloss(self, isolated_db):
        sr = isolated_db
        sr.create_stub("Phoenix Pumps", domain="phoenixpumps.com")

        # Re-run the migration repeatedly — must not raise and must not drop data.
        for _ in range(3):
            conn = sr._get_conn()
            try:
                sr._migrate(conn)  # explicit re-run on top of the _get_conn() run
            finally:
                conn.close()

        rec = sr.lookup_by_domain("phoenixpumps.com")
        assert rec is not None
        assert rec["name"] == "Phoenix Pumps"
        # seeded vendors also survive
        assert sr.lookup_by_domain("grainger.com") is not None


# ---------------------------------------------------------------------------
# upsert_apollo_data
# ---------------------------------------------------------------------------

class TestUpsertApolloData:
    def test_write_and_readback_including_json(self, isolated_db):
        sr = isolated_db
        ok = sr.upsert_apollo_data("phoenixpumps.com", {
            "apollo_org_name": "Phoenix Pumps Inc",
            "apollo_description": "Industrial pump distributor",
            "apollo_industry": "industrial automation",
            "apollo_keywords": ["pumps", "seals"],
            "apollo_country": "United States",
            "apollo_state": "Arizona",
            "apollo_raw_address": "123 Main St, Phoenix, AZ",
            "is_us_confirmed": True,
            "suitability_status": "confirmed",
            "apollo_departmental_head_count": {"sales": 5, "engineering": 12},
            "apollo_technology_names": ["Shopify", "Salesforce"],
        })
        assert ok is True

        rec = sr.lookup_by_domain("phoenixpumps.com")
        assert rec["apollo_org_name"] == "Phoenix Pumps Inc"
        assert rec["apollo_description"] == "Industrial pump distributor"
        assert rec["apollo_industry"] == "industrial automation"
        assert rec["apollo_country"] == "United States"
        assert rec["apollo_state"] == "Arizona"
        assert rec["apollo_raw_address"] == "123 Main St, Phoenix, AZ"
        assert rec["is_us_confirmed"] == 1  # bool -> int
        assert rec["suitability_status"] == "confirmed"
        assert rec["apollo_enriched_at"]  # auto-stamped

        # Contact-resolution JSON fields round-trip.
        assert json.loads(rec["apollo_keywords"]) == ["pumps", "seals"]
        assert json.loads(rec["apollo_departmental_head_count"]) == {"sales": 5, "engineering": 12}
        assert json.loads(rec["apollo_technology_names"]) == ["Shopify", "Salesforce"]

    def test_upsert_creates_row_when_domain_absent(self, isolated_db):
        sr = isolated_db
        assert sr.lookup_by_domain("brand-new-co.com") is None
        ok = sr.upsert_apollo_data("brand-new-co.com", {"suitability_status": "unconfirmed_flag_human"})
        assert ok is True
        rec = sr.lookup_by_domain("brand-new-co.com")
        assert rec is not None
        assert rec["suitability_status"] == "unconfirmed_flag_human"
        assert rec["onboarding_status"] == "discovery_only"  # minimal stub default

    def test_confirmed_suitability_coexists_with_discovery_only(self, isolated_db):
        """suitability_status is independent of onboarding_status (module docstring)."""
        sr = isolated_db
        sr.create_stub("Phoenix Pumps", domain="phoenixpumps.com")
        sr.upsert_apollo_data("phoenixpumps.com", {"suitability_status": "confirmed"})
        rec = sr.lookup_by_domain("phoenixpumps.com")
        assert rec["suitability_status"] == "confirmed"
        assert rec["onboarding_status"] == "discovery_only"  # untouched by Apollo upsert

    def test_ignores_non_apollo_fields(self, isolated_db):
        """Apollo upsert must not write onboarding-lifecycle columns (whitelist boundary)."""
        sr = isolated_db
        sr.create_stub("X", domain="x.com")
        sr.upsert_apollo_data("x.com", {
            "onboarding_status": "onboarded_arkim_supplier",  # not an Apollo column -> ignored
            "apollo_industry": "valves",
        })
        rec = sr.lookup_by_domain("x.com")
        assert rec["apollo_industry"] == "valves"
        assert rec["onboarding_status"] == "discovery_only"  # unchanged

    def test_caller_supplied_enriched_at_is_preserved(self, isolated_db):
        sr = isolated_db
        pinned = (datetime.utcnow() - timedelta(days=10)).isoformat()
        sr.upsert_apollo_data("x.com", {"suitability_status": "confirmed", "apollo_enriched_at": pinned})
        rec = sr.lookup_by_domain("x.com")
        assert rec["apollo_enriched_at"] == pinned

    def test_empty_domain_returns_false(self, isolated_db):
        assert isolated_db.upsert_apollo_data("", {"apollo_industry": "x"}) is False

    def test_no_apollo_fields_returns_false(self, isolated_db):
        isolated_db.create_stub("X", domain="x.com")
        assert isolated_db.upsert_apollo_data("x.com", {"foo": "bar"}) is False

    def test_url_normalizes_to_domain(self, isolated_db):
        sr = isolated_db
        sr.upsert_apollo_data("https://www.phoenixpumps.com/contact", {"apollo_industry": "pumps"})
        rec = sr.lookup_by_domain("phoenixpumps.com")
        assert rec is not None
        assert rec["apollo_industry"] == "pumps"


# ---------------------------------------------------------------------------
# needs_reenrichment (pure logic)
# ---------------------------------------------------------------------------

class TestStaleness:
    def test_fresh_confirmed_not_stale(self):
        s = {"onboarding_status": "discovery_only",
             "apollo_enriched_at": datetime.utcnow().isoformat()}
        assert supplier_registry.needs_reenrichment(s) is False

    def test_stale_confirmed_needs_reenrich(self):
        old = (datetime.utcnow() - timedelta(days=200)).isoformat()
        s = {"onboarding_status": "discovery_only", "apollo_enriched_at": old}
        assert supplier_registry.needs_reenrichment(s) is True

    def test_onboarded_exempt_even_if_ancient(self):
        old = (datetime.utcnow() - timedelta(days=5000)).isoformat()
        s = {"onboarding_status": "onboarded_arkim_supplier", "apollo_enriched_at": old}
        assert supplier_registry.needs_reenrichment(s) is False

    def test_invited_not_exempt(self):
        old = (datetime.utcnow() - timedelta(days=200)).isoformat()
        s = {"onboarding_status": "invited", "apollo_enriched_at": old}
        assert supplier_registry.needs_reenrichment(s) is True

    def test_never_enriched_non_onboarded_is_stale(self):
        s = {"onboarding_status": "discovery_only", "apollo_enriched_at": None}
        assert supplier_registry.needs_reenrichment(s) is True

    def test_unparseable_date_treated_stale(self):
        s = {"onboarding_status": "discovery_only", "apollo_enriched_at": "not-a-date"}
        assert supplier_registry.needs_reenrichment(s) is True

    def test_tz_aware_fresh_not_stale(self):
        # tz-AWARE timestamp (+00:00) must not crash the naive subtraction.
        s = {"onboarding_status": "discovery_only",
             "apollo_enriched_at": datetime.now(timezone.utc).isoformat()}
        assert supplier_registry.needs_reenrichment(s) is False

    def test_tz_aware_stale_needs_reenrich(self):
        old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        s = {"onboarding_status": "discovery_only", "apollo_enriched_at": old}
        assert supplier_registry.needs_reenrichment(s) is True

    def test_falsy_supplier_false(self):
        assert supplier_registry.needs_reenrichment(None) is False
        assert supplier_registry.needs_reenrichment({}) is False

    def test_ttl_param_respected(self):
        d = (datetime.utcnow() - timedelta(days=10)).isoformat()
        s = {"onboarding_status": "discovery_only", "apollo_enriched_at": d}
        assert supplier_registry.needs_reenrichment(s, ttl_days=5) is True
        assert supplier_registry.needs_reenrichment(s, ttl_days=30) is False

    def test_staleness_via_store_roundtrip(self, isolated_db):
        sr = isolated_db
        old = (datetime.utcnow() - timedelta(days=200)).isoformat()
        sr.upsert_apollo_data("stale-co.com", {"suitability_status": "confirmed", "apollo_enriched_at": old})
        rec = sr.lookup_by_domain("stale-co.com")
        assert sr.needs_reenrichment(rec) is True

        fresh = datetime.utcnow().isoformat()
        sr.upsert_apollo_data("fresh-co.com", {"suitability_status": "confirmed", "apollo_enriched_at": fresh})
        assert sr.needs_reenrichment(sr.lookup_by_domain("fresh-co.com")) is False
