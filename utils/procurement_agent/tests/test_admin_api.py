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


_DATA_ENDPOINTS = [
    "/api/admin/runs", "/api/admin/suppliers", "/api/admin/sent-messages",
    "/api/admin/review-queue", "/api/admin/orders", "/api/admin/prices",
]


def _seed(tmp_stores):
    """Seed one of each record across the isolated stores; returns ids/keys."""
    sr, orders, price_db, persistence = tmp_stores
    run = persistence.create_run(asset_specs={"manufacturer": "Baldor", "model": "EM3770T",
                                              "part_number": "EM3770T"})
    sr.upsert_contact("baypower.com", {"contact_email": "sales@baypower.com",
                                       "contact_method": "generic_inbox", "contact_status": "resolved"})
    sr.record_sent_message(run_id=run["id"], supplier_domain="baypower.com",
                           vendor_name="Bay Power", to=["sales@baypower.com"])
    sr.record_review_item("quote", {"unit_price": 85.0}, run_id=run["id"],
                          supplier_domain="baypower.com", vendor_name="Bay Power",
                          manufacturer="Baldor", part_number="EM3770T", confidence=0.9)
    price_db.save_price("Baldor", "EM3770T", "Bay Power", 1650.0, source="rfq")
    o = orders.create_order({"run_id": run["id"], "manufacturer": "Baldor",
                             "part_number": "EM3770T", "vendor_name": "Bay Power",
                             "unit_price": 1650.0})
    return run["id"], o["id"]


@pytest.fixture
def stores():
    """Hand back the (already-isolated) store modules for seeding inside a test."""
    from utils import supplier_registry, orders, price_db
    from utils.procurement_agent.state import persistence
    return supplier_registry, orders, price_db, persistence


class TestAdminDataEndpoints:
    def test_all_data_endpoints_require_admin(self, admin_api):
        for ep in _DATA_ENDPOINTS:
            assert admin_api.get(ep).status_code == 401, ep                      # no header
            assert admin_api.get(ep, headers=_auth("nope")).status_code == 403, ep  # non-admin

    def test_runs_list_and_detail(self, admin_api, stores):
        run_id, _ = _seed(stores)
        lst = admin_api.get("/api/admin/runs", headers=_auth(_TOKEN)).json()
        assert lst["count"] >= 1
        assert any(r["id"] == run_id and "Baldor" in (r["part"] or "") for r in lst["runs"])
        detail = admin_api.get(f"/api/admin/runs/{run_id}", headers=_auth(_TOKEN))
        assert detail.status_code == 200
        body = detail.json()
        assert body["id"] == run_id
        assert body["asset_specs_json"]["part_number"] == "EM3770T"   # full record
        # 404 for unknown run
        assert admin_api.get("/api/admin/runs/nope", headers=_auth(_TOKEN)).status_code == 404

    def test_suppliers_sent_review_orders_prices_shapes(self, admin_api, stores):
        _seed(stores)
        sup = admin_api.get("/api/admin/suppliers", headers=_auth(_TOKEN)).json()
        assert sup["count"] >= 1 and "needs_reenrichment" in sup["suppliers"][0]

        sent = admin_api.get("/api/admin/sent-messages", headers=_auth(_TOKEN)).json()
        assert sent["count"] == 1 and sent["sent_messages"][0]["vendor_name"] == "Bay Power"

        rq = admin_api.get("/api/admin/review-queue", headers=_auth(_TOKEN)).json()
        assert rq["count"] == 1 and rq["review_items"][0]["kind"] == "quote"
        assert rq["review_items"][0]["payload"]["unit_price"] == 85.0   # full record

        orders_resp = admin_api.get("/api/admin/orders", headers=_auth(_TOKEN)).json()
        assert orders_resp["count"] == 1 and orders_resp["orders"][0]["status"] == "draft"

        prices = admin_api.get("/api/admin/prices", headers=_auth(_TOKEN)).json()
        assert prices["count"] == 1
        assert prices["prices"][0]["vendor"] == "Bay Power"
        assert prices["prices"][0]["source"] == "rfq"

    def test_endpoints_are_read_only(self, admin_api, stores):
        sr, orders, price_db, _ = stores
        _seed(stores)
        before = (len(orders.get_orders()), len(sr.get_sent_messages()),
                  len(sr.get_review_items()), len(sr.all_entries()), len(price_db.all_entries()))
        for ep in _DATA_ENDPOINTS:
            assert admin_api.get(ep, headers=_auth(_TOKEN)).status_code == 200, ep
        after = (len(orders.get_orders()), len(sr.get_sent_messages()),
                 len(sr.get_review_items()), len(sr.all_entries()), len(price_db.all_entries()))
        assert before == after   # GETs mutated nothing


# ---------------------------------------------------------------------------
# Order lifecycle endpoints (execute / mark-delivered / list ; admin status/cancel)
# ---------------------------------------------------------------------------

