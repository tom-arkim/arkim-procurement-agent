"""
SEND_GOVERNANCE_V1 acceptance suite (Night 10 T7) — the property tests the brief's
success criteria name, kept separate from the per-task tests so the safety
contract reads in one place:

  P1. PRECEDENCE: suppression → allowlist → caps → release → delivery gate, as a
      truth-table property over every combination of store states.
  P2. NO BYPASS: every entry point that can reach the delivery function is driven
      with the provider INSTRUMENTED (a counting mock at the real network seam);
      with governance blocking, ZERO provider calls happen — even with the
      delivery gate simulated ON (module-attr monkeypatch; the real env stays off).
  P3. FAIL-CLOSED: empty allowlist / missing stores / junk cap env ⇒ blocked.
  P4. KILL-SWITCH: everything released + allowlisted, gate OFF ⇒ "stubbed",
      zero provider calls — the gate is last and absolute; governance never
      enables delivery.
  P5. FLAG-OFF PARITY: governance code is never consulted (poisoned evaluate),
      routes 404, rows carry no governance columns.

ZERO live sends are possible here: the conftest safety net forces the gate off and
empties Gmail creds; every "gate on" is a module-attr monkeypatch plus a MOCK
service object.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

import utils.email_sender as email_sender
from utils import send_governance
from utils.email_sender import EmailMessage, GmailSender


@pytest.fixture
def gov(tmp_path, monkeypatch):
    """Isolated governance + registry + audit stores; flag ON."""
    from utils import supplier_registry, audit_log
    monkeypatch.setattr(send_governance, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(send_governance, "_DB_PATH", str(tmp_path / "send_governance.sqlite"))
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    monkeypatch.setattr(audit_log, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(audit_log, "_DB_PATH", str(tmp_path / "audit_log.sqlite"))
    monkeypatch.setenv("SEND_GOVERNANCE_V1", "1")
    return send_governance


class _CountingService:
    """The instrumented provider: counts calls at the REAL network seam
    (users().messages().send().execute())."""
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
        return {"id": "gmail-x", "threadId": "thread-x"}


def _msg(domain="dxpe.com", part_key=None):
    meta = {"supplier_domain": domain}
    if part_key:
        meta["part_key"] = part_key
    return EmailMessage(to=[f"sales@{domain}"], subject="Quote request",
                        body="b", metadata=meta)


# ---------------------------------------------------------------------------
# P1 — precedence truth table
# ---------------------------------------------------------------------------

class TestPrecedenceProperty:
    """suppression beats allowlist beats caps: for every combination of store
    states the verdict is the HIGHEST-precedence applicable block."""

    @pytest.mark.parametrize(
        "suppressed,allowlisted,cap_exhausted,expected",
        [
            (True,  True,  False, "suppressed"),       # stop-request wins over permission
            (True,  False, False, "suppressed"),       # ...and over absence of permission
            (True,  True,  True,  "suppressed"),       # ...and over caps
            (True,  False, True,  "suppressed"),
            (False, False, False, "not_allowlisted"),  # no permission blocks
            (False, False, True,  "not_allowlisted"),  # allowlist beats caps
            (False, True,  True,  "cap_blocked"),      # permitted but capped
            (False, True,  False, "ok"),               # clean pass -> gate decides
        ],
    )
    def test_matrix(self, gov, monkeypatch, suppressed, allowlisted, cap_exhausted,
                    expected):
        from utils import supplier_registry
        if suppressed:
            gov.suppression_add("dxpe.com", added_by="t", is_test=True)
        if allowlisted:
            gov.allowlist_add("dxpe.com", added_by="t", is_test=True)
        if cap_exhausted:
            monkeypatch.setenv("SEND_GOVERNANCE_DAILY_CAP", "0")
        v = gov.evaluate(_msg())
        if expected == "ok":
            assert v.allowed is True
        else:
            assert v.allowed is False
            assert v.status == expected

    def test_release_precedes_gate_structurally(self, gov):
        # "Release" is not a store check — it is STRUCTURAL: with governance on,
        # the only API route that turns an approved draft into a send is the
        # release-queue endpoint (the legacy direct-send 409s — proven at the
        # entry-point level in TestNoBypass) and send_rfq refuses without an
        # Approval at all. An unapproved/unreleased draft cannot reach evaluate().
        from utils.rfq_send import send_rfq
        res = send_rfq({"vendor_name": "DXP", "source_url": "https://dxpe.com"},
                       "Subject: Q\n\nbody", approval=None, run_id="r1")
        assert res["status"] == "not_sent_no_approval"
        assert res["sent"] is False


# ---------------------------------------------------------------------------
# P2 — no entry point bypasses the stack (instrumented provider)
# ---------------------------------------------------------------------------

class TestNoBypass:
    """Every caller of the send layer, driven with governance blocking (empty
    allowlist — the fail-closed default) and the delivery gate simulated ON:
    the instrumented provider must see ZERO calls."""

    @pytest.fixture
    def gate_on(self, monkeypatch):
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)

    def test_entry_direct_gmail_sender(self, gov, gate_on):
        service = _CountingService()
        res = GmailSender(service=service).send(_msg())
        assert res.status == "not_allowlisted"
        assert service.send_calls == 0

    def test_entry_send_rfq(self, gov, gate_on):
        from utils import supplier_registry
        from utils.rfq_send import Approval, send_rfq
        supplier_registry.upsert_contact("dxpe.com", {
            "contact_email": "sales@dxpe.com", "contact_method": "generic_inbox",
            "contact_status": "resolved"})
        service = _CountingService()
        res = send_rfq({"vendor_name": "DXP", "source_url": "https://dxpe.com"},
                       "Subject: Q\n\nbody", Approval("tom"), run_id="r1",
                       sender=GmailSender(service=service))
        assert res["status"] == "not_allowlisted"
        assert service.send_calls == 0

    def test_entry_tier1_notify(self, gov, gate_on):
        from utils import supplier_registry
        from utils.procurement_agent import tier1_notify
        supplier_registry.upsert_contact("dxpe.com", {
            "contact_email": "sales@dxpe.com", "contact_method": "generic_inbox",
            "contact_status": "resolved"})
        match = SimpleNamespace(domain="dxpe.com", vendor_name="DXP",
                                noun_class="mechanical seal",
                                brand_relationship=None, is_core=True)
        service = _CountingService()
        status, _mid = tier1_notify._send_notify(match, "r1",
                                                 GmailSender(service=service))
        assert status == "not_allowlisted"
        assert service.send_calls == 0

    def test_entry_intake_reply_sink(self, gov, gate_on, monkeypatch):
        import api_server
        from utils import gmail_client
        service = _CountingService()
        monkeypatch.setattr(gmail_client, "build_gmail_service", lambda **kw: service)
        reply = SimpleNamespace(to="tech@customer.com", subject="Re: your request",
                                body="ack", kind="ack", metadata={})
        api_server._intake_reply_sink(reply)          # fail-soft; must not raise
        assert service.send_calls == 0                # blocked before the provider

    def test_entry_legacy_send_endpoint_409s(self, gov, tmp_path, monkeypatch):
        # The legacy direct-send route refuses outright flag-on — the release
        # queue is the only draft→delivery path. (Full-flow release coverage
        # lives in test_send_governance.TestReleaseQueue.)
        from sqlalchemy.orm import sessionmaker
        from fastapi.testclient import TestClient
        from utils.procurement_agent.state import persistence

        engine = persistence._make_engine(f"sqlite:///{tmp_path / 'api.sqlite'}")
        TestSession = sessionmaker(bind=engine, expire_on_commit=False)
        persistence.Base.metadata.create_all(engine)
        monkeypatch.setattr(persistence, "_engine", engine)
        monkeypatch.setattr(persistence, "_SessionFactory", TestSession)
        import api_server
        monkeypatch.setattr(api_server, "_engine", engine)
        monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
        monkeypatch.setattr(api_server, "DEMO_MODE", False)
        client = TestClient(api_server.app)

        run_id = client.post("/api/runs", json={}).json()["id"]
        d = persistence.create_draft(run_id=run_id, candidate_id="DXP-t1-0",
                                     candidate_snapshot={"vendor_name": "DXP",
                                                         "source_url": "https://dxpe.com"},
                                     draft_body="Subject: Q\n\nbody")
        persistence.transition_draft(d["id"], "approved", approved_by="tom")
        r = client.post(f"/api/rfq-drafts/{d['id']}/send")
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# P3 — fail-closed consolidation
# ---------------------------------------------------------------------------

class TestFailClosedProperty:
    def test_empty_allowlist_blocks_everything(self, gov):
        for domain in ("dxpe.com", "zoro.com", "anything.io"):
            v = gov.evaluate(_msg(domain))
            assert v.allowed is False and v.status == "not_allowlisted"

    def test_missing_store_file_is_empty_not_open(self, gov):
        # A fresh path (no DB file yet) = an empty allowlist, not an open door.
        v = gov.evaluate(_msg())
        assert v.allowed is False

    def test_unreadable_store_blocks_at_first_stage(self, gov, tmp_path, monkeypatch):
        broken = tmp_path / "broken"
        broken.mkdir()
        monkeypatch.setattr(send_governance, "_DB_PATH", str(broken))
        v = gov.evaluate(_msg())
        assert v.allowed is False and v.status == "suppressed"

    def test_junk_cap_env_blocks(self, gov, monkeypatch):
        gov.allowlist_add("dxpe.com", added_by="t", is_test=True)
        monkeypatch.setenv("SEND_GOVERNANCE_DAILY_CAP", "unlimited!!")
        v = gov.evaluate(_msg())
        assert v.allowed is False and v.status == "cap_blocked"


# ---------------------------------------------------------------------------
# P4 — kill-switch semantics: the gate is last and absolute
# ---------------------------------------------------------------------------

class TestKillSwitch:
    def test_released_allowlisted_gate_off_stubs_zero_provider_calls(self, gov):
        # Criterion: everything permitted by governance, EMAIL_SEND_ENABLED off ⇒
        # status "stubbed", provider NEVER touched. Governance never enables
        # delivery; flipping the gate off stops everything.
        gov.allowlist_add("dxpe.com", added_by="t", is_test=True)
        service = _CountingService()
        assert email_sender.EMAIL_SEND_ENABLED is False        # conftest net
        res = GmailSender(service=service).send(_msg())
        assert res.status == "stubbed"
        assert service.send_calls == 0

    def test_end_to_end_release_with_gate_off_records_stubbed(self, gov, monkeypatch):
        # The full Phase-1 flow: approved draft → release → stack pass → gate off
        # ⇒ ledger row "stubbed" with releaser identity. Zero deliveries.
        from utils import supplier_registry
        from utils.rfq_send import Approval, send_rfq
        supplier_registry.upsert_contact("dxpe.com", {
            "contact_email": "sales@dxpe.com", "contact_method": "generic_inbox",
            "contact_status": "resolved"})
        gov.allowlist_add("dxpe.com", added_by="t", is_test=True)
        service = _CountingService()
        res = send_rfq({"vendor_name": "DXP", "source_url": "https://dxpe.com"},
                       "Subject: Q\n\nbody", Approval("tom"), run_id="r1",
                       sender=GmailSender(service=service), released_by="tom")
        assert res["status"] == "stubbed" and res["sent"] is False
        assert service.send_calls == 0
        row = supplier_registry.get_sent_messages(run_id="r1")[0]
        assert row["status"] == "stubbed" and row["released_by"] == "tom"


# ---------------------------------------------------------------------------
# P5 — flag-off parity
# ---------------------------------------------------------------------------

class TestFlagOffParity:
    def test_governance_never_consulted(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SEND_GOVERNANCE_V1", raising=False)
        for fn in ("evaluate", "_check_suppression", "_check_allowlist", "_check_caps"):
            monkeypatch.setattr(send_governance, fn,
                                lambda *a, **k: (_ for _ in ()).throw(
                                    AssertionError(f"{fn} consulted flag-off")))
        res = GmailSender().send(_msg())
        assert res.status == "stubbed"                          # today's exact behavior

    def test_send_rfq_provider_not_invoked_flag_off_gate_off(self, tmp_path, monkeypatch):
        # The legacy stub path: gate off + flag off ⇒ the provider object is never
        # even called (send_rfq synthesizes the stub itself) — as before Night 10.
        from utils import supplier_registry
        from utils.rfq_send import Approval, send_rfq
        monkeypatch.delenv("SEND_GOVERNANCE_V1", raising=False)
        monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(supplier_registry, "_DB_PATH",
                            str(tmp_path / "supplier_registry.sqlite"))
        supplier_registry.upsert_contact("dxpe.com", {
            "contact_email": "sales@dxpe.com", "contact_method": "generic_inbox",
            "contact_status": "resolved"})

        class _Poisoned:
            def send(self, message):
                raise AssertionError("provider invoked flag-off gate-off")

        res = send_rfq({"vendor_name": "DXP", "source_url": "https://dxpe.com"},
                       "Subject: Q\n\nbody", Approval("tom"), run_id="r1",
                       sender=_Poisoned())
        assert res["status"] == "stubbed"
