"""Run Labels — Night 2 of the Arkim overnight build program.

Append-only label records that turn captured runs (Night 1's `run_capture`
store) into ground-truth eval cases. A labeler (admin) reviews a captured run
and records: was the intake classification correct (+ structured corrections /
ground truth), and per-candidate: the right part type + the floor verdict the
scorer should have reached. Free-text note + labeled_by + ts on every record.

DESIGN (per the Night 2 brief + investigation findings I1–I4):

* **Own store only.** `data/run_labels.sqlite` (raw `sqlite3`, WAL, mirrors the
  `run_capture.py` / `orders.py` / `supplier_registry.py` convention — NOT the
  SQLAlchemy `persistence.py` stack). Labels READ the run_capture + persistence
  stores to render the queue; they never mutate any other store.
* **Flag-gated, default OFF.** `RUN_CAPTURE` env flag (extends Night 1 —
  labeling is part of the same capture flywheel). Strict `_env_truthy` parse
  (mirrors `run_capture` / `EMAIL_SEND_ENABLED`). Flag-off → every public
  function is a no-op → zero writes, byte-identical API (the T6 inertness wall).
  The labeling ENDPOINTS are additionally admin-gated (require_admin) — that
  layer lives in api_server; this module is the pure store.
* **Fail-soft + visible.** Every write is wrapped; a failure increments a
  thread-safe counter surfaced via `/api/health` (`label_failures: N`) ONLY when
  the flag is on (mirrors capture_failures). A label write NEVER raises into the
  request path and NEVER silently dies — it is counted.
* **Append-only.** A `run_labels` row is inserted per POST, never UPDATEd. The
  "current" label for a (run_id, scope, candidate_ref) is the latest by ts
  (mirrors the run_events event-log philosophy). This preserves label history
  (auditable) and lets a labeler revise a label without destroying the prior
  verdict.

Label scopes:
  - ``run``       : a run-level (intake) label. payload = {intake_correct,
                    expected_part_type, expected_component_of, expected_regime,
                    corrections, note}. expected_* are the GROUND TRUTH the
                    intake classifier should reach (so the exporter can emit a
                    drop-in intake eval case); intake_correct is the labeler's
                    yes/no verdict; corrections is free-text.
  - ``candidate`` : a per-candidate (scoring) label. payload = {right_part_type,
                    should_pass_floor, note}. Maps to the scoring eval's
                    expected.should_pass_floor (+ the detection eval's
                    expected_noun_class via right_part_type).

This module is pure + standalone + tested + fail-soft from the start (the house
standard for new modules abutting pre-standard code — CLAUDE.md §5).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Flag (strict _env_truthy — matches run_capture / api_server convention)
# ---------------------------------------------------------------------------

def _env_truthy(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


# Labeling rides the SAME flag as capture (the brief: "All new behavior behind
# flag RUN_CAPTURE (extends Night 1)"). Read once at import; tests monkeypatch.
RUN_CAPTURE: bool = _env_truthy(os.environ.get("RUN_CAPTURE"))


# ---------------------------------------------------------------------------
# Store path (mirrors run_capture.py:68 / orders.py / supplier_registry.py)
# ---------------------------------------------------------------------------

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DB_PATH = os.path.join(_DATA_DIR, "run_labels.sqlite")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS run_labels (
    label_id       TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    scope          TEXT NOT NULL,
    candidate_ref  TEXT,
    label_json     TEXT NOT NULL,
    labeled_by     TEXT,
    ts             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_run_labels_run_id ON run_labels (run_id);
CREATE INDEX IF NOT EXISTS ix_run_labels_run_scope ON run_labels (run_id, scope, candidate_ref);
"""


