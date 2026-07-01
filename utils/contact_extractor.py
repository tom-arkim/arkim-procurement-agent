"""
utils/contact_extractor.py
Extract a NOMINATED procurement contact from an inbound RFQ reply — Layer 3.

When a supplier answers the §3c outbound ask ("send these to Jane Smith, Purchasing,
jane@supplier.com"), pull out name / position / email. This is what later UPGRADES a
found_no_email / generic contact record to a resolved primary
(source="supplier_nominated") — but ONLY through human review (utils/reply_processor);
nothing here promotes a contact.

Abstention (same discipline as the quote extractor): no nominated contact -> None (no
fabricated person); a usable nomination REQUIRES an email (a name alone can't become a
resolved primary). Low confidence -> needs_human_review=True.

The LLM `complete(system, user) -> str` is INJECTABLE and fail-soft (mocked in tests;
default is the shared thin local call from quote_extractor).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Optional

from utils.quote_extractor import _default_complete, _to_float, _CONFIDENCE_FLOOR

_CONTACT_SYSTEM = """You extract a NOMINATED procurement contact from a supplier's
email reply -- the person they say future quote/procurement requests should go to.
Return ONLY valid JSON with these exact keys:
{
  "has_contact": boolean,   // false if the reply names no specific contact
  "name":        string or null,
  "position":    string or null,   // e.g. "Purchasing Manager"
  "email":       string or null,
  "confidence":  number     // 0.0-1.0
}
Only set has_contact=true when a real person/contact is named. Never invent a contact.
"""


@dataclass
class NominatedContact:
    name: Optional[str]
    position: Optional[str]
    email: Optional[str]
    confidence: float = 0.0
    needs_human_review: bool = False


def extract_nominated_contact(
    reply,
    *,
    complete: Optional[Callable[[str, str], str]] = None,
) -> Optional[NominatedContact]:
    """Extract a nominated procurement contact, or None. Requires an email to be
    usable. Never raises; never fabricates."""
    complete = complete or _default_complete
    body = getattr(reply, "body", "") or ""
    if not body.strip():
        return None

    raw = complete(_CONTACT_SYSTEM, body) or ""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None

    if not data.get("has_contact") or not data.get("email"):
        return None  # no contact / no email -> not promotable

    conf = _to_float(data.get("confidence")) or 0.0
    return NominatedContact(
        name=data.get("name"),
        position=data.get("position"),
        email=data.get("email"),
        confidence=conf,
        needs_human_review=conf < _CONFIDENCE_FLOOR,
    )
