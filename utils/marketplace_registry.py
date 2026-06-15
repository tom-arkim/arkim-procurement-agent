"""
utils/marketplace_registry.py — CURATED allowlist of transactable marketplaces.

A "marketplace" here means a site where a buyer can purchase the part DIRECTLY online
(live price → add-to-cart / checkout), as opposed to a reference listing whose price
we merely read off the page. State M ("buy now") in the customer Options view is gated
on membership in this list.

  ┌────────────────────────────────────────────────────────────────────────────┐
  │  THIS LIST IS CURATED AND MANUALLY MAINTAINED.                               │
  │  Each entry is an explicit assertion that "you can buy directly at this      │
  │  domain." A wrong entry produces a wrong "buy here now" claim, so:           │
  │    • add / remove a marketplace by editing the ONE-LINE entries in           │
  │      _MARKETPLACE_DOMAINS below — nothing else needs to change;              │
  │    • keep it obviously curated — do NOT auto-populate it from discovery.     │
  └────────────────────────────────────────────────────────────────────────────┘

Detection is registry-only by design — no commerce-signal / add-to-cart page parsing.
That heuristic is a later refinement and is deliberately NOT done here.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

# --- The curated allowlist. Add or remove a marketplace by editing one line. ------
_MARKETPLACE_DOMAINS: set[str] = {
    # Broad industrial / MRO marketplaces (buy online directly)
    "grainger.com",
    "mcmaster.com",
    "mscdirect.com",
    "zoro.com",
    "globalindustrial.com",
    "fastenal.com",
    "motionindustries.com",
    "applied.com",
    # Niche seal marketplaces observed live (Gusher mechanical-seal sourcing)
    "sealit123.com",
    "seals-direct.com",
    "allseals.com",
}


def _domain_of(url: str) -> str:
    """Normalized host of a URL: lowercase, scheme-optional, leading 'www.' stripped.
    Returns '' for empty/unparseable input."""
    if not url:
        return ""
    raw = url.strip().lower()
    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = parsed.hostname or ""
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_marketplace(url: Optional[str]) -> bool:
    """True iff the URL's domain is a registered transactable marketplace.

    Matches the registered domain itself and any subdomain of it (e.g.
    shop.grainger.com), tolerant of www./scheme/path. Does NOT over-match a different
    domain that merely contains a registered string — "notgrainger.com" and
    "grainger.com.evil.com" both return False.
    """
    host = _domain_of(url or "")
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in _MARKETPLACE_DOMAINS)
