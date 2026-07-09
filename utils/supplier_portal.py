"""
utils/supplier_portal.py
Night 6 - Supplier claim-portal read + demand-teaser helpers (T2).

Pure-data helpers the public portal routes (api_server `/api/portal/*`) call.
Keeps the portal's read logic in one standalone, typed, tested module (house
standard) so the api_server handlers stay thin.

  - ``read_profile(supplier_domain)`` - the editable profile the supplier sees:
    brands (tri-state relationship, the centerpiece), classes, ship_area, and
    the aftermarket disclosure. NEVER includes lifecycle / performance / other
    suppliers (the brief's decision 4). Read-only.
  - ``demand_teaser(supplier_domain, window_days)`` - the read-only, honest,
    time-windowed aggregate count of GENUINE buyer-match events from the
    ``supplier_notifications`` ledger, scoped to the single supplier. Returns
    only the bare count + the window - no per-request / per-buyer / per-time
    detail. Zero-state -> honest category/network framing (NEVER a "0" hero,
    NEVER a fabricated count - the honesty carve-out).

Flag gating: ``SUPPLIER_PORTAL_V1`` gates the ROUTE (api_server). These helpers
additionally guard on the flag (defense-in-depth) so a direct call also no-ops
when the portal is off. The demand teaser read of ``supplier_notifications`` is
TIER1_V2-gated at the registry layer (returns [] when TIER1_V2 off).
"""
from __future__ import annotations

import os
from typing import Optional

from utils import supplier_registry as sr


# ---------------------------------------------------------------------------
# Feature flag (defense-in-depth; the route in api_server is the load-bearing gate)
# ---------------------------------------------------------------------------
def _env_truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _portal_enabled() -> bool:
    return _env_truthy(os.environ.get("SUPPLIER_PORTAL_V1"))


# The demand-teaser default window (the brief's "in the last 30 days").
TEASER_WINDOW_DAYS_DEFAULT = 30

# Aftermarket disclosure text (mirrors tier1_matcher._AFTERMARKET_DISCLOSURE so
# the portal shows the SAME disclosure the sourcing card carries).
_AFTERMARKET_DISCLOSURE = (
    "Aftermarket-compatible part — not the OEM brand. Verify fit and warranty terms "
    "before purchase; aftermarket parts may affect OEM warranty coverage."
)

# Honest zero-state framing (the honesty carve-out: NEVER a "0 matches" hero,
# NEVER a fabricated count). Category/network-level, no numbers.
_ZERO_STATE_FRAMING = (
    "Buyers post procurement requests across your categories. "
    "Confirm your profile — brands, classes, and ship area — so Arkim can match "
    "you to the right requests."
)


# ---------------------------------------------------------------------------
# Profile read (T2)
# ---------------------------------------------------------------------------

def read_profile(supplier_domain: str) -> Optional[dict]:
    """Read the editable supplier profile for the portal page. Returns None
    when the supplier is unknown / the portal flag is off. The profile exposes
    ONLY: name, domain, brands (tri-state), classes, ship_area, and the
    aftermarket disclosure. It NEVER exposes lifecycle status, performance,
    onboarding_status, or any other supplier."""
    if not _portal_enabled():
        return None
    dom = sr._normalize_domain(supplier_domain)
    rec = sr.lookup_by_domain(dom)
    if not rec:
        return None
    brands = sr.get_supplier_brands(dom)
    classes = sr.get_supplier_classes(dom)
    terr = sr.get_supplier_territory(dom)
    # The aftermarket disclosure is a function of the brand relationships: a
    # supplier carrying any AFTERMARKET_COMPATIBLE brand gets the disclosure.
    has_aftermarket = any(b.get("relationship") == sr.BRAND_AFTERMARKET_COMPATIBLE
                          for b in brands)
    return {
        "supplier_domain": dom,
        "name": rec.get("name"),
        "brands": [
            {"brand_id": b.get("brand_id"),
             "relationship": b.get("relationship"),
             "evidence": b.get("evidence"),
             "classes_for_brand": b.get("classes_for_brand") or []}
            for b in brands
        ],
        "classes": [
            {"class_id": c.get("class_id"),
             "is_core": bool(c.get("is_core")),
             "subtype": c.get("subtype")}
            for c in classes
        ],
        "ship_area": terr.get("ship_area"),
        "aftermarket_disclosure": _AFTERMARKET_DISCLOSURE if has_aftermarket else None,
    }


# ---------------------------------------------------------------------------
# Demand teaser (T2) - read-only, honest, time-windowed
# ---------------------------------------------------------------------------

def demand_teaser(supplier_domain: str,
                  *, window_days: int = TEASER_WINDOW_DAYS_DEFAULT) -> dict:
    """The read-only demand teaser for the portal HERO. Counts GENUINE buyer-
    match events from ``supplier_notifications`` for the single supplier within
    a stated time window. Returns ONLY the bare count + the window - no per-
    request / per-buyer / per-time detail (so nothing derivable beyond the
    count). Zero-state -> honest category/network framing (never a "0" hero,
    never a fabricated count).

    Honesty (I4): the ``supplier_notifications`` ledger has NO seed/demo/
    synthetic rows by construction - the sole writer is the live notify layer
    (``record_supplier_notification`` from ``tier1_notify.notify_tier1``). So
    the count is naturally honest: it counts real notify events. See
    audit/NIGHT6_INVESTIGATION.md I4 for the dev-DB-hygiene caveat."""
    if not _portal_enabled():
        return _zero_state()
    dom = sr._normalize_domain(supplier_domain)
    rows = sr.get_supplier_notifications(domain=dom)  # [] when TIER1_V2 off
    # Window the count (rows are newest-first; notified_at is ISO UTC).
    cutoff = _cutoff_iso(window_days)
    count = 0
    for r in rows:
        at = (r.get("notified_at") or r.get("created_at") or "")
        if at and at >= cutoff:
            count += 1
    if count <= 0:
        return _zero_state(window_days=window_days)
    return {
        "has_matches": True,
        "count": count,
        "window_days": window_days,
        "framing": None,  # only present in the zero-state
    }


def _cutoff_iso(window_days: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.utcnow() - timedelta(days=window_days)).isoformat()


def _zero_state(*, window_days: int = TEASER_WINDOW_DAYS_DEFAULT) -> dict:
    """Honest zero-state: category/network framing, no fabricated numbers,
    never a '0 matches' hero."""
    return {
        "has_matches": False,
        "count": 0,
        "window_days": window_days,
        "framing": _ZERO_STATE_FRAMING,
    }
