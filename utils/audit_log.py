"""
Arkim Audit Log — SQLite-backed, Postgres-compatible schema.

Implemented with Python's built-in sqlite3 for zero-dependency prototype deployment.
The table schema maps 1-to-1 to a SQLAlchemy ORM model — when moving to Postgres
post-seed, wrap each function body in the equivalent SQLAlchemy session call.

Table: audit_log
  id                     UUID string   (primary key)
  sourcing_run_id        UUID string   (matches ArkimQuote.sourcing_run_id)
  created_at             ISO 8601      (UTC)
  agent_version          text
  user_id                text          (nullable)
  asset_specs_json       JSON blob     (full AssetSpecs fields)
  input_summary          text          (e.g. "Bearing for Bellatrx conveyor SN 12345")
  vendors_considered     JSON array    (all vendors evaluated, with rejection reasons)
  vendors_surfaced       JSON array    (vendors that made it to the UI)
  final_recommendation   text          (vendor_name of top result)
  user_selection         text          (nullable — populated on Accept Offer)
  urgency_factor_used    float
  warranty_status_used   text          (nullable)
  workflow_mode          text
  llm_calls_made         int
  estimated_llm_cost_usd float
  duration_ms            int
  error_log              JSON array
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime
from typing import Optional

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DB_PATH  = os.path.join(_DATA_DIR, "audit_log.sqlite")

_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id                     TEXT PRIMARY KEY,
    sourcing_run_id        TEXT,
    created_at             TEXT NOT NULL,
    agent_version          TEXT,
    user_id                TEXT,
    asset_specs_json       TEXT,
    input_summary          TEXT,
    vendors_considered     TEXT,
    vendors_surfaced       TEXT,
    final_recommendation   TEXT,
    user_selection         TEXT,
    urgency_factor_used    REAL    DEFAULT 0.3,
    warranty_status_used   TEXT,
    workflow_mode          TEXT,
    llm_calls_made         INTEGER DEFAULT 0,
    estimated_llm_cost_usd REAL    DEFAULT 0.0,
    duration_ms            INTEGER,
    error_log              TEXT
);
"""


def _get_conn() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(_DDL)
    conn.commit()
    return conn


def _j(value) -> Optional[str]:
    """Serialize to JSON string, or None if value is None."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        return str(value)


def write_audit_log(run_data: dict) -> str:
    """Persist one sourcing run row. Returns the new entry id.

    Expected keys in run_data (all optional except noted):
      sourcing_run_id       str  (required for correlation)
      agent_version         str
      user_id               str
      asset_specs_json      dict
      input_summary         str
      vendors_considered    list[dict]   — all vendors including rejected
      vendors_surfaced      list[dict]   — vendors shown in UI
      final_recommendation  str          — vendor_name of best result
      user_selection        str          — populated when user accepts
      urgency_factor_used   float
      warranty_status_used  str
      workflow_mode         str
      llm_calls_made        int
      estimated_llm_cost_usd float
      duration_ms           int
      error_log             list[str]
    """
    entry_id = str(uuid.uuid4())
    now      = datetime.utcnow().isoformat()

    row = (
        entry_id,
        run_data.get("sourcing_run_id"),
        now,
        run_data.get("agent_version", "1.0.0-phase2"),
        run_data.get("user_id"),
        _j(run_data.get("asset_specs_json")),
        run_data.get("input_summary"),
        _j(run_data.get("vendors_considered")),
        _j(run_data.get("vendors_surfaced")),
        run_data.get("final_recommendation"),
        run_data.get("user_selection"),
        float(run_data.get("urgency_factor_used") or 0.3),
        run_data.get("warranty_status_used"),
        run_data.get("workflow_mode"),
        int(run_data.get("llm_calls_made") or 0),
        float(run_data.get("estimated_llm_cost_usd") or 0.0),
        run_data.get("duration_ms"),
        _j(run_data.get("error_log") or []),
    )

    try:
        with _get_conn() as conn:
            conn.execute(
                """INSERT INTO audit_log VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
        print(f"[AuditLog] Written: {entry_id} (run={run_data.get('sourcing_run_id', '?')})")
    except Exception as exc:
        print(f"[AuditLog] Write failed: {exc}")

    return entry_id


def recent_entries(limit: int = 20) -> list[dict]:
    """Return the N most recent audit log rows as dicts."""
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        print(f"[AuditLog] Read failed: {exc}")
        return []


def get_entry(sourcing_run_id: str) -> Optional[dict]:
    """Look up a specific run by sourcing_run_id."""
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM audit_log WHERE sourcing_run_id = ? LIMIT 1",
            (sourcing_run_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None
