"""
Night 5 — Tier 1 runtime LIVE-FAITHFUL tests (T5 inertness + the live-faithful path).

Two load-bearing things this file proves:

1. INERTNESS (T5) — flag-off byte-identical. With TIER1_V2 OFF, a sourcing run
   through the real API produces an honest-empty Tier 1 (tier1 == []) byte-identical
   to pre-Night-5, the candidate shape carries NO registry-backed fields, and the
   notify surface records no events. The SCORING_V2 + purge-guard suites are
   untouched (asserted green separately, not here).

2. LIVE-FAITHFUL (guardrail 7) — the matcher is tested through the REAL
   `_run_tier1` path via the API (TestClient), not only by calling the matcher
   directly. With TIER1_V2 ON + a fixture onboarded supplier in the registry, a
   matching request renders an honest Tier 1 card through the real
   SourcingAgent → _transform_sourcing_results → candidate payload; a wrong-class
   request renders NO Tier 1 card; the card carries NO fabricated price; the
   aftermarket disclosure reaches the payload; notify events are recorded behind
   the stubbed/flagged EmailSender (zero live sends).

The sourcing pipeline runs for real (the SourcingAgent is NOT mocked) — only the
external providers (Tavily/Anthropic/Apollo) are neutralized by the conftest
autouse fixture + the `api` fixture's empty key env. Tier 2/3 return [] (no keys),
so Tier 1 is the only populated lane, isolating the matcher path.
"""
from __future__ import annotations

import json

import pytest

from utils import supplier_registry as sr


# ---------------------------------------------------------------------------
# Fixtures — the `api` fixture (from test_api_server) + an isolated registry
# with TIER1_V2 ON and an onboarded supplier matching a Goulds SEAL request.
# ---------------------------------------------------------------------------

