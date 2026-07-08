"""
Night 4 — onboarding admin API tests (api_server /api/admin/onboarding/*).

Security-critical: every endpoint is require_admin (401/403/503) AND gated on
TIER1_V2 (503 dormant when off). The harvester endpoint fetches arbitrary URLs
server-side (SSRF caution) — these tests assert it is NOT reachable
unauthenticated and that it never fetches a non-public host.

The live fetcher is replaced with a fixture/injected fetcher so no network
fires. The LLM is mocked via the extractor's llm_caller injection (no live
Anthropic call).
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

_TOKEN = "test-admin-secret-onboarding"

_FIXTURE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))),
    "tests", "fixtures", "supplier_sites",
)


@pytest.fixture
def admin_api(tmp_path, monkeypatch):
    from utils.procurement_agent.state import persistence
    engine = persistence._make_engine(f"sqlite:///{tmp_path / 'api.sqlite'}")
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    persistence.Base.metadata.create_all(engine)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-test-key")  # path enabled; _call_llm is mocked per-test
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr(persistence, "_engine", engine)
    monkeypatch.setattr(persistence, "_SessionFactory", TestSession)

    from utils import supplier_registry, orders, price_db, site_settings
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    monkeypatch.setattr(supplier_registry, "TIER1_V2", True)
    monkeypatch.setattr(orders, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(orders, "_DB_PATH", str(tmp_path / "orders.sqlite"))
    monkeypatch.setattr(price_db, "_DB_PATH", str(tmp_path / "price_db.json"))
    monkeypatch.setattr(site_settings, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(site_settings, "_DB_PATH", str(tmp_path / "site_settings.sqlite"))
    monkeypatch.setenv("ARKIM_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setenv("TIER1_V2", "1")

    import api_server
    # A prior test module (test_demo_mode) may have reloaded api_server with
    # DEMO_MODE=true and left it cached in sys.modules; the allowlist middleware
    # reads the module global at request time, so reset it to False here or the
    # onboarding routes (not on the demo allowlist) get 403 instead of 401/503.
    monkeypatch.setattr(api_server, "DEMO_MODE", False)
    monkeypatch.setattr(api_server, "_engine", engine)
    monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
    monkeypatch.setattr(api_server, "_messages", {})

    client = TestClient(api_server.app)
    client._token = _TOKEN
    return client


@pytest.fixture
def admin_api_off(tmp_path, monkeypatch):
    """TIER1_V2 OFF — the onboarding surface must be dormant (503)."""
    from utils.procurement_agent.state import persistence
    engine = persistence._make_engine(f"sqlite:///{tmp_path / 'api_off.sqlite'}")
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    persistence.Base.metadata.create_all(engine)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-test-key")  # path enabled; _call_llm is mocked per-test
    monkeypatch.setattr(persistence, "_engine", engine)
    monkeypatch.setattr(persistence, "_SessionFactory", TestSession)
    from utils import supplier_registry
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "sr.sqlite"))
    monkeypatch.setattr(supplier_registry, "TIER1_V2", False)
    monkeypatch.setenv("TIER1_V2", "")
    monkeypatch.setenv("ARKIM_ADMIN_TOKEN", _TOKEN)
    import api_server
    monkeypatch.setattr(api_server, "DEMO_MODE", False)  # see admin_api note (demo-mode reload poisoning)
    monkeypatch.setattr(api_server, "_engine", engine)
    monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
    monkeypatch.setattr(api_server, "_messages", {})
    client = TestClient(api_server.app)
    client._token = _TOKEN
    return client


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _patch_harvester_fetch(monkeypatch, slug):
    """Replace the live harvester fetcher with a fixture-backed one (offline)."""
    import json
    manifest = json.load(open(os.path.join(_FIXTURE_ROOT, slug, "manifest.json"),
                              encoding="utf-8"))

    def fake_fetch(url, *a, **kw):
        for u, fname in manifest["pages"].items():
            if url.rstrip("/").lower() == u.rstrip("/").lower():
                return open(os.path.join(_FIXTURE_ROOT, slug, fname),
                            encoding="utf-8", errors="replace").read()
        return None

    from utils.procurement_agent.onboarding import harvester
    monkeypatch.setattr(harvester, "default_fetch_html", fake_fetch)


def _patch_llm(monkeypatch, parsed):
    """Mock the extractor's LLM call (no live Anthropic)."""
    from utils.procurement_agent.onboarding import extractor
    def fake_call(system, user, key, model):
        return dict(parsed)
    monkeypatch.setattr(extractor, "_call_llm", fake_call)


# ---------------------------------------------------------------------------
# Auth gating (401/403/503)
# ---------------------------------------------------------------------------

