"""Run Capture — Night 1 of the Arkim overnight build program.

Every procurement run produces a complete, queryable capture record (inputs,
queries, candidates, scores, verdicts, user actions) in a first-party store,
fail-soft but alert-visible. This is the flywheel's raw material.

DESIGN (per the Night 1 brief + investigation findings I1–I4):

* **Own store only.** `data/run_capture.sqlite` (raw `sqlite3`, WAL, mirrors the
  `orders.py` / `supplier_registry.py` / `audit_log.py` convention — NOT the
  SQLAlchemy `persistence.py` stack). Capture READS seams across the codebase;
  it never mutates any other store (`price_db`, `known_parts`,
  `supplier_registry`, `orders`, `sent_messages`, `review_items` are untouched).
* **Flag-gated, default OFF.** `RUN_CAPTURE` env flag, strict `_env_truthy`
  parse (`1/true/yes/on`), read once at import (mirrors `EMAIL_SEND_ENABLED` /
  `DEMO_MODE` / `SCORING_V2`). Flag-off → every public capture function is a
  no-op → zero writes, zero new codepath effects, byte-identical API responses
  (the T5 inertness wall). Tests monkeypatch the module attr per-test.
* **Fail-soft + visible.** Every write is wrapped; a failure increments a
  thread-safe counter surfaced via `/api/health` (`capture_failures: N`) ONLY
  when the flag is on. A capture failure NEVER raises into the request path and
  NEVER silently dies — it is counted.
* **Append-only event log.** `run_events` rows are the queryable flywheel
  surface; they intentionally DUPLICATE per-candidate data that also lives in
  `sourcing_runs.sourcing_results_json` (I1) — the event log is the
  queryable/temporal view, the run blob is the state view. User/agent TURNS are
  NOT durably persisted anywhere today (in-memory `_messages`, api_server:322) —
  capture is the first durable home for them (I1 gap).
* **PII (I3).** No redaction pipeline exists today; "post-redaction" therefore
  means "the text as the intake path sees it" (as-is). Fidelity cost = none
  (no redaction transform to lose). Capture is a NEW durable PII surface;
  flag-gating (default OFF) keeps the public demo unaffected. A real
  redaction pipeline + consent gate is a flagged supervised follow-up.

This module is pure + standalone + tested + fail-soft from the start (the house
standard for new modules abutting pre-standard code — CLAUDE.md §5). The api_server
call sites are thin one-liners that no-op when the flag is off.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional


# ---------------------------------------------------------------------------
# Flag (strict _env_truthy — matches api_server/email_sender/scoring convention)
# ---------------------------------------------------------------------------

def _env_truthy(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


RUN_CAPTURE: bool = _env_truthy(os.environ.get("RUN_CAPTURE"))


# ---------------------------------------------------------------------------
# Store path (mirrors orders.py:39 / supplier_registry.py:72 / audit_log.py:65)
# ---------------------------------------------------------------------------

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DB_PATH = os.path.join(_DATA_DIR, "run_capture.sqlite")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS run_events (
    event_id     TEXT PRIMARY KEY,
    run_id       TEXT,
    ts           TEXT NOT NULL,
    source_tag   TEXT,
    event_type   TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_run_events_run_id ON run_events (run_id);
CREATE INDEX IF NOT EXISTS ix_run_events_run_id_type ON run_events (run_id, event_type);
CREATE INDEX IF NOT EXISTS ix_run_events_event_type ON run_events (event_type);

CREATE TABLE IF NOT EXISTS run_outcomes (
    run_id       TEXT PRIMARY KEY,
    outcome      TEXT NOT NULL,
    details_json TEXT,
    computed_at  TEXT NOT NULL
);
"""


def _get_conn() -> sqlite3.Connection:
    """Open a connection, ensuring the schema exists. Mirrors orders.py:_get_conn."""
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
_capture_failures: int = 0


def capture_failures() -> int:
    """Current capture-write failure count (for /api/health)."""
    with _fail_lock:
        return _capture_failures


def reset_failures() -> None:
    """Test hook: reset the counter."""
    global _capture_failures
    with _fail_lock:
        _capture_failures = 0


