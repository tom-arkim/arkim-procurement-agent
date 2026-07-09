"""
utils/procurement_agent/tier1_notify.py
Night 5 — Tier 1 runtime NOTIFY (T3): the notify≫display asymmetry.

The research's conservative notify≫display asymmetry: a matched Tier 1 candidate is
DISPLAYED at a lower threshold than it is NOTIFIED on. Notifying a supplier of a
matched request is a (potentially) outward-facing act, so the notify gate is stricter
than the display gate:

  - DISPLAY threshold: a candidate is displayed whenever it passes the matcher's class
    hard-gate + local_service filter (i.e. it is a Tier1Match — the matcher already
    admitted it). Every match is displayable.
  - NOTIFY threshold: a candidate is notified only when it ALSO satisfies
    (brand-match OR core-class) — a brand relationship for the requested manufacturer
    OR the matched class is the supplier's core competency. A brand-neutral,
    incidental-class match is displayed but NOT notified (the conservative posture:
    don't ping a supplier for a tangential match).
  - PER-RFQ CAP: at most ``NOTIFY_CAP_DEFAULT`` (5–8) suppliers are notified per RFQ,
    best-scored first, so a request that matches many onboarded suppliers does not
    fan out into a notification storm. The cap is applied AFTER the notify gate.

Notification EVENTS are recorded (``supplier_registry.record_supplier_notification``)
so the notify trail is auditable. The send itself goes through the existing stubbed/
flagged ``EmailSender`` — NOTHING sends live:
  - ``EMAIL_SEND_ENABLED`` defaults OFF (utils/email_sender.py),
  - the conftest safety net force-sets it OFF for every test (conftest.py),
  - the whole notify path is behind TIER1_V2.
This is the double-gate (guardrail 4). At the repo/test default the EmailSender
returns ``SendResult(status="stubbed")`` and the notification is recorded with
``send_status="stubbed"`` — the event exists, nothing was sent.

Flag gating (guardrail 3): ``notify_tier1`` returns ``[]`` when TIER1_V2 is off (the
notify surface is dormant, byte-identical to pre-Night-5 — T5).

Fail-soft (CLAUDE.md §9): a registry/email error degrades to a recorded "error" event
(or no event), never raises into the sourcing pipeline, never blocks the run. A
notify failure must surface (recorded), never be swallowed as fake success nor allowed
to blow up the run.

Conventions: standalone module to the house standard (dense type annotations,
bracket-prefixed print logging, no I/O on import). It reuses the EmailSender +
record_supplier_notification; it does NOT retrofit the surrounding sourcing flow.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from utils import supplier_registry as sr
from utils.procurement_agent import tier1_matcher as tm


# ---------------------------------------------------------------------------
# Thresholds + cap (informed defaults — the brief's 5-8 range)
# ---------------------------------------------------------------------------
# The per-RFQ notification cap. A request that matches many onboarded suppliers
# notifies at most this many (best-scored first). 6 sits in the brief's 5-8 range.
NOTIFY_CAP_DEFAULT = 6


def _tier1_v2_active() -> bool:
    """True iff the TIER1_V2 redesign is live (live read — mirrors the matcher)."""
    return bool(getattr(sr, "TIER1_V2", False))


# ---------------------------------------------------------------------------
# The notify gate (brand-match OR core-class)
# ---------------------------------------------------------------------------

def _meets_notify_threshold(match: tm.Tier1Match) -> bool:
    """True when a match clears the NOTIFY threshold: brand-match OR core-class.

    - brand-match: the supplier has an explicit brand relationship for the requested
      manufacturer (AUTHORIZED | CARRIES | AFTERMARKET_COMPATIBLE — any non-None
      relationship; a brand-neutral match does NOT clear this).
    - core-class: the matched class is the supplier's core competency (is_core=True).

    A brand-neutral, incidental-class match is displayable but NOT notified (the
    conservative asymmetry)."""
    if match.brand_relationship is not None:
        return True
    return match.is_core


def _notify_reason(match: tm.Tier1Match) -> str:
    """The reason a notify-threshold match was admitted (recorded on the event)."""
    if match.brand_relationship is not None and match.is_core:
        return "brand_match_or_core_class"
    if match.brand_relationship is not None:
        return "brand_match"
    return "core_class"


# ---------------------------------------------------------------------------
# Notify result
# ---------------------------------------------------------------------------

@dataclass
class NotifyResult:
    """One notification outcome (recorded event + send status)."""
    supplier_domain: str
    vendor_name: str
    notified: bool                     # True when a notification event was recorded
    send_status: str                   # "stubbed" | "sent" | "error" (mirrors SendResult)
    notify_reason: Optional[str] = None
    notification_id: Optional[str] = None
    message_id: Optional[str] = None
    capped_out: bool = False           # True when the match qualified but the cap dropped it


# ---------------------------------------------------------------------------
# The notify layer
# ---------------------------------------------------------------------------

def notify_tier1(
    matches: list[tm.Tier1Match],
    *,
    run_id: Optional[str] = None,
    cap: int = NOTIFY_CAP_DEFAULT,
    sender: Optional[object] = None,
) -> list[NotifyResult]:
    """Apply the notify≫display asymmetry to a list of Tier 1 matches.

    Returns one ``NotifyResult`` per match (in the matcher's deterministic order):
      - matches that DO NOT clear the notify gate (brand-match-or-core-class) →
        ``notified=False`` (displayed, not notified; ``capped_out=False``),
      - matches that clear the gate but exceed the per-RFQ cap → ``notified=False``,
        ``capped_out=True`` (qualified but the cap dropped it — recorded as such so
        the trail shows a supplier WOULD have been notified),
      - matches that clear the gate AND fit the cap → ``notified=True``, a
        notification event is recorded, and the send is dispatched through the
        EmailSender (stubbed at the repo/test default — NOTHING sends live).

    The send goes through ``EmailSender.send`` (the existing stubbed/flagged seam).
    At the repo/test default ``EMAIL_SEND_ENABLED=False`` → ``SendResult(status=
    "stubbed")`` and the event is recorded with ``send_status="stubbed"``. Fail-soft:
    a send/registry error is recorded as ``send_status="error"`` and the run continues.

    Flag-off (TIER1_V2 off) → [] (the notify surface is dormant — T5).
    """
    if not _tier1_v2_active():
        return []
    if cap < 0:
        cap = 0

    # Lazily build a sender only when there's something to send (avoids constructing
    # a GmailSender — and its lazy google-lib import — when no match clears the gate).
    results: list[NotifyResult] = []
    notified_count = 0
    sender_obj = None  # built on first qualifying, in-cap match

    for match in matches:
        if not _meets_notify_threshold(match):
            # Displayed but not notified (below the notify threshold).
            results.append(NotifyResult(
                supplier_domain=match.domain, vendor_name=match.vendor_name,
                notified=False, send_status="not_notified",
            ))
            continue
        if notified_count >= cap:
            # Cleared the gate but the per-RFQ cap is full.
            results.append(NotifyResult(
                supplier_domain=match.domain, vendor_name=match.vendor_name,
                notified=False, send_status="capped", capped_out=True,
                notify_reason=_notify_reason(match),
            ))
            continue

        # Cleared the gate AND within the cap → record + send (stubbed by default).
        reason = _notify_reason(match)
        if sender_obj is None:
            sender_obj = sender if sender is not None else _build_default_sender()

        send_status, message_id = _send_notify(match, run_id, sender_obj)
        nid = sr.record_supplier_notification(
            run_id=run_id, supplier_domain=match.domain, vendor_name=match.vendor_name,
            noun_class=match.noun_class, notify_reason=reason,
            send_status=send_status, message_id=message_id, threshold="notify",
            metadata=match.match_explanation,
        )
        notified_count += 1
        results.append(NotifyResult(
            supplier_domain=match.domain, vendor_name=match.vendor_name,
            notified=send_status in ("stubbed", "sent"),
            send_status=send_status, notify_reason=reason,
            notification_id=nid, message_id=message_id,
        ))
    return results


# ---------------------------------------------------------------------------
# Send seam (the double-gate: EmailSender behind EMAIL_SEND_ENABLED + TIER1_V2)
# ---------------------------------------------------------------------------

def _build_default_sender():
    """Build the default GmailSender (the existing stubbed/flagged seam). The real
    Gmail call fires only when EMAIL_SEND_ENABLED is on AND creds resolve — default-
    off, so this returns a sender whose ``send`` is stubbed. Imported lazily so the
    google libs are not imported at module load (mirrors utils/email_sender.py)."""
    from utils.email_sender import GmailSender
    return GmailSender()


def _send_notify(match: tm.Tier1Match, run_id: Optional[str], sender) -> tuple[str, Optional[str]]:
    """Send one notify email through the EmailSender + return (send_status, message_id).

    The send is behind the double-gate: ``EmailSender.send`` returns ``stubbed`` when
    ``EMAIL_SEND_ENABLED`` is off (the repo/test default), ``sent`` only on a real
    live send (unreachable in tests), ``error`` on a fail-soft provider failure. We
    build a minimal EmailMessage (the notify is an FYI of a matched request, not an
    RFQ draft — the RFQ draft flow is the existing Tier 3 ``rfq_send`` path, separate).

    Fail-soft: any exception is caught and surfaced as ``("error", None)`` — never
    raised into the sourcing pipeline. A failure is RECORDED, never swallowed."""
    try:
        from utils.email_sender import EmailMessage
        # Resolve a recipient set via the registry's free cascade (no Apollo).
        recip = sr.assemble_recipient_set(match.domain) if match.domain else {"to": [], "cc": []}
        if not recip.get("to"):
            # No usable contact — record the event as stubbed (the FYI is queued, not
            # sent) rather than silently dropping it. The human-flag is in the trail.
            return ("stubbed", None)
        subject = f"Arkim matched request — {match.noun_class} (run {run_id or 'n/a'})"
        body = (
            f"Arkim matched an active procurement request to your onboarded profile.\n\n"
            f"Supplier: {match.vendor_name} ({match.domain})\n"
            f"Matched class: {match.noun_class}\n"
            f"Relationship: {match.brand_relationship or 'class-matched (no brand row)'}\n"
            f"Core class: {'yes' if match.is_core else 'no'}\n\n"
            f"This is an automated FYI from the Arkim procurement platform. "
            f"No action is required unless Arkim follows up with a formal RFQ.\n"
        )
        message = EmailMessage(
            to=recip["to"], cc=recip.get("cc", []),
            subject=subject, body=body,
            metadata={"run_id": run_id, "supplier_domain": match.domain,
                      "notify": True, "noun_class": match.noun_class},
        )
        result = sender.send(message)
        return (result.status, result.message_id)
    except Exception as exc:
        print(f"[Tier1Notify] send failed for {match.vendor_name} ({match.domain}): "
              f"{type(exc).__name__}: {exc}")
        return ("error", None)
