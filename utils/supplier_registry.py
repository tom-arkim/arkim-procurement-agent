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
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


# ---------------------------------------------------------------------------
# TIER1_V2 feature flag (Night 3) — the supplier-scope redesign gate
# ---------------------------------------------------------------------------
# Mirrors SCORING_V2 / INTAKE_TYPE_AWARE: strict truthy parse (only
# "1/true/yes/on" enables; everything else -> OFF, fails safe). Default OFF ->
# the registry extensions (supplier scope schema, enforced lifecycle state
# machine, graduation from Apollo refresh, scope lookups) are DORMANT and the
# Apollo-cache clarifier path is byte-identical to pre-Night-3 behavior (T5).
def _env_truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


TIER1_V2: bool = _env_truthy(os.environ.get("TIER1_V2"))

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
    resolved_at     TEXT,
    thread_id       TEXT,
    sent_message_id TEXT,
    message_id      TEXT
);
"""

# Deterministic-join keys (State C, increment 3a) carried from the matched sent_messages
# row onto an inbound quote so a returned quote ties back to the EXACT outbound we sent
# (thread_id is the reliable live key; sent_message_id is the row id; message_id the
# original outbound id). All nullable — legacy + out-of-thread rows lack them and fall
# back to the (run_id + supplier_domain) domain join. Added to existing tables by
# _migrate (ALTER TABLE ADD COLUMN), like the suppliers columns above.
_REVIEW_ITEM_LINK_COLUMNS: dict[str, str] = {
    "thread_id":       "TEXT",
    "sent_message_id": "TEXT",
    "message_id":      "TEXT",
}

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

# ---------------------------------------------------------------------------
# Night 3 — TIER1_V2 supplier-scope schema (research model)
# ---------------------------------------------------------------------------
# The supplier entity per the settled research record: brands x classes x
# territory, tri-state authorization, enforced lifecycle. This extends the
# Apollo-cache `suppliers` row with scope data carried in child tables + a few
# nullable JSON columns, ALL populated only under TIER1_V2 (flag-off = dormant,
# the Apollo clarifier byte-identical — T5). Existing Apollo-cache rows remain
# valid discovered/contacted-stage records (T4); the new lifecycle is a SEPARATE
# column (`tier1_lifecycle`) so the existing `onboarding_status` semantics the
# clarifier depends on are untouched.
#
# Lifecycle (enforced state machine — see TIER1_TRANSITIONS):
#   discovered -> contacted -> quoted -> onboarding -> onboarded
#   suspended is the off-ramp from any pre-onboarded state; onboarded/suspended
#   are terminal-ish (onboarded can still be suspended). This MIRRORS the orders
#   state machine (utils/orders.py): a transition is legal only if the target is
#   in TIER1_TRANSITIONS[current]; illegal transitions (skip-ahead, backward,
#   un-suspend) are REJECTED, not merely recorded.
TIER1_DISCOVERED = "discovered"
TIER1_CONTACTED = "contacted"
TIER1_QUOTED = "quoted"
TIER1_ONBOARDING = "onboarding"
TIER1_ONBOARDED = "onboarded"
TIER1_SUSPENDED = "suspended"

TIER1_LIFECYCLE_STATUSES: tuple[str, ...] = (
    TIER1_DISCOVERED, TIER1_CONTACTED, TIER1_QUOTED,
    TIER1_ONBOARDING, TIER1_ONBOARDED, TIER1_SUSPENDED,
)

# Allowed forward lifecycle + suspend off-ramp. onboarded -> suspended (a
# supplier can be suspended after onboarding); suspended -> onboarded is the
# only re-activation (NOT suspended -> contacted: no backward skip). discovered
# -> contacted -> quoted -> onboarding -> onboarded is the happy path.
TIER1_TRANSITIONS: dict[str, set[str]] = {
    TIER1_DISCOVERED:  {TIER1_CONTACTED, TIER1_SUSPENDED},
    TIER1_CONTACTED:   {TIER1_QUOTED, TIER1_SUSPENDED},
    TIER1_QUOTED:      {TIER1_ONBOARDING, TIER1_SUSPENDED},
    TIER1_ONBOARDING:  {TIER1_ONBOARDED, TIER1_SUSPENDED},
    TIER1_ONBOARDED:   {TIER1_SUSPENDED},
    TIER1_SUSPENDED:   {TIER1_ONBOARDED},   # re-activate only (no backward skip)
}

# Relationship vocabulary for a brand a supplier carries (tri-state filter).
#   AUTHORIZED            — the supplier is an authorized distributor/dealer for
#                           the brand (the OEM-sanctioned channel).
#   CARRIES               — the supplier stocks/sells the brand but is NOT a
#                           sanctioned channel (broad-line distributor).
#   AFTERMARKET_COMPATIBLE — the supplier sells an aftermarket/cross-reference
#                           part COMPATIBLE with the brand (not the brand itself).
BRAND_AUTHORIZED = "AUTHORIZED"
BRAND_CARRIES = "CARRIES"
BRAND_AFTERMARKET_COMPATIBLE = "AFTERMARKET_COMPATIBLE"
BRAND_RELATIONSHIPS: tuple[str, ...] = (
    BRAND_AUTHORIZED, BRAND_CARRIES, BRAND_AFTERMARKET_COMPATIBLE,
)

# Ship-area sentinel for nationwide US coverage (vs an explicit states[] list).
SHIP_AREA_NATIONWIDE_US = "NATIONWIDE_US"

# TIER1_V2 supplier-scope columns added by _migrate to the base `suppliers` table
# (all nullable, ADD COLUMN — safe on existing rows, no backfill). The scope
# child rows live in their own tables (supplier_classes / supplier_brands /
# supplier_local_service) keyed by supplier_id, so the lookups (by class, by
# brand+relationship, by territory) are queryable rather than JSON-scans.
_TIER1_SUPPLIER_COLUMNS: dict[str, str] = {
    "tier1_lifecycle":   "TEXT",   # discovered|contacted|quoted|onboarding|onboarded|suspended
    "ship_area_json":    "TEXT",   # {"kind":"NATIONWIDE_US"} | {"kind":"STATES","states":["NY",...]}
    "verticals_json":    "TEXT",   # JSON list[str] of industry verticals
    "performance_json":  "TEXT",   # JSON dict (placeholder — {} until perf data lands)
    "scope_source":      "TEXT",   # provenance: where the scope was asserted from
    "scope_set_by":      "TEXT",   # provenance: who set it (user id / system)
    "scope_set_at":      "TEXT",   # provenance: ISO 8601 UTC of last scope write
}
_TIER1_SUPPLIER_WRITABLE = set(_TIER1_SUPPLIER_COLUMNS)

# Source vocabulary for a supplier-class row (where the class coverage came from).
SCOPE_SOURCE_MANUAL = "manual"
SCOPE_SOURCE_APOLLO = "apollo"
SCOPE_SOURCE_INFERRED = "inferred"
SCOPE_SOURCES: tuple[str, ...] = (SCOPE_SOURCE_MANUAL, SCOPE_SOURCE_APOLLO, SCOPE_SOURCE_INFERRED)

# JSON-valued TIER1 columns decoded on read.
_TIER1_JSON_FIELDS = {"ship_area_json", "verticals_json", "performance_json"}

# Child-table DDL for the scope (created in _get_conn, IF NOT EXISTS — harmless
# empty tables even flag-off; only populated under TIER1_V2).
_SUPPLIER_CLASSES_DDL = """
CREATE TABLE IF NOT EXISTS supplier_classes (
    id              TEXT PRIMARY KEY,
    supplier_id     TEXT NOT NULL,
    class_id        TEXT NOT NULL,        -- NounClass.canonical (e.g. 'SEAL')
    subtype         TEXT,                 -- free-text sub-type within the class
    unspsc          TEXT,                 -- commodity code (crosswalk from the class)
    is_core         INTEGER NOT NULL DEFAULT 0,  -- 0|1 — core competency vs incidental
    confidence      REAL,                 -- 0..1 — how confident the coverage claim is
    source          TEXT,                 -- manual|apollo|inferred
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(supplier_id, class_id, subtype)
);
"""

_SUPPLIER_BRANDS_DDL = """
CREATE TABLE IF NOT EXISTS supplier_brands (
    id                       TEXT PRIMARY KEY,
    supplier_id              TEXT NOT NULL,
    brand_id                 TEXT NOT NULL,          -- manufacturer/brand canonical id
    relationship             TEXT NOT NULL,          -- AUTHORIZED|CARRIES|AFTERMARKET_COMPATIBLE
    authorized_territory     TEXT,                   -- territory where authorized (nullable)
    classes_for_brand_json   TEXT,                   -- JSON list[str] of class_ids the brand coverage spans
    evidence                 TEXT,                   -- free-text provenance / source link
    confidence               REAL,                   -- 0..1
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    UNIQUE(supplier_id, brand_id, relationship)
);
"""

_SUPPLIER_LOCAL_SERVICE_DDL = """
CREATE TABLE IF NOT EXISTS supplier_local_service (
    id              TEXT PRIMARY KEY,
    supplier_id     TEXT NOT NULL,
    branch_zip      TEXT NOT NULL,          -- branch ZIP/postal code
    radius_miles    REAL,                   -- service radius around the branch
    services_json   TEXT,                   -- JSON list[str] of service capabilities
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""

# Night 5 — supplier notification EVENTS (T3). One row per matched-request→notify
# event recorded for an onboarded Tier 1 supplier. The notify layer is the
# research's conservative notify≫display asymmetry: a candidate is DISPLAYED at a
# lower threshold than it is NOTIFIED on (notify requires brand-match-or-core-class
# + a per-RFQ cap). The send itself goes through the existing stubbed/flagged
# EmailSender (utils/email_sender.py) — NOTHING sends live (EMAIL_SEND_ENABLED
# defaults OFF + the conftest safety net + TIER1_V2 gate = the double-gate).
# `send_status` mirrors SendResult.status: "stubbed" (gated, the repo/test default),
# "sent" (live, unreachable in tests), "error" (fail-soft). Empty table even when
# TIER1_V2 is off (no code populates it flag-off — the notify layer no-ops, T5).
_SUPPLIER_NOTIFICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS supplier_notifications (
    id                TEXT PRIMARY KEY,
    run_id            TEXT,
    supplier_domain   TEXT NOT NULL,
    vendor_name       TEXT,
    noun_class        TEXT,                -- the matched request class (the class-gate)
    notify_reason     TEXT,                -- "brand_match" | "core_class" | "brand_match_or_core_class"
    notified_at       TEXT NOT NULL,       -- ISO 8601 UTC
    send_status       TEXT NOT NULL,       -- "stubbed" | "sent" | "error" (mirrors SendResult)
    message_id        TEXT,                -- provider placeholder until live send
    threshold         TEXT,                -- "notify" (the higher threshold that admitted it)
    metadata_json     TEXT,                -- JSON: match-explanation snapshot
    created_at        TEXT NOT NULL
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


def _tier1_can_transition(current: Optional[str], new: str) -> bool:
    """True iff `current -> new` is a legal tier1 lifecycle transition (pure; no I/O).

    A NULL/missing current (a legacy Apollo-cache row with no tier1_lifecycle yet)
    may transition to `discovered` or `suspended` only — i.e. entering the
    machine at its start state, not skipping into a later stage.
    """
    if current is None or current == "":
        return new in (TIER1_DISCOVERED, TIER1_SUSPENDED)
    return new in TIER1_TRANSITIONS.get(current, set())


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotently add the Apollo + contact-resolution columns if missing.

    Safe to run on every connection: checks PRAGMA table_info and only issues
    ALTER TABLE ADD COLUMN for columns not already present. Existing rows and
    data are preserved (new columns are nullable, no backfill).
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(suppliers)").fetchall()}
    added = []
    for col, coltype in {**_APOLLO_COLUMNS, **_CONTACT_COLUMNS,
                         **_PRIMARY_COLUMNS, **_TIER1_SUPPLIER_COLUMNS}.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE suppliers ADD COLUMN {col} {coltype}")
            added.append(col)

    # review_items deterministic-join keys (State C 3a) — same idempotent ADD COLUMN.
    ri_existing = {row[1] for row in conn.execute("PRAGMA table_info(review_items)").fetchall()}
    for col, coltype in _REVIEW_ITEM_LINK_COLUMNS.items():
        if col not in ri_existing:
            conn.execute(f"ALTER TABLE review_items ADD COLUMN {col} {coltype}")
            added.append(f"review_items.{col}")

    if added:
        conn.commit()
        print(f"[SupplierRegistry] Migration added {len(added)} column(s): {', '.join(added)}")


def _get_conn() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(_DDL)
    conn.execute(_SENT_MESSAGES_DDL)
    conn.execute(_REVIEW_ITEMS_DDL)
    # Night 3 TIER1_V2 scope child tables. CREATE TABLE IF NOT EXISTS is
    # idempotent + harmless: empty tables even when TIER1_V2 is off (the
    # extensions are dormant because no code populates them flag-off — T5).
    conn.execute(_SUPPLIER_CLASSES_DDL)
    conn.execute(_SUPPLIER_BRANDS_DDL)
    conn.execute(_SUPPLIER_LOCAL_SERVICE_DDL)
    # Night 5 — supplier notification events table (T3). IF NOT EXISTS is idempotent
    # + harmless: an empty table even when TIER1_V2 is off (the notify layer no-ops
    # flag-off — no code populates it — so flag-off is byte-identical, T5).
    conn.execute(_SUPPLIER_NOTIFICATIONS_DDL)
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
        with closing(_get_conn()) as conn:
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
        with closing(_get_conn()) as conn:
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
        with closing(_get_conn()) as conn:
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
        with closing(_get_conn()) as conn:
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
        with closing(_get_conn()) as conn:
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
        with closing(_get_conn()) as conn:
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
        with closing(_get_conn()) as conn:
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
        with closing(_get_conn()) as conn:
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
        with closing(_get_conn()) as conn:
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


def assemble_recipient_set(domain_or_url: Optional[str]) -> dict:
    """Resolve a usable recipient set for an RFQ, using ONLY the free path — NEVER Apollo.

    The standalone free cascade (mirrors SourcingAgent._resolve_contact): a cached non-bounced
    contact is reused; otherwise a generic ``sales@{domain}`` inbox is CONSTRUCTED, written back
    to the store, and used; a missing domain yields no recipients and a human-flag. ZERO Apollo
    exposure — only lookup_by_domain (SELECT), upsert_contact (write), recipient_set
    (data-model). The credit-gated people-search/enrich escalation is a SEPARATE, manually
    triggered step and is never reached here.

    Returns {"to": list[str], "cc": list[str], "status": "resolved"|"needs_human"}.
    """
    norm = _normalize_domain(domain_or_url or "")
    if not norm:
        return {"to": [], "cc": [], "status": "needs_human"}   # no domain -> honest human-flag

    rs = recipient_set(lookup_by_domain(norm))
    if not rs["to"]:
        # No usable contact yet — construct + seed the generic inbox (free; not verified).
        upsert_contact(norm, {
            "contact_email": f"sales@{norm}",
            "contact_method": "generic_inbox",
            "contact_status": "resolved",
        })
        rs = recipient_set(lookup_by_domain(norm))
    return {"to": rs["to"], "cc": rs["cc"], "status": "resolved" if rs["to"] else "needs_human"}


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
        with closing(_get_conn()) as conn:
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
        with closing(_get_conn()) as conn:
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
      - Under TIER1_V2 (Night 3): a supplier whose tier1 lifecycle is `onboarded`
        is ALSO exempt — the graduation rule (onboarded => exits Apollo staleness
        refresh), wired here at the refresh site the clarifier calls (I3). This
        branch is dormant when TIER1_V2 is off, so flag-off is byte-identical.
      - Otherwise, True if `apollo_enriched_at` is older than `ttl_days` (180 by
        default). Never-enriched (missing/blank/unparseable date) on a
        non-onboarded supplier counts as stale -> True.
      - A falsy supplier returns False (nothing to re-enrich).
    """
    if not supplier:
        return False
    if supplier.get("onboarding_status") in _ONBOARDED_STATUSES:
        return False
    # Night 3 graduation (I3): tier1-onboarded suppliers exit the Apollo refresh.
    # Gated by TIER1_V2 so flag-off behavior is byte-identical (the column is NULL
    # on legacy rows, so this never fires on the pre-existing clarifier tests).
    if TIER1_V2 and supplier.get("tier1_lifecycle") == TIER1_ONBOARDED:
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
    thread_id: Optional[str] = None,
    sent_message_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> Optional[str]:
    """Queue one inbound-extracted item (kind="quote"|"contact"|"unmatched_reply") for
    human review.

    NOTHING is applied to the platform here — this only records the extracted payload
    so a human can confirm/reject later. Returns the new row id, or None on failure
    (fail-soft).

    thread_id/sent_message_id/message_id are the deterministic-join keys (State C 3a)
    copied from the matched sent_messages row so a quote ties back to the EXACT outbound
    (not merely the supplier domain). All optional/nullable — legacy + out-of-thread
    rows omit them and the caller falls back to the domain join."""
    domain = _normalize_domain(supplier_domain) if supplier_domain else None
    now = datetime.utcnow().isoformat()
    row = (
        str(uuid.uuid4()), kind, status, run_id, domain, vendor_name,
        manufacturer, part_number, json.dumps(payload or {}),
        confidence, raw_source, now, None,
        thread_id, sent_message_id, message_id,
    )
    try:
        with closing(_get_conn()) as conn:
            conn.execute(
                """INSERT INTO review_items
                   (id, kind, status, run_id, supplier_domain, vendor_name, manufacturer,
                    part_number, payload_json, confidence, raw_source, created_at, resolved_at,
                    thread_id, sent_message_id, message_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
        with closing(_get_conn()) as conn:
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
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            r = conn.execute("SELECT * FROM review_items WHERE id = ?", (item_id,)).fetchone()
            return _row_to_review_item(r) if r else None
    except Exception:
        return None


def set_review_item_status(item_id: str, status: str) -> bool:
    """Set a review item's status (e.g. confirmed/rejected) + stamp resolved_at."""
    try:
        with closing(_get_conn()) as conn:
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
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Night 3 TIER1_V2 — supplier-scope API (internal, no HTTP endpoints)
# ---------------------------------------------------------------------------
# Every function below is gated by TIER1_V2: when the flag is OFF it no-ops
# (writes return False, reads return empty/None), so the registry extensions are
# DORMANT and the Apollo clarifier path is byte-identical to pre-Night-3 (T5).
# There are NO HTTP endpoints here — this is the internal read/write API the
# future onboarding UI / sourcing-scope layer will call.
# ---------------------------------------------------------------------------

def _tier1_dormant() -> bool:
    """True when the TIER1_V2 redesign is off (extensions must no-op)."""
    return not TIER1_V2


def _supplier_id_for(domain: str) -> Optional[str]:
    """Resolve the suppliers.id for a domain (normalized), or None if absent."""
    norm = _normalize_domain(domain)
    if not norm:
        return None
    try:
        with closing(_get_conn()) as conn:
            row = conn.execute(
                "SELECT id FROM suppliers WHERE domain = ?", (norm,)
            ).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _ensure_supplier_row(domain: str, name: Optional[str] = None) -> Optional[str]:
    """Return the supplier_id for `domain`, creating a discovery_only stub if
    absent (mirrors upsert_apollo_data's create-on-miss). Used by the scope
    writers so a scope write can land against a newly-seen domain. None on failure.
    """
    norm = _normalize_domain(domain)
    if not norm:
        return None
    sid = _supplier_id_for(norm)
    if sid:
        return sid
    # Create a minimal discovery_only stub (reuse the existing path).
    create_stub(name or norm, domain=norm)
    return _supplier_id_for(norm)


def _serialize_json_fields(payload: dict, fields: set[str]) -> dict:
    """Return a copy of `payload` with the named JSON fields serialized to text
    when given as list/dict (SQLite stores them as TEXT). String/None pass through."""
    out = dict(payload)
    for jf in fields:
        if jf in out and not isinstance(out[jf], (str, type(None))):
            out[jf] = json.dumps(out[jf])
    return out


def _decode_json_field(raw: Any, default: Any) -> Any:
    """Decode a JSON TEXT column to its value, returning `default` on null/parse error."""
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


# --- scope: classes ----------------------------------------------------------

def set_supplier_classes(domain: str, classes: list[dict],
                         *, source: str = SCOPE_SOURCE_MANUAL,
                         set_by: Optional[str] = None) -> bool:
    """Replace a supplier's class coverage with the given list (idempotent full
    replace). Each class dict: {class_id, subtype?, unspsc?, is_core?, confidence?,
    source?}. `class_id` is a NounClass canonical (e.g. 'SEAL'). Returns True on a
    write. No-ops (returns False) when TIER1_V2 is off.
    """
    if _tier1_dormant():
        return False
    sid = _ensure_supplier_row(domain)
    if not sid:
        return False
    now = datetime.utcnow().isoformat()
    try:
        with closing(_get_conn()) as conn:
            conn.execute("DELETE FROM supplier_classes WHERE supplier_id = ?", (sid,))
            for c in classes or []:
                cid = (c.get("class_id") or "").upper().strip()
                if not cid:
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO supplier_classes
                       (id, supplier_id, class_id, subtype, unspsc, is_core,
                        confidence, source, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), sid, cid, c.get("subtype"),
                     c.get("unspsc"), int(bool(c.get("is_core"))),
                     c.get("confidence"), c.get("source") or source, now, now),
                )
            # Provenance stamp on the supplier row.
            conn.execute(
                "UPDATE suppliers SET scope_source = ?, scope_set_by = ?, scope_set_at = ? "
                "WHERE id = ?",
                (source, set_by, now, sid),
            )
            conn.commit()
        return True
    except Exception as exc:
        print(f"[SupplierRegistry] set_supplier_classes failed: {exc}")
        return False


def get_supplier_classes(domain: str) -> list[dict]:
    """Return the supplier's class coverage rows. Empty list when TIER1_V2 is off
    or no coverage is set."""
    if _tier1_dormant():
        return []
    sid = _supplier_id_for(domain)
    if not sid:
        return []
    try:
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT class_id, subtype, unspsc, is_core, confidence, source "
                "FROM supplier_classes WHERE supplier_id = ? ORDER BY class_id",
                (sid,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


# --- scope: brands -----------------------------------------------------------

def set_supplier_brands(domain: str, brands: list[dict],
                        *, source: str = SCOPE_SOURCE_MANUAL,
                        set_by: Optional[str] = None) -> bool:
    """Replace a supplier's brand coverage. Each brand dict:
    {brand_id, relationship, authorized_territory?, classes_for_brand?, evidence?,
    confidence?}. `relationship` must be one of BRAND_RELATIONSHIPS. Returns True
    on a write. No-ops (False) when TIER1_V2 is off.
    """
    if _tier1_dormant():
        return False
    sid = _ensure_supplier_row(domain)
    if not sid:
        return False
    now = datetime.utcnow().isoformat()
    try:
        with closing(_get_conn()) as conn:
            conn.execute("DELETE FROM supplier_brands WHERE supplier_id = ?", (sid,))
            for b in brands or []:
                bid = (b.get("brand_id") or "").strip()
                rel = (b.get("relationship") or "").upper().strip()
                if not bid or rel not in BRAND_RELATIONSHIPS:
                    continue  # skip malformed
                cfb = b.get("classes_for_brand")
                cfb_json = json.dumps(cfb) if isinstance(cfb, (list, tuple)) else cfb
                conn.execute(
                    """INSERT OR IGNORE INTO supplier_brands
                       (id, supplier_id, brand_id, relationship, authorized_territory,
                        classes_for_brand_json, evidence, confidence, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), sid, bid, rel, b.get("authorized_territory"),
                     cfb_json, b.get("evidence"), b.get("confidence"), now, now),
                )
            conn.execute(
                "UPDATE suppliers SET scope_source = ?, scope_set_by = ?, scope_set_at = ? "
                "WHERE id = ?",
                (source, set_by, now, sid),
            )
            conn.commit()
        return True
    except Exception as exc:
        print(f"[SupplierRegistry] set_supplier_brands failed: {exc}")
        return False


def get_supplier_brands(domain: str) -> list[dict]:
    """Return the supplier's brand coverage rows (classes_for_brand decoded). Empty
    when TIER1_V2 is off or none set."""
    if _tier1_dormant():
        return []
    sid = _supplier_id_for(domain)
    if not sid:
        return []
    try:
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT brand_id, relationship, authorized_territory, "
                "classes_for_brand_json, evidence, confidence "
                "FROM supplier_brands WHERE supplier_id = ? ORDER BY brand_id",
                (sid,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["classes_for_brand"] = _decode_json_field(d.pop("classes_for_brand_json"), [])
                out.append(d)
            return out
    except Exception:
        return []


# --- scope: territory / verticals / performance ------------------------------

def set_supplier_territory(domain: str, ship_area: dict,
                           local_service: Optional[list[dict]] = None,
                           *, source: str = SCOPE_SOURCE_MANUAL,
                           set_by: Optional[str] = None) -> bool:
    """Set the supplier's ship_area + local_service_area[].

    ship_area is either {"kind": "NATIONWIDE_US"} or {"kind": "STATES",
    "states": ["NY", ...]}. local_service is a list of
    {branch_zip, radius?, services?}. Returns True on a write; no-ops (False)
    when TIER1_V2 is off.
    """
    if _tier1_dormant():
        return False
    sid = _ensure_supplier_row(domain)
    if not sid:
        return False
    if not isinstance(ship_area, dict) or ship_area.get("kind") not in (
        SHIP_AREA_NATIONWIDE_US, "STATES"
    ):
        return False
    now = datetime.utcnow().isoformat()
    try:
        with closing(_get_conn()) as conn:
            conn.execute(
                "UPDATE suppliers SET ship_area_json = ?, scope_source = ?, "
                "scope_set_by = ?, scope_set_at = ? WHERE id = ?",
                (json.dumps(ship_area), source, set_by, now, sid),
            )
            conn.execute(
                "DELETE FROM supplier_local_service WHERE supplier_id = ?", (sid,)
            )
            for ls in local_service or []:
                bz = (ls.get("branch_zip") or "").strip()
                if not bz:
                    continue
                svcs = ls.get("services")
                svcs_json = json.dumps(svcs) if isinstance(svcs, (list, tuple)) else svcs
                conn.execute(
                    """INSERT INTO supplier_local_service
                       (id, supplier_id, branch_zip, radius_miles, services_json,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), sid, bz, ls.get("radius"), svcs_json, now, now),
                )
            conn.commit()
        return True
    except Exception as exc:
        print(f"[SupplierRegistry] set_supplier_territory failed: {exc}")
        return False


def get_supplier_territory(domain: str) -> dict:
    """Return {"ship_area": dict|None, "local_service": list[dict]}. Empty when
    TIER1_V2 is off or none set."""
    if _tier1_dormant():
        return {"ship_area": None, "local_service": []}
    sid = _supplier_id_for(domain)
    if not sid:
        return {"ship_area": None, "local_service": []}
    try:
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            r = conn.execute(
                "SELECT ship_area_json FROM suppliers WHERE id = ?", (sid,)
            ).fetchone()
            ship = _decode_json_field(r[0] if r else None, None)
            rows = conn.execute(
                "SELECT branch_zip, radius_miles, services_json "
                "FROM supplier_local_service WHERE supplier_id = ?", (sid,),
            ).fetchall()
            local = []
            for row in rows:
                d = dict(row)
                d["services"] = _decode_json_field(d.pop("services_json"), [])
                d["radius"] = d.pop("radius_miles")
                local.append(d)
            return {"ship_area": ship, "local_service": local}
    except Exception:
        return {"ship_area": None, "local_service": []}


def set_supplier_verticals(domain: str, verticals: list[str],
                           *, source: str = SCOPE_SOURCE_MANUAL,
                           set_by: Optional[str] = None) -> bool:
    """Set the supplier's industry verticals (a simple list stored as JSON). No-ops
    (False) when TIER1_V2 is off."""
    if _tier1_dormant():
        return False
    sid = _ensure_supplier_row(domain)
    if not sid:
        return False
    now = datetime.utcnow().isoformat()
    try:
        with closing(_get_conn()) as conn:
            conn.execute(
                "UPDATE suppliers SET verticals_json = ?, scope_source = ?, "
                "scope_set_by = ?, scope_set_at = ? WHERE id = ?",
                (json.dumps(list(verticals or [])), source, set_by, now, sid),
            )
            conn.commit()
        return True
    except Exception as exc:
        print(f"[SupplierRegistry] set_supplier_verticals failed: {exc}")
        return False


def get_supplier_verticals(domain: str) -> list[str]:
    """Return the supplier's verticals, or [] when TIER1_V2 is off / none set."""
    if _tier1_dormant():
        return []
    sid = _supplier_id_for(domain)
    if not sid:
        return []
    try:
        with closing(_get_conn()) as conn:
            r = conn.execute(
                "SELECT verticals_json FROM suppliers WHERE id = ?", (sid,)
            ).fetchone()
            return _decode_json_field(r[0] if r else None, [])
    except Exception:
        return []


# --- lifecycle (enforced state machine) --------------------------------------

def get_tier1_lifecycle(domain: str) -> Optional[str]:
    """Return the supplier's tier1 lifecycle status, or None when TIER1_V2 is off
    or no lifecycle is set (a legacy Apollo-cache row)."""
    if _tier1_dormant():
        return None
    rec = lookup_by_domain(domain)
    if not rec:
        return None
    return rec.get("tier1_lifecycle")


def tier1_transition(domain: str, new_status: str,
                     *, set_by: Optional[str] = None) -> Optional[dict]:
    """Drive the tier1 lifecycle forward, ENFORCING the state machine (mirrors
    utils/orders.update_order_status). Rejects illegal transitions (skip-ahead,
    backward, un-suspend) by returning None. A supplier with no current
    tier1_lifecycle may enter at `discovered` or `suspended` only. Returns the
    updated supplier record on success, or None on rejection / flag-off / missing
    supplier. Stamps updated_at.
    """
    if _tier1_dormant():
        return None
    if new_status not in TIER1_LIFECYCLE_STATUSES:
        print(f"[SupplierRegistry] tier1_transition: unknown status {new_status!r}")
        return None
    rec = lookup_by_domain(domain)
    if not rec:
        return None
    current = rec.get("tier1_lifecycle")
    if not _tier1_can_transition(current, new_status):
        print(f"[SupplierRegistry] tier1 illegal transition rejected: "
              f"{current!r} -> {new_status!r}")
        return None
    now = datetime.utcnow().isoformat()
    try:
        with closing(_get_conn()) as conn:
            conn.execute(
                "UPDATE suppliers SET tier1_lifecycle = ?, updated_at = ?, "
                "scope_set_by = COALESCE(?, scope_set_by), scope_set_at = ? "
                "WHERE id = ?",
                (new_status, now, set_by, now, rec["id"]),
            )
            conn.commit()
        return lookup_by_domain(domain)
    except Exception as exc:
        print(f"[SupplierRegistry] tier1_transition failed: {exc}")
        return None


# --- lookup primitives -------------------------------------------------------

def find_suppliers_by_class(class_id: str, *, core_only: bool = False) -> list[dict]:
    """Return supplier records carrying the given class. When core_only, restrict
    to is_core=1 rows (the supplier's core competencies, not incidental coverage).
    Empty when TIER1_V2 is off. Joins supplier_classes -> suppliers on supplier_id.
    """
    if _tier1_dormant():
        return []
    cid = (class_id or "").upper().strip()
    if not cid:
        return []
    sql = (
        "SELECT s.* FROM suppliers s "
        "JOIN supplier_classes c ON c.supplier_id = s.id "
        "WHERE c.class_id = ?"
    )
    params: list = [cid]
    if core_only:
        sql += " AND c.is_core = 1"
    sql += " GROUP BY s.id ORDER BY s.name"
    try:
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def find_suppliers_by_brand(brand_id: str,
                            relationship: Optional[str] = None) -> list[dict]:
    """Return supplier records carrying the given brand, optionally filtered by
    the tri-state relationship (AUTHORIZED | CARRIES | AFTERMARKET_COMPATIBLE).
    None relationship = all. Empty when TIER1_V2 is off."""
    if _tier1_dormant():
        return []
    bid = (brand_id or "").strip()
    if not bid:
        return []
    sql = (
        "SELECT s.*, b.relationship FROM suppliers s "
        "JOIN supplier_brands b ON b.supplier_id = s.id "
        "WHERE b.brand_id = ?"
    )
    params: list = [bid]
    if relationship:
        rel = relationship.upper().strip()
        if rel not in BRAND_RELATIONSHIPS:
            return []
        sql += " AND b.relationship = ?"
        params.append(rel)
    sql += " GROUP BY s.id ORDER BY s.name"
    try:
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


# Territory-fit RANK tiers (higher = broader coverage). NOT a hard exclusion —
# the caller decides; find_suppliers_by_territory returns EVERY supplier with
# scope data, annotated with a rank (except local_service which IS a hard
# inclusion via find_suppliers_with_local_service).
TERRITORY_RANK_NONE = 0       # no ship_area set
TERRITORY_RANK_STATE = 1      # states[] set but does not cover the query state
TERRITORY_RANK_STATE_MATCH = 2  # states[] covers the query state
TERRITORY_RANK_NATIONWIDE = 3   # NATIONWIDE_US covers any state


def find_suppliers_by_territory(state: Optional[str] = None) -> list[dict]:
    """Return suppliers that have ANY scope territory data, annotated with a
    `territory_rank` (NATIONWIDE > state-match > state-no-match > none). This is
    RANK data, NOT a hard exclusion — a non-matching supplier is still returned
    (rank TERRITORY_RANK_STATE / TERRITORY_RANK_NONE) so the caller can decide.
    Empty when TIER1_V2 is off. `state` is a 2-letter US state code (uppercased).
    """
    if _tier1_dormant():
        return []
    st = (state or "").upper().strip()
    try:
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, name, domain, ship_area_json FROM suppliers "
                "WHERE ship_area_json IS NOT NULL AND ship_area_json != '' "
                "ORDER BY name"
            ).fetchall()
            out = []
            for r in rows:
                ship = _decode_json_field(r["ship_area_json"], None)
                if not isinstance(ship, dict):
                    rank = TERRITORY_RANK_NONE
                elif ship.get("kind") == SHIP_AREA_NATIONWIDE_US:
                    rank = TERRITORY_RANK_NATIONWIDE
                elif ship.get("kind") == "STATES" and isinstance(ship.get("states"), list):
                    states = [s.upper().strip() for s in ship["states"]]
                    rank = (TERRITORY_RANK_STATE_MATCH if st and st in states
                            else TERRITORY_RANK_STATE)
                else:
                    rank = TERRITORY_RANK_NONE
                out.append({
                    "id": r["id"], "name": r["name"], "domain": r["domain"],
                    "territory_rank": rank,
                })
            return out
    except Exception:
        return []