def _record_failure() -> None:
    global _capture_failures
    with _fail_lock:
        _capture_failures += 1


# ---------------------------------------------------------------------------
# Source-tag derivation
# ---------------------------------------------------------------------------

def _source_tag() -> str:
    """Derive the capture source_tag for this process.

    `demo_prospect` under DEMO_MODE (the public no-login demo), else
    `internal_test`. Customer/tenant tagging (`customer:<tenant>`) awaits tenant
    identity infra (Arc 1) — flagged in the morning report as a placeholder.
    """
    try:
        from api_server import DEMO_MODE  # late import — api_server imports us
        if DEMO_MODE:
            return "demo_prospect"
    except Exception:
        pass
    return "internal_test"


# ---------------------------------------------------------------------------
# Public capture API — every function is flag-gated + fail-soft (never raises)
# ---------------------------------------------------------------------------

# Event type vocabulary (the brief's T1 schema):
TURN_USER = "turn_user"
TURN_AGENT = "turn_agent"
INTAKE_RESULT = "intake_result"
QUERY_ISSUED = "query_issued"
CANDIDATE_SCORED = "candidate_scored"
CANDIDATE_REJECTED = "candidate_rejected"
RESULTS_DISPLAYED = "results_displayed"
USER_ACTION = "user_action"
OUTCOME = "outcome"


def _write(event_type: str, run_id: str, payload: Dict[str, Any]) -> None:
    """Append one event row. No-op when flag off. Never raises."""
    if not RUN_CAPTURE:
        return
    try:
        event = {
            "event_id": str(uuid.uuid4()),
            "run_id": run_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "source_tag": _source_tag(),
            "event_type": event_type,
            "payload_json": json.dumps(payload, default=str),
        }
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO run_events "
                "(event_id, run_id, ts, source_tag, event_type, payload_json) "
                "VALUES (:event_id, :run_id, :ts, :source_tag, :event_type, :payload_json)",
                event,
            )
            conn.commit()
    except Exception:
        # Fail-soft: count and swallow. Never break a run, never silently die.
        _record_failure()


def capture_turn(run_id: str, role: str, content: str, *, extra: Optional[Dict[str, Any]] = None) -> None:
    """Capture a user or agent turn (role ∈ {'user','agent'}). I2(a) seam.

    The user/agent turn is NOT durably persisted anywhere today (in-memory
    `_messages`, cleared on restart — I1). This is the first durable home.
    Stores the text as-is (no redaction pipeline exists — I3 default decision).
    """
    payload: Dict[str, Any] = {"role": role, "content": content}
    if extra:
        payload.update(extra)
    _write(TURN_USER if role == "user" else TURN_AGENT, run_id, payload)


def capture_intake_result(
    run_id: str,
    *,
    sufficient: bool,
    proceed_state: Optional[str],
    manufacturer_confidence: Optional[float],
    part_id_confidence: Optional[float],
    asset_specs: Optional[Dict[str, Any]] = None,
    follow_up_question: Optional[str] = None,
) -> None:
    """Capture the intake agent's per-turn result (I2(a) intake_result seam)."""
    _write(
        INTAKE_RESULT,
        run_id,
        {
            "sufficient": sufficient,
            "proceed_state": proceed_state,
            "manufacturer_confidence": manufacturer_confidence,
            "part_id_confidence": part_id_confidence,
            "asset_specs": asset_specs or {},
            "follow_up_question": follow_up_question,
        },
    )


def capture_query(run_id: str, tier: int, query_intent: str, *, part_key: Optional[str] = None) -> None:
    """Capture a per-tier sourcing query INTENT (I2(b) seam).

    The literal provider query string (Tavily/Parallel) is built deep inside
    `sourcing_archieved/enterprise_search.py` where NO run_id is in scope (I2
    EXPECTED, confirmed) — threading run_id there is load-bearing and out of
    scope tonight. We capture the query INTENT derived from specs (the useful
    flywheel signal: what was searched for). The literal provider query string
    is a flagged not-captured gap.
    """
    _write(QUERY_ISSUED, run_id, {"tier": tier, "query_intent": query_intent, "part_key": part_key})


