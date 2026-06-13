"""
utils/email_sender.py
Provider-agnostic outbound email SEND interface (Layer 1, outbound only).

This is the send seam for Tier 3 RFQs. It is deliberately split from drafting and
from the send *flow* (utils/rfq_send.py): this module only knows how to hand a
fully-formed message to a provider and report the result. The provider is Gmail
(sends as GMAIL_SENDER, default procurement@arkim.ai); the real Gmail API call is
wired via utils.gmail_client (google libs imported lazily — the suite needs neither
the libs nor credentials, and EMAIL_SEND_ENABLED defaults OFF).

Safety (this is the first layer that can take an external action):
  - EMAIL_SEND_ENABLED is the canonical send gate. It defaults OFF and is opt-in via
    the environment (set EMAIL_SEND_ENABLED to a truthy value in .env) — a deliberate,
    documented enabling decision. This is a fresh constant owned by the send layer; the
    identically-named flag in utils/sourcing_archieved/tier3_outreach.py is DEAD code
    (see CLAUDE.md §6) and is intentionally NOT reused. (CLEANUP notes the duplication.)
  - While EMAIL_SEND_ENABLED is False, a sender returns a STUBBED SendResult and makes
    ZERO network calls. The double gate (this flag AND the per-draft approval in
    rfq_send) is unchanged.
  - Flag True + a usable Gmail service -> a real send, returning the RFC822 Message-ID
    (we set it, so a bounce DSN matches) and Gmail threadId (so a reply matches).
  - Flag True + NO credentials -> fail-soft "error" (clear, no crash, no half-send),
    never a silent stub. Tests inject a mocked service, so no real Gmail call occurs.

Conventions mirror utils/apollo_client.py (the sibling standalone provider client):
bracket-prefixed print logging ("[EmailSender] ..."), explicit type annotations,
fail-soft by contract.
"""

from __future__ import annotations

import base64
import mimetypes
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from typing import Optional

from utils import gmail_client

def _env_truthy(value: Optional[str]) -> bool:
    """Strict opt-in parse: only an explicit truthy token enables. Anything else
    (None, "", "0", "false", "no", junk) -> False, so the gate fails safe."""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


# Canonical outbound send gate. Default OFF; opt-in by setting EMAIL_SEND_ENABLED to a
# truthy value (1/true/yes/on) in the environment (.env) — a deliberate, documented
# enabling decision (legal review of templates + real provider wiring). Read once at
# import; tests force it off via the conftest safety net so no test can send by accident.
EMAIL_SEND_ENABLED: bool = _env_truthy(os.environ.get("EMAIL_SEND_ENABLED"))


@dataclass
class EmailAttachment:
    """One file part for an outbound message (e.g. a quote/RFQ PDF).

    Held in memory as bytes so the build/encode path is pure and unit-testable
    without disk I/O. `mime_type` is split into maintype/subtype for the MIME part;
    it defaults to a generic binary type when unknown.
    """
    filename: str
    content: bytes
    mime_type: str = "application/octet-stream"

    @classmethod
    def from_path(cls, path: str, filename: Optional[str] = None) -> "EmailAttachment":
        """Read a file from disk into an attachment, guessing the MIME type from its
        extension. `filename` overrides the displayed name (defaults to the basename)."""
        with open(path, "rb") as fh:
            content = fh.read()
        guessed, _ = mimetypes.guess_type(path)
        return cls(filename=filename or os.path.basename(path), content=content,
                   mime_type=guessed or "application/octet-stream")


@dataclass
class EmailMessage:
    """One outbound message addressed to a supplier's recipient set.

    `metadata` carries the keys later inbound matching (bounce/quote ingestion)
    keys on: run_id, supplier_domain, rfq_id. `to`/`cc` are the assembled recipient
    set (named primary in `to`, generic inbox in `cc`, per the recipient-set rule).
    `attachments`, when non-empty, makes the built message multipart/mixed (the body
    text part first, then each file part); empty keeps the single text/plain message.
    """
    to: list[str]
    subject: str
    body: str
    cc: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    attachments: list[EmailAttachment] = field(default_factory=list)

    @property
    def all_recipients(self) -> list[str]:
        """To + CC, de-duplicated, order-preserving."""
        seen: set[str] = set()
        out: list[str] = []
        for addr in [*self.to, *self.cc]:
            if addr and addr not in seen:
                seen.add(addr)
                out.append(addr)
        return out