def find_suppliers_with_local_service(zip_code: Optional[str] = None) -> list[dict]:
    """Return suppliers that have a local service branch. This IS a hard inclusion
    (the local_service_area exception per the brief): only suppliers with a row
    in supplier_local_service are returned. When `zip_code` is given, the result
    is annotated with the branch_zip/radius (the caller applies the radius test);
    without a zip, all suppliers with any local-service row are returned. Empty
    when TIER1_V2 is off.
    """
    if _tier1_dormant():
        return []
    try:
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT s.id, s.name, s.domain, ls.branch_zip, ls.radius_miles "
                "FROM suppliers s "
                "JOIN supplier_local_service ls ON ls.supplier_id = s.id "
                "ORDER BY s.name"
            ).fetchall()
            out = []
            for r in rows:
                d = {"id": r["id"], "name": r["name"], "domain": r["domain"],
                     "branch_zip": r["branch_zip"], "radius_miles": r["radius_miles"]}
                out.append(d)
            return out
    except Exception:
        return []


# --- full scope read (for diagnostics / the future onboarding UI) ------------

def get_supplier_scope(domain: str) -> dict:
    """Return the full supplier scope: lifecycle, classes, brands, territory,
    verticals, performance, provenance. Empty/null fields when TIER1_V2 is off or
    nothing is set (a legacy Apollo-cache row returns an all-empty scope)."""
    if _tier1_dormant():
        return {"tier1_lifecycle": None, "classes": [], "brands": [],
                "ship_area": None, "local_service": [], "verticals": [],
                "performance": {}, "scope_source": None,
                "scope_set_by": None, "scope_set_at": None}
    rec = lookup_by_domain(domain)
    if not rec:
        return {"tier1_lifecycle": None, "classes": [], "brands": [],
                "ship_area": None, "local_service": [], "verticals": [],
                "performance": {}, "scope_source": None,
                "scope_set_by": None, "scope_set_at": None}
    terr = get_supplier_territory(domain)
    return {
        "tier1_lifecycle": rec.get("tier1_lifecycle"),
        "classes": get_supplier_classes(domain),
        "brands": get_supplier_brands(domain),
        "ship_area": terr["ship_area"],
        "local_service": terr["local_service"],
        "verticals": _decode_json_field(rec.get("verticals_json"), []),
        "performance": _decode_json_field(rec.get("performance_json"), {}),
        "scope_source": rec.get("scope_source"),
        "scope_set_by": rec.get("scope_set_by"),
        "scope_set_at": rec.get("scope_set_at"),
    }


