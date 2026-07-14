"""
utils/inbox_reader.py
Inbound mail READ interface (Layer 2, inbound-bounce only) — provider-agnostic.

The read-side mirror of utils/email_sender.py: it pulls bounce DSNs (Layer 2) and RFQ
replies (Layer 3) out of the Arkim sourcing inbox and feeds them to the existing
parser/processor pipeline (unchanged). The provider is Gmail, wired via
utils.gmail_client (google libs lazy-imported; the suite needs neither the libs nor
credentials and makes no real Gmail call — the service is injected/mocked in tests).

Gating mirrors send: a live read is reachable only when email_sender.EMAIL_SEND_ENABLED
is True AND a Gmail service is available (env creds or injected). While gated or
uncredentialled, fetch_bounces()/fetch_replies() return [] and touch ZERO network.
Fail-soft: any Gmail error returns what was gathered so far, never raises.

BounceNotice is the shared parsed-bounce shape (the DSN parser in
utils/bounce_parser.py produces these; the processor in utils/bounce_processor.py
consumes them). Conventions mirror the sibling modules (typed, fail-soft,
bracket-prefixed logging).
"""

from __future__ import annotations

import base64
import email as _email
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr
from typing import Optional

import utils.email_sender as email_sender
from utils import gmail_client


def _is_intake_address(addr: str) -> bool:
    """True for an intake-stream address — ``intake@`` or ``intake+<tenant>@`` on
    the intake domain (utils.intake_channels convention). The SEND_GOVERNANCE_V1
    cross-stream filter drops such mail from the RFQ reader: the two streams share
    a mailbox but must never cross (spec §8 / Night 8 I2)."""
    if not addr or "@" not in addr:
        return False
    from utils.intake_channels import INTAKE_DOMAIN
    local, _, domain = addr.strip().lower().partition("@")
    return (domain == INTAKE_DOMAIN.lower()
            and (local == "intake" or local.startswith("intake+")))


@dataclass
class ReplyNotice:
    """One inbound REPLY to a sent RFQ (Layer 3), normalized for matching/extraction.

    sender — the supplier address the reply came FROM.
    message_id / thread_id / in_reply_to — identifiers used to match back to a
      sent_messages row (in_reply_to/message_id preferred, then thread, then
      sender domain).
    body — the plain-text reply body (free-text quote / nominated contact).
    attachments — list of {filename, content_type, data}; a PDF here is run through
      the OCR seam before LLM extraction.
    form — a structured quote-form submission, when the supplier used Arkim's form
      link (clean fields, highest confidence). None otherwise.
    """
    sender: str
    message_id: Optional[str] = None
    thread_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    subject: Optional[str] = None
    body: str = ""
    attachments: list = field(default_factory=list)
    form: Optional[dict] = None
    # Recipient addresses (To/Cc/Delivered-To), parsed for stream attribution —
    # the SEND_GOVERNANCE_V1 cross-stream filter keys on these (an intake-addressed
    # message must never surface to the RFQ reader). Always populated; only the
    # flag-gated filter acts on it.
    to_addresses: list = field(default_factory=list)


@dataclass
class BounceNotice:
    """One parsed bounce/delivery-failure, normalized for matching.

    failed_recipient — the address that failed (from the DSN Final-Recipient).
    message_id / thread_id — identifiers of the ORIGINAL outbound message, if
      recoverable from the DSN, used to match back to a sent_messages row.
    reason — the human/diagnostic text (for logging / human review).
    is_hard — True for a permanent failure (Action: failed / 5.x.x); False for a
      soft/transient failure (Action: delayed / 4.x.x). Only hard bounces clear.
    status_code — the DSN status (e.g. "5.1.1"), when present.
    """
    failed_recipient: str
    message_id: Optional[str] = None
    thread_id: Optional[str] = None
    reason: Optional[str] = None
    is_hard: bool = True
    status_code: Optional[str] = None


class InboxReader(ABC):
    """Swappable inbound-read interface. Implementations must be fail-soft:
    fetch_bounces() returns a list (possibly empty) and never raises into the
    caller (except the deliberately-unwired live Gmail branch, which is gated off)."""

    @abstractmethod
    def fetch_bounces(self) -> list[BounceNotice]:
        ...

    def fetch_replies(self) -> list["ReplyNotice"]:
        """Inbound REPLIES to sent RFQs (Layer 3). Concrete default returns [] so
        existing bounce-only readers remain valid; reply-capable readers override."""
        return []


