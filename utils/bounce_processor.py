"""
utils/bounce_processor.py
Bounce matching + drive the bounce model — Layer 2, inbound-bounce only.

process_bounces(reader) pulls BounceNotices from an InboxReader (Gmail read STUBBED),
matches each back to the sent_messages row it came from, and — for HARD, confidently-
matched bounces only — calls supplier_registry.mark_contact_bounced(domain, which) so
the bad address is cleared + re-flagged (and excluded by the next resolution).

Conservative by design (the wrong-attribution caution from the name-consistency work):
  - SOFT/transient bounce -> NOT cleared (may be temporary); logged only.
  - NO confident match -> NOTHING cleared (never guess the wrong supplier's contact);
    logged for human review.
  - Failed address that matches neither the supplier's current primary nor generic
    contact -> NOT cleared (ambiguous); logged for human review.
Only HARD + confidently-matched + address-classified bounces clear a contact.

Match precedence (a bounce -> a sent_messages row):
  1. original message_id  (exact; reliable once live sends carry real ids)
  2. thread_id            (exact; reliable once live)
  3. failed-recipient address present in the row's recipients AND the address domain
     matches the row's supplier_domain (the fallback that works today, while
     message_id/thread_id are placeholders).
"""

from __future__ import annotations

from typing import Optional

from utils import supplier_registry
from utils.inbox_reader import BounceNotice, GmailInboxReader, InboxReader


def _recipient_domain(address: str) -> str:
    """Normalized domain of an email address ('a@Sub.X.com' -> 'sub.x.com')."""
    if not address or "@" not in address:
        return ""
    return supplier_registry._normalize_domain(address.rsplit("@", 1)[-1])


def _match_row(notice: BounceNotice, rows: list[dict]) -> Optional[dict]:
    """Match a bounce to the sent_messages row it originated from (precedence above)."""
    if notice.message_id:
        for r in rows:
            if r.get("message_id") and r["message_id"] == notice.message_id:
                return r
    if notice.thread_id:
        for r in rows:
            if r.get("thread_id") and r["thread_id"] == notice.thread_id:
                return r
    addr = (notice.failed_recipient or "").lower()
    if not addr:
        return None
    addr_domain = _recipient_domain(addr)
    for r in rows:
        recips = [str(x).lower() for x in
                  (r.get("recipients_to") or []) + (r.get("recipients_cc") or [])]
        if addr in recips and (not addr_domain or r.get("supplier_domain") == addr_domain):
            return r
    return None


def _classify_which(failed_address: str, domain: str) -> Optional[str]:
    """Decide whether the failed address is the supplier's PRIMARY or GENERIC contact,
    by comparing to the registry record's CURRENT contacts. Returns "primary" |
    "generic", or None when it matches neither (ambiguous -> do not clear)."""
    record = supplier_registry.lookup_by_domain(domain)
    if not record:
        return None
    a = (failed_address or "").lower()
    primary = (record.get("primary_contact_email") or "").lower()
    generic = (record.get("contact_email") or "").lower()
    if primary and a == primary:
        return "primary"
    if generic and a == generic:
        return "generic"
    return None


def process_bounces(reader: Optional[InboxReader] = None) -> dict:
    """Fetch + process bounces. Returns a summary dict:
      processed     int
      cleared       list[{domain, which, address}]   (hard + matched + classified)
      soft_skipped  list[address]                    (transient; not cleared)
      unmatched     list[address]                    (no confident match; human review)
      ambiguous     list[{address, domain}]          (matched, but address != current
                                                       primary/generic; not cleared)
    """
    reader = reader or GmailInboxReader()
    notices = reader.fetch_bounces()
    rows = supplier_registry.get_sent_messages()  # all sent messages, newest first

    summary: dict = {"processed": 0, "cleared": [], "soft_skipped": [],
                     "unmatched": [], "ambiguous": []}

    for notice in notices:
        summary["processed"] += 1
        addr = notice.failed_recipient

        row = _match_row(notice, rows)
        if not row:
            print(f"[BounceProcessor] UNMATCHED bounce for {addr!r} -> human review (no clear)")
            summary["unmatched"].append(addr)
            continue

        if not notice.is_hard:
            print(f"[BounceProcessor] SOFT/transient bounce for {addr!r} "
                  f"(status={notice.status_code}) -> not cleared")
            summary["soft_skipped"].append(addr)
            continue

        domain = row.get("supplier_domain") or _recipient_domain(addr)
        which = _classify_which(addr, domain)
        if which is None:
            print(f"[BounceProcessor] AMBIGUOUS hard bounce for {addr!r} @ {domain} "
                  f"(matches neither current primary nor generic) -> human review (no clear)")
            summary["ambiguous"].append({"address": addr, "domain": domain})
            continue

        supplier_registry.mark_contact_bounced(domain, which=which)
        # SEND_GOVERNANCE_V1 (T6 ledger): stamp the matched outbound row "bounced"
        # so the daily digest sees it. Flag OFF: rows untouched (byte-identical).
        from utils.send_governance import send_governance_active
        if send_governance_active() and row.get("id"):
            supplier_registry.update_sent_message_status(row["id"], "bounced")
        print(f"[BounceProcessor] HARD bounce for {addr!r} @ {domain} -> cleared {which} contact")
        summary["cleared"].append({"domain": domain, "which": which, "address": addr})

    return summary