def capture_candidate(run_id: str, tier: int, candidate: Dict[str, Any]) -> None:
    """Capture one sourcing candidate with score + gate verdict + rejection reason.

    Routed (scored vs rejected) by presence of `rejection_reason`. I2(b)/(c)
    seam — captured from the `result` dict at `_run_sourcing_background`
    (api_server ~1107-1182) where run_id IS in scope; the deep `[Sourcing]`
    print sites in enterprise_search.py have no run_id and are NOT used.
    """
    if candidate.get("rejection_reason"):
        _write(
            CANDIDATE_REJECTED,
            run_id,
            {
                "tier": tier,
                "vendor_name": candidate.get("vendor_name"),
                "candidate_id": candidate.get("candidate_id"),
                "suitability_score": candidate.get("suitability_score"),
                "rejection_reason": candidate.get("rejection_reason"),
                "confidence_score": candidate.get("confidence_score"),
            },
        )
    else:
        _write(
            CANDIDATE_SCORED,
            run_id,
            {
                "tier": tier,
                "vendor_name": candidate.get("vendor_name"),
                "candidate_id": candidate.get("candidate_id"),
                "suitability_score": candidate.get("suitability_score"),
                "pn_match_status": candidate.get("pn_match_status"),
                "match_type": candidate.get("match_type"),
                "match_basis": candidate.get("match_basis"),
                "confidence_score": candidate.get("confidence_score"),
                "base_price": candidate.get("base_price"),
                "price_tbd": candidate.get("price_tbd"),
                "lead_time_days": candidate.get("lead_time_days"),
                "vendor_authorization_status": candidate.get("vendor_authorization_status"),
                "onboarding_status": candidate.get("onboarding_status"),
            },
        )


def capture_results_displayed(run_id: str, displayed: Iterable[Dict[str, Any]]) -> None:
    """Capture the displayed candidate set (I2(c) — post-floor, post-reject)."""
    _write(
        RESULTS_DISPLAYED,
        run_id,
        {
            "displayed": [
                {
                    "tier": c.get("tier"),
                    "vendor_name": c.get("vendor_name"),
                    "candidate_id": c.get("candidate_id"),
                    "suitability_score": c.get("suitability_score"),
                }
                for c in displayed
            ]
        },
    )


def capture_user_action(run_id: str, action: str, *, detail: Optional[Dict[str, Any]] = None) -> None:
    """Capture a frontend→backend user action (I2(d) seam). action ∈
    {select_candidate, order_now, approve, reject, confirm_intake, outreach,
    save_outreach, rfq_draft, mark_delivered, ...}."""
    _write(USER_ACTION, run_id, {"action": action, "detail": detail or {}})


# ---------------------------------------------------------------------------
# Outcome computation (T3) — implicit signals, computed on read
# ---------------------------------------------------------------------------

OUTCOME_COMPLETED_WITH_ACTION = "completed_with_action"
OUTCOME_ABANDONED_AFTER_RESULTS = "abandoned_after_results"
OUTCOME_ZERO_RESULTS = "zero_results"
OUTCOME_ALL_REJECTED = "all_rejected"
OUTCOME_REPHRASED = "rephrased"
OUTCOME_INCOMPLETE = "incomplete"  # not enough signal yet

# User actions that act on sourcing RESULTS (a completion signal), vs
# `confirm_intake` which is a phase-transition action (pre-results — it advances
# the run to sourcing, it is NOT "acted on a result"). The outcome classifier
# only treats result-actions as `completed_with_action`.
_COMPLETION_ACTIONS = {
    "select_candidate", "order_now", "approve", "reject",
    "outreach", "save_outreach", "rfq_draft", "mark_delivered",
}