class GmailInboxReader(InboxReader):
    """Gmail-backed bounce reader. The real Gmail read is STUBBED (not wired).

    While EMAIL_SEND_ENABLED is False or no credentials are configured,
    fetch_bounces() returns [] and touches no network. The live branch (flag True +
    creds) is unimplemented and raises NotImplementedError — wiring it (querying
    mailer-daemon DSNs and parsing them via utils.bounce_parser.parse_bounce) is the
    deliberate next step.
    """

    def __init__(self, credentials: Optional[object] = None,
                 service: Optional[object] = None):
        # `credentials` retained for interface back-compat; the real path builds a
        # service from env (gmail_client) or uses an injected `service` (tests).
        self._credentials = credentials
        self._service = service

    @property
    def configured(self) -> bool:
        """True only when the gate is on AND a read path is available (injected
        service/credentials, or env-configured credentials)."""
        env = any(os.environ.get(v) for v in (
            "GMAIL_SERVICE_ACCOUNT_JSON", "GMAIL_SERVICE_ACCOUNT_FILE", "GMAIL_OAUTH_TOKEN_FILE"))
        return bool(email_sender.EMAIL_SEND_ENABLED and (self._service or self._credentials or env))

    # ------------------------------------------------------------------
    # Live Gmail reads. Gated like send (flag off / no creds -> [], zero network).
    # The parsing/matching/extraction logic (Layers 2/3) is UNCHANGED — these only
    # feed real inbox messages into it. Fail-soft: any error returns what we have.
    # ------------------------------------------------------------------

    def _service_or_none(self, kind: str):
        if not email_sender.EMAIL_SEND_ENABLED:
            print(f"[InboxReader] STUBBED (EMAIL_SEND_ENABLED=False) -> 0 {kind}")
            return None
        service = self._service or gmail_client.build_gmail_service()
        if service is None:
            print(f"[InboxReader] no Gmail credentials -> 0 {kind}")
        return service

    @staticmethod
    def _get_raw(service, msg_id: str) -> tuple:
        """Fetch a message as raw RFC822 text + its Gmail threadId. (None, None) on error."""
        try:
            resp = service.users().messages().get(
                userId="me", id=msg_id, format="raw").execute()
            raw_b64 = resp.get("raw")
            if not raw_b64:
                return None, None
            text = base64.urlsafe_b64decode(raw_b64).decode("utf-8", errors="replace")
            return text, resp.get("threadId")
        except Exception as exc:
            print(f"[InboxReader] get message {msg_id} failed: {type(exc).__name__}: {exc}")
            return None, None

    def fetch_bounces(self) -> list[BounceNotice]:
        from utils.bounce_parser import parse_bounce  # lazy (parser imports BounceNotice)
        service = self._service_or_none("bounces")
        if service is None:
            return []
        notices: list[BounceNotice] = []
        try:
            q = 'from:mailer-daemon OR from:postmaster subject:"Delivery Status Notification"'
            listed = service.users().messages().list(userId="me", q=q).execute()
            for m in listed.get("messages", []) or []:
                raw, _thread = self._get_raw(service, m.get("id"))
                if not raw:
                    continue
                notice = parse_bounce(raw)
                if notice:
                    notices.append(notice)
        except Exception as exc:
            print(f"[InboxReader] fetch_bounces failed: {type(exc).__name__}: {exc}")
        return notices

    @staticmethod
    def _parse_reply(raw: str, thread_id: Optional[str]) -> Optional[ReplyNotice]:
        """Parse a raw RFC822 reply into a ReplyNotice (sender, ids, body, attachments).
        Pure/testable. None on a malformed/sender-less message."""
        try:
            msg = _email.message_from_string(raw)
        except Exception:
            return None
        sender = parseaddr(msg.get("From") or "")[1]
        if not sender:
            return None
        # Recipient set for stream attribution (T5): To/Cc plus the delivery
        # headers Gmail stamps — Delivered-To survives plus-address routing even
        # when the visible To differs.
        to_addresses: list = []
        for header in ("To", "Cc", "Delivered-To", "X-Original-To"):
            for _, addr in getaddresses(msg.get_all(header) or []):
                if addr and addr not in to_addresses:
                    to_addresses.append(addr)

        def _strip(v):
            return (v or "").strip().strip("<>").strip() or None

        body = ""
        attachments: list = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                ctype = part.get_content_type()
                disp = (part.get("Content-Disposition") or "").lower()
                filename = part.get_filename()
                if filename or "attachment" in disp:
                    attachments.append({"filename": filename, "content_type": ctype,
                                        "data": part.get_payload(decode=True)})
                elif ctype == "text/plain" and not body:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode(part.get_content_charset() or "utf-8",
                                              errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            body = (payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
                    if payload else (msg.get_payload() or ""))

        return ReplyNotice(
            sender=sender,
            message_id=_strip(msg.get("Message-ID")),
            thread_id=thread_id,
            in_reply_to=_strip(msg.get("In-Reply-To")),
            subject=msg.get("Subject"),
            body=body,
            attachments=attachments,
            to_addresses=to_addresses,
        )

    def fetch_replies(self) -> list[ReplyNotice]:
        service = self._service_or_none("replies")
        if service is None:
            return []
        notices: list[ReplyNotice] = []
        try:
            # Inbox messages that aren't bounces (DSNs are handled by fetch_bounces).
            q = "in:inbox -from:mailer-daemon -from:postmaster"
            # SEND_GOVERNANCE_V1 (T5) — cross-stream isolation: scope the RFQ
            # reader to ITS OWN stream. Two layers, belt and braces:
            #   1. Gmail-side: narrow the query to mail addressed to the RFQ
            #      sending identity (replies come back to the address we sent from).
            #   2. Post-parse (below): drop anything addressed to an intake
            #      address — intake mail must NEVER surface as an RFQ reply, even
            #      if the provider-side query lets it through.
            # Flag OFF ⇒ the query string and behavior are byte-identical to before.
            from utils import send_governance
            governed = send_governance.send_governance_active()
            if governed:
                q += f" to:{gmail_client.gmail_sender_address()}"
            listed = service.users().messages().list(userId="me", q=q).execute()
            for m in listed.get("messages", []) or []:
                raw, thread_id = self._get_raw(service, m.get("id"))
                if not raw:
                    continue
                notice = self._parse_reply(raw, thread_id)
                if not notice:
                    continue
                if governed and any(_is_intake_address(a) for a in notice.to_addresses):
                    print(f"[InboxReader] DROPPED cross-stream (intake-addressed) "
                          f"message from {notice.sender} — not an RFQ reply")
                    continue
                notices.append(notice)
        except Exception as exc:
            print(f"[InboxReader] fetch_replies failed: {type(exc).__name__}: {exc}")
        return notices
