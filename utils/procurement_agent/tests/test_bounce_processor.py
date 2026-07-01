"""
Tests for utils/bounce_processor.py — matching bounces to sent_messages and driving
mark_contact_bounced.

Invariants asserted (the wrong-attribution caution applies throughout):
  - hard bounce matched by message_id  -> correct supplier's correct address cleared.
  - hard bounce matched by recipient+domain fallback (no ids) -> correct clear.
  - soft/transient bounce -> NOT cleared.
  - unmatched bounce -> NOTHING cleared (human review).
  - ambiguous (address != current primary/generic) -> NOTHING cleared.
  - primary vs generic: a bounce on the named primary clears primary (failover to
    generic); a bounce on the generic clears generic (escalation-eligible).
  - default GmailInboxReader is stubbed -> zero notices, zero clears, no network.

supplier_registry is isolated to a tmp sqlite file; no live mail is read.
"""

import pytest

import utils.email_sender as email_sender
from utils import supplier_registry
from utils.inbox_reader import BounceNotice, InboxReader
from utils.bounce_processor import process_bounces


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    return supplier_registry


class _FakeReader(InboxReader):
    def __init__(self, notices):
        self._notices = notices

    def fetch_bounces(self):
        return list(self._notices)


def _seed_generic(sr, domain="baypower.com", email="sales@baypower.com"):
    sr.upsert_contact(domain, {"contact_email": email, "contact_method": "generic_inbox",
                               "contact_status": "resolved"})


def _seed_primary(sr, domain="baypower.com", email="jeff@baypower.com"):
    sr.upsert_primary_contact(domain, {"primary_contact_email": email,
                                       "primary_contact_name": "Jeff Baker",
                                       "primary_contact_status": "resolved"})


def _sent(sr, *, domain="baypower.com", vendor="Bay Power",
          to=None, cc=None, message_id=None):
    sr.record_sent_message(run_id="run1", supplier_domain=domain, vendor_name=vendor,
                           to=to or [], cc=cc or [], status="sent", message_id=message_id)


# ---------------------------------------------------------------------------
# Hard bounce -> clears the right contact
# ---------------------------------------------------------------------------

class TestHardBounceClears:
    def test_matched_by_message_id_clears_generic(self, isolated_db):
        sr = isolated_db
        _seed_generic(sr)
        _sent(sr, to=["sales@baypower.com"], message_id="rfq-abc@arkim.ai")
        notice = BounceNotice(failed_recipient="sales@baypower.com",
                              message_id="rfq-abc@arkim.ai", is_hard=True)

        summary = process_bounces(reader=_FakeReader([notice]))

        assert summary["cleared"] == [{"domain": "baypower.com", "which": "generic",
                                       "address": "sales@baypower.com"}]
        rec = sr.lookup_by_domain("baypower.com")
        assert rec["contact_email"] is None and rec["contact_status"] == "bounced"

    def test_matched_by_recipient_fallback_no_ids(self, isolated_db):
        sr = isolated_db
        _seed_generic(sr)
        _sent(sr, to=["sales@baypower.com"], message_id=None)  # ids are placeholders today
        notice = BounceNotice(failed_recipient="sales@baypower.com", message_id=None, is_hard=True)

        summary = process_bounces(reader=_FakeReader([notice]))

        assert len(summary["cleared"]) == 1
        assert sr.lookup_by_domain("baypower.com")["contact_status"] == "bounced"


# ---------------------------------------------------------------------------
# primary vs generic
# ---------------------------------------------------------------------------