@dataclass
class SendResult:
    """Outcome of a send attempt.

    status:
      "sent"    — provider accepted the message (live path; not reachable yet).
      "stubbed" — gated/no-creds: nothing was sent, recorded for the demo trail.
      "error"   — provider call failed (fail-soft; never raised into the flow).
    message_id / thread_id are placeholders until the live provider returns real
    ids; sent-message persistence stores them so inbound matching can resolve later.
    """
    status: str
    message_id: Optional[str] = None
    thread_id: Optional[str] = None
    error: Optional[str] = None
    sent_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class EmailSender(ABC):
    """Swappable send-provider interface. Implementations must be fail-soft:
    send() returns a SendResult and never raises into the caller (except the
    deliberately-unwired live Gmail branch, which is gated off)."""

    @abstractmethod
    def send(self, message: EmailMessage) -> SendResult:
        ...


class GmailSender(EmailSender):
    """Gmail-backed sender. The real Gmail API call is WIRED (utils.gmail_client) and
    fires only when EMAIL_SEND_ENABLED is on AND a Gmail service/creds resolve —
    default-off and double-gated, so the repo/test default sends nothing.

    Behaviour:
      - EMAIL_SEND_ENABLED False (repo/test default) -> "stubbed", zero network.
      - flag True + a usable Gmail service -> real send as GMAIL_SENDER
        (procurement@arkim.ai); returns the RFC822 Message-ID (set by us, for bounce
        matching) + Gmail threadId (for reply matching).
      - flag True + NO credentials -> fail-soft "error" (clear, no crash, no half-send).
    The double gate (this flag AND the per-draft approval in rfq_send) is unchanged.
    The Gmail service is built lazily from env creds (utils.gmail_client) or injected
    (the `service` arg, used by tests — no real Gmail call in the suite).
    """

    def __init__(self, credentials: Optional[object] = None,
                 service: Optional[object] = None, sender: Optional[str] = None):
        # `credentials` is retained for interface back-compat; the real path builds a
        # service from env (gmail_client) or uses an injected `service` (tests).
        self._credentials = credentials
        self._service = service
        self._sender = sender

    @staticmethod
    def _env_creds_present() -> bool:
        return any(os.environ.get(v) for v in (
            "GMAIL_SERVICE_ACCOUNT_JSON", "GMAIL_SERVICE_ACCOUNT_FILE", "GMAIL_OAUTH_TOKEN_FILE"))

    @property
    def configured(self) -> bool:
        """True only when the gate is on AND a sending path is available (an injected
        service/credentials, or env-configured credentials)."""
        return bool(EMAIL_SEND_ENABLED and (self._service or self._credentials
                                            or self._env_creds_present()))

    def _build_raw(self, message: EmailMessage) -> tuple:
        """Build the base64url-encoded RFC822 message and its Message-ID (no brackets).

        The Message-ID is deterministic from the rfq_id when present, so a later bounce
        DSN (whose In-Reply-To references it) matches the sent_messages row.

        With no attachments the message is a single text/plain part (unchanged). With
        attachments it becomes multipart/mixed: the body text first, then one base64-
        encoded file part per attachment.
        """
        body_part = MIMEText(message.body or "")
        if message.attachments:
            mime: MIMEText | MIMEMultipart = MIMEMultipart("mixed")
            mime.attach(body_part)
            for att in message.attachments:
                maintype, _, subtype = (att.mime_type or "application/octet-stream").partition("/")
                part = MIMEBase(maintype, subtype or "octet-stream")
                part.set_payload(att.content)
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment", filename=att.filename)
                mime.attach(part)
        else:
            mime = body_part
        mime["To"] = ", ".join(message.to)
        if message.cc:
            mime["Cc"] = ", ".join(message.cc)
        mime["From"] = self._sender or gmail_client.gmail_sender_address()
        mime["Subject"] = message.subject or ""
        rfq_id = (message.metadata or {}).get("rfq_id")
        header = f"<{rfq_id}@arkim.ai>" if rfq_id else make_msgid(domain="arkim.ai")
        mime["Message-ID"] = header
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        return raw, header.strip("<>")

    def send(self, message: EmailMessage) -> SendResult:
        if not EMAIL_SEND_ENABLED:
            print(f"[EmailSender] STUBBED (EMAIL_SEND_ENABLED=False) -> {message.all_recipients}")
            return SendResult(status="stubbed")
        service = self._service or gmail_client.build_gmail_service()
        if service is None:
            # Flag on but no usable credentials: surface the misconfig (fail-soft),
            # never a silent stub/half-send.
            print(f"[EmailSender] ERROR: send enabled but no Gmail credentials -> {message.all_recipients}")
            return SendResult(status="error", error="Gmail credentials not configured")
        try:
            raw, message_id = self._build_raw(message)
            sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
            thread_id = sent.get("threadId")
            print(f"[EmailSender] SENT -> {message.all_recipients} "
                  f"(gmail_id={sent.get('id')} thread={thread_id} msgid={message_id})")
            return SendResult(status="sent", message_id=message_id, thread_id=thread_id)
        except Exception as exc:
            print(f"[EmailSender] send failed: {type(exc).__name__}: {exc}")
            return SendResult(status="error", error=str(exc))
