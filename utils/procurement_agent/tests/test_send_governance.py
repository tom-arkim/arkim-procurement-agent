"""
Tests for utils/send_governance.py + its wiring (SEND_GOVERNANCE_V1 — Night 10).

The safety contract under test, with ZERO live sends possible (conftest forces
EMAIL_SEND_ENABLED off; where a test needs the gate "on" it monkeypatches the module
attr and injects a MOCK Gmail service — no credentials, no network):

  - FAIL-CLOSED: empty allowlist ⇒ blocked; unreadable store ⇒ blocked; never
    allow-on-error.
  - Enforcement lives at the LAST seam (GmailSender.send), ahead of the delivery
    gate: a blocked message returns its governance status and the provider service
    is NEVER touched — even with the gate simulated on.
  - Governance can only BLOCK — an allowlisted message with the gate off still
    stubs (the kill-switch semantics; governance never enables delivery).
  - Flag OFF ⇒ byte-identical: governance is never consulted (parity proven by
    poisoning evaluate()).

Stores isolated to tmp_path throughout — the real data/*.sqlite are never touched;
rows created via the admin surface in tests carry is_test where applicable.
"""

import pytest

import utils.email_sender as email_sender
from utils import send_governance
from utils.email_sender import EmailMessage, GmailSender, SendResult


