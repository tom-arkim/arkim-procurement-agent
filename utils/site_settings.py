"""
utils/site_settings.py
Per-site delivery (ship-to) settings — the durable store behind the customer Delivery
Settings screen and the graduated shipping disclosure at order placement.

Raw-sqlite3 module (mirrors utils/orders.py): its own data/site_settings.sqlite,
idempotent CREATE TABLE, fail-soft, bracket-prefixed logging. One ship-to row per
site_id. Replaces the earlier client-only (localStorage) persistence.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Optional

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DB_PATH = os.path.join(_DATA_DIR, "site_settings.sqlite")

# The fields a caller may write (matches the customer form + order-review ship-to block).
_WRITABLE = ("company", "address", "city", "attention", "hours", "instructions")

_DDL = """
CREATE TABLE IF NOT EXISTS site_shipto (
    site_id       TEXT PRIMARY KEY,
    company       TEXT,
    address       TEXT,
    city          TEXT,
    attention     TEXT,
    hours         TEXT,
    instructions  TEXT,
    updated_at    TEXT NOT NULL
);
"""


def _get_conn() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(_DDL)
    conn.commit()
    return conn


def get_shipto(site_id: str) -> Optional[dict]:
    """Return the stored ship-to for a site (dict of _WRITABLE fields + updated_at), or
    None when nothing has been saved yet (the UI falls back to its seeded default)."""
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM site_shipto WHERE site_id = ?", (site_id,)).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        print(f"[SiteSettings] get_shipto failed for {site_id!r}: {exc}")
        return None


def upsert_shipto(site_id: str, fields: dict) -> bool:
    """Insert or replace a site's ship-to. Only _WRITABLE keys are accepted; missing keys
    store as empty strings. Fail-soft: returns False on a write error, never raises."""
    if not site_id:
        return False
    values = {k: (fields.get(k) or "") for k in _WRITABLE}
    now = datetime.utcnow().isoformat()
    try:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO site_shipto
                   (site_id, company, address, city, attention, hours, instructions, updated_at)
               VALUES (:site_id, :company, :address, :city, :attention, :hours, :instructions, :updated_at)
               ON CONFLICT(site_id) DO UPDATE SET
                   company=excluded.company, address=excluded.address, city=excluded.city,
                   attention=excluded.attention, hours=excluded.hours,
                   instructions=excluded.instructions, updated_at=excluded.updated_at""",
            {"site_id": site_id, **values, "updated_at": now},
        )
        conn.commit()
        print(f"[SiteSettings] ship-to saved for {site_id!r}")
        return True
    except Exception as exc:
        print(f"[SiteSettings] upsert_shipto failed for {site_id!r}: {exc}")
        return False
