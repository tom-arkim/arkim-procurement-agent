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
# Suppression (T3): "supplier asked to stop" — first in the stack
# ---------------------------------------------------------------------------

class TestSuppression:
    def test_suppressed_domain_blocks_with_suppressed_status(self, gov_store):
        gov_store.suppression_add("dxpe.com", added_by="tom",
                                  reason="asked to stop", is_test=True)
        v = gov_store.evaluate(_msg())
        assert v.allowed is False
        assert v.status == "suppressed"

    def test_suppression_beats_allowlist(self, gov_store):
        # The precedence rule: a domain BOTH allowlisted and suppressed is blocked
        # as suppressed — the stop request wins over the permission.
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        gov_store.suppression_add("dxpe.com", added_by="tom", is_test=True)
        v = gov_store.evaluate(_msg())
        assert v.status == "suppressed"

    def test_any_suppressed_recipient_blocks(self, gov_store):
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        gov_store.allowlist_add("other.com", added_by="tom", is_test=True)
        gov_store.suppression_add("other.com", added_by="tom", is_test=True)
        v = gov_store.evaluate(_msg(to=("sales@dxpe.com",), cc=("info@other.com",)))
        assert v.status == "suppressed"

    def test_permanent_until_removed(self, gov_store):
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        gov_store.suppression_add("dxpe.com", added_by="tom", is_test=True)
        assert gov_store.evaluate(_msg()).status == "suppressed"
        gov_store.suppression_remove("dxpe.com", removed_by="tom")
        assert gov_store.evaluate(_msg()).allowed is True

    def test_unreadable_store_fails_closed_as_suppressed(self, gov_store, tmp_path,
                                                         monkeypatch):
        broken = tmp_path / "broken_gov"
        broken.mkdir()
        monkeypatch.setattr(send_governance, "_DB_PATH", str(broken))
        v = gov_store.evaluate(_msg())
        assert v.allowed is False
        assert v.status == "suppressed"     # blocked at the FIRST failing stage

    def test_sender_returns_suppressed_and_never_touches_provider(
            self, gov_store, flag_on, monkeypatch):
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        gov_store.suppression_add("dxpe.com", added_by="tom", is_test=True)
        service = _MockGmailService()
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        res = GmailSender(service=service).send(_msg())
        assert res.status == "suppressed"
        assert service.send_calls == 0

    def test_send_rfq_records_suppressed(self, tmp_path, monkeypatch, gov_store, flag_on):
        from utils import supplier_registry
        from utils.rfq_send import Approval, send_rfq
        monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(supplier_registry, "_DB_PATH",
                            str(tmp_path / "supplier_registry.sqlite"))
        supplier_registry.upsert_contact("dxpe.com", {
            "contact_email": "sales@dxpe.com", "contact_method": "generic_inbox",
            "contact_status": "resolved"})
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        gov_store.suppression_add("dxpe.com", added_by="tom", is_test=True)
        res = send_rfq({"vendor_name": "DXP", "source_url": "https://dxpe.com"},
                       "Subject: Q\n\nbody", Approval("tom"), run_id="r1")
        assert res["status"] == "suppressed"
        assert res["outreach_status"] == "blocked"
        rows = supplier_registry.get_sent_messages(run_id="r1")
        assert len(rows) == 1 and rows[0]["status"] == "suppressed"


class TestSuppressionAdminEndpoints:
    def test_roundtrip(self, admin_api, flag_on):
        r = admin_api.post("/api/admin/send-governance/suppression", headers=_auth(),
                           json={"domain": "https://DXPE.com", "added_by": "tom",
                                 "reason": "asked to stop"})
        assert r.status_code == 201 and r.json()["domain"] == "dxpe.com"
        r = admin_api.get("/api/admin/send-governance/suppression", headers=_auth())
        assert [x["domain"] for x in r.json()["suppression"]] == ["dxpe.com"]
        r = admin_api.post("/api/admin/send-governance/suppression/dxpe.com/remove",
                           headers=_auth(), json={"removed_by": "tom"})
        assert r.status_code == 200
        assert admin_api.get("/api/admin/send-governance/suppression",
                             headers=_auth()).json()["count"] == 0

    def test_flag_off_404(self, admin_api, monkeypatch):
        monkeypatch.delenv("SEND_GOVERNANCE_V1", raising=False)
        assert admin_api.get("/api/admin/send-governance/suppression",
                             headers=_auth()).status_code == 404


