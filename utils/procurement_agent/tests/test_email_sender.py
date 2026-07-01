"""
Tests for utils/email_sender.py — the provider-agnostic send interface + the
STUBBED GmailSender.

Safety focus: with EMAIL_SEND_ENABLED False (the default), send() returns a stubbed
result and makes ZERO real calls; flag True + no credentials fails soft (error, no
crash); the real Gmail path is exercised only through a MOCKED service (no real send,
no google libs, no network in the suite).
"""

import base64
import email
import os
import subprocess
import sys

import pytest
from unittest.mock import MagicMock

import utils.email_sender as es
from utils.email_sender import (
    EmailAttachment,
    EmailMessage,
    SendResult,
    GmailSender,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))


@pytest.fixture(autouse=True)
def _no_gmail_env(monkeypatch):
    for var in ("GMAIL_SERVICE_ACCOUNT_JSON", "GMAIL_SERVICE_ACCOUNT_FILE",
                "GMAIL_OAUTH_TOKEN_FILE", "GMAIL_SENDER"):
        monkeypatch.delenv(var, raising=False)


class TestModuleGate:
    def test_default_off_opt_in_via_env(self):
        """The gate is opt-in: only an explicit truthy env value enables it; anything
        else fails safe (off). This is the source default — nothing sends unless the
        environment deliberately turns it on."""
        for off in (None, "", "0", "false", "False", "no", "off", "garbage"):
            assert es._env_truthy(off) is False, off
        for on in ("1", "true", "TRUE", "yes", "On"):
            assert es._env_truthy(on) is True, on

    def test_gate_off_in_tests(self):
        """The suite is hermetic: the autouse safety net forces the gate off regardless
        of a real EMAIL_SEND_ENABLED=True in .env, so no test sends by accident."""
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
        """Importing the send module pulls in NO google libraries (they're lazy). Checked
        in a FRESH interpreter so suite-order pollution (another test importing google to
        exercise the build path) can't make this spuriously pass/fail now that the google
        libs are actually installed."""
        code = (
            "import sys\n"
            "import utils.email_sender  # noqa: F401\n"
            "assert 'googleapiclient' not in sys.modules, 'googleapiclient eagerly imported'\n"
            "assert 'google.oauth2' not in sys.modules, 'google.oauth2 eagerly imported'\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], cwd=_ROOT,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr or proc.stdout


def _decode_sent_mime(send_mock) -> email.message.Message:
    """Pull the base64url `raw` body off the mocked Gmail send call and parse it back
    into an email.message.Message so the wire MIME can be asserted on."""
    raw = send_mock.call_args.kwargs["body"]["raw"]
    return email.message_from_bytes(base64.urlsafe_b64decode(raw))


class TestAttachments:
    def test_attachments_default_empty(self):
        msg = EmailMessage(to=["a@x.com"], subject="s", body="b")
        assert msg.attachments == []

    def test_from_path_reads_bytes_and_guesses_mime(self, tmp_path):
        pdf = tmp_path / "quote.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake pdf bytes")
        att = EmailAttachment.from_path(str(pdf))
        assert att.filename == "quote.pdf"
        assert att.content == b"%PDF-1.4 fake pdf bytes"
        assert att.mime_type == "application/pdf"

    def test_from_path_unknown_extension_is_octet_stream(self, tmp_path):
        blob = tmp_path / "part.bin"
        blob.write_bytes(b"\x00\x01\x02")
        assert EmailAttachment.from_path(str(blob)).mime_type == "application/octet-stream"

    def test_no_attachment_is_single_text_part(self, monkeypatch):
        """Regression: without attachments the wire message stays single text/plain."""
        monkeypatch.setattr(es, "EMAIL_SEND_ENABLED", True)
        service = MagicMock()
        send = service.users.return_value.messages.return_value.send
        send.return_value.execute.return_value = {"id": "g1", "threadId": "T1"}
        GmailSender(service=service).send(
            EmailMessage(to=["jane@x.com"], subject="s", body="hello")
        )
        parsed = _decode_sent_mime(send)
        assert not parsed.is_multipart()
        assert parsed.get_content_type() == "text/plain"

    def test_live_path_sends_pdf_attachment_via_mocked_client(self, monkeypatch):
        """Flag True + injected MOCKED service => multipart/mixed send carrying the PDF.
        The body text is preserved and the file part is base64 with the right filename.
        No real Gmail call."""
        monkeypatch.setattr(es, "EMAIL_SEND_ENABLED", True)
        service = MagicMock()
        send = service.users.return_value.messages.return_value.send
        send.return_value.execute.return_value = {"id": "g1", "threadId": "T1"}

        msg = EmailMessage(
            to=["jane@x.com"], subject="Arkim RFQ", body="Please quote.",
            attachments=[EmailAttachment("quote.pdf", b"%PDF-1.4 body", "application/pdf")],
            metadata={"rfq_id": "rfq1"},
        )
        res = GmailSender(service=service).send(msg)

        assert res.status == "sent"
        parsed = _decode_sent_mime(send)
        assert parsed.is_multipart()

        body_parts = [p for p in parsed.walk() if p.get_content_type() == "text/plain"]
        assert any("Please quote." in p.get_payload(decode=True).decode() for p in body_parts)

        pdf_parts = [p for p in parsed.walk() if p.get_filename() == "quote.pdf"]
        assert len(pdf_parts) == 1
        part = pdf_parts[0]
        assert part.get_content_type() == "application/pdf"
        assert part.get("Content-Transfer-Encoding") == "base64"
        assert part.get_payload(decode=True) == b"%PDF-1.4 body"
