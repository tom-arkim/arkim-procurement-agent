"""
Tests for utils/rfq_send.py — the HITL-gated Tier 3 outbound RFQ send flow.

Safety invariants under test (this is the first layer that can take an external
action), all with the provider/Gmail STUBBED — ZERO real emails:
  - EMAIL_SEND_ENABLED False  => stub path only; provider NOT invoked; vendor awaiting.
  - EMAIL_SEND_ENABLED True + approval => provider invoked exactly once, correct
    recipient set, sent-message recorded, vendor contacted.
  - No approval (even with flag True) => NOT sent (HITL gate holds).
  - Recipient assembly: named present (To named / CC generic) and named absent
    (generic only); bounced generic excluded.
  - Sent-message persisted with the fields inbound matching needs.

Both raw-sqlite stores (supplier_registry, audit_log) are isolated to tmp_path so
the real data/*.sqlite files are never touched.
"""

import pytest

import utils.email_sender as email_sender
from utils import supplier_registry, audit_log
from utils.email_sender import EmailSender, SendResult, GmailSender
from utils.rfq_send import Approval, send_rfq


@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
    """Point supplier_registry AND audit_log at throwaway sqlite files."""
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    monkeypatch.setattr(audit_log, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(audit_log, "_DB_PATH", str(tmp_path / "audit_log.sqlite"))
    return supplier_registry


class _FakeSender(EmailSender):
    """Records the messages it is asked to send and returns a canned result."""
    def __init__(self, result: SendResult):
        self.result = result
        self.calls: list = []

    def send(self, message):
        self.calls.append(message)
        return self.result


def _candidate(domain: str = "baypower.com", name: str = "Bay Power") -> dict:
    return {"vendor_name": name, "source_url": f"https://www.{domain}/quote"}


_DRAFT = "Subject: Quote request - Bay Power\n\nHello,\nPlease quote PN EM3770T.\n"


def _seed_generic(sr, domain="baypower.com", email="sales@baypower.com"):
    sr.upsert_contact(domain, {"contact_email": email, "contact_method": "generic_inbox",
                               "contact_status": "resolved"})


def _seed_primary(sr, domain="baypower.com", email="jeff@baypower.com"):
    sr.upsert_primary_contact(domain, {"primary_contact_email": email,
                                       "primary_contact_name": "Jeff Baker",
                                       "primary_contact_status": "resolved"})


# ---------------------------------------------------------------------------
# Flag False — stub path only
# ---------------------------------------------------------------------------

class TestFlagFalseStubPath:
    def test_stub_path_does_not_invoke_provider(self, isolated_stores):
        sr = isolated_stores
        _seed_generic(sr)
        sender = _FakeSender(SendResult(status="sent"))  # would be "sent" IF called
        cand = _candidate()

        res = send_rfq(cand, _DRAFT, Approval("Maintenance Director"),
                       run_id="run1", sender=sender)

        assert email_sender.EMAIL_SEND_ENABLED is False     # default gate
        assert sender.calls == []                           # provider NOT invoked
        assert res["sent"] is False
        assert res["status"] == "stubbed"
        assert res["outreach_status"] == "awaiting"
        assert cand["outreach_status"] == "awaiting"
        # Sent-message still recorded (a real demo event), as "stubbed".
        rows = sr.get_sent_messages(run_id="run1")
        assert len(rows) == 1 and rows[0]["status"] == "stubbed"

    def test_default_gmail_sender_stays_stubbed_zero_network(self, isolated_stores):
        """No injected sender => default GmailSender; flag False => stubbed, no network."""
        _seed_generic(isolated_stores)
        res = send_rfq(_candidate(), _DRAFT, Approval("Dir"), run_id="run1")
        assert res["status"] == "stubbed" and res["sent"] is False


# ---------------------------------------------------------------------------
# Flag True + approval — provider invoked once
# ---------------------------------------------------------------------------

class TestFlagTrueWithApproval:
    def test_sends_once_with_named_to_generic_cc(self, isolated_stores, monkeypatch):
        sr = isolated_stores
        _seed_generic(sr)
        _seed_primary(sr)
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        sender = _FakeSender(SendResult(status="sent", message_id="m1", thread_id="t1"))
        cand = _candidate()

        res = send_rfq(cand, _DRAFT, Approval("Maintenance Director"),
                       run_id="run1", sender=sender)

        assert len(sender.calls) == 1                       # invoked exactly once
        msg = sender.calls[0]
        assert msg.to == ["jeff@baypower.com"]              # named primary -> To
        assert msg.cc == ["sales@baypower.com"]             # generic inbox -> CC
        assert msg.metadata["run_id"] == "run1"
        assert msg.metadata["supplier_domain"] == "baypower.com"
        assert "rfq_id" in msg.metadata

        assert res["sent"] is True and res["status"] == "sent"
        assert res["outreach_status"] == "contacted"
        assert cand["outreach_status"] == "contacted"

        rows = sr.get_sent_messages(run_id="run1")
        assert len(rows) == 1
        assert rows[0]["status"] == "sent"
        assert rows[0]["message_id"] == "m1" and rows[0]["thread_id"] == "t1"
        assert rows[0]["approved_by"] == "Maintenance Director"

    def test_named_absent_generic_only(self, isolated_stores, monkeypatch):
        sr = isolated_stores
        _seed_generic(sr)                                   # generic only, no primary
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        sender = _FakeSender(SendResult(status="sent", message_id="m1"))

        send_rfq(_candidate(), _DRAFT, Approval("Dir"), run_id="run1", sender=sender)

        msg = sender.calls[0]
        assert msg.to == ["sales@baypower.com"]
        assert msg.cc == []

    def test_default_gmail_sender_with_flag_true_still_stubbed(self, isolated_stores, monkeypatch):
        """Flag True but the default GmailSender has no creds => stubbed, no real send."""
        _seed_generic(isolated_stores)
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        res = send_rfq(_candidate(), _DRAFT, Approval("Dir"), run_id="run1")
        assert res["status"] == "stubbed" and res["sent"] is False


# ---------------------------------------------------------------------------
# HITL gate — no approval, no send
# ---------------------------------------------------------------------------

class TestApprovalGate:
    def test_no_approval_never_sends_even_with_flag_true(self, isolated_stores, monkeypatch):
        sr = isolated_stores
        _seed_generic(sr)
        _seed_primary(sr)
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        sender = _FakeSender(SendResult(status="sent"))
        cand = _candidate()

        res = send_rfq(cand, _DRAFT, approval=None, run_id="run1", sender=sender)

        assert sender.calls == []                           # provider NOT invoked
        assert res["sent"] is False
        assert res["status"] == "not_sent_no_approval"
        assert cand["outreach_status"] == "pending_approval"
        assert sr.get_sent_messages(run_id="run1") == []    # nothing recorded


# ---------------------------------------------------------------------------
# Recipient edge cases
# ---------------------------------------------------------------------------

class TestRecipientEdges:
    def test_bounced_generic_excluded_primary_used(self, isolated_stores, monkeypatch):
        sr = isolated_stores
        _seed_generic(sr)
        _seed_primary(sr)
        sr.mark_contact_bounced("baypower.com", which="generic")   # kill the generic
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        sender = _FakeSender(SendResult(status="sent", message_id="m1"))

        send_rfq(_candidate(), _DRAFT, Approval("Dir"), run_id="run1", sender=sender)

        msg = sender.calls[0]
        assert msg.to == ["jeff@baypower.com"]              # primary still used
        assert msg.cc == []                                 # bounced generic excluded

    def test_no_recipients_blocks_send(self, isolated_stores, monkeypatch):
        """No store contact at all => no recipients => not sent, provider untouched."""
        sr = isolated_stores
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        sender = _FakeSender(SendResult(status="sent"))
        cand = _candidate(domain="unknown-vendor.com", name="Unknown Vendor")

        res = send_rfq(cand, _DRAFT, Approval("Dir"), run_id="run1", sender=sender)

        assert sender.calls == []
        assert res["status"] == "no_recipients" and res["sent"] is False
        assert cand["outreach_status"] == "needs_human_contact"
        assert sr.get_sent_messages(run_id="run1") == []


# ---------------------------------------------------------------------------
# Sent-message record carries the inbound-matching fields
# ---------------------------------------------------------------------------

class TestSentMessageRecord:
    def test_record_has_inbound_matching_fields(self, isolated_stores):
        sr = isolated_stores
        _seed_generic(sr)
        send_rfq(_candidate(), _DRAFT, Approval("Dir"), run_id="run-xyz")
        rows = sr.get_sent_messages(run_id="run-xyz")
        assert len(rows) == 1
        row = rows[0]
        # Keys later inbound (bounce/quote) matching joins on:
        assert row["run_id"] == "run-xyz"
        assert row["supplier_domain"] == "baypower.com"
        assert row["recipients_to"] == ["sales@baypower.com"]
        assert "thread_id" in row and "message_id" in row  # placeholders present
        assert row["sent_at"]
