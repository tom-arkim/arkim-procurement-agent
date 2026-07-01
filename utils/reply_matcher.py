"""
utils/reply_matcher.py
Match an inbound REPLY to the sent_messages row it answers — Layer 3.

Reuses Layer 2's matching precedence (see utils/bounce_processor._match_row), adapted
for a reply whose key signal is the SENDER (the supplier writing back), not a failed
recipient:

  1. in_reply_to / message_id -> sent_messages.message_id  (exact; reliable once live
     sends + replies carry real ids)
  2. thread_id                -> sent_messages.thread_id    (exact; reliable once live)
  3. sender address present in the row recipients, else sender DOMAIN == the row's
     supplier_domain (the fallback that works today while ids are placeholders;
     domain-only picks the most recent RFQ to that supplier).

No confident match -> None (caller flags for human; never guess which RFQ).
"""

from __future__ import annotations

from typing import Optional

from utils import supplier_registry
from utils.inbox_reader import ReplyNotice


def _sender_domain(address: str) -> str:
    if not address or "@" not in address:
        return ""
    return supplier_registry._normalize_domain(address.rsplit("@", 1)[-1])


def match_reply(notice: ReplyNotice, rows: list[dict]) -> Optional[dict]:
    """Return the sent_messages row this reply answers, or None (no confident match)."""
    for key in (notice.in_reply_to, notice.message_id):
        if key:
            for r in rows:
                if r.get("message_id") and r["message_id"] == key:
                    return r
    if notice.thread_id:
        for r in rows:
            if r.get("thread_id") and r["thread_id"] == notice.thread_id:
                return r
    sender = (notice.sender or "").lower()
    if sender:
        for r in rows:
            recips = [str(x).lower() for x in
                      (r.get("recipients_to") or []) + (r.get("recipients_cc") or [])]
            if sender in recips:
                return r
        sdomain = _sender_domain(sender)
        if sdomain:
            for r in rows:  # rows are newest-first -> most recent RFQ to this supplier
                if r.get("supplier_domain") == sdomain:
                    return r
    return None
