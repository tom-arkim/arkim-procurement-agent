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

Apollo enrichment cache (added by _migrate; all nullable, populated by upsert_apollo_data):
  apollo_org_name                text        Apollo's resolved org name (domain->org mismatch checks)
  apollo_description             text        org short description
  apollo_industry                text
  apollo_keywords                text        JSON-encoded list[str]
  apollo_country                 text
  apollo_state                   text
  apollo_raw_address             text
  is_us_confirmed                integer     0 | 1 | NULL (SQLite has no bool)
  suitability_status             text        "confirmed" | "unconfirmed_flag_human" | "rejected_unsuitable" | NULL
  apollo_enriched_at             text        ISO 8601 UTC of last Apollo enrich (drives staleness)
  apollo_departmental_head_count text        JSON-encoded dict (contact-resolution support)
  apollo_technology_names        text        JSON-encoded list[str] (contact-resolution support)

`suitability_status` is INDEPENDENT of `onboarding_status`: the former records Apollo's
US+requirement verdict, the latter the onboarding lifecycle. A supplier can legitimately be
suitability_status="confirmed" AND onboarding_status="discovery_only" (validated by Apollo but
not yet onboarded as an Arkim supplier).

Seeded with known Tier 1/1.5 vendors from _VENDOR_DOMAINS (all "discovery_only" initially).
New vendors encountered during sourcing are auto-added as "discovery_only".
"""

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
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

# Apollo enrichment columns added by _migrate(). All nullable so the migration is
# a safe ALTER TABLE ADD COLUMN on existing rows (no default backfill needed).
# Column -> SQLite type. JSON-valued columns are stored as TEXT.
_APOLLO_COLUMNS: dict[str, str] = {
    "apollo_org_name":                "TEXT",     # Apollo's resolved org name (domain->org mismatch checks)
    "apollo_description":             "TEXT",
    "apollo_industry":                "TEXT",
    "apollo_keywords":                "TEXT",     # JSON list[str]
    "apollo_country":                 "TEXT",
    "apollo_state":                   "TEXT",
    "apollo_raw_address":             "TEXT",
    "is_us_confirmed":                "INTEGER",  # 0 | 1 | NULL
    "suitability_status":             "TEXT",
    "apollo_enriched_at":             "TEXT",     # ISO 8601 UTC
    "apollo_departmental_head_count": "TEXT",     # JSON dict
    "apollo_technology_names":        "TEXT",     # JSON list[str]
}

# Apollo columns whose values may arrive as list/dict and are stored as JSON text.
_JSON_APOLLO_FIELDS = {
    "apollo_keywords",
    "apollo_departmental_head_count",
    "apollo_technology_names",
}

# suitability_status vocabulary (independent of onboarding_status — see module docstring).
SUITABILITY_STATUSES = {"confirmed", "unconfirmed_flag_human", "rejected_unsuitable"}

# onboarding_status values treated as "onboarded" — exempt from re-enrichment staleness.
_ONBOARDED_STATUSES = {"onboarded_arkim_supplier"}

# Default re-enrichment TTL for confirmed-not-onboarded suppliers.
_REENRICH_TTL_DAYS = 180

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


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotently add the Apollo enrichment columns if missing.

    Safe to run on every connection: checks PRAGMA table_info and only issues
    ALTER TABLE ADD COLUMN for columns not already present. Existing rows and
    data are preserved (new columns are nullable, no backfill).
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(suppliers)").fetchall()}
    added = []
    for col, coltype in _APOLLO_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE suppliers ADD COLUMN {col} {coltype}")
            added.append(col)
    if added:
        conn.commit()
        print(f"[SupplierRegistry] Migration added {len(added)} column(s): {', '.join(added)}")


def _get_conn() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(_DDL)
    conn.commit()
    _migrate(conn)
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


def upsert_apollo_data(domain: str, fields: dict) -> bool:
    """Write Apollo enrichment fields to the supplier row keyed by domain.

    Upsert semantics: if no row exists for the domain, a minimal discovery_only
    row is created first (so cache write-back on an Apollo miss always lands),
    then the Apollo columns are written. Stamps `apollo_enriched_at` to now
    unless the caller supplies it explicitly (callers normally don't).

    Only the Apollo columns in `_APOLLO_COLUMNS` are accepted; anything else is
    ignored. JSON-valued fields (keywords, departmental_head_count,
    technology_names) may be passed as list/dict and are serialized to JSON text.
    `is_us_confirmed` may be passed as a bool and is stored as 0/1.

    Does NOT touch the name-keyed update_supplier path. Returns True on a write.
    """
    norm = _normalize_domain(domain)
    if not norm:
        print("[SupplierRegistry] upsert_apollo_data skipped -- empty domain")
        return False

    updates = {k: v for k, v in (fields or {}).items() if k in _APOLLO_COLUMNS}
    if not updates:
        print(f"[SupplierRegistry] upsert_apollo_data skipped -- no Apollo fields for {norm!r}")
        return False

    # Serialize JSON-valued fields if given as list/dict.
    for jf in _JSON_APOLLO_FIELDS:
        if jf in updates and not isinstance(updates[jf], (str, type(None))):
            updates[jf] = json.dumps(updates[jf])

    # bool -> int for the SQLite "boolean" column.
    if updates.get("is_us_confirmed") is not None:
        updates["is_us_confirmed"] = int(bool(updates["is_us_confirmed"]))

    # Stamp the enrich date (drives staleness) unless caller pinned it.
    updates.setdefault("apollo_enriched_at", datetime.utcnow().isoformat())
    updates["updated_at"] = datetime.utcnow().isoformat()

    try:
        conn = _get_conn()
        exists = conn.execute(
            "SELECT 1 FROM suppliers WHERE domain = ?", (norm,)
        ).fetchone()
        if not exists:
            now = datetime.utcnow().isoformat()
            conn.execute(
                """INSERT OR IGNORE INTO suppliers
                   (id, domain, name, onboarding_status, vendor_authorization_status, created_at, updated_at)
                   VALUES (?,?,?,'discovery_only','Unknown',?,?)""",
                (str(uuid.uuid4()), norm, norm, now, now),
            )
        set_clause = ", ".join(f"{k} = :{k}" for k in updates)
        params = {**updates, "_domain": norm}
        cursor = conn.execute(
            f"UPDATE suppliers SET {set_clause} WHERE domain = :_domain", params
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as exc:
        print(f"[SupplierRegistry] upsert_apollo_data failed for {norm!r}: {exc}")
        return False


def needs_reenrichment(supplier: Optional[dict], ttl_days: int = _REENRICH_TTL_DAYS) -> bool:
    """Whether a supplier's Apollo data should be re-fetched (logic only — no API call).

    Rules (CLAUDE.md §9 / rollout plan §5):
      - Onboarded / Tier 1 suppliers are EXEMPT — onboarding is their source of
        truth — so they return False regardless of enrich date.
      - Otherwise, True if `apollo_enriched_at` is older than `ttl_days` (180 by
        default). Never-enriched (missing/blank/unparseable date) on a
        non-onboarded supplier counts as stale -> True.
      - A falsy supplier returns False (nothing to re-enrich).
    """
    if not supplier:
        return False
    if supplier.get("onboarding_status") in _ONBOARDED_STATUSES:
        return False
    enriched_at = supplier.get("apollo_enriched_at")
    if not enriched_at:
        return True
    try:
        dt = datetime.fromisoformat(enriched_at)
    except (ValueError, TypeError):
        return True
    # Normalize a tz-aware timestamp to naive UTC so the subtraction below (against
    # a naive utcnow()) never raises "can't subtract offset-naive and offset-aware".
    # Writers use naive utcnow() today; this tolerates an aware value defensively.
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return (datetime.utcnow() - dt) > timedelta(days=ttl_days)


def all_entries() -> list[dict]:
    """Return all supplier records for diagnostics."""
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()]
    except Exception:
        return []
