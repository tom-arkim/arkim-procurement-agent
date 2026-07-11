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

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from utils import supplier_registry as sr

log = logging.getLogger(__name__)


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
    "Confirm your profile — brands, classes, and ship area — so Gofer can match "
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
    """The read-only demand teaser for the portal HERO. Counts DISTINCT REAL
    buyer requests from ``supplier_notifications`` for the single supplier
    within a stated time window. Returns ONLY the bare count + the window - no
    per-request / per-buyer / per-time detail (so nothing derivable beyond the
    count). Zero-state -> honest category/network framing (never a "0" hero,
    never a fabricated count).

    The count is ``DISTINCT run_id`` over LIVE rows only (``is_test = 0``),
    within the window — i.e. distinct buyer requests, NOT notification events.
    A single sourcing run that re-fires the same class-match across runs is ONE
    request; a request that triggers many notifications is still one request.
    Test/fixture/seed rows (``is_test = 1``) are excluded so they never inflate
    a supplier's demand.

    Window comparison: timestamps are PARSED to timezone-aware UTC datetimes and
    compared as datetimes (not ISO strings). The stored ``notified_at`` may be
    naive UTC (the historical ``datetime.utcnow().isoformat()`` form, no tz
    suffix, microsecond precision - see ``record_supplier_notification``) or
    tz-aware (``+00:00``, the current writer form); a manual insert could emit a
    ``Z`` / offset / different-precision string too. A string compare would
    silently miscount those (a ``Z``-suffixed row in the same second as the
    cutoff but earlier sub-second sorts AFTER a naive microsecond cutoff, so
    string compare wrongly
    counts it). Parsing makes the result correct regardless of string format.
    A malformed/missing timestamp is excluded from the count (never crashes,
    never counts-as-matched).

    A row with no ``run_id`` is skipped: a distinct-request count needs a
    request key, and a NULL run_id can't be de-duplicated (counting NULL once
    would conflate an unknown number of requests; counting it per-row would
    re-introduce the row-count bug)."""
    if not _portal_enabled():
        return _zero_state()
    dom = sr._normalize_domain(supplier_domain)
    rows = sr.get_supplier_notifications(domain=dom)  # [] when TIER1_V2 off
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    distinct_runs: set[str] = set()
    for r in rows:
        if r.get("is_test"):  # exclude test/fixture/seed provenance
            continue
        run_id = r.get("run_id")
        if not run_id:
            continue  # no request key to de-duplicate on
        at = r.get("notified_at") or r.get("created_at") or ""
        if _ts_in_window(at, cutoff):
            distinct_runs.add(run_id)
    count = len(distinct_runs)
    if count <= 0:
        return _zero_state(window_days=window_days)
    return {
        "has_matches": True,
        "count": count,
        "window_days": window_days,
        "framing": None,  # only present in the zero-state
    }


