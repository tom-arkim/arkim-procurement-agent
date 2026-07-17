"""
Night 11 T3 — the quote-submission API + promotion wiring (QUOTE_SUBMIT_V1).

Covers (QUOTE_SUBMISSION_SPEC.md §2/§3/§6, brief T3):
  - FLAG-OFF PARITY: every quote route absent — 404 byte-identical to an
    unknown route, for public AND admin paths (admin flag check precedes auth),
  - path A (token): form context privacy contract, closed-state honesty,
    uniform 404 on unknown tokens, submission → active quote, claim pitch
    (unclaimed only), single-RFQ scope, revision supersede,
  - path B (portal): claim-token-auth'd, open-RFQ required, provenance,
  - path C (admin): concierge entry + the review queue (list/approve/reject),
    full require_admin contract when the flag is on,
  - sanity checks through the API (price band from price_db; qty; pn_differs),
  - PROMOTION WIRING: an active structured quote promotes the supplier's card
    to Band A on the live run via the EXISTING T4 seam (no fork),
  - NO-AUTO-ORDER property: no quote endpoint reaches order placement,
  - cross-token isolation at the route level (claim token ≠ quote token).

Fixture mirrors test_api_server's `api` + test_admin_api's stores isolation.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

_ADMIN_TOKEN = "test-admin-secret-123"

_GUSHER_SPECS = {"manufacturer": "Gusher Pumps", "part_number": "84004-28",
                 "quantity": 2}


@pytest.fixture
def qapi(tmp_path, monkeypatch):
    """TestClient with QUOTE_SUBMIT_V1 ON and every store isolated to tmp."""
    from utils.procurement_agent.state import persistence

    engine = persistence._make_engine(f"sqlite:///{tmp_path / 'api.sqlite'}")
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    persistence.Base.metadata.create_all(engine)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr(persistence, "_engine", engine)
    monkeypatch.setattr(persistence, "_SessionFactory", TestSession)

    from utils import supplier_registry, price_db, quote_store, quote_tokens
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH",
                        str(tmp_path / "supplier_registry.sqlite"))
    monkeypatch.setattr(price_db, "_DB_PATH", str(tmp_path / "price_db.json"))
    monkeypatch.setattr(quote_store, "_DB_PATH", str(tmp_path / "quotes.sqlite"))
    monkeypatch.setattr(quote_tokens, "_DB_PATH",
                        str(tmp_path / "quote_tokens.sqlite"))

    monkeypatch.setenv("ARKIM_ADMIN_TOKEN", _ADMIN_TOKEN)
    monkeypatch.setenv("QUOTE_SUBMIT_V1", "1")

    import api_server
    monkeypatch.setattr(api_server, "_engine", engine)
    monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
    monkeypatch.setattr(api_server, "_messages", {})
    monkeypatch.setattr(api_server, "DEMO_MODE", False)
    monkeypatch.setattr(api_server, "_quote_rate_buckets", {})  # fresh limiter

    client = TestClient(api_server.app)
    client._api_server = api_server
    return client


def _auth(token: str = _ADMIN_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_run(client, specs=None) -> str:
    resp = client.post("/api/runs", json={})
    assert resp.status_code == 201
    rid = resp.json()["id"]
    SF = client._api_server._SessionFactory
    ORM = client._api_server.SourcingRunORM
    with SF() as session:
        run = session.get(ORM, rid)
        run.asset_specs_json = json.dumps(specs or _GUSHER_SPECS)
        session.commit()
    return rid


def _mint_token(run_id: str, **overrides) -> dict:
    from utils import quote_tokens
    kwargs = dict(
        supplier_domain="dxpe.com", vendor_name="DXP Enterprises",
        run_id=run_id, rfq_id="rfq-9",
        part_key="gusher pumps|84004-28",
        manufacturer="Gusher Pumps", part_number="84004-28",
        quantity=2, need_by="2026-08-01",
    )
    kwargs.update(overrides)
    out = quote_tokens.mint_for_rfq(**kwargs)
    assert out is not None
    return out


def _valid_body(**overrides) -> dict:
    body = {"quote_number": "DXP-0091", "unit_price": 189.0, "quantity": 2,
            "lead_time": "3 days"}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Flag-off parity — endpoints ABSENT, byte-identical 404 (criterion 9)
# ---------------------------------------------------------------------------

class TestFlagOffParity:
    def _assert_absent(self, client, method: str, path: str, **kw):
        unknown = client.get("/api/definitely-not-a-route")
        resp = getattr(client, method)(path, **kw)
        assert resp.status_code == 404
        assert resp.content == unknown.content  # byte-identical body

    def test_public_routes_absent(self, qapi, monkeypatch):
        rid = _make_run(qapi)
        tok = _mint_token(rid)  # minted while ON...
        monkeypatch.setenv("QUOTE_SUBMIT_V1", "")
        self._assert_absent(qapi, "get", f"/api/quote/{tok['token']}")
        self._assert_absent(qapi, "post", f"/api/quote/{tok['token']}",
                            json=_valid_body())

    def test_portal_quote_route_absent_even_with_portal_on(self, qapi,
                                                           monkeypatch):
        monkeypatch.setenv("SUPPLIER_PORTAL_V1", "1")
        monkeypatch.setenv("QUOTE_SUBMIT_V1", "")
        self._assert_absent(qapi, "post", "/api/portal/any-token/quotes",
                            json={**_valid_body(), "run_id": "run-1"})

    def test_admin_routes_absent_regardless_of_auth(self, qapi, monkeypatch):
        monkeypatch.setenv("QUOTE_SUBMIT_V1", "")
        for headers in ({}, _auth(), _auth("wrong-token")):
            self._assert_absent(qapi, "get", "/api/admin/quotes",
                                headers=headers)
            self._assert_absent(qapi, "post", "/api/admin/quotes/q1/approve",
                                headers=headers)
            self._assert_absent(qapi, "post", "/api/admin/quotes/q1/reject",
                                headers=headers)
        self._assert_absent(qapi, "post", "/api/admin/quotes", headers=_auth(),
                            json={**_valid_body(), "run_id": "r",
                                  "supplier_domain": "dxpe.com"})

    def test_flag_off_run_detail_has_no_quote_overlay(self, qapi, monkeypatch):
        # The index merge is skipped flag-off: an active structured quote
        # written while ON must not surface once the flag is off.
        rid = _make_run(qapi)
        from utils import quote_store
        quote_store.submit_quote(
            supplier_domain="dxpe.com", unit_price=189.0,
            submitted_via="concierge", run_id=rid)
        monkeypatch.setenv("QUOTE_SUBMIT_V1", "")
        idx = qapi._api_server._build_quote_index(rid)
        assert idx["by_domain"] == {}


# ---------------------------------------------------------------------------
# Token security at the route (criterion 7)
# ---------------------------------------------------------------------------

class TestTokenSecurity:
    def test_unknown_token_uniform_404(self, qapi):
        unknown = qapi.get("/api/definitely-not-a-route")
        for resp in (qapi.get("/api/quote/garbage-token-1"),
                     qapi.post("/api/quote/garbage-token-2",
                               json=_valid_body())):
            assert resp.status_code == 404
            assert resp.content == unknown.content

    def test_closed_rfq_renders_closed_state_not_error(self, qapi):
        rid = _make_run(qapi)
        tok = _mint_token(rid, expiry_days=0)  # already at/past the window
        resp = qapi.get(f"/api/quote/{tok['token']}")
        assert resp.status_code == 200
        assert resp.json() == {"state": "closed"}

    def test_closed_rfq_rejects_submission(self, qapi):
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        from utils import quote_tokens
        quote_tokens.revoke(tok["token_id"], reason="rfq_withdrawn")
        resp = qapi.post(f"/api/quote/{tok['token']}", json=_valid_body())
        assert resp.status_code == 409
        # ...and nothing was written.
        from utils import quote_store
        assert quote_store.get_quotes(run_id=rid) == []

    def test_claim_token_never_opens_the_quote_form(self, qapi, monkeypatch,
                                                    tmp_path):
        from utils import claim_tokens as ct
        monkeypatch.setenv("SUPPLIER_PORTAL_V1", "1")
        monkeypatch.setattr(ct, "CLAIM_TOKENS_ENABLED", True)
        monkeypatch.setattr(ct, "_DB_PATH", str(tmp_path / "claim_tokens.sqlite"))
        claim = ct.generate_for("dxpe.com")
        unknown = qapi.get("/api/definitely-not-a-route")
        resp = qapi.get(f"/api/quote/{claim['token']}")
        assert resp.status_code == 404
        assert resp.content == unknown.content

    def test_security_headers_on_quote_responses(self, qapi):
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        resp = qapi.get(f"/api/quote/{tok['token']}")
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        assert resp.headers["Cache-Control"] == "no-store"

    def test_rate_limit_throttles_token_spray(self, qapi):
        cap = qapi._api_server._QUOTE_RATE_CAP_PER_BUCKET
        token = "sprayed-token-fixed-prefix"
        last = None
        for _ in range(cap + 1):
            last = qapi.get(f"/api/quote/{token}")
        assert last.status_code == 429
        assert last.headers.get("Retry-After")

    def test_token_scoped_to_its_rfq(self, qapi):
        # A token can only quote THE request it was minted for: the run
        # binding comes from the token row, never from the caller.
        rid1, rid2 = _make_run(qapi), _make_run(qapi)
        tok1 = _mint_token(rid1)
        resp = qapi.post(f"/api/quote/{tok1['token']}", json=_valid_body())
        assert resp.status_code == 200
        from utils import quote_store
        assert len(quote_store.get_quotes(run_id=rid1)) == 1
        assert quote_store.get_quotes(run_id=rid2) == []


# ---------------------------------------------------------------------------
# Path A — form context + submission (criterion 1)
# ---------------------------------------------------------------------------

class TestPathAFormContext:
    def test_live_context_shape_and_privacy(self, qapi):
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        body = qapi.get(f"/api/quote/{tok['token']}").json()
        # Exact top-level contract — nothing beyond the spec §3 exposure list
        # (no buyer identity, no other suppliers, no pricing signals).
        assert set(body) == {"state", "request", "supplier", "expires_at",
                             "existing_quote"}
        assert body["state"] == "live"
        assert body["request"] == {"manufacturer": "Gusher Pumps",
                                   "part_number": "84004-28",
                                   "quantity": 2, "need_by": "2026-08-01"}
        assert body["supplier"] == {"name": "DXP Enterprises",
                                    "domain": "dxpe.com"}
        assert body["existing_quote"] is None

    def test_existing_quote_surfaces_for_revision(self, qapi):
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        qapi.post(f"/api/quote/{tok['token']}", json=_valid_body())
        body = qapi.get(f"/api/quote/{tok['token']}").json()
        assert body["existing_quote"]["status"] == "active"
        assert body["existing_quote"]["unit_price"] == 189.0


class TestPathASubmission:
    def test_clean_submission_activates_no_account_needed(self, qapi):
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        resp = qapi.post(f"/api/quote/{tok['token']}", json=_valid_body())
        assert resp.status_code == 200
        out = resp.json()
        assert out["ok"] is True and out["status"] == "active"
        assert out["review_reasons"] == []
        from utils import quote_store
        (q,) = quote_store.get_quotes(run_id=rid)
        assert q["submitted_via"] == "rfq_link"
        assert q["submitted_by"] == tok["token_id"]
        assert q["quote_number"] == "DXP-0091"

    def test_claim_pitch_renders_for_unclaimed_only(self, qapi):
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        out = qapi.post(f"/api/quote/{tok['token']}", json=_valid_body()).json()
        assert out["claim_pitch"] is True  # unknown supplier ⇒ unclaimed

    def test_claim_pitch_suppressed_for_onboarded_supplier(self, qapi):
        from utils import supplier_registry
        supplier_registry.create_stub("DXP Enterprises", domain="dxpe.com")
        supplier_registry.update_supplier(
            "DXP Enterprises", onboarding_status="onboarded_arkim_supplier")
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        out = qapi.post(f"/api/quote/{tok['token']}", json=_valid_body()).json()
        assert out["claim_pitch"] is False

    def test_validation_422s(self, qapi):
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        url = f"/api/quote/{tok['token']}"
        assert qapi.post(url, json=_valid_body(unit_price=0)).status_code == 422
        assert qapi.post(url, json=_valid_body(quantity=-1)).status_code == 422
        assert qapi.post(url, json=_valid_body(quote_number="  ")).status_code == 422
        assert qapi.post(url, json=_valid_body(lead_time="")).status_code == 422
        missing = _valid_body()
        del missing["unit_price"]
        assert qapi.post(url, json=missing).status_code == 422
        from utils import quote_store
        assert quote_store.get_quotes(run_id=rid) == []  # nothing written

    def test_revision_supersedes_prior(self, qapi):
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        qapi.post(f"/api/quote/{tok['token']}", json=_valid_body())
        qapi.post(f"/api/quote/{tok['token']}",
                  json=_valid_body(unit_price=175.0))
        from utils import quote_store
        active = quote_store.get_active_quotes(rid)
        assert len(active) == 1 and active[0]["unit_price"] == 175.0
        assert len(quote_store.get_quotes(run_id=rid)) == 2  # history kept


# ---------------------------------------------------------------------------
# Sanity checks through the API (criteria 4/5)
# ---------------------------------------------------------------------------

class TestSanityThroughApi:
    def test_edited_pn_lands_in_review_not_active(self, qapi):
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        out = qapi.post(f"/api/quote/{tok['token']}",
                        json=_valid_body(part_number="84004-28SP")).json()
        assert out["status"] == "review"
        assert out["pn_differs"] is True
        assert "pn_differs" in out["review_reasons"]
        from utils import quote_store
        assert quote_store.get_active_quotes(rid) == []  # never promotes raw

    def test_100x_price_lands_in_review_via_price_db_band(self, qapi):
        from utils import price_db
        for vendor, price in (("A", 50.0), ("B", 53.25), ("C", 60.0)):
            price_db.save_price("Gusher Pumps", "84004-28", vendor, price)
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        out = qapi.post(f"/api/quote/{tok['token']}",
                        json=_valid_body(unit_price=5325.0)).json()
        assert out["status"] == "review"
        assert "price_out_of_band" in out["review_reasons"]

    def test_band_absent_skips_price_check(self, qapi):
        rid = _make_run(qapi)  # price_db empty for this part
        tok = _mint_token(rid)
        out = qapi.post(f"/api/quote/{tok['token']}",
                        json=_valid_body(unit_price=5325.0)).json()
        assert out["status"] == "active"

    def test_wild_qty_lands_in_review(self, qapi):
        rid = _make_run(qapi)
        tok = _mint_token(rid)  # requested qty 2 on the token
        out = qapi.post(f"/api/quote/{tok['token']}",
                        json=_valid_body(quantity=500)).json()
        assert out["status"] == "review"
        assert "qty_out_of_band" in out["review_reasons"]


# ---------------------------------------------------------------------------
# Path B — portal submit (criterion 2 wiring; the page itself is T5)
# ---------------------------------------------------------------------------

class TestPathBPortal:
    def _portal_setup(self, qapi, monkeypatch, tmp_path):
        from utils import claim_tokens as ct, supplier_registry
        monkeypatch.setenv("SUPPLIER_PORTAL_V1", "1")
        monkeypatch.setattr(ct, "CLAIM_TOKENS_ENABLED", True)
        monkeypatch.setattr(ct, "_DB_PATH", str(tmp_path / "claim_tokens.sqlite"))
        supplier_registry.create_stub("DXP Enterprises", domain="dxpe.com")
        rid = _make_run(qapi)
        supplier_registry.record_sent_message(
            run_id=rid, supplier_domain="dxpe.com",
            vendor_name="DXP Enterprises", to=["sales@dxpe.com"],
            status="stubbed", part_key="gusher pumps|84004-28")
        claim = ct.generate_for("dxpe.com")
        return rid, claim

    def test_claimed_supplier_quotes_their_open_request(self, qapi,
                                                        monkeypatch, tmp_path):
        rid, claim = self._portal_setup(qapi, monkeypatch, tmp_path)
        resp = qapi.post(f"/api/portal/{claim['token']}/quotes",
                         json={**_valid_body(), "run_id": rid})
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"
        from utils import quote_store
        (q,) = quote_store.get_quotes(run_id=rid)
        assert q["submitted_via"] == "portal"
        assert q["supplier_domain"] == "dxpe.com"
        assert q["requested_part_number"] == "84004-28"  # from the run specs

    def test_no_open_rfq_no_quote(self, qapi, monkeypatch, tmp_path):
        rid, claim = self._portal_setup(qapi, monkeypatch, tmp_path)
        other = _make_run(qapi)  # no RFQ sent to dxpe.com on this run
        resp = qapi.post(f"/api/portal/{claim['token']}/quotes",
                         json={**_valid_body(), "run_id": other})
        assert resp.status_code == 404

    def test_quote_token_never_opens_the_portal_submit(self, qapi, monkeypatch,
                                                       tmp_path):
        rid, claim = self._portal_setup(qapi, monkeypatch, tmp_path)
        qtok = _mint_token(rid)
        resp = qapi.post(f"/api/portal/{qtok['token']}/quotes",
                         json={**_valid_body(), "run_id": rid})
        assert resp.status_code == 404  # uniform portal rejection


# ---------------------------------------------------------------------------
# Path B — T5: open requests + quote history (own-only visibility)
# ---------------------------------------------------------------------------

class TestPortalOpenRequestsAndHistory:
    def _setup(self, qapi, monkeypatch, tmp_path):
        from utils import claim_tokens as ct, supplier_registry
        monkeypatch.setenv("SUPPLIER_PORTAL_V1", "1")
        monkeypatch.setattr(ct, "CLAIM_TOKENS_ENABLED", True)
        monkeypatch.setattr(ct, "_DB_PATH", str(tmp_path / "claim_tokens.sqlite"))
        rid = _make_run(qapi)
        supplier_registry.record_sent_message(
            run_id=rid, supplier_domain="dxpe.com",
            vendor_name="DXP Enterprises", to=["sales@dxpe.com"],
            status="stubbed", part_key="gusher pumps|84004-28")
        claim = ct.generate_for("dxpe.com")
        return rid, claim

    def test_open_requests_lists_the_suppliers_own_rfqs_only(self, qapi,
                                                             monkeypatch,
                                                             tmp_path):
        rid, claim = self._setup(qapi, monkeypatch, tmp_path)
        from utils import supplier_registry
        # Another supplier's RFQ on another run must NOT appear.
        other = _make_run(qapi)
        supplier_registry.record_sent_message(
            run_id=other, supplier_domain="sealit.example", vendor_name="Seal It",
            to=["sales@sealit.example"], status="stubbed")
        body = qapi.get(f"/api/portal/{claim['token']}/open-requests").json()
        assert [r["run_id"] for r in body["requests"]] == [rid]
        (row,) = body["requests"]
        assert row["manufacturer"] == "Gusher Pumps"
        assert row["part_number"] == "84004-28"
        assert row["quantity"] == 2
        assert row["quoted"] is None

    def test_resolved_rfq_statuses_are_not_open(self, qapi, monkeypatch,
                                                tmp_path):
        rid, claim = self._setup(qapi, monkeypatch, tmp_path)
        from utils import supplier_registry
        replied_run = _make_run(qapi)
        mid = supplier_registry.record_sent_message(
            run_id=replied_run, supplier_domain="dxpe.com", vendor_name="DXP",
            to=["sales@dxpe.com"], status="stubbed")
        supplier_registry.update_sent_message_status(mid, "replied")
        body = qapi.get(f"/api/portal/{claim['token']}/open-requests").json()
        assert [r["run_id"] for r in body["requests"]] == [rid]

    def test_quoted_marker_appears_after_submit(self, qapi, monkeypatch,
                                                tmp_path):
        rid, claim = self._setup(qapi, monkeypatch, tmp_path)
        assert qapi.post(f"/api/portal/{claim['token']}/quotes",
                         json={**_valid_body(), "run_id": rid}).status_code == 200
        body = qapi.get(f"/api/portal/{claim['token']}/open-requests").json()
        (row,) = body["requests"]
        assert row["quoted"] == {"status": "active", "unit_price": 189.0,
                                 "submitted_at": row["quoted"]["submitted_at"]}

    def test_history_is_own_quotes_only_with_honest_status(self, qapi,
                                                           monkeypatch,
                                                           tmp_path):
        rid, claim = self._setup(qapi, monkeypatch, tmp_path)
        qapi.post(f"/api/portal/{claim['token']}/quotes",
                  json={**_valid_body(), "run_id": rid})
        # Another supplier's quote on the same run must NOT appear.
        from utils import quote_store
        quote_store.submit_quote(supplier_domain="sealit.example",
                                 unit_price=53.25, submitted_via="concierge",
                                 run_id=rid)
        body = qapi.get(f"/api/portal/{claim['token']}/quotes").json()
        (q,) = body["quotes"]
        assert q["unit_price"] == 189.0
        assert q["status"] == "active"
        assert q["submitted_via"] == "portal"
        # No cross-supplier fields leak (no domain of others, no buyer data).
        assert "supplier_domain" not in q

    def test_t5_routes_absent_when_quote_flag_off(self, qapi, monkeypatch,
                                                  tmp_path):
        rid, claim = self._setup(qapi, monkeypatch, tmp_path)
        monkeypatch.setenv("QUOTE_SUBMIT_V1", "")
        unknown = qapi.get("/api/definitely-not-a-route")
        for path in (f"/api/portal/{claim['token']}/open-requests",
                     f"/api/portal/{claim['token']}/quotes"):
            resp = qapi.get(path)
            assert resp.status_code == 404
            assert resp.content == unknown.content

    def test_t5_routes_reject_bad_claim_token(self, qapi, monkeypatch,
                                              tmp_path):
        self._setup(qapi, monkeypatch, tmp_path)
        unknown = qapi.get("/api/definitely-not-a-route")
        resp = qapi.get("/api/portal/garbage-claim-token/open-requests")
        assert resp.status_code == 404
        assert resp.content == unknown.content


# ---------------------------------------------------------------------------
# Path C — concierge entry + review queue (criterion 3 wiring)
# ---------------------------------------------------------------------------

class TestPathCAdmin:
    def test_concierge_entry_records_identical_promotion_record(self, qapi):
        rid = _make_run(qapi)
        resp = qapi.post("/api/admin/quotes", headers=_auth(),
                         json={**_valid_body(), "run_id": rid,
                               "supplier_domain": "dxpe.com",
                               "vendor_name": "DXP Enterprises"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"
        from utils import quote_store
        (q,) = quote_store.get_active_quotes(rid)
        assert q["submitted_via"] == "concierge"
        assert q["submitted_by"] == "admin"
        rec = quote_store.as_confirmation_record(q)
        assert rec["status"] == "confirmed"
        assert rec["payload"]["unit_price"] == 189.0

    def test_unknown_run_404(self, qapi):
        resp = qapi.post("/api/admin/quotes", headers=_auth(),
                         json={**_valid_body(), "run_id": "no-such-run",
                               "supplier_domain": "dxpe.com"})
        assert resp.status_code == 404

    def test_admin_auth_contract_when_flag_on(self, qapi, monkeypatch):
        assert qapi.get("/api/admin/quotes").status_code == 401
        assert qapi.get("/api/admin/quotes",
                        headers=_auth("wrong")).status_code == 403
        monkeypatch.delenv("ARKIM_ADMIN_TOKEN", raising=False)
        assert qapi.get("/api/admin/quotes",
                        headers=_auth()).status_code == 503  # fail-closed

    def test_review_queue_list_approve_promotes(self, qapi):
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        out = qapi.post(f"/api/quote/{tok['token']}",
                        json=_valid_body(part_number="84004-28SP")).json()
        queue = qapi.get("/api/admin/quotes?status=review",
                         headers=_auth()).json()["quotes"]
        assert [q["id"] for q in queue] == [out["quote_id"]]
        assert queue[0]["review_reasons"] == ["pn_differs"]
        appr = qapi.post(f"/api/admin/quotes/{out['quote_id']}/approve",
                         headers=_auth())
        assert appr.status_code == 200
        quote = appr.json()["quote"]
        assert quote["status"] == "active"
        # The quoted PN stays the quoted PN (criterion 4 labelling substrate).
        assert quote["quoted_part_number"] == "84004-28SP"
        assert quote["pn_differs"] is True
        from utils import quote_store
        assert len(quote_store.get_active_quotes(rid)) == 1

    def test_review_reject_withdraws(self, qapi):
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        out = qapi.post(f"/api/quote/{tok['token']}",
                        json=_valid_body(part_number="OTHER")).json()
        rej = qapi.post(f"/api/admin/quotes/{out['quote_id']}/reject",
                        headers=_auth())
        assert rej.status_code == 200
        assert rej.json()["quote"]["status"] == "withdrawn"
        from utils import quote_store
        assert quote_store.get_active_quotes(rid) == []

    def test_approve_guards(self, qapi):
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        out = qapi.post(f"/api/quote/{tok['token']}", json=_valid_body()).json()
        assert qapi.post(f"/api/admin/quotes/{out['quote_id']}/approve",
                         headers=_auth()).status_code == 409  # already active
        assert qapi.post("/api/admin/quotes/no-such-id/approve",
                         headers=_auth()).status_code == 404


# ---------------------------------------------------------------------------
# Promotion wiring — the live-run Band-A jump via the EXISTING T4 seam
# ---------------------------------------------------------------------------

def _banded_run_with_dxp_outreach(qapi, monkeypatch) -> str:
    """A persisted banded run whose tier_1 holds DXP as a Band-C onboarded
    class match (the outreach block) — the exact live shape T4 promotes from
    (mirrors test_api_server.TestBandedTier1ReDeriveOrdering)."""
    from unittest.mock import Mock
    from utils.procurement_agent import tier1_matcher
    from utils import known_parts
    import utils.procurement_agent.agents.sourcing_agent as sa_mod
    import utils.procurement_agent.agents.spec_comparison_agent as sca_mod

    dxp = {
        "vendor_name": "DXP Enterprises", "source_url": "https://dxpe.com",
        "found_part_number": None, "price_tbd": True, "base_price": 0.0,
        "suitability_score": 92.0, "merchant_type": "Arkim Network",
        "is_registry_backed": True,
    }
    monkeypatch.setattr(tier1_matcher, "tier1_v2_active", lambda: True)
    monkeypatch.setattr(tier1_matcher, "match_tier1", lambda **kw: ["match"])
    monkeypatch.setattr(tier1_matcher, "candidates_from_matches",
                        lambda matches, **kw: [dict(dxp)])
    monkeypatch.setattr(known_parts, "get_edges", lambda key: [])
    monkeypatch.setattr(known_parts, "upsert_edges", lambda key, cands: 0)

    result = {
        "tier_1": {"results": [], "count": 0},
        "tier_2": {"results": [{
            "vendor_name": "Seal It", "found_part_number": "84004-28",
            "source_url": "https://sealit.example/gusher/84004-28",
            "base_price": 53.25, "price_tbd": False,
            "suitability_score": 80.0, "confidence_score": 70.0,
        }], "count": 1},
        "tier_3": {"results": [], "count": 0},
        "filters_applied": ["ranking_bands:v1"],
    }
    sourcing_agent = Mock()
    sourcing_agent.run.return_value = result
    monkeypatch.setattr(sa_mod, "SourcingAgent", Mock(return_value=sourcing_agent))
    comp_agent = Mock()
    comp_agent.run.return_value = None
    monkeypatch.setattr(sca_mod, "SpecComparisonAgent",
                        Mock(return_value=comp_agent))

    rid = _make_run(qapi)
    assert qapi.post(f"/api/runs/{rid}/confirm-intake").status_code == 200
    return rid


class TestPromotionWiring:
    def test_structured_quote_promotes_dxp_to_band_a_on_the_live_run(
            self, qapi, monkeypatch):
        rid = _banded_run_with_dxp_outreach(qapi, monkeypatch)
        # Before the quote: DXP is an outreach target, not a finding.
        sr = qapi.get(f"/api/runs/{rid}").json()["sourcing_results"]
        assert "DXP Enterprises" in [
            s["vendorName"] for s in sr["outreachTargets"]["suppliers"]]
        # The supplier submits through the REAL public endpoint...
        tok = _mint_token(rid)
        assert qapi.post(f"/api/quote/{tok['token']}",
                         json=_valid_body()).status_code == 200
        # ...and jumps to the TOP of Band A on the next read (onboarded-first).
        sr = qapi.get(f"/api/runs/{rid}").json()["sourcing_results"]
        top = sr["findings"][0]
        assert top["vendorName"] == "DXP Enterprises"
        assert top["band"] == "A"
        assert top["price"] == 189.0
        assert top["supplierConfirmed"] is True
        assert "DXP Enterprises" not in [
            s["vendorName"] for s in sr["outreachTargets"]["suppliers"]]

    def test_review_flagged_quote_does_not_promote(self, qapi, monkeypatch):
        rid = _banded_run_with_dxp_outreach(qapi, monkeypatch)
        tok = _mint_token(rid)
        qapi.post(f"/api/quote/{tok['token']}",
                  json=_valid_body(part_number="OTHER-PN"))
        sr = qapi.get(f"/api/runs/{rid}").json()["sourcing_results"]
        assert "DXP Enterprises" not in [f["vendorName"] for f in sr["findings"]]

    def test_expired_quote_demotes_back_to_outreach(self, qapi, monkeypatch):
        rid = _banded_run_with_dxp_outreach(qapi, monkeypatch)
        tok = _mint_token(rid)
        out = qapi.post(f"/api/quote/{tok['token']}", json=_valid_body()).json()
        # Force the quote past its validity window (read-time expiry).
        import sqlite3
        from utils import quote_store
        conn = sqlite3.connect(quote_store._DB_PATH)
        conn.execute("UPDATE quotes SET valid_until = ? WHERE id = ?",
                     ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                      out["quote_id"]))
        conn.commit()
        conn.close()
        sr = qapi.get(f"/api/runs/{rid}").json()["sourcing_results"]
        assert "DXP Enterprises" not in [f["vendorName"] for f in sr["findings"]]
        assert "DXP Enterprises" in [
            s["vendorName"] for s in sr["outreachTargets"]["suppliers"]]


# ---------------------------------------------------------------------------
# NO-AUTO-ORDER property (spec principle 4 / criterion 8)
# ---------------------------------------------------------------------------

class TestNoAutoOrderProperty:
    def test_no_quote_endpoint_reaches_order_placement(self, qapi, monkeypatch,
                                                       tmp_path):
        from utils import orders

        def _forbidden(*a, **kw):
            raise AssertionError("order placement reached from a quote endpoint")

        monkeypatch.setattr(orders, "create_order", _forbidden)
        monkeypatch.setattr(orders, "place_order", _forbidden)

        rid = _make_run(qapi)
        tok = _mint_token(rid)
        # Path A: form + submit + revise (active AND review outcomes).
        assert qapi.get(f"/api/quote/{tok['token']}").status_code == 200
        assert qapi.post(f"/api/quote/{tok['token']}",
                         json=_valid_body()).status_code == 200
        r = qapi.post(f"/api/quote/{tok['token']}",
                      json=_valid_body(part_number="OTHER"))
        review_id = r.json()["quote_id"]
        # Path B.
        from utils import claim_tokens as ct, supplier_registry
        monkeypatch.setenv("SUPPLIER_PORTAL_V1", "1")
        monkeypatch.setattr(ct, "CLAIM_TOKENS_ENABLED", True)
        monkeypatch.setattr(ct, "_DB_PATH", str(tmp_path / "claim_tokens.sqlite"))
        supplier_registry.record_sent_message(
            run_id=rid, supplier_domain="dxpe.com", vendor_name="DXP",
            to=["sales@dxpe.com"], status="stubbed")
        claim = ct.generate_for("dxpe.com")
        assert qapi.post(f"/api/portal/{claim['token']}/quotes",
                         json={**_valid_body(), "run_id": rid}).status_code == 200
        # Path C + the review queue transitions.
        assert qapi.post("/api/admin/quotes", headers=_auth(),
                         json={**_valid_body(), "run_id": rid,
                               "supplier_domain": "sealit.example"}).status_code == 200
        assert qapi.get("/api/admin/quotes", headers=_auth()).status_code == 200
        assert qapi.post(f"/api/admin/quotes/{review_id}/reject",
                         headers=_auth()).status_code in (200, 409)
        # If any of the above had touched orders.create_order/place_order the
        # monkeypatched AssertionError would have propagated as a 500.
