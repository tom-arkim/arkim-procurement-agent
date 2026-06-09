"""
Tests for utils/email_sender.py — the provider-agnostic send interface + the
STUBBED GmailSender.

Safety focus: with EMAIL_SEND_ENABLED False (the default), send() returns a stubbed
result and makes ZERO real calls; flag True + no credentials fails soft (error, no
crash); the real Gmail path is exercised only through a MOCKED service (no real send,
no google libs, no network in the suite).
"""

import pytest
from unittest.mock import MagicMock

import utils.email_sender as es
from utils.email_sender import EmailMessage, SendResult, GmailSender, EMAIL_SEND_ENABLED


@pytest.fixture(autouse=True)
def _no_gmail_env(monkeypatch):
    for var in ("GMAIL_SERVICE_ACCOUNT_JSON", "GMAIL_SERVICE_ACCOUNT_FILE",
                "GMAIL_OAUTH_TOKEN_FILE", "GMAIL_SENDER"):
        monkeypatch.delenv(var, raising=False)


class TestModuleGate:
    def test_send_disabled_by_default(self):
        """The canonical gate ships False — nothing sends without a deliberate flip."""
        assert EMAIL_SEND_ENABLED is False
        assert es.EMAIL_SEND_ENABLED is False


class TestEmailMessage:
    def test_all_recipients_dedupes_preserving_order(self):
        msg = EmailMessage(
            to=["jane@x.com"],
            cc=["sales@x.com", "jane@x.com", "info@x.com"],
            subject="Quote request",
            body="...",
        )
        assert msg.all_recipients == ["jane@x.com", "sales@x.com", "info@x.com"]

    def test_metadata_defaults_empty(self):
        msg = EmailMessage(to=["a@x.com"], subject="s", body="b")
        assert msg.cc == []
        assert msg.metadata == {}


class TestGmailSenderStubbed:
    def _msg(self) -> EmailMessage:
        return EmailMessage(
            to=["jane@baypower.com"], cc=["sales@baypower.com"],
            subject="Quote request", body="hello",
            metadata={"run_id": "r1", "supplier_domain": "baypower.com", "rfq_id": "rfq1"},
        )

    def test_flag_false_returns_stubbed_no_send(self):
        """Default gate off => stubbed result, regardless of credentials."""
        sender = GmailSender(credentials=object())  # creds present, but flag is False
        res = sender.send(self._msg())
        assert isinstance(res, SendResult)
        assert res.status == "stubbed"
        assert res.message_id is None and res.thread_id is None
        assert sender.configured is False  # flag off => not configured

    def test_flag_true_no_creds_fails_soft(self, monkeypatch):
        """Flag on but no usable credentials => fail-soft 'error' (clear, no crash,
        no half-send) — NOT a silent stub."""
        monkeypatch.setattr(es, "EMAIL_SEND_ENABLED", True)
        sender = GmailSender()           # no injected service, no env creds
        res = sender.send(self._msg())
        assert res.status == "error" and res.error
        assert sender.configured is False  # no creds => not configured

    def test_live_path_sends_via_mocked_client(self, monkeypatch):
        """Flag True + an injected (MOCKED) Gmail service => one real-shaped send,
        returning the RFC822 Message-ID + Gmail threadId. No real Gmail call."""
        monkeypatch.setattr(es, "EMAIL_SEND_ENABLED", True)
        service = MagicMock()
        send = service.users.return_value.messages.return_value.send
        send.return_value.execute.return_value = {"id": "gmail-123", "threadId": "T-1"}

        sender = GmailSender(service=service)
        assert sender.configured is True
        res = sender.send(self._msg())

        assert res.status == "sent"
        assert res.thread_id == "T-1"
        assert res.message_id == "rfq1@arkim.ai"   # deterministic from metadata.rfq_id
        send.assert_called_once()
        assert send.call_args.kwargs["userId"] == "me"
        assert "raw" in send.call_args.kwargs["body"]   # base64url MIME

    def test_send_failure_is_failsoft(self, monkeypatch):
        monkeypatch.setattr(es, "EMAIL_SEND_ENABLED", True)
        service = MagicMock()
        service.users.return_value.messages.return_value.send.return_value.execute.side_effect = \
            RuntimeError("api down")
        res = GmailSender(service=service).send(self._msg())
        assert res.status == "error" and "api down" in (res.error or "")

    def test_google_libs_lazy_not_imported_at_load(self):
        """Importing the send module pulls in NO google libraries (they're lazy), so
        the suite needs neither the libs nor credentials."""
        import sys
        assert "googleapiclient" not in sys.modules
        assert "google.oauth2" not in sys.modules
