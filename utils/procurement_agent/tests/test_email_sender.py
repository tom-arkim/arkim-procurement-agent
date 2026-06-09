"""
Tests for utils/email_sender.py — the provider-agnostic send interface + the
STUBBED GmailSender.

Safety focus: with EMAIL_SEND_ENABLED False (the default) or no credentials, send()
returns a stubbed result and makes ZERO real calls; the live Gmail path is unwired
and raises rather than faking a send. No network is ever touched here.
"""

import pytest

import utils.email_sender as es
from utils.email_sender import EmailMessage, SendResult, GmailSender, EMAIL_SEND_ENABLED


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

    def test_flag_true_no_creds_returns_stubbed(self, monkeypatch):
        monkeypatch.setattr(es, "EMAIL_SEND_ENABLED", True)
        sender = GmailSender(credentials=None)
        res = sender.send(self._msg())
        assert res.status == "stubbed"
        assert sender.configured is False  # no creds => not configured

    def test_live_path_is_unwired_and_raises(self, monkeypatch):
        """Flag True + creds present => the live branch, which is intentionally not
        wired: it raises rather than pretending to send. Proves no silent real send."""
        monkeypatch.setattr(es, "EMAIL_SEND_ENABLED", True)
        sender = GmailSender(credentials=object())
        assert sender.configured is True
        with pytest.raises(NotImplementedError):
            sender.send(self._msg())

    def test_no_network_dependency_imported(self):
        """The send module imports no HTTP/SMTP client — structurally cannot do a
        real send from the stub path."""
        import sys
        src = es.__file__
        with open(src, "r", encoding="utf-8") as fh:
            text = fh.read()
        for banned in ("import requests", "import smtplib", "import http", "urllib.request"):
            assert banned not in text
