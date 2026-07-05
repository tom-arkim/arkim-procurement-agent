"""
Arkim Supplier Price Database — JSON-backed, human-readable.
Stores prices keyed by ("manufacturer|PART_NUMBER" → vendor → entry).
Source values: "live" (Tavily search), "rfq" (manually entered response).

Cache key note (CLEANUP.md §3.3): keying on part number alone let two
manufacturers' parts that share a part number collide and silently serve the
wrong price. The key is a composite of (manufacturer, part_number). Legacy
part-number-only keys written before this change no longer match a lookup and
are treated as cache misses (re-fetched live); they are left in the file
untouched rather than rewritten, so no existing data is corrupted.

Null-PN guard (CLEANUP.md §7.1 — the cross-query contamination fix): a
manufacturer-less / PN-less (spec-based) request must NEVER read or write this
cache. Every such request collapses to the same ``unknown|UNKNOWN-PN`` bucket,
so caching it serves one vague query's vendors on the next unrelated vague
query (a motor page topping a valve request, at a stale static 50% score with
no per-request re-scoring). ``_make_key`` returns "" for those specs →
``get_cached_prices`` returns {} (miss) and ``save_price`` no-ops. This mirrors
``known_parts.canonical_part_key``'s ""-on-null-PN guard exactly and reuses the
existing ``_NULL_PN_TOKENS`` token set (no new token list) + the
Unknown-class manufacturer convention from ``tavily_client._build_search_query``
(``("Unknown", "N/A", "null")``).
"""
import json
import os
from datetime import datetime, timedelta
from typing import Optional

_DB_PATH = os.path.join(os.path.dirname(__file__), "price_db.json")


# Manufacturer values that mean "no real manufacturer was identified" — the
# Unknown-class convention already used by the query builders
# (tavily_client._build_search_query line ~80: `specs.manufacturer not in
# ("Unknown", "N/A", "null")`). A cache key built on any of these is semantically
# meaningless (it would pool every manufacturer-less part together).
_NULL_MFG_TOKENS = {"", "unknown", "n/a", "na", "null", "none"}


def _is_cacheable_identity(manufacturer: str, part_number: str) -> bool:
    """True only when BOTH a real manufacturer and a real PN are present.

    Reuses known_parts._NULL_PN_TOKENS (the existing placeholder-PN set) for the
    PN check via the same normalize-then-test discipline, and the existing
    Unknown-class manufacturer convention for the mfg check. No new token lists.
    """
    from utils.known_parts import _NULL_PN_TOKENS
    from utils.procurement_agent.agents.sourcing_agent import normalize_part_number

    if (manufacturer or "").strip().lower() in _NULL_MFG_TOKENS:
        return False
    if normalize_part_number(part_number or "") in _NULL_PN_TOKENS:
        return False
    return True


def _make_key(manufacturer: str, part_number: str) -> str:
    """Composite cache key: manufacturer (lower) + part number (upper).

    Including the manufacturer prevents two makers' parts that share a part
    number from colliding (CLEANUP.md §3.3). Returns "" for null/placeholder
    identities (CLEANUP.md §7.1) — callers treat "" as a miss/no-op so a
    manufacturer-less or PN-less (spec-based) request never reads or writes the
    cache (it would otherwise pool every vague query into one bucket).
    """
    if not _is_cacheable_identity(manufacturer, part_number):
        return ""
    return f"{(manufacturer or '').lower().strip()}|{(part_number or '').upper().strip()}"


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


def save_price(manufacturer: str, part_number: str, vendor_name: str, price: float,
               lead_days: Optional[int] = None, source: str = "live",
               url: Optional[str] = None) -> None:
    # CLEANUP §7.1 null-PN guard: a manufacturer-less / PN-less request never
    # writes — its key would be "" (the shared unknown bucket) and poisoning it
    # serves one vague query's vendors on the next unrelated vague query.
    key = _make_key(manufacturer, part_number)
    if not key:
        return
    db  = _load()
    if key not in db:
        db[key] = {}
    db[key][vendor_name] = {
        "price":        price,
        "lead_days":    lead_days,
        "date_fetched": datetime.now().isoformat(),
        "source":       source,
        "url":          url,
    }
    _save(db)


def get_cached_prices(manufacturer: str, part_number: str, max_age_days: int = 30) -> dict:
    """Return {vendor_name: {price, lead_days, date_fetched, source}} for entries within max_age_days."""
    # CLEANUP §7.1 null-PN guard: "" key → miss (no pooling of unrelated vague queries).
    key = _make_key(manufacturer, part_number)
    if not key:
        return {}
    db      = _load()
    entries = db.get(key, {})
    cutoff  = datetime.now() - timedelta(days=max_age_days)
    result  = {}
    for vendor, data in entries.items():
        try:
            if datetime.fromisoformat(data["date_fetched"]) >= cutoff:
                result[vendor] = data
        except (KeyError, ValueError):
            pass
    return result


def all_entries() -> dict:
    """Return the full raw database for diagnostics / display."""
    return _load()