def _events_for(run_id: str) -> list[Dict[str, Any]]:
    """Read all events for a run, oldest-first (helper for outcome computation)."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT event_type, payload_json FROM run_events "
            "WHERE run_id = ? ORDER BY ts ASC, rowid ASC",
            (run_id,),
        ).fetchall()
    return [{"event_type": r[0], "payload": json.loads(r[1])} for r in rows]


def compute_outcome(run_id: str) -> str:
    """Classify a run's outcome from its captured events (T3).

    Deterministic signals:
      - completed_with_action : a `user_action` event exists (select/order/
        approve/outreach/...).
      - zero_results          : `results_displayed` with an empty displayed set.
      - all_rejected          : only `candidate_rejected` events, no scored.
      - abandoned_after_results : `results_displayed` non-empty but no user_action.
      - incomplete            : not enough events (no results_displayed yet).

    `rephrased` (same session new run with similar input) needs cross-run
    similarity — implemented as a separate helper `detect_rephrase` (best-effort
    heuristic, flagged) rather than forced into this single-run classifier.
    """
    events = _events_for(run_id)
    if not events:
        return OUTCOME_INCOMPLETE

    types = [e["event_type"] for e in events]
    has_displayed = RESULTS_DISPLAYED in types
    has_scored = CANDIDATE_SCORED in types
    has_rejected = CANDIDATE_REJECTED in types
    # A "completion" is a user action that acts on sourcing RESULTS (select /
    # order / approve / reject / outreach / ...). `confirm_intake` is a
    # phase-transition action (pre-results — it advances the run to sourcing),
    # NOT a completion, so it does not by itself mark a run completed.
    has_completion_action = any(
        e["event_type"] == USER_ACTION and e["payload"].get("action") in _COMPLETION_ACTIONS
        for e in events
    )

    if has_completion_action:
        return OUTCOME_COMPLETED_WITH_ACTION

    if has_displayed:
        displayed_event = next(e for e in events if e["event_type"] == RESULTS_DISPLAYED)
        displayed_list = displayed_event["payload"].get("displayed") or []
        if not displayed_list:
            return OUTCOME_ZERO_RESULTS
        return OUTCOME_ABANDONED_AFTER_RESULTS

    if has_rejected and not has_scored:
        return OUTCOME_ALL_REJECTED

    return OUTCOME_INCOMPLETE


def detect_rephrase(run_id: str, all_run_specs: Dict[str, Dict[str, Any]]) -> bool:
    """Best-effort `rephrased` signal (T3): did the same session start a NEW run
    with similar input after this run was abandoned? `all_run_specs` is a
    {run_id: asset_specs} map for the same session/source_tag. Heuristic —
    flagged in the report; not asserted as deterministic."""
    return False  # placeholder: cross-run similarity is a flagged follow-up


def write_outcome(run_id: str, outcome: str, details: Optional[Dict[str, Any]] = None) -> None:
    """Materialize a computed outcome into run_outcomes (post-run or on read)."""
    if not RUN_CAPTURE:
        return
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO run_outcomes (run_id, outcome, details_json, computed_at) "
                "VALUES (:run_id, :outcome, :details, :ts) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "outcome=excluded.outcome, details_json=excluded.details_json, "
                "computed_at=excluded.computed_at",
                {
                    "run_id": run_id,
                    "outcome": outcome,
                    "details": json.dumps(details or {}, default=str),
                    "ts": datetime.now(timezone.utc).isoformat(),
                },
            )
            conn.commit()
    except Exception:
        _record_failure()


# ---------------------------------------------------------------------------
# Read helpers (for tests + the morning read snippet)
# ---------------------------------------------------------------------------

def read_events(run_id: str) -> list[Dict[str, Any]]:
    """Read all events for a run, oldest-first."""
    return _events_for(run_id)


def read_all_events() -> list[Dict[str, Any]]:
    """Read every event (morning inspection helper)."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT event_id, run_id, ts, source_tag, event_type, payload_json "
            "FROM run_events ORDER BY ts ASC, rowid ASC"
        ).fetchall()
    return [
        {
            "event_id": r[0],
            "run_id": r[1],
            "ts": r[2],
            "source_tag": r[3],
            "event_type": r[4],
            "payload": json.loads(r[5]),
        }
        for r in rows
    ]


def read_outcome(run_id: str) -> Optional[Dict[str, Any]]:
    """Read a materialized outcome row if present."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT run_id, outcome, details_json, computed_at "
            "FROM run_outcomes WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "run_id": row[0],
        "outcome": row[1],
        "details": json.loads(row[2]) if row[2] else {},
        "computed_at": row[3],
    }