_SPECS = {"manufacturer": "Baldor", "part_number": "EM3770T", "model": "EM3770T"}
_BUY_SELECTION = {"vendor_name": "Global Industrial", "base_price": 1799.0,
                  "source_url": "https://www.globalindustrial.com/p/x", "lead_time_days": 3}


def _make_approved_run(selection: dict) -> str:
    from utils.procurement_agent.state import persistence
    run = persistence.create_run(asset_specs=_SPECS)
    persistence.update_run(run["id"], {
        "selected_candidate_json": selection,
        "approval_history_json": [{"action": "approved", "approver_name": "Maintenance Director"}],
        "current_phase": "approved",
    })
    return run["id"]


class TestOrderEndpoints:
    def test_buyer_loop_end_to_end_execute_places_order(self, admin_api):
        """select -> approve -> execute -> a durable PLACED order (not a stub)."""
        run_id = _make_approved_run(_BUY_SELECTION)
        r = admin_api.post(f"/api/runs/{run_id}/execute")   # run-scoped, ungated
        assert r.status_code == 200
        body = r.json()
        assert "stub" not in body and body["placed"] is True
        assert body["order"]["status"] == "placed"
        assert body["order"]["unit_price"] == 1799.0
        assert body["order"]["placed_by"] == "Maintenance Director"

        # Persisted onto the run + retrievable via the run-scoped list.
        listed = admin_api.get(f"/api/runs/{run_id}/orders").json()
        assert listed["count"] == 1 and listed["orders"][0]["status"] == "placed"

    def test_execute_without_price_stays_draft(self, admin_api):
        run_id = _make_approved_run({"vendor_name": "Nobody", "price_tbd": True})
        body = admin_api.post(f"/api/runs/{run_id}/execute").json()
        assert body["placed"] is False and body["order"]["status"] == "draft"

    def test_execute_unknown_run_404(self, admin_api):
        assert admin_api.post("/api/runs/nope/execute").status_code == 404

    def test_admin_status_update_enforces_machine(self, admin_api):
        run_id = _make_approved_run(_BUY_SELECTION)
        oid = admin_api.post(f"/api/runs/{run_id}/execute").json()["order"]["id"]

        # legal placed -> confirmed
        r = admin_api.post(f"/api/admin/orders/{oid}/status",
                           json={"status": "confirmed"}, headers=_auth(_TOKEN))
        assert r.status_code == 200 and r.json()["status"] == "confirmed"
        # illegal confirmed -> received (skip shipped)
        r = admin_api.post(f"/api/admin/orders/{oid}/status",
                           json={"status": "received"}, headers=_auth(_TOKEN))
        assert r.status_code == 409

    def test_admin_status_and_cancel_are_gated(self, admin_api):
        run_id = _make_approved_run(_BUY_SELECTION)
        oid = admin_api.post(f"/api/runs/{run_id}/execute").json()["order"]["id"]
        # no token / wrong token rejected at the API
        assert admin_api.post(f"/api/admin/orders/{oid}/status",
                              json={"status": "confirmed"}).status_code == 401
        assert admin_api.post(f"/api/admin/orders/{oid}/cancel", json={},
                              headers=_auth("nope")).status_code == 403

    def test_admin_cancel_then_blocks_recancel(self, admin_api):
        run_id = _make_approved_run(_BUY_SELECTION)
        oid = admin_api.post(f"/api/runs/{run_id}/execute").json()["order"]["id"]
        r = admin_api.post(f"/api/admin/orders/{oid}/cancel",
                           json={"reason": "supplier backordered"}, headers=_auth(_TOKEN))
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        # cancelled is terminal -> further transition rejected
        r2 = admin_api.post(f"/api/admin/orders/{oid}/status",
                            json={"status": "confirmed"}, headers=_auth(_TOKEN))
        assert r2.status_code == 409

    def test_mark_delivered_through_machine(self, admin_api):
        run_id = _make_approved_run(_BUY_SELECTION)
        oid = admin_api.post(f"/api/runs/{run_id}/execute").json()["order"]["id"]
        for s in ("confirmed", "shipped"):
            admin_api.post(f"/api/admin/orders/{oid}/status",
                           json={"status": s}, headers=_auth(_TOKEN))
        r = admin_api.post(f"/api/runs/{run_id}/mark-delivered")
        assert r.status_code == 200 and r.json()["order"]["status"] == "received"


# ---------------------------------------------------------------------------
# Buyer-loop endpoints (customer-facing, UNGATED like execute/select): run-scoped
# review-items read, confirm/reject (the ONLY UI path that writes price_db), and
# process-replies (a live Gmail READ, fail-soft without creds; never a send).
# ---------------------------------------------------------------------------

