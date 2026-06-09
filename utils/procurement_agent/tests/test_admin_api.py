"""
Tests for the internal admin/inspector API (api_server /api/admin/*).

The SECURITY-CRITICAL part: real role enforcement via require_admin (admin bearer
token). A non-admin caller is rejected at the API (403), not merely hidden in the UI.

Isolation mirrors test_api_server's `api` fixture (persistence DB on a temp file,
keys neutralized), plus the raw-sqlite3 / json stores the admin endpoints read
(supplier_registry, orders, price_db) redirected to tmp, and ARKIM_ADMIN_TOKEN set.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

_TOKEN = "test-admin-secret-123"


@pytest.fixture
def admin_api(tmp_path, monkeypatch):
    from utils.procurement_agent.state import persistence

    engine = persistence._make_engine(f"sqlite:///{tmp_path / 'api.sqlite'}")
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    persistence.Base.metadata.create_all(engine)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr(persistence, "_engine", engine)
    monkeypatch.setattr(persistence, "_SessionFactory", TestSession)

    # Redirect the raw-sqlite3 / json stores the admin endpoints read.
    from utils import supplier_registry, orders, price_db
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    monkeypatch.setattr(orders, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(orders, "_DB_PATH", str(tmp_path / "orders.sqlite"))
    monkeypatch.setattr(price_db, "_DB_PATH", str(tmp_path / "price_db.json"))

    monkeypatch.setenv("ARKIM_ADMIN_TOKEN", _TOKEN)

    import api_server
    monkeypatch.setattr(api_server, "_engine", engine)
    monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
    monkeypatch.setattr(api_server, "_messages", {})

    client = TestClient(api_server.app)
    client._token = _TOKEN
    return client


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAdminEnforcement:
    def test_admin_token_grants_access(self, admin_api):
        r = admin_api.get("/api/admin/ping", headers=_auth(_TOKEN))
        assert r.status_code == 200
        assert r.json() == {"ok": True, "role": "admin"}

    def test_wrong_token_is_forbidden(self, admin_api):
        r = admin_api.get("/api/admin/ping", headers=_auth("not-the-secret"))
        assert r.status_code == 403          # non-admin credential rejected at the API

    def test_missing_header_unauthorized(self, admin_api):
        assert admin_api.get("/api/admin/ping").status_code == 401

    def test_non_bearer_header_unauthorized(self, admin_api):
        r = admin_api.get("/api/admin/ping", headers={"Authorization": _TOKEN})  # no "Bearer "
        assert r.status_code == 401

    def test_server_secret_unset_disables_admin(self, admin_api, monkeypatch):
        monkeypatch.delenv("ARKIM_ADMIN_TOKEN", raising=False)
        r = admin_api.get("/api/admin/ping", headers=_auth("anything"))
        assert r.status_code == 503          # fail-closed, never open
