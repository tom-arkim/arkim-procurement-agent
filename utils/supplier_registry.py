"""
Arkim Supplier Registry — SQLite-backed supplier onboarding state.

Schema (suppliers table):
  id                          UUID string   (primary key)
  domain                      text UNIQUE   (normalized: lowercase, www. stripped)
  name                        text
  onboarding_status           text          "onboarded_arkim_supplier" | "discovery_only" | "invited"
  contact_email               text          (nullable)
  contract_status             text          (nullable: "active" | "pending" | "none")
  vendor_authorization_status text          "Authorized" | "Unauthorized" | "Unknown"
  counterfeit_risk_notes      text          (nullable)
  created_at                  text          ISO 8601 UTC
  updated_at                  text          ISO 8601 UTC

Seeded with known Tier 1/1.5 vendors from _VENDOR_DOMAINS (all "discovery_only" initially).
New vendors encountered during sourcing are auto-added as "discovery_only".
"""

import os
import re
import sqlite3
import uuid
from datetime import datetime
from typing import Optional

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DB_PATH  = os.path.join(_DATA_DIR, "supplier_registry.sqlite")

_DDL = """
CREATE TABLE IF NOT EXISTS suppliers (
    id                          TEXT PRIMARY KEY,
    domain                      TEXT UNIQUE,
    name                        TEXT NOT NULL,
    onboarding_status           TEXT NOT NULL DEFAULT 'discovery_only',
    contact_email               TEXT,
    contract_status             TEXT,
    vendor_authorization_status TEXT NOT NULL DEFAULT 'Unknown',
    counterfeit_risk_notes      TEXT,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL
);
"""

# Seed data — Tier 1 and Tier 1.5 known vendors, all discovery_only until onboarded
_SEED_VENDORS = [
    ("grainger.com",          "Grainger"),
    ("mcmaster.com",          "McMaster-Carr"),
    ("mscdirect.com",         "MSC Industrial"),
    ("motionindustries.com",  "Motion Industries"),
    ("applied.com",           "Applied Industrial"),
    ("pumpman.com",           "Pumpman"),
    ("pumpproducts.com",      "Pump Products"),
    ("pumpcatalog.com",       "Pump Catalog"),
    ("zoro.com",              "Zoro"),
    ("globalindustrial.com",  "Global Industrial"),
    ("fastenal.com",          "Fastenal"),
]


def _normalize_domain(raw: str) -> str:
    """Lowercase, strip www., strip trailing slash and path."""
    raw = (raw or "").lower().strip()
    try:
        from urllib.parse import urlparse
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = parsed.hostname or raw
    except Exception:
        host = raw
    host = re.sub(r"^www\.", "", host)
    return host.strip()


def _get_conn() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(_DDL)
    conn.commit()
    _maybe_seed(conn)
    return conn


def _maybe_seed(conn: sqlite3.Connection) -> None:
    """Insert seed vendors if not already present."""
    count = conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
    if count > 0:
        return
    now = datetime.utcnow().isoformat()
    for domain, name in _SEED_VENDORS:
        conn.execute(
            """INSERT OR IGNORE INTO suppliers
               (id, domain, name, onboarding_status, vendor_authorization_status, created_at, updated_at)
               VALUES (?,?,?,'discovery_only','Unknown',?,?)""",
            (str(uuid.uuid4()), domain, name, now, now),
        )
    conn.commit()
    print(f"[SupplierRegistry] Seeded {len(_SEED_VENDORS)} known vendors.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_registry() -> dict:
    """Return all suppliers indexed by lowercased name for O(1) lookup."""
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM suppliers").fetchall()
        result = {}
        for r in rows:
            d = dict(r)
            result[d["name"].lower()] = d
            if d.get("domain"):
                result[d["domain"]] = d
        return result
    except Exception as exc:
        print(f"[SupplierRegistry] load_registry failed: {exc}")
        return {}


def lookup_by_domain(domain: str) -> Optional[dict]:
    """Look up a supplier by domain (normalized). Returns None if not found."""
    norm = _normalize_domain(domain)
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM suppliers WHERE domain = ?", (norm,)
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def lookup_supplier(name: str) -> Optional[dict]:
    """Look up by vendor name (case-insensitive)."""
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM suppliers WHERE LOWER(name) = ?", (name.lower(),)
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def get_contact_email(name: str) -> Optional[str]:
    entry = lookup_supplier(name)
    return entry.get("contact_email") if entry else None


def create_stub(name: str, domain: str = "", source_url: str = "") -> dict:
    """Create a discovery_only stub if not already registered.

    Returns the existing entry if already present (idempotent).
    """
    if domain:
        existing = lookup_by_domain(domain)
    else:
        existing = lookup_supplier(name)
    if existing:
        return existing

    norm_domain = _normalize_domain(domain or source_url)
    now = datetime.utcnow().isoformat()
    stub = {
        "id":                          str(uuid.uuid4()),
        "domain":                      norm_domain or None,
        "name":                        name,
        "onboarding_status":           "discovery_only",
        "contact_email":               None,
        "contract_status":             None,
        "vendor_authorization_status": "Unknown",
        "counterfeit_risk_notes":      None,
        "created_at":                  now,
        "updated_at":                  now,
    }
    try:
        conn = _get_conn()
        conn.execute(
            """INSERT OR IGNORE INTO suppliers
               (id, domain, name, onboarding_status, vendor_authorization_status, created_at, updated_at)
               VALUES (:id, :domain, :name, :onboarding_status, :vendor_authorization_status, :created_at, :updated_at)""",
            stub,
        )
        conn.commit()
        print(f"[SupplierRegistry] Created stub: {name} ({norm_domain})")
    except Exception as exc:
        print(f"[SupplierRegistry] create_stub failed: {exc}")
    return stub


def enrich_option(option) -> None:
    """Populate onboarding_status and vendor_authorization_status from registry.

    Mutates the SourcingOption in place.
    Looks up by URL domain first, then by vendor name.
    Creates a discovery_only stub if vendor is completely unknown.
    """
    url  = getattr(option, "source_url", None) or ""
    name = option.vendor_name

    record = None
    if url:
        domain = _normalize_domain(url)
        record = lookup_by_domain(domain)
    if record is None:
        record = lookup_supplier(name)
    if record is None:
        # Auto-register unknown vendor as discovery_only
        record = create_stub(name, source_url=url)

    if record:
        option.onboarding_status           = record.get("onboarding_status", "discovery_only")
        option.vendor_authorization_status = record.get("vendor_authorization_status", "Unknown")


def update_supplier(name: str, **fields) -> bool:
    """Update mutable fields on a supplier record. Returns True on success.

    Allowed fields: onboarding_status, contact_email, contract_status,
                    vendor_authorization_status, counterfeit_risk_notes.
    """
    allowed = {
        "onboarding_status", "contact_email", "contract_status",
        "vendor_authorization_status", "counterfeit_risk_notes",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates["updated_at"] = datetime.utcnow().isoformat()
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["_name_lower"] = name.lower()
    try:
        conn = _get_conn()
        cursor = conn.execute(
            f"UPDATE suppliers SET {set_clause} WHERE LOWER(name) = :_name_lower",
            updates,
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as exc:
        print(f"[SupplierRegistry] update failed: {exc}")
        return False


def all_entries() -> list[dict]:
    """Return all supplier records for diagnostics."""
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()]
    except Exception:
        return []
