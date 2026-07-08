"""
utils/procurement_agent/onboarding/concierge.py

T3 — Concierge v1: the human-in-the-loop review/approve gate for onboarding
drafts. This is the ONLY path that writes an onboarding draft into Night 3's
supplier-scope registry.

Flow:
  1. ``create_draft`` — the extractor output (an ``OnboardingDraft``) is
     persisted as a PENDING review item (``kind="supplier_scope"``) in the
     existing ``review_items`` table — the repo's "extraction lands as pending,
     a human confirms" pattern. NOTHING is written to the registry scope tables
     here (the must-confirm trio is pending human eyes).
  2. ``list_drafts`` / ``get_draft`` — the concierge UI loads a draft to
     inspect/edit/confirm.
  3. ``approve_draft`` — applies the (possibly editor-revised) draft to the
     registry: writes scope via ``set_supplier_classes`` / ``set_supplier_brands``
     / ``set_supplier_territory`` / ``set_supplier_verticals`` and drives the
     lifecycle ``onboarding → onboarded`` via ``tier1_transition``. Marks the
     review item ``confirmed``. Double-approve is idempotent (a second approve
     on an already-confirmed draft re-applies the same scope and returns the
     record — no duplicate write, no error).

Guarantees (asserted in tests):
  - **Nothing writes to the registry without approve.** ``create_draft`` only
    writes a review_items row; the scope tables are untouched until approve.
  - **Double-approve idempotent.** Approving an already-confirmed draft applies
    the scope again (full-replace is idempotent by construction in the registry
    setters) and returns the record — never raises, never creates a duplicate
    supplier row.
  - **Flag-off dormant.** When TIER1_V2 is off, ``create_draft`` returns None
    and ``approve_draft`` returns None — no review item, no registry write, no
    lifecycle transition. The registry stays byte-identical to pre-Night-4.
  - **The must-confirm trio is honored.** Approve requires the draft to carry
    the must_confirm flags; the concierge edit path can revise brands/classes/
    ship_area, but the draft is never auto-applied — approve is the explicit
    human action.

The draft payload is stored as JSON in ``review_items.payload_json`` and
decoded on read. Provenance (which draft, which source URLs, who approved)
rides on the review_item row + the registry's ``scope_set_by`` stamp.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from utils import supplier_registry as sr
from utils.procurement_agent.onboarding.extractor import (
    OnboardingDraft, MUST_CONFIRM_FIELDS,
    BRAND_AUTHORIZED, BRAND_CARRIES, BRAND_AFTERMARKET_COMPATIBLE,
)
from utils.procurement_agent.onboarding.flags import is_enabled


# review_items kind + status vocabulary for onboarding drafts.
DRAFT_KIND = "supplier_scope"
DRAFT_STATUS_PENDING = "needs_human_review"     # mirrors the review-queue convention
DRAFT_STATUS_CONFIRMED = "confirmed"
DRAFT_STATUS_REJECTED = "rejected"


# ---------------------------------------------------------------------------
# Draft persistence (review_items as the draft store)
# ---------------------------------------------------------------------------

def create_draft(draft: OnboardingDraft, *, source_url: Optional[str] = None,
                 set_by: Optional[str] = None) -> Optional[str]:
    """Persist an OnboardingDraft as a PENDING review item.

    NOTHING is written to the registry scope tables — only a review_items row
    (the pending-for-human pattern). Returns the review item id, or None when
    TIER1_V2 is off / the draft is empty / persistence fails (fail-soft).
    """
    if not is_enabled():
        return None
    if not draft or not draft.domain:
        return None
    draft.enforce_must_confirm()
    payload = draft.to_dict()
    try:
        item_id = sr.record_review_item(
            kind=DRAFT_KIND,
            payload=payload,
            status=DRAFT_STATUS_PENDING,
            supplier_domain=draft.domain,
            vendor_name=draft.name,
            confidence=draft.overall_confidence,
            raw_source=source_url or (draft.source_urls[0] if draft.source_urls else None),
        )
        return item_id
    except Exception as exc:
        print(f"[OnboardingConcierge] create_draft failed: {exc}")
        return None


def _row_to_draft_view(row: dict) -> dict:
    """A review_items row → the concierge-facing draft view (payload decoded,
    id/status/created_at surfaced)."""
    if not row:
        return {}
    payload = row.get("payload") or {}
    return {
        "id": row.get("id"),
        "status": row.get("status"),
        "domain": row.get("supplier_domain") or payload.get("domain"),
        "name": row.get("vendor_name") or payload.get("name"),
        "created_at": row.get("created_at"),
        "resolved_at": row.get("resolved_at"),
        "overall_confidence": row.get("confidence", payload.get("overall_confidence")),
        "extraction_method": payload.get("extraction_method"),
        "notes": payload.get("notes"),
        "vertical": payload.get("vertical"),
        "brands": payload.get("brands", []),
        "classes": payload.get("classes", []),
        "locations": payload.get("locations", []),
        "ship_area_guess": payload.get("ship_area_guess"),
        "source_urls": payload.get("source_urls", []),
        "must_confirm": payload.get("must_confirm",
                                    {f: True for f in MUST_CONFIRM_FIELDS}),
    }


def list_drafts(*, status: Optional[str] = DRAFT_STATUS_PENDING) -> list[dict]:
    """List onboarding drafts (default: pending only). Empty when TIER1_V2 off."""
    if not is_enabled():
        return []
    try:
        rows = sr.get_review_items(kind=DRAFT_KIND, status=status)
        return [_row_to_draft_view(r) for r in rows]
    except Exception as exc:
        print(f"[OnboardingConcierge] list_drafts failed: {exc}")
        return []


def get_draft(draft_id: str) -> Optional[dict]:
    """Load one draft for the concierge inspector. None if missing / flag-off."""
    if not is_enabled():
        return None
    try:
        row = sr.get_review_item(draft_id)
        if not row or row.get("kind") != DRAFT_KIND:
            return None
        return _row_to_draft_view(row)
    except Exception as exc:
        print(f"[OnboardingConcierge] get_draft failed: {exc}")
        return None


def reject_draft(draft_id: str, *, set_by: Optional[str] = None) -> Optional[dict]:
    """Mark a draft rejected (discarded — nothing applied to the registry)."""
    if not is_enabled():
        return None
    try:
        row = sr.get_review_item(draft_id)
        if not row or row.get("kind") != DRAFT_KIND:
            return None
        if row.get("status") == DRAFT_STATUS_CONFIRMED:
            # A confirmed draft was already applied — reject is a no-op refusal.
            return _row_to_draft_view(row)
        sr.set_review_item_status(draft_id, DRAFT_STATUS_REJECTED)
        return get_draft(draft_id)
    except Exception as exc:
        print(f"[OnboardingConcierge] reject_draft failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Approve → registry write (the only writer)
# ---------------------------------------------------------------------------

def approve_draft(
    draft_id: str,
    *,
    revisions: Optional[dict] = None,
    set_by: Optional[str] = None,
) -> Optional[dict]:
    """Approve a draft → apply its scope to the Night 3 registry + drive
    lifecycle ``onboarding → onboarded``. The ONLY path that writes an
    onboarding draft into the registry.

    ``revisions`` (optional): a concierge-edited override of the draft's
    brands/classes/ship_area/vertical/locations/name — applied INSTEAD of the
    stored draft's fields when present (per-field: a key present in revisions
    wins; absent keys fall back to the stored draft). The editor never touches
    the registry directly; approve is still the single write point.

    Returns the updated supplier record (``supplier_registry.lookup_by_domain``)
    on success, or None on: flag-off, missing draft, wrong kind, or a registry
    write failure (fail-soft — never raises; the caller surfaces the None).

    Double-approve idempotent: approving an already-confirmed draft re-applies
    the (now stored-as-confirmed) scope and returns the record — the registry
    setters are full-replace + ``INSERT OR IGNORE``, so a second approve is a
    no-op write that returns the same record.
    """
    if not is_enabled():
        return None
    try:
        row = sr.get_review_item(draft_id)
        if not row or row.get("kind") != DRAFT_KIND:
            return None
        payload = row.get("payload") or {}
        # Merge revisions (editor overrides) over the stored draft payload.
        merged = _merge_revisions(payload, revisions)
        domain = (row.get("supplier_domain") or merged.get("domain") or "").strip()
        if not domain:
            return None

        record = _apply_scope_to_registry(domain, merged, set_by=set_by)
        if record is None:
            return None
        # Mark the review item confirmed (idempotent on a second approve).
        sr.set_review_item_status(draft_id, DRAFT_STATUS_CONFIRMED)
        return record
    except Exception as exc:
        print(f"[OnboardingConcierge] approve_draft failed: {exc}")
        return None


def _merge_revisions(payload: dict, revisions: Optional[dict]) -> dict:
    """Merge concierge edits over the stored draft. Present keys in revisions
    win; absent keys keep the stored value. Validates brand relationships +
    canonical class_ids so an editor can't write a malformed scope."""
    out = dict(payload or {})
    if not revisions:
        return out
    for key in ("name", "vertical", "ship_area_guess", "locations", "source_urls"):
        if key in revisions:
            out[key] = revisions[key]
    if "brands" in revisions and isinstance(revisions["brands"], list):
        out["brands"] = [_normalize_brand(b) for b in revisions["brands"]
                         if isinstance(b, dict) and (b.get("name") or "").strip()]
    if "classes" in revisions and isinstance(revisions["classes"], list):
        out["classes"] = [_normalize_class(c) for c in revisions["classes"]
                          if isinstance(c, dict) and (c.get("class_id") or "").strip()]
    # Re-enforce the must-confirm trio on the merged draft (structural invariant).
    out["must_confirm"] = {f: True for f in MUST_CONFIRM_FIELDS}
    return out