# ---------------------------------------------------------------------------
# Caps (T2): daily global + per-supplier-per-part open-RFQ, from sent_messages
# ---------------------------------------------------------------------------

class TestCaps:
    @pytest.fixture
    def stores(self, tmp_path, monkeypatch, gov_store):
        """Governance + supplier_registry stores on tmp; dxpe.com allowlisted so
        the caps stage (after allowlist) is what's under test."""
        from utils import supplier_registry
        monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(supplier_registry, "_DB_PATH",
                            str(tmp_path / "supplier_registry.sqlite"))
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        return supplier_registry

    def _attempt_row(self, sr, status="stubbed", domain="dxpe.com", part_key=None,
                     run_id="r1"):
        return sr.record_sent_message(
            run_id=run_id, supplier_domain=domain, vendor_name="DXP",
            to=[f"sales@{domain}"], status=status, part_key=part_key)

    def _pk_msg(self, part_key="gusher pumps|8400428"):
        m = _msg()
        m.metadata["part_key"] = part_key
        return m

    def test_daily_cap_blocks_at_limit(self, stores, monkeypatch):
        monkeypatch.setenv("SEND_GOVERNANCE_DAILY_CAP", "3")
        for _ in range(3):
            self._attempt_row(stores)
        v = send_governance.evaluate(_msg())
        assert v.allowed is False
        assert v.status == "cap_blocked"
        assert "daily" in v.reason

    def test_daily_cap_allows_below_limit(self, stores, monkeypatch):
        monkeypatch.setenv("SEND_GOVERNANCE_DAILY_CAP", "3")
        for _ in range(2):
            self._attempt_row(stores)
        assert send_governance.evaluate(_msg()).allowed is True

    def test_default_daily_cap_is_10(self, stores):
        for _ in range(10):
            self._attempt_row(stores)
        v = send_governance.evaluate(_msg())
        assert v.allowed is False and v.status == "cap_blocked"

    def test_blocked_rows_never_consume_cap(self, stores, monkeypatch):
        monkeypatch.setenv("SEND_GOVERNANCE_DAILY_CAP", "1")
        for st in ("not_allowlisted", "suppressed", "cap_blocked"):
            self._attempt_row(stores, status=st)
        assert send_governance.evaluate(_msg()).allowed is True

    def test_utc_day_rollover_resets(self, stores, monkeypatch):
        # Criterion 6: yesterday's attempts don't count against today.
        import sqlite3
        monkeypatch.setenv("SEND_GOVERNANCE_DAILY_CAP", "1")
        self._attempt_row(stores)
        with sqlite3.connect(stores._DB_PATH) as conn:
            conn.execute("UPDATE sent_messages SET created_at = '2026-07-10T12:00:00+00:00'")
            conn.commit()
        assert send_governance.evaluate(_msg()).allowed is True

    def test_open_rfq_cap_blocks_same_part_same_supplier(self, stores):
        self._attempt_row(stores, part_key="gusher pumps|8400428")
        v = send_governance.evaluate(self._pk_msg())
        assert v.allowed is False
        assert v.status == "cap_blocked"
        assert "open-RFQ" in v.reason

    def test_open_rfq_cap_scoped_to_part_and_supplier(self, stores, gov_store):
        gov_store.allowlist_add("other.com", added_by="tom", is_test=True)
        self._attempt_row(stores, part_key="gusher pumps|8400428")
        # different part, same supplier ⇒ allowed
        assert send_governance.evaluate(self._pk_msg("skf|6205")).allowed is True
        # same part, different supplier ⇒ allowed
        m = EmailMessage(to=["sales@other.com"], subject="s", body="b",
                         metadata={"supplier_domain": "other.com",
                                   "part_key": "gusher pumps|8400428"})
        assert send_governance.evaluate(m).allowed is True

    def test_open_rfq_cap_env_override(self, stores, monkeypatch):
        monkeypatch.setenv("SEND_GOVERNANCE_OPEN_RFQ_CAP", "2")
        self._attempt_row(stores, part_key="gusher pumps|8400428")
        assert send_governance.evaluate(self._pk_msg()).allowed is True

    def test_unparseable_cap_env_fails_closed(self, stores, monkeypatch):
        monkeypatch.setenv("SEND_GOVERNANCE_DAILY_CAP", "ten")
        v = send_governance.evaluate(_msg())
        assert v.allowed is False and v.status == "cap_blocked"

    def test_unreadable_sent_messages_store_fails_closed(self, stores, tmp_path, monkeypatch):
        broken = tmp_path / "broken_sm"
        broken.mkdir()
        from utils import supplier_registry
        monkeypatch.setattr(supplier_registry, "_DB_PATH", str(broken))
        v = send_governance.evaluate(_msg())
        assert v.allowed is False and v.status == "cap_blocked"

    def test_allowlist_beats_caps(self, stores, monkeypatch):
        # Precedence: a non-allowlisted domain reports not_allowlisted even when
        # the daily cap is also exhausted.
        monkeypatch.setenv("SEND_GOVERNANCE_DAILY_CAP", "0")
        m = EmailMessage(to=["sales@nowhere.com"], subject="s", body="b")
        v = send_governance.evaluate(m)
        assert v.status == "not_allowlisted"

    def test_send_rfq_records_cap_blocked_and_part_key(self, stores, flag_on):
        from utils.rfq_send import Approval, send_rfq
        stores.upsert_contact("dxpe.com", {"contact_email": "sales@dxpe.com",
                                           "contact_method": "generic_inbox",
                                           "contact_status": "resolved"})
        cand = {"vendor_name": "DXP Enterprises", "source_url": "https://dxpe.com"}
        pk = "gusher pumps|8400428"
        r1 = send_rfq(cand, "Subject: Q\n\nbody", Approval("tom"), run_id="r1", part_key=pk)
        assert r1["status"] == "stubbed"                      # first: through the stack
        rows = stores.get_sent_messages(run_id="r1")
        assert rows[0]["part_key"] == pk                      # part identity recorded
        r2 = send_rfq(dict(cand), "Subject: Q\n\nbody", Approval("tom"), run_id="r1",
                      part_key=pk)
        assert r2["status"] == "cap_blocked"                  # open-RFQ cap bites
        assert r2["outreach_status"] == "blocked"
        statuses = {r["status"] for r in stores.get_sent_messages(run_id="r1")}
        assert statuses == {"stubbed", "cap_blocked"}         # blocked outcome recorded


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