@pytest.fixture
def api_with_tier1(tmp_path, monkeypatch):
    """TestClient over api_server + isolated supplier_registry with TIER1_V2 ON and
    an onboarded Goulds-authorized SEAL distributor + an aftermarket seal shop + a
    PUMP-only supplier (the Goulds anchor registry, live)."""
    from utils.procurement_agent.state import persistence

    engine = persistence._make_engine(f"sqlite:///{tmp_path / 'api.sqlite'}")
    from sqlalchemy.orm import sessionmaker
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    persistence.Base.metadata.create_all(engine)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr(persistence, "_engine", engine)
    monkeypatch.setattr(persistence, "_SessionFactory", TestSession)

    # Isolate the supplier registry + turn TIER1_V2 ON (the live-faithful path).
    monkeypatch.setattr(sr, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sr, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    monkeypatch.setattr(sr, "TIER1_V2", True)

    import api_server
    # test_demo_mode reloads api_server with DEMO_MODE=True and leaves it cached in
    # sys.modules; the allowlist middleware + create_run read the module global at
    # request time, so reset it to False here or POST /api/runs returns the demo
    # 422 (X-Session-Id required) instead of 201 (the onboarding_api fixture does
    # the same reset). DEMO_MODE is inert for the matcher path either way.
    monkeypatch.setattr(api_server, "DEMO_MODE", False)
    monkeypatch.setattr(api_server, "_engine", engine)
    monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
    monkeypatch.setattr(api_server, "_messages", {})

    from fastapi.testclient import TestClient
    client = TestClient(api_server.app)
    client._api_server = api_server

    # Onboard the Goulds-anchor fixture suppliers into the isolated registry.
    _onboard(sr, "goulds-auth.com", "Goulds Authorized Seal Co",
             classes=[{"class_id": "SEAL", "is_core": True}],
             brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
             ship_area={"kind": "NATIONWIDE_US"})
    _onboard(sr, "aftermarket-seals.com", "Aftermarket Seals Shop",
             classes=[{"class_id": "SEAL", "is_core": True}],
             brands=[{"brand_id": "Goulds",
                      "relationship": "AFTERMARKET_COMPATIBLE"}],
             ship_area={"kind": "NATIONWIDE_US"})
    _onboard(sr, "pump-only.com", "Pump Only Supplier",
             classes=[{"class_id": "PUMP", "is_core": True}],
             brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
             ship_area={"kind": "NATIONWIDE_US"})
    return client


@pytest.fixture
def api_tier1_off(tmp_path, monkeypatch):
    """Same isolation but TIER1_V2 OFF — for the inertness wall (T5)."""
    from utils.procurement_agent.state import persistence

    engine = persistence._make_engine(f"sqlite:///{tmp_path / 'api_off.sqlite'}")
    from sqlalchemy.orm import sessionmaker
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    persistence.Base.metadata.create_all(engine)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr(persistence, "_engine", engine)
    monkeypatch.setattr(persistence, "_SessionFactory", TestSession)

    monkeypatch.setattr(sr, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sr, "_DB_PATH", str(tmp_path / "supplier_registry_off.sqlite"))
    monkeypatch.setattr(sr, "TIER1_V2", False)  # flag OFF

    import api_server
    monkeypatch.setattr(api_server, "DEMO_MODE", False)  # same demo-reload reset as above
    monkeypatch.setattr(api_server, "_engine", engine)
    monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
    monkeypatch.setattr(api_server, "_messages", {})

    from fastapi.testclient import TestClient
    client = TestClient(api_server.app)
    client._api_server = api_server
    # Still onboard a SEAL supplier — flag-off must ignore it (honest-empty).
    _onboard(sr, "sealco.com", "SealCo",
             classes=[{"class_id": "SEAL", "is_core": True}],
             ship_area={"kind": "NATIONWIDE_US"})
    return client


def _onboard(reg, domain, name, *, classes, brands=None, ship_area=None):
    """Onboard a supplier with the given scope (mirrors test_tier1_matcher)."""
    reg._ensure_supplier_row(domain, name=name)
    reg.set_supplier_classes(domain, [
        {"class_id": c["class_id"], "is_core": c.get("is_core", False),
         "confidence": 0.8, "source": "manual"}
        for c in classes
    ])
    if brands:
        reg.set_supplier_brands(domain, [
            {"brand_id": b["brand_id"], "relationship": b["relationship"],
             "confidence": 0.9, "source": "manual"}
            for b in brands
        ])
    if ship_area:
        reg.set_supplier_territory(domain, ship_area)
    reg.tier1_transition(domain, "discovered")
    reg.tier1_transition(domain, "contacted")
    reg.tier1_transition(domain, "quoted")
    reg.tier1_transition(domain, "onboarding")
    reg.tier1_transition(domain, "onboarded")


def _run_sourcing(client, *, detected_type, manufacturer, part_number="UNKNOWN-PN"):
    """Create a run, seed SEAL specs, confirm-intake (fires the real background
    sourcing), and return the run-detail JSON."""
    rid = client.post("/api/runs", json={}).json()["id"]
    specs = {"manufacturer": manufacturer, "part_number": part_number,
             "detected_type": detected_type, "voltage": "N/A"}
    client.put(f"/api/runs/{rid}/asset-specs", json={"asset_specs": specs})
    client.post(f"/api/runs/{rid}/confirm-intake")
    return client.get(f"/api/runs/{rid}").json()


# ---------------------------------------------------------------------------
# INERTNESS (T5) — flag-off byte-identical honest-empty
# ---------------------------------------------------------------------------

class TestInertnessFlagOff:
    def test_flag_off_tier1_honest_empty(self, api_tier1_off):
        """TIER1_V2 OFF → a SEAL request renders NO Tier 1 cards, even with an
        onboarded SEAL supplier in the registry. Tier 1 is byte-identical empty."""
        detail = _run_sourcing(api_tier1_off, detected_type="mechanical seal",
                               manufacturer="Goulds")
        assert detail["sourcing_results"]["tier1"] == []
        # No registry-backed fields on any candidate (Tier 2/3 are empty too — no keys).
        for c in detail["sourcing_results"]["tier1"]:
            assert c.get("registryBacked") is not True

    def test_flag_off_no_notification_events(self, api_tier1_off):
        """Flag-off → the notify surface records NO events (dormant)."""
        _run_sourcing(api_tier1_off, detected_type="mechanical seal",
                      manufacturer="Goulds")
        # No run_id-specific notification events (record_supplier_notification no-ops).
        events = sr.get_supplier_notifications()
        assert events == []

    def test_flag_off_candidate_payload_no_registry_fields(self, api_tier1_off):
        """A flag-off candidate (Tier 2/3, if any) does not carry registry-backed
        fields — the aftermarketDisclosure / registryBacked / tier1MatchExplanation
        keys are absent (or falsy) on non-registry candidates."""
        detail = _run_sourcing(api_tier1_off, detected_type="mechanical seal",
                               manufacturer="Goulds")
        for tier in ("tier1", "tier2", "tier3"):
            for c in detail["sourcing_results"].get(tier, []):
                assert c.get("registryBacked") in (False, None)


# ---------------------------------------------------------------------------
# LIVE-FAITHFUL (guardrail 7) — the real _run_tier1 path via the API
# ---------------------------------------------------------------------------

class TestLiveFaithfulMatcher:
    def test_matching_request_renders_tier1_card(self, api_with_tier1):
        """A Goulds SEAL request renders honest Tier 1 cards through the real
        SourcingAgent → transform path. The authorized distributor + the aftermarket
        shop match (both SEAL); the authorized one ranks first."""
        detail = _run_sourcing(api_with_tier1, detected_type="mechanical seal",
                               manufacturer="Goulds")
        tier1 = detail["sourcing_results"]["tier1"]
        names = [c["vendorName"] for c in tier1]
        assert "Goulds Authorized Seal Co" in names
        assert "Aftermarket Seals Shop" in names
        assert "Pump Only Supplier" not in names  # class hard-gate excludes PUMP-only
        # Authorized ranks first.
        assert tier1[0]["vendorName"] == "Goulds Authorized Seal Co"
        assert tier1[0]["isAuthorizedDistributor"] is True

    def test_wrong_class_renders_no_seal_card_for_pump_request(self, api_with_tier1):
        """A PUMP request renders the PUMP-only supplier (class match) but NOT the
        SEAL suppliers (class hard-gate) — the load-bearing correctness property,
        live through the API."""
        detail = _run_sourcing(api_with_tier1, detected_type="centrifugal pump",
                               manufacturer="Goulds")
        tier1 = detail["sourcing_results"]["tier1"]
        names = [c["vendorName"] for c in tier1]
        assert "Pump Only Supplier" in names
        assert "Goulds Authorized Seal Co" not in names
        assert "Aftermarket Seals Shop" not in names

    def test_undetectable_class_renders_no_tier1(self, api_with_tier1):
        """A request with no detectable class → no Tier 1 cards (no class to gate on)."""
        detail = _run_sourcing(api_with_tier1, detected_type=None,
                               manufacturer="Goulds")
        assert detail["sourcing_results"]["tier1"] == []

    def test_no_fabricated_price_quote_required(self, api_with_tier1):
        """A registry-backed Tier 1 card with NO confirmed price_db quote renders
        quote-expected framing: price is None (price_tbd), evidenceState is
        'uncontacted' — NO fabricated price. (gate-2 trust, live through the API.)"""
        detail = _run_sourcing(api_with_tier1, detected_type="mechanical seal",
                               manufacturer="Goulds")
        tier1 = detail["sourcing_results"]["tier1"]
        for c in tier1:
            # No confirmed price_db entry exists in the isolated registry → no price.
            assert c["price"] is None
            assert c["evidenceState"] == "uncontacted"

    def test_aftermarket_disclosure_reaches_payload(self, api_with_tier1):
        """The AFTERMARKET_COMPATIBLE card carries the disclosure text on the payload
        (T4 — the DATA is the contract; frontend rendering is morning work)."""
        detail = _run_sourcing(api_with_tier1, detected_type="mechanical seal",
                               manufacturer="Goulds")
        tier1 = detail["sourcing_results"]["tier1"]
        aftermarket = next(c for c in tier1 if c["vendorName"] == "Aftermarket Seals Shop")
        assert aftermarket["isAftermarket"] is True
        assert aftermarket["aftermarketDisclosure"] is not None
        assert "Aftermarket" in aftermarket["aftermarketDisclosure"]
        assert aftermarket["registryBacked"] is True
        # The authorized card does NOT carry an aftermarket disclosure.
        authorized = next(c for c in tier1 if c["vendorName"] == "Goulds Authorized Seal Co")
        assert authorized["aftermarketDisclosure"] is None
        assert authorized["registryBacked"] is True

    def test_match_explanation_reaches_payload(self, api_with_tier1):
        """Each registry-backed Tier 1 card carries the match-explanation metadata
        (class_gate, brand_relationship, onboarded) for audit / card debugging."""
        detail = _run_sourcing(api_with_tier1, detected_type="mechanical seal",
                               manufacturer="Goulds")
        tier1 = detail["sourcing_results"]["tier1"]
        for c in tier1:
            expl = c["tier1MatchExplanation"]
            assert expl["class_gate"] == "SEAL"
            assert expl["onboarded"] is True
            assert "brand_relationship" in expl

    def test_notify_events_recorded_zero_live_sends(self, api_with_tier1):
        """The notify layer fires post-sourcing: notification events are recorded
        for the matching suppliers, all with send_status='stubbed' (EMAIL_SEND_ENABLED
        is OFF — the conftest safety net — so ZERO live sends). The notify gate
        (brand-match-or-core-class) admits both SEAL suppliers (authorized = brand+
        core; aftermarket = brand+core)."""
        detail = _run_sourcing(api_with_tier1, detected_type="mechanical seal",
                               manufacturer="Goulds")
        # The run went to comparison (sourcing completed + notify fired).
        assert detail["phase"] == "comparison"
        events = sr.get_supplier_notifications()
        assert len(events) >= 2  # both SEAL suppliers notified
        # Every event is stubbed — never sent (the double-gate).
        assert all(e["send_status"] == "stubbed" for e in events)
        assert all(e["send_status"] != "sent" for e in events)
        notified_domains = {e["supplier_domain"] for e in events}
        assert "goulds-auth.com" in notified_domains
        assert "aftermarket-seals.com" in notified_domains
        # The PUMP-only supplier was NOT matched → NOT notified.
        assert "pump-only.com" not in notified_domains

    def test_confirmed_quote_surfaces_price_on_card(self, api_with_tier1, tmp_path, monkeypatch):
        """When a dated confirmed price_db entry (source='rfq') exists for
        (manufacturer, part_number, vendor), the Tier 1 card carries the price
        (price_tbd=False) — the ONLY honest price source. Without it, quote-expected."""
        from utils import price_db
        # Isolate price_db to a temp file + write a confirmed RFQ quote for the
        # authorized distributor on a real PN.
        monkeypatch.setattr(price_db, "_DB_PATH", str(tmp_path / "price_db.json"))
        price_db.save_price("Goulds", "ST-1.375-T1", "Goulds Authorized Seal Co",
                            142.50, source="rfq")
        detail = _run_sourcing(api_with_tier1, detected_type="mechanical seal",
                               manufacturer="Goulds", part_number="ST-1.375-T1")
        tier1 = detail["sourcing_results"]["tier1"]
        auth = next(c for c in tier1 if c["vendorName"] == "Goulds Authorized Seal Co")
        assert auth["price"] == 142.50
        # The matcher's to_candidate surfaces a confirmed price_db quote as a priced
        # Tier 1 card (price_tbd=False → evidenceState="priced"). The State-C
        # "quoted" overlay (review_items) is the RFQ-reply path; a price_db
        # source="rfq" entry is the confirmed-quote PRICE store the matcher reads
        # directly. Either way, the card carries a real, dated, confirmed price —
        # not fabricated. evidenceState is "priced" (a real price exists).
        assert auth["evidenceState"] == "priced"
        # The aftermarket shop has no confirmed quote → still quote-expected.
        aftermarket = next(c for c in tier1 if c["vendorName"] == "Aftermarket Seals Shop")
        assert aftermarket["price"] is None

    def test_registry_tier1_not_cached_to_known_parts(self, api_with_tier1, tmp_path, monkeypatch):
        """I4: registry-backed Tier 1 candidates are NOT written to known_parts. A
        second sourcing run for the same part re-derives Tier 1 fresh (the card
        still renders) — it does NOT come from the cache, and no registry edge
        pollutes the cache. (Isolate known_parts to a temp file to inspect.)"""
        from utils import known_parts
        monkeypatch.setattr(known_parts, "_DB_PATH", str(tmp_path / "known_parts.json"))
        # First run: a real-PN Goulds seal request (cacheable key).
        detail1 = _run_sourcing(api_with_tier1, detected_type="mechanical seal",
                                manufacturer="Goulds", part_number="ST-1.375-T1")
        assert len(detail1["sourcing_results"]["tier1"]) == 2
        # The known_parts store must contain NO registry-backed Tier 1 edges.
        entries = known_parts.all_entries()
        key = known_parts.canonical_part_key("Goulds", "ST-1.375-T1")
        if key and key in entries:
            edges = entries[key].get("edges", {})
            # No edge for the registry-backed supplier domains.
            for eid, edge in edges.items():
                assert edge.get("tier") != 1, \
                    f"registry-backed Tier 1 edge was cached to known_parts: {eid}"
        # Second run for the same part — Tier 1 still renders (re-derived fresh).
        detail2 = _run_sourcing(api_with_tier1, detected_type="mechanical seal",
                                manufacturer="Goulds", part_number="ST-1.375-T1")
        assert len(detail2["sourcing_results"]["tier1"]) == 2
        assert detail2["sourcing_results"]["tier1"][0]["registryBacked"] is True