class TestPrimaryVsGeneric:
    def test_primary_bounce_clears_primary_leaves_generic(self, isolated_db):
        sr = isolated_db
        _seed_generic(sr)
        _seed_primary(sr)
        _sent(sr, to=["jeff@baypower.com"], cc=["sales@baypower.com"])
        notice = BounceNotice(failed_recipient="jeff@baypower.com", is_hard=True)

        summary = process_bounces(reader=_FakeReader([notice]))

        assert summary["cleared"][0]["which"] == "primary"
        rec = sr.lookup_by_domain("baypower.com")
        assert rec["primary_contact_email"] is None and rec["primary_contact_status"] == "bounced"
        assert rec["contact_email"] == "sales@baypower.com"   # generic untouched

    def test_generic_bounce_clears_generic_leaves_primary(self, isolated_db):
        sr = isolated_db
        _seed_generic(sr)
        _seed_primary(sr)
        _sent(sr, to=["jeff@baypower.com"], cc=["sales@baypower.com"])
        notice = BounceNotice(failed_recipient="sales@baypower.com", is_hard=True)

        summary = process_bounces(reader=_FakeReader([notice]))

        assert summary["cleared"][0]["which"] == "generic"
        rec = sr.lookup_by_domain("baypower.com")
        assert rec["contact_email"] is None and rec["contact_status"] == "bounced"
        assert rec["primary_contact_email"] == "jeff@baypower.com"   # primary untouched


# ---------------------------------------------------------------------------
# Conservative paths: no destructive action
# ---------------------------------------------------------------------------

class TestNoDestructiveAction:
    def test_soft_bounce_not_cleared(self, isolated_db):
        sr = isolated_db
        _seed_generic(sr)
        _sent(sr, to=["sales@baypower.com"])
        notice = BounceNotice(failed_recipient="sales@baypower.com", is_hard=False,
                              status_code="4.2.2")

        summary = process_bounces(reader=_FakeReader([notice]))

        assert summary["soft_skipped"] == ["sales@baypower.com"]
        assert summary["cleared"] == []
        assert sr.lookup_by_domain("baypower.com")["contact_email"] == "sales@baypower.com"

    def test_unmatched_bounce_clears_nothing(self, isolated_db):
        sr = isolated_db
        _seed_generic(sr)                       # contact exists...
        # ...but NO sent_messages row to this address -> not a confident match.
        notice = BounceNotice(failed_recipient="sales@baypower.com", is_hard=True)

        summary = process_bounces(reader=_FakeReader([notice]))

        assert summary["unmatched"] == ["sales@baypower.com"]
        assert summary["cleared"] == []
        assert sr.lookup_by_domain("baypower.com")["contact_email"] == "sales@baypower.com"

    def test_wrong_domain_recipient_not_matched(self, isolated_db):
        """Failed address belongs to a different domain than the sent row -> no match,
        no clear (no wrong-supplier attribution)."""
        sr = isolated_db
        _seed_generic(sr, domain="baypower.com", email="sales@baypower.com")
        _sent(sr, domain="baypower.com", to=["sales@baypower.com"])
        notice = BounceNotice(failed_recipient="sales@evil-other.com", is_hard=True)

        summary = process_bounces(reader=_FakeReader([notice]))

        assert summary["cleared"] == []
        assert summary["unmatched"] == ["sales@evil-other.com"]

    def test_ambiguous_address_not_cleared(self, isolated_db):
        """A confident row match, but the failed address is neither the current
        primary nor generic (e.g. contact changed since send) -> no clear."""
        sr = isolated_db
        _seed_generic(sr, email="sales@baypower.com")        # current generic
        _sent(sr, to=["old-buyer@baypower.com"])             # we sent to a now-stale addr
        notice = BounceNotice(failed_recipient="old-buyer@baypower.com", is_hard=True)

        summary = process_bounces(reader=_FakeReader([notice]))

        assert summary["cleared"] == []
        assert len(summary["ambiguous"]) == 1
        assert sr.lookup_by_domain("baypower.com")["contact_email"] == "sales@baypower.com"


# ---------------------------------------------------------------------------
# Default stubbed reader -> zero work, no network
# ---------------------------------------------------------------------------

class TestDefaultReaderStubbed:
    def test_default_gmail_reader_yields_nothing(self, isolated_db, monkeypatch):
        # Default reader is GmailInboxReader, stubbed (flag False) -> no notices.
        assert email_sender.EMAIL_SEND_ENABLED is False
        summary = process_bounces()
        assert summary == {"processed": 0, "cleared": [], "soft_skipped": [],
                           "unmatched": [], "ambiguous": []}
