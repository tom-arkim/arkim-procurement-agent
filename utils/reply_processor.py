"""
utils/reply_processor.py
Inbound reply ingestion + the human-review gate — Layer 3.

process_replies(reader) reads replies (Gmail STUBBED), matches each to its sent_messages
row (utils.reply_matcher), extracts a quote and/or a nominated contact, and QUEUES the
results for human review (utils.supplier_registry.record_review_item). It updates
NOTHING on the platform.

A human then applies or discards:
  confirm_quote(item_id)   -> price_db.save_price(..., source="rfq")
  confirm_contact(item_id) -> upsert_primary_contact(status="resolved",
                                                      source="supplier_nominated")
  reject(item_id)          -> discard (status="rejected")

Conservative (wrong-attribution / no-auto-update caution):
  - unmatched reply        -> queued as kind="unmatched_reply", needs_human_review, with
                              NO run/candidate attribution (never guess the RFQ). It is
                              recorded, not dropped — a supplier reply must not vanish.
  - junk / no quote        -> no quote queued (extractor returns None, no hallucination).
  - low-confidence extract -> queued as "needs_human_review" (not "pending"), never
                              applied without a human.
LLM / OCR / Gmail are injectable + mocked in tests; nothing here makes a live call by
default in tests (the autouse key-neutralizer disables the default clients).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Callable, Optional

from utils import price_db, supplier_registry
from utils.contact_extractor import extract_nominated_contact
from utils.inbox_reader import GmailInboxReader, InboxReader
from utils.quote_extractor import extract_quote
from utils.reply_matcher import match_reply


def _default_specs_lookup(run_id: Optional[str]) -> dict:
    """Best-effort {manufacturer, part_number} for a run, from the audit log. Fail-soft
    -> {} (a confirmed quote then needs the part identity supplied at confirm time)."""
    if not run_id:
        return {}
    try:
        from utils.audit_log import get_entry
        entry = get_entry(run_id) or {}
        specs = entry.get("asset_specs_json")
        if isinstance(specs, str):
            specs = json.loads(specs or "{}")
        specs = specs or {}
        return {"manufacturer": specs.get("manufacturer"),
                "part_number": specs.get("part_number")}
    except Exception:
        return {}


def process_replies(
    reader: Optional[InboxReader] = None,
    *,
    complete: Optional[Callable[[str, str], str]] = None,
    ocr_text: Optional[Callable[[dict], str]] = None,
    specs_lookup: Optional[Callable[[Optional[str]], dict]] = None,
) -> dict:
    """Read replies, match, extract, and QUEUE for review. Returns a summary dict:
      processed, queued_quotes, queued_contacts, needs_review, unmatched.
    Applies nothing to the platform (that's confirm_quote / confirm_contact)."""
    reader = reader or GmailInboxReader()
    specs_lookup = specs_lookup or _default_specs_lookup
    notices = reader.fetch_replies()
    rows = supplier_registry.get_sent_messages()

    summary: dict = {"processed": 0, "queued_quotes": 0, "queued_contacts": 0,
                     "needs_review": 0, "queued_unmatched": 0, "unmatched": []}

    for notice in notices:
        summary["processed"] += 1
        row = match_reply(notice, rows)
        if not row:
            # GAP FIX (State C 3a): a reply we cannot confidently match (sender domain we
            # never emailed) used to be logged-and-dropped. Queue it for human review
            # instead — a supplier reply must not silently vanish once sends are live —
            # but DO NOT auto-attribute it to a run/candidate (it has no verified thread).
            print(f"[ReplyProcessor] UNMATCHED reply from {notice.sender!r} -> human review")
            summary["unmatched"].append(notice.sender)
            sender = notice.sender or ""
            sender_domain = (supplier_registry._normalize_domain(sender.rsplit("@", 1)[-1])
                             if "@" in sender else "")
            supplier_registry.record_review_item(
                "unmatched_reply",
                {"sender": notice.sender, "sender_domain": sender_domain,
                 "thread_id": notice.thread_id, "message_id": notice.message_id,
                 "in_reply_to": notice.in_reply_to, "subject": notice.subject,
                 "body": notice.body},
                status="needs_human_review",
                run_id=None, supplier_domain=None, vendor_name=None,  # NOT attributed
                raw_source="reply",
                thread_id=notice.thread_id, message_id=notice.message_id,
            )
            summary["queued_unmatched"] += 1
            summary["needs_review"] += 1
            continue

        run_id = row.get("run_id")
        domain = row.get("supplier_domain")
        vendor = row.get("vendor_name")
        # SEND_GOVERNANCE_V1 (T6 ledger): a matched reply transitions its outbound
        # row to "replied" (frees the per-part open-RFQ cap slot; feeds the daily
        # digest). Flag OFF: rows untouched (byte-identical).
        from utils.send_governance import send_governance_active
        if send_governance_active() and row.get("id"):
            supplier_registry.update_sent_message_status(row["id"], "replied")
        specs = specs_lookup(run_id) or {}
        # Deterministic-join keys carried from the matched outbound (State C 3a) — the data
        # is in hand at the match; previously it was dropped at the forward below.
        thread_id = row.get("thread_id")
        sent_message_id = row.get("id")
        message_id = row.get("message_id")

        quote = extract_quote(notice, complete=complete, ocr_text=ocr_text)
        if quote is not None:
            status = "needs_human_review" if quote.needs_human_review else "pending"
            supplier_registry.record_review_item(
                "quote", asdict(quote), status=status, run_id=run_id,
                supplier_domain=domain, vendor_name=vendor,
                manufacturer=specs.get("manufacturer"), part_number=specs.get("part_number"),
                confidence=quote.confidence, raw_source=quote.raw_source,
                thread_id=thread_id, sent_message_id=sent_message_id, message_id=message_id,
            )
            summary["queued_quotes"] += 1
            if status == "needs_human_review":
                summary["needs_review"] += 1

        contact = extract_nominated_contact(notice, complete=complete)
        if contact is not None:
            status = "needs_human_review" if contact.needs_human_review else "pending"
            supplier_registry.record_review_item(
                "contact", asdict(contact), status=status, run_id=run_id,
                supplier_domain=domain, vendor_name=vendor,
                confidence=contact.confidence, raw_source="reply",
                thread_id=thread_id, sent_message_id=sent_message_id, message_id=message_id,
            )
            summary["queued_contacts"] += 1
            if status == "needs_human_review":
                summary["needs_review"] += 1

    return summary


def _load_open_item(item_id: str, kind: str) -> Optional[dict]:
    item = supplier_registry.get_review_item(item_id)
    if not item:
        print(f"[ReplyProcessor] confirm: review item {item_id!r} not found")
        return None
    if item.get("kind") != kind:
        print(f"[ReplyProcessor] confirm: item {item_id!r} is {item.get('kind')}, not {kind}")
        return None
    if item.get("status") not in ("pending", "needs_human_review"):
        print(f"[ReplyProcessor] confirm: item {item_id!r} already {item.get('status')}")
        return None
    return item


def confirm_quote(item_id: str) -> bool:
    """Human-confirm a queued quote -> apply to price_db (source="rfq"). Needs the part
    identity (manufacturer + part_number) on the item to key pricing."""
    item = _load_open_item(item_id, "quote")
    if not item:
        return False
    mfg, pn = item.get("manufacturer"), item.get("part_number")
    if not (mfg and pn):
        print(f"[ReplyProcessor] confirm_quote {item_id!r}: missing manufacturer/part_number "
              f"-> cannot key price_db (left for manual entry)")
        return False
    payload = item.get("payload") or {}
    price = payload.get("unit_price")
    if price is None:
        return False
    price_db.save_price(mfg, pn, item.get("vendor_name") or "Unknown", float(price),
                        lead_days=None, source="rfq")
    supplier_registry.set_review_item_status(item_id, "confirmed")
    print(f"[ReplyProcessor] CONFIRMED quote -> price_db: {mfg} {pn} @ {price} "
          f"({item.get('vendor_name')})")
    return True


def confirm_contact(item_id: str) -> bool:
    """Human-confirm a nominated contact -> upgrade the supplier's primary contact to a
    resolved primary (source="supplier_nominated")."""
    item = _load_open_item(item_id, "contact")
    if not item:
        return False
    domain = item.get("supplier_domain")
    payload = item.get("payload") or {}
    email = payload.get("email")
    if not (domain and email):
        print(f"[ReplyProcessor] confirm_contact {item_id!r}: missing domain/email")
        return False
    supplier_registry.upsert_primary_contact(domain, {
        "primary_contact_email":  email,
        "primary_contact_name":   payload.get("name"),
        "primary_contact_title":  payload.get("position"),
        "primary_contact_source": "supplier_nominated",
        "primary_contact_status": "resolved",
    })
    supplier_registry.set_review_item_status(item_id, "confirmed")
    print(f"[ReplyProcessor] CONFIRMED contact -> primary: {email} @ {domain}")
    return True


def reject(item_id: str) -> bool:
    """Human-reject a queued item -> discard (no platform change)."""
    item = supplier_registry.get_review_item(item_id)
    if not item or item.get("status") not in ("pending", "needs_human_review"):
        return False
    return supplier_registry.set_review_item_status(item_id, "rejected")