@pytest.fixture
def gov_store(tmp_path, monkeypatch):
    """Point the governance store at a throwaway sqlite file."""
    monkeypatch.setattr(send_governance, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(send_governance, "_DB_PATH", str(tmp_path / "send_governance.sqlite"))
    # audit_log writes on admin mutations — isolate it too.
    from utils import audit_log
    monkeypatch.setattr(audit_log, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(audit_log, "_DB_PATH", str(tmp_path / "audit_log.sqlite"))
    return send_governance


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("SEND_GOVERNANCE_V1", "1")


def _msg(to=("sales@dxpe.com",), cc=()):
    return EmailMessage(to=list(to), cc=list(cc), subject="Quote request",
                        body="body", metadata={"supplier_domain": "dxpe.com"})


class _MockGmailService:
    """Counts real provider calls; the T7-style instrumentation seam."""
    def __init__(self):
        self.send_calls = 0

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId=None, body=None):
        self.send_calls += 1
        return self

    def execute(self):
        return {"id": "gmail-1", "threadId": "thread-1"}


# ---------------------------------------------------------------------------
# Flag parse + store CRUD
# ---------------------------------------------------------------------------

class TestFlagAndStore:
    @pytest.mark.parametrize("val,expected", [
        ("1", True), ("true", True), ("YES", True), ("on", True),
        ("", False), ("0", False), ("false", False), ("junk", False), (None, False),
    ])
    def test_flag_strict_truthy(self, monkeypatch, val, expected):
        if val is None:
            monkeypatch.delenv("SEND_GOVERNANCE_V1", raising=False)
        else:
            monkeypatch.setenv("SEND_GOVERNANCE_V1", val)
        assert send_governance.send_governance_active() is expected

    def test_add_normalizes_domain(self, gov_store):
        gov_store.allowlist_add("https://www.DXPE.com/some/page", added_by="tom", is_test=True)
        rows = gov_store.allowlist_list()
        assert [r["domain"] for r in rows] == ["dxpe.com"]
        assert rows[0]["added_by"] == "tom"
        assert rows[0]["is_test"] == 1

    def test_add_unusable_domain_raises(self, gov_store):
        with pytest.raises(ValueError):
            gov_store.allowlist_add("   ", added_by="tom")

    def test_add_is_idempotent_upsert(self, gov_store):
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        gov_store.allowlist_add("dxpe.com", added_by="tom2", note="refresh", is_test=True)
        rows = gov_store.allowlist_list()
        assert len(rows) == 1
        assert rows[0]["added_by"] == "tom2"

    def test_remove(self, gov_store):
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        assert gov_store.allowlist_remove("dxpe.com", removed_by="tom") is True
        assert gov_store.allowlist_list() == []
        assert gov_store.allowlist_remove("dxpe.com", removed_by="tom") is False


# ---------------------------------------------------------------------------
# evaluate() — fail-closed allowlist semantics
# ---------------------------------------------------------------------------

class TestEvaluateFailClosed:
    def test_empty_allowlist_blocks(self, gov_store):
        v = gov_store.evaluate(_msg())
        assert v.allowed is False
        assert v.status == "not_allowlisted"

    def test_allowlisted_domain_passes(self, gov_store):
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        v = gov_store.evaluate(_msg())
        assert v.allowed is True

    def test_every_recipient_domain_must_be_allowlisted(self, gov_store):
        # to allowlisted, cc on a second, non-allowlisted domain ⇒ blocked.
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        v = gov_store.evaluate(_msg(to=("sales@dxpe.com",), cc=("info@other.com",)))
        assert v.allowed is False
        assert v.status == "not_allowlisted"
        assert "other.com" in v.reason

    def test_no_recipients_blocks(self, gov_store):
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        v = gov_store.evaluate(EmailMessage(to=[], subject="s", body="b"))
        assert v.allowed is False

    def test_unreadable_store_blocks_never_allows(self, gov_store, tmp_path, monkeypatch):
        # Point the DB path at a DIRECTORY: sqlite cannot open it ⇒ the check
        # raises internally ⇒ verdict must be BLOCKED (fail-closed), not allowed.
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        broken = tmp_path / "not_a_db"
        broken.mkdir()
        monkeypatch.setattr(send_governance, "_DB_PATH", str(broken))
        v = gov_store.evaluate(_msg())
        assert v.allowed is False
        assert "fail-closed" in v.reason


# ---------------------------------------------------------------------------
# GmailSender.send — governance ahead of the delivery gate, at the last seam
# ---------------------------------------------------------------------------

class TestSenderEnforcement:
    def test_blocked_before_gate_flag_on_gate_off(self, gov_store, flag_on):
        # Gate OFF (conftest default): a non-allowlisted send reports its REAL
        # verdict, not "stubbed" — the governance stack precedes the gate.
        res = GmailSender().send(_msg())
        assert res.status == "not_allowlisted"

    def test_blocked_send_never_touches_provider_even_gate_on(
            self, gov_store, flag_on, monkeypatch):
        # Criterion 2: empty allowlist ⇒ nothing delivers even with the gate
        # simulated ON (module-attr monkeypatch only — the real env stays off).
        service = _MockGmailService()
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        res = GmailSender(service=service).send(_msg())
        assert res.status == "not_allowlisted"
        assert service.send_calls == 0                      # provider untouched

    def test_allowlisted_with_gate_off_stubs(self, gov_store, flag_on):
        # Kill-switch semantics: governance passes, the (off) delivery gate still
        # stubs. Governance can only block, never enable.
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        res = GmailSender().send(_msg())
        assert res.status == "stubbed"

    def test_allowlisted_with_gate_on_delivers_via_mock(self, gov_store, flag_on, monkeypatch):
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        service = _MockGmailService()
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        res = GmailSender(service=service).send(_msg())
        assert res.status == "sent"
        assert service.send_calls == 1

    def test_flag_off_parity_governance_never_consulted(self, gov_store, monkeypatch):
        # Flag OFF: byte-identical behavior — evaluate() must never run. Poison it.
        monkeypatch.delenv("SEND_GOVERNANCE_V1", raising=False)
        def _boom(message):
            raise AssertionError("evaluate() consulted with SEND_GOVERNANCE_V1 off")
        monkeypatch.setattr(send_governance, "evaluate", _boom)
        res = GmailSender().send(_msg())                    # non-allowlisted domain
        assert res.status == "stubbed"                      # exactly today's behavior


# ---------------------------------------------------------------------------
# send_rfq — the RFQ flow records the real verdict on sent_messages (flag-on)
# ---------------------------------------------------------------------------

class TestSendRfqRecordsVerdict:
    @pytest.fixture
    def rfq_stores(self, tmp_path, monkeypatch, gov_store):
        from utils import supplier_registry
        monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(supplier_registry, "_DB_PATH",
                            str(tmp_path / "supplier_registry.sqlite"))
        supplier_registry.upsert_contact("dxpe.com", {
            "contact_email": "sales@dxpe.com", "contact_method": "generic_inbox",
            "contact_status": "resolved"})
        return supplier_registry

    def _cand(self):
        return {"vendor_name": "DXP Enterprises", "source_url": "https://dxpe.com"}

    def test_not_allowlisted_recorded_not_masked_as_stubbed(self, rfq_stores, flag_on):
        from utils.rfq_send import Approval, send_rfq
        res = send_rfq(self._cand(), "Subject: Q\n\nbody", Approval("tom"), run_id="r1")
        assert res["status"] == "not_allowlisted"
        assert res["sent"] is False
        assert res["outreach_status"] == "blocked"
        rows = rfq_stores.get_sent_messages(run_id="r1")
        assert len(rows) == 1 and rows[0]["status"] == "not_allowlisted"

    def test_allowlisted_stubs_and_records_stubbed(self, rfq_stores, flag_on, gov_store):
        from utils.rfq_send import Approval, send_rfq
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        res = send_rfq(self._cand(), "Subject: Q\n\nbody", Approval("tom"), run_id="r2")
        assert res["status"] == "stubbed"
        assert res["outreach_status"] == "awaiting"
        rows = rfq_stores.get_sent_messages(run_id="r2")
        assert len(rows) == 1 and rows[0]["status"] == "stubbed"


# ---------------------------------------------------------------------------
# Admin endpoints (flag-gated + admin-token-gated)
# ---------------------------------------------------------------------------

_TOKEN = "test-admin-secret-123"


def _auth():
    return {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture
def admin_api(tmp_path, monkeypatch, gov_store):
    """Minimal admin client (mirrors test_admin_api isolation) + governance store."""
    from sqlalchemy.orm import sessionmaker
    from fastapi.testclient import TestClient
    from utils.procurement_agent.state import persistence

    engine = persistence._make_engine(f"sqlite:///{tmp_path / 'api.sqlite'}")
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    persistence.Base.metadata.create_all(engine)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr(persistence, "_engine", engine)
    monkeypatch.setattr(persistence, "_SessionFactory", TestSession)
    monkeypatch.setenv("ARKIM_ADMIN_TOKEN", _TOKEN)

    import api_server
    monkeypatch.setattr(api_server, "_engine", engine)
    monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
    # Guard against a leaked DEMO_MODE=True from test_demo_mode's module reload
    # earlier in the session — its allowlist middleware would 403 the admin routes
    # (same guard as test_api_server's api fixture).
    monkeypatch.setattr(api_server, "DEMO_MODE", False)
    return TestClient(api_server.app)


class TestAdminEndpoints:
    def test_flag_off_routes_404(self, admin_api, monkeypatch):
        monkeypatch.delenv("SEND_GOVERNANCE_V1", raising=False)
        r = admin_api.get("/api/admin/send-governance/allowlist", headers=_auth())
        assert r.status_code == 404                    # route absent flag-off

    def test_add_list_remove_roundtrip(self, admin_api, flag_on):
        r = admin_api.post("/api/admin/send-governance/allowlist", headers=_auth(),
                           json={"domain": "https://www.DXPE.com/x", "added_by": "tom",
                                 "note": "phase 1"})
        assert r.status_code == 201
        assert r.json()["domain"] == "dxpe.com"

        r = admin_api.get("/api/admin/send-governance/allowlist", headers=_auth())
        assert r.status_code == 200
        assert [x["domain"] for x in r.json()["allowlist"]] == ["dxpe.com"]

        r = admin_api.post("/api/admin/send-governance/allowlist/dxpe.com/remove",
                           headers=_auth(), json={"removed_by": "tom"})
        assert r.status_code == 200
        assert admin_api.get("/api/admin/send-governance/allowlist",
                             headers=_auth()).json()["count"] == 0

    def test_remove_unknown_404(self, admin_api, flag_on):
        r = admin_api.post("/api/admin/send-governance/allowlist/nope.com/remove",
                           headers=_auth(), json={"removed_by": "tom"})
        assert r.status_code == 404

    def test_unusable_domain_422(self, admin_api, flag_on):
        r = admin_api.post("/api/admin/send-governance/allowlist", headers=_auth(),
                           json={"domain": "   ", "added_by": "tom"})
        assert r.status_code == 422

    def test_requires_admin_token(self, admin_api, flag_on):
        assert admin_api.get("/api/admin/send-governance/allowlist").status_code == 401