class TestOnboardingAuthGating:
    _ENDPOINTS = [
        ("GET", "/api/admin/onboarding/drafts"),
    ]

    def test_no_header_401(self, admin_api):
        assert admin_api.get("/api/admin/onboarding/drafts").status_code == 401

    def test_wrong_token_403(self, admin_api):
        r = admin_api.get("/api/admin/onboarding/drafts",
                          headers=_auth("not-the-secret"))
        assert r.status_code == 403

    def test_admin_token_200(self, admin_api):
        r = admin_api.get("/api/admin/onboarding/drafts", headers=_auth(_TOKEN))
        assert r.status_code == 200
        assert r.json() == {"count": 0, "drafts": []}

    def test_flag_off_503(self, admin_api_off):
        # Admin token present but TIER1_V2 off -> 503 dormant.
        r = admin_api_off.get("/api/admin/onboarding/drafts", headers=_auth(_TOKEN))
        assert r.status_code == 503

    def test_harvest_no_header_401(self, admin_api):
        r = admin_api.post("/api/admin/onboarding/harvest", json={"url": "https://x.com/"})
        assert r.status_code == 401

    def test_harvest_wrong_token_403(self, admin_api):
        r = admin_api.post("/api/admin/onboarding/harvest",
                           json={"url": "https://x.com/"}, headers=_auth("nope"))
        assert r.status_code == 403

    def test_server_secret_unset_disables(self, admin_api, monkeypatch):
        monkeypatch.delenv("ARKIM_ADMIN_TOKEN", raising=False)
        r = admin_api.get("/api/admin/onboarding/drafts", headers=_auth("anything"))
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# SSRF — the harvester must refuse non-public hosts (admin-gated; this is the
# server-side-fetch caution). Tested via the harvester unit tests directly; here
# we assert the endpoint refuses a non-http scheme at the API layer.
# ---------------------------------------------------------------------------

class TestOnboardingSSRF:
    def test_harvest_rejects_non_http_scheme(self, admin_api):
        r = admin_api.post("/api/admin/onboarding/harvest",
                           json={"url": "file:///etc/passwd"},
                           headers=_auth(_TOKEN))
        assert r.status_code == 422  # api_server._url_scheme_ok gate


# ---------------------------------------------------------------------------
# End-to-end: harvest → draft → approve (offline fetcher + mocked LLM)
# ---------------------------------------------------------------------------

