"""
Night 6 - Supplier claim-portal API tests (T1 admin surface + T2 profile/teaser).

Through the REAL API (TestClient) - guardrail 8 (live-faithfulness):

  T1 - the admin "Generate claim link" surface (admin-gated + SUPPLIER_PORTAL_V1
       gated; mints a hashed-at-rest token; returns the raw token ONCE).
  T2 - the public supplier route: valid token -> prepopulated profile + the
       read-only demand teaser (time-windowed, honest count from
       supplier_notifications). Zero-state -> honest category/network framing
       (never a "0" hero, never a fabricated count). Referrer-Policy header;
       no session cookie.

The fixtures isolate supplier_registry + claim_tokens to temp sqlite files,
flip SUPPLIER_PORTAL_V1 + TIER1_V2 ON, and neutralize external keys (the
conftest autouse safety net). No live network; no live email (EMAIL_SEND_ENABLED
force-OFF by the conftest safety net - the double-gate).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

_TOKEN = "test-admin-secret-portal"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def portal_api(tmp_path, monkeypatch):
    """TestClient over api_server + isolated stores with SUPPLIER_PORTAL_V1 ON
    and TIER1_V2 ON, plus one onboarded fixture supplier (DXP-like)."""
    from utils.procurement_agent.state import persistence
    engine = persistence._make_engine(f"sqlite:///{tmp_path / 'api.sqlite'}")
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    persistence.Base.metadata.create_all(engine)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr(persistence, "_engine", engine)
    monkeypatch.setattr(persistence, "_SessionFactory", TestSession)

    from utils import supplier_registry, claim_tokens
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "sr.sqlite"))
    monkeypatch.setattr(supplier_registry, "TIER1_V2", True)
    monkeypatch.setattr(claim_tokens, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(claim_tokens, "_DB_PATH", str(tmp_path / "claim_tokens.sqlite"))
    monkeypatch.setattr(claim_tokens, "CLAIM_TOKENS_ENABLED", True)
    monkeypatch.setenv("SUPPLIER_PORTAL_V1", "1")
    monkeypatch.setenv("TIER1_V2", "1")
    monkeypatch.setenv("ARKIM_ADMIN_TOKEN", _TOKEN)

    import api_server
    monkeypatch.setattr(api_server, "DEMO_MODE", False)
    monkeypatch.setattr(api_server, "_engine", engine)
    monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
    monkeypatch.setattr(api_server, "_messages", {})

    client = TestClient(api_server.app)
    client._token = _TOKEN
    client._api_server = api_server

    # Onboard one fixture supplier (a Goulds-authorized SEAL distributor).
    _onboard(supplier_registry, "dxpe.com", "DXP Enterprises",
             classes=[{"class_id": "SEAL", "is_core": True},
                      {"class_id": "PUMP", "is_core": False}],
             brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
             ship_area={"kind": "NATIONWIDE_US"})
    return client


@pytest.fixture
def portal_api_off(tmp_path, monkeypatch):
    """SUPPLIER_PORTAL_V1 OFF - for the inertness wall (T5)."""
    from utils.procurement_agent.state import persistence
    engine = persistence._make_engine(f"sqlite:///{tmp_path / 'api_off.sqlite'}")
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    persistence.Base.metadata.create_all(engine)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr(persistence, "_engine", engine)
    monkeypatch.setattr(persistence, "_SessionFactory", TestSession)

    from utils import supplier_registry, claim_tokens
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "sr_off.sqlite"))
    monkeypatch.setattr(supplier_registry, "TIER1_V2", True)
    monkeypatch.setattr(claim_tokens, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(claim_tokens, "_DB_PATH", str(tmp_path / "ct_off.sqlite"))
    monkeypatch.setattr(claim_tokens, "CLAIM_TOKENS_ENABLED", False)
    monkeypatch.setenv("SUPPLIER_PORTAL_V1", "")  # flag OFF
    monkeypatch.setenv("TIER1_V2", "1")
    monkeypatch.setenv("ARKIM_ADMIN_TOKEN", _TOKEN)

    import api_server
    monkeypatch.setattr(api_server, "DEMO_MODE", False)
    monkeypatch.setattr(api_server, "_engine", engine)
    monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
    monkeypatch.setattr(api_server, "_messages", {})
    client = TestClient(api_server.app)
    client._token = _TOKEN
    return client


def _onboard(reg, domain, name, *, classes, brands=None, ship_area=None):
    """Onboard a supplier with scope (mirrors test_tier1_runtime_live._onboard)."""
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
    for s in ("discovered", "contacted", "quoted", "onboarding", "onboarded"):
        reg.tier1_transition(domain, s)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mint(client, domain="dxpe.com"):
    """Mint a claim link via the admin endpoint; return the raw token."""
    r = client.post("/api/admin/suppliers/claim-link",
                    json={"supplier_domain": domain}, headers=_auth(client._token))
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _seed_notification(domain="dxpe.com", *, days_ago=0, reason="core_class"):
    """Record a genuine buyer-match notification event for a fixture supplier
    (the ONLY writer the portal teaser reads - the live notify layer)."""
    from utils import supplier_registry as sr
    at = (datetime.utcnow() - timedelta(days=days_ago)).isoformat()
    return sr.record_supplier_notification(
        run_id="fixture-run-uuid", supplier_domain=domain, vendor_name="DXP Enterprises",
        noun_class="SEAL", notify_reason=reason, send_status="stubbed",
        notified_at=at, metadata={"fixture": True},
    )


# ---------------------------------------------------------------------------
# T1 - admin "Generate claim link" surface
# ---------------------------------------------------------------------------

class TestClaimLinkAdmin:
    def test_generate_returns_link_and_raw_token_once(self, portal_api):
        r = portal_api.post("/api/admin/suppliers/claim-link",
                            json={"supplier_domain": "dxpe.com"},
                            headers=_auth(portal_api._token))
        assert r.status_code == 200
        b = r.json()
        assert b["ok"] is True
        assert b["supplier_domain"] == "dxpe.com"
        assert b["supplier_name"] == "DXP Enterprises"
        assert b["token"] and len(b["token"]) >= 32
        assert b["link_path"].startswith("/portal/")
        assert b["token"] in b["link_path"]
        assert b["expires_at"]

    def test_generate_no_admin_header_401(self, portal_api):
        r = portal_api.post("/api/admin/suppliers/claim-link",
                            json={"supplier_domain": "dxpe.com"})
        assert r.status_code == 401

    def test_generate_wrong_token_403(self, portal_api):
        r = portal_api.post("/api/admin/suppliers/claim-link",
                            json={"supplier_domain": "dxpe.com"},
                            headers=_auth("nope"))
        assert r.status_code == 403

    def test_generate_unknown_supplier_404(self, portal_api):
        r = portal_api.post("/api/admin/suppliers/claim-link",
                            json={"supplier_domain": "nope.com"},
                            headers=_auth(portal_api._token))
        assert r.status_code == 404

    def test_generate_empty_domain_422(self, portal_api):
        r = portal_api.post("/api/admin/suppliers/claim-link",
                            json={"supplier_domain": "  "},
                            headers=_auth(portal_api._token))
        assert r.status_code == 422

    def test_generate_flag_off_503(self, portal_api_off):
        r = portal_api_off.post("/api/admin/suppliers/claim-link",
                                json={"supplier_domain": "dxpe.com"},
                                headers=_auth(portal_api_off._token))
        # No supplier onboarded under the off fixture, but the flag gate fires
        # first (503) before the lookup.
        assert r.status_code == 503

    def test_regenerate_revokes_prior(self, portal_api):
        first = _mint(portal_api)
        r = portal_api.post("/api/admin/suppliers/claim-link/regenerate",
                            json={"supplier_domain": "dxpe.com"},
                            headers=_auth(portal_api._token))
        assert r.status_code == 200
        new = r.json()["token"]
        assert new != first
        # The prior token no longer validates through the public route.
        prior = portal_api.get(f"/api/portal/{first}/profile")
        assert prior.status_code == 404  # uniform rejection (T5)
        # The new token works.
        ok = portal_api.get(f"/api/portal/{new}/profile")
        assert ok.status_code == 200

    def test_list_tokens_metadata_only(self, portal_api):
        _mint(portal_api)
        r = portal_api.get("/api/admin/suppliers/dxpe.com/claim-tokens",
                           headers=_auth(portal_api._token))
        assert r.status_code == 200
        b = r.json()
        assert b["count"] >= 1
        # The raw token / hash never appear in the list.
        for row in b["tokens"]:
            assert "token" not in row
            assert "token_hash" not in row
        # token_prefix is present (rate-limit key, not a secret).
        assert "token_prefix" in b["tokens"][0]


# ---------------------------------------------------------------------------
# T2 - public supplier route: profile + demand teaser
# ---------------------------------------------------------------------------

class TestPublicProfileAndTeaser:
    def test_valid_token_renders_profile(self, portal_api):
        tok = _mint(portal_api)
        r = portal_api.get(f"/api/portal/{tok}/profile")
        assert r.status_code == 200
        b = r.json()
        assert b["supplier_domain"] == "dxpe.com"
        assert b["name"] == "DXP Enterprises"
        # The editable profile surfaces brands (tri-state), classes, ship-area,
        # aftermarket disclosure. The teaser is the HERO (first element).
        assert "teaser" in b
        assert "brands" in b
        assert "classes" in b
        assert "ship_area" in b
        # Brand relationship is the centerpiece (present + tri-state value).
        assert any(br["brand_id"] == "Goulds" and br["relationship"] == "AUTHORIZED"
                   for br in b["brands"])
        # NEVER exposes lifecycle status / performance / other suppliers.
        assert "tier1_lifecycle" not in b
        assert "performance" not in b
        assert "onboarding_status" not in b

    def test_teaser_hero_first_in_contract(self, portal_api):
        tok = _mint(portal_api)
        b = portal_api.get(f"/api/portal/{tok}/profile").json()
        keys = list(b.keys())
        # The teaser is the primary element of the returned page contract.
        assert keys.index("teaser") < keys.index("brands")

    def test_referrer_policy_header(self, portal_api):
        tok = _mint(portal_api)
        r = portal_api.get(f"/api/portal/{tok}/profile")
        assert r.status_code == 200
        assert r.headers.get("Referrer-Policy") == "no-referrer"

    def test_no_session_cookie_issued(self, portal_api):
        tok = _mint(portal_api)
        r = portal_api.get(f"/api/portal/{tok}/profile")
        # The route issues NO session cookies (token-only auth).
        set_cookie = r.headers.get("Set-Cookie")
        assert not set_cookie

    def test_teaser_has_matches_count(self, portal_api):
        _seed_notification("dxpe.com", days_ago=1)
        _seed_notification("dxpe.com", days_ago=2)
        tok = _mint(portal_api)
        b = portal_api.get(f"/api/portal/{tok}/profile").json()
        t = b["teaser"]
        assert t["has_matches"] is True
        assert t["count"] == 2
        # The window is shown (stated, bounded).
        assert "window_days" in t and t["window_days"] > 0
        # Only the bare count - no per-buyer / per-request detail.
        assert "events" not in t
        assert "runs" not in t

    def test_teaser_window_excludes_old_events(self, portal_api):
        # One within the window, one outside (40 days > 30-day default window).
        _seed_notification("dxpe.com", days_ago=5)
        _seed_notification("dxpe.com", days_ago=40)
        tok = _mint(portal_api)
        b = portal_api.get(f"/api/portal/{tok}/profile").json()
        assert b["teaser"]["count"] == 1  # only the within-window event

    def test_teaser_zero_state_honest_framing(self, portal_api):
        # NO notifications for the supplier -> zero-state, honest framing.
        tok = _mint(portal_api)
        b = portal_api.get(f"/api/portal/{tok}/profile").json()
        t = b["teaser"]
        assert t["has_matches"] is False
        assert t["count"] == 0
        # Never a "0 matches" hero; honest category/network framing text.
        assert "framing" in t and t["framing"]
        # No fabricated numbers in the zero-state.
        assert t["count"] == 0

    def test_teaser_supplier_scoped_not_other_suppliers(self, portal_api):
        # Notifications for a DIFFERENT supplier must not count toward this one.
        from utils import supplier_registry as sr
        sr._ensure_supplier_row("other.com", name="Other Co")
        sr.record_supplier_notification(
            run_id="r-other", supplier_domain="other.com", vendor_name="Other Co",
            noun_class="SEAL", notify_reason="core_class", send_status="stubbed",
            metadata={})
        _seed_notification("dxpe.com", days_ago=1)
        tok = _mint(portal_api)
        b = portal_api.get(f"/api/portal/{tok}/profile").json()
        assert b["teaser"]["count"] == 1  # only dxpe's own event


# ---------------------------------------------------------------------------
# T3 - propose-revision endpoint (registry UNCHANGED until approve)
# ---------------------------------------------------------------------------

class TestProposeRevision:
    def test_edit_lands_as_pending_revision(self, portal_api):
        tok = _mint(portal_api)
        # Edit: change the Goulds relationship AUTHORIZED -> CARRIES + add a class.
        r = portal_api.post(f"/api/portal/{tok}/propose-revision",
                            json={"brands": [
                                {"brand_id": "Goulds", "relationship": "CARRIES"}],
                                "classes": [
                                    {"class_id": "SEAL", "is_core": True}],
                                "ship_area": {"kind": "NATIONWIDE_US"}})
        assert r.status_code == 200
        b = r.json()
        assert b["ok"] is True
        assert b["revision_id"]
        assert b["status"] == "pending"

    def test_registry_unchanged_until_approve(self, portal_api):
        from utils import supplier_registry as sr
        tok = _mint(portal_api)
        before = sr.get_supplier_brands("dxpe.com")
        auth_before = [b for b in before if b["brand_id"] == "Goulds"][0]["relationship"]
        assert auth_before == "AUTHORIZED"
        portal_api.post(f"/api/portal/{tok}/propose-revision",
                        json={"brands": [
                            {"brand_id": "Goulds", "relationship": "CARRIES"}]})
        after = sr.get_supplier_brands("dxpe.com")
        auth_after = [b for b in after if b["brand_id"] == "Goulds"][0]["relationship"]
        # Registry UNCHANGED - the proposal is pending, not applied.
        assert auth_after == "AUTHORIZED"

    def test_propose_revision_no_admin_token_required(self, portal_api):
        # The propose endpoint is token-authed (portal token), NOT admin. An
        # admin header is NOT needed (and irrelevant).
        tok = _mint(portal_api)
        r = portal_api.post(f"/api/portal/{tok}/propose-revision",
                            json={"brands": [
                                {"brand_id": "Goulds", "relationship": "CARRIES"}]})
        assert r.status_code == 200

    def test_propose_invalid_relationship_rejected(self, portal_api):
        tok = _mint(portal_api)
        r = portal_api.post(f"/api/portal/{tok}/propose-revision",
                            json={"brands": [
                                {"brand_id": "Goulds", "relationship": "BOGUS"}]})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# T4 - concierge review of a supplier-proposed revision
# ---------------------------------------------------------------------------

class TestConciergeReviewRevision:
    def test_revision_surfaces_in_review_queue(self, portal_api):
        tok = _mint(portal_api)
        portal_api.post(f"/api/portal/{tok}/propose-revision",
                        json={"brands": [
                            {"brand_id": "Goulds", "relationship": "CARRIES"}]})
        q = portal_api.get("/api/admin/review-queue",
                           headers=_auth(portal_api._token)).json()
        revisions = [r for r in q["review_items"] if r.get("kind") == "supplier_revision"]
        assert len(revisions) == 1
        assert revisions[0]["status"] == "needs_human_review"
        assert revisions[0]["supplier_domain"] == "dxpe.com"

    def test_approve_applies_revision_to_registry(self, portal_api):
        from utils import supplier_registry as sr
        tok = _mint(portal_api)
        portal_api.post(f"/api/portal/{tok}/propose-revision",
                        json={"brands": [
                            {"brand_id": "Goulds", "relationship": "CARRIES"}]})
        q = portal_api.get("/api/admin/review-queue",
                           headers=_auth(portal_api._token)).json()
        rid = [r for r in q["review_items"] if r.get("kind") == "supplier_revision"][0]["id"]
        r = portal_api.post(f"/api/admin/portal/revisions/{rid}/approve",
                            headers=_auth(portal_api._token))
        assert r.status_code == 200
        # The registry now reflects the approved revision.
        brands = sr.get_supplier_brands("dxpe.com")
        assert [b for b in brands if b["brand_id"] == "Goulds"][0]["relationship"] == "CARRIES"

    def test_approve_no_lifecycle_drive(self, portal_api):
        """A revision approve re-applies scope but does NOT re-drive the
        lifecycle (the supplier is already onboarded - approve_draft drives
        discovered->onboarded; the revision path must not)."""
        from utils import supplier_registry as sr
        tok = _mint(portal_api)
        before_lc = sr.get_tier1_lifecycle("dxpe.com")
        assert before_lc == "onboarded"
        portal_api.post(f"/api/portal/{tok}/propose-revision",
                        json={"brands": [
                            {"brand_id": "Goulds", "relationship": "CARRIES"}]})
        q = portal_api.get("/api/admin/review-queue",
                           headers=_auth(portal_api._token)).json()
        rid = [r for r in q["review_items"] if r.get("kind") == "supplier_revision"][0]["id"]
        portal_api.post(f"/api/admin/portal/revisions/{rid}/approve",
                        headers=_auth(portal_api._token))
        after_lc = sr.get_tier1_lifecycle("dxpe.com")
        assert after_lc == "onboarded"  # unchanged

    def test_reject_discards_revision(self, portal_api):
        from utils import supplier_registry as sr
        tok = _mint(portal_api)
        portal_api.post(f"/api/portal/{tok}/propose-revision",
                        json={"brands": [
                            {"brand_id": "Goulds", "relationship": "CARRIES"}]})
        q = portal_api.get("/api/admin/review-queue",
                           headers=_auth(portal_api._token)).json()
        rid = [r for r in q["review_items"] if r.get("kind") == "supplier_revision"][0]["id"]
        r = portal_api.post(f"/api/admin/portal/revisions/{rid}/reject",
                            headers=_auth(portal_api._token))
        assert r.status_code == 200
        # Registry unchanged.
        brands = sr.get_supplier_brands("dxpe.com")
        assert [b for b in brands if b["brand_id"] == "Goulds"][0]["relationship"] == "AUTHORIZED"
        # The revision is marked rejected.
        q2 = portal_api.get("/api/admin/review-queue",
                            headers=_auth(portal_api._token)).json()
        rev = [r for r in q2["review_items"] if r["id"] == rid][0]
        assert rev["status"] == "rejected"


# ---------------------------------------------------------------------------
# T5 - inertness + security
# ---------------------------------------------------------------------------

class TestInertnessFlagOff:
    def test_flag_off_route_byte_identical_to_unknown(self, portal_api_off):
        """SUPPLIER_PORTAL_V1 OFF -> the public route responds byte-identically
        to an unknown route (404 + FastAPI's unknown-route body)."""
        r_portal = portal_api_off.get("/api/portal/some-token/profile")
        r_unknown = portal_api_off.get("/api/this-route-does-not-exist")
        assert r_portal.status_code == 404
        assert r_unknown.status_code == 404
        assert r_portal.json() == r_unknown.json()

    def test_flag_off_propose_revision_absent(self, portal_api_off):
        r = portal_api_off.post("/api/portal/some-token/propose-revision",
                                json={"brands": []})
        r_unknown = portal_api_off.post("/api/nope", json={})
        assert r.status_code == 404
        assert r.json() == r_unknown.json()

    @pytest.mark.parametrize("flag", ["", "0", "false", "no", "off", "junk", None])
    def test_falsy_token_is_flag_off(self, monkeypatch, tmp_path, flag):
        """Every non-truthy flag token -> the route is OFF (fail safe/closed)."""
        import api_server
        # The live resolver reads os.environ; a falsy token -> OFF.
        monkeypatch.setenv("SUPPLIER_PORTAL_V1", flag or "")
        assert api_server._portal_enabled() is False


class TestUniformRejection:
    def _profile(self, client, token):
        return client.get(f"/api/portal/{token}/profile")

    def test_invalid_expired_reused_uniform(self, portal_api):
        live = _mint(portal_api)
        # Make an expired token (mint then force its row into the past).
        from utils import claim_tokens as ct
        import hashlib, sqlite3
        exp = ct.generate_for("dxpe.com", expiry_days=0)
        conn = sqlite3.connect(ct._DB_PATH)
        conn.execute(
            "UPDATE claim_tokens SET expires_at = ? WHERE token_hash = ?",
            ((datetime.utcnow() - timedelta(days=1)).isoformat(),
             hashlib.sha256(exp["token"].encode()).hexdigest()),
        )
        conn.commit()
        conn.close()
        # All three rejected categories -> the SAME 404 (no oracle).
        r_invalid = self._profile(portal_api, "totally-garbage")
        r_expired = self._profile(portal_api, exp["token"])
        # Regeneration revokes `live`; a reused-after-regen token is rejected.
        new = portal_api.post("/api/admin/suppliers/claim-link/regenerate",
                              json={"supplier_domain": "dxpe.com"},
                              headers=_auth(portal_api._token)).json()["token"]
        r_reused = self._profile(portal_api, live)
        r_live = self._profile(portal_api, new)
        assert r_invalid.status_code == r_expired.status_code == r_reused.status_code
        assert r_invalid.json() == r_expired.json() == r_reused.json()
        assert r_invalid.status_code == 404
        # The live token still works.
        assert r_live.status_code == 200


class TestNoAdminSurfaceFromPortal:
    def test_portal_token_reaches_no_admin(self, portal_api):
        """A portal token must NEVER satisfy require_admin - no /api/admin/*
        path is reachable through it."""
        tok = _mint(portal_api)
        # Use the portal token as an admin bearer - must be rejected.
        for path in ("/api/admin/ping", "/api/admin/suppliers",
                     "/api/admin/review-queue"):
            r = portal_api.get(path, headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code in (401, 403), (path, r.status_code, r.text)


class TestNoRegistryWriteFromPortal:
    def test_property_no_registry_write_from_supplier_route(self, portal_api):
        """PROPERTY TEST: no code path in the supplier route writes the
        supplier registry. The only writers are the admin concierge paths
        (approve). A propose-revision writes a review_items row ONLY (the
        pending store), never the scope tables."""
        from utils import supplier_registry as sr
        tok = _mint(portal_api)
        # Snapshot the registry scope BEFORE.
        before_brands = sr.get_supplier_brands("dxpe.com")
        before_classes = sr.get_supplier_classes("dxpe.com")
        before_terr = sr.get_supplier_territory("dxpe.com")
        # Exercise every supplier-route mutation.
        portal_api.post(f"/api/portal/{tok}/propose-revision",
                        json={"brands": [{"brand_id": "Goulds", "relationship": "CARRIES"}],
                              "classes": [{"class_id": "PUMP", "is_core": True}],
                              "ship_area": {"kind": "STATES", "states": ["TX"]}})
        portal_api.get(f"/api/portal/{tok}/profile")
        # The registry scope tables are UNCHANGED.
        assert sr.get_supplier_brands("dxpe.com") == before_brands
        assert sr.get_supplier_classes("dxpe.com") == before_classes
        assert sr.get_supplier_territory("dxpe.com") == before_terr


class TestRateLimit:
    def test_rate_limit_keyed_on_ip_and_prefix(self, portal_api, monkeypatch):
        """Repeated invalid-token hits from the same IP are rate-limited (429
        after the cap); the limiter is keyed on IP + token-prefix (so a valid
        token is not penalized by an attacker's noise on the same IP beyond the
        prefix dimension)."""
        tok = _mint(portal_api)
        # Hammer the route with garbage tokens from one IP (TestClient has a
        # fixed client IP). The cap is small (test default); after it, 429.
        statuses = []
        for i in range(60):
            r = portal_api.get(f"/api/portal/garbage-{i}/profile")
            statuses.append(r.status_code)
            if r.status_code == 429:
                break
        assert 429 in statuses, "rate limit never engaged"
        # A VALID token still works through the rate-limited IP (not blocked by
        # the invalid-token bucket - keyed on token-prefix, the valid token has
        # a distinct prefix).
        r_live = portal_api.get(f"/api/portal/{tok}/profile")
        assert r_live.status_code == 200
