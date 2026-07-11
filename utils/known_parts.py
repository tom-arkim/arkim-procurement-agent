"""
utils/known_parts.py — the part→supplier graph ("known parts" cache).

A NEW sibling store (it does NOT extend price_db). Roles of the three stores:
  - supplier_registry  → durable PER-SUPPLIER facts (firmographics/contact), keyed by domain.
  - price_db           → volatile PRICE facts, keyed by (part, vendor), timestamped.
  - known_parts (here) → the missing EDGE table: canonical part-key → supplier edges
                         (which supplier carries this part, via which channel).

Why this exists: uncached Tier-3 discovery (Tavily + LLM) returns DIFFERENT candidates
each run for the same part, so results vary run-to-run. Caching the full candidate set
per part makes a previously-seen part read from here (consistent, no fresh web search)
and builds the proprietary part→supplier graph.

Two HARD requirements (the cache fragments without them):
  1. CANONICAL part-key — manufacturer aliases + PN-normalization applied BEFORE keying
     (canonical_part_key). Raw LLM-extracted text would fork "Gusher" vs "Gusher Pumps".
  2. DOMAIN-keyed edges — each edge is keyed by normalized domain, NOT vendor_name. The
     same supplier surfaces as "Seal It 123" and "sealit123.com" across runs; domain
     keying collapses them (a stepping stone toward §5a entity resolution).

Durable vs volatile: a supplier edge (this supplier carries this part; its channel) is
DURABLE and long-lived. The price is VOLATILE — stamped and stale-after-N-days. The two
are NOT bundled: get_edges always returns the durable edge and merely FLAGS a stale price
(price_stale=True) — a stale price never drops the edge (the bug price_db has, where a
>30-day price is skipped and the supplier mapping is lost with it, is not repeated here).

JSON-backed (like price_db), human-readable; prototype-grade (same read-modify-write
concurrency caveat as price_db).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

_DB_PATH = os.path.join(os.path.dirname(__file__), "known_parts.json")

# Volatile-price freshness: a cached price older than this is "stale" and must be
# re-verified before it's shown as current (until then it carries the priceUnverified
# treatment). Tunable. The durable supplier edge is NOT subject to this — it's long-lived.
PRICE_TTL_DAYS: int = 30

# Part-number tokens that are not real PNs — a key built on these would collide across
# unrelated parts, so caching is disabled for them (canonical_part_key returns "").
_NULL_PN_TOKENS = {"", "UNKNOWNPN", "UNKNOWN", "NA", "TBD", "NONE", "NONE0"}


# ---------------------------------------------------------------------------
# HARD REQ 1 — canonical part-key (aliases + PN-normalize before keying)
# ---------------------------------------------------------------------------

def canonical_part_key(manufacturer: Optional[str], part_number: Optional[str]) -> str:
    """Stable cache key for a physical part: canonical manufacturer + normalized PN.

    Applies manufacturer aliases (so "Gusher" / "Gusher Pumps" / "Gusher Pumps Type 21"
    collapse to one) and delimiter-agnostic PN normalization (so "84004-28-C238CBC" and
    "8400428C238CBC" collapse). Falls back to the PN-prefix manufacturer when the
    manufacturer is blank. Returns "" when there is no real part number — PN-less
    (spec-based) parts are too ambiguous to key, so caching is disabled for them.
    """
    # Lazy imports: keep this module import-light and avoid cycles.
    from utils.brand_intelligence import get_manufacturer_aliases, lookup_manufacturer_from_pn
    from utils.procurement_agent.agents.sourcing_agent import normalize_part_number

    pn_norm = normalize_part_number(part_number or "")
    if pn_norm in _NULL_PN_TOKENS:
        return ""

    mfg = (manufacturer or "").strip()
    if not mfg and part_number:
        mfg = lookup_manufacturer_from_pn(part_number) or ""
    if mfg:
        aliases = get_manufacturer_aliases(mfg)          # [canonical, ...] or [raw]
        canonical_mfg = aliases[0] if aliases else mfg
    else:
        canonical_mfg = ""

    return f"{canonical_mfg.lower().strip()}|{pn_norm}"


# ---------------------------------------------------------------------------
# HARD REQ 2 — domain-keyed supplier edges
# ---------------------------------------------------------------------------

def _edge_id(url: Optional[str], name: Optional[str]) -> str:
    """Canonical supplier id for an edge: normalized domain when a URL exists (collapses
    "Seal It 123"/"sealit123.com" → one), else a normalized-name fallback for URL-less
    suppliers (e.g. seeded RFQ-only distributors, which are deterministic anyway)."""
    from utils.supplier_registry import _normalize_domain
    dom = _normalize_domain(url) if url else ""
    if dom:
        return dom
    slug = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return f"name:{slug}" if slug else ""


def _channel(price: Optional[float], url: Optional[str]) -> str:
    """marketplace = buyable price at a curated marketplace; reference = priced elsewhere;
    rfq = no buyable price (quote-required)."""
    from utils.marketplace_registry import is_marketplace
    if price is None:
        return "rfq"
    return "marketplace" if is_marketplace(url) else "reference"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    if os.path.exists(_DB_PATH):
        try:
            with open(_DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(db: dict) -> None:
    with open(_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)


def _price_of(c: dict) -> Optional[float]:
    """Buyable price from a candidate dict (raw SourcingOption shape or transformed).
    None when the row is quote-required / price-hidden."""
    if c.get("price_tbd") or c.get("requires_rfq"):
        return None
    raw = c.get("base_price")
    if raw is None:
        raw = c.get("price")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Write-back / read
# ---------------------------------------------------------------------------

def upsert_edges(part_key: str, candidates: list[dict]) -> int:
    """Write/update the supplier edges discovered for a part. Durable fields are set on
    first write and kept (first_seen); volatile price is refreshed when present. Edges
    dedupe by domain (HARD REQ 2). No-op on an empty key. Returns edges written."""
    if not part_key or not candidates:
        return 0
    db = _load()
    entry = db.setdefault(part_key, {"edges": {}, "updated_at": _now()})
    edges = entry["edges"]
    now = _now()
    written = 0
    for c in candidates:
        url = c.get("source_url") or c.get("url")
        name = c.get("vendor_name") or c.get("vendorName")
        eid = _edge_id(url, name)
        if not eid:
            continue
        price = _price_of(c)
        prior = edges.get(eid, {})
        tier = c.get("tier")
        edges[eid] = {
            # durable
            "supplier_id":      eid,
            "display_name":     name or prior.get("display_name"),
            "purchase_channel": _channel(price, url),
            "tier":             tier if tier in (1, 2, 3) else prior.get("tier", (3 if price is None else 2)),
            "match_type":       c.get("match_type") or prior.get("match_type"),
            "found_pn":         c.get("found_part_number") or c.get("foundPartNumber") or prior.get("found_pn"),
            "suitability":      float(c.get("suitability_score") or c.get("suitability") or 0.0),
            "source_url":       url or prior.get("source_url"),
            "first_seen":       prior.get("first_seen", now),
            "last_seen":        now,
            # volatile (kept separate; a stale price never drops this edge)
            "price":            price if price is not None else prior.get("price"),
            "price_date":       now if price is not None else prior.get("price_date"),
            "lead_days":        c.get("lead_time_days") if c.get("lead_time_days") is not None else prior.get("lead_days"),
        }
        written += 1
    entry["updated_at"] = now
    _save(db)
    return written


def get_edges(part_key: str, price_ttl_days: int = PRICE_TTL_DAYS) -> list[dict]:
    """Return the durable supplier edges for a part (stable order). Each edge ALWAYS
    comes back regardless of price age; the price carries `price_stale` (True when a
    real price is older than the TTL) so the caller can flag it unverified rather than
    show a stale price as current. Empty list for an unknown/blank key."""
    if not part_key:
        return []
    entry = _load().get(part_key)
    if not entry:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=price_ttl_days)
    out: list[dict] = []
    for edge in entry.get("edges", {}).values():
        e = dict(edge)
        stale = False
        if edge.get("price") is not None:
            pd = edge.get("price_date")
            try:
                # Normalize to aware UTC: stored price_date was historically naive
                # UTC (datetime.utcnow().isoformat()); newer writes are tz-aware.
                # Treat a naive value as UTC so the aware `cutoff` comparison never
                # raises "can't compare offset-naive and offset-aware".
                pd_dt = datetime.fromisoformat(pd)
                if pd_dt.tzinfo is None:
                    pd_dt = pd_dt.replace(tzinfo=timezone.utc)
                stale = pd_dt < cutoff
            except (ValueError, TypeError):
                stale = True
        e["price_stale"] = bool(edge.get("price") is not None and stale)
        out.append(e)
    # Stable order: best suitability first, then supplier id — deterministic across reads.
    out.sort(key=lambda e: (-(e.get("suitability") or 0.0), e.get("supplier_id") or ""))
    return out


def all_entries() -> dict:
    """Full raw store for diagnostics."""
    return _load()