class TestOnboardingEndToEnd:
    def test_harvest_creates_pending_draft(self, admin_api, monkeypatch):
        _patch_harvester_fetch(monkeypatch, "ibt")
        _patch_llm(monkeypatch, {
            "name": "IBT Industrial Solutions",
            "vertical": "industrial power transmission distribution",
            "brands": [{"name": "SKF", "relationship_guess": "CARRIES",
                        "confidence": 0.8, "evidence": "SKF on brands page",
                        "source_url": "https://ibtinc.com/brands/"}],
            "classes": [{"class_id": "BEARING", "confidence": 0.9,
                         "is_core_guess": True, "evidence": "bearings",
                         "source_url": "https://ibtinc.com/"}],
            "locations": [{"locality": "Merriam", "region": "KS", "country": "US",
                           "confidence": 0.7, "evidence": "Merriam, KS"}],
            "ship_area_guess": {"kind": "NATIONWIDE_US"},
            "overall_confidence": 0.8,
        })
        r = admin_api.post("/api/admin/onboarding/harvest",
                           json={"url": "https://ibtinc.com/"},
                           headers=_auth(_TOKEN))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["draft_id"]
        assert body["draft"]["status"] == "needs_human_review"
        assert body["draft"]["domain"] == "ibtinc.com"
        assert any(b["name"] == "SKF" for b in body["draft"]["brands"])
        # Nothing in the registry yet (approve-gated).
        from utils import supplier_registry as sr
        assert sr.get_supplier_classes("ibtinc.com") == []
        assert sr.get_tier1_lifecycle("ibtinc.com") is None

    def test_drafts_list_and_detail(self, admin_api, monkeypatch):
        _patch_harvester_fetch(monkeypatch, "ibt")
        _patch_llm(monkeypatch, {"name": "IBT", "vertical": "pt",
                                 "brands": [], "classes": [],
                                 "locations": [], "ship_area_guess": None,
                                 "overall_confidence": 0.3})
        admin_api.post("/api/admin/onboarding/harvest",
                       json={"url": "https://ibtinc.com/"}, headers=_auth(_TOKEN))
        lst = admin_api.get("/api/admin/onboarding/drafts", headers=_auth(_TOKEN)).json()
        assert lst["count"] == 1
        did = lst["drafts"][0]["id"]
        detail = admin_api.get(f"/api/admin/onboarding/drafts/{did}",
                               headers=_auth(_TOKEN))
        assert detail.status_code == 200
        assert detail.json()["id"] == did

    def test_approve_writes_registry_and_onboards(self, admin_api, monkeypatch):
        _patch_harvester_fetch(monkeypatch, "ibt")
        _patch_llm(monkeypatch, {
            "name": "IBT Industrial Solutions",
            "vertical": "industrial power transmission distribution",
            "brands": [{"name": "SKF", "relationship_guess": "CARRIES",
                        "confidence": 0.8, "evidence": "x",
                        "source_url": "https://ibtinc.com/brands/"}],
            "classes": [{"class_id": "BEARING", "confidence": 0.9,
                         "is_core_guess": True, "evidence": "x",
                         "source_url": "https://ibtinc.com/"}],
            "locations": [], "ship_area_guess": {"kind": "NATIONWIDE_US"},
            "overall_confidence": 0.8,
        })
        h = admin_api.post("/api/admin/onboarding/harvest",
                           json={"url": "https://ibtinc.com/"}, headers=_auth(_TOKEN))
        did = h.json()["draft_id"]
        ap = admin_api.post(f"/api/admin/onboarding/drafts/{did}/approve",
                            json={}, headers=_auth(_TOKEN))
        assert ap.status_code == 200, ap.text
        from utils import supplier_registry as sr
        assert sr.get_tier1_lifecycle("ibtinc.com") == sr.TIER1_ONBOARDED
        assert any(c["class_id"] == "BEARING"
                   for c in sr.get_supplier_classes("ibtinc.com"))
        assert any(b["brand_id"] == "SKF"
                   for b in sr.get_supplier_brands("ibtinc.com"))
        # Draft is now confirmed.
        d = admin_api.get(f"/api/admin/onboarding/drafts/{did}", headers=_auth(_TOKEN))
        assert d.json()["status"] == "confirmed"

    def test_double_approve_idempotent(self, admin_api, monkeypatch):
        _patch_harvester_fetch(monkeypatch, "ibt")
        _patch_llm(monkeypatch, {"name": "IBT", "vertical": "pt",
                                 "brands": [], "classes": [],
                                 "locations": [], "ship_area_guess": None,
                                 "overall_confidence": 0.3})
        did = admin_api.post("/api/admin/onboarding/harvest",
                             json={"url": "https://ibtinc.com/"},
                             headers=_auth(_TOKEN)).json()["draft_id"]
        a1 = admin_api.post(f"/api/admin/onboarding/drafts/{did}/approve",
                            json={}, headers=_auth(_TOKEN))
        a2 = admin_api.post(f"/api/admin/onboarding/drafts/{did}/approve",
                            json={}, headers=_auth(_TOKEN))
        assert a1.status_code == 200 and a2.status_code == 200
        from utils import supplier_registry as sr
        assert sr.get_tier1_lifecycle("ibtinc.com") == sr.TIER1_ONBOARDED

    def test_reject_discards(self, admin_api, monkeypatch):
        _patch_harvester_fetch(monkeypatch, "ibt")
        _patch_llm(monkeypatch, {"name": "IBT", "vertical": "pt",
                                 "brands": [], "classes": [],
                                 "locations": [], "ship_area_guess": None,
                                 "overall_confidence": 0.3})
        did = admin_api.post("/api/admin/onboarding/harvest",
                             json={"url": "https://ibtinc.com/"},
                             headers=_auth(_TOKEN)).json()["draft_id"]
        r = admin_api.post(f"/api/admin/onboarding/drafts/{did}/reject",
                           headers=_auth(_TOKEN))
        assert r.status_code == 200
        from utils import supplier_registry as sr
        assert sr.get_tier1_lifecycle("ibtinc.com") is None
        assert sr.get_supplier_classes("ibtinc.com") == []

    def test_approve_with_revisions(self, admin_api, monkeypatch):
        _patch_harvester_fetch(monkeypatch, "ibt")
        _patch_llm(monkeypatch, {"name": "IBT", "vertical": "pt",
                                 "brands": [], "classes": [],
                                 "locations": [], "ship_area_guess": None,
                                 "overall_confidence": 0.3})
        did = admin_api.post("/api/admin/onboarding/harvest",
                             json={"url": "https://ibtinc.com/"},
                             headers=_auth(_TOKEN)).json()["draft_id"]
        r = admin_api.post(f"/api/admin/onboarding/drafts/{did}/approve",
                           json={"name": "IBT Industrial",
                                 "vertical": "power transmission",
                                 "ship_area_guess": {"kind": "NATIONWIDE_US"},
                                 "brands": [{"name": "Timken", "relationship_guess": "AUTHORIZED"}],
                                 "classes": [{"class_id": "BEARING", "is_core_guess": True}]},
                           headers=_auth(_TOKEN))
        assert r.status_code == 200
        from utils import supplier_registry as sr
        assert sr.lookup_by_domain("ibtinc.com")["name"] == "IBT Industrial"
        assert any(b["brand_id"] == "Timken"
                   for b in sr.get_supplier_brands("ibtinc.com"))

    def test_detail_404_unknown(self, admin_api):
        r = admin_api.get("/api/admin/onboarding/drafts/nonexistent",
                          headers=_auth(_TOKEN))
        assert r.status_code == 404

    def test_approve_404_unknown(self, admin_api):
        r = admin_api.post("/api/admin/onboarding/drafts/nonexistent/approve",
                           json={}, headers=_auth(_TOKEN))
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# DEMO_MODE allowlist — onboarding routes must NOT be on it (fail-closed)
# ---------------------------------------------------------------------------

class TestOnboardingNotOnDemoAllowlist:
    def test_onboarding_routes_absent_from_demo_allowlist(self):
        import api_server
        for method, path in api_server._DEMO_ALLOWLIST:
            assert "onboarding" not in path, (method, path)
