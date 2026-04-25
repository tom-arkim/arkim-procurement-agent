"""
Arkim Supplier Price Database — JSON-backed, human-readable.
Stores prices keyed by (PART_NUMBER → vendor → entry).
Source values: "live" (Tavily search), "rfq" (manually entered response).
"""
import json
import os
from datetime import datetime, timedelta
from typing import Optional

_DB_PATH = os.path.join(os.path.dirname(__file__), "price_db.json")


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


def save_price(part_number: str, vendor_name: str, price: float,
               lead_days: Optional[int] = None, source: str = "live",
               url: Optional[str] = None) -> None:
    db  = _load()
    key = part_number.upper().strip()
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


def get_cached_prices(part_number: str, max_age_days: int = 30) -> dict:
    """Return {vendor_name: {price, lead_days, date_fetched, source}} for entries within max_age_days."""
    db      = _load()
    key     = part_number.upper().strip()
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
