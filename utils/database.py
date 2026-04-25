"""
Arkim Price Cache — SQLite-backed supplier price memory.
Stores (part_number, vendor_name, price, lead_days, date_fetched).
"""
import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "price_cache.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _init():
    with _conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS price_cache (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            part_number  TEXT NOT NULL,
            vendor_name  TEXT NOT NULL,
            price        REAL NOT NULL,
            lead_days    INTEGER,
            date_fetched TEXT NOT NULL,
            UNIQUE(part_number, vendor_name)
        )""")


def save_price(part_number: str, vendor_name: str,
               price: float, lead_days: int = None) -> None:
    _init()
    with _conn() as c:
        c.execute("""
        INSERT INTO price_cache (part_number, vendor_name, price, lead_days, date_fetched)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(part_number, vendor_name) DO UPDATE SET
            price        = excluded.price,
            lead_days    = excluded.lead_days,
            date_fetched = excluded.date_fetched
        """, (part_number, vendor_name, price, lead_days,
              datetime.now().isoformat()))


def get_cached_prices(part_number: str,
                      max_age_days: int = 30) -> dict[str, dict]:
    """
    Return {vendor_name: {price, lead_days, date_fetched}} for cache hits
    within max_age_days.
    """
    _init()
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    with _conn() as c:
        rows = c.execute("""
        SELECT vendor_name, price, lead_days, date_fetched
        FROM price_cache
        WHERE part_number = ? AND date_fetched >= ?
        """, (part_number, cutoff)).fetchall()
    return {r["vendor_name"]: dict(r) for r in rows}


def clear_cache(part_number: str = None) -> None:
    _init()
    with _conn() as c:
        if part_number:
            c.execute("DELETE FROM price_cache WHERE part_number = ?",
                      (part_number,))
        else:
            c.execute("DELETE FROM price_cache")


def cache_summary() -> list[dict]:
    """Return all cached entries sorted by date for display."""
    _init()
    with _conn() as c:
        rows = c.execute("""
        SELECT part_number, vendor_name, price, lead_days, date_fetched
        FROM price_cache
        ORDER BY date_fetched DESC
        """).fetchall()
    return [dict(r) for r in rows]
