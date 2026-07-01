"""
DEMO_MODE allowlist middleware + email boot-refusal tests (the public demo spine).

Two guards, both active ONLY when DEMO_MODE is truthy, both inert otherwise:
  1. deny-by-default allowlist middleware in api_server — only the confirmed
     demo routes reach their handler; everything else 403s (incl. /docs,
     /openapi.json, mutation/admin/RFQ/email routes, and any unlisted route).
     Fail-closed: a route not explicitly listed is DENIED, never open.
  2. startup boot-refusal: api_server will not import under DEMO_MODE if
     EMAIL_SEND_ENABLED is true (a public demo must not boot with email on).

Isolation: each test imports api_server under a controlled env (DEMO_MODE /
EMAIL_SEND_ENABLED set BEFORE a clean re-import, since the module reads them at
import time) and points persistence + supplier_registry at a per-test temp DB, so
the module's import-time create_all/_migrate_schema/_seed never touch real data.
The standard `api` fixture in test_api_server.py imports with DEMO_MODE unset
(inert) and is unaffected by anything here.
"""
import json
import sys
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


def _import_api_server_fresh(monkeypatch, tmp_path):
    """Import api_server under a controlled env + isolated DB, regardless of
    whether it was already imported earlier in the session.

    The caller is expected to have set DEMO_MODE / EMAIL_SEND_ENABLED on the env
    BEFORE calling (the module reads them at import time). Returns the imported
    module with persistence globals re-bound to the temp DB so create_all /
    _migrate_schema / _seed never touch real data."""
    import importlib
    from utils.procurement_agent.state import persistence

    engine = persistence._make_engine(f"sqlite:///{tmp_path / 'demo.sqlite'}")
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    persistence.Base.metadata.create_all(engine)
    monkeypatch.setattr(persistence, "_engine", engine)
    monkeypatch.setattr(persistence, "_SessionFactory", TestSession)

    from utils import supplier_registry
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))

    # api_server reads env at import; force a clean re-import so it sees the env
    # the test just set. Reload, then rebind the names api_server bound at import.
    sys.modules.pop("api_server", None)
    import api_server
    importlib.reload(api_server)
    monkeypatch.setattr(api_server, "_engine", engine)
    monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
    monkeypatch.setattr(api_server, "_messages", {})
    return api_server


@pytest.fixture
def demo_off(monkeypatch, tmp_path):
    """api_server with DEMO_MODE unset — the middleware is INERT (no regression)."""
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("EMAIL_SEND_ENABLED", "")
    api = _import_api_server_fresh(monkeypatch, tmp_path)
    assert api.DEMO_MODE is False
    return TestClient(api.app), api


@pytest.fixture
def demo_on(monkeypatch, tmp_path):
    """api_server with DEMO_MODE=true and email OFF — the allowlist is ACTIVE."""
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("EMAIL_SEND_ENABLED", "")   # boot-refusal would fire otherwise
    api = _import_api_server_fresh(monkeypatch, tmp_path)
    assert api.DEMO_MODE is True
    return TestClient(api.app), api


# ---------------------------------------------------------------------------
# FAIL-CLOSED (the property that matters most): dangerous routes -> 403, not 200.
# ---------------------------------------------------------------------------

class TestDemoAllowlistFailClosed:
    def test_execute_is_403(self, demo_on):
        client, _ = demo_on
        assert client.post("/api/runs/r1/execute").status_code == 403

    def test_rfq_send_is_403(self, demo_on):
        client, _ = demo_on
        assert client.post("/api/rfq-drafts/d1/send").status_code == 403

    def test_put_asset_specs_is_403(self, demo_on):
        client, _ = demo_on
        r = client.put("/api/runs/r1/asset-specs", json={"asset_specs": {"x": 1}})
        assert r.status_code == 403

    def test_order_now_is_403(self, demo_on):
        client, _ = demo_on
        r = client.post("/api/runs/r1/order-now", json={"candidate_id": "c", "tier": 1})
        assert r.status_code == 403

    def test_group_approve_is_403(self, demo_on):
        client, _ = demo_on
        r = client.post("/api/groups/g1/approve",
                        json={"approver_name": "a", "approver_role": "r"})
        assert r.status_code == 403

    def test_approval_rules_post_is_403(self, demo_on):
        client, _ = demo_on
        r = client.post("/api/approval-rules",
                        json={"facility_id": "f", "threshold": 0})
        assert r.status_code == 403

    def test_debug_llm_is_403(self, demo_on):
        client, _ = demo_on
        assert client.get("/api/debug/llm").status_code == 403

    def test_select_candidate_is_403(self, demo_on):
        client, _ = demo_on
        r = client.post("/api/runs/r1/select-candidate",
                        json={"candidate_id": "c", "tier": 1})
        assert r.status_code == 403

    def test_approve_is_403(self, demo_on):
        client, _ = demo_on
        r = client.post("/api/runs/r1/approve",
                        json={"approver_name": "a", "approver_role": "r"})
        assert r.status_code == 403

    def test_admin_ping_is_403(self, demo_on):
        # defense-in-depth: allowlist-blocks even though require_admin already gates it.
        client, _ = demo_on
        assert client.get("/api/admin/ping").status_code == 403

    def test_dev_reseed_is_403(self, demo_on):
        client, _ = demo_on
        assert client.post("/api/dev/reseed-handoffs").status_code == 403

    def test_requests_front_door_is_403(self, demo_on):
        # /api/requests is NOT used by the proc demo frontend -> stays BLOCK.
        client, _ = demo_on
        r = client.post("/api/requests", json={"parts": []})
        assert r.status_code == 403

    def test_request_confirmation_is_403(self, demo_on):
        # Mock-only but not called by the proc demo path -> stays BLOCK.
        client, _ = demo_on
        r = client.post("/api/runs/r1/request-confirmation", json={"candidate_ids": ["c"]})
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# SCHEMA-LEAK CLOSED: /docs + /openapi.json -> 403 (the auto-route catch).
# ---------------------------------------------------------------------------