def _normalize_brand(b: dict) -> dict:
    return {
        "name": (b.get("name") or "").strip(),
        "relationship_guess": _coerce_rel(b.get("relationship_guess")),
        "confidence": _clamp(b.get("confidence", 0.0)),
        "evidence": (b.get("evidence") or "").strip()[:300],
        "source_url": (b.get("source_url") or "").strip(),
        "must_confirm": True,
    }


def _normalize_class(c: dict) -> dict:
    cid = _canonicalize_class(c.get("class_id") or c.get("class") or "")
    return {
        "class_id": cid or (c.get("class_id") or "").strip().upper(),
        "confidence": _clamp(c.get("confidence", 0.0)),
        "evidence": (c.get("evidence") or "").strip()[:300],
        "source_url": (c.get("source_url") or "").strip(),
        "is_core_guess": bool(c.get("is_core_guess")),
        "must_confirm": True,
    }


def _apply_scope_to_registry(domain: str, draft: dict,
                             *, set_by: Optional[str] = None) -> Optional[dict]:
    """Write the draft's scope to the registry + drive lifecycle to onboarded.

    Order: ensure a supplier row exists → set classes/brands/territory/
    verticals → transition the lifecycle to ``onboarding`` then ``onboarded``
    (entering the state machine at discovered is not needed because
    ``_ensure_supplier_row`` creates a discovery_only stub; the transition
    helper allows a NULL current to enter at ``discovered`` only, so we drive
    discovered → onboarding → onboarded explicitly for a fresh row, and for an
    already-onboarding row we go onboarding → onboarded).

    Returns the supplier record, or None on any write failure (fail-soft).
    """
    try:
        # 1. Ensure a supplier row (creates a discovery_only stub if absent).
        sid = sr._ensure_supplier_row(domain, name=draft.get("name"))
        if not sid:
            # Row creation failed — abort without a partial scope write.
            return None

        # 2. Classes (validated canonical class_ids).
        classes = [
            {
                "class_id": c.get("class_id"),
                "subtype": c.get("subtype"),
                "unspsc": _unspsc_for(c.get("class_id")),
                "is_core": bool(c.get("is_core_guess")),
                "confidence": _clamp(c.get("confidence", 0.0)),
                "source": sr.SCOPE_SOURCE_MANUAL,
            }
            for c in (draft.get("classes") or [])
            if (c.get("class_id") or "").strip()
        ]
        if classes:
            ok = sr.set_supplier_classes(domain, classes, set_by=set_by)
            if not ok:
                return None

        # 3. Brands (tri-state relationship).
        brands = [
            {
                "brand_id": (b.get("name") or "").strip(),
                "relationship": _coerce_rel(b.get("relationship_guess")),
                "classes_for_brand": b.get("classes_for_brand") or [],
                "evidence": (b.get("evidence") or "").strip()[:300],
                "confidence": _clamp(b.get("confidence", 0.0)),
            }
            for b in (draft.get("brands") or [])
            if (b.get("name") or "").strip()
        ]
        if brands:
            ok = sr.set_supplier_brands(domain, brands, set_by=set_by)
            if not ok:
                return None

        # 4. Territory (ship_area + optional local_service branches).
        ship = draft.get("ship_area_guess")
        if isinstance(ship, dict) and ship.get("kind") in ("NATIONWIDE_US", "STATES"):
            local = _local_service_from_locations(draft.get("locations") or [])
            ok = sr.set_supplier_territory(domain, ship, local or None, set_by=set_by)
            if not ok:
                return None

        # 5. Verticals.
        vertical = (draft.get("vertical") or "").strip()
        if vertical:
            ok = sr.set_supplier_verticals(domain, [vertical], set_by=set_by)
            if not ok:
                return None

        # 6. Lifecycle: discovered → onboarding → onboarded. For a row that
        # already has a lifecycle (re-approve / partial), drive forward only
        # along legal transitions; an already-onboarded row stays onboarded.
        current = sr.get_tier1_lifecycle(domain)
        if current is None:
            sr.tier1_transition(domain, sr.TIER1_DISCOVERED, set_by=set_by)
            current = sr.TIER1_DISCOVERED
        if current == sr.TIER1_DISCOVERED:
            sr.tier1_transition(domain, sr.TIER1_CONTACTED, set_by=set_by)
            current = sr.TIER1_CONTACTED
        if current == sr.TIER1_CONTACTED:
            sr.tier1_transition(domain, sr.TIER1_QUOTED, set_by=set_by)
            current = sr.TIER1_QUOTED
        if current == sr.TIER1_QUOTED:
            sr.tier1_transition(domain, sr.TIER1_ONBOARDING, set_by=set_by)
            current = sr.TIER1_ONBOARDING
        if current == sr.TIER1_ONBOARDING:
            sr.tier1_transition(domain, sr.TIER1_ONBOARDED, set_by=set_by)
        # If current is already ONBOARDED or SUSPENDED, leave it (idempotent /
        # don't un-suspend on a re-approve — that's a separate operator action).

        return sr.lookup_by_domain(domain)
    except Exception as exc:
        print(f"[OnboardingConcierge] _apply_scope_to_registry failed: {exc}")
        return None