def _parse_ts_utc(raw: str) -> Optional[datetime]:
    """Parse a stored timestamp to a timezone-aware UTC datetime. A naive
    timestamp (the historical repo default - ``datetime.utcnow().isoformat()``)
    is assumed UTC. A tz-aware timestamp (``+00:00`` / ``Z`` / offset, the
    current writer form) is respected and normalized to UTC. Returns None on a
    malformed/empty value so the caller can exclude it from the count (never
    crashes, never counts-as-matched)."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ts_in_window(raw: str, cutoff: datetime) -> bool:
    """True iff ``raw`` parses to an instant at-or-after ``cutoff`` (both aware
    UTC). False on a malformed/missing timestamp (excluded from the count)."""
    ts = _parse_ts_utc(raw)
    return ts is not None and ts >= cutoff


def _zero_state(*, window_days: int = TEASER_WINDOW_DAYS_DEFAULT) -> dict:
    """Honest zero-state: category/network framing, no fabricated numbers,
    never a '0 matches' hero."""
    return {
        "has_matches": False,
        "count": 0,
        "window_days": window_days,
        "framing": _ZERO_STATE_FRAMING,
    }


# ---------------------------------------------------------------------------
# Propose revision (T3) - pending store ONLY, never the registry
# ---------------------------------------------------------------------------

# The review_items kind for a supplier-proposed profile revision (distinct from
# the onboarding "supplier_scope" kind so the onboarding queue is not polluted).
REVISION_KIND = "supplier_revision"
REVISION_STATUS_PENDING = "needs_human_review"


def propose_revision(supplier_domain: str, revisions: dict,
                     *, proposed_by: Optional[str] = None) -> Optional[str]:
    """Land a supplier-proposed profile edit as a PENDING revision in
    ``review_items`` (kind=``supplier_revision``) via Night 4's review
    machinery. NOTHING writes the registry here - the concierge approve is the
    only writer (decision 1: supplier-proposes / concierge-approves). Returns
    the revision id, or None on flag-off / empty domain / store failure
    (fail-soft - never raises).

    The payload is a full proposed-scope snapshot (brands/classes/ship_area)
    + proposer provenance, stored in ``payload_json``. The low-friction edit
    surface (the research's anti-Ariba-ification): the trio
    (brands/classes/ship-area) is editable in a single lightweight pass, with
    the tri-state brand relationship as the centerpiece (only the supplier
    authoritatively knows "authorized vs compatible-alternatives")."""
    if not _portal_enabled():
        return None
    dom = sr._normalize_domain(supplier_domain)
    if not dom:
        return None
    payload = {
        "domain": dom,
        "brands": revisions.get("brands") or [],
        "classes": revisions.get("classes") or [],
        "ship_area": revisions.get("ship_area"),
        "proposed_by": proposed_by or "supplier",
    }
    try:
        item_id = sr.record_review_item(
            kind=REVISION_KIND,
            payload=payload,
            status=REVISION_STATUS_PENDING,
            supplier_domain=dom,
        )
        return item_id
    except Exception as exc:
        log.error("[SupplierPortal] propose_revision failed for %r: %s", dom, exc)
        return None


# ---------------------------------------------------------------------------
# Concierge apply/reject (T4) - the ONLY writer, via Night 4's scope setters
# ---------------------------------------------------------------------------

REVISION_STATUS_CONFIRMED = "confirmed"
REVISION_STATUS_REJECTED = "rejected"


def apply_revision(revision_id: str, *, set_by: Optional[str] = None) -> Optional[dict]:
    """Approve a supplier-proposed revision: apply its proposed scope to the
    registry via the four full-replace scope setters (set_supplier_classes /
    _brands / _territory / _verticals) - WITHOUT the lifecycle drive (the
    supplier is already onboarded; re-driving is idempotent but semantically
    wrong for a profile edit). Marks the review item ``confirmed``. Returns the
    updated supplier record, or None on flag-off / missing revision / wrong
    kind / write failure (fail-soft). Double-approve idempotent (mirrors
    concierge.approve_draft)."""
    if not _portal_enabled():
        return None
    try:
        row = sr.get_review_item(revision_id)
        if not row or row.get("kind") != REVISION_KIND:
            return None
        payload = row.get("payload") or {}
        domain = (row.get("supplier_domain") or payload.get("domain") or "").strip()
        if not domain:
            return None
        record = _apply_scope_no_lifecycle(domain, payload, set_by=set_by)
        if record is None:
            return None
        sr.set_review_item_status(revision_id, REVISION_STATUS_CONFIRMED)
        return record
    except Exception as exc:
        log.error("[SupplierPortal] apply_revision failed for %r: %s", revision_id, exc)
        return None


def reject_revision(revision_id: str) -> Optional[dict]:
    """Reject a supplier-proposed revision - nothing is applied to the registry.
    Marks the review item ``rejected``. Returns the updated view, or None on
    flag-off / missing / wrong kind."""
    if not _portal_enabled():
        return None
    try:
        row = sr.get_review_item(revision_id)
        if not row or row.get("kind") != REVISION_KIND:
            return None
        if row.get("status") == REVISION_STATUS_CONFIRMED:
            return row  # already applied - reject is a no-op refusal
        sr.set_review_item_status(revision_id, REVISION_STATUS_REJECTED)
        return sr.get_review_item(revision_id)
    except Exception as exc:
        log.error("[SupplierPortal] reject_revision failed for %r: %s", revision_id, exc)
        return None


def _apply_scope_no_lifecycle(domain: str, payload: dict,
                              *, set_by: Optional[str] = None) -> Optional[dict]:
    """Apply the proposed scope via the four setters WITHOUT driving the
    lifecycle (mirrors concierge._apply_scope_to_registry minus the lifecycle
    drive). Brand relationships + class_ids are validated/canonicalized so a
    supplier can't write a malformed scope (the approve is still the gate)."""
    try:
        sid = sr._ensure_supplier_row(domain, name=payload.get("name"))
        if not sid:
            return None
        # Classes.
        classes = [
            {"class_id": (c.get("class_id") or "").upper().strip(),
             "is_core": bool(c.get("is_core")),
             "confidence": c.get("confidence", 0.8),
             "source": sr.SCOPE_SOURCE_MANUAL}
            for c in (payload.get("classes") or [])
            if (c.get("class_id") or "").strip()
        ]
        if classes:
            ok = sr.set_supplier_classes(domain, classes, set_by=set_by)
            if not ok:
                return None
        # Brands (tri-state relationship - validated against BRAND_RELATIONSHIPS).
        brands = [
            {"brand_id": (b.get("brand_id") or "").strip(),
             "relationship": (b.get("relationship") or "").upper().strip(),
             "confidence": b.get("confidence", 0.9)}
            for b in (payload.get("brands") or [])
            if (b.get("brand_id") or "").strip()
            and (b.get("relationship") or "").upper().strip() in sr.BRAND_RELATIONSHIPS
        ]
        if brands:
            ok = sr.set_supplier_brands(domain, brands, set_by=set_by)
            if not ok:
                return None
        # Territory.
        ship = payload.get("ship_area")
        if isinstance(ship, dict) and ship.get("kind") in (
                sr.SHIP_AREA_NATIONWIDE_US, "STATES"):
            ok = sr.set_supplier_territory(domain, ship, set_by=set_by)
            if not ok:
                return None
        return sr.lookup_by_domain(domain)
    except Exception as exc:
        log.error("[SupplierPortal] _apply_scope_no_lifecycle failed for %r: %s",
                  domain, exc)
        return None