def _get_conn() -> sqlite3.Connection:
    """Open a connection, ensuring the schema exists. Mirrors run_capture._get_conn."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in _DDL.split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Fail-soft failure counter (thread-safe; surfaced on /api/health when flag on)
# ---------------------------------------------------------------------------

_fail_lock = threading.Lock()
_label_failures: int = 0


def label_failures() -> int:
    """Current label-write failure count (for /api/health)."""
    with _fail_lock:
        return _label_failures


def reset_failures() -> None:
    """Test hook: reset the counter."""
    global _label_failures
    with _fail_lock:
        _label_failures = 0


def _record_failure() -> None:
    global _label_failures
    with _fail_lock:
        _label_failures += 1


# ---------------------------------------------------------------------------
# Public label API — every function is flag-gated + fail-soft (never raises)
# ---------------------------------------------------------------------------

SCOPE_RUN = "run"
SCOPE_CANDIDATE = "candidate"
_SCOPES = {SCOPE_RUN, SCOPE_CANDIDATE}


def write_label(
    run_id: str,
    scope: str,
    payload: Dict[str, Any],
    *,
    candidate_ref: Optional[str] = None,
    labeled_by: Optional[str] = None,
) -> None:
    """Append one label row. No-op when flag off. Never raises.

    ``scope`` ∈ {"run","candidate"}. ``candidate_ref`` is required for the
    candidate scope (ignored for run scope). The latest row by ts for a given
    (run_id, scope, candidate_ref) is the current label.
    """
    if not RUN_CAPTURE:
        return
    if scope not in _SCOPES:
        _record_failure()
        return
    if scope == SCOPE_CANDIDATE and not candidate_ref:
        _record_failure()
        return
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO run_labels "
                "(label_id, run_id, scope, candidate_ref, label_json, labeled_by, ts) "
                "VALUES (:label_id, :run_id, :scope, :candidate_ref, :label_json, "
                "        :labeled_by, :ts)",
                {
                    "label_id": str(uuid.uuid4()),
                    "run_id": run_id,
                    "scope": scope,
                    "candidate_ref": candidate_ref,
                    "label_json": json.dumps(payload, default=str),
                    "labeled_by": labeled_by,
                    "ts": datetime.now(timezone.utc).isoformat(),
                },
            )
            conn.commit()
    except Exception:
        # Fail-soft: count and swallow. Never break a request, never silently die.
        _record_failure()


# ---------------------------------------------------------------------------
# Read helpers (for tests, the labeling UI queue, and the exporter)
# ---------------------------------------------------------------------------

def _row_to_dict(r: tuple) -> Dict[str, Any]:
    return {
        "label_id": r[0],
        "run_id": r[1],
        "scope": r[2],
        "candidate_ref": r[3],
        "label": json.loads(r[4]) if r[4] else {},
        "labeled_by": r[5],
        "ts": r[6],
    }


def read_labels(run_id: str) -> List[Dict[str, Any]]:
    """Read all label rows for a run, oldest-first (label history)."""
    if not RUN_CAPTURE:
        return []
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT label_id, run_id, scope, candidate_ref, label_json, labeled_by, ts "
                "FROM run_labels WHERE run_id = ? ORDER BY ts ASC",
                (run_id,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        _record_failure()
        return []


def current_label(
    run_id: str, scope: str, candidate_ref: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """The latest label row for a (run_id, scope, candidate_ref), or None."""
    if not RUN_CAPTURE:
        return None
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT label_id, run_id, scope, candidate_ref, label_json, labeled_by, ts "
                "FROM run_labels WHERE run_id = ? AND scope = ? AND candidate_ref IS ? "
                "ORDER BY ts DESC LIMIT 1",
                (run_id, scope, candidate_ref),
            ).fetchone()
        return _row_to_dict(row) if row else None
    except Exception:
        _record_failure()
        return None


def labeled_run_ids() -> List[str]:
    """Run ids that have at least one label (for the exporter + provenance)."""
    if not RUN_CAPTURE:
        return []
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT run_id FROM run_labels ORDER BY run_id ASC"
            ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        _record_failure()
        return []


def read_all_labels() -> List[Dict[str, Any]]:
    """Read every label row (morning inspection helper)."""
    if not RUN_CAPTURE:
        return []
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT label_id, run_id, scope, candidate_ref, label_json, labeled_by, ts "
                "FROM run_labels ORDER BY ts ASC"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        _record_failure()
        return []