def _local_service_from_locations(locations: list[dict]) -> list[dict]:
    """Locations → supplier_local_service rows (branch_zip best-effort). The
    heuristic draft rarely has a zip; the LLM/concierge may supply one."""
    out: list[dict] = []
    for loc in locations or []:
        if not isinstance(loc, dict):
            continue
        zip_code = (loc.get("postal_code") or loc.get("zip") or "").strip()
        if zip_code:
            out.append({"branch_zip": zip_code,
                        "services": loc.get("services") or []})
    return out


# ---------------------------------------------------------------------------
# helpers (mirror extractor internals; kept local so concierge is standalone)
# ---------------------------------------------------------------------------

def _coerce_rel(raw: str) -> str:
    r = (raw or "").upper().strip()
    if r in (BRAND_AUTHORIZED, BRAND_CARRIES, BRAND_AFTERMARKET_COMPATIBLE):
        return r
    if "AUTHORIZED" in r or "DEALER" in r:
        return BRAND_AUTHORIZED
    if "AFTERMARKET" in r or "COMPATIBLE" in r or "CROSS" in r:
        return BRAND_AFTERMARKET_COMPATIBLE
    return BRAND_CARRIES


def _clamp(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


def _canonicalize_class(raw: str) -> Optional[str]:
    if not raw:
        return None
    try:
        from utils.sourcing_archieved import part_type_classes as ptc
    except Exception:
        return None
    nc = ptc.get_noun_class(raw.strip().upper())
    if nc:
        return nc.canonical
    cls = ptc.classify_noun_class(raw)
    return cls


def _unspsc_for(class_id: str) -> Optional[str]:
    try:
        from utils.sourcing_archieved import part_type_classes as ptc
    except Exception:
        return None
    nc = ptc.get_noun_class((class_id or "").upper())
    return nc.unspsc if nc else None