class TestDemoSchemaLeakClosed:
    def test_docs_is_403(self, demo_on):
        client, _ = demo_on
        assert client.get("/docs").status_code == 403

    def test_openapi_json_is_403(self, demo_on):
        client, _ = demo_on
        assert client.get("/openapi.json").status_code == 403


# ---------------------------------------------------------------------------
# DENY-BY-DEFAULT PROOF: it's an allowlist, not a blocklist. A path nobody
# listed and nobody explicitly tested is still 403 -> a missed route fails closed.
# ---------------------------------------------------------------------------

class TestDemoDenyByDefault:
    def test_unlisted_made_up_path_is_403(self, demo_on):
        client, _ = demo_on
        assert client.get("/api/some-route-that-does-not-exist-in-allowlist").status_code == 403

    def test_unlisted_made_up_mutation_is_403(self, demo_on):
        client, _ = demo_on
        assert client.post("/api/totally-uninvented-route").status_code == 403


# ---------------------------------------------------------------------------
# ALLOWLIST WORKS: each confirmed demo route reaches its handler (not 403).
# ---------------------------------------------------------------------------

class TestDemoAllowlistWorks:
    def test_health_reaches_handler(self, demo_on):
        client, _ = demo_on
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_facilities_reaches_handler(self, demo_on):
        client, _ = demo_on
        r = client.get("/api/facilities")
        assert r.status_code == 200
        assert isinstance(r.json(), list) and r.json()

    def test_create_run_reaches_handler(self, demo_on):
        client, _ = demo_on
        r = client.post("/api/runs", json={"facility_id": "fac-stockton"})
        assert r.status_code == 201
        assert r.json()["phase"] == "intake"

    def test_get_run_reaches_handler(self, demo_on):
        client, _ = demo_on
        rid = client.post("/api/runs", json={}).json()["id"]
        r = client.get(f"/api/runs/{rid}")
        assert r.status_code == 200
        assert r.json()["id"] == rid

    def test_messages_reaches_handler(self, demo_on, monkeypatch):
        client, api = demo_on
        # Mock IntakeAgent so the handler returns without a live Anthropic call.
        agent = Mock()
        agent.run.return_value = {
            "sufficient": False, "asset_specs": {}, "manufacturer_confidence": 0,
            "part_id_confidence": 0, "follow_up_question": "Which manufacturer?",
            "confidence_summary": {},
        }
        monkeypatch.setattr(api, "IntakeAgent", Mock(return_value=agent))
        rid = client.post("/api/runs", json={}).json()["id"]
        r = client.post(f"/api/runs/{rid}/messages", json={"content": "a pump"})
        assert r.status_code == 200   # reaches handler (not 403)

    def test_confirm_intake_reaches_handler(self, demo_on, monkeypatch):
        client, api = demo_on
        rid = client.post("/api/runs", json={}).json()["id"]
        # Seed specs so confirm-intake passes the 422 "no specs" guard and the
        # 409 phase guard — proving the handler actually ran (a 422/409 here would
        # be a handler response, not the middleware's 403).
        SF = api._SessionFactory
        ORM = api.SourcingRunORM
        with SF() as s:
            run = s.get(ORM, rid)
            run.asset_specs_json = json.dumps({"manufacturer": "Goulds", "part_number": "PN-1"})
            s.commit()
        # Keep the background sourcing task offline (no Tavily/Anthropic key).
        monkeypatch.setattr(api, "_run_sourcing_background", lambda *a, **k: None)
        r = client.post(f"/api/runs/{rid}/confirm-intake")
        assert r.status_code == 200
        assert r.json()["phase"] == "sourcing"

    def test_groups_get_reaches_handler(self, demo_on):
        client, _ = demo_on
        # Unknown group -> 404 from the handler, NOT 403 from the middleware.
        # (A 404 is a handler response — proves the route is allowlisted.)
        r = client.get("/api/groups/no-such-group")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# PATH-MATCH SAFETY: the matcher must not let extra trailing segments through an
