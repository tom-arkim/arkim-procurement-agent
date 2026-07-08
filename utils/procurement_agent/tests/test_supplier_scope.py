"""
Night 3 — TIER1_V2 supplier-scope registry tests (schema + API + state machine).

Covers the research-model supplier entity: brands x classes x territory, tri-state
authorization, enforced lifecycle, graduation from Apollo refresh. ALL behavior is
behind TIER1_V2; the inertness section proves flag-off is byte-identical to the
pre-Night-3 registry (the extensions no-op: writes return False, reads return
empty, the clarifier's needs_reenrichment is unchanged).

The DB path is isolated to a tmp file (mirrors test_supplier_registry.py /
test_orders.py). TIER1_V2 is turned ON per-test via monkeypatch so the redesign
path is exercised; the inertness tests turn it OFF and assert dormancy.
"""

import json
from datetime import datetime, timedelta

import pytest

from utils import supplier_registry as sr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point the registry at a throwaway sqlite file + turn TIER1_V2 ON."""
    monkeypatch.setattr(sr, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sr, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    monkeypatch.setattr(sr, "TIER1_V2", True)
    return sr


@pytest.fixture
def isolated_db_off(tmp_path, monkeypatch):
    """Same isolation but TIER1_V2 OFF (for inertness/dormancy tests)."""
    monkeypatch.setattr(sr, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sr, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    monkeypatch.setattr(sr, "TIER1_V2", False)
    return sr


def _table_names(s) -> set:
    conn = s._get_conn()
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()


def _supplier_columns(s) -> set:
    conn = s._get_conn()
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(suppliers)").fetchall()}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema migration (T2)
# ---------------------------------------------------------------------------

class TestSchemaMigration:
    def test_scope_child_tables_created(self, isolated_db):
        names = _table_names(isolated_db)
        for t in ("supplier_classes", "supplier_brands", "supplier_local_service"):
            assert t in names, f"missing scope table {t}"

    def test_tier1_supplier_columns_added(self, isolated_db):
        cols = _supplier_columns(isolated_db)
        for col in isolated_db._TIER1_SUPPLIER_COLUMNS:
            assert col in cols, f"missing tier1 column {col}"
        # original + Apollo columns still present
        for col in ("id", "domain", "name", "onboarding_status",
                    "apollo_enriched_at", "contact_email"):
            assert col in cols

    def test_migration_idempotent_no_dataloss(self, isolated_db):
        s = isolated_db
        s.create_stub("Phoenix Pumps", domain="phoenixpumps.com")
        for _ in range(3):
            conn = s._get_conn()
            try:
                s._migrate(conn)
            finally:
                conn.close()
        rec = s.lookup_by_domain("phoenixpumps.com")
        assert rec is not None and rec["name"] == "Phoenix Pumps"

    def test_legacy_apollo_row_survives_unmodified(self, isolated_db):
        """An Apollo-cache-only row (no scope data) remains a valid record — the
        coexistence guarantee (T4): existing rows are valid discovered-stage
        records, Apollo upsert still works."""
        s = isolated_db
        ok = s.upsert_apollo_data("legacy-co.com", {
            "suitability_status": "confirmed",
            "apollo_country": "United States",
        })
        assert ok is True
        rec = s.lookup_by_domain("legacy-co.com")
        assert rec["onboarding_status"] == "discovery_only"
        assert rec["suitability_status"] == "confirmed"
        # tier1_lifecycle is NULL on a legacy row (no scope set yet)
        assert rec.get("tier1_lifecycle") is None


# ---------------------------------------------------------------------------
# Scope CRUD round-trips (T2/T3)
# ---------------------------------------------------------------------------

class TestScopeClassesCRUD:
    def test_set_and_get_classes_roundtrip(self, isolated_db):
        s = isolated_db
        ok = s.set_supplier_classes("acme.com", [
            {"class_id": "SEAL", "is_core": True, "confidence": 0.9,
             "unspsc": "31162701", "source": "manual"},
            {"class_id": "PUMP", "is_core": True, "confidence": 0.85},
            {"class_id": "HOSE", "is_core": False, "confidence": 0.5,
             "subtype": "hydraulic"},
        ], set_by="ops@example.com")
        assert ok is True
        classes = s.get_supplier_classes("acme.com")
        cids = {c["class_id"] for c in classes}
        assert cids == {"SEAL", "PUMP", "HOSE"}
        seal = next(c for c in classes if c["class_id"] == "SEAL")
        assert seal["is_core"] == 1
        assert seal["confidence"] == 0.9
        assert seal["unspsc"] == "31162701"
        hose = next(c for c in classes if c["class_id"] == "HOSE")
        assert hose["is_core"] == 0
        assert hose["subtype"] == "hydraulic"
        # provenance stamped on the supplier row
        rec = s.lookup_by_domain("acme.com")
        assert rec["scope_set_by"] == "ops@example.com"
        assert rec["scope_set_at"] is not None

    def test_class_id_uppercased(self, isolated_db):
        s = isolated_db
        s.set_supplier_classes("acme.com", [{"class_id": "seal"}])
        classes = s.get_supplier_classes("acme.com")
        assert classes[0]["class_id"] == "SEAL"

    def test_replace_is_idempotent_full_replace(self, isolated_db):
        s = isolated_db
        s.set_supplier_classes("acme.com", [{"class_id": "SEAL"}, {"class_id": "PUMP"}])
        assert {c["class_id"] for c in s.get_supplier_classes("acme.com")} == {"SEAL", "PUMP"}
        # Full replace drops SEAL/PUMP, lands only VALVE.
        s.set_supplier_classes("acme.com", [{"class_id": "VALVE"}])
        assert {c["class_id"] for c in s.get_supplier_classes("acme.com")} == {"VALVE"}

    def test_set_classes_creates_stub_when_domain_absent(self, isolated_db):
        s = isolated_db
        assert s.lookup_by_domain("brand-new.com") is None
        s.set_supplier_classes("brand-new.com", [{"class_id": "SEAL"}])
        assert s.lookup_by_domain("brand-new.com") is not None


class TestScopeBrandsCRUD:
    def test_set_and_get_brands_roundtrip(self, isolated_db):
        s = isolated_db
        ok = s.set_supplier_brands("acme.com", [
            {"brand_id": "goulds", "relationship": "AUTHORIZED",
             "authorized_territory": "US-Midwest", "classes_for_brand": ["PUMP"],
             "evidence": "OEM distributor letter 2024", "confidence": 0.95},
            {"brand_id": "skf", "relationship": "CARRIES", "confidence": 0.7},
            {"brand_id": "aftermarket-co", "relationship": "AFTERMARKET_COMPATIBLE",
             "confidence": 0.5},
        ])
        assert ok is True
        brands = s.get_supplier_brands("acme.com")
        by_brand = {b["brand_id"]: b for b in brands}
        assert set(by_brand) == {"goulds", "skf", "aftermarket-co"}
        assert by_brand["goulds"]["relationship"] == "AUTHORIZED"
        assert by_brand["goulds"]["authorized_territory"] == "US-Midwest"
        assert by_brand["goulds"]["classes_for_brand"] == ["PUMP"]  # JSON decoded
        assert by_brand["skf"]["relationship"] == "CARRIES"
        assert by_brand["aftermarket-co"]["relationship"] == "AFTERMARKET_COMPATIBLE"

    def test_invalid_relationship_skipped(self, isolated_db):
        s = isolated_db
        s.set_supplier_brands("acme.com", [
            {"brand_id": "good", "relationship": "AUTHORIZED"},
            {"brand_id": "bad", "relationship": "UNSANCTIONED"},  # not in vocab
        ])
        brands = {b["brand_id"] for b in s.get_supplier_brands("acme.com")}
        assert brands == {"good"}  # the bad one was skipped, the good one kept

    def test_relationship_uppercased(self, isolated_db):
        s = isolated_db
        s.set_supplier_brands("acme.com", [{"brand_id": "x", "relationship": "authorized"}])
        assert s.get_supplier_brands("acme.com")[0]["relationship"] == "AUTHORIZED"


class TestScopeTerritoryCRUD:
    def test_nationwide_ship_area(self, isolated_db):
        s = isolated_db
        ok = s.set_supplier_territory("acme.com", {"kind": "NATIONWIDE_US"})
        assert ok is True
        terr = s.get_supplier_territory("acme.com")
        assert terr["ship_area"] == {"kind": "NATIONWIDE_US"}
        assert terr["local_service"] == []

    def test_states_ship_area(self, isolated_db):
        s = isolated_db
        s.set_supplier_territory("acme.com", {"kind": "STATES", "states": ["NY", "NJ", "CT"]})
        terr = s.get_supplier_territory("acme.com")
        assert terr["ship_area"]["kind"] == "STATES"
        assert set(terr["ship_area"]["states"]) == {"NY", "NJ", "CT"}

    def test_local_service_area_roundtrip(self, isolated_db):
        s = isolated_db
        s.set_supplier_territory(
            "acme.com",
            {"kind": "STATES", "states": ["NY"]},
            local_service=[
                {"branch_zip": "10001", "radius": 50, "services": ["repair", "stocking"]},
                {"branch_zip": "11201", "radius": 25, "services": ["repair"]},
            ],
        )
        terr = s.get_supplier_territory("acme.com")
        ls = terr["local_service"]
        zips = {l["branch_zip"] for l in ls}
        assert zips == {"10001", "11201"}
        first = next(l for l in ls if l["branch_zip"] == "10001")
        assert first["radius"] == 50
        assert first["services"] == ["repair", "stocking"]  # JSON decoded

    def test_invalid_ship_area_rejected(self, isolated_db):
        s = isolated_db
        assert s.set_supplier_territory("acme.com", {"kind": "GLOBAL"}) is False
        assert s.set_supplier_territory("acme.com", {"states": ["NY"]}) is False  # no kind
        # nothing written
        assert s.get_supplier_territory("acme.com")["ship_area"] is None


class TestScopeVerticals:
    def test_set_and_get_verticals(self, isolated_db):
        s = isolated_db
        s.set_supplier_verticals("acme.com", ["food_beverage", "pharma"])
        assert s.get_supplier_verticals("acme.com") == ["food_beverage", "pharma"]


# ---------------------------------------------------------------------------
# Enforced lifecycle state machine (T3)
# ---------------------------------------------------------------------------

class TestTier1StateMachine:
    def test_legal_forward_lifecycle(self, isolated_db):
        s = isolated_db
        s.create_stub("Acme", domain="acme.com")
        for nxt in ("discovered", "contacted", "quoted", "onboarding", "onboarded"):
            r = s.tier1_transition("acme.com", nxt)
            assert r is not None, f"transition to {nxt} rejected"
            assert r["tier1_lifecycle"] == nxt

    def test_skip_ahead_rejected(self, isolated_db):
        s = isolated_db
        s.create_stub("Acme", domain="acme.com")
        # discovered -> quoted is illegal (must go through contacted)
        assert s.tier1_transition("acme.com", "discovered") is not None
        assert s.tier1_transition("acme.com", "quoted") is None  # skip-ahead blocked
        assert s.get_tier1_lifecycle("acme.com") == "discovered"  # unchanged

    def test_backward_rejected(self, isolated_db):
        s = isolated_db
        s.create_stub("Acme", domain="acme.com")
        for nxt in ("discovered", "contacted", "quoted"):
            assert s.tier1_transition("acme.com", nxt) is not None
        # quoted -> contacted is backward, illegal
        assert s.tier1_transition("acme.com", "contacted") is None
        assert s.get_tier1_lifecycle("acme.com") == "quoted"

    def test_suspend_from_any_pre_onboarded(self, isolated_db):
        s = isolated_db
        s.create_stub("Acme", domain="acme.com")
        for start in ("discovered", "contacted", "quoted", "onboarding"):
            s.create_stub(f"S{start}", domain=f"{start}.com")
            assert s.tier1_transition(f"{start}.com", "discovered") is not None
            # advance to the start state
            if start == "contacted":
                s.tier1_transition(f"{start}.com", "contacted")
            elif start == "quoted":
                s.tier1_transition(f"{start}.com", "contacted")
                s.tier1_transition(f"{start}.com", "quoted")
            elif start == "onboarding":
                s.tier1_transition(f"{start}.com", "contacted")
                s.tier1_transition(f"{start}.com", "quoted")
                s.tier1_transition(f"{start}.com", "onboarding")
            assert s.tier1_transition(f"{start}.com", "suspended") is not None, \
                f"{start} -> suspended should be legal"

    def test_onboarded_can_suspend(self, isolated_db):
        s = isolated_db
        s.create_stub("Acme", domain="acme.com")
        for nxt in ("discovered", "contacted", "quoted", "onboarding", "onboarded"):
            assert s.tier1_transition("acme.com", nxt) is not None
        assert s.tier1_transition("acme.com", "suspended") is not None
        assert s.get_tier1_lifecycle("acme.com") == "suspended"

    def test_unsuspend_only_to_onboarded(self, isolated_db):
        s = isolated_db
        s.create_stub("Acme", domain="acme.com")
        s.tier1_transition("acme.com", "discovered")
        s.tier1_transition("acme.com", "suspended")
        # suspended -> onboarded is the only re-activation (no backward skip)
        assert s.tier1_transition("acme.com", "onboarded") is not None
        # suspended -> contacted is a backward skip, blocked
        s.tier1_transition("acme.com", "suspended")
        assert s.tier1_transition("acme.com", "contacted") is None

    def test_enter_at_discovered_only_from_legacy(self, isolated_db):
        s = isolated_db
        s.create_stub("Acme", domain="acme.com")
        # No tier1_lifecycle yet (NULL) -> may enter at discovered, not quoted.
        assert s.tier1_transition("acme.com", "discovered") is not None

    def test_enter_at_quoted_from_legacy_blocked(self, isolated_db):
        s = isolated_db
        s.create_stub("Acme", domain="acme.com")
        assert s.tier1_transition("acme.com", "quoted") is None  # NULL -> quoted blocked
        assert s.get_tier1_lifecycle("acme.com") is None  # unchanged

    def test_unknown_status_rejected(self, isolated_db):
        s = isolated_db
        s.create_stub("Acme", domain="acme.com")
        assert s.tier1_transition("acme.com", "vaporware") is None

    def test_missing_supplier_returns_none(self, isolated_db):
        assert isolated_db.tier1_transition("no-such.com", "discovered") is None

    def test_can_transition_pure(self):
        # Pure helper, no I/O — independent of the DB fixture.
        assert sr._tier1_can_transition("discovered", "contacted") is True
        assert sr._tier1_can_transition("contacted", "discovered") is False
        assert sr._tier1_can_transition(None, "discovered") is True
        assert sr._tier1_can_transition(None, "quoted") is False
        assert sr._tier1_can_transition("suspended", "onboarded") is True
        assert sr._tier1_can_transition("suspended", "contacted") is False


# ---------------------------------------------------------------------------
# Graduation: onboarded exits Apollo staleness refresh (I3)
# ---------------------------------------------------------------------------

class TestGraduation:
    def test_onboarded_excluded_from_refresh_under_flag(self, isolated_db):
        s = isolated_db
        old = (datetime.utcnow() - timedelta(days=5000)).isoformat()
        s.upsert_apollo_data("acme.com", {"suitability_status": "confirmed",
                                          "apollo_enriched_at": old})
        # Drive the tier1 lifecycle to onboarded.
        for nxt in ("discovered", "contacted", "quoted", "onboarding", "onboarded"):
            assert s.tier1_transition("acme.com", nxt) is not None
        rec = s.lookup_by_domain("acme.com")
        # Graduation: an onboarded supplier is exempt from refresh even with an
        # ancient apollo_enriched_at (the I3 site is needs_reenrichment).
        assert s.needs_reenrichment(rec) is False

    def test_non_onboarded_still_refreshes_when_stale(self, isolated_db):
        s = isolated_db
        old = (datetime.utcnow() - timedelta(days=5000)).isoformat()
        s.upsert_apollo_data("acme.com", {"suitability_status": "confirmed",
                                          "apollo_enriched_at": old})
        # Drive to quoted (not onboarded) — still stale.
        for nxt in ("discovered", "contacted", "quoted"):
            s.tier1_transition("acme.com", nxt)
        rec = s.lookup_by_domain("acme.com")
        assert s.needs_reenrichment(rec) is True

    def test_legacy_onboarding_status_graduation_unchanged(self, isolated_db):
        """The pre-existing graduation (onboarding_status=onboarded_arkim_supplier)
        still applies — the new tier1 branch is ADDITIVE, not a replacement."""
        s = isolated_db
        old = (datetime.utcnow() - timedelta(days=5000)).isoformat()
        s.upsert_apollo_data("acme.com", {"suitability_status": "confirmed",
                                          "apollo_enriched_at": old})
        s.update_supplier("acme.com", onboarding_status="onboarded_arkim_supplier")
        rec = s.lookup_by_domain("acme.com")
        assert s.needs_reenrichment(rec) is False  # legacy graduation still fires


# ---------------------------------------------------------------------------
# Lookup primitives (T3)
# ---------------------------------------------------------------------------

class TestLookups:
    def _seed(self, s):
        # acme: core SEAL+PUMP, carries goulds authorized
        s.set_supplier_classes("acme.com", [
            {"class_id": "SEAL", "is_core": True},
            {"class_id": "PUMP", "is_core": True},
            {"class_id": "HOSE", "is_core": False},
        ])
        s.set_supplier_brands("acme.com", [
            {"brand_id": "goulds", "relationship": "AUTHORIZED"},
        ])
        s.set_supplier_territory("acme.com", {"kind": "NATIONWIDE_US"})
        # beta: core VALVE only, nationwide
        s.set_supplier_classes("beta.com", [
            {"class_id": "VALVE", "is_core": True},
            {"class_id": "SEAL", "is_core": False},
        ])
        s.set_supplier_brands("beta.com", [
            {"brand_id": "skf", "relationship": "CARRIES"},
        ])
        s.set_supplier_territory("beta.com", {"kind": "STATES", "states": ["CA", "NV"]})
        # gamma: local-service only (no ship_area), carries goulds aftermarket
        s.set_supplier_classes("gamma.com", [{"class_id": "PUMP", "is_core": True}])
        s.set_supplier_brands("gamma.com", [
            {"brand_id": "goulds", "relationship": "AFTERMARKET_COMPATIBLE"},
        ])
        s.set_supplier_territory(
            "gamma.com",
            {"kind": "STATES", "states": ["NY"]},
            local_service=[{"branch_zip": "10001", "radius": 30}],
        )

    def test_find_by_class(self, isolated_db):
        s = isolated_db
        self._seed(s)
        seal_suppliers = {r["domain"] for r in s.find_suppliers_by_class("SEAL")}
        assert seal_suppliers == {"acme.com", "beta.com"}

    def test_find_by_class_core_only(self, isolated_db):
        s = isolated_db
        self._seed(s)
        core_seal = {r["domain"] for r in s.find_suppliers_by_class("SEAL", core_only=True)}
        assert core_seal == {"acme.com"}  # beta's SEAL is not core
        core_pump = {r["domain"] for r in s.find_suppliers_by_class("PUMP", core_only=True)}
        assert core_pump == {"acme.com", "gamma.com"}

    def test_find_by_brand_all_relationships(self, isolated_db):
        s = isolated_db
        self._seed(s)
        goulds_all = {r["domain"] for r in s.find_suppliers_by_brand("goulds")}
        assert goulds_all == {"acme.com", "gamma.com"}

    def test_find_by_brand_tri_state_filter(self, isolated_db):
        s = isolated_db
        self._seed(s)
        authorized = {r["domain"] for r in s.find_suppliers_by_brand("goulds", "AUTHORIZED")}
        assert authorized == {"acme.com"}
        aftermarket = {r["domain"] for r in s.find_suppliers_by_brand("goulds", "AFTERMARKET_COMPATIBLE")}
        assert aftermarket == {"gamma.com"}
        carries = {r["domain"] for r in s.find_suppliers_by_brand("skf", "CARRIES")}
        assert carries == {"beta.com"}
        # an AUTHORIZED filter on skf returns nothing (skf is CARRIES at beta)
        assert s.find_suppliers_by_brand("skf", "AUTHORIZED") == []

    def test_find_by_brand_invalid_relationship_returns_empty(self, isolated_db):
        s = isolated_db
        self._seed(s)
        assert s.find_suppliers_by_brand("goulds", "BOGUS") == []

    def test_find_by_territory_returns_rank_not_exclusion(self, isolated_db):
        s = isolated_db
        self._seed(s)
        # Querying NY: acme (nationwide) rank 3, gamma (states NY) rank 2,
        # beta (states CA/NV) rank 1 (state set but no NY match) — beta is STILL
        # returned (rank, not hard exclusion).
        ranked = {r["domain"]: r["territory_rank"] for r in s.find_suppliers_by_territory("NY")}
        assert ranked == {"acme.com": sr.TERRITORY_RANK_NATIONWIDE,
                          "beta.com": sr.TERRITORY_RANK_STATE,
                          "gamma.com": sr.TERRITORY_RANK_STATE_MATCH}
        # beta is present even though it doesn't cover NY — rank data, not exclusion
        assert "beta.com" in ranked

    def test_find_by_territory_nationwide_beats_state(self, isolated_db):
        s = isolated_db
        self._seed(s)
        # Querying any state: nationwide acme rank 3 > state-match gamma rank 2
        ranked = {r["domain"]: r["territory_rank"] for r in s.find_suppliers_by_territory("CA")}
        assert ranked["acme.com"] == sr.TERRITORY_RANK_NATIONWIDE
        assert ranked["beta.com"] == sr.TERRITORY_RANK_STATE_MATCH  # CA in beta's states

    def test_find_with_local_service_is_hard_inclusion(self, isolated_db):
        s = isolated_db
        self._seed(s)
        local = {r["domain"] for r in s.find_suppliers_with_local_service()}
        # Only gamma has a local-service branch — hard inclusion, others excluded.
        assert local == {"gamma.com"}
        # The branch data is returned so the caller can apply the radius test
        gamma_row = next(r for r in s.find_suppliers_with_local_service() if r["domain"] == "gamma.com")
        assert gamma_row["branch_zip"] == "10001"
        assert gamma_row["radius_miles"] == 30


# ---------------------------------------------------------------------------
# Full scope read
# ---------------------------------------------------------------------------

class TestGetSupplierScope:
    def test_full_scope_roundtrip(self, isolated_db):
        s = isolated_db
        s.set_supplier_classes("acme.com", [{"class_id": "SEAL", "is_core": True}])
        s.set_supplier_brands("acme.com", [{"brand_id": "goulds", "relationship": "AUTHORIZED"}])
        s.set_supplier_territory("acme.com", {"kind": "NATIONWIDE_US"})
        s.set_supplier_verticals("acme.com", ["pharma"])
        scope = s.get_supplier_scope("acme.com")
        assert scope["classes"][0]["class_id"] == "SEAL"
        assert scope["brands"][0]["brand_id"] == "goulds"
        assert scope["ship_area"] == {"kind": "NATIONWIDE_US"}
        assert scope["verticals"] == ["pharma"]
        assert scope["performance"] == {}
        assert scope["tier1_lifecycle"] is None  # not transitioned yet

    def test_scope_on_legacy_row_is_empty(self, isolated_db):
        s = isolated_db
        s.upsert_apollo_data("legacy.com", {"suitability_status": "confirmed"})
        scope = s.get_supplier_scope("legacy.com")
        assert scope == {"tier1_lifecycle": None, "classes": [], "brands": [],
                         "ship_area": None, "local_service": [], "verticals": [],
                         "performance": {}, "scope_source": None,
                         "scope_set_by": None, "scope_set_at": None}


# ---------------------------------------------------------------------------
# Inertness — flag-off = extensions dormant (T5)
# ---------------------------------------------------------------------------

class TestInertnessFlagOff:
    def test_writes_noop_flag_off(self, isolated_db_off):
        s = isolated_db_off
        assert s.set_supplier_classes("acme.com", [{"class_id": "SEAL"}]) is False
        assert s.set_supplier_brands("acme.com", [{"brand_id": "x", "relationship": "AUTHORIZED"}]) is False
        assert s.set_supplier_territory("acme.com", {"kind": "NATIONWIDE_US"}) is False
        assert s.set_supplier_verticals("acme.com", ["pharma"]) is False
        # Nothing was written — the stub wasn't even created.
        assert s.lookup_by_domain("acme.com") is None

    def test_reads_empty_flag_off(self, isolated_db_off):
        s = isolated_db_off
        # Seed a supplier via the un-gated Apollo path, then assert scope reads
        # are empty even with a row present (the scope extensions are dormant).
        s.upsert_apollo_data("acme.com", {"suitability_status": "confirmed"})
        assert s.get_supplier_classes("acme.com") == []
        assert s.get_supplier_brands("acme.com") == []
        assert s.get_supplier_territory("acme.com") == {"ship_area": None, "local_service": []}
        assert s.get_supplier_verticals("acme.com") == []
        assert s.get_tier1_lifecycle("acme.com") is None
        scope = s.get_supplier_scope("acme.com")
        assert scope["classes"] == [] and scope["brands"] == []
        assert scope["ship_area"] is None

    def test_transition_noop_flag_off(self, isolated_db_off):
        s = isolated_db_off
        s.create_stub("Acme", domain="acme.com")
        assert s.tier1_transition("acme.com", "discovered") is None
        # tier1_lifecycle stays NULL — the state machine did not run.
        assert s.get_tier1_lifecycle("acme.com") is None

    def test_lookups_empty_flag_off(self, isolated_db_off):
        s = isolated_db_off
        # Even if a row exists (Apollo path), scope lookups return empty flag-off.
        s.upsert_apollo_data("acme.com", {"suitability_status": "confirmed"})
        assert s.find_suppliers_by_class("SEAL") == []
        assert s.find_suppliers_by_brand("goulds") == []
        assert s.find_suppliers_by_territory("NY") == []
        assert s.find_suppliers_with_local_service() == []

    def test_needs_reenrichment_unchanged_flag_off(self, isolated_db_off):
        """The I3 graduation branch is dormant flag-off: a supplier with
        tier1_lifecycle='onboarded' (set somehow) is NOT exempted by the tier1
        branch — only the legacy onboarding_status graduation applies. Proves
        the clarifier's needs_reenrichment is byte-identical to pre-Night-3."""
        s = isolated_db_off
        old = (datetime.utcnow() - timedelta(days=5000)).isoformat()
        s.upsert_apollo_data("acme.com", {"suitability_status": "confirmed",
                                          "apollo_enriched_at": old})
        rec = s.lookup_by_domain("acme.com")
        # No onboarding_status graduation, TIER1_V2 off -> stale, must re-enrich.
        assert s.needs_reenrichment(rec) is True
        # The tier1 branch would have made this False if it fired flag-on; flag-off
        # it does NOT fire, so the verdict is the pre-Night-3 True. Inject a
        # tier1_lifecycle value directly to prove the branch is dormant flag-off:
        with s._get_conn() as conn:
            conn.execute("UPDATE suppliers SET tier1_lifecycle = 'onboarded' WHERE domain = 'acme.com'")
            conn.commit()
        rec2 = s.lookup_by_domain("acme.com")
        # Still True: the tier1 graduation branch is gated off, so an onboarded
        # tier1_lifecycle does NOT exempt the supplier flag-off. (Only the legacy
        # onboarding_status branch would, and that's not set here.)
        assert s.needs_reenrichment(rec2) is True
