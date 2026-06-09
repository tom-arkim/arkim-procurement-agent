"""
utils/bounce_parser.py
Pure DSN (bounce) parser — Layer 2, inbound-bounce only.

parse_bounce(raw_email_text) -> Optional[BounceNotice]

Takes one raw bounce email (RFC 3464 delivery-status notification, e.g. from
mailer-daemon@googlemail.com) and extracts:
  - the failed recipient address (Final-/Original-Recipient),
  - the ORIGINAL message's Message-ID if recoverable (from the attached original
    message/headers, else the DSN's In-Reply-To/References), used to match a
    sent_messages row,
  - a hard-vs-soft classification (Action: failed / 5.x.x => hard; Action: delayed /
    4.x.x => soft/transient).

Pure function, no I/O, no network. Robust by contract:
  - a non-bounce message (no DSN recipient/action) -> None (never a false bounce),
  - a malformed DSN -> None (never crashes, never fabricates a recipient).

Conventions mirror the sibling modules (typed; the BounceNotice shape is shared
with utils/inbox_reader.py).
"""

from __future__ import annotations

import re
from email import message_from_string
from email.message import Message
from typing import Optional

from utils.inbox_reader import BounceNotice

# DSN fields are line-oriented within the message/delivery-status block.
_RECIP_RE = re.compile(r'^(?:Final|Original)-Recipient:\s*(?:[^;]*;)?\s*(.+?)\s*$', re.I | re.M)
_ACTION_RE = re.compile(r'^Action:\s*([A-Za-z]+)', re.I | re.M)
_STATUS_RE = re.compile(r'^Status:\s*([0-9]+\.[0-9]+\.[0-9]+)', re.I | re.M)
_DIAG_RE = re.compile(r'^Diagnostic-Code:\s*(.+?)\s*$', re.I | re.M)
_MID_RE = re.compile(r'^Message-ID:\s*(.+?)\s*$', re.I | re.M)


def _strip_brackets(mid: Optional[str]) -> Optional[str]:
    if not mid:
        return None
    return mid.strip().strip("<>").strip() or None


def _delivery_status_text(msg: Message) -> str:
    """Concatenate the text of any message/delivery-status parts (the structured
    DSN fields live here). Empty string when none found."""
    chunks: list[str] = []
    for part in msg.walk():
        if part.get_content_type() == "message/delivery-status":
            payload = part.get_payload()
            if isinstance(payload, list):
                for sub in payload:
                    chunks.append(sub.as_string() if isinstance(sub, Message) else str(sub))
            elif isinstance(payload, str):
                chunks.append(payload)
    return "\n".join(chunks)


def _original_message_id(msg: Message) -> Optional[str]:
    """Recover the ORIGINAL outbound message's Message-ID (NOT the DSN's own).

    Prefer the Message-ID inside the attached original message/headers; fall back to
    the DSN's In-Reply-To / References (which reference the original).
    """
    for part in msg.walk():
        if part.get_content_type() in ("message/rfc822", "text/rfc822-headers", "message/rfc822-headers"):
            payload = part.get_payload()
            if isinstance(payload, list):
                for sub in payload:
                    if isinstance(sub, Message) and sub.get("Message-ID"):
                        return _strip_brackets(sub.get("Message-ID"))
                    text = sub.as_string() if isinstance(sub, Message) else str(sub)
                    m = _MID_RE.search(text)
                    if m:
                        return _strip_brackets(m.group(1))
            elif isinstance(payload, str):
                m = _MID_RE.search(payload)
                if m:
                    return _strip_brackets(m.group(1))
    for hdr in ("In-Reply-To", "References"):
        v = msg.get(hdr)
        if v:
            return _strip_brackets(v.split()[0])
    return None


def parse_bounce(raw: Optional[str]) -> Optional[BounceNotice]:
    """Parse one raw DSN into a BounceNotice, or None if it isn't a recognizable
    bounce (non-bounce or malformed). Never raises."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        msg = message_from_string(raw)
    except Exception:
        return None

    # Structured DSN fields live in the delivery-status part; fall back to the whole
    # raw text if the structure is loose.
    ds_text = _delivery_status_text(msg) or raw

    recip_m = _RECIP_RE.search(ds_text)
    action_m = _ACTION_RE.search(ds_text)
    if not (recip_m and action_m):
        return None  # not a DSN we recognize -> no false bounce
    failed = recip_m.group(1).strip()
    if not failed or "@" not in failed:
        return None  # malformed -> never fabricate a recipient

    action = action_m.group(1).lower()
    status_m = _STATUS_RE.search(ds_text)
    status = status_m.group(1) if status_m else None
    diag_m = _DIAG_RE.search(ds_text)
    reason = diag_m.group(1).strip() if diag_m else None

    # Hard = permanent. Action "failed" is hard unless the status says transient
    # (4.x.x); "delayed" is always soft/transient.
    is_hard = (action == "failed")
    if status and status.startswith("4"):
        is_hard = False
    if action in ("delayed", "delay"):
        is_hard = False

    try:
        message_id = _original_message_id(msg)
    except Exception:
        message_id = None

    return BounceNotice(
        failed_recipient=failed,
        message_id=message_id,
        thread_id=None,
        reason=reason,
        is_hard=is_hard,
        status_code=status,
    )
