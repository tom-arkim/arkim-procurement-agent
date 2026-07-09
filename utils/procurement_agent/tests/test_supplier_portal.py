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
