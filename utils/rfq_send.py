"""
utils/rfq_send.py
Tier 3 outbound RFQ SEND flow (Layer 1, outbound only) — HITL-gated.

Turns an APPROVED draft into a sent RFQ:
    existing draft -> HUMAN approval -> send via the EmailSender interface
    -> record sent-message metadata (for later inbound matching).

This is the only place a send is *initiated*. It is deliberately separate from
drafting (utils/procurement_agent/outreach.py) and from the provider
(utils/email_sender.py). The real Gmail send is WIRED via the provider but stays
behind EMAIL_SEND_ENABLED (default-off): at the repo/test default the flag is off,
so NO real emails are sent in this layer.

Hard gates (this is the first layer that can take an external action):
  - HUMAN APPROVAL: a send requires an explicit Approval for that draft. With no
    approval, send_rfq NEVER calls the provider — drafting is automatic, sending is
    not. (No auto-send.)
  - EMAIL_SEND_ENABLED stays False: while False, the provider interface is NOT
    invoked at all (stub path only) — the message is recorded as "stubbed" and the
    vendor is marked "awaiting", matching the existing no-send demo behaviour.
  - When True + approval, the EmailSender is invoked exactly once; the concrete
    GmailSender makes the real Gmail call only if creds resolve — with no creds it
    fail-softs to "error" (no network, no half-send), never a silent stub.

Inbound concerns (bounce detection, quote ingestion) are SEPARATE later layers.
Conventions mirror the sibling util modules (bracket-prefixed print logging,
explicit type annotations, fail-soft).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import utils.email_sender as email_sender
from utils import supplier_registry
from utils.audit_log import write_audit_log
from utils.email_sender import EmailMessage, EmailSender, GmailSender, SendResult


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Approval:
    """A recorded human approval for sending one draft.

    Presence of an Approval is the HITL gate — its absence means "do not send".
    RBAC is NOT enforced at the prototype stage (any approved_by is accepted); this
    records WHO approved for the audit trail, not an authorization check.
    """
    approved_by: str
    approved_at: str = field(default_factory=_now_iso)


def _subject_from_draft(draft: str) -> Optional[str]:
    """Pull a 'Subject: ...' line off the top of a draft, if present."""
    for line in (draft or "").splitlines():
        s = line.strip()
        if s.lower().startswith("subject:"):
            return s.split(":", 1)[1].strip()
        if s:
            break  # first non-blank line isn't a subject -> none
    return None


def _result(
    *, status: str, sent: bool, vendor_name: Optional[str], domain: str,
    recipients: dict, outreach_status: str,
    send_result: Optional[SendResult] = None, sent_message_id: Optional[str] = None,
) -> dict:
    return {
        "sent": sent,
        "status": status,                 # "sent" | "stubbed" | "not_sent_no_approval" | "no_recipients" | "error"
        "vendor_name": vendor_name,
        "domain": domain,
        "recipients": recipients,         # {"to": [...], "cc": [...]}
        "outreach_status": outreach_status,
        "send_result": send_result,
        "sent_message_id": sent_message_id,
    }


def send_rfq(
    candidate: dict,
    approved_draft: str,
    approval: Optional[Approval],
    *,
    run_id: Optional[str] = None,
    sender: Optional[EmailSender] = None,
) -> dict:
    """Send (or stub) one approved Tier 3 RFQ to a supplier's recipient set.

    Recipient set comes from the supplier_registry store record (the source of truth
    that contact resolution / escalation write to): named primary -> To, generic
    inbox -> CC, bounced excluded (supplier_registry.recipient_set).

    Returns a result dict (see _result). Mutates candidate["outreach_status"].
    Fail-soft: a provider failure is captured as status="error", never raised.
    """
    sender = sender or GmailSender()
    vendor_name = candidate.get("vendor_name")
    url = candidate.get("source_url")
    domain = supplier_registry._normalize_domain(url) if url else ""

    record = supplier_registry.lookup_by_domain(domain) if domain else None
    recipients = supplier_registry.recipient_set(record)
    to, cc = recipients["to"], recipients["cc"]

    # ── HITL gate: no recorded approval => never send. ──────────────────────
    if approval is None:
        candidate["outreach_status"] = "pending_approval"
        print(f"[RFQSend] BLOCKED (no human approval) -> {vendor_name} ({domain})")
        return _result(status="not_sent_no_approval", sent=False, vendor_name=vendor_name,
                       domain=domain, recipients=recipients, outreach_status="pending_approval")

    # ── No usable recipient => cannot send; needs human contact resolution. ──
    if not to:
        candidate["outreach_status"] = "needs_human_contact"
        print(f"[RFQSend] No recipients for {vendor_name} ({domain}) -> human contact needed")
        return _result(status="no_recipients", sent=False, vendor_name=vendor_name,
                       domain=domain, recipients=recipients, outreach_status="needs_human_contact")

    rfq_id = str(uuid.uuid4())
    subject = _subject_from_draft(approved_draft) or f"Quote request - {vendor_name or 'part'}"
    message = EmailMessage(
        to=to, cc=cc, subject=subject, body=approved_draft,
        metadata={"run_id": run_id, "supplier_domain": domain, "rfq_id": rfq_id},
    )

    # ── Send path vs stub path. Flag False => provider is NOT invoked. ───────
    if email_sender.EMAIL_SEND_ENABLED:
        try:
            send_result = sender.send(message)
        except Exception as exc:  # fail-soft: a provider error must not crash the flow
            print(f"[RFQSend] provider send failed for {vendor_name} ({domain}): "
                  f"{type(exc).__name__}: {exc}")
            candidate["outreach_status"] = "error"
            return _result(status="error", sent=False, vendor_name=vendor_name, domain=domain,
                           recipients=recipients, outreach_status="error",
                           send_result=SendResult(status="error", error=str(exc)))
    else:
        # Gated: behave like the existing no-send stub WITHOUT touching the provider.
        send_result = SendResult(status="stubbed")

    status = send_result.status                      # "sent" | "stubbed" | "error"
    sent = status == "sent"
    outreach_status = "contacted" if sent else "awaiting"

    # ── Persist the sent-message record (the inbound-matching key). ──────────
    sent_message_id = supplier_registry.record_sent_message(
        run_id=run_id, supplier_domain=domain, vendor_name=vendor_name,
        to=to, cc=cc, subject=subject, body=approved_draft, status=status,
        message_id=send_result.message_id, thread_id=send_result.thread_id,
        approved_by=approval.approved_by, sent_at=send_result.sent_at,
    )

    # ── Human-readable audit event (run-level trail). ────────────────────────
    write_audit_log({
        "sourcing_run_id": run_id,
        "input_summary": (
            f"Tier 3 RFQ {status} -> {vendor_name} ({domain}); to={to} cc={cc}; "
            f"approved_by={approval.approved_by}"
        ),
        "workflow_mode": "tier3_rfq_sent",
        "user_selection": approval.approved_by,
        "final_recommendation": vendor_name,
        "agent_version": "1.0.0-phase3",
    })

    candidate["outreach_status"] = outreach_status
    print(f"[RFQSend] {status.upper()} -> {vendor_name} ({domain}) to={to} cc={cc}")
    return _result(status=status, sent=sent, vendor_name=vendor_name, domain=domain,
                   recipients=recipients, outreach_status=outreach_status,
                   send_result=send_result, sent_message_id=sent_message_id)