# allow rule. /api/runs/{id}/execute is DENIED under the /api/runs/{id} GET rule.
# ---------------------------------------------------------------------------

class TestDemoPathMatchSafety:
    def test_execute_under_run_get_rule_is_denied(self, demo_on):
        client, _ = demo_on
        assert client.get("/api/runs/r1/execute").status_code == 403
        assert client.post("/api/runs/r1/execute").status_code == 403

    def test_extra_segment_on_health_is_denied(self, demo_on):
        client, _ = demo_on
        assert client.get("/api/health/extra").status_code == 403

    def test_extra_segment_on_facilities_is_denied(self, demo_on):
        client, _ = demo_on
        assert client.get("/api/facilities/extra").status_code == 403

    def test_method_mismatch_is_denied(self, demo_on):
        client, _ = demo_on
        # GET /api/runs is not on the list (only POST /api/runs is).
        assert client.get("/api/runs").status_code == 403
        # PUT /api/health is not on the list (only GET is).
        assert client.put("/api/health").status_code == 403


# ---------------------------------------------------------------------------
# DEMO_MODE OFF (no regression): with DEMO_MODE unset the middleware is inert —
# every route behaves exactly as today. A blocked-under-demo route returns its
# NORMAL (non-403) response, not the spine's 403.
# ---------------------------------------------------------------------------

class TestDemoModeOffNoRegression:
    def test_debug_llm_reaches_handler_when_off(self, demo_off):
        client, _ = demo_off
        # /api/debug/llm is 403 under DEMO_MODE; with it off the handler runs and
        # returns 200 (ok:false — no key in the test env), NOT 403.
        r = client.get("/api/debug/llm")
        assert r.status_code != 403
        assert r.status_code == 200

    def test_docs_served_when_off(self, demo_off):
        client, _ = demo_off
        # /docs is 403 under DEMO_MODE; with it off FastAPI serves it normally.
        assert client.get("/docs").status_code == 200

    def test_openapi_served_when_off(self, demo_off):
        client, _ = demo_off
        r = client.get("/openapi.json")
        assert r.status_code == 200
        assert "paths" in r.json()

    def test_create_run_still_201_when_off(self, demo_off):
        client, _ = demo_off
        assert client.post("/api/runs", json={}).status_code == 201


# ---------------------------------------------------------------------------
# EMAIL ASSERTION: under DEMO_MODE the app refuses to boot if email send is on.
# The conftest safety net forces EMAIL_SEND_ENABLED off for every test, so the
# only way a refusal can fire here is the value the test sets on the module
# attribute at import time.
# ---------------------------------------------------------------------------

class TestEmailBootRefusal:
    def _import_raises_refusal(self, monkeypatch, tmp_path, demo_env, email_on):
        """True iff importing api_server under the given env raises the refusal.

        api_server reads DEMO_MODE from the env at import, and reads the email
        gate as `email_sender.EMAIL_SEND_ENABLED` (the module attribute) at
        import. We control both: DEMO_MODE via setenv, the email gate via a
        direct setattr on the email_sender module (robust against the conftest
        autouse safety net, which forces the attribute False — our setattr runs
        after it and wins). api_server is popped so it re-imports and re-runs
        its top-level assertion against the values we just set."""
        monkeypatch.delenv("DEMO_MODE", raising=False)
        if demo_env is not None:
            monkeypatch.setenv("DEMO_MODE", demo_env)
        import utils.email_sender as _es
        monkeypatch.setattr(_es, "EMAIL_SEND_ENABLED", bool(email_on))
        sys.modules.pop("api_server", None)
        try:
            _import_api_server_fresh(monkeypatch, tmp_path)
            return False   # imported without raising
        except RuntimeError as exc:
            return "email send must be disabled in DEMO_MODE" in str(exc)
        finally:
            # Restore email_sender to the conftest-safe OFF state for the rest of
            # the session, and drop api_server so no test reuses a bad copy.
            sys.modules.pop("api_server", None)
            monkeypatch.setattr(_es, "EMAIL_SEND_ENABLED", False)

    def test_demo_on_email_on_refuses_to_start(self, monkeypatch, tmp_path):
        assert self._import_raises_refusal(monkeypatch, tmp_path, "true", True) is True

    def test_demo_on_email_off_starts_fine(self, monkeypatch, tmp_path):
        assert self._import_raises_refusal(monkeypatch, tmp_path, "true", False) is False

    def test_demo_off_email_on_starts_fine(self, monkeypatch, tmp_path):
        # Normal ops: no DEMO_MODE -> the assertion never fires, even with email on.
        assert self._import_raises_refusal(monkeypatch, tmp_path, "", True) is False

    def test_demo_off_email_off_starts_fine(self, monkeypatch, tmp_path):
        assert self._import_raises_refusal(monkeypatch, tmp_path, "", False) is False