def _seed_quote_item(stores, *, status="pending"):
    sr, _o, _p, persistence = stores
    run = persistence.create_run(asset_specs=_SPECS)
    sr.record_sent_message(run_id=run["id"], supplier_domain="acme.com",
                           vendor_name="Acme Motor Supply", to=["sales@acme.com"], status="sent")
    item_id = sr.record_review_item(
        "quote",
        {"unit_price": 1210.0, "currency": "USD", "quantity": 1,
         "lead_time": "5 business days", "terms": "Net 30"},
        status=status, run_id=run["id"], supplier_domain="acme.com",
        vendor_name="Acme Motor Supply", manufacturer="Baldor", part_number="EM3770T",
        confidence=0.9,
    )
    return run["id"], item_id


class TestBuyerLoopEndpoints:
    def test_review_items_run_scoped_read(self, admin_api, stores):
        run_id, item_id = _seed_quote_item(stores)
        r = admin_api.get(f"/api/runs/{run_id}/review-items")   # ungated
        assert r.status_code == 200
        body = r.json()
        assert body["run_id"] == run_id
        assert body["sent_count"] == 1 and body["quote_count"] == 1
        assert len(body["review_items"]) == 1
        item = body["review_items"][0]
        assert item["id"] == item_id and item["kind"] == "quote"
        assert item["payload"]["unit_price"] == 1210.0
        assert item["vendor_name"] == "Acme Motor Supply"

    def test_review_items_unknown_run_empty(self, admin_api):
        body = admin_api.get("/api/runs/nope/review-items").json()
        assert body["review_items"] == [] and body["sent_count"] == 0

    def test_confirm_quote_writes_price_db(self, admin_api, stores):
        sr, _o, price_db, _p = stores
        run_id, item_id = _seed_quote_item(stores)
        assert price_db.all_entries() == {}                     # nothing applied yet

        r = admin_api.post(f"/api/review-items/{item_id}/confirm")
        assert r.status_code == 200
        body = r.json()
        assert body["confirmed"] is True and body["kind"] == "quote"
        assert body["item"]["status"] == "confirmed"
        # confirm is the ONLY UI path that writes price_db.
        cached = price_db.get_cached_prices("Baldor", "EM3770T")
        assert cached["Acme Motor Supply"]["price"] == 1210.0
        assert cached["Acme Motor Supply"]["source"] == "rfq"

    def test_confirm_contact_upserts_primary(self, admin_api, stores):
        sr, _o, _p, persistence = stores
        run = persistence.create_run(asset_specs=_SPECS)
        item_id = sr.record_review_item(
            "contact", {"email": "jane.smith@acme.com", "name": "Jane Smith",
                        "position": "Purchasing Manager"},
            run_id=run["id"], supplier_domain="acme.com", vendor_name="Acme Motor Supply",
        )
        r = admin_api.post(f"/api/review-items/{item_id}/confirm")
        assert r.status_code == 200 and r.json()["confirmed"] is True
        rec = sr.lookup_by_domain("acme.com")
        assert rec["primary_contact_email"] == "jane.smith@acme.com"
        assert rec["primary_contact_status"] == "resolved"

    def test_reject_discards_without_price_write(self, admin_api, stores):
        sr, _o, price_db, _p = stores
        run_id, item_id = _seed_quote_item(stores)
        r = admin_api.post(f"/api/review-items/{item_id}/reject")
        assert r.status_code == 200 and r.json()["rejected"] is True
        assert sr.get_review_item(item_id)["status"] == "rejected"
        assert price_db.all_entries() == {}                     # reject writes nothing

    def test_confirm_unknown_item_404(self, admin_api):
        assert admin_api.post("/api/review-items/nope/confirm").status_code == 404
        assert admin_api.post("/api/review-items/nope/reject").status_code == 404

    def test_process_replies_unavailable_without_creds(self, admin_api, stores):
        # Conftest forces EMAIL_SEND_ENABLED off + clears GMAIL_* -> reader not configured
        # -> fail-soft "unavailable", ZERO live calls (no reader.fetch ever runs).
        run = stores[3].create_run(asset_specs=_SPECS)
        r = admin_api.post(f"/api/runs/{run['id']}/process-replies")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False and body["summary"] is None

    def test_process_replies_happy_path_reader_mocked(self, admin_api, stores, monkeypatch):
        # Configured path, but process_replies is STUBBED so no real Gmail call happens.
        import utils.email_sender as es
        from utils import reply_processor
        run_id, _item = _seed_quote_item(stores)
        monkeypatch.setattr(es, "EMAIL_SEND_ENABLED", True)
        monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_FILE", "/fake/key.json")  # -> reader.configured
        canned = {"processed": 2, "queued_quotes": 1, "queued_contacts": 0,
                  "needs_review": 0, "unmatched": ["x@y.com"]}
        monkeypatch.setattr(reply_processor, "process_replies", lambda reader=None, **k: canned)

        r = admin_api.post(f"/api/runs/{run_id}/process-replies")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["summary"] == canned
        assert body["queued_for_run"] == 1          # the seeded pending quote

    def test_process_replies_unknown_run_404(self, admin_api):
        assert admin_api.post("/api/runs/nope/process-replies").status_code == 404
