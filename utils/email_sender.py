"""
utils/email_sender.py
Provider-agnostic outbound email SEND interface (Layer 1, outbound only).

This is the send seam for Tier 3 RFQs. It is deliberately split from drafting and
from the send *flow* (utils/rfq_send.py): this module only knows how to hand a
fully-formed message to a provider and report the result. The provider is Gmail
(a designated Arkim sourcing address), but the actual Gmail API call is STUBBED
here — the live wiring + first real send is a separate, deliberate next step.

Safety (this is the first layer that can take an external action):
  - EMAIL_SEND_ENABLED is the canonical send gate and stays False. This is a fresh
    constant owned by the send layer; the identically-named flag in
    utils/sourcing_archieved/tier3_outreach.py is DEAD code (see CLAUDE.md §6) and
    is intentionally NOT reused. (CLEANUP notes the duplication.)
  - While EMAIL_SEND_ENABLED is False OR no provider credentials are configured, a
    sender returns a STUBBED SendResult and makes ZERO network calls.
  - The real Gmail send is unimplemented: the live branch (flag True + creds) raises
    NotImplementedError so an accidental enable fails loud rather than silently
    pretending to send. No code path here opens a socket.

Conventions mirror utils/apollo_client.py (the sibling standalone provider client):
bracket-prefixed print logging ("[EmailSender] ..."), explicit type annotations,
fail-soft by contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# Canonical outbound send gate. MUST stay False until a deliberate, documented
# enabling decision (legal review of templates + real provider wiring). Owned here.
EMAIL_SEND_ENABLED: bool = False


@dataclass
class EmailMessage:
    """One outbound message addressed to a supplier's recipient set.

    `metadata` carries the keys later inbound matching (bounce/quote ingestion)
    keys on: run_id, supplier_domain, rfq_id. `to`/`cc` are the assembled recipient
    set (named primary in `to`, generic inbox in `cc`, per the recipient-set rule).
    """
    to: list[str]
    subject: str
    body: str
    cc: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

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
    """Gmail-backed sender. The real Gmail API call is STUBBED (not wired).

    While EMAIL_SEND_ENABLED is False or no credentials are configured, send()
    returns a stubbed result and touches no network. The live branch (flag True +
    creds) is unimplemented and raises NotImplementedError — wiring it (and the
    first real send, to ourselves) is the deliberate next step.
    """

    def __init__(self, credentials: Optional[object] = None):
        # No credentials are wired in this layer; the parameter exists so the live
        # provider can be injected later without changing the interface.
        self._credentials = credentials

    @property
    def configured(self) -> bool:
        """True only when both the gate is on AND credentials are present."""
        return bool(EMAIL_SEND_ENABLED and self._credentials)

    def send(self, message: EmailMessage) -> SendResult:
        if not EMAIL_SEND_ENABLED:
            print(f"[EmailSender] STUBBED (EMAIL_SEND_ENABLED=False) -> {message.all_recipients}")
            return SendResult(status="stubbed")
        if not self._credentials:
            print(f"[EmailSender] STUBBED (no Gmail credentials) -> {message.all_recipients}")
            return SendResult(status="stubbed")
        # Live path — intentionally not wired. Fail loud rather than fake a send.
        raise NotImplementedError(
            "GmailSender live send is not wired yet — live Gmail API integration and "
            "the first real send are a deliberate separate step."
        )
