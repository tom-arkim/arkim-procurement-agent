"""
Night 5 — Tier 1 runtime MATCHER tests (T1: the matcher truth table + Goulds anchor).

The matcher (``utils.procurement_agent.tier1_matcher.match_tier1``) is exercised
DIRECTLY here; the live-faithful path (through the real ``_run_tier1`` via the API
TestClient) is covered in ``test_tier1_runtime_live.py``.

Fixture registry: an isolated supplier_registry sqlite + TIER1_V2 ON (mirrors
test_supplier_scope.py). The Goulds anchor builds (a) an authorized Goulds seal
distributor, (b) an aftermarket fits-Goulds seal shop, (c) an onboarded PUMP-only
supplier, and asserts a & b match (a first, b carries disclosure), c is EXCLUDED by
the class hard-gate.

Honesty: a property test asserts NO match ever carries a price — the matcher emits
identity + relationship + rank only (price is T2's job, and only from a dated
confirmed price_db entry). The matcher never touches known_parts/price_db writes.

Flag gating: an inertness test asserts flag-off → [] (byte-identical empty), and a
falsy-token test asserts the flag fails safe/closed.
"""
from __future__ import annotations

import pytest

from utils import supplier_registry as sr
from utils.procurement_agent import tier1_matcher as tm


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def reg(tmp_path, monkeypatch):
    """Isolated supplier_registry + TIER1_V2 ON (mirrors test_supplier_scope)."""
    monkeypatch.setattr(sr, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sr, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    monkeypatch.setattr(sr, "TIER1_V2", True)
    return sr


def _onboard(reg, domain, name, *, classes, brands=None, ship_area=None,
             local_service=None, lifecycle="onboarded"):
    """Create a supplier row, set its scope, and drive its lifecycle to `lifecycle`
    (default onboarded). `classes` is a list of {class_id, is_core, confidence?}.
    `brands` is a list of {brand_id, relationship}. `ship_area` is a dict
    ({"kind":"NATIONWIDE_US"} | {"kind":"STATES","states":[...]}). `local_service`
    is a list of {branch_zip, radius_miles?}."""
    reg._ensure_supplier_row(domain, name=name)
    if classes:
        reg.set_supplier_classes(domain, [
            {"class_id": c["class_id"], "is_core": c.get("is_core", False),
             "confidence": c.get("confidence", 0.8), "source": "manual"}
            for c in classes
        ])
    if brands:
        reg.set_supplier_brands(domain, [
            {"brand_id": b["brand_id"], "relationship": b["relationship"],
             "confidence": 0.9, "source": "manual"}
            for b in brands
        ])
    if ship_area:
        reg.set_supplier_territory(domain, ship_area, local_service or None)
    # Drive the lifecycle: a fresh stub has NULL lifecycle → enter at discovered.
    reg.tier1_transition(domain, "discovered")
    reg.tier1_transition(domain, "contacted")
    reg.tier1_transition(domain, "quoted")
    reg.tier1_transition(domain, "onboarding")
    reg.tier1_transition(domain, lifecycle)
    return reg.lookup_by_domain(domain)


# ---------------------------------------------------------------------------
# Flag gating + inertness (T1 / T5)
# ---------------------------------------------------------------------------

class TestFlagGating:
    def test_flag_off_returns_empty(self, reg, monkeypatch):
        """TIER1_V2 OFF → match_tier1 returns [] always (byte-identical empty)."""
        monkeypatch.setattr(sr, "TIER1_V2", False)
        _onboard(reg, "sealco.com", "SealCo",
                 classes=[{"class_id": "SEAL", "is_core": True}])
        # Even with a perfectly-matching onboarded supplier, flag-off → [].
        assert tm.match_tier1(detected_type="mechanical seal",
                              manufacturer="Goulds") == []

    @pytest.mark.parametrize("token", ["", "0", "false", "no", "off", "junk", None])
    def test_falsy_token_is_flag_off(self, monkeypatch, token):
        """Every non-truthy token parses to OFF (fail safe/closed)."""
        monkeypatch.setattr(sr, "TIER1_V2", sr._env_truthy(token))
        assert tm.tier1_v2_active() is (token in ("1", "true", "yes", "on"))


# ---------------------------------------------------------------------------
# Class hard-gate (the load-bearing correctness property)
# ---------------------------------------------------------------------------

class TestClassHardGate:
    def test_wrong_class_excluded_even_when_brand_matches(self, reg):
        """A PUMP-only supplier carrying the Goulds brand as AUTHORIZED must NOT match
        a SEAL request — the class hard-gate excludes ALWAYS, even brand-matched."""
        _onboard(reg, "pumponly.com", "PumpOnly",
                 classes=[{"class_id": "PUMP", "is_core": True}],
                 brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")
        assert matches == [], "PUMP-only supplier matched a SEAL request — class gate failed"

    def test_class_match_admits_incidental_non_core(self, reg):
        """A supplier carrying SEAL as INCIDENTAL (is_core=0) still matches a SEAL
        request — class is the gate, core-ness is a ranker. (Core just ranks higher.)"""
        _onboard(reg, "broadline.com", "Broadline",
                 classes=[{"class_id": "SEAL", "is_core": False}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")
        assert len(matches) == 1
        assert matches[0].is_core is False
        assert matches[0].noun_class == "SEAL"

    def test_undetectable_request_class_returns_empty(self, reg):
        """No detected_type / description / model → no class to gate on → no matches."""
        _onboard(reg, "sealco.com", "SealCo",
                 classes=[{"class_id": "SEAL", "is_core": True}])
        assert tm.match_tier1(detected_type=None, manufacturer="Goulds") == []
        assert tm.match_tier1(detected_type="", manufacturer="Goulds") == []

    def test_non_onboarded_supplier_excluded(self, reg):
        """A discovered/quoted-but-not-onboarded supplier does NOT surface as Tier 1 —
        it belongs to Tier 2/3 outreach. Only lifecycle==onboarded matches."""
        # Onboarded SEAL supplier.
        _onboard(reg, "onboarded-seal.com", "OnboardedSeal",
                 classes=[{"class_id": "SEAL", "is_core": True}])
        # Discovered-only SEAL supplier (lifecycle stays NULL — never driven forward).
        reg._ensure_supplier_row("discovered-seal.com", name="DiscoveredSeal")
        reg.set_supplier_classes("discovered-seal.com",
                                 [{"class_id": "SEAL", "is_core": True}])
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")
        assert len(matches) == 1
        assert matches[0].domain == "onboarded-seal.com"


# ---------------------------------------------------------------------------
# Brand tri-state ordering (the amplifier)
# ---------------------------------------------------------------------------

class TestBrandAmplifier:
    def test_authorized_ranks_above_carries_above_aftermarket(self, reg):
        """Three SEAL suppliers, identical class+territory, differing only in brand
        relationship for the requested manufacturer. AUTHORIZED > CARRIES >
        AFTERMARKET_COMPATIBLE in the ranking."""
        _onboard(reg, "auth.com", "AuthDistributor",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        _onboard(reg, "carries.com", "CarriesDistributor",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds", "relationship": "CARRIES"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        _onboard(reg, "aftermarket.com", "AftermarketShop",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds",
                          "relationship": "AFTERMARKET_COMPATIBLE"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")
        assert [m.domain for m in matches] == ["auth.com", "carries.com", "aftermarket.com"]
        assert matches[0].brand_relationship == "AUTHORIZED"
        assert matches[1].brand_relationship == "CARRIES"
        assert matches[2].brand_relationship == "AFTERMARKET_COMPATIBLE"
        assert matches[2].is_aftermarket is True
        assert matches[0].is_aftermarket is False

    def test_brand_neutral_still_matches(self, reg):
        """A class-matched supplier with NO brand row for the requested manufacturer
        still matches (brand is an amplifier, not a gate) — brand-neutral."""
        _onboard(reg, "neutral.com", "NeutralDistributor",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")
        assert len(matches) == 1
        assert matches[0].brand_relationship is None

    def test_brand_neutral_ranks_below_aftermarket(self, reg):
        """A brand-neutral supplier ranks below an AFTERMARKET_COMPATIBLE one (both
        class-matched) — the amplifier ordering extends to neutral (lowest)."""
        _onboard(reg, "aftermarket.com", "AftermarketShop",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds",
                          "relationship": "AFTERMARKET_COMPATIBLE"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        _onboard(reg, "neutral.com", "NeutralDistributor",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")
        assert [m.domain for m in matches] == ["aftermarket.com", "neutral.com"]

    def test_unknown_manufacturer_is_brand_neutral(self, reg):
        """A request with manufacturer='Unknown' → no brand amplifier (neutral), still
        matches on class."""
        _onboard(reg, "sealco.com", "SealCo",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Unknown")
        assert len(matches) == 1
        assert matches[0].brand_relationship is None


# ---------------------------------------------------------------------------
# Territory ranks, never filters (except local_service)
# ---------------------------------------------------------------------------

class TestTerritoryRanksNotFilters:
    def test_nationwide_ranks_above_state_match(self, reg):
        """NATIONWIDE > state-match > state-no-match in the ranking; ALL are returned
        (territory never filters). buyer_state='CA'."""
        _onboard(reg, "nationwide.com", "NationwideCo",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 ship_area={"kind": "NATIONWIDE_US"})
        _onboard(reg, "ca-seal.com", "CaSealCo",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 ship_area={"kind": "STATES", "states": ["CA"]})
        _onboard(reg, "ny-seal.com", "NySealCo",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 ship_area={"kind": "STATES", "states": ["NY"]})
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds", buyer_state="CA")
        domains = [m.domain for m in matches]
        assert set(domains) == {"nationwide.com", "ca-seal.com", "ny-seal.com"}  # none filtered
        # nationwide (rank 3) > ca-seal (rank 2, state match) > ny-seal (rank 1, no match)
        assert domains[0] == "nationwide.com"
        assert domains[1] == "ca-seal.com"
        assert domains[2] == "ny-seal.com"

    def test_no_buyer_location_degrades_to_neutral(self, reg):
        """No buyer_state → territory degrades gracefully: NATIONWIDE still ranks at
        the top of the neutral band; a STATES supplier is NOT excluded (it ranks at
        STATE, neutral). No supplier is dropped for territory."""
        _onboard(reg, "nationwide.com", "NationwideCo",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 ship_area={"kind": "NATIONWIDE_US"})
        _onboard(reg, "ny-seal.com", "NySealCo",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 ship_area={"kind": "STATES", "states": ["NY"]})
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")  # no buyer_state
        domains = [m.domain for m in matches]
        assert set(domains) == {"nationwide.com", "ny-seal.com"}  # none filtered
        assert domains[0] == "nationwide.com"  # nationwide ranks above states-no-match


# ---------------------------------------------------------------------------
# Local-service hard filter (the ONLY geographic exclusion)
# ---------------------------------------------------------------------------

class TestLocalServiceHardFilter:
    def test_local_service_included_without_buyer_zip(self, reg):
        """A local_service supplier is INCLUDED when the request carries no buyer zip
        (degrade-graceful — I2). Excluding it would silently drop onboarded local
        suppliers when the request has no location (the common case today)."""
        _onboard(reg, "local-seal.com", "LocalSealCo",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 ship_area={"kind": "STATES", "states": ["CA"]},
                 local_service=[{"branch_zip": "90001", "radius": 50}])
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")  # no buyer_zip
        assert len(matches) == 1
        assert matches[0].local_service is True

    def test_local_service_included_with_buyer_zip_in_range(self, reg):
        """buyer_zip present + a branch with a positive radius → included (in-range)."""
        _onboard(reg, "local-seal.com", "LocalSealCo",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 ship_area={"kind": "STATES", "states": ["CA"]},
                 local_service=[{"branch_zip": "90001", "radius": 50}])
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds", buyer_zip="90001")
        assert len(matches) == 1

    def test_local_service_excluded_with_buyer_zip_out_of_range(self, reg):
        """buyer_zip present + a branch with a ZERO radius → excluded (out of range).
        A zero/empty radius means the branch serves no area around the buyer zip."""
        _onboard(reg, "local-seal.com", "LocalSealCo",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 ship_area={"kind": "STATES", "states": ["CA"]},
                 local_service=[{"branch_zip": "90001", "radius": 0}])
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds", buyer_zip="10001")
        assert matches == [], "out-of-range local_service supplier was not excluded"


# ---------------------------------------------------------------------------
# THE GOULDS ANCHOR (the load-bearing test)
# ---------------------------------------------------------------------------

class TestGouldsAnchor:
    """The Goulds anchor: request class SEAL + manufacturer Goulds vs a fixture
    registry with (a) an authorized Goulds seal distributor, (b) an aftermarket
    fits-Goulds seal shop, (c) an onboarded PUMP-only supplier. a & b match (a ranks
    first, b carries the disclosure flag), c is EXCLUDED by the class hard-gate."""

    @pytest.fixture
    def goulds_registry(self, reg):
        _onboard(reg, "goulds-auth.com", "Goulds Authorized Seal Co",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        _onboard(reg, "aftermarket-seals.com", "Aftermarket Seals Shop",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds",
                          "relationship": "AFTERMARKET_COMPATIBLE"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        _onboard(reg, "pump-only.com", "Pump Only Supplier",
                 classes=[{"class_id": "PUMP", "is_core": True}],
                 brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        return reg

    def test_a_and_b_match_c_excluded(self, goulds_registry):
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")
        domains = [m.domain for m in matches]
        # a & b match (both SEAL), c excluded (PUMP-only — class hard-gate).
        assert "goulds-auth.com" in domains
        assert "aftermarket-seals.com" in domains
        assert "pump-only.com" not in domains
        assert len(matches) == 2

    def test_a_ranks_first(self, goulds_registry):
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")
        assert matches[0].domain == "goulds-auth.com"
        assert matches[0].brand_relationship == "AUTHORIZED"

    def test_b_carries_aftermarket_disclosure(self, goulds_registry):
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")
        aftermarket = next(m for m in matches if m.domain == "aftermarket-seals.com")
        assert aftermarket.brand_relationship == "AFTERMARKET_COMPATIBLE"
        assert aftermarket.is_aftermarket is True
        # The disclosure flag is carried on the match (T4 surfaces it on the candidate).
        assert aftermarket.match_explanation["brand_relationship"] == "AFTERMARKET_COMPATIBLE"

    def test_match_explanation_carries_class_gate(self, goulds_registry):
        """Every match carries the class-gate that admitted it (human-reviewable)."""
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")
        for m in matches:
            assert m.match_explanation["class_gate"] == "SEAL"
            assert m.match_explanation["onboarded"] is True


# ---------------------------------------------------------------------------
# Honesty: the matcher NEVER carries a price (gate-2 trust guarantee)
# ---------------------------------------------------------------------------

class TestHonestyNoPrice:
    """The matcher emits identity + relationship + rank only — NEVER a price. A
    Tier1Match has no price field at all (the dataclass is price-free by construction).
    This is the structural half of the gate-2 trust guarantee; T2 enforces it on the
    candidate dict too, and the live-faithful test re-asserts it through the API."""

    @pytest.fixture
    def seeded(self, reg):
        _onboard(reg, "auth.com", "AuthDistributor",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        return reg

    def test_match_has_no_price_field(self, seeded):
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")
        assert len(matches) == 1
        m = matches[0]
        # The dataclass carries NO price attribute (price is T2's job).
        assert not hasattr(m, "price")
        assert not hasattr(m, "base_price")

    def test_no_price_anywhere_in_match_explanation(self, seeded):
        """The match-explanation metadata carries no price either."""
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")
        for m in matches:
            for key, val in m.match_explanation.items():
                assert "price" not in key.lower(), \
                    f"match_explanation carries a price key: {key}"


# ---------------------------------------------------------------------------
# Determinism (stable ordering for the live-faithful API test)
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_equal_score_suppliers_ordered_by_name(self, reg):
        """Two identical-supplier SEAL matches (same brand rel, core, territory) order
        by vendor name — deterministic across runs so the API test is stable."""
        _onboard(reg, "zeta.com", "Zeta Seal",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 ship_area={"kind": "NATIONWIDE_US"})
        _onboard(reg, "alpha.com", "Alpha Seal",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")
        assert [m.domain for m in matches] == ["alpha.com", "zeta.com"]

    def test_repeated_calls_identical(self, reg):
        """Two calls with the same inputs return identical results (fresh-per-run, no
        cache → no drift)."""
        _onboard(reg, "auth.com", "AuthDistributor",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        a = tm.match_tier1(detected_type="mechanical seal", manufacturer="Goulds")
        b = tm.match_tier1(detected_type="mechanical seal", manufacturer="Goulds")
        assert [(m.domain, m.score) for m in a] == [(m.domain, m.score) for m in b]