# ---------------------------------------------------------------------------
# Night 5 — supplier notification EVENTS (T3)
# ---------------------------------------------------------------------------
# The notify layer records matched-request→notify events for onboarded Tier 1
# suppliers, behind the research's conservative notify≫display asymmetry. The send
# itself goes through the existing stubbed/flagged EmailSender — NOTHING sends live
# (EMAIL_SEND_ENABLED defaults OFF + the conftest safety net + TIER1_V2 gate). These
# functions are gated by TIER1_V2: when the flag is OFF they no-op (writes return
# None, reads return []), so the notify surface is byte-identical to pre-Night-5 (T5).

def record_supplier_notification(
    run_id: Optional[str],
    supplier_domain: str,
    vendor_name: Optional[str],
    *,
    noun_class: Optional[str] = None,
    notify_reason: Optional[str] = None,
    send_status: str = "stubbed",
    message_id: Optional[str] = None,
    threshold: str = "notify",
    metadata: Optional[dict] = None,
    notified_at: Optional[str] = None,
) -> Optional[str]:
    """Record one matched-request→notify event for an onboarded Tier 1 supplier.

    ``send_status`` mirrors SendResult.status ("stubbed" at the repo/test default —
    the EmailSender gate is OFF, so the notify layer records the event WITHOUT a live
    send). Returns the new row id, or None on flag-off / failure (fail-soft — never
    raises into the sourcing pipeline). No-ops (None) when TIER1_V2 is off."""
    if _tier1_dormant():
        return None
    dom = _normalize_domain(supplier_domain) if supplier_domain else None
    if not dom:
        return None
    now = notified_at or datetime.utcnow().isoformat()
    row = (
        str(uuid.uuid4()), run_id, dom, vendor_name, noun_class, notify_reason,
        now, send_status, message_id, threshold,
        json.dumps(metadata) if metadata is not None else None, now,
    )
    try:
        with closing(_get_conn()) as conn:
            conn.execute(
                """INSERT INTO supplier_notifications
                   (id, run_id, supplier_domain, vendor_name, noun_class, notify_reason,
                    notified_at, send_status, message_id, threshold, metadata_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
            conn.commit()
        print(f"[SupplierRegistry] Notification recorded: {vendor_name} ({dom}) "
              f"reason={notify_reason} status={send_status}")
        return row[0]
    except Exception as exc:
        print(f"[SupplierRegistry] record_supplier_notification failed for {dom!r}: {exc}")
        return None


def get_supplier_notifications(
    run_id: Optional[str] = None, domain: Optional[str] = None,
) -> list[dict]:
    """Return notification event rows (newest first), optionally filtered by run_id
    and/or supplier domain. ``metadata_json`` is decoded into ``metadata``. Empty when
    TIER1_V2 is off or no rows match. Fail-soft: [] on error."""
    if _tier1_dormant():
        return []
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
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM supplier_notifications{where} ORDER BY created_at DESC",
                params,
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["metadata"] = _decode_json_field(d.pop("metadata_json"), None)
                out.append(d)
            return out
    except Exception as exc:
        print(f"[SupplierRegistry] get_supplier_notifications failed: {exc}")
        return []

