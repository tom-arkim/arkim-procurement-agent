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
"""
import json
import os
from datetime import datetime, timedelta
from typing import Optional

_DB_PATH = os.path.join(os.path.dirname(__file__), "price_db.json")


def _make_key(manufacturer: str, part_number: str) -> str:
    """Composite cache key: manufacturer (lower) + part number (upper).

    Including the manufacturer prevents two makers' parts that share a part
    number from colliding (CLEANUP.md §3.3).
    """
    return f"{(manufacturer or '').lower().strip()}|{part_number.upper().strip()}"


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
    db  = _load()
    key = _make_key(manufacturer, part_number)
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
    db      = _load()
    key     = _make_key(manufacturer, part_number)
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
