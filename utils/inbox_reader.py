"""
utils/inbox_reader.py
Inbound mail READ interface (Layer 2, inbound-bounce only) — provider-agnostic.

The read-side mirror of utils/email_sender.py: it knows how to pull bounce
notifications (DSNs) out of the Arkim sourcing inbox and hand them to the bounce
processor. The provider is Gmail, but the actual Gmail read is STUBBED here — no
live wiring, no network, no real inbox access in this layer (same discipline as
GmailSender).

Gating mirrors send exactly: the live read path is reachable only when the canonical
email_sender.EMAIL_SEND_ENABLED is True AND credentials are present. While gated or
uncredentialled, fetch_bounces() returns [] and touches ZERO network; the live
branch raises NotImplementedError rather than faking a read.

BounceNotice is the shared parsed-bounce shape (the DSN parser in
utils/bounce_parser.py produces these; the processor in utils/bounce_processor.py
consumes them). Conventions mirror the sibling modules (typed, fail-soft,
bracket-prefixed logging).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import utils.email_sender as email_sender


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

    def __init__(self, credentials: Optional[object] = None):
        self._credentials = credentials

    @property
    def configured(self) -> bool:
        """True only when the canonical gate is on AND credentials are present."""
        return bool(email_sender.EMAIL_SEND_ENABLED and self._credentials)

    def fetch_bounces(self) -> list[BounceNotice]:
        if not email_sender.EMAIL_SEND_ENABLED:
            print("[InboxReader] STUBBED (EMAIL_SEND_ENABLED=False) -> 0 bounces")
            return []
        if not self._credentials:
            print("[InboxReader] STUBBED (no Gmail credentials) -> 0 bounces")
            return []
        # Live path — intentionally not wired. Fail loud rather than fake a read.
        raise NotImplementedError(
            "GmailInboxReader live read is not wired yet — live Gmail inbox access "
            "and DSN fetching are a deliberate separate step."
        )

    def fetch_replies(self) -> list[ReplyNotice]:
        """Inbound replies to sent RFQs. STUBBED identically to fetch_bounces:
        [] while gated/uncredentialled (zero network); live branch unwired."""
        if not email_sender.EMAIL_SEND_ENABLED:
            print("[InboxReader] STUBBED (EMAIL_SEND_ENABLED=False) -> 0 replies")
            return []
        if not self._credentials:
            print("[InboxReader] STUBBED (no Gmail credentials) -> 0 replies")
            return []
        raise NotImplementedError(
            "GmailInboxReader live reply read is not wired yet — live Gmail inbox "
            "access and reply threading are a deliberate separate step."
        )
