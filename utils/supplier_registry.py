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

Contact resolution (added by _migrate; resolved email reuses base `contact_email`):
  contact_method                 text        "store" | "generic_inbox" | "human_flag"
  contact_status                 text        "resolved" | "needs_human" | "bounced"
  contact_resolved_at            text        ISO 8601 UTC
The above is the FALLBACK (generic inbox). The PRIMARY (named, Apollo-enriched)
contact sits above it (dual-contact model; set only by the escalation, never the
free default path):
  primary_contact_email          text
  primary_contact_name           text
  primary_contact_title          text
  primary_contact_person_id      text        Apollo person_id (kept even on an enrich miss)
  primary_contact_source         text        "apollo_enriched" (email) | "apollo_search" (found, no email)
  primary_contact_status         text        "resolved" | "found_no_email" | "no_response" | "bounced" | "none"
  primary_contact_at             text        ISO 8601 UTC

primary_contact_status values:
  "resolved"       — a named primary WITH a usable email (escalation enrich hit).
  "found_no_email" — search found a real sales person but no email is available
                     (name/title/person_id kept; the generic inbox is still used).
  "none"           — no sales person found at all.
  "no_response" / "bounced" — had a primary; it didn't respond / bounced.
Only "resolved" makes the primary the effective contact; the others fall back to
the generic inbox (effective_contact / recipient_set unchanged).

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