# ---------------------------------------------------------------------------
# Release queue (T4): the concierge send-approval step
# ---------------------------------------------------------------------------

class TestReleaseQueue:
    @pytest.fixture
    def queue_env(self, admin_api, tmp_path, monkeypatch, gov_store, flag_on):
        """A run + two approved drafts (dxpe.com), registry contact seeded, stores
        isolated. Returns (client, draft_ids, supplier_registry)."""
        from utils import supplier_registry
        from utils.procurement_agent.state import persistence
        monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(supplier_registry, "_DB_PATH",
                            str(tmp_path / "supplier_registry.sqlite"))
        supplier_registry.upsert_contact("dxpe.com", {
            "contact_email": "sales@dxpe.com", "contact_method": "generic_inbox",
            "contact_status": "resolved"})
        run_id = admin_api.post("/api/runs", json={"asset_specs": {
            "manufacturer": "Gusher Pumps", "part_number": "84004-28"}}).json()["id"]
        ids = []
        for i in range(2):
            d = persistence.create_draft(
                run_id=run_id, candidate_id=f"DXP-t1-{i}",
                candidate_snapshot={"vendor_name": "DXP Enterprises",
                                    "source_url": "https://dxpe.com"},
                draft_body=f"Subject: Quote request {i}\n\nbody {i}")
            persistence.transition_draft(d["id"], "approved", approved_by="tom")
            ids.append(d["id"])
        return admin_api, ids, supplier_registry

    def test_queue_lists_approved_drafts_with_content(self, queue_env):
        client, ids, _sr = queue_env
        r = client.get("/api/admin/send-governance/release-queue", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2
        listed = {d["id"] for d in body["pending"]}
        assert listed == set(ids)
        d0 = body["pending"][0]
        assert d0["draft_body"].startswith("Subject:")          # full rendered content
        assert d0["recipients"]["to"] == ["sales@dxpe.com"]     # who it goes to

    def test_release_records_stubbed_with_releaser_identity(self, queue_env, gov_store):
        # SUCCESS CRITERION 4: released + delivery gate off ⇒ sent_messages row
        # status 'stubbed' carrying released_by + released_at. Zero deliveries.
        client, ids, sr = queue_env
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        r = client.post("/api/admin/send-governance/release-queue/release",
                        headers=_auth(),
                        json={"draft_ids": ids[:1], "released_by": "tom"})
        assert r.status_code == 200
        res = r.json()["results"][0]
        assert res["released"] is True
        assert res["send_status"] == "stubbed"                  # gate off: no delivery
        assert res["sent"] is False
        assert res["draft_status"] == "approved"                # 'sent' = really went
        rows = sr.get_sent_messages()
        assert len(rows) == 1
        assert rows[0]["status"] == "stubbed"
        assert rows[0]["released_by"] == "tom"
        assert rows[0]["released_at"]                           # timestamp recorded
        assert rows[0]["part_key"]                              # cap key stamped

    def test_release_runs_the_full_stack_not_allowlisted(self, queue_env):
        # Empty allowlist: a released draft is BLOCKED and the block is recorded
        # with the releaser identity (the release action was real).
        client, ids, sr = queue_env
        r = client.post("/api/admin/send-governance/release-queue/release",
                        headers=_auth(),
                        json={"draft_ids": ids[:1], "released_by": "tom"})
        res = r.json()["results"][0]
        assert res["send_status"] == "not_allowlisted"
        rows = sr.get_sent_messages()
        assert rows[0]["status"] == "not_allowlisted"
        assert rows[0]["released_by"] == "tom"

    def test_release_suppressed_beats_allowlisted(self, queue_env, gov_store):
        client, ids, sr = queue_env
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        gov_store.suppression_add("dxpe.com", added_by="tom", is_test=True)
        r = client.post("/api/admin/send-governance/release-queue/release",
                        headers=_auth(),
                        json={"draft_ids": ids[:1], "released_by": "tom"})
        assert r.json()["results"][0]["send_status"] == "suppressed"

    def test_batch_release_mixed_outcomes(self, queue_env, gov_store):
        # Batch: first release stubs; the second hits the per-part open-RFQ cap
        # (same supplier, same part) — per-draft outcomes, no all-or-nothing.
        client, ids, sr = queue_env
        gov_store.allowlist_add("dxpe.com", added_by="tom", is_test=True)
        r = client.post("/api/admin/send-governance/release-queue/release",
                        headers=_auth(),
                        json={"draft_ids": ids, "released_by": "tom"})
        statuses = [x["send_status"] for x in r.json()["results"]]
        assert statuses == ["stubbed", "cap_blocked"]

    def test_release_unknown_and_unapproved_ids_error_per_draft(self, queue_env):
        client, ids, _sr = queue_env
        from utils.procurement_agent.state import persistence
        persistence.release_reject_draft(ids[1], rejected_by="tom")   # now 'rejected'
        r = client.post("/api/admin/send-governance/release-queue/release",
                        headers=_auth(),
                        json={"draft_ids": ["nope", ids[1]], "released_by": "tom"})
        res = r.json()["results"]
        assert res[0]["released"] is False and "not found" in res[0]["error"]
        assert res[1]["released"] is False and "rejected" in res[1]["error"]

    def test_release_reject_endpoint(self, queue_env):
        client, ids, _sr = queue_env
        r = client.post(f"/api/admin/send-governance/release-queue/{ids[0]}/reject",
                        headers=_auth(), json={"rejected_by": "tom"})
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"
        # …and it left the queue.
        q = client.get("/api/admin/send-governance/release-queue", headers=_auth())
        assert ids[0] not in {d["id"] for d in q.json()["pending"]}

    def test_legacy_send_endpoint_409s_flag_on(self, queue_env):
        client, ids, _sr = queue_env
        r = client.post(f"/api/rfq-drafts/{ids[0]}/send")
        assert r.status_code == 409
        assert "release" in r.json()["detail"].lower()

    def test_legacy_lifecycle_untouched_flag_off(self, queue_env, monkeypatch):
        # Flag OFF: approved→rejected stays ILLEGAL through the legacy transition
        # (release_reject_draft is reachable only via the flag-gated endpoint).
        from utils.procurement_agent.state import persistence
        client, ids, _sr = queue_env
        monkeypatch.delenv("SEND_GOVERNANCE_V1", raising=False)
        with pytest.raises(persistence.DraftTransitionError):
            persistence.transition_draft(ids[0], "rejected", rejected_by="x")
        # and the queue routes are gone (404, route absent).
        assert client.get("/api/admin/send-governance/release-queue",
                          headers=_auth()).status_code == 404


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
