"""
Tests for reply detection (GmailInboxReader.fetch_replies, stubbed) and reply→RFQ
matching (utils/reply_matcher.match_reply).

Stub focus: fetch_replies returns [] with zero network while gated/uncredentialled
(mirrors fetch_bounces). Matching focus: in_reply_to/message_id/thread, sender-in-
recipients and sender-domain fallback; no confident match -> None.
"""

import pytest

import utils.email_sender as email_sender
from utils import supplier_registry
from utils.inbox_reader import ReplyNotice, GmailInboxReader, InboxReader
from utils.reply_matcher import match_reply


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    return supplier_registry


class TestFetchRepliesStubbed:
    def test_flag_false_returns_empty(self):
        reader = GmailInboxReader(credentials=object())
        assert email_sender.EMAIL_SEND_ENABLED is False
        assert reader.fetch_replies() == []

    def test_flag_true_no_creds_empty(self, monkeypatch):
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        assert GmailInboxReader(credentials=None).fetch_replies() == []

    def test_live_path_unwired_raises(self, monkeypatch):
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        with pytest.raises(NotImplementedError):
            GmailInboxReader(credentials=object()).fetch_replies()

    def test_base_reader_default_is_empty(self):
        # A bounce-only reader (implements just fetch_bounces) still instantiates and
        # inherits the concrete fetch_replies default -> [] (no break to Layer 2).
        class _BounceOnly(InboxReader):
            def fetch_bounces(self):
                return []
        assert _BounceOnly().fetch_replies() == []


class TestMatchReply:
    def _rows(self, sr):
        sr.record_sent_message(run_id="run1", supplier_domain="baypower.com",
                               vendor_name="Bay Power", to=["sales@baypower.com"],
                               message_id="rfq-abc@arkim.ai", thread_id="thread-1")
        return sr.get_sent_messages()

    def test_match_by_in_reply_to(self, isolated_db):
        rows = self._rows(isolated_db)
        n = ReplyNotice(sender="sales@baypower.com", in_reply_to="rfq-abc@arkim.ai")
        assert match_reply(n, rows)["run_id"] == "run1"

    def test_match_by_thread_id(self, isolated_db):
        rows = self._rows(isolated_db)
        n = ReplyNotice(sender="someone@baypower.com", thread_id="thread-1")
        assert match_reply(n, rows)["vendor_name"] == "Bay Power"

    def test_match_by_sender_in_recipients(self, isolated_db):
        rows = self._rows(isolated_db)
        n = ReplyNotice(sender="sales@baypower.com")  # no ids
        assert match_reply(n, rows)["supplier_domain"] == "baypower.com"

    def test_match_by_sender_domain_fallback(self, isolated_db):
        rows = self._rows(isolated_db)
        n = ReplyNotice(sender="jeff@baypower.com")  # diff local part, same domain
        assert match_reply(n, rows)["supplier_domain"] == "baypower.com"

    def test_no_match_returns_none(self, isolated_db):
        rows = self._rows(isolated_db)
        n = ReplyNotice(sender="someone@unrelated-domain.com")
        assert match_reply(n, rows) is None
