"""
Tests for utils/inbox_reader.py — the inbound READ interface + the real (but
MOCKED-in-tests) GmailInboxReader.

Safety: with EMAIL_SEND_ENABLED False (the default) or no credentials, fetch_bounces()/
fetch_replies() return [] and make ZERO real calls. The live read is exercised only via
a MOCKED Gmail service (no real Gmail call, no google libs, no network in the suite).
"""

import base64

import pytest
from unittest.mock import MagicMock

import utils.email_sender as email_sender
from utils.inbox_reader import BounceNotice, ReplyNotice, GmailInboxReader
from utils.procurement_agent.tests._dsn_fixtures import HARD_BOUNCE


@pytest.fixture(autouse=True)
def _no_gmail_env(monkeypatch):
    for var in ("GMAIL_SERVICE_ACCOUNT_JSON", "GMAIL_SERVICE_ACCOUNT_FILE",
                "GMAIL_OAUTH_TOKEN_FILE", "GMAIL_SENDER"):
        monkeypatch.delenv(var, raising=False)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode()


def _mock_service(list_resp, get_resp):
    """Gmail service mock: messages().list().execute() and .get().execute()."""
    service = MagicMock()
    msgs = service.users.return_value.messages.return_value
    msgs.list.return_value.execute.return_value = list_resp
    msgs.get.return_value.execute.return_value = get_resp
    return service


class TestBounceNotice:
    def test_defaults_hard_no_ids(self):
        n = BounceNotice(failed_recipient="x@y.com")
        assert n.is_hard is True
        assert n.message_id is None and n.thread_id is None


class TestStubbed:
    def test_flag_false_returns_empty_no_read(self):
        reader = GmailInboxReader(credentials=object())  # creds present, flag False
        assert email_sender.EMAIL_SEND_ENABLED is False
        assert reader.fetch_bounces() == [] and reader.fetch_replies() == []
        assert reader.configured is False

    def test_flag_true_no_creds_returns_empty(self, monkeypatch):
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        reader = GmailInboxReader()                       # no service, no env creds
        assert reader.fetch_bounces() == [] and reader.fetch_replies() == []
        assert reader.configured is False                 # reader stays empty on no creds


class TestLiveReadMocked:
    def test_fetch_bounces_parses_dsn(self, monkeypatch):
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        service = _mock_service({"messages": [{"id": "b1"}]},
                                {"raw": _b64(HARD_BOUNCE), "threadId": "T1"})
        notices = GmailInboxReader(service=service).fetch_bounces()
        assert len(notices) == 1
        assert notices[0].failed_recipient == "sales@baypower.com"
        assert notices[0].is_hard is True

    def test_fetch_replies_builds_notice_with_thread(self, monkeypatch):
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        raw = ("From: Jeff Baker <jeff@baypower.com>\r\n"
               "To: procurement@arkim.ai\r\n"
               "Subject: Re: Quote request - Bay Power\r\n"
               "In-Reply-To: <rfq-abc@arkim.ai>\r\n"
               "Message-ID: <reply-1@baypower.com>\r\n"
               "Content-Type: text/plain; charset=\"utf-8\"\r\n\r\n"
               "Hi - $85 ea, 2 week lead.\r\n")
        service = _mock_service({"messages": [{"id": "r1"}]},
                                {"raw": _b64(raw), "threadId": "T-9"})
        notices = GmailInboxReader(service=service).fetch_replies()
        assert len(notices) == 1
        n = notices[0]
        assert n.sender == "jeff@baypower.com"
        assert n.in_reply_to == "rfq-abc@arkim.ai"   # matches a sent_messages.message_id
        assert n.thread_id == "T-9"                  # matches a sent_messages.thread_id
        assert "85 ea" in n.body

    def test_parse_reply_extracts_pdf_attachment(self):
        raw = (
            "From: sales@acme.com\r\n"
            "Subject: Quote\r\n"
            'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
            "--b\r\nContent-Type: text/plain\r\n\r\nSee attached.\r\n"
            "--b\r\nContent-Type: application/pdf\r\n"
            'Content-Disposition: attachment; filename="quote.pdf"\r\n'
            "Content-Transfer-Encoding: base64\r\n\r\n"
            + base64.b64encode(b"%PDF-1.4 fake").decode() + "\r\n--b--\r\n"
        )
        n = GmailInboxReader._parse_reply(raw, thread_id="T")
        assert n.sender == "sales@acme.com"
        assert n.body.strip() == "See attached."
        assert len(n.attachments) == 1
        assert n.attachments[0]["content_type"] == "application/pdf"
        assert n.attachments[0]["filename"] == "quote.pdf"

    def test_gmail_error_is_failsoft(self, monkeypatch):
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        service = MagicMock()
        service.users.return_value.messages.return_value.list.return_value.execute.side_effect = \
            RuntimeError("api down")
        assert GmailInboxReader(service=service).fetch_bounces() == []   # no crash


class TestLazyImport:
    def test_google_libs_not_imported_at_load(self):
        import sys
        assert "googleapiclient" not in sys.modules
        assert "google.oauth2" not in sys.modules