# Sent-message log (one row per outbound RFQ send attempt — many per supplier).
# Lives alongside the contact data so later inbound matching (bounce/quote
# ingestion) can join on supplier_domain / thread_id. status is "sent" (live, not
# reachable yet) or "stubbed" (gated/no-creds). message_id/thread_id are provider
# placeholders (NULL) until the live provider returns real ids.
_SENT_MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS sent_messages (
    id                 TEXT PRIMARY KEY,
    run_id             TEXT,
    supplier_domain    TEXT,
    vendor_name        TEXT,
    recipients_to_json TEXT,
    recipients_cc_json TEXT,
    subject            TEXT,
    body               TEXT,
    message_id         TEXT,
    thread_id          TEXT,
    status             TEXT NOT NULL,
    approved_by        TEXT,
    sent_at            TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
"""

# Human-review queue for inbound-extracted data (Layer 3). Extraction NEVER updates
# the platform directly — it lands here as a "pending" (or "needs_human_review") row;
# a human confirm applies it (price_db for quotes, primary contact for contacts) and a
# reject discards it. kind = "quote" | "contact". payload_json holds the extracted
# Quote / NominatedContact dict. manufacturer/part_number are captured so a confirmed
# quote can key price_db.
_REVIEW_ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS review_items (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL,
    run_id          TEXT,
    supplier_domain TEXT,
    vendor_name     TEXT,
    manufacturer    TEXT,
    part_number     TEXT,
    payload_json    TEXT,
    confidence      REAL,
    raw_source      TEXT,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
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

# Contact-resolution columns added by _migrate() (all nullable). The resolved email
# reuses the existing base `contact_email` column — these add HOW/STATUS only.
_CONTACT_COLUMNS: dict[str, str] = {
    "contact_method":      "TEXT",   # "store" | "generic_inbox" | "human_flag"
    "contact_status":      "TEXT",   # "resolved" | "needs_human" | "bounced"
    "contact_resolved_at": "TEXT",   # ISO 8601 UTC
}

# Fields upsert_contact() may write (resolved email reuses the base contact_email).
_CONTACT_WRITABLE = {"contact_email", "contact_method", "contact_status", "contact_resolved_at"}

# PRIMARY (named) contact columns — the Apollo-enriched escalation contact, sitting
# ABOVE the generic-inbox fallback (contact_email/contact_method). Added by _migrate.
_PRIMARY_COLUMNS: dict[str, str] = {
    "primary_contact_email":     "TEXT",
    "primary_contact_name":      "TEXT",
    "primary_contact_title":     "TEXT",
    "primary_contact_person_id": "TEXT",   # Apollo person_id (kept even on enrich miss)
    "primary_contact_source":    "TEXT",   # "apollo_enriched" | "apollo_search"
    "primary_contact_status":    "TEXT",   # resolved | found_no_email | no_response | bounced | none
    "primary_contact_at":        "TEXT",   # ISO 8601 UTC
}
_PRIMARY_WRITABLE = set(_PRIMARY_COLUMNS)

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
    """Idempotently add the Apollo + contact-resolution columns if missing.

    Safe to run on every connection: checks PRAGMA table_info and only issues
    ALTER TABLE ADD COLUMN for columns not already present. Existing rows and
    data are preserved (new columns are nullable, no backfill).
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(suppliers)").fetchall()}
    added = []
    for col, coltype in {**_APOLLO_COLUMNS, **_CONTACT_COLUMNS, **_PRIMARY_COLUMNS}.items():
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
    conn.execute(_SENT_MESSAGES_DDL)
    conn.execute(_REVIEW_ITEMS_DDL)
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


def upsert_contact(domain: str, fields: dict) -> bool:
    """Write resolved-contact fields to the supplier row keyed by domain.

    Upsert semantics (mirrors upsert_apollo_data): creates a minimal discovery_only
    row if the domain is absent, then writes the contact fields. Only
    `_CONTACT_WRITABLE` keys are accepted (resolved email reuses the base
    `contact_email` column). Returns True on a write.
    """
    norm = _normalize_domain(domain)
    if not norm:
        print("[SupplierRegistry] upsert_contact skipped -- empty domain")
        return False

    updates = {k: v for k, v in (fields or {}).items() if k in _CONTACT_WRITABLE}
    if not updates:
        print(f"[SupplierRegistry] upsert_contact skipped -- no contact fields for {norm!r}")
        return False

    # Stamp the resolution time (naive UTC, matching the store) unless caller pinned it.
    updates.setdefault("contact_resolved_at", datetime.utcnow().isoformat())
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
        cursor = conn.execute(
            f"UPDATE suppliers SET {set_clause} WHERE domain = :_domain",
            {**updates, "_domain": norm},
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as exc:
        print(f"[SupplierRegistry] upsert_contact failed for {norm!r}: {exc}")
        return False


def upsert_primary_contact(domain: str, fields: dict) -> bool:
    """Write the PRIMARY (named, Apollo-enriched) contact, keyed by domain.

    Upsert semantics (mirrors upsert_contact): creates a discovery_only stub if the
    domain is absent. Only `_PRIMARY_WRITABLE` keys accepted. Auto-stamps
    primary_contact_at. The generic-inbox fallback (contact_email/contact_method) is
    left untouched. Returns True on a write.
    """
    norm = _normalize_domain(domain)
    if not norm:
        print("[SupplierRegistry] upsert_primary_contact skipped -- empty domain")
        return False

    updates = {k: v for k, v in (fields or {}).items() if k in _PRIMARY_WRITABLE}
    if not updates:
        print(f"[SupplierRegistry] upsert_primary_contact skipped -- no primary fields for {norm!r}")
        return False

    updates.setdefault("primary_contact_at", datetime.utcnow().isoformat())
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
        cursor = conn.execute(
            f"UPDATE suppliers SET {set_clause} WHERE domain = :_domain",
            {**updates, "_domain": norm},
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as exc:
        print(f"[SupplierRegistry] upsert_primary_contact failed for {norm!r}: {exc}")
        return False


def mark_contact_bounced(domain: str, which: str = "generic") -> bool:
    """Bounce primitive (logic only — the future inbound-bounce handler calls this).

    which="generic" (default): clear the generic-inbox contact_email and set
      contact_status='bounced' — a trigger to escalate (try a named contact) and a
      signal the next free resolution must not reuse the dead address.
    which="primary": clear primary_contact_email and set primary_contact_status=
      'bounced' — failover then uses the generic-inbox fallback (see effective_contact).
    """
    norm = _normalize_domain(domain)
    if not norm:
        return False
    if which == "primary":
        sql = ("UPDATE suppliers SET primary_contact_email = NULL, "
               "primary_contact_status = 'bounced', updated_at = ? WHERE domain = ?")
    else:
        sql = ("UPDATE suppliers SET contact_email = NULL, contact_status = 'bounced', "
               "updated_at = ? WHERE domain = ?")
    try:
        conn = _get_conn()
        cursor = conn.execute(sql, (datetime.utcnow().isoformat(), norm))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as exc:
        print(f"[SupplierRegistry] mark_contact_bounced({which}) failed for {norm!r}: {exc}")
        return False


def effective_contact(record: Optional[dict]) -> dict:
    """Failover decision (data-model only): which email to actually use.

    Rule: a resolved PRIMARY (named) contact wins; otherwise (primary none /
    no_response / bounced) fall back to the generic inbox (when not itself bounced);
    otherwise none. The future send layer sets primary_contact_status and calls this.

    Returns {"email": str|None, "source": "primary"|"fallback"|"none"}.
    """
    if not record:
        return {"email": None, "source": "none"}
    if record.get("primary_contact_email") and record.get("primary_contact_status") == "resolved":
        return {"email": record["primary_contact_email"], "source": "primary"}
    if record.get("contact_email") and record.get("contact_status") != "bounced":
        return {"email": record["contact_email"], "source": "fallback"}
    return {"email": None, "source": "none"}


def recipient_set(record: Optional[dict]) -> dict:
    """Assemble the To/CC recipient set for one outbound message (data-model only).

    Same precedence as effective_contact, but returns BOTH addresses on one message
    (the send layer addresses a single email to the set, not two emails):
      - resolved PRIMARY present -> To: [primary]; CC: [generic] when not bounced.
      - no usable primary        -> To: [generic] when not bounced.
      - both bounced / absent     -> empty (caller must NOT send; human-flag).
    A bounced address (contact_status / primary_contact_status == "bounced") is
    excluded, reusing the bounce model.

    Returns {"to": list[str], "cc": list[str]}.
    """
    to: list[str] = []
    cc: list[str] = []
    if not record:
        return {"to": to, "cc": cc}

    primary = record.get("primary_contact_email")
    primary_ok = bool(primary) and record.get("primary_contact_status") == "resolved"
    generic = record.get("contact_email")
    generic_ok = bool(generic) and record.get("contact_status") != "bounced"

    if primary_ok:
        to.append(primary)
        if generic_ok:
            cc.append(generic)
    elif generic_ok:
        to.append(generic)
    return {"to": to, "cc": cc}


def record_sent_message(
    run_id: Optional[str],
    supplier_domain: Optional[str],
    vendor_name: Optional[str],
    to: list[str],
    cc: Optional[list[str]] = None,
    subject: Optional[str] = None,
    body: Optional[str] = None,
    status: str = "stubbed",
    message_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    approved_by: Optional[str] = None,
    sent_at: Optional[str] = None,
) -> Optional[str]:
    """Persist one outbound-send record (the key inbound matching will later join on).

    status is "sent" (live) or "stubbed" (gated/no-creds). message_id/thread_id are
    provider placeholders (None) until the live provider returns real ids. Returns
    the new row id, or None on failure (fail-soft — never raises into the flow).
    """
    domain = _normalize_domain(supplier_domain) if supplier_domain else None
    now = datetime.utcnow().isoformat()
    row = (
        str(uuid.uuid4()),
        run_id,
        domain,
        vendor_name,
        json.dumps(to or []),
        json.dumps(cc or []),
        subject,
        body,
        message_id,
        thread_id,
        status,
        approved_by,
        sent_at or now,
        now,
    )
    try:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO sent_messages
               (id, run_id, supplier_domain, vendor_name, recipients_to_json,
                recipients_cc_json, subject, body, message_id, thread_id, status,
                approved_by, sent_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            row,
        )
        conn.commit()
        print(f"[SupplierRegistry] Sent-message recorded: {vendor_name} ({domain}) status={status}")
        return row[0]
    except Exception as exc:
        print(f"[SupplierRegistry] record_sent_message failed for {domain!r}: {exc}")
        return None


def get_sent_messages(
    run_id: Optional[str] = None, domain: Optional[str] = None
) -> list[dict]:
    """Return sent-message rows, optionally filtered by run_id and/or domain
    (newest first). JSON recipient columns are decoded into `recipients_to` /
    `recipients_cc` lists. Fail-soft: returns [] on error."""
    clauses: list[str] = []
    params: list = []
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if domain is not None:
        clauses.append("supplier_domain = ?")
        params.append(_normalize_domain(domain))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM sent_messages{where} ORDER BY created_at DESC", params
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["recipients_to"] = json.loads(d.get("recipients_to_json") or "[]")
            d["recipients_cc"] = json.loads(d.get("recipients_cc_json") or "[]")
            out.append(d)
        return out
    except Exception as exc:
        print(f"[SupplierRegistry] get_sent_messages failed: {exc}")
        return []


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


def record_review_item(
    kind: str,
    payload: dict,
    *,
    status: str = "pending",
    run_id: Optional[str] = None,
    supplier_domain: Optional[str] = None,
    vendor_name: Optional[str] = None,
    manufacturer: Optional[str] = None,
    part_number: Optional[str] = None,
    confidence: Optional[float] = None,
    raw_source: Optional[str] = None,
) -> Optional[str]:
    """Queue one inbound-extracted item (kind="quote"|"contact") for human review.

    NOTHING is applied to the platform here — this only records the extracted payload
    so a human can confirm/reject later. Returns the new row id, or None on failure
    (fail-soft)."""
    domain = _normalize_domain(supplier_domain) if supplier_domain else None
    now = datetime.utcnow().isoformat()
    row = (
        str(uuid.uuid4()), kind, status, run_id, domain, vendor_name,
        manufacturer, part_number, json.dumps(payload or {}),
        confidence, raw_source, now, None,
    )
    try:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO review_items
               (id, kind, status, run_id, supplier_domain, vendor_name, manufacturer,
                part_number, payload_json, confidence, raw_source, created_at, resolved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            row,
        )
        conn.commit()
        print(f"[SupplierRegistry] Review item queued: {kind} status={status} "
              f"({vendor_name} / {domain})")
        return row[0]
    except Exception as exc:
        print(f"[SupplierRegistry] record_review_item failed: {exc}")
        return None


def _row_to_review_item(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["payload"] = json.loads(d.get("payload_json") or "{}")
    except (ValueError, TypeError):
        d["payload"] = {}
    return d


def get_review_items(
    status: Optional[str] = None, kind: Optional[str] = None,
    run_id: Optional[str] = None,
) -> list[dict]:
    """Return review-queue rows (newest first), optionally filtered. payload_json is
    decoded into `payload`. Fail-soft: [] on error."""
    clauses: list[str] = []
    params: list = []
    for col, val in (("status", status), ("kind", kind), ("run_id", run_id)):
        if val is not None:
            clauses.append(f"{col} = ?")
            params.append(val)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM review_items{where} ORDER BY created_at DESC", params
        ).fetchall()
        return [_row_to_review_item(r) for r in rows]
    except Exception as exc:
        print(f"[SupplierRegistry] get_review_items failed: {exc}")
        return []


def get_review_item(item_id: str) -> Optional[dict]:
    """Return one review item by id (payload decoded), or None."""
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM review_items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_review_item(r) if r else None
    except Exception:
        return None


def set_review_item_status(item_id: str, status: str) -> bool:
    """Set a review item's status (e.g. confirmed/rejected) + stamp resolved_at."""
    try:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE review_items SET status = ?, resolved_at = ? WHERE id = ?",
            (status, datetime.utcnow().isoformat(), item_id),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception as exc:
        print(f"[SupplierRegistry] set_review_item_status failed: {exc}")
        return False


def all_entries() -> list[dict]:
    """Return all supplier records for diagnostics."""
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()]
    except Exception:
        return []
