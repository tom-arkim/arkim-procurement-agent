"""
Arkim Sourcing Engine — FastAPI server.

Exposes the existing SQLAlchemy-backed sourcing pipeline as REST endpoints
consumed by the React frontend (the shipping front end). SQLite DB via WAL mode.

Start with:
    uvicorn api_server:app --reload --port 8001
"""

import json
import os
import re
import threading
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
load_dotenv()


def _env_truthy(value: Optional[str]) -> bool:
    """Strict opt-in parse: only an explicit truthy token is True. Anything else
    (None, "", "0", "false", "no", junk) -> False, so a flag fails safe/closed."""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# SUPPLIER_PORTAL_V1 — the supplier claim-portal feature flag (Night 6).
# ---------------------------------------------------------------------------
# The app's FIRST PUBLIC ROUTE gets its OWN independent kill switch — it does
# NOT extend TIER1_V2 (the brief's REVIEW-ADD: the first public route gets its
# own kill switch). The portal MAY DEPEND on TIER1_V2 data (the profile scope
# + the supplier_notifications demand ledger), but the ROUTE'S EXISTENCE gates
# on SUPPLIER_PORTAL_V1 alone. Flag unset/absent/falsy -> the public route
# DOES NOT EXIST (response byte-identical to any unknown route — FastAPI 404
# {"detail":"Not Found"}), proven by an inertness test. Read live from the env
# so a test that sets it via monkeypatch.setenv is honored (mirrors TIER1_V2).
SUPPLIER_PORTAL_V1: bool = _env_truthy(os.environ.get("SUPPLIER_PORTAL_V1"))


def _portal_enabled() -> bool:
    """Live check for the portal route gate (honors monkeypatched os.environ).
    Used at every portal handler entry; flag-off -> 404 (the route is absent)."""
    return _env_truthy(os.environ.get("SUPPLIER_PORTAL_V1"))


# ---------------------------------------------------------------------------
# INTAKE_CHANNELS_V1 — the channel-agnostic intake surface flag (Night 8).
# ---------------------------------------------------------------------------
# Independent kill switch for the multi-channel intake endpoints (email adapter
# + SMS/voice contract-stubs + the unknown-sender confirm step). Flag off ⇒
# the intake routes DO NOT EXIST (byte-identical 404 {"detail":"Not Found"},
# matching FastAPI's unknown-route body — proven by an inertness test), and the
# intake_channels store/decision functions no-op (defense-in-depth). Read live
# so a monkeypatched os.environ is honored (mirrors SUPPLIER_PORTAL_V1).
def _intake_enabled() -> bool:
    """Live check for the intake route gate (honors monkeypatched os.environ)."""
    return _env_truthy(os.environ.get("INTAKE_CHANNELS_V1"))


# ---------------------------------------------------------------------------
# QUOTE_SUBMIT_V1 — the supplier structured-quote submission flag (Night 11).
# ---------------------------------------------------------------------------
# Independent kill switch for the quote-submission surface (the public
# /api/quote/{token} form endpoints, the portal path-B submit, the admin
# path-C entry + review queue, and the structured-quote merge in
# _build_quote_index). Flag off ⇒ the quote routes DO NOT EXIST
# (byte-identical 404 {"detail":"Not Found"}) and the index merge is skipped —
# behavior byte-identical to pre-Night-11 (parity-tested). Read live so a
# monkeypatched os.environ is honored (mirrors SUPPLIER_PORTAL_V1).
def _quote_submit_enabled() -> bool:
    """Live check for the quote-submission route gate (honors monkeypatched
    os.environ)."""
    return _env_truthy(os.environ.get("QUOTE_SUBMIT_V1"))


# ---------------------------------------------------------------------------
# DEMO_MODE — public no-login demo spine (procurement-dev.arkim.ai cold outreach)
# ---------------------------------------------------------------------------
# Guards active ONLY when env DEMO_MODE is truthy, all completely inert otherwise
# (every route behaves exactly as today — no regression in normal ops):
#   1. An allowlist middleware (added below, after `app`) that DENIES-by-default:
#      only the confirmed demo routes reach their handler; everything else 403s,
#      including /docs, /openapi.json, mutation/admin/RFQ/email routes, and any
#      route not on the list (a new route added later is DENIED until listed —
#      fail-closed, never open). See _DEMO_ALLOWLIST + _demo_path_allowed.
#   2. A startup boot-refusal assertion (here, at import time) that the email
#      send gate is OFF under DEMO_MODE — a public demo must not be able to boot
#      with outbound email enabled. EMAIL_SEND_ENABLED is the canonical send gate
#      (utils/email_sender.py); this refuses the boot before any request is served.
#   3. A startup boot-refusal assertion that APOLLO_API_KEY is unset under DEMO_MODE
#      — a public demo must not spend Apollo credits (org_enrich, ~1/match). The
#      SourcingAgent reads APOLLO_API_KEY from os.environ directly (apollo_client.py)
#      regardless of what api_server passes to its constructor, so "leave it unset"
#      is not a strong enough guard — refuse to boot if it is present. See FIX 2(b)
#      for the belt: the construction path forces Apollo off too.
DEMO_MODE: bool = _env_truthy(os.environ.get("DEMO_MODE"))

if DEMO_MODE:
    # Boot-refusal guards (run at uvicorn boot / module import, before any request).
    # Each refuses to start if a capability that a public no-login demo must not
    # exercise is left enabled in the env — fail loud at boot rather than silently
    # spend/egress. Inert when DEMO_MODE is off (normal dev/prod operation unchanged).
    from utils import email_sender as _email_sender_for_assertion
    if _email_sender_for_assertion.EMAIL_SEND_ENABLED:
        raise RuntimeError(
            "Refusing to start: email send must be disabled in DEMO_MODE "
            "(EMAIL_SEND_ENABLED is true). Unset it before launching the demo."
        )
    if os.environ.get("APOLLO_API_KEY"):
        raise RuntimeError(
            "Refusing to start: APOLLO_API_KEY must be unset in DEMO_MODE "
            "(Apollo credits would be spent on every sourcing run). "
            "Remove it from the demo environment before launching."
        )

from utils.procurement_agent.agents.intake_agent import IntakeAgent
from utils.models import SourcingRun
from utils.marketplace_registry import is_marketplace
from utils import run_capture as _run_capture  # Night 1 — RUN_CAPTURE flag-gated, inert when off

import secrets

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Header, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Auth (ported Cognito identity). get_caller is an OPTIONAL dependency on the customer
# endpoints: it returns None when no token is presented (the current no-auth demo path)
# and a verified Caller when one is. M1 (distinct-approver) enforces ONLY on a verified
# identity — never on a body-supplied name — so it is real when identity is present and
# inert (today's behaviour) when it is not.
from utils.auth import Caller, get_caller

# Import the existing persistence layer
from utils.procurement_agent.state.persistence import (
    SourcingRunORM,
    RequestGroupApprovalORM,
    _SessionFactory,
    Base,
    _engine,
)
from sqlalchemy import text
from utils.procurement_agent.state.phases import Phase
from utils.procurement_agent import tier1_matcher, tier1_notify
from utils import claim_tokens  # Night 6 — supplier claim-portal token store (T1)
from utils import intake_channels  # Night 8 — channel-agnostic intake spine

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Arkim Sourcing Engine API",
    version="1.0.0",
    description="REST API for the Arkim production frontend.",
)

# CORS allowed origins. Localhost is the dev base (kept in every case so normal
# dev is unaffected); CORS_ALLOW_ORIGINS is a comma-separated escape hatch; under
# DEMO_MODE the cross-origin demo frontend origin is appended (DEMO_FRONTEND_ORIGIN,
# default the procurement-dev subdomain) so the browser preflight accepts it.
# Deduped so an env value that repeats a base origin doesn't list it twice.
_cors_origins: list[str] = []
for _o in [
    "http://localhost:3000",    # Next.js dev server
    "http://127.0.0.1:3000",
    "http://localhost:8000",    # same-origin health checks
]:
    if _o not in _cors_origins:
        _cors_origins.append(_o)
_extra_cors = os.environ.get("CORS_ALLOW_ORIGINS")
if _extra_cors:
    for _o in _extra_cors.split(","):
        _o = _o.strip()
        if _o and _o not in _cors_origins:
            _cors_origins.append(_o)
if DEMO_MODE:
    _demo_origin = os.environ.get("DEMO_FRONTEND_ORIGIN", "https://procurement-dev.arkim.ai")
    if _demo_origin not in _cors_origins:
        _cors_origins.append(_demo_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# DEMO_MODE allowlist middleware — deny-by-default / fail-closed
# ---------------------------------------------------------------------------
# Active ONLY when DEMO_MODE is truthy (read above from the env at import). When
# off, the middleware short-circuits and calls the app unchanged — byte-for-byte
# the same behaviour as today (no regression). When on, it permits ONLY the
# confirmed demo routes and 403s everything else, including routes FastAPI serves
# automatically (/docs, /openapi.json) and any route not on the list.
#
# This is an ALLOWLIST (permit-list), not a blocklist: a route is reachable iff it
# is explicitly listed. Anything missed — a new route, a typo, an auto-route — is
# DENIED (403). That is the safety property: a missed route fails closed.
#
# Path matching is exact-segment against parameterised PATTERNS, not prefix
# matching. /api/runs/{run_id} (GET) admits /api/runs/123 but NOT
# /api/runs/123/execute — the latter has an extra trailing segment and falls
# through to 403. So an allowed GET on the run cannot be tricked into admitting a
# denied mutation on the same prefix.

# The confirmed demo surface (verified against the frontend proc/ intake→sourcing
# →results flow): describe a part → get it identified → see live sourcing results
# → manage the session. Read-only or run/session-scoped only; nothing that mutates
# shared/real state, approves, orders, sends, or configures.
_DEMO_ALLOWLIST: set[tuple[str, str]] = {
    ("POST", "/api/runs"),
    ("GET", "/api/runs/{run_id}"),
    ("POST", "/api/runs/{run_id}/messages"),
    ("POST", "/api/runs/{run_id}/upload"),
    ("POST", "/api/runs/{run_id}/confirm-intake"),
    ("GET", "/api/facilities"),
    ("GET", "/api/health"),
    ("GET", "/api/groups/{group_id}"),
}


def _demo_path_allowed(method: str, path: str) -> bool:
    """True iff (METHOD, path) matches a pattern in _DEMO_ALLOWLIST.

    Match is exact-segment: a pattern segment is either a literal (matched
    exactly) or a {param} placeholder (matches any single non-empty segment,
    i.e. no '/'). The full path must be consumed — extra trailing segments on an
    allowed prefix are NOT admitted (so /api/runs/{id} does not cover
    /api/runs/{id}/execute). Method is case-sensitive uppercase."""
    m = method.upper()
    pat_segs: list[list[str]] = []
    for pm, pp in _DEMO_ALLOWLIST:
        if pm != m:
            continue
        pat_segs.append(pp.split("/"))
    if not pat_segs:
        return False
    req_segs = path.split("/")
    for pat in pat_segs:
        if len(req_segs) != len(pat):
            continue
        if all(
            ps == rs or (ps.startswith("{") and ps.endswith("}") and rs != "")
            for ps, rs in zip(pat, req_segs)
        ):
            return True
    return False


@app.middleware("http")
async def demo_allowlist_middleware(request: Request, call_next):
    """Permit ONLY the confirmed demo routes under DEMO_MODE; 403 everything else.

    Inert when DEMO_MODE is off — the app is called unchanged (no regression)."""
    if not DEMO_MODE:
        return await call_next(request)
    # CORS preflight: an OPTIONS request carries no credentials/body and never
    # reaches application logic — no route in this app handles OPTIONS (all routes
    # are GET/POST/PUT), so an OPTIONS only ever gets answered by CORSMiddleware
    # (preflight -> Allow-* headers) or 405s. Letting it pass to CORSMiddleware (the
    # next layer inward) is the ONLY way a cross-origin demo's browser preflight can
    # succeed, and it opens no hole: a real attack uses the real method (POST
    # /execute, etc.), which the allowlist below still 403s. Narrowest possible
    # carve-out — exactly one method, the preflight method.
    if request.method == "OPTIONS":
        return await call_next(request)
    if _demo_path_allowed(request.method, request.url.path):
        return await call_next(request)
    return Response(
        content='{"detail":"Forbidden: route not on the demo allowlist"}',
        status_code=403,
        media_type="application/json",
    )

# Ensure tables exist (idempotent)
Base.metadata.create_all(bind=_engine)


def _migrate_schema() -> None:
    """Add columns introduced after initial table creation."""
    import logging
    log = logging.getLogger(__name__)
    with _engine.connect() as conn:
        for stmt in [
            "ALTER TABLE sourcing_runs ADD COLUMN tier3_selection_json TEXT",
            "ALTER TABLE sourcing_runs ADD COLUMN maintenance_handoff_json TEXT",
            "ALTER TABLE sourcing_runs ADD COLUMN tier3_outreach_sent_json TEXT",
            # D2 prereq #1 — nullable tenant key (company PIN). Keys only, no enforcement.
            "ALTER TABLE sourcing_runs ADD COLUMN company_id VARCHAR(36)",
            # Multi-part Increment 1 — nullable basket grouping label. NULL for every
            # single-part run (indistinguishable from a pre-migration run); set only when
            # an intake fans one request into N runs. The CREATE INDEX is separate (below)
            # because the bare ALTER does not index the column on already-migrated DBs.
            "ALTER TABLE sourcing_runs ADD COLUMN group_id VARCHAR(36)",
            "CREATE INDEX IF NOT EXISTS ix_sourcing_runs_group_id ON sourcing_runs (group_id)",
            # DEMO_MODE session isolation — nullable per-visitor id (X-Session-Id) stamped on
            # a run at birth so the public no-login demo can scope reads/writes to the creating
            # visitor (IDOR fix). NULL for every non-demo / seeded run. Separate from company_id
            # (the validated Cognito tenant PIN) — see SourcingRunORM.session_id doc. Idempotent
            # additive ALTER, same pattern as company_id/group_id above.
            "ALTER TABLE sourcing_runs ADD COLUMN session_id VARCHAR(64)",
            "CREATE INDEX IF NOT EXISTS ix_sourcing_runs_session_id ON sourcing_runs (session_id)",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
                log.info("_migrate_schema: %s", stmt)
            except Exception:
                pass  # column already exists
    # Phase B0 — run-state foundation (document_status). Idempotent/additive.
    from utils.procurement_agent.state import persistence as _persistence
    _persistence.migrate_run_state(_engine)


_migrate_schema()


_HANDOFFS_PATH = os.path.join(os.path.dirname(__file__), "data", "mock_maintenance_handoffs.json")

def _seed_demo_maintenance_run() -> None:
    """Seed pending_intake runs from data/mock_maintenance_handoffs.json (idempotent per submission_id)."""
    try:
        with open(_HANDOFFS_PATH, encoding="utf-8") as f:
            handoffs = json.load(f)
    except FileNotFoundError:
        return

    with _SessionFactory() as session:
        all_runs = session.query(SourcingRunORM).all()
        existing_ids: set[str] = set()
        for r in all_runs:
            if r.maintenance_handoff_json:
                try:
                    sid = json.loads(r.maintenance_handoff_json).get("submission_id")
                    if sid:
                        existing_ids.add(sid)
                except (json.JSONDecodeError, AttributeError):
                    pass

        _urgency_map = {"emergency": 0.9, "predictive": 0.6, "standard": 0.3, "stocking": 0.1}
        now = datetime.now(timezone.utc)
        for handoff in handoffs:
            if handoff.get("submission_id") in existing_ids:
                continue
            urgency = handoff.get("context", {}).get("urgency", "standard")
            run = SourcingRunORM(
                id=str(uuid.uuid4()),
                facility_id=handoff["facility_id"],
                current_phase=Phase.PENDING_INTAKE.value,
                urgency_factor=_urgency_map.get(urgency, 0.3),
                warranty_status="unknown",
                asset_specs_json=json.dumps(handoff.get("asset_specs")) if handoff.get("asset_specs") else None,
                maintenance_handoff_json=json.dumps(handoff),
                approval_history_json="[]",
                initiated_at=now,
                updated_at=now,
            )
            session.add(run)
        session.commit()


_seed_demo_maintenance_run()

# ---------------------------------------------------------------------------
# In-memory chat message store (Phase 3 prototype — cleared on server restart)
# ---------------------------------------------------------------------------

_messages: Dict[str, List[Dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# DEMO_MODE session isolation — per-visitor IDOR scoping (X-Session-Id)
# ---------------------------------------------------------------------------
# The public no-login demo has no auth, so runs/groups have no owner. Run + group
# ids are UUIDv4 (not guessable), but if an id ever leaks (shared URL, logs, Referer)
# any visitor could read that run's full detail (specs, sourcing results, chat) or
# act on it. The X-Session-Id header (an unguessable, client-generated per-browser-
# session token) is stamped on a run at birth and scoped on every read/write under
# DEMO_MODE. All guards here are INERT when DEMO_MODE is off — normal dev/prod ops
# are byte-for-byte unchanged (no session_id is ever read or written).
#
# Existence-oracle discipline: a mismatched-owner access returns 404, NEVER 403. A
# 403 tells an attacker "this id exists but isn't yours"; a 404 is indistinguishable
# from "no such id" and leaks nothing. So not-found and not-owned are the SAME 404.

# An X-Session-Id must be 16–64 chars of [A-Za-z0-9-] (admits a UUIDv4 with hyphens
# or a base62 token). Length + charset bounded so the value can't be used for
# injection or unbounded storage. Validated identically on the read and write paths.
_DEMO_SESSION_ID_RE: re.Pattern = re.compile(r"^[A-Za-z0-9-]{16,64}\Z")


def _demo_session_id_from_request(request: Request) -> Optional[str]:
    """Read + validate the X-Session-Id header under DEMO_MODE. Returns the validated
    session id, or None when absent/malformed (a read with a bad/missing header is
    treated as "no session" -> 404 on any owned row, never an existence oracle).
    Inert when DEMO_MODE is off (returns None — no scoping)."""
    if not DEMO_MODE:
        return None
    raw = (request.headers.get("x-session-id") or "").strip()
    if not raw:
        return None
    return raw if _DEMO_SESSION_ID_RE.fullmatch(raw) else None


def _require_demo_session_id(request: Request) -> str:
    """DEMO_MODE write-path validation: return a valid X-Session-Id or 422. A write
    (run birth) must carry a valid session so the run can be scoped to its creator.
    Inert when DEMO_MODE is off (returns "" -> no session_id stamped; caller maps
    "" to None on the ORM)."""
    if not DEMO_MODE:
        return ""
    raw = (request.headers.get("x-session-id") or "").strip()
    if not raw:
        raise HTTPException(
            status_code=422,
            detail="X-Session-Id header is required in DEMO_MODE",
        )
    if not _DEMO_SESSION_ID_RE.fullmatch(raw):
        raise HTTPException(
            status_code=422,
            detail="X-Session-Id header is malformed (expected 16–64 chars of [A-Za-z0-9-])",
        )
    return raw


def _demo_run_owned_by_session(run: SourcingRunORM, session_id: Optional[str]) -> bool:
    """Under DEMO_MODE, True iff `session_id` owns `run`. False for a NULL/missing
    row session (seeded/legacy runs are not owned by any demo visitor) and for a
    missing/mismatched request session. The caller returns 404 on False — NOT 403 —
    so not-owned is indistinguishable from not-found (no existence oracle). Inert
    when DEMO_MODE is off (always True — no scoping)."""
    if not DEMO_MODE:
        return True
    row_sid = getattr(run, "session_id", None)
    if not row_sid:        # NULL/legacy/seeded -> not owned by any demo visitor
        return False
    if not session_id:     # no/invalid request session -> can't own anything
        return False
    return row_sid == session_id


# ---------------------------------------------------------------------------
# DEMO_MODE rate limiting — per-session spend/run caps (the cost-DoS ceiling)
# ---------------------------------------------------------------------------
# A visitor can loop the legit intake -> confirm -> source path; each sourcing run
# is ~13 external calls (Tavily x6, Anthropic intake/brand-intel, comparison LLMs).
# Unbounded that is a cost-DoS. Under DEMO_MODE a per-session cap bounds it. The
# expensive op is confirm-intake (it schedules the background sourcing); run birth
# (POST /api/runs) is cheap but is also capped to bound row spam and the funnel top.
#
# In-process counter store (single-instance ECS demo — no Redis). Counters reset on
# server restart (CLEANUP: if the demo ever scales to multi-instance, this state
# must move to a shared store or a visitor just restarts their count per instance).
# Acceptable for a bounded public demo; spend is also hard-capped by the Apollo
# block and the per-run external-call shape.
#
# Store is keyed by (scope, key) so a NEW cap dimension is an added scope+key, NOT a
# refactor: today key = session_id (per-session). A future per-IP cap is a second
# call, e.g. _demo_enforce_cap("runs_per_ip", client_ip, DEMO_MAX_RUNS_PER_IP, ...),
# reusing the same store + helper unchanged.

def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to `default` on missing/unparseable (fail
    to the safe default rather than crashing the boot on a bad config value)."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# Caps are read at import (module globals); tests override via monkeypatch on the
# module attribute (the helpers resolve them at call time). Defaults bound abuse
# while leaving headroom for a genuine multi-part demo.
_DEMO_MAX_RUNS_PER_SESSION: int = _env_int("DEMO_MAX_RUNS_PER_SESSION", 10)
_DEMO_MAX_SOURCING_PER_SESSION: int = _env_int("DEMO_MAX_SOURCING_PER_SESSION", 5)
# Seconds hinted in the 429 Retry-After header (the caps are simple totals, not a
# sliding window, so this is a courtesy hint rather than a precise reset time).
_DEMO_RETRY_AFTER_SEC: int = _env_int("DEMO_RATE_RETRY_AFTER_SEC", 60)
# Upload size cap (FIX C) — max bytes for POST /api/runs/{id}/upload, enforced
# read-then-check before vision extraction. Default 10MB (a nameplate photo).
_DEMO_MAX_UPLOAD_BYTES: int = _env_int("DEMO_MAX_UPLOAD_BYTES", 10 * 1024 * 1024)


class _DemoRateCounter:
    """Thread-safe in-process counter for DEMO_MODE rate caps. A counter is keyed by
    a (scope, key) tuple — `scope` names the dimension ("runs_per_session",
    "sourcing_per_session", a future "runs_per_ip"...), `key` is the identity within
    it (session_id, client IP, ...). checked_incr is the atomic check-and-increment:
    returns the new count, or -1 when the cap is already reached (no increment on a
    denied attempt — a rejected call never counts against the limit)."""

    def __init__(self) -> None:
        self._counts: Dict[tuple, int] = {}
        self._lock = threading.Lock()

    def checked_incr(self, scope: str, key: str, cap: int) -> int:
        """Atomically: if the (scope, key) count is already >= cap, return -1 (denied,
        no increment); else increment and return the new count. cap <= 0 means
        unlimited (returns 0, no increment)."""
        if cap <= 0:
            return 0
        with self._lock:
            k = (scope, key)
            cur = self._counts.get(k, 0)
            if cur >= cap:
                return -1
            self._counts[k] = cur + 1
            return cur + 1

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


_DEMO_COUNTERS = _DemoRateCounter()


def _demo_enforce_cap(scope: str, key: str, cap: int, label: str) -> None:
    """Under DEMO_MODE, raise 429 (with Retry-After) if the (scope, key) counter has
    reached `cap`; otherwise increment it. Inert when DEMO_MODE is off, when cap <= 0
    (unlimited), or when `key` is empty (no session -> the write path already 422s
    via Part A; reads don't spend). A new cap dimension (e.g. per client-IP) is a new
    scope + key passed here — the store and this helper do not change."""
    if not DEMO_MODE or cap <= 0 or not key:
        return
    result = _DEMO_COUNTERS.checked_incr(scope, key, cap)
    if result < 0:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Demo {label} limit reached ({cap} per session). "
                "Please start a new browser session to continue the demo."
            ),
            headers={"Retry-After": str(_DEMO_RETRY_AFTER_SEC)},
        )


# ---------------------------------------------------------------------------
# Pydantic I/O models
# ---------------------------------------------------------------------------

class RunListItem(BaseModel):
    id: str
    phase: str
    urgency: str          # "Stocking" | "Predictive" | "Emergency"
    warranty: str         # "Active" | "Expired" | "Unknown"
    facility_id: str
    group_id: Optional[str] = None        # basket grouping label; NULL for single-part runs
    asset_summary: Optional[str] = None   # e.g. "Goulds 3196MTX · 5HP pump"
    amount: Optional[float] = None
    maintenance_submission_id: Optional[str] = None
    created_at: str
    updated_at: str


class RunDetail(BaseModel):
    id: str
    phase: str
    urgency: str
    warranty: str
    facility_id: str
    facility_state: str = "unknown"      # for geographic indicator on vendor cards
    group_id: Optional[str] = None       # basket grouping label; NULL for single-part runs
    asset_specs: Optional[Dict[str, Any]] = None
    inventory_result: Optional[Dict[str, Any]] = None
    sourcing_results: Optional[Dict[str, Any]] = None
    selected_candidate: Optional[Dict[str, Any]] = None
    approval_history: List[Dict[str, Any]] = []
    messages: List[Dict[str, Any]] = []
    tier3_selection: Optional[List[str]] = None
    tier3_outreach_sent: Optional[Dict[str, str]] = None  # candidateId → sentAt ISO (legacy synthetic stamp)
    # Real per-candidate outreach signal derived from rfq_drafts (the A0–A2 flow), keyed by the
    # candidate display id: {candidate_id: drafted|approved|rejected|sent}. Additive companion to
    # the legacy tier3_outreach_sent map — 'sent' here means a genuine message went (A2 marks
    # 'sent' only on a real send), so it is dispatch-truth, not a synthetic timestamp.
    rfq_draft_status: Optional[Dict[str, str]] = None
    maintenance_handoff: Optional[Dict[str, Any]] = None
    # True when T2+T3 have candidates but none have pnMatchLevel=="exact".
    # Suppressed when spec_based_sourcing or part_number is absent (not a typo case).
    no_exact_match: bool = False
    created_at: str
    updated_at: str


class SubmissionContext(BaseModel):
    chat_thread_summary: str
    urgency: str = "standard"   # "emergency" | "predictive" | "standard"
    work_order_id: Optional[str] = None
    asset_tag: Optional[str] = None


class MaintenanceSubmission(BaseModel):
    submission_id: str
    facility_id: str
    submitted_by: str
    asset_specs: Optional[Dict[str, Any]] = None
    context: SubmissionContext


class CreateRunRequest(BaseModel):
    facility_id: str = Field("00000000-0000-0000-0000-000000000000", max_length=64)
    urgency_factor: float = Field(0.3, ge=0.0, le=1.0)
    warranty_status: str = "unknown"
    # Optional basket label so a per-item list can mint a group up front and create each run
    # into it (the incremental "+ add another part" flow). NULL/omitted -> group-less, exactly
    # as the legacy single-run path. SECURITY DEBT: client-supplied + unvalidated — see CLEANUP.
    group_id: Optional[str] = Field(None, max_length=64)
    # Optional pre-extracted asset_specs to SEED the run at birth (multi-part fan-out seeds each
    # card from the already-parsed per-part specs — no re-extraction). None -> bare intake run,
    # exactly the legacy path.
    asset_specs: Optional[Dict[str, Any]] = None


class CreateRunResponse(BaseModel):
    id: str
    phase: str
    created_at: str


class AssetSpecsSeedRequest(BaseModel):
    """Body for PUT /api/runs/{id}/asset-specs — the post-birth equivalent of the createRun
    birth-seed: write pre-extracted specs onto an EXISTING run (multi-part fan-out seeds part 1
    onto the already-created run 0). Same shape/security posture as CreateRunRequest.asset_specs."""
    asset_specs: Dict[str, Any]


class IntakeRequest(BaseModel):
    """Front-door intake body (multi-part Increment 1, Stage 2). Only the COUNT of `parts`
    routes single-vs-multi; the part CONTENTS are not parsed here — intake yields one
    asset_specs per run today, and multi-part extraction is a separate later concern. An
    empty or single-element `parts` takes the existing single-run path verbatim."""
    parts: List[Dict[str, Any]] = Field(default_factory=list, max_length=10)
    facility_id: str = Field("00000000-0000-0000-0000-000000000000", max_length=64)
    urgency_factor: float = Field(0.3, ge=0.0, le=1.0)
    warranty_status: str = "unknown"


class SendMessageRequest(BaseModel):
    # Always-on cap (FIX C): a 4000-char limit bounds per-request work (a pathological
    # description could fan out into many sourcing calls / a huge LLM prompt) and
    # protects both demo and prod — no legitimate part description exceeds it. 422 on
    # exceed (Pydantic).
    content: str = Field(..., max_length=4000)
    role: str = "user"


class SendMessageResponse(BaseModel):
    run_id: str
    message: Dict[str, Any]
    updated_phase: str
    # Intake sufficiency state (e.g. "multi_part_detected") so the frontend can react — None on
    # legacy/single-part responses (additive, non-breaking).
    proceed_state: Optional[str] = None
    # When proceed_state == "multi_part_detected", the N parsed per-part extraction dicts so the
    # frontend can fan them into N seeded cards. None otherwise.
    parts: Optional[List[Dict[str, Any]]] = None


class SelectCandidateRequest(BaseModel):
    candidate_id: str
    tier: int


class ConfirmationRequest(BaseModel):
    candidate_ids: List[str]


class ApproveRequest(BaseModel):
    approver_name: str
    approver_role: str
    notes: Optional[str] = None


class RejectRequest(BaseModel):
    approver_name: str
    approver_role: str
    notes: str


class OutreachRequest(BaseModel):
    candidate_ids: List[str]


class SaveOutreachRequest(BaseModel):
    candidate_ids: List[str]


class FacilityOut(BaseModel):
    id: str
    name: str
    state: str


class ApprovalRuleOut(BaseModel):
    id: str
    facility_id: str
    threshold: float
    cap: Optional[float]
    approvers_required: int
    approver_roles: List[str]
    applies_to: str          # "buy" | "outreach"


class ApprovalRuleIn(BaseModel):
    facility_id: str
    threshold: float
    cap: Optional[float] = None
    approvers_required: int = 1
    approver_roles: List[str] = []
    applies_to: str = "buy"
    id: Optional[str] = None          # set to update a rule in place; omit to insert


# ---------------------------------------------------------------------------
# Facility state lookup (mock — Phase 5 will pull from DB table)
# ---------------------------------------------------------------------------

_FACILITY_STATES: Dict[str, str] = {
    "fac-stockton": "CA",
    "fac-modesto":  "CA",
    "fac-fresno":   "CA",
    "fac-salinas":  "CA",
    "fac-cerritos": "CA",
}

# Tier 1 mock confirmation delay range (seconds).
# Simulates vendor response time for prototype demos.
_MOCK_CONFIRMATION_DELAY_RANGE: tuple = (3, 8)

_URGENCY_FACTORS: Dict[str, float] = {
    "emergency":  0.9,
    "predictive": 0.5,
    "standard":   0.3,
}


# ---------------------------------------------------------------------------
# Sourcing results transformation: backend SourcingOption → frontend Candidate
# ---------------------------------------------------------------------------

# Below this identification/extraction confidence (0–100), an extracted price is shown
# as UNVERIFIED rather than a firm figure (the number is kept, but labelled). Tunable.
_PRICE_CONFIDENCE_FLOOR: float = 40.0

# State C: a CONFIRMED quote's extraction confidence floor — on the Quote.confidence
# 0.0–1.0 scale (NOT the 0–100 _PRICE_CONFIDENCE_FLOOR above). Below this a quote is
# shown supplier-confirmed but with the unverified treatment (a shaky extraction is not
# masked by the confirm). Tunable.
_QUOTE_CONFIDENCE_FLOOR: float = 0.4


def _lead_time_label(days: int) -> str:
    if days <= 1:  return "Next day"
    if days <= 5:  return f"{days} days"
    if days <= 14: return "1–2 weeks"
    if days <= 30: return "2–4 weeks"
    return "4+ weeks"


def _lead_time_emit(opt: dict) -> Optional[str]:
    """Honest leadTime for the API: a day-count label ONLY when a real value backs it, else
    None. A 'placeholder' (T3 pre-quote) or an absent lead_time_days returns None — never the
    fabricated 'Next day' the old `or 0` produced. A stated 0/1-day lead correctly still shows
    'Next day'. 'defaulted'/'extracted' values render their number (the UI qualifies via
    leadTimeSource); a confirmed quote stamps 'quoted' on the overlay (see _quote_overlay)."""
    source = opt.get("lead_time_source") or "defaulted"   # None (cache-recon path) -> defaulted
    raw_days = opt.get("lead_time_days")
    if source == "placeholder" or raw_days is None:
        return None
    try:
        return _lead_time_label(int(raw_days))
    except (TypeError, ValueError):
        return None


def _vendor_type(merchant_type: str) -> str:
    return {
        "Enterprise":           "NetworkPartner",
        "Direct Buy via Arkim": "NetworkPartner",
        "National Specialist":  "NationalDistributor",
        "Quote Request":        "RegionalSpecialist",
        "Local":                "RegionalSpecialist",
    }.get(merchant_type, "NationalDistributor")


def _specs_from_dict(specs_dict: dict):
    """Build an AssetSpecs from the run's asset_specs_json dict for the cache path.

    Minimal, mock-independent field-filter (mirrors SourcingAgent._dict_to_specs'
    filtering without coupling the cache path to the SourcingAgent class — which
    tests mock, and which the discovery path below replaces wholesale). Used only
    to detect the request noun-class for the T2 cache type-gate; on any failure
    the caller treats the class as undetectable (None) and keeps cached edges.
    """
    import dataclasses
    from utils.models import AssetSpecs
    fields = {f.name for f in dataclasses.fields(AssetSpecs)}
    filtered = {k: v for k, v in (specs_dict or {}).items() if k in fields}
    required = {"manufacturer", "model", "part_number", "voltage"}
    kwargs = {k: v for k, v in filtered.items() if k not in required}
    return AssetSpecs(
        manufacturer=filtered.get("manufacturer") or "Unknown",
        model=filtered.get("model") or "Unknown",
        part_number=filtered.get("part_number") or "UNKNOWN-PN",
        voltage=filtered.get("voltage") or "N/A",
        **kwargs,
    )


def _pn_match_level(opt: dict, tier: int) -> str:
    if tier == 1:
        return "exact" if opt.get("found_part_number") else "none"
    return {
        "exact_match":   "exact",
        "partial_match": "normalized",
        "no_match":      "none",
        "not_visible":   "none",
    }.get(opt.get("pn_match_status") or "", "none")


def _camel_artifact(art: Optional[dict]) -> Optional[dict]:
    """Map the snake_case spec-comparison artifact to the camelCase shape the frontend
    Candidate.comparisonArtifact expects. Until now the raw artifact was passed through
    verbatim, so the frontend's comparison[] / engineerNotes reads silently saw nothing
    (snake/camel mismatch)."""
    if not art:
        return None
    return {
        "fidelity":             art.get("fidelity"),
        "compatibilitySummary": art.get("compatibility_summary"),
        "comparison": [
            {"field": f.get("field"), "fieldLabel": f.get("field_label"),
             "assetValue": f.get("asset_value"), "candidateValue": f.get("candidate_value"),
             "match": f.get("match"), "notes": f.get("notes")}
            for f in (art.get("comparison") or [])
        ],
        "verificationRequiredFields": art.get("verification_required_fields") or [],
        "engineerNotes":        art.get("engineer_notes"),
    }


def _transform_option(opt: dict, tier: int, idx: int, quote: Optional[dict] = None) -> dict:
    price_hidden = opt.get("price_tbd", False) or opt.get("requires_rfq", False)
    price = None if price_hidden else opt.get("base_price")
    out = {
        "id":                    f"{opt.get('vendor_name','')}-t{tier}-{idx}",
        "vendorName":            opt.get("vendor_name") or "Unknown",
        "vendorType":            _vendor_type(opt.get("merchant_type") or ""),
        "tier":                  tier,
        "price":                 price,
        # Evidence state: "priced" = a real listing price exists; "uncontacted" = no
        # price/quote, so the UI must NOT assert a part match. "quoted" (the strongest
        # state — a human-confirmed RFQ quote) is applied by _quote_overlay below when a
        # matched confirmed quote is passed in (State C, increment 3b).
        "evidenceState":         "priced" if price is not None else "uncontacted",
        # Purchase channel (increment 2, State M): "marketplace" = a buyable price at a
        # curated transactable marketplace (buy directly); any other priced row =
        # "reference" (price read off a page). State M requires a real price.
        # DEFERRED DECISION: this is transform-derived (display-only) because State M is a
        # label + coming-soon button with no real purchase behaviour yet. When manual-
        # fulfilment "buy now" becomes a real ACTION, promote purchase_channel to a
        # SourcingOption model field (it will then drive behaviour, not just display).
        "purchaseChannel":       "marketplace" if (price is not None and is_marketplace(opt.get("source_url"))) else "reference",
        # Lead time, HONESTLY (see lead_time_source / models.lead_time_source_for): emit a
        # day-count ONLY when there's a real value behind it. A "placeholder" (T3 pre-quote — no
        # lead time exists yet) or an absent lead_time_days -> null, NEVER the fabricated
        # "Next day" the old `or 0` produced. A "defaulted" heuristic still shows its number but
        # carries leadTimeSource so the UI (Stage 3) can mark it estimated; "extracted"/"quoted"
        # are real. leadTimeSource is always present; leadTime may be null.
        "leadTime":              _lead_time_emit(opt),
        "leadTimeSource":        opt.get("lead_time_source") or "defaulted",
        "url":                   opt.get("source_url") or "",
        # The listing's actual PN — surfaced so a priced exact/equivalent claim is verifiable.
        "foundPartNumber":       opt.get("found_part_number"),
        "suitability":           float(opt.get("suitability_score") or 0),
        "confidence":            float(opt.get("confidence_score") or 0),
        "pnMatchLevel":          _pn_match_level(opt, tier),
        "loc":                   opt.get("ship_from_country") or "",
        "isExactMatch":          opt.get("match_type") == "Exact OEM",
        "isAftermarket":         opt.get("match_type") == "Aftermarket Compatible",
        "isOemDirect":           bool(opt.get("is_oem_direct")),
        "isAuthorizedDistributor": opt.get("vendor_authorization_status") == "Authorized",
        "priceVerified":         not opt.get("limited_price_data", False),
        # A priced row whose extraction confidence is a real low reading (0 < c < floor)
        # is shown as an UNVERIFIED price (kept + labelled), not a firm figure — displayed
        # confidence must match actual confidence (live: $173 @ conf 28% was a mis-extraction).
        # A 0/absent score is "no confidence signal" (e.g. the Tier 2 lane doesn't populate
        # it), NOT low confidence — those rows are not flagged here (their price honesty is
        # handled by priceVerified/limited_price_data).
        # RANKING_BANDS_V1 cache path: an explicit price_stale flag also marks the
        # price unverified (the band pass replaces the legacy 1.0-confidence stale
        # marker with evidence-derived confidence, so the flag carries the signal).
        # Flag-off candidates never carry price_stale — behavior unchanged.
        "priceUnverified":       price is not None and (
                                     0 < float(opt.get("confidence_score") or 0) < _PRICE_CONFIDENCE_FLOOR
                                     or bool(opt.get("price_stale"))),
        # Real stock signal only when the listing actually reported in-stock; else omitted
        # (no fabricated stock). Wires the previously-dead frontend `stock` read.
        "stock":                 "In stock" if opt.get("in_stock") is True else None,
        "shipFrom":              opt.get("ship_from_country"),
        "contact":               opt.get("contact_email"),
        "relationship":          opt.get("suitability_tier") or None,
        "comparisonArtifact":    _camel_artifact(opt.get("comparison_artifact")),
        # Tier 1 two-mode display: all Tier 1 candidates start in confirmation-needed mode.
        # After POST /request-confirmation fires and mock response arrives (3-8 s),
        # confirmation_needed is set False in the raw data and this flips to False,
        # causing the frontend card to switch from "Request Confirmation" to "Buy Now".
        "confirmationPending":   tier == 1 and bool(opt.get("confirmation_needed", True)),
        # Night 5 (T4) — aftermarket disclosure + registry provenance. The DATA reaches
        # the candidate payload here; frontend rendering is morning work. isAftermarket
        # above is derived from match_type (kept for back-compat); the explicit
        # aftermarket_disclosure text + tier1_match_explanation are registry-backed Tier 1
        # provenance that a card / audit can surface.
        "aftermarketDisclosure":  opt.get("aftermarket_disclosure"),
        "registryBacked":         bool(opt.get("is_registry_backed")),
        "tier1MatchExplanation":  opt.get("tier1_match_explanation"),
    }
    if quote:
        out.update(_quote_overlay(quote))
    return out


# ---------------------------------------------------------------------------
# State C (increment 3b): join CONFIRMED quotes back to the candidate we emailed and
# overlay the supplier-confirmed claim. The deterministic join is the thread key carried
# in 3a (reply -> thread -> the exact outbound); a domain fallback covers legacy /
# out-of-thread quotes (NULL thread_id). Candidate identity within a run is its
# normalized source_url domain (the candidate set is domain-deduped), which is also how
# the quote's supplier_domain was recorded — so domain is the operative candidate-level
# key, and thread disambiguates when several outbounds went to one domain in a run.
# ---------------------------------------------------------------------------

def _index_quotes(quotes: list[dict], sent: list[dict]) -> dict:
    """Build the lookup used to overlay confirmed quotes onto candidates.

    Only status=="confirmed" quotes participate (a human-confirmed quote earns the
    supplier-confirmed claim; pending / needs_human_review stay in the review queue).
    `quotes`/`sent` come newest-first (get_review_items / get_sent_messages order), so a
    setdefault keeps the most recent per key. Returns {by_thread, by_domain,
    domain_threads}: by_thread/by_domain index the quotes; domain_threads maps a supplier
    domain -> the thread_ids we sent it (to resolve a candidate's outbound thread)."""
    confirmed = [q for q in quotes if q.get("status") == "confirmed"]
    domain_threads: dict[str, set] = {}
    for s in sent:
        dom, tid = s.get("supplier_domain"), s.get("thread_id")
        if dom and tid:
            domain_threads.setdefault(dom, set()).add(tid)
    by_thread: dict[str, dict] = {}
    by_domain: dict[str, dict] = {}
    for q in confirmed:
        if q.get("thread_id"):
            by_thread.setdefault(q["thread_id"], q)
        if q.get("supplier_domain"):
            by_domain.setdefault(q["supplier_domain"], q)
    return {"by_thread": by_thread, "by_domain": by_domain, "domain_threads": domain_threads}


def _build_quote_index(run_id: str) -> dict:
    """Assemble the quote index for a run from the registry (confirmed quotes + sent
    rows). Fail-soft: any registry error degrades to an empty index (no overlay), never
    breaks the run-detail read.

    Night 11 (QUOTE_SUBMIT_V1): ACTIVE structured quotes (utils/quote_store) are
    merged in as confirmation records — the I1 decision: adapt new records into
    the EXISTING index shape here, at the single index builder, so the promotion
    path downstream (_resolve_quote → _quote_overlay → the T4 read-time Band-C
    promotion) consumes them unchanged. No second promotion mechanism.
    Precedence: a structured quote OVERRIDES the domain-fallback slot (it is the
    supplier's own authoritative submission, with a real lifecycle — supersede/
    expiry — behind it); thread-matched concierge-confirmed email quotes still
    win thread-primary resolution for candidates with thread history. Read-time
    expiry in get_active_quotes means a lapsed quote simply stops appearing here
    — the card reverts honestly, no zombie confirmations (spec §6). Flag OFF ⇒
    this block is skipped and the index is byte-identical to pre-Night-11."""
    try:
        from utils import supplier_registry
        quotes = supplier_registry.get_review_items(run_id=run_id, kind="quote")
        sent = supplier_registry.get_sent_messages(run_id=run_id)
        index = _index_quotes(quotes, sent)
    except Exception as exc:  # never let a quote-store hiccup break the run view
        import logging
        logging.getLogger(__name__).warning(
            "[%s] _build_quote_index failed, no quote overlay: %s", run_id, exc)
        return {"by_thread": {}, "by_domain": {}, "domain_threads": {}}
    if _quote_submit_enabled():
        try:
            from utils import quote_store
            seen: set = set()
            for q in quote_store.get_active_quotes(run_id):  # newest first
                rec = quote_store.as_confirmation_record(q)
                dom = rec.get("supplier_domain")
                if dom and dom not in seen:  # newest structured quote per domain
                    index["by_domain"][dom] = rec
                    seen.add(dom)
        except Exception as exc:  # same fail-soft discipline as the registry read
            import logging
            logging.getLogger(__name__).warning(
                "[%s] structured-quote merge failed, registry index kept: %s",
                run_id, exc)
    return index


def _resolve_quote(opt: dict, index: dict) -> Optional[dict]:
    """Find the confirmed quote for a candidate: thread-precise first (a quote on a thread
    we sent to this candidate's domain), domain fallback second (legacy / NULL-thread).
    None when the candidate has no domain or no confirmed quote matches."""
    if not index:
        return None
    from utils.supplier_registry import _normalize_domain
    url = opt.get("source_url")
    dom = _normalize_domain(url) if url else ""
    if not dom:
        return None
    for tid in index.get("domain_threads", {}).get(dom, ()):   # thread-primary
        q = index.get("by_thread", {}).get(tid)
        if q:
            return q
    return index.get("by_domain", {}).get(dom)                 # domain fallback


def _quote_overlay(quote: dict) -> dict:
    """The State-C overlay applied to a candidate with a matched confirmed quote: the
    quote's price/lead/terms OVERRIDE the listing/discovery values and the row claims
    supplier-confirmed (evidenceState="quoted"). The extraction-quality honesty COMPOSES:
    a confirmed-but-shaky extraction (0 < confidence < floor, 0–1 scale) still carries
    quoteUnverified, so "supplier-confirmed" never masks a weak extraction. A 0/absent
    confidence is "no signal" (not flagged), mirroring the priceUnverified discipline."""
    payload = quote.get("payload") or {}
    conf = quote.get("confidence")
    if conf is None:
        conf = payload.get("confidence")
    conf = float(conf) if conf is not None else None
    overlay: dict = {
        "evidenceState":     "quoted",
        "quoteConfirmed":    True,
        "supplierConfirmed": True,
        "quoteUnverified":   conf is not None and 0 < conf < _QUOTE_CONFIDENCE_FLOOR,
        "quoteThreadId":     quote.get("thread_id"),   # provenance: the exact outbound
        "quoteCurrency":     payload.get("currency") or "USD",
    }
    price = payload.get("unit_price")
    if price is not None:
        try:
            overlay["price"] = float(price)
        except (TypeError, ValueError):
            pass
    if payload.get("lead_time"):
        overlay["leadTime"] = payload["lead_time"]        # quote free-text lead overrides label
        overlay["leadTimeSource"] = "quoted"              # the strongest provenance: a confirmed quote
    if payload.get("terms"):
        overlay["terms"] = payload["terms"]
    # Night 11 (QUOTE_SUBMIT_V1) — buyer-card provenance from STRUCTURED quotes.
    # Keyed on the payload's own fields (only quote_store records carry them), so
    # email-parsed quotes and flag-off responses are byte-identical:
    #   quoteConfirmedAt — "confirmed by supplier {date}" on the card.
    #   quotedPartNumber/pnDiffers — a review-APPROVED alternative promotes
    #   labelled as the QUOTED PN with equivalent-alternative framing, never
    #   silently as the requested PN (spec §6 / criterion 4).
    if payload.get("submitted_at"):
        overlay["quoteConfirmedAt"] = payload["submitted_at"]
    if payload.get("quote_id"):
        overlay["quoteId"] = payload["quote_id"]  # order-provenance hook (spec §7)
    if payload.get("pn_differs"):
        overlay["pnDiffers"] = True
        if payload.get("quoted_part_number"):
            overlay["quotedPartNumber"] = payload["quoted_part_number"]
    return overlay


def _transform_sourcing_results(raw: dict, quote_index: Optional[dict] = None) -> dict:
    """Convert SourcingAgent output dict to the shape expected by the React frontend.
    When `quote_index` is given (the run's confirmed quotes), a matched candidate is
    overlaid with the State-C supplier-confirmed claim; without it the transform is
    unchanged (back-compat)."""
    def _tier(key: str, n: int) -> list:
        out = []
        for i, o in enumerate(raw.get(key, {}).get("results", [])):
            if o.get("rejection_reason"):
                continue
            out.append(_transform_option(o, n, i, quote=_resolve_quote(o, quote_index)))
        return out
    result = {
        "tier1":               _tier("tier_1", 1),
        "tier2":               _tier("tier_2", 2),
        "tier3":               _tier("tier_3", 3),
        "warrantyBanner":      raw.get("warranty_banner"),
        "tier3CapabilityPivot": raw.get("tier3_capability_pivot", False),
    }
    # RANKING_BANDS_V1 (spec §7) — a BANDED raw result additionally distinguishes
    # findings (Band A/B cards, banded order) from outreachTargets (the Band-C
    # ask-and-see block: onboarded supplier named first, capped seeds, provenance
    # strings, NO numbers). Keyed off the result's own ranking_bands:v1 marker
    # (not the live env) so a stored flag-off run never grows these keys —
    # flag-off responses stay byte-identical. Fail-soft: a builder error degrades
    # to the legacy shape.
    if "ranking_bands:v1" in (raw.get("filters_applied") or []):
        try:
            from utils.procurement_agent.ranking_bands import (
                banded_findings, is_onboarded, outreach_targets,
                promote_confirmed, provenance_for,
            )
            # T4 — band mobility (spec §3): a Band-C candidate whose CONFIRMED
            # quote carries a real price is promoted to Band A at read time (the
            # quote arrives after sourcing results were persisted, so promotion
            # is computed per read from the live quote index — "at that moment").
            # Being onboarded, a promoted supplier tops Band A (onboarded-first).
            # Mutates only this request's parsed copy; nothing is written back.
            for _tier_key in ("tier_1", "tier_2", "tier_3"):
                for _o in (raw.get(_tier_key) or {}).get("results") or []:
                    if _o.get("band") == "C" and not _o.get("rejection_reason"):
                        _q = _resolve_quote(_o, quote_index)
                        if _q and (_q.get("payload") or {}).get("unit_price") is not None:
                            promote_confirmed(_o)
            result["findings"] = [
                {
                    **_transform_option(o, n, i, quote=_resolve_quote(o, quote_index)),
                    "band":            o.get("band"),
                    "evidenceQuality": o.get("evidence_quality"),
                    "isMock":          bool(o.get("is_mock")),  # contract: always False here
                }
                for o, n, i in banded_findings(raw)
            ]
            result["outreachTargets"] = {
                "suppliers": [
                    {
                        "vendorName": c.get("vendor_name"),
                        "onboarded":  is_onboarded(c),
                        "provenance": c.get("provenance") or provenance_for(c),
                    }
                    for c in outreach_targets(raw)
                ],
            }
            result["outreachTargets"]["requestedCount"] = \
                len(result["outreachTargets"]["suppliers"])
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "findings/outreach build failed (legacy shape kept): %s", exc)
    return result


# ---------------------------------------------------------------------------
# Background sourcing task
# ---------------------------------------------------------------------------

def _result_from_cached_edges(edges: list,
                              request_noun_class: Optional[str] = None) -> dict:
    """LEGACY (flag-off) cache replay: reconstruct the SourcingAgent result shape
    from cached known_parts edges, so a cache hit flows through the same
    transform/persist path without re-discovery.

    RANKING_BANDS_V1 runs NEVER take this path (design correction, RANKING_BANDS
    spec §6 primary rule: a fresh Tier-2/3 discovery ALWAYS runs — the cache
    ACCELERATES discovery as seeds, it never replaces it; see
    _seed_candidates_into_result). This function is the byte-identical legacy
    behavior for flag-off runs only.

    A STALE cached price is marked low-confidence so the transform flags it
    priceUnverified (a stale price shown as current is an overclaim — same honesty
    discipline as the unverified-price work). No comparison artifact on cache hits —
    the spec comparison is recomputed only on a fresh discovery.

    Fix B1 — validation parity with the fresh-discovery path: cached edges are
    run through the SAME suitability floor + rejection_reason filter the
    SourcingAgent applies to live results, so a cached edge that is below-floor
    (or was written with a rejection_reason) does NOT surface to the UI just
    because the cache-hit path bypassed SourcingAgent. Without this, a poisoned
    cache entry (wrong-part results that scored >=30 on vendor authority when
    the QUERY was wrong) would be served forever without re-validation. The
    filter here only catches below-suitability cached edges; it does NOT catch
    wrong-PART-TYPE edges (a pump edge cached under a seal part-key can still
    score >=30) — clearing those requires the query fix (Fix A) + clearing the
    poisoned cache key (Fix B2), not this floor. Re-uses the same
    TIER_SURFACE_MIN_SUITABILITY constant and the annotate-then-skip discipline
    the SourcingAgent + _transform_sourcing_results use.

    T2 — cache type-gate (CLEANUP §7.1 / Phase 1): in addition to the floor, a
    cached edge is DROPPED when its noun-class (classified from vendor + url) is
    confirmed DIFFERENT from the request's noun-class. This catches the
    wrong-PART-TYPE case the floor misses (the :987-990 comment above): a pump
    edge cached under a seal part-key can still score >=30, but it is NOT a
    seal, so on a seal request it is now dropped. ``request_noun_class`` is the
    request's detected noun-class (from ``scoring._query_noun_class``), threaded
    in from the call site; None (request undetectable) → keep everything
    (neutral, mirrors the TypeGate's query-undetectable floor). Undetectable
    result class → keep (ESCI floor). Same class → keep. Confirmed-different
    → drop."""
    from utils.sourcing_archieved.constants import TIER_SURFACE_MIN_SUITABILITY
    from utils.sourcing_archieved.scoring import classify_result_noun_class_dominant
    tiers: dict = {1: [], 2: [], 3: []}
    for e in edges:
        price = e.get("price")
        mt = e.get("match_type") or "Functional Alternative"
        pn_status = ("exact_match" if mt == "Exact OEM"
                     else "partial_match" if mt == "Aftermarket Compatible"
                     else "not_visible")
        cand = {
            "vendor_name":       e.get("display_name") or e.get("supplier_id"),
            "base_price":        price,
            "price_tbd":         price is None,
            "requires_rfq":      e.get("purchase_channel") == "rfq",
            "source_url":        e.get("source_url"),
            "match_type":        mt,
            "pn_match_status":   pn_status,
            "found_part_number": e.get("found_pn"),
            "suitability_score": e.get("suitability") or 0,
            # Stale cached price -> low confidence -> priceUnverified in the UI; fresh ->
            # no signal (shown firm). Reuses the existing confidence-floor treatment.
            "confidence_score":  1.0 if e.get("price_stale") else 0.0,
            "lead_time_days":    e.get("lead_days") or 5,
            "from_cache":        True,
        }
        # Fix B1 — drop cached edges already carrying a rejection_reason, and
        # apply the suitability floor to the rest (same threshold as live runs).
        if e.get("rejection_reason"):
            continue
        if float(cand["suitability_score"] or 0.0) < TIER_SURFACE_MIN_SUITABILITY:
            cand["rejection_reason"] = "suitability_below_floor"
            print(
                f"[Sourcing] Rejected cached edge (suitability_below_floor): "
                f"{cand['vendor_name']} suitability={cand['suitability_score']:.1f}% "
                f"< {TIER_SURFACE_MIN_SUITABILITY:.0f}% floor"
            )
            continue
        # T2 — cache type-gate: drop a confirmed-different-type cached edge. The
        # floor above catches below-score edges; this catches wrong-PART-TYPE
        # edges that score well (the :987-990 known-gap, now closed).
        if request_noun_class is not None:
            r_cls = classify_result_noun_class_dominant(
                cand["vendor_name"], None, "", cand.get("source_url") or "")
            if r_cls is not None and r_cls != request_noun_class:
                print(
                    f"[Sourcing] Rejected cached edge (type_gate): "
                    f"{cand['vendor_name']} result_class={r_cls} "
                    f"!= request_class={request_noun_class}"
                )
                continue
        t = e.get("tier") if e.get("tier") in (1, 2, 3) else (3 if price is None else 2)
        tiers[t].append(cand)

    def _t(n: int) -> dict:
        return {"results": tiers[n], "count": len(tiers[n]), "status": "ok"}

    return {
        "tier_1": _t(1), "tier_2": _t(2), "tier_3": _t(3),
        "warranty_banner": None, "urgency_applied": "cached",
        "filters_applied": ["known_parts_cache"], "tier3_capability_pivot": False,
    }


def _seed_candidates_into_result(result: dict, seed_edges: list,
                                 request_noun_class: Optional[str],
                                 searched_pn: Optional[str]) -> dict:
    """RANKING_BANDS_V1 seed merge (design correction, spec §6 primary rule):
    the known_parts cache ACCELERATES discovery, it never replaces it. Fresh
    Tier-2/3 discovery has already run; TTL-fresh edges now join TODAY'S
    candidate pool so a known vendor missing from today's search still surfaces
    (and still gets the RFQ), then ONE band pass runs over the union.

    Merge rules:
      - Dedupe by normalized source_url domain (name fallback): a vendor found
        fresh today stays ONE candidate — the fresh result wins volatile fields
        (price, lead, scores); the edge contributes its stored evidence
        (found_pn / match_type) only where today's listing showed none.
      - A seed-only edge is reconstructed like the legacy replay candidate and
        appended to its stored tier, marked seeded_from_cache for audit.
      - The legacy suitability floor is ANNOTATED, never a drop — the band
        pass (rescope_floor) re-scopes it exactly as fresh discovery does.
      - Type gate (Fix-B1 protection): a seed whose vendor+url class is
        confirmed DIFFERENT from the request's is dropped — genuine
        cross-class junk must not surface via the cache.
      - Stored rejection_reason edges never seed (unchanged Fix-B1 rule).

    Fail-soft at the caller; this function only mutates + returns `result`.
    Flag-off runs never reach here (their replay path is _result_from_cached_edges)."""
    from utils.sourcing_archieved.constants import TIER_SURFACE_MIN_SUITABILITY
    from utils.sourcing_archieved.scoring import classify_result_noun_class_dominant
    from utils.supplier_registry import _normalize_domain

    def _key(url: Optional[str], name: Optional[str]) -> str:
        dom = _normalize_domain(url) if url else ""
        return dom or (name or "").strip().lower()

    existing: dict = {}
    for tier_key in ("tier_1", "tier_2", "tier_3"):
        for c in (result.get(tier_key) or {}).get("results") or []:
            k = _key(c.get("source_url"), c.get("vendor_name"))
            if k:
                existing.setdefault(k, c)

    seeded = 0
    for e in seed_edges:
        if e.get("rejection_reason"):
            continue
        name = e.get("display_name") or e.get("supplier_id")
        key = _key(e.get("source_url"), name)
        if not key:
            continue
        mt = e.get("match_type") or "Functional Alternative"
        pn_status = ("exact_match" if mt == "Exact OEM"
                     else "partial_match" if mt == "Aftermarket Compatible"
                     else "not_visible")
        if key in existing:
            # Vendor also found fresh today: fresh fields win; the edge only
            # contributes PN evidence today's listing didn't show.
            fresh = existing[key]
            if (e.get("found_pn") or "").strip() and \
                    not (fresh.get("found_part_number") or "").strip():
                fresh["found_part_number"] = e.get("found_pn")
                if e.get("pn_source"):
                    fresh["pn_source"] = e["pn_source"]  # F2: URL-PN cap survives the cache
                if fresh.get("pn_match_status") in (None, "", "not_visible"):
                    fresh["pn_match_status"] = pn_status
                if not fresh.get("match_type"):
                    fresh["match_type"] = mt
            continue
        # Type gate on seed-only edges — the PN-evidence exception. The stored
        # class comes from vendor+url ONLY (the weakest signal; the fresh path
        # scores with full snippet/title context and never hard-drops — its
        # TypeGate is multiplicative). Observed live: Springer Pumps classed
        # PUMP at write (a seal kit listed on a pump-parts page) while carrying
        # found_pn=84004-28SP — the requested family. Rule: an edge WITH PN
        # evidence reaches banding regardless of stored class (annotate, don't
        # remove — banding's evidence rules decide); an edge with NO PN
        # evidence and a confirmed class mismatch keeps the Fix-B1 drop
        # (genuine cross-class junk must not surface via the cache).
        has_pn_evidence = bool((e.get("found_pn") or "").strip())
        type_gate_note = None
        if request_noun_class is not None:
            r_cls = classify_result_noun_class_dominant(
                name, None, "", e.get("source_url") or "")
            if r_cls is not None and r_cls != request_noun_class:
                if not has_pn_evidence:
                    print(f"[Sourcing] Seed edge dropped (type_gate): {name} "
                          f"result_class={r_cls} != request_class={request_noun_class}")
                    continue
                type_gate_note = "class_mismatch_pn_evidence"
                print(f"[Sourcing] Seed edge kept despite class mismatch "
                      f"(PN evidence {e.get('found_pn')}): {name} "
                      f"result_class={r_cls} != request_class={request_noun_class}")
        price = e.get("price")
        cand = {
            "vendor_name":       name,
            "base_price":        price,
            "price_tbd":         price is None,
            "requires_rfq":      e.get("purchase_channel") == "rfq",
            "source_url":        e.get("source_url"),
            "match_type":        mt,
            "pn_match_status":   pn_status,
            "found_part_number": e.get("found_pn"),
            "suitability_score": e.get("suitability") or 0,
            "confidence_score":  1.0 if e.get("price_stale") else 0.0,
            "lead_time_days":    e.get("lead_days") or 5,
            "from_cache":        True,
            "seeded_from_cache": True,
            "price_stale":       bool(e.get("price_stale")),
        }
        if e.get("pn_source"):
            cand["pn_source"] = e["pn_source"]  # F2: URL-PN cap survives the cache
        if type_gate_note:
            cand["type_gate_note"] = type_gate_note  # audit: why the gate stood down
        if float(cand["suitability_score"] or 0.0) < TIER_SURFACE_MIN_SUITABILITY:
            # Annotate-don't-remove: the band pass re-scopes the floor verdict
            # (Band A / C / B-with-PN-evidence cleared; evidence-less Band B
            # keeps it and stays off the UI) — same rule as fresh discovery.
            cand["rejection_reason"] = "suitability_below_floor"
            print(f"[Sourcing] Floored seed edge (band rescope pending): {name} "
                  f"suitability={float(cand['suitability_score']):.1f}% "
                  f"< {TIER_SURFACE_MIN_SUITABILITY:.0f}% floor")
        t = e.get("tier") if e.get("tier") in (1, 2, 3) else (3 if price is None else 2)
        tier = result.setdefault(f"tier_{t}", {"results": [], "count": 0})
        tier.setdefault("results", []).append(cand)
        existing[key] = cand
        seeded += 1

    for tier_key in ("tier_1", "tier_2", "tier_3"):
        tier = result.get(tier_key) or {}
        if "results" in tier:
            tier["count"] = len(tier["results"])

    # ONE band pass over the union (idempotent re-annotation — the same pass
    # the Step-3 tier-1 re-derive fix re-applies). Keyed off the result's own
    # marker so an un-banded (band-pass-failed) result stays legacy-shaped.
    if "ranking_bands:v1" in (result.get("filters_applied") or []):
        from utils.procurement_agent.ranking_bands import apply_ranking_bands
        apply_ranking_bands(result, searched_pn)
    print(f"[Sourcing] Seed merge: {seeded} cached edge(s) joined fresh discovery")
    return result


def _run_sourcing_background(
    run_id: str,
    specs_dict: dict,
    urgency_factor: float,
    warranty_status: str,
) -> None:
    """Run SourcingAgent then ComparisonAgent in a single background thread.

    Cache-first: a previously-seen part returns its remembered supplier set from
    known_parts (deterministic, no web discovery). Otherwise discovery runs and the
    candidate set is written back so the NEXT request for this part is consistent.

    Order of operations is load-bearing:
    0. known_parts cache-first read → if HIT, reconstruct results, skip discovery.
    1. (miss) SourcingAgent runs → raw results, phase stays SOURCING (polling active).
    2. (miss) ComparisonAgent runs in parallel over all candidates (phase still SOURCING).
       Then the candidate set is written back to known_parts.
    3. Persist results, advance phase to COMPARISON, commit (both paths).
    """
    import logging
    from concurrent.futures import ThreadPoolExecutor
    log = logging.getLogger(__name__)

    result = None

    # ── Step 0: known_parts cache read ───────────────────────────────────────
    # Canonical part-key (aliases + PN-normalize) so the cache doesn't fork on
    # "Gusher" vs "Gusher Pumps". exact_only runs bypass the cache (filtered set).
    #
    # RANKING_BANDS_V1 — DESIGN CORRECTION (spec §6 primary rule): the cache
    # ACCELERATES discovery, it never REPLACES it. Fresh Tier-2/3 discovery runs
    # on EVERY flag-on run — no cache short-circuit. TTL-fresh, current-version
    # edges are collected as SEEDS and merged into the fresh candidate pool
    # AFTER discovery (_seed_candidates_into_result); the TTL now governs seed
    # RELEVANCE, not replay rights (an expired edge simply stops seeding).
    # Within-TTL replay-only runs served a thinner page (4-5 vs 15+ candidates),
    # could never surface newly-listed vendors, and let cached classification
    # mistakes veto findings with no fresh evidence to correct them.
    # Flag OFF: the legacy cache-first replay below is byte-identical.
    part_key = ""
    seed_edges: list = []
    seed_req_cls = None
    try:
        from utils import known_parts
        from utils.sourcing_archieved.scoring import _query_noun_class
        part_key = known_parts.canonical_part_key(
            specs_dict.get("manufacturer"), specs_dict.get("part_number"))
        if part_key and not specs_dict.get("exact_only"):
            edges = known_parts.get_edges(part_key)
            from utils.procurement_agent.ranking_bands import ranking_bands_active
            if ranking_bands_active():
                seed_edges = [e for e in edges if not e.get("edge_stale")]
                if seed_edges:
                    # T2 — the request's noun-class, threaded into the seed
                    # merge's type gate. Built directly off AssetSpecs fields
                    # (not SourcingAgent._dict_to_specs) so the seed path does
                    # not depend on the SourcingAgent class — which tests mock.
                    try:
                        seed_req_cls = _query_noun_class(_specs_from_dict(specs_dict))
                    except Exception:
                        seed_req_cls = None
                    log.info("[%s] known_parts: %d TTL-fresh edge(s) will seed the "
                             "discovery merge (discovery still runs)",
                             run_id, len(seed_edges))
            elif edges:
                # Legacy flag-off cache-first replay — byte-identical behavior.
                req_cls = None
                try:
                    req_cls = _query_noun_class(_specs_from_dict(specs_dict))
                except Exception:
                    req_cls = None
                result = _result_from_cached_edges(edges, request_noun_class=req_cls)
                log.info("[%s] known_parts cache HIT (%d suppliers) — skipping discovery", run_id, len(edges))
    except Exception as exc:
        log.warning("[%s] known_parts cache read failed: %s", run_id, exc)
        result = None
        seed_edges = []

    if result is None:
        # ── Step 1: Sourcing (discovery) ─────────────────────────────────────
        try:
            from utils.procurement_agent.agents.sourcing_agent import SourcingAgent
            from utils.models import SourcingRun as _SourcingRunModel
            # Under DEMO_MODE force Apollo OFF at construction: pass apollo_api_key=""
            # (NOT None — None falls through to the env read inside ApolloClient and
            # would re-enable Apollo if APOLLO_API_KEY were in the env). "" is `not None`,
            # so it overrides the env read and ApolloClient.enabled is False. The boot
            # assertion (part a) refuses to start with APOLLO_API_KEY set at all; this is
            # the suspenders — even if that env somehow leaks in, this path can't spend.
            # When DEMO_MODE is off, construct exactly as today (None -> env read, unchanged).
            agent = SourcingAgent(
                tavily_api_key=os.environ.get("TAVILY_API_KEY"),
                anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
                apollo_api_key="" if DEMO_MODE else None,
            )
            run_model = _SourcingRunModel(
                id=run_id,
                current_phase="sourcing",
                asset_specs_json=specs_dict,
                urgency_factor=urgency_factor,
                warranty_status=warranty_status,
            )
            result = agent.run(run_model)
            # Exact-only mode (no-spec-sheet honesty branch): drop aftermarket/equivalent
            # Tier 2/3 candidates, leaving only exact OEM matches (Tier 1 untouched).
            if specs_dict.get("exact_only"):
                from utils.sourcing_filter import apply_exact_only
                result = apply_exact_only(result)
        except Exception as exc:
            log.error("[%s] Sourcing failed: %s", run_id, exc)
            with _SessionFactory() as session:
                orm = session.get(SourcingRunORM, run_id)
                if orm:
                    orm.sourcing_results_json = json.dumps({
                        "error": str(exc),
                        "tier_1": {"results": [], "count": 0, "status": "error"},
                        "tier_2": {"results": [], "count": 0, "status": "error"},
                        "tier_3": {"results": [], "count": 0, "status": "error"},
                    })
                    # Surface the failure as an explicit terminal-ish state so the run
                    # is not stranded at "sourcing" (where the frontend polls forever).
                    orm.current_phase = Phase.ERROR.value
                    orm.updated_at = datetime.now(timezone.utc)
                    session.commit()
            return

        # ── Step 2: Comparison (parallel, phase stays SOURCING) ──────────────
        try:
            from utils.procurement_agent.agents.spec_comparison_agent import SpecComparisonAgent
            comp_agent = SpecComparisonAgent(
                anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY")
            )
            work = [
                (candidate, tier_num)
                for tier_key, tier_num in [("tier_1", 1), ("tier_2", 2), ("tier_3", 3)]
                for candidate in result.get(tier_key, {}).get("results", [])
            ]

            def _compare_one(args: tuple) -> tuple:
                candidate, tier_num = args
                try:
                    return candidate, comp_agent.run(run_model, candidate, tier=tier_num)
                except Exception as cexc:
                    log.warning("[%s] Comparison skipped for %s: %s",
                                run_id, candidate.get("vendor_name"), cexc)
                    return candidate, None

            with ThreadPoolExecutor(max_workers=5) as pool:
                for candidate, artifact in pool.map(_compare_one, work):
                    candidate["comparison_artifact"] = artifact

            log.info("[%s] Comparison artifacts attached (%d candidates)", run_id, len(work))
        except Exception as exc:
            log.error("[%s] Comparison step failed: %s", run_id, exc)
            # Non-fatal: advance to COMPARISON with artifacts absent rather than stalling.

        # ── Write-back: remember this part's suppliers so the next request reads the
        # cache (consistent, no re-roll). Skip exact_only (a filtered set would poison
        # the part's cache). Durable supplier edges; volatile prices via the store. ──
        try:
            if part_key and not specs_dict.get("exact_only"):
                from utils import known_parts
                cands = [
                    {**o, "tier": n}
                    for tier_key, n in (("tier_1", 1), ("tier_2", 2), ("tier_3", 3))
                    for o in result.get(tier_key, {}).get("results", [])
                    if not o.get("rejection_reason")
                ]
                # Night 5 (I4): registry-backed Tier 1 candidates are computed fresh
                # per run from the supplier registry and MUST NOT be cached into
                # known_parts — a registry card cached then served from the cache-
                # first path would lose its relationship/discard provenance and could
                # be served for a request the registry no longer matches (the poison-
                # bug class). The matcher marks these with is_registry_backed=True;
                # drop them from the write-back so only Tier 2/3 discovery edges (and
                # any legacy non-registry Tier 1) are cached. Registry Tier 1 is re-
                # derived on every run (cheap local lookup), so nothing is lost.
                cands = [c for c in cands
                         if not (c.get("tier") == 1 and c.get("is_registry_backed"))]
                written = known_parts.upsert_edges(part_key, cands)
                log.info("[%s] known_parts write-back: %d supplier edge(s) for %r", run_id, written, part_key)
        except Exception as exc:
            log.warning("[%s] known_parts write-back failed: %s", run_id, exc)

        # ── Seed merge (RANKING_BANDS_V1 design correction) ──────────────────
        # TTL-fresh cached edges join TODAY'S discovery pool (dedupe by domain,
        # fresh wins, one band pass over the union) — a known vendor missing
        # from today's search still surfaces and still gets the RFQ. Runs AFTER
        # the write-back so seed-only edges never self-refresh their own TTL.
        # seed_edges is only ever non-empty on flag-on runs. Fail-soft: a merge
        # error keeps the fresh-only result.
        if seed_edges:
            try:
                result = _seed_candidates_into_result(
                    result, seed_edges, seed_req_cls,
                    specs_dict.get("part_number"))
            except Exception as exc:
                log.warning("[%s] seed merge failed (fresh-only result kept): %s",
                            run_id, exc)

    # ── Step 3: Persist final results and advance phase (both paths) ─────────
    # Night 5 (I4) — registry-backed Tier 1 is re-derived FRESH per run, on BOTH
    # the cache-hit and discovery paths. The cache-first path bypasses SourcingAgent
    # (so _run_tier1's matcher never runs on a hit), and a cached Tier 2/3 edge set
    # must NOT crowd out the onboarded-supplier relationship lane. So: when TIER1_V2
    # is on, run the matcher here and set result["tier_1"] to its candidates — the
    # matcher is the single source of truth for Tier 1, its ordering is the
    # relationship-aware one, and registry Tier 1 never enters staleness (it is not
    # read from nor written to known_parts). Fail-soft: a matcher error leaves the
    # existing tier_1 (cache or discovery) untouched.
    _tier1_matches = []
    try:
        if tier1_matcher.tier1_v2_active():
            _tier1_matches = tier1_matcher.match_tier1(
                detected_type=specs_dict.get("detected_type"),
                manufacturer=specs_dict.get("manufacturer") or "Unknown",
                description=specs_dict.get("description"),
                model=specs_dict.get("model"),
            )
            if _tier1_matches:
                result["tier_1"] = {
                    "results": tier1_matcher.candidates_from_matches(
                        _tier1_matches,
                        manufacturer=specs_dict.get("manufacturer") or "Unknown",
                        part_number=specs_dict.get("part_number") or "",
                    ),
                    "count": len(_tier1_matches), "status": "ok",
                }
            else:
                result["tier_1"] = {"results": [], "count": 0, "status": "ok"}
    except Exception as exc:
        log.warning("[%s] Tier 1 fresh re-derive failed (keeping %s tier_1): %s",
                    run_id, "cached" if result is not None else "discovered", exc)

    # RANKING_BANDS_V1 × TIER1_V2 ordering fix: the fresh Tier-1 re-derive above
    # REPLACES result["tier_1"] AFTER the band pass already ran (SourcingAgent on
    # the discovery path, the cache-hit post-pass above), so the re-derived
    # candidates carried no band and vanished from findings/outreachTargets/the
    # T4 promotion loop at read time (observed live: DXP, the onboarded Band-C
    # class match). Re-apply the band pass when the result carries the marker —
    # annotate/floor/cap/sort are pure and deterministic, so re-running over the
    # already-banded Tier 2/3 is a no-op; only the fresh Tier 1 changes. Keyed
    # off the result's own marker (not the live env) and fail-soft, same as the
    # read-time transform.
    if "ranking_bands:v1" in (result.get("filters_applied") or []):
        try:
            from utils.procurement_agent.ranking_bands import apply_ranking_bands
            apply_ranking_bands(result, specs_dict.get("part_number"))
        except Exception as exc:
            log.warning("[%s] tier-1 band re-annotation failed (unbanded tier_1 kept): %s",
                        run_id, exc)

    with _SessionFactory() as session:
        orm = session.get(SourcingRunORM, run_id)
        if orm and orm.current_phase == Phase.SOURCING.value:
            orm.sourcing_results_json = json.dumps(result)
            orm.current_phase = Phase.COMPARISON.value
            orm.updated_at = datetime.now(timezone.utc)
            session.commit()
    # Night 1 — capture the sourcing result set (RUN_CAPTURE-gated, fail-soft).
    # Captured HERE (not at the deep `[Sourcing]` print sites in
    # enterprise_search.py) because run_id is in scope only at this boundary —
    # the per-tier query builders have no run_id (I2 EXPECTED, confirmed).
    # query_issued captures the per-tier INTENT derived from specs (the literal
    # provider query string is built deeper and is a flagged not-captured gap).
    try:
        _mfr = specs_dict.get("manufacturer") or ""
        _model = specs_dict.get("model") or ""
        _pn = specs_dict.get("part_number") or ""
        _query_intent = " ".join(p for p in (_mfr, _model, _pn) if p).strip() or "(spec-based)"
        for _tier_key, _tier_n in (("tier_1", 1), ("tier_2", 2), ("tier_3", 3)):
            _run_capture.capture_query(run_id, _tier_n, _query_intent, part_key=part_key or None)
            for _idx, _cand in enumerate(result.get(_tier_key, {}).get("results", [])):
                _cand_id = f"{_cand.get('vendor_name', '')}-t{_tier_n}-{_idx}"
                # Capture EVERY candidate — scored OR rejected — so the flywheel
                # sees the full verdict set (the rejected ones carry the gate's
                # rejection_reason: the "why this was cut" signal).
                _run_capture.capture_candidate(run_id, _tier_n, {**_cand, "candidate_id": _cand_id, "tier": _tier_n})
        _displayed = [
            {**_c, "candidate_id": f"{_c.get('vendor_name', '')}-t{_n}-{_i}", "tier": _n}
            for _tk, _n in (("tier_1", 1), ("tier_2", 2), ("tier_3", 3))
            for _i, _c in enumerate(result.get(_tk, {}).get("results", []))
            if not _c.get("rejection_reason")
        ]
        _run_capture.capture_results_displayed(run_id, _displayed)
    except Exception:
        # Capture must never break sourcing write-back; run_capture counts its own failures.
        pass

    # Night 5 (T3) — Tier 1 notify≫display asymmetry. The matches were re-derived
    # above (fresh per run, I4); fire the notify layer over them. The notify gate
    # (brand-match-or-core-class) + per-RFQ cap are applied; events are recorded
    # behind the stubbed/flagged EmailSender (double-gate: EMAIL_SEND_ENABLED
    # defaults OFF + TIER1_V2). Gated by TIER1_V2; fail-soft (a notify error is
    # recorded, never breaks sourcing). The notify set matches the displayed Tier 1
    # set exactly (same _tier1_matches).
    try:
        if _tier1_matches:
            tier1_notify.notify_tier1(_tier1_matches, run_id=run_id)
    except Exception as exc:
        log.warning("[%s] Tier 1 notify failed (fail-soft, sourcing unaffected): %s",
                    run_id, exc)
    log.info("[%s] Sourcing complete → comparison", run_id)


# ---------------------------------------------------------------------------
# Helper: urgency_factor → display label
# ---------------------------------------------------------------------------

def _urgency_label(factor: float) -> str:
    if factor >= 0.9:
        return "Emergency"
    if factor >= 0.5:
        return "Predictive"
    return "Stocking"


def _warranty_label(status: str) -> str:
    mapping = {"active": "Active", "expired": "Expired", "unknown": "Unknown"}
    return mapping.get(status.lower(), "Unknown")


def _orm_to_list_item(run: SourcingRunORM) -> RunListItem:
    specs = json.loads(run.asset_specs_json) if run.asset_specs_json else {}
    asset_summary = None
    if specs:
        mfr = specs.get("manufacturer", "")
        model = specs.get("model", "")
        if mfr or model:
            asset_summary = f"{mfr} {model}".strip()

    handoff = json.loads(run.maintenance_handoff_json) if run.maintenance_handoff_json else {}

    return RunListItem(
        id=run.id,
        phase=run.current_phase,
        urgency=_urgency_label(run.urgency_factor),
        warranty=_warranty_label(run.warranty_status),
        facility_id=run.facility_id,
        group_id=getattr(run, "group_id", None),
        asset_summary=asset_summary,
        amount=None,
        maintenance_submission_id=handoff.get("submission_id"),
        created_at=run.initiated_at.isoformat() if run.initiated_at else "",
        updated_at=run.updated_at.isoformat() if run.updated_at else "",
    )


def _rfq_draft_status_for_run(run_id: str) -> dict:
    """Real per-candidate outreach signal from rfq_drafts: {candidate_id: status}. Newest draft
    per candidate wins (list_drafts is newest-first). A 'sent' status already means a genuine
    send went (A2 claim-matches-reality), so it is dispatch-truth — no synthetic stamp needed.
    Read-only, fail-soft (returns {} on any error). Additive: leaves tier3_outreach_sent intact."""
    import logging
    from utils.procurement_agent.state import persistence
    out: dict = {}
    try:
        for d in persistence.list_drafts(run_id):   # newest-first
            cid = d.get("candidate_id")
            if cid and cid not in out:               # first (newest) wins
                out[cid] = d.get("status")
    except Exception as exc:
        logging.getLogger(__name__).warning("[rfq] draft-status read failed for %s: %s", run_id, exc)
        return {}
    return out


def _redact_sourcing_error(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Replace a failed-sourcing error stub with a client-safe shape.

    A failed sourcing run stores `{"error": str(exc), "tier_1": {...status:error}, ...}`
    (see _run_sourcing_background). The `str(exc)` can carry upstream API errors, request
    URLs, or internal detail that must not reach a client response — especially an
    unauthenticated demo visitor polling GET /api/runs/{id}. Return a generic message
    while preserving the empty-tier structure the frontend reads (it keys off
    `phase == error`, not this string, and reads tier1/tier2/tier3 arrays). The real
    `str(exc)` is retained in the stored column and the admin surface reads the raw row
    via persistence.get_run — so this redacts ONLY the customer response boundary."""
    return {
        "error": "Sourcing failed — please try again.",
        "tier_1": {"results": [], "count": 0, "status": "error"},
        "tier_2": {"results": [], "count": 0, "status": "error"},
        "tier_3": {"results": [], "count": 0, "status": "error"},
    }


def _orm_to_detail(run: SourcingRunORM) -> RunDetail:
    def _parse(col): return json.loads(col) if col else None

    raw_sourcing = _parse(run.sourcing_results_json)
    # Transform if we have real sourcing data (not an error stub). State C (3b): overlay
    # any human-confirmed quotes for this run onto their candidates (deterministic thread
    # join, domain fallback) — the assembly step that has run_id in scope.
    sourcing: Optional[Dict[str, Any]] = None
    if raw_sourcing and "error" not in raw_sourcing:
        sourcing = _transform_sourcing_results(raw_sourcing, _build_quote_index(run.id))
    elif raw_sourcing:
        # A failed sourcing run stored a raw `str(exc)` in the error stub. That string can
        # carry upstream API errors / request URLs / internal detail, which must NOT reach a
        # client response (esp. an unauthenticated demo visitor). Redact at this response
        # boundary: return a generic message + the empty-tier shape the frontend expects,
        # while the real `str(exc)` is retained in the stored column (server-side / admin
        # surface reads the raw row via persistence.get_run, not this transform). Done
        # unconditionally — the frontend only keys off `phase == error`, never this string,
        # so redaction is harmless to the internal UI and better for prod too.
        sourcing = _redact_sourcing_error(raw_sourcing)

    # Derive no_exact_match: fires when T2+T3 combined have at least one candidate
    # but none have pnMatchLevel=="exact" (mapped from pn_match_status=="exact_match").
    # Suppressed when spec_based_sourcing=True or part_number is absent/null-equivalent
    # (spec-based and no-PN scenarios are "by design," not typo cases).
    _null_pn_vals = {"", "N/A", "n/a", "null", "None", "UNKNOWN-PN", "Unknown", "unknown"}
    _asset_specs = _parse(run.asset_specs_json)
    # Strip internal `_`-prefixed ledger keys (intake turn counter / asked-fields ledger) so
    # they never reach the frontend specs display. They ride on asset_specs_json by design
    # (the intake over-questioning fix's state vehicle — no separate column) but are not for
    # display. (spec_based_sourcing / classification_override etc. are NOT `_`-prefixed and
    # pass through unchanged.)
    if _asset_specs:
        _asset_specs = {k: v for k, v in _asset_specs.items() if not str(k).startswith("_")}
    _pn = ((_asset_specs or {}).get("part_number") or "").strip()
    _spec_based = (_asset_specs or {}).get("spec_based_sourcing", False)
    no_exact_match = False
    if sourcing and not _spec_based and _pn not in _null_pn_vals:
        _t2_t3 = sourcing.get("tier2", []) + sourcing.get("tier3", [])
        if _t2_t3:
            no_exact_match = not any(c.get("pnMatchLevel") == "exact" for c in _t2_t3)

    return RunDetail(
        id=run.id,
        phase=run.current_phase,
        urgency=_urgency_label(run.urgency_factor),
        warranty=_warranty_label(run.warranty_status),
        facility_id=run.facility_id,
        facility_state=_FACILITY_STATES.get(run.facility_id, "unknown"),
        group_id=getattr(run, "group_id", None),
        asset_specs=_asset_specs,
        inventory_result=_parse(run.inventory_result_json),
        sourcing_results=sourcing,
        selected_candidate=_parse(run.selected_candidate_json),
        approval_history=json.loads(run.approval_history_json)
            if isinstance(run.approval_history_json, str)
            else (run.approval_history_json or []),
        tier3_selection=json.loads(run.tier3_selection_json) if run.tier3_selection_json else None,
        tier3_outreach_sent=json.loads(run.tier3_outreach_sent_json) if run.tier3_outreach_sent_json else None,
        rfq_draft_status=_rfq_draft_status_for_run(run.id),
        maintenance_handoff=_parse(run.maintenance_handoff_json),
        no_exact_match=no_exact_match,
        created_at=run.initiated_at.isoformat() if run.initiated_at else "",
        updated_at=run.updated_at.isoformat() if run.updated_at else "",
    )


# ---------------------------------------------------------------------------
# Sourcing run endpoints
# ---------------------------------------------------------------------------

def _new_run_orm(
    *,
    facility_id: str,
    urgency_factor: float,
    warranty_status: str,
    company_id: Optional[str] = None,
    group_id: Optional[str] = None,
    asset_specs: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
) -> SourcingRunORM:
    """Build (do not persist) a fresh run at phase=intake — the SINGLE construction the
    create path uses. Shared by create_run (commits one) and _fan_out_intake (commits N in
    one transaction), so fan-out reuses the create path instead of reimplementing it.
    group_id is NULL for a single run, the shared basket label for a fanned one.
    asset_specs seeds the run's specs at birth (multi-part fan-out); None -> bare intake run.
    session_id is the DEMO_MODE per-visitor token (None when DEMO_MODE off or absent)."""
    now = datetime.now(timezone.utc)
    return SourcingRunORM(
        id=str(uuid.uuid4()),
        facility_id=facility_id,
        company_id=company_id,
        group_id=group_id,
        current_phase=Phase.INTAKE.value,
        urgency_factor=urgency_factor,
        warranty_status=warranty_status,
        asset_specs_json=json.dumps(asset_specs) if asset_specs else None,
        session_id=session_id,
        initiated_at=now,
        updated_at=now,
    )


@app.post("/api/runs", response_model=CreateRunResponse, status_code=201)
def create_run(
    body: CreateRunRequest,
    request: Request,
    caller: Optional[Caller] = Depends(get_caller),
):
    """Create a new sourcing run and return it in intake phase.

    D2 prereq #1 (keys only): stamp the run's tenant key (company PIN) from the verified
    Caller when one is present — NEVER from the body. No token (today's demo) -> NULL.

    DEMO_MODE session isolation: stamp the per-visitor X-Session-Id on the run at birth
    (required -> 422 if missing/malformed) so subsequent reads/writes can be scoped to
    this visitor (IDOR fix). Inert when DEMO_MODE is off: no session_id stamped.

    DEMO_MODE spend-abuse guard (FIX 3): a public no-login demo must not let a client
    birth a run already carrying asset_specs — that would let a script POST specs then
    confirm-intake straight into a full sourcing run (Tavily x6 + Anthropic + brand-intel
    + comparison LLMs) with NO intake LLM call and attacker-chosen specs, a cheaper-per-
    iteration path to the most expensive capability that bypasses the "describe in plain
    words" intake gate bounding what gets sourced. Under DEMO_MODE the seed is ignored
    (forced bare intake); group_id is still honored (it only labels runs for the basket
    rollup — no spend risk, and the demo's basket view needs it). Inert when DEMO_MODE is
    off: asset_specs is honored exactly as today (the real multi-part fan-out unbroken)."""
    demo_sid = _require_demo_session_id(request)   # "" when DEMO_MODE off (-> None below)
    # DEMO_MODE run-creation cap (bounds row spam + the funnel top). Inert when off.
    _demo_enforce_cap("runs_per_session", demo_sid, _DEMO_MAX_RUNS_PER_SESSION, "run creation")
    seeded_specs = body.asset_specs
    if DEMO_MODE:
        seeded_specs = None   # forced bare intake — the bypass can't skip the intake gate
    run = _new_run_orm(
        facility_id=body.facility_id,
        urgency_factor=body.urgency_factor,
        warranty_status=body.warranty_status,
        company_id=caller.company_id if caller else None,
        group_id=body.group_id,   # opt-in basket label; None -> group-less (legacy, unchanged)
        asset_specs=seeded_specs,   # opt-in seed; None -> bare intake run (legacy, unchanged)
        session_id=demo_sid or None,   # DEMO_MODE visitor token; None when off/absent
    )
    with _SessionFactory() as session:
        session.add(run)
        session.commit()
        session.refresh(run)
        return CreateRunResponse(
            id=run.id,
            phase=run.current_phase,
            created_at=run.initiated_at.isoformat(),
        )


@app.put("/api/runs/{run_id}/asset-specs")
def seed_asset_specs(run_id: str, body: AssetSpecsSeedRequest):
    """Write pre-extracted specs onto an EXISTING run — the post-birth equivalent of the
    createRun birth-seed (multi-part fan-out seeds part 1 onto the already-created run 0).

    Scoped to the seed purpose ONLY: it sets asset_specs and nothing else — no phase advance,
    no re-extraction, no sufficiency assessment. 404 if the run doesn't exist. SECURITY DEBT:
    client-supplied + unvalidated, same posture as the birth-seed — see CLEANUP §4.1."""
    with _SessionFactory() as session:
        orm = session.get(SourcingRunORM, run_id)
        if not orm:
            raise HTTPException(status_code=404, detail="Run not found")
        orm.asset_specs_json = json.dumps(body.asset_specs)
        orm.updated_at = datetime.now(timezone.utc)
        session.commit()
    return {"run_id": run_id, "asset_specs": body.asset_specs}


# ---------------------------------------------------------------------------
# Intake routing front door (multi-part Increment 1, Stage 2)
#
# A deterministic gate that decides whether a request fans out. It routes on a PROVIDED
# part count only — it does NOT parse/extract parts (intake yields one asset_specs per run
# today). One part takes the existing single-run path, unchanged; two or more take the
# fan-out path (Stage 3). This stage establishes the branch + the seam, nothing more.
# ---------------------------------------------------------------------------

def route_intake(part_count: int) -> str:
    """Single-vs-multi routing decision. Deterministic — no LLM, no randomness:
    <= 1 part -> 'single' (the existing path); >= 2 -> 'multi' (fan-out, Stage 3)."""
    return "multi" if part_count >= 2 else "single"


def _fan_out_intake(body: IntakeRequest, caller: Optional[Caller]) -> Dict[str, Any]:
    """Fan a >=2-part request into N independent single-part runs under ONE shared group_id.

    Each run is constructed EXACTLY as the single create_run does (same bare intake shell;
    part contents are filled later via intake chat per run), plus the group_id label. The
    runs are joined ONLY by that label: distinct run_ids, distinct rows, no FK between
    siblings, no shared mutable state, no run-spanning lock — each advances on its own
    run_id-scoped guards after birth.

    Partial-failure policy: ALL-OR-NOTHING. The N runs are built then committed in ONE
    transaction; if any insert fails the whole batch rolls back (zero runs persisted). For a
    money-adjacent basket a half-created request — some lines sourced, others silently
    dropped — is a correctness hazard; a clean failure the caller can retry is safer than an
    ambiguous partial basket. (Atomic birth only — it adds no shared runtime state; once
    committed the runs are fully independent.)"""
    group_id = str(uuid.uuid4())
    company_id = caller.company_id if caller else None
    runs = [
        _new_run_orm(
            facility_id=body.facility_id,
            urgency_factor=body.urgency_factor,
            warranty_status=body.warranty_status,
            company_id=company_id,
            group_id=group_id,
        )
        for _ in body.parts
    ]
    run_ids = [r.id for r in runs]  # client-assigned UUIDs — safe to read pre-commit
    with _SessionFactory() as session:
        session.add_all(runs)
        session.commit()  # single atomic commit — all N or none
    return {"group_id": group_id, "run_ids": run_ids}


@app.post("/api/requests", status_code=201)
def create_request(
    body: IntakeRequest,
    request: Request,
    caller: Optional[Caller] = Depends(get_caller),
):
    """Intake front door: route a request to the single-run path (<=1 part) or the fan-out
    path (>=2 parts). The single branch DELEGATES to the unchanged create_run — byte-for-byte
    the existing path; part contents (if any) are filled via intake chat exactly as today.
    The multi branch fans out into N grouped independent runs (one shared group_id)."""
    if route_intake(len(body.parts)) == "multi":
        return _fan_out_intake(body, caller)
    return create_run(
        CreateRunRequest(
            facility_id=body.facility_id,
            urgency_factor=body.urgency_factor,
            warranty_status=body.warranty_status,
        ),
        request,
        caller,
    )


@app.get("/api/runs", response_model=List[RunListItem])
def list_runs(
    facility_id: Optional[str] = None,
    phase: Optional[str] = None,
    group_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """List sourcing runs with optional filtering. `group_id` returns only the runs in one
    basket; absent, the result is unchanged (no filtering, same ordering)."""
    with _SessionFactory() as session:
        q = session.query(SourcingRunORM)
        if facility_id:
            q = q.filter(SourcingRunORM.facility_id == facility_id)
        if phase:
            q = q.filter(SourcingRunORM.current_phase == phase)
        if group_id:
            q = q.filter(SourcingRunORM.group_id == group_id)
        runs = q.order_by(SourcingRunORM.initiated_at.desc()).offset(offset).limit(limit).all()
        return [_orm_to_list_item(r) for r in runs]


@app.post("/api/runs/from-maintenance", status_code=201)
def create_run_from_maintenance(body: MaintenanceSubmission, caller: Optional[Caller] = Depends(get_caller)):
    """Create a sourcing run in pending_intake from a maintenance handoff payload.

    D2 prereq #1 (keys only): the tenant key (company PIN) comes from the validated
    X-Arkim-CompanyId via the Caller (core forwards the user JWT + company + service
    signature) — NEVER from the body's facility_id. Header absent (today) -> NULL."""
    now = datetime.now(timezone.utc)
    urgency_factor = _URGENCY_FACTORS.get(body.context.urgency, 0.3)
    handoff_dict = body.model_dump()
    run = SourcingRunORM(
        id=str(uuid.uuid4()),
        facility_id=body.facility_id,
        company_id=caller.company_id if caller else None,
        current_phase=Phase.PENDING_INTAKE.value,
        urgency_factor=urgency_factor,
        warranty_status="unknown",
        asset_specs_json=json.dumps(body.asset_specs) if body.asset_specs else None,
        maintenance_handoff_json=json.dumps(handoff_dict),
        approval_history_json="[]",
        initiated_at=now,
        updated_at=now,
    )
    with _SessionFactory() as session:
        session.add(run)
        session.commit()
        run_id = run.id
    return {
        "run_id": run_id,
        "phase": Phase.PENDING_INTAKE.value,
        "maintenance_submission_id": body.submission_id,
    }


@app.get("/api/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str, request: Request):
    """Fetch full run state by ID.

    DEMO_MODE session isolation: under DEMO_MODE the run is returned ONLY to the
    visitor whose X-Session-Id matches the run's stamped session_id. A mismatched/
    missing session (or a NULL-session row like a seeded maintenance run) returns
    404 — NOT 403 — so not-owned is indistinguishable from not-found (no existence
    oracle). Inert when DEMO_MODE is off (today's behaviour, no scoping)."""
    demo_sid = _demo_session_id_from_request(request)
    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run or not _demo_run_owned_by_session(run, demo_sid):
            raise HTTPException(status_code=404, detail="Run not found")
        detail = _orm_to_detail(run)
        detail.messages = _messages.get(run_id, [])
        return detail


@app.post("/api/runs/{run_id}/open-from-pending")
def open_from_pending(run_id: str):
    """Transition pending_intake → intake and seed chat with the maintenance summary."""
    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        if run.current_phase != Phase.PENDING_INTAKE.value:
            raise HTTPException(
                status_code=409,
                detail={"message": "Run is not in pending_intake phase", "current_phase": run.current_phase},
            )
        handoff = json.loads(run.maintenance_handoff_json) if run.maintenance_handoff_json else {}
        summary = handoff.get("context", {}).get("chat_thread_summary")
        run.current_phase = Phase.INTAKE.value
        run.updated_at = datetime.now(timezone.utc)
        session.commit()

    if summary:
        _messages[run_id] = [{
            "id": str(uuid.uuid4()),
            "role": "agent",
            "content": summary,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }]
    return {"run_id": run_id, "phase": Phase.INTAKE.value}


@app.post("/api/runs/{run_id}/reject-submission")
def reject_submission(run_id: str):
    """Transition pending_intake → cancelled (maintenance handoff declined)."""
    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        if run.current_phase != Phase.PENDING_INTAKE.value:
            raise HTTPException(
                status_code=409,
                detail={"message": "Run is not in pending_intake phase", "current_phase": run.current_phase},
            )
        run.current_phase = Phase.CANCELLED.value
        run.updated_at = datetime.now(timezone.utc)
        session.commit()
    return {"run_id": run_id, "phase": Phase.CANCELLED.value}


@app.post("/api/runs/{run_id}/messages", response_model=SendMessageResponse)
def send_message(run_id: str, body: SendMessageRequest, request: Request):
    """
    Send a chat message to the live IntakeAgent.
    Extracts specs, updates asset_specs_json on the run, and returns the
    agent's clarification question (or a transition message when sufficient).

    DEMO_MODE session isolation: scoped to the run's owner (404 on mismatch/missing/
    NULL-session — not 403). Inert when DEMO_MODE is off.
    """
    demo_sid = _demo_session_id_from_request(request)
    with _SessionFactory() as session:
        orm = session.get(SourcingRunORM, run_id)
        if not orm or not _demo_run_owned_by_session(orm, demo_sid):
            raise HTTPException(status_code=404, detail="Run not found")
        current_phase = orm.current_phase
        prior_specs: Dict[str, Any] = (
            json.loads(orm.asset_specs_json) if orm.asset_specs_json else {}
        )

    now = datetime.now(timezone.utc).isoformat()

    # Persist user message
    thread = _messages.setdefault(run_id, [])
    thread.append({"id": str(uuid.uuid4()), "role": "user",
                   "content": body.content, "created_at": now})
    # Night 1 — capture the user turn (RUN_CAPTURE-gated, fail-soft, no-op when off).
    # The user/agent turn is NOT durably persisted anywhere today (in-memory
    # `_messages`); this is the first durable home (I1 gap). Stores text as-is
    # (no redaction pipeline exists — I3 default; flagged PII surface).
    _run_capture.capture_turn(run_id, "user", body.content)

    # Find the last agent clarification question for context
    prior_question: Optional[str] = None
    for msg in reversed(thread[:-1]):
        if msg["role"] == "agent":
            prior_question = msg["content"]
            break

    # Run IntakeAgent
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    # NOTE: do NOT log visitor free text (body.content) to stdout — it may contain
    # PII (names, facility addresses, real part numbers) and a public no-login demo
    # has no consent gate. The api_key_present / sufficiency / confidence signals are
    # also dropped here; structured interaction capture + a deliberate PII policy are
    # a separate (deferred) pass — for now the visitor's text simply never reaches the
    # logs from this path. See CLEANUP / the demo data-capture plan.
    agent = IntakeAgent(anthropic_api_key=api_key)
    run_obj = SourcingRun(
        id=run_id,
        current_phase=current_phase,
        asset_specs_json=prior_specs,
    )
    try:
        result = agent.run(run_obj, {
            "text": body.content,
            "prior_question": prior_question,
        })
    except Exception:
        # Surface the failure honestly as a 502 rather than masking it as a
        # successful 200 with a synthetic agent reply. The frontend's send-message
        # mutation already handles a non-2xx via its onError path (restores the
        # draft + "Message failed" toast). Broad catch is intentional: any agent /
        # upstream (Anthropic) failure maps to a Bad Gateway.
        traceback.print_exc()
        raise HTTPException(
            status_code=502,
            detail="Intake processing failed — please retry.",
        )

    # Determine reply — do NOT auto-advance on sufficient=True.
    # The confirm-intake endpoint owns the intake → sourcing transition.
    proceed_state = result.get("confidence_summary", {}).get("proceed_state", "")
    if result["sufficient"]:
        _null_vals = {"", "N/A", "n/a", "null", "None", "UNKNOWN-PN", "Unknown", "unknown", None}
        _specs_dict = result.get("asset_specs") or {}
        _has_model = _specs_dict.get("model") not in _null_vals
        _has_pn = _specs_dict.get("part_number") not in _null_vals
        _mfg_c = float(result.get("manufacturer_confidence") or 0)
        _part_c = float(result.get("part_id_confidence") or 0)
        # Guard: only emit manufacturer caveat when mfg confidence is genuinely low.
        # If both dimensions are above threshold and data is present, fall through to
        # the full-confidence message — avoids contradictory caveat on high-conf extractions.
        _emit_caveat = (
            proceed_state == "proceed_with_manufacturer_caveat"
            and not (_mfg_c >= 70 and _part_c >= 70 and _has_model and _has_pn)
        )
        if result.get("commit_message"):
            # Intake cap / nothing-left-to-ask commit (IntakeAgent set spec_based_sourcing
            # on the specs). Routes through the spec-based path but with the honest commit
            # message that flags what could not be confirmed.
            reply_text = result["commit_message"]
        elif _emit_caveat:
            reply_text = (
                "Specs extracted but the manufacturer could not be confirmed. "
                "Verify the manufacturer in the panel before confirming."
            )
        else:
            if not _has_model and not _has_pn:
                _specs_dict["spec_based_sourcing"] = True
                result["asset_specs"] = _specs_dict
                reply_text = (
                    "Sourcing by category — we have enough specs (manufacturer, type, key dimensions) "
                    "to find functionally equivalent options. No specific part number or model is required."
                )
            else:
                reply_text = "Specs look complete — review in the panel and confirm to start sourcing."
        new_phase = current_phase
    else:
        reply_text = result.get("follow_up_question") or (
            "Can you provide more details? I need the manufacturer, model, and part number."
        )
        new_phase = current_phase

    # Persist updated specs (and phase) back to the DB
    with _SessionFactory() as session:
        orm = session.get(SourcingRunORM, run_id)
        if orm:
            orm.asset_specs_json = json.dumps(result["asset_specs"])
            if new_phase != current_phase:
                orm.current_phase = new_phase
            orm.updated_at = datetime.now(timezone.utc)
            session.commit()

    agent_reply: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "role": "agent",
        "content": reply_text,
        "created_at": now,
    }
    thread.append(agent_reply)
    # Night 1 — capture the agent reply + the intake result for this turn.
    _run_capture.capture_turn(run_id, "agent", reply_text)
    _run_capture.capture_intake_result(
        run_id,
        sufficient=bool(result.get("sufficient")),
        proceed_state=proceed_state or None,
        manufacturer_confidence=result.get("manufacturer_confidence"),
        part_id_confidence=result.get("part_id_confidence"),
        asset_specs=result.get("asset_specs"),
        follow_up_question=result.get("follow_up_question"),
    )

    return SendMessageResponse(
        run_id=run_id,
        message=agent_reply,
        updated_phase=new_phase,
        proceed_state=proceed_state or None,
        # The N per-part dicts ride along ONLY on a multi-part detection (None otherwise), so the
        # frontend can fan them into seeded cards.
        parts=result.get("multi_part_specs") if proceed_state == "multi_part_detected" else None,
    )


@app.post("/api/runs/{run_id}/upload")
async def upload_nameplate(
    run_id: str,
    request: Request,
    file: UploadFile = File(...),
    text: str = Form("", max_length=4000),
):
    """
    Upload a nameplate image for vision extraction.

    Pipes image bytes to IntakeAgent multimodal extraction, updates asset_specs_json,
    and returns a three-case agent reply:
    (a) high confidence  — specs complete, confirm to proceed
    (b) low confidence   — partial extraction, ask user to verify
    (c) failed           — nothing readable, offer three recovery paths

    DEMO_MODE session isolation: scoped to the run's owner (404 on mismatch/missing/
    NULL-session — not 403). Inert when DEMO_MODE is off.
    """
    demo_sid = _demo_session_id_from_request(request)
    with _SessionFactory() as session:
        orm = session.get(SourcingRunORM, run_id)
        if not orm or not _demo_run_owned_by_session(orm, demo_sid):
            raise HTTPException(status_code=404, detail="Run not found")
        current_phase = orm.current_phase
        prior_specs: Dict[str, Any] = (
            json.loads(orm.asset_specs_json) if orm.asset_specs_json else {}
        )

    contents = await file.read()
    # Upload size cap (FIX C): reject an oversized image before the (expensive) vision
    # extraction runs. Read-then-check: the bytes are buffered (Starlette spills >1MB
    # to a spool file, so memory isn't unbounded), but the cap stops the work before
    # the LLM call. DEMO_MAX_UPLOAD_BYTES env-configurable; default 10MB (a nameplate
    # photo). Always-on — a prod upload cap is sensible too. 422 on exceed.
    if len(contents) > _DEMO_MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"Image too large (max {_DEMO_MAX_UPLOAD_BYTES} bytes) — use a smaller photo.",
        )
    now = datetime.now(timezone.utc).isoformat()

    thread = _messages.setdefault(run_id, [])
    thread.append({
        "id": str(uuid.uuid4()),
        "role": "system",
        "content": f"Nameplate uploaded: {file.filename}",
        "created_at": now,
        "attachment": {
            "type": "image",
            "filename": file.filename,
            "size_bytes": len(contents),
        },
    })

    # Run IntakeAgent multimodal extraction
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    agent = IntakeAgent(anthropic_api_key=api_key)
    run_obj = SourcingRun(
        id=run_id,
        current_phase=current_phase,
        asset_specs_json=prior_specs,
    )

    try:
        # Pass the user's typed description ALONGSIDE the image — the multimodal extractor
        # templates it in, so an attached image never silently eats the text (and a multi-part
        # description + image reaches the same array detection as the text path).
        result = agent.run(run_obj, {"text": text, "images": [contents]})
    except Exception:
        traceback.print_exc()
        result = None

    proceed_state = ""   # set from the result below; "" (None on the wire) when the image threw

    if result is None:
        # Extraction threw — treat as failed case
        reply_text = (
            "Something went wrong reading that image.\n\n"
            "Options:\n"
            "• Try uploading a clearer photo\n"
            "• Type the key specs (manufacturer, model, part number) instead\n"
            "• Continue with partial information already captured"
        )
        new_specs = prior_specs
    else:
        mfg_conf  = float(result.get("manufacturer_confidence") or 0)
        part_conf = float(result.get("part_id_confidence") or 0)
        specs     = result.get("asset_specs") or {}
        mfg       = specs.get("manufacturer") or ""
        model     = specs.get("model") or ""
        pn        = specs.get("part_number") or ""
        proceed_state = result.get("confidence_summary", {}).get("proceed_state", "")

        # Multi-part detected is a SUCCESS (N parts found), not a read failure — handle it FIRST
        # so it never falls through to case (d) "couldn't read the nameplate". The read worked;
        # the reply is the same "N detected, one at a time" follow-up the text path uses.
        if proceed_state == "multi_part_detected":
            reply_text = result.get("follow_up_question") or (
                "It looks like you've described several parts. Please submit one part at a time for now."
            )
        # (a) High confidence — both thresholds met
        elif result.get("sufficient"):
            ident = " ".join(p for p in [mfg, pn or model] if p)
            reply_text = (
                f"Extracted: {ident} — specs are in the panel. "
                "Review and confirm to start sourcing."
            )
        # (b) Both confidences above threshold but a required field is still missing
        elif mfg_conf >= 70 and part_conf >= 70 and mfg and mfg not in ("Unknown", "N/A", "null", "unknown"):
            ident = " ".join(p for p in [mfg, pn or model] if p)
            reply_text = (
                f"Read the nameplate: {ident}. "
                "Some required fields may still be missing — review the panel and fill in any gaps before confirming."
            )
        # (c) Low confidence — something extracted but at least one threshold not met
        elif mfg and mfg not in ("Unknown", "N/A", "null", "unknown"):
            ident = " ".join(p for p in [mfg, model] if p)
            if mfg_conf >= 70 and part_conf < 70:
                low_conf_detail = "Part identification confidence is low"
            elif mfg_conf < 70 and part_conf >= 70:
                low_conf_detail = "Manufacturer confidence is low"
            else:
                low_conf_detail = "Confidence is low"
            _pn_null = {"", "N/A", "n/a", "null", "None", "UNKNOWN-PN", "Unknown", "unknown"}
            pn_suggestion = "" if pn and pn not in _pn_null else " or provide the part number directly"
            reply_text = (
                f"Read the nameplate: {ident} "
                f"(manufacturer confidence {mfg_conf:.0f}%). "
                f"{low_conf_detail} — please verify the specs in the panel{pn_suggestion}."
            )
        # (d) Nothing readable
        else:
            reply_text = (
                "Couldn't read the nameplate clearly.\n\n"
                "Options:\n"
                "• Try a clearer or closer photo\n"
                "• Describe the part (manufacturer, model, part number)\n"
                "• Continue with partial information already captured"
            )

        new_specs = specs

        # Persist updated specs to DB
        with _SessionFactory() as session:
            orm = session.get(SourcingRunORM, run_id)
            if orm:
                orm.asset_specs_json = json.dumps(new_specs)
                orm.updated_at = datetime.now(timezone.utc)
                session.commit()

    agent_reply: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "role": "agent",
        "content": reply_text,
        "created_at": now,
    }
    thread.append(agent_reply)
    # Night 1 — capture the nameplate upload turn + intake result (RUN_CAPTURE-gated).
    _run_capture.capture_turn(run_id, "agent", reply_text)
    if result is not None:
        _run_capture.capture_intake_result(
            run_id,
            sufficient=bool(result.get("sufficient")),
            proceed_state=proceed_state or None,
            manufacturer_confidence=result.get("manufacturer_confidence"),
            part_id_confidence=result.get("part_id_confidence"),
            asset_specs=result.get("asset_specs"),
            follow_up_question=result.get("follow_up_question"),
        )

    return {
        "run_id":   run_id,
        "filename": file.filename,
        "size_bytes": len(contents),
        "message":  agent_reply,
        # Mirror send_message: surface the intake signal so the frontend can fan a multi-part
        # image into N seeded cards. parts ride along ONLY on a multi-part detection.
        "proceed_state": proceed_state or None,
        "parts": result.get("multi_part_specs") if (result and proceed_state == "multi_part_detected") else None,
        "extraction": {
            "status":           "ok" if result else "error",
            "sufficient":       result.get("sufficient") if result else False,
            "mfg_confidence":   result.get("manufacturer_confidence") if result else 0,
            "part_confidence":  result.get("part_id_confidence") if result else 0,
        },
    }


def _mock_confirmation_response(run_id: str, candidate_ids: list) -> None:
    """Simulate Tier 1 vendor confirming price and availability after a short delay.

    Invoked as a BackgroundTask after POST /request-confirmation.
    Sleeps _MOCK_CONFIRMATION_DELAY_RANGE seconds then sets confirmation_needed=False
    on matching raw Tier 1 candidates in sourcing_results_json. The frontend's
    comparison-phase polling picks up the change and transitions cards to Buy Now.
    If a candidate ID cannot be matched in the raw results, logs a warning and skips
    rather than crashing — guards against results being mutated between request and response.
    """
    import time, random, logging
    log = logging.getLogger(__name__)
    time.sleep(random.uniform(*_MOCK_CONFIRMATION_DELAY_RANGE))

    candidate_id_set = set(candidate_ids)

    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run or not run.sourcing_results_json:
            log.warning("[%s] _mock_confirmation_response: run not found or no sourcing results", run_id)
            return
        try:
            raw = json.loads(run.sourcing_results_json)
        except (json.JSONDecodeError, ValueError):
            log.warning("[%s] _mock_confirmation_response: could not parse sourcing_results_json", run_id)
            return

        t1_results = raw.get("tier_1", {}).get("results", [])
        matched = 0
        for i, opt in enumerate(t1_results):
            cid = f"{opt.get('vendor_name', '')}-t1-{i}"
            if cid in candidate_id_set:
                opt["confirmation_needed"] = False
                matched += 1
                candidate_id_set.discard(cid)

        if candidate_id_set:
            log.warning("[%s] _mock_confirmation_response: %d candidate(s) not matched: %s",
                        run_id, len(candidate_id_set), list(candidate_id_set))
        if matched == 0:
            return

        raw["tier_1"]["results"] = t1_results
        run.sourcing_results_json = json.dumps(raw)
        run.updated_at = datetime.now(timezone.utc)
        session.commit()


@app.post("/api/runs/{run_id}/request-confirmation")
def request_confirmation(run_id: str, body: ConfirmationRequest, background_tasks: BackgroundTasks):
    """Request price and availability confirmation from Tier 1 vendors.

    Schedules a mock response (3-8 seconds) that sets confirmation_needed=False
    on the matching candidates. Frontend polls at comparison phase and transitions
    cards from "Awaiting response" to Buy Now when the flag flips.
    """
    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        # Validate the requested ids against the run's Tier 1 candidates before
        # scheduling, so an id that matches nothing fails loudly here rather than
        # returning 200 for a background no-op the caller can't detect.
        raw = json.loads(run.sourcing_results_json) if run.sourcing_results_json else {}
        t1_results = raw.get("tier_1", {}).get("results", [])
        valid_ids = {f"{opt.get('vendor_name', '')}-t1-{i}" for i, opt in enumerate(t1_results)}
        if not (set(body.candidate_ids) & valid_ids):
            raise HTTPException(
                status_code=404,
                detail="No matching Tier 1 candidate for the requested id(s)",
            )
    background_tasks.add_task(_mock_confirmation_response, run_id, body.candidate_ids)
    return {
        "run_id": run_id,
        "candidates": body.candidate_ids,
        "mock_response_in": f"{_MOCK_CONFIRMATION_DELAY_RANGE[0]}-{_MOCK_CONFIRMATION_DELAY_RANGE[1]} seconds",
    }


def _selected_candidate_total(sourcing_results_json: Optional[str], candidate_id: str, tier: int) -> float:
    """Resolve the selected candidate's purchase total (USD) from the stored raw sourcing
    results, by reconstructing its display id (`{vendor}-t{tier}-{idx}`, the same scheme
    _transform_option uses). Mirrors the Orchestrator: grand_total_usd, else a buyable
    base_price, else 0.0 (price-hidden / RFQ / not-found -> $0 -> single-approver path)."""
    try:
        raw = json.loads(sourcing_results_json) if sourcing_results_json else {}
    except (ValueError, TypeError):
        return 0.0
    results = (raw.get(f"tier_{tier}") or {}).get("results", []) or []
    for idx, opt in enumerate(results):
        if f"{opt.get('vendor_name', '')}-t{tier}-{idx}" != candidate_id:
            continue
        if opt.get("grand_total_usd") is not None:
            try:
                return float(opt["grand_total_usd"])
            except (TypeError, ValueError):
                return 0.0
        if opt.get("price_tbd") or opt.get("requires_rfq"):
            return 0.0
        base = opt.get("base_price")
        try:
            return float(base) if base is not None else 0.0
        except (TypeError, ValueError):
            return 0.0
    return 0.0


@app.post("/api/runs/{run_id}/select-candidate")
def select_candidate(run_id: str, body: SelectCandidateRequest):
    """Lock in a candidate and advance the run to pending_first_approval.

    H1: evaluate the facility's approval rules against the selected candidate's total and
    persist the `_approval_path` (approvers_required + roles) on the selection, so the
    approve endpoint can route the $5k/$25k dual-approver requirement (mirrors the
    Orchestrator's select_candidate). Auth-independent — threshold routing only."""
    from utils.procurement_agent.state.approval_rules import determine_approval_path

    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        total_usd = _selected_candidate_total(run.sourcing_results_json, body.candidate_id, body.tier)
        facility_id = run.facility_id or "00000000-0000-0000-0000-000000000000"
        approvers_required, approver_roles = determine_approval_path(facility_id, total_usd)

        run.selected_candidate_json = json.dumps({
            "candidate_id": body.candidate_id,
            "tier": body.tier,
            "selected_at": datetime.now(timezone.utc).isoformat(),
            "_approval_path": {
                "approvers_required": approvers_required,
                "approver_roles":     approver_roles,
                "grand_total_usd":    total_usd,
            },
        })
        run.current_phase = Phase.PENDING_FIRST_APPROVAL.value
        run.updated_at = datetime.now(timezone.utc)
        session.commit()

    # Night 1 — capture the select-candidate user action (RUN_CAPTURE-gated, fail-soft).
    _run_capture.capture_user_action(run_id, "select_candidate",
                                     detail={"candidate_id": body.candidate_id, "tier": body.tier})
    return {"run_id": run_id, "phase": Phase.PENDING_FIRST_APPROVAL.value}


class OrderNowRequest(BaseModel):
    candidate_id: str
    tier: int
    quantity: int = 1


# Phases that mean the run's single selection/approval cycle is already committed
# (past the options stage) — order-now is once per run ("buying => selecting").
_COMMITTED_PHASES: set[str] = {
    Phase.PENDING_FIRST_APPROVAL.value, Phase.PENDING_SECOND_APPROVAL.value,
    Phase.APPROVED.value, Phase.EXECUTING.value, Phase.FULFILLING.value, Phase.COMPLETED.value,
}


def _transition_run(run: SourcingRunORM, target: Phase) -> bool:
    """Apply a run phase transition THROUGH the state machine (never a direct set, so a
    run can't be moved backwards illegally). Returns True if applied/already-there."""
    from utils.procurement_agent.state.phases import Phase as _P, validate_transition
    current = _P(run.current_phase)
    if current == target:
        return True
    if validate_transition(current, target):
        run.current_phase = target.value
        return True
    return False


def _run_already_committed(run: SourcingRunORM, run_id: str, orders_mod) -> bool:
    """True if the run already has a committed order/selection — the double-order guard.
    Three independent signals (defence-in-depth): committed phase, an existing order, or
    a manual-fulfilment selection already recorded."""
    if run.current_phase in _COMMITTED_PHASES:
        return True
    if orders_mod.get_orders(run_id=run_id):
        return True
    try:
        sel = json.loads(run.selected_candidate_json) if run.selected_candidate_json else {}
    except (ValueError, TypeError):
        sel = {}
    return isinstance(sel, dict) and sel.get("fulfilment") == "manual"


def _reconstruct_candidate(sourcing_results_json: Optional[str], candidate_id: str, tier: int) -> Optional[dict]:
    """Return the raw candidate dict from a run's stored sourcing results by rebuilding
    its positional display id (`{vendor}-t{tier}-{idx}`) — the AUTHORITATIVE source
    (never a client-supplied candidate/price). None if not found."""
    try:
        raw = json.loads(sourcing_results_json) if sourcing_results_json else {}
    except (ValueError, TypeError):
        return None
    results = (raw.get(f"tier_{tier}") or {}).get("results", []) or []
    for idx, opt in enumerate(results):
        if f"{opt.get('vendor_name', '')}-t{tier}-{idx}" == candidate_id:
            return opt
    return None


@app.post("/api/runs/{run_id}/order-now")
def create_order_now(run_id: str, body: OrderNowRequest):
    """Manual fulfilment "Order" / "Order through Arkim" on ANY PRICED candidate. Buying
    => selecting — the candidate becomes the run's selection — and the spend routes
    through the SAME approval path as everything else (NOT exempt: "available
    immediately" is speed, not spend authority).

    Works for marketplace (source="marketplace") and reference (source="buy") candidates
    alike; only a PRICE-LESS (quote-required) candidate is rejected (422 — get a quote
    first). The order is manually fulfilled either way (an operator buys/sources it —
    increment 2), so it is tagged fulfilment="manual" on the selection.

    - sub-threshold (0 approvers): the order is created immediately in
      pending_manual_fulfilment.
    - at/above threshold (>=1): NO order yet; the run goes to PENDING_FIRST_APPROVAL and
      the existing approve flow (H1/M1) runs unchanged; the order materialises
      post-approval via /execute (the manual-fulfilment branch).

    The candidate + price are reconstructed server-side from the stored sourcing results
    and the channel re-derived server-side — the client supplies neither price nor
    channel. 404 unknown run/candidate; 422 price-less."""
    from utils import orders
    from utils.marketplace_registry import is_marketplace
    from utils.procurement_agent.state.approval_rules import determine_approval_path

    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        # Committed-run guard (DOUBLE-ORDER safety): order-now is once per run. A repeat
        # call would otherwise create a second order (sub-threshold) or clobber the
        # selection + reset approval (at/above). Reject a run that's already committed.
        if _run_already_committed(run, run_id, orders):
            raise HTTPException(
                status_code=409,
                detail="This run already has a committed order — start a new request to order another part.",
            )

        cand = _reconstruct_candidate(run.sourcing_results_json, body.candidate_id, body.tier)
        if cand is None:
            raise HTTPException(status_code=404, detail="Candidate not found in this run")

        # A real, buyable price is required (a quote-required row goes via "Get quote",
        # not here). Channel is re-derived server-side — never trust client price/channel.
        price_hidden = cand.get("price_tbd") or cand.get("requires_rfq")
        raw_price = None if price_hidden else cand.get("base_price")
        if raw_price is None:
            raise HTTPException(status_code=422,
                                detail="Candidate has no buyable price — request a quote instead")
        try:
            unit_price = float(raw_price)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Candidate price is not a number")
        source_url = cand.get("source_url")
        channel = "marketplace" if (source_url and is_marketplace(source_url)) else "buy"

        qty = body.quantity or 1
        total_usd = unit_price * qty
        facility_id = run.facility_id or "00000000-0000-0000-0000-000000000000"
        approvers_required, approver_roles = determine_approval_path(facility_id, total_usd)

        now = datetime.now(timezone.utc)
        # Buying => selecting: record the candidate as the run's selection. fulfilment=
        # "manual" routes the post-approval execute to pending_manual_fulfilment (vs the
        # existing place flow); source carries the channel for the operator.
        selected = {
            "candidate_id": body.candidate_id,
            "tier": body.tier,
            "selected_at": now.isoformat(),
            "source": channel,
            "fulfilment": "manual",
            "quantity": qty,
            "_approval_path": {
                "approvers_required": approvers_required,
                "approver_roles":     approver_roles,
                "grand_total_usd":    total_usd,
            },
        }

        if approvers_required >= 1:
            # At/above threshold: run-phase approval; NO order yet (Model A). Route the
            # transition THROUGH the state machine (no direct set / no backward move).
            run.selected_candidate_json = json.dumps(selected)
            _transition_run(run, Phase.PENDING_FIRST_APPROVAL)
            run.updated_at = now
            phase = run.current_phase
            session.commit()
            # Night 1 — capture the order-now user action (at/above-threshold path).
            _run_capture.capture_user_action(run_id, "order_now",
                                             detail={"candidate_id": body.candidate_id, "tier": body.tier,
                                                     "quantity": qty, "channel": channel,
                                                     "pending_approval": True})
            return {"pending_approval": True, "order": None, "phase": phase}

        # Sub-threshold (auto-approved, 0 approvers): advance the run THROUGH approval so
        # "committed" is reflected by phase (comparison -> pending_first -> approved, both
        # validated), then create the operator-fulfillable order. Safe with OrderSection,
        # which renders BeingPurchased (the pending order) over the place-order card.
        run.selected_candidate_json = json.dumps(selected)
        _transition_run(run, Phase.PENDING_FIRST_APPROVAL)
        _transition_run(run, Phase.APPROVED)
        run.updated_at = now
        company_id = run.company_id
        specs = json.loads(run.asset_specs_json) if run.asset_specs_json else {}
        phase = run.current_phase
        session.commit()

    selection = {
        "run_id":        run_id,
        "manufacturer":  specs.get("manufacturer"),
        "part_number":   specs.get("part_number"),
        "vendor_name":   cand.get("vendor_name"),
        "source_url":    source_url,
        "unit_price":    unit_price,
        "currency":      cand.get("currency") or "USD",
        "source":        channel,
        "quantity":      qty,
    }
    order = orders.create_order(selection, quantity=qty, company_id=company_id,
                                initial_status=orders.STATUS_PENDING_FULFILMENT)
    if not order:
        raise HTTPException(status_code=500, detail="Order capture failed")
    _persist_order_on_run(run_id, order)
    # Night 1 — capture the order-now user action (sub-threshold path; the
    # at/above-threshold path returns earlier and is captured below).
    _run_capture.capture_user_action(run_id, "order_now",
                                     detail={"candidate_id": body.candidate_id, "tier": body.tier,
                                             "quantity": qty, "channel": channel})
    return {"pending_approval": False, "order": order, "phase": phase}


@app.post("/api/runs/{run_id}/approve")
def approve_run(run_id: str, body: ApproveRequest, caller: Optional[Caller] = Depends(get_caller)):
    """
    Record an approval action and route by the persisted approval path (H1) with
    distinct-approver enforcement (M1).

    H1: a first approval on a run whose `_approval_path` requires >= 2 approvers advances
    to pending_second_approval; the second approval (or any single-approver path)
    advances to approved.

    M1: when the request carries a verified identity (Cognito JWT -> Caller), the
    authenticated `sub` is recorded as `approver_id` and a second approval from the SAME
    identity is rejected (409) — the two approvals must come from DISTINCT people. This
    enforces on the verified `sub` only, NEVER on the body-supplied approver_name/role
    (which a caller could spoof). With no token (the current no-auth demo), approver_id
    is null and distinctness is not enforced — behaviour is unchanged until the frontend
    forwards identity. D2 tenant-scoping is deferred (no tenant key in the stores yet;
    needs core's assigned_sites — see CLEANUP §4.1).
    """
    approver_id = caller.user_id if caller else None
    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        history = (
            json.loads(run.approval_history_json)
            if isinstance(run.approval_history_json, str) and run.approval_history_json
            else (run.approval_history_json or [])
        )

        # M1: a verified approver may not approve the same run twice (distinct-approver).
        # Enforced only on the authenticated sub — body identity is never trusted for this.
        if approver_id is not None:
            prior_approvers = {
                h.get("approver_id") for h in history if h.get("action") == "approved"
            }
            if approver_id in prior_approvers:
                raise HTTPException(
                    status_code=409,
                    detail="A second, distinct approver is required — you have already approved this run.",
                )

        history.append({
            "sequence": len(history) + 1,
            "approver_id": approver_id,
            "approver_name": body.approver_name,
            "approver_role": body.approver_role,
            "action": "approved",
            "notes": body.notes,
            "acted_at": datetime.now(timezone.utc).isoformat(),
        })
        run.approval_history_json = json.dumps(history)

        # Route on the persisted approval path. A first approval that requires a second
        # approver holds at pending_second_approval; everything else (the second approval,
        # or a single-approver path / no path) reaches approved — preserving the prior
        # single-approval behaviour when no dual-approver requirement was recorded.
        selected = (
            json.loads(run.selected_candidate_json)
            if run.selected_candidate_json else {}
        )
        approvers_required = int((selected.get("_approval_path") or {}).get("approvers_required", 1))
        if run.current_phase == Phase.PENDING_FIRST_APPROVAL.value and approvers_required >= 2:
            next_phase = Phase.PENDING_SECOND_APPROVAL
        else:
            next_phase = Phase.APPROVED

        run.current_phase = next_phase.value
        run.updated_at = datetime.now(timezone.utc)
        session.commit()

    # Night 1 — capture the approve user action (RUN_CAPTURE-gated, fail-soft).
    _run_capture.capture_user_action(run_id, "approve",
                                     detail={"approver_role": body.approver_role, "next_phase": next_phase.value})
    return {"run_id": run_id, "phase": next_phase.value}


@app.post("/api/runs/{run_id}/reject")
def reject_run(run_id: str, body: RejectRequest):
    """
    Record a rejection, unselect the candidate, and return to comparison.
    Notes are required (enforced by the Pydantic model).
    """
    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        history = (
            json.loads(run.approval_history_json)
            if isinstance(run.approval_history_json, str) and run.approval_history_json
            else (run.approval_history_json or [])
        )
        history.append({
            "sequence": len(history) + 1,
            "approver_name": body.approver_name,
            "approver_role": body.approver_role,
            "action": "rejected",
            "notes": body.notes,
            "acted_at": datetime.now(timezone.utc).isoformat(),
        })
        run.approval_history_json = json.dumps(history)
        run.selected_candidate_json = None
        run.current_phase = Phase.COMPARISON.value
        run.updated_at = datetime.now(timezone.utc)
        session.commit()

    # Night 1 — capture the reject user action (RUN_CAPTURE-gated, fail-soft).
    _run_capture.capture_user_action(run_id, "reject", detail={"approver_role": body.approver_role})
    return {"run_id": run_id, "phase": Phase.COMPARISON.value}


@app.post("/api/runs/{run_id}/confirm-intake")
def confirm_intake(
    run_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    exact_only: bool = False,
    open_family: bool = False,
):
    """
    Confirm intake specs and atomically advance to sourcing.

    Writes the inventory stub and transitions phase in a single DB commit so
    there is no window where the run is phase=sourcing without inventory_result.
    Idempotent: returns 409 if the run is already past intake.

    exact_only=true ("find exact replacements only" — the no-spec-sheet honesty
    branch): records the flag so the background sourcing drops aftermarket/equivalent
    Tier 2/3 candidates, surfacing only exact OEM matches (Tier 1 network unaffected).

    open_family=true ("I don't know the rating — source the family as-is"):
    bypasses the family-variant binding guard (below) for a family-level request
    (model present, no part_number) that has not resolved a variant-selecting
    attr. Records the choice so the run commits as an HONEST open-family /
    spec-based commit (the existing spec_based_sourcing / "could not be
    confirmed" honesty path), not a silent variant pick. Inert for non-family
    requests (clean-PN, spec-described-no-model, non-variant classes).

    Family-variant binding guard: a family-level request for a variant-selecting
    class (a model names a FAMILY, e.g. "PowerFlex 40" — the intake chat asked
    for HP/kW + voltage) cannot be silently confirmed until at least one
    variant-selecting attr is answered. Without this, confirm_intake bypasses
    the chat ask (it is phase-gated, not sufficiency-gated) and sourcing runs on
    an open family spec — or on an extractor-hallucinated rating. The guard
    fires AFTER the 409/422/cap guards and BEFORE the phase mutate; it returns
    422 with a frontend-consumable detail naming the missing attrs so the intake
    card can re-surface the ask + the open-family affordance. The chat-side
    ask-then-resolve is SOFT (any reply clears the pending flag — the
    conversation stays conversational); the HARD enforcement lives ONLY here.

    DEMO_MODE session isolation: scoped to the run's owner (404 on mismatch/missing/
    NULL-session — not 403). Inert when DEMO_MODE is off.
    """
    demo_sid = _demo_session_id_from_request(request)
    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run or not _demo_run_owned_by_session(run, demo_sid):
            raise HTTPException(status_code=404, detail="Run not found")
        if run.current_phase != Phase.INTAKE.value:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Run is not in intake phase",
                    "current_phase": run.current_phase,
                },
            )
        if not run.asset_specs_json:
            raise HTTPException(
                status_code=422,
                detail="No asset specs captured yet — complete intake chat first",
            )
        # DEMO_MODE sourcing cap — the spend trigger. Checked AFTER the 409/422 guards
        # so only a valid intake->sourcing transition counts (a re-fire on an already-
        # sourcing run hits 409 first and never reaches here). Before the phase mutate +
        # background scheduling so a denied 429 leaves the run untouched. Inert when off.
        _demo_enforce_cap(
            "sourcing_per_session", demo_sid, _DEMO_MAX_SOURCING_PER_SESSION, "sourcing"
        )
        specs_dict = json.loads(run.asset_specs_json)

        # Family-variant binding guard (T3) — AFTER the 409/422/cap guards, BEFORE
        # the phase mutate. A family-level request (model present, no PN) for a
        # variant-selecting class cannot be silently confirmed without an answered
        # variant-selecting attr OR an explicit open_family opt-in. The opt-in
        # commits as an honest open-family/spec-based source; the block returns a
        # stable, frontend-consumable 422 so the intake card can re-surface the ask
        # and the open-family affordance (T5). Clean-PN, spec-described-no-model,
        # and non-variant classes: family_disambig_block returns None -> byte-
        # identical to the pre-guard path.
        if not open_family:
            from utils.procurement_agent.agents.intake_agent import family_disambig_block
            block = family_disambig_block(specs_dict)
            if block is not None:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": (
                            f"{block['model']} is a product family, not a specific variant — "
                            f"confirm the rating ({', '.join(block['missing_labels'])}) in the "
                            f"chat, or source the family as-is if you don't know it."
                        ),
                        "reason": block["reason"],
                        "model": block["model"],
                        "missing_attrs": block["missing_attrs"],
                        "missing_labels": block["missing_labels"],
                        "pending": block.get("pending", False),
                    },
                )

        urgency_factor, warranty_status = _commit_intake_to_sourcing(
            session, run, specs_dict, exact_only=exact_only, open_family=open_family,
            background_tasks=background_tasks,
        )

    # Night 1 — capture the confirm-intake user action (RUN_CAPTURE-gated, fail-soft).
    _run_capture.capture_user_action(
        run_id, "confirm_intake",
        detail={"exact_only": exact_only, "open_family": open_family},
    )
    return {"run_id": run_id, "phase": Phase.SOURCING.value}


def _commit_intake_to_sourcing(
    session,
    run: SourcingRunORM,
    specs_dict: Dict[str, Any],
    *,
    exact_only: bool = False,
    open_family: bool = False,
    background_tasks: Optional[BackgroundTasks] = None,
) -> tuple:
    """The intake → sourcing transition — the SINGLE load-bearing mutation both
    the in-app ``confirm_intake`` path and the Night 8 channel-agnostic intake
    consumer fire. Factored out of confirm_intake so the intake consumer reuses
    the EXACT same transition (a transport, never a parallel pipeline).

    Writes the inventory stub, applies the exact_only / open_family honesty
    flags to the specs, advances ``current_phase`` to SOURCING, commits, then
    schedules ``_run_sourcing_background`` (when background_tasks is provided).
    Returns ``(urgency_factor, warranty_status)`` so callers can pass them to
    the background task (confirm_intake schedules itself; the intake consumer
    lets this helper schedule).

    The family-variant binding guard is NOT here — it is a pre-gate each caller
    runs (confirm_intake raises 422; the intake consumer replies
    NEEDS_CLARIFICATION). This helper assumes the family guard already passed.
    NEVER places/approves/advances an order — sourcing only (auto-order is out).
    """
    if exact_only:
        specs_dict["exact_only"] = True
    if open_family:
        # Honest open-family commit: the user explicitly chose to source the
        # family as-is rather than confirm a variant. Recorded (not silent) so
        # sourcing + the UI can flag the run as family-open / spec-based.
        specs_dict["spec_based_sourcing"] = True
        specs_dict["family_open_commit"] = True
    if exact_only or open_family:
        run.asset_specs_json = json.dumps(specs_dict)
    urgency_factor = run.urgency_factor
    warranty_status = run.warranty_status
    run.inventory_result_json = json.dumps({
        "status": "no_data",
        "message": "Inventory agent not yet connected (Phase 5).",
    })
    run.current_phase = Phase.SOURCING.value
    run.updated_at = datetime.now(timezone.utc)
    session.commit()

    if background_tasks is not None:
        background_tasks.add_task(
            _run_sourcing_background, run.id, specs_dict, urgency_factor, warranty_status
        )
    return urgency_factor, warranty_status


def _fire_sourcing_run_for_intake(
    specs_dict: Dict[str, Any],
    tenant_key: str,
    *,
    background_tasks: Optional[BackgroundTasks] = None,
    is_test: bool = True,
) -> Optional[str]:
    """The injected ``fire_sourcing_run`` for the intake consumer — creates a
    sourcing run (Phase.INTAKE) attributed to the resolved tenant, seeds the
    parser's proposed specs, and fires ``_commit_intake_to_sourcing`` (the same
    transition in-app confirm_intake uses) → Phase.SOURCING + the background
    sourcing task. Returns the run_id, or None on a missing tenant / store error.

    This is the EXISTING pipeline seam (I1) the channel-agnostic consumer feeds:
    create_run (bare, tenant-stamped) → seed specs → confirm-intake transition.
    Same flags, same gates as an in-app request. NEVER places/approves/advances
    an order — sourcing only (auto-order is explicitly out, Night 8).

    ``is_test`` marks the run's provenance — the intake surface is exercised
    under the suite's TestClient, so runs it births carry the test marker until
    a live intake path exists (the run ORM has no is_test column; provenance is
    carried via the company_id stamp + the intake_channels held/known-sender
    rows which DO carry is_test=1). A real intake path would set is_test=False.
    """
    tenant = intake_channels.tenant_lookup(tenant_key)
    if tenant is None:
        return None
    now = datetime.now(timezone.utc)
    run = SourcingRunORM(
        id=str(uuid.uuid4()),
        facility_id=tenant["facility_id"],
        company_id=tenant["company_id"],
        group_id=None,
        current_phase=Phase.INTAKE.value,
        urgency_factor=0.3,
        warranty_status="unknown",
        asset_specs_json=json.dumps(specs_dict),
        initiated_at=now,
        updated_at=now,
    )
    try:
        with _SessionFactory() as session:
            session.add(run)
            session.commit()
            session.refresh(run)
            _commit_intake_to_sourcing(
                session, run, specs_dict, exact_only=False, open_family=False,
                background_tasks=background_tasks,
            )
        return run.id
    except Exception as exc:
        print(f"[Intake] _fire_sourcing_run_for_intake failed: {exc}")
        return None


@app.post("/api/runs/{run_id}/outreach")
def initiate_outreach(run_id: str, body: OutreachRequest):
    """Mark Tier 3 vendors as contacted and persist sent timestamps.

    EMAIL_SEND_ENABLED is False per brief Section 12 — no actual email dispatch.
    Records {candidateId: sentAt} in tier3_outreach_sent_json. Frontend polls
    and transitions cards to "Awaiting response" state.
    """
    sent_at = datetime.now(timezone.utc).isoformat()
    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        existing: Dict[str, str] = json.loads(run.tier3_outreach_sent_json) if run.tier3_outreach_sent_json else {}
        for cid in body.candidate_ids:
            if cid not in existing:  # preserve original sent_at on re-fire
                existing[cid] = sent_at
        run.tier3_outreach_sent_json = json.dumps(existing)
        run.updated_at = datetime.now(timezone.utc)
        session.commit()

    # Night 1 — capture the outreach user action (RUN_CAPTURE-gated, fail-soft).
    _run_capture.capture_user_action(run_id, "outreach", detail={"candidate_ids": body.candidate_ids})
    return {
        "run_id": run_id,
        "candidates_contacted": len(body.candidate_ids),
        "sent_at": sent_at,
        "tier3_outreach_sent": existing,
    }


@app.post("/api/runs/{run_id}/save-outreach")
def save_outreach_selection(run_id: str, body: SaveOutreachRequest):
    """Persist vendor selection without sending — user can resume later."""
    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        run.tier3_selection_json = json.dumps(body.candidate_ids)
        run.updated_at = datetime.now(timezone.utc)
        phase = run.current_phase
        session.commit()

    return {
        "run_id": run_id,
        "saved_count": len(body.candidate_ids),
        "phase": phase,
    }


# ---------------------------------------------------------------------------
# Facility endpoints
# ---------------------------------------------------------------------------

# Static mock facilities — Phase 2 will pull these from a DB table.
_MOCK_FACILITIES: List[FacilityOut] = [
    FacilityOut(id="fac-stockton", name="Bay Foods · Stockton", state="CA"),
    FacilityOut(id="fac-modesto",  name="Bay Foods · Modesto",  state="CA"),
    FacilityOut(id="fac-fresno",   name="Bay Foods · Fresno",   state="CA"),
    FacilityOut(id="fac-salinas",  name="Bay Foods · Salinas",  state="CA"),
    FacilityOut(id="fac-cerritos", name="Bay Foods · Cerritos", state="CA"),
]


@app.get("/api/facilities", response_model=List[FacilityOut])
def list_facilities():
    """List all facilities. Stubbed in Phase 1."""
    return _MOCK_FACILITIES


# ---------------------------------------------------------------------------
# Approval rule endpoints (delegating to the existing approval_rules module)
# ---------------------------------------------------------------------------

@app.get("/api/approval-rules/{facility_id}", response_model=List[ApprovalRuleOut])
def get_approval_rules(facility_id: str):
    """
    Approval tiers that GOVERN routing for a facility — read from the approval_rules table
    (the SAME source `determine_approval_path` keys on), falling back to DEFAULT_RULES when
    the facility has no custom rules yet. Returns the buy tiers ascending by threshold.

    `cap` is derived for display only (one below the next tier's threshold; null for the
    top tier) — it is not stored and does not affect routing (routing keys on threshold).
    `applies_to` is "buy": these are the purchase-approval tiers the engine reads (the
    prototype persists buy tiers only; outreach routing is not wired through this store).
    A fallback (default) tier carries an empty `id` — saving it inserts a real row.
    """
    from utils.procurement_agent.state import persistence
    from utils.procurement_agent.state.approval_rules import DEFAULT_RULES

    persisted = persistence.list_approval_rules(facility_id)
    if persisted:
        rules = sorted(persisted, key=lambda r: r["threshold_usd"])
        ids = [r["id"] for r in rules]
    else:
        rules = sorted(DEFAULT_RULES, key=lambda r: r["threshold_usd"])
        ids = ["" for _ in rules]   # "" => not yet persisted; first save inserts the row

    out: List[ApprovalRuleOut] = []
    for i, r in enumerate(rules):
        nxt = rules[i + 1]["threshold_usd"] if i + 1 < len(rules) else None
        out.append(ApprovalRuleOut(
            id=ids[i],
            facility_id=facility_id,
            threshold=r["threshold_usd"],
            cap=(nxt - 1) if nxt is not None else None,
            approvers_required=int(r["approvers_required"]),
            approver_roles=list(r.get("approver_roles") or []),
            applies_to="buy",
        ))
    return out


@app.post("/api/approval-rules", response_model=ApprovalRuleOut, status_code=201)
def upsert_approval_rule(body: ApprovalRuleIn):
    """
    Create or update a single buy-approval tier for a facility, PERSISTED to the
    approval_rules table — so `determine_approval_path` reads the change on the next order
    (it actually governs routing; it is NOT echoed).

    Validation: threshold >= 0 and approvers_required >= 0 (a sane non-negative count).
    `cap` is display-only (derived on read) and not stored. `applies_to` other than "buy"
    is rejected — the store routes buys only. Pass `id` to update a tier in place; omit it
    (or send "") to insert a new one.
    """
    from utils.procurement_agent.state import persistence

    if body.threshold < 0:
        raise HTTPException(status_code=422, detail="threshold must be >= 0")
    if body.approvers_required < 0:
        raise HTTPException(status_code=422, detail="approvers_required must be >= 0")
    if body.applies_to != "buy":
        raise HTTPException(status_code=422, detail="only 'buy' approval rules are persisted")

    rule = persistence.upsert_approval_rule(
        facility_id=body.facility_id,
        threshold_usd=body.threshold,
        approvers_required=body.approvers_required,
        approver_roles=body.approver_roles,
        rule_id=body.id or None,
    )
    return ApprovalRuleOut(
        id=rule["id"],
        facility_id=rule["facility_id"],
        threshold=rule["threshold_usd"],
        cap=body.cap,
        approvers_required=int(rule["approvers_required"]),
        approver_roles=list(rule.get("approver_roles") or []),
        applies_to="buy",
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    # demo_mode lets the frontend derive the demo state from the backend it's actually talking
    # to (single source of truth) — used to gate transparency copy (e.g. drop the gated-off
    # Tier-1 "Arkim network" mention from the sourcing loader) so the UI matches what runs.
    body = {"status": "ok", "version": "1.0.0-phase1", "demo_mode": DEMO_MODE}
    # Night 1 — surface the capture-write failure counter ONLY when RUN_CAPTURE
    # is on, so the flag-off health response stays byte-identical (T5 inertness).
    # The existing test_health assertion (test_api_server.py) pins the flag-off
    # body and stays green untouched.
    if _run_capture.RUN_CAPTURE:
        body["capture_failures"] = _run_capture.capture_failures()
        # Night 2 — surface the label-write failure counter alongside capture's,
        # ONLY when the flag is on (same inertness wall: flag-off health stays
        # byte-identical — test_api_server.test_health pins the flag-off body).
        from utils import run_labels as _run_labels
        if _run_labels.RUN_CAPTURE:
            body["label_failures"] = _run_labels.label_failures()
    return body


@app.get("/api/debug/llm")
def debug_llm():
    """Smoke-test: confirms the API key loads and the LLM responds from this process."""
    import requests as _req
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set — .env not loaded"}
    try:
        resp = _req.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":    "claude-sonnet-4-6",
                "max_tokens": 20,
                "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"]
        return {"ok": True, "model": "claude-sonnet-4-6", "reply": text, "key_prefix": key[:14] + "..."}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "key_prefix": key[:14] + "..."}


@app.post("/api/dev/reseed-handoffs")
def dev_reseed_handoffs():
    """Delete seeded demo handoff runs and re-seed from fixture JSON. Dev/testing only."""
    try:
        with open(_HANDOFFS_PATH, encoding="utf-8") as f:
            handoffs = json.load(f)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="mock_maintenance_handoffs.json not found")

    seed_ids = {h.get("submission_id") for h in handoffs if h.get("submission_id")}

    with _SessionFactory() as session:
        deleted = 0
        for run in session.query(SourcingRunORM).all():
            if not run.maintenance_handoff_json:
                continue
            try:
                sid = json.loads(run.maintenance_handoff_json).get("submission_id")
            except (json.JSONDecodeError, AttributeError):
                continue
            if sid in seed_ids:
                session.delete(run)
                deleted += 1
        session.commit()

    _seed_demo_maintenance_run()
    return {"ok": True, "deleted": deleted, "reseeded": len(seed_ids)}


# ---------------------------------------------------------------------------
# Internal INSPECTOR / ADMIN surface (read-only, role-gated)
#
# REAL enforcement (not a UI toggle): every /api/admin/* endpoint depends on
# require_admin, which checks an admin bearer token against the server-side secret
# ARKIM_ADMIN_TOKEN. There is no login/session in this prototype yet (CLEANUP §6:
# RBAC unenforced), so possession of the admin token IS the admin role — the smallest
# real server-side gate. A non-admin caller cannot reach admin data even by calling
# the API directly. Fail-closed: if the server secret is unset, admin is DISABLED.
# Interim mechanism until real auth lands (noted in CLEANUP).
# ---------------------------------------------------------------------------

def require_admin(authorization: Optional[str] = Header(default=None)) -> str:
    """FastAPI dependency enforcing the admin bearer token.

      - server secret ARKIM_ADMIN_TOKEN unset            -> 503 (admin disabled, fail-closed)
      - no/!bearer Authorization header                  -> 401
      - token present but != the secret (non-admin)      -> 403
    Returns the role label on success. Constant-time compare to avoid token leakage.
    """
    server_token = os.environ.get("ARKIM_ADMIN_TOKEN") or ""
    if not server_token:
        raise HTTPException(status_code=503, detail="Admin surface disabled (ARKIM_ADMIN_TOKEN unset)")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing admin bearer token")
    presented = authorization[len("Bearer "):].strip()
    if not secrets.compare_digest(presented, server_token):
        raise HTTPException(status_code=403, detail="Admin role required")
    return "admin"


@app.get("/api/admin/ping")
def admin_ping(role: str = Depends(require_admin)):
    """Liveness probe proving the admin gate works (admin -> 200; else 401/403/503)."""
    return {"ok": True, "role": role}


# ---------------------------------------------------------------------------
# Admin/inspector READ-ONLY data endpoints (all require_admin).
# Debug surface: return FULL records (verbosity is the goal — do not trim fields).
# None of these mutate state; they call the stores' read accessors only.
# ---------------------------------------------------------------------------

@app.get("/api/admin/runs")
def admin_runs(role: str = Depends(require_admin)):
    """List sourcing runs (summary). Drill into one via /api/admin/runs/{id}."""
    from utils.procurement_agent.state import persistence
    runs = persistence.list_runs(limit=500)
    out = []
    for r in runs:
        specs = r.get("asset_specs_json") or {}
        part = " ".join(
            str(specs.get(k)) for k in ("manufacturer", "model", "part_number") if specs.get(k)
        ) or None
        out.append({
            "id": r["id"],
            "part": part,
            "phase": r.get("current_phase"),
            "facility_id": r.get("facility_id"),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        })
    return {"count": len(out), "runs": out}


@app.get("/api/admin/runs/{run_id}")
def admin_run_detail(run_id: str, role: str = Depends(require_admin)):
    """Full run record — every column incl. the sourcing_results candidates with all
    suitability / contact / apollo annotations (verbatim, untrimmed)."""
    from utils.procurement_agent.state import persistence
    run = persistence.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/admin/suppliers")
def admin_suppliers(role: str = Depends(require_admin)):
    """supplier_registry rows — all apollo_* / suitability / contact / primary fields,
    plus a computed needs_reenrichment staleness flag."""
    from utils import supplier_registry
    rows = supplier_registry.all_entries()
    for r in rows:
        r["needs_reenrichment"] = supplier_registry.needs_reenrichment(r)
    return {"count": len(rows), "suppliers": rows}


@app.get("/api/admin/sent-messages")
def admin_sent_messages(role: str = Depends(require_admin)):
    """sent_messages — recipients, status, thread/message ids, sent_at, approved_by."""
    from utils import supplier_registry
    rows = supplier_registry.get_sent_messages()
    return {"count": len(rows), "sent_messages": rows}


# ---------------------------------------------------------------------------
# Send-governance admin surface (SEND_GOVERNANCE_V1 — Night 10)
#
# Flag-gated: with SEND_GOVERNANCE_V1 off these routes 404 byte-identically to an
# unknown route (mirrors the intake-channels flag-off pattern), so flag-off parity
# holds at the API surface. All mutations carry actor identity and are audit-logged
# in utils/send_governance. NONE of this enables delivery — the allowlist can only
# ever BLOCK; EMAIL_SEND_ENABLED remains the untouched delivery gate.
# ---------------------------------------------------------------------------

def _require_send_governance_enabled():
    """Shared route gate: SEND_GOVERNANCE_V1 off -> 404 (route absent)."""
    from utils import send_governance
    if not send_governance.send_governance_active():
        raise HTTPException(status_code=404, detail="Not Found")


class AllowlistAddRequest(BaseModel):
    domain: str
    added_by: str
    note: Optional[str] = None


class AllowlistRemoveRequest(BaseModel):
    removed_by: str


@app.get("/api/admin/send-governance/allowlist")
def admin_allowlist_list(role: str = Depends(require_admin)):
    """The send allowlist (fail-closed: empty list ⇒ nothing can deliver)."""
    _require_send_governance_enabled()
    from utils import send_governance
    rows = send_governance.allowlist_list()
    return {"count": len(rows), "allowlist": rows}


@app.post("/api/admin/send-governance/allowlist", status_code=201)
def admin_allowlist_add(body: AllowlistAddRequest, role: str = Depends(require_admin)):
    """Allow sends to one supplier domain (audit-logged who/when). 422 on an
    unusable domain."""
    _require_send_governance_enabled()
    from utils import send_governance
    try:
        row = send_governance.allowlist_add(body.domain, added_by=body.added_by,
                                            note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return row


@app.post("/api/admin/send-governance/allowlist/{domain}/remove")
def admin_allowlist_remove(domain: str, body: AllowlistRemoveRequest,
                           role: str = Depends(require_admin)):
    """Remove one domain from the allowlist (audit-logged). 404 if not listed.
    POST (not DELETE) so the remover's identity travels in the body."""
    _require_send_governance_enabled()
    from utils import send_governance
    if not send_governance.allowlist_remove(domain, removed_by=body.removed_by):
        raise HTTPException(status_code=404, detail="Domain not on the allowlist")
    return {"domain": domain, "removed": True, "removed_by": body.removed_by}


class SuppressionAddRequest(BaseModel):
    domain: str
    added_by: str
    reason: Optional[str] = None


@app.get("/api/admin/send-governance/suppression")
def admin_suppression_list(role: str = Depends(require_admin)):
    """The suppression list ("supplier asked to stop") — beats the allowlist."""
    _require_send_governance_enabled()
    from utils import send_governance
    rows = send_governance.suppression_list()
    return {"count": len(rows), "suppression": rows}


@app.post("/api/admin/send-governance/suppression", status_code=201)
def admin_suppression_add(body: SuppressionAddRequest, role: str = Depends(require_admin)):
    """Suppress one domain (audit-logged who/when/why). Permanent until removed."""
    _require_send_governance_enabled()
    from utils import send_governance
    try:
        row = send_governance.suppression_add(body.domain, added_by=body.added_by,
                                              reason=body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return row


@app.post("/api/admin/send-governance/suppression/{domain}/remove")
def admin_suppression_remove(domain: str, body: AllowlistRemoveRequest,
                             role: str = Depends(require_admin)):
    """Lift a suppression (audit-logged). 404 if not suppressed."""
    _require_send_governance_enabled()
    from utils import send_governance
    if not send_governance.suppression_remove(domain, removed_by=body.removed_by):
        raise HTTPException(status_code=404, detail="Domain not suppressed")
    return {"domain": domain, "removed": True, "removed_by": body.removed_by}


# --- Release queue (T4): the concierge send-approval step ---------------------
#
# Phase-1 model (spec §3): an approved RFQ draft is never delivered directly — a
# human lists the pending drafts (full rendered content + recipients) and releases
# or rejects each. Release runs the FULL gate stack (suppression → allowlist →
# caps → EMAIL_SEND_ENABLED) via send_rfq → GmailSender; with the delivery gate
# off, a released draft records "stubbed" with releaser identity — the whole
# governance flow exercised end-to-end with zero deliveries. Batch = same
# endpoint, list of ids. A released-but-not-delivered draft stays 'approved'
# (claim-matches-reality: 'sent' means a message actually went) and therefore
# remains in the queue — deliberate for shadow mode, documented in the report.

def _release_part_key(run_id: str) -> Optional[str]:
    """Canonical part identity for a run's sends (the open-RFQ cap key).
    Fail-soft: a derivation error returns None (the daily cap still binds)."""
    try:
        from utils import known_parts
        with _SessionFactory() as session:
            run = session.get(SourcingRunORM, run_id)
            specs = json.loads(run.asset_specs_json) if run and run.asset_specs_json else {}
        return known_parts.canonical_part_key(
            specs.get("manufacturer"), specs.get("part_number")) or None
    except Exception:
        return None


class ReleaseRequest(BaseModel):
    draft_ids: List[str]
    released_by: str


class ReleaseRejectRequest(BaseModel):
    rejected_by: str


@app.get("/api/admin/send-governance/release-queue")
def admin_release_queue(role: str = Depends(require_admin)):
    """Pending (approved, not yet sent) RFQ drafts across all runs, oldest first,
    with the FULL rendered content + current recipient set — what the concierge
    reviews before releasing."""
    _require_send_governance_enabled()
    from utils.procurement_agent.state import persistence
    drafts = persistence.list_drafts_by_status("approved")
    for d in drafts:
        d["recipients"] = _draft_recipients(d, seed=False)
    return {"count": len(drafts), "pending": drafts}


@app.post("/api/admin/send-governance/release-queue/release")
def admin_release_drafts(body: ReleaseRequest, role: str = Depends(require_admin)):
    """Release one or more approved drafts (batch = list of ids). Each release
    runs the full gate stack; the sent_messages row records the releaser identity
    and timestamp alongside the outcome status (stubbed while the delivery gate is
    off; suppressed/not_allowlisted/cap_blocked when governance blocks). Per-draft
    results — one bad id never voids the rest of the batch."""
    _require_send_governance_enabled()
    from utils.procurement_agent.state import persistence
    from utils import rfq_send

    results = []
    for draft_id in body.draft_ids:
        draft = persistence.get_draft(draft_id)
        if draft is None:
            results.append({"draft_id": draft_id, "released": False,
                            "error": "Draft not found"})
            continue
        if draft["status"] != "approved":
            results.append({"draft_id": draft_id, "released": False,
                            "error": f"draft is '{draft['status']}' - only an approved "
                                     f"draft can be released"})
            continue
        approval = rfq_send.Approval(approved_by=draft["approved_by"],
                                     approved_at=draft["approved_at"])
        result = rfq_send.send_rfq(
            draft["candidate_snapshot"],
            draft["draft_body"],
            approval,
            run_id=draft["run_id"],
            part_key=_release_part_key(draft["run_id"]),
            released_by=body.released_by,
        )
        draft_status = draft["status"]
        if result.get("sent"):   # genuine delivery only (claim-matches-reality)
            persistence.transition_draft(draft_id, "sent",
                                         sent_message_id=result.get("sent_message_id"))
            draft_status = "sent"
        results.append({
            "draft_id": draft_id,
            "released": True,
            "released_by": body.released_by,
            "send_status": result.get("status"),
            "sent": bool(result.get("sent")),
            "draft_status": draft_status,
            "sent_message_id": result.get("sent_message_id"),
            "recipients": result.get("recipients"),
        })
    return {"released_by": body.released_by, "results": results}


@app.get("/api/admin/send-governance/digest")
def admin_send_digest(day: Optional[str] = None, role: str = Depends(require_admin)):
    """The daily-ritual digest (spec §4, 5 minutes): status-event counts for one
    UTC day (default today) + the detail lists — bounces, replies, anything
    blocked. Counts EVENTS from each row's status history, so a Monday release
    that bounces Tuesday shows in both days honestly. Read-only; endpoint-only
    by design (the ritual starts as curl/CLI, no UI)."""
    _require_send_governance_enabled()
    from utils import supplier_registry
    if day is not None and (len(day) != 10 or day[4] != "-" or day[7] != "-"):
        raise HTTPException(status_code=422, detail="day must be YYYY-MM-DD")
    digest = supplier_registry.sent_messages_digest(day)
    # Night 11 (QUOTE_SUBMIT_V1, spec §8): the digest grows a quotes section —
    # no new email surface. Submitted-that-day (info), review-flagged pending
    # (action), active quotes nearing expiry on an open request (info, ≤3 days).
    # Flag OFF ⇒ the key is absent and the digest is byte-identical. Fail-soft:
    # a quote-store error never breaks the send digest.
    if _quote_submit_enabled():
        try:
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            from utils import quote_store
            day_val = digest.get("day") or ""
            all_quotes = quote_store.get_quotes()
            def _brief(q: dict) -> dict:
                return {"quote_id": q["id"], "run_id": q.get("run_id"),
                        "vendor_name": q.get("vendor_name"),
                        "supplier_domain": q.get("supplier_domain"),
                        "unit_price": q.get("unit_price"),
                        "status": q["effective_status"],
                        "review_reasons": q.get("review_reasons") or [],
                        "submitted_via": q.get("submitted_via"),
                        "valid_until": q.get("valid_until")}
            soon = _dt.now(_tz.utc) + _td(days=3)
            expiring = []
            for q in all_quotes:
                if q["effective_status"] != "active" or not q.get("valid_until"):
                    continue
                try:
                    until = _dt.fromisoformat(q["valid_until"])
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=_tz.utc)
                except (ValueError, TypeError):
                    continue
                if until <= soon:
                    expiring.append(_brief(q))
            digest["quotes"] = {
                "submitted": [_brief(q) for q in all_quotes
                              if (q.get("submitted_at") or "")[:10] == day_val],
                "review_pending": [_brief(q) for q in all_quotes
                                   if q["effective_status"] == "review"],
                "expiring_soon": expiring,
            }
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "digest quotes section failed (send digest kept): %s", exc)
    return digest


@app.post("/api/admin/send-governance/release-queue/{draft_id}/reject")
def admin_release_reject(draft_id: str, body: ReleaseRejectRequest,
                         role: str = Depends(require_admin)):
    """Concierge decline at the release step: approved -> rejected (terminal).
    Governance-only transition (the legacy lifecycle keeps approved drafts
    unrejectable, byte-identical flag-off). 404 unknown; 409 not-approved."""
    _require_send_governance_enabled()
    from utils.procurement_agent.state import persistence
    if persistence.get_draft(draft_id) is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    try:
        draft = persistence.release_reject_draft(draft_id, rejected_by=body.rejected_by)
    except persistence.DraftTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"draft_id": draft["id"], "status": draft["status"],
            "rejected_by": draft["rejected_by"]}


@app.get("/api/admin/review-queue")
def admin_review_queue(role: str = Depends(require_admin)):
    """review_items — extracted quotes/contacts, confidence, status (incl.
    needs_human_review), raw source, the matched RFQ's run/domain/vendor.

    Excludes kind="unmatched_reply": those now have their own operator surface
    (/api/admin/unmatched-replies) and shouldn't be double-surfaced here."""
    from utils import supplier_registry
    rows = [r for r in supplier_registry.get_review_items() if r.get("kind") != "unmatched_reply"]
    return {"count": len(rows), "review_items": rows}


# Body snippet length for the unmatched-reply LIST (full body stays in payload for view).
_UNMATCHED_SNIPPET_LEN = 200


@app.get("/api/admin/unmatched-replies")
def admin_unmatched_replies(role: str = Depends(require_admin)):
    """Open inbound replies that did NOT auto-match an outbound RFQ
    (kind="unmatched_reply", status="needs_human_review") — the operator triage list.

    OPERATOR-ONLY: these rows have no tenant attribution (run_id / company_id absent),
    so they can't be customer-scoped. V1 is list + view + dismiss; manual attribution
    (linking to a run/candidate) is a later increment. Each item flattens the payload's
    sender/subject for the list and carries the FULL payload for the raw-JSON view."""
    from utils import supplier_registry
    rows = supplier_registry.get_review_items(kind="unmatched_reply", status="needs_human_review")
    items = []
    for r in rows:
        p = r.get("payload") or {}
        body = p.get("body") or ""
        snippet = (body[:_UNMATCHED_SNIPPET_LEN] + "…") if len(body) > _UNMATCHED_SNIPPET_LEN else body
        items.append({
            "id":            r.get("id"),
            "created_at":    r.get("created_at"),
            "thread_id":     r.get("thread_id"),
            "message_id":    r.get("message_id"),
            "sender":        p.get("sender"),
            "sender_domain": p.get("sender_domain"),
            "subject":       p.get("subject"),
            "snippet":       snippet,
            "status":        r.get("status"),
            "payload":       p,        # full payload -> the raw-JSON expand renders the body
        })
    return {"count": len(items), "unmatched_replies": items}


@app.post("/api/admin/unmatched-replies/{item_id}/dismiss")
def admin_dismiss_unmatched_reply(item_id: str, role: str = Depends(require_admin)):
    """Operator dismiss of an unmatched reply -> status "dismissed" + resolved_at.

    Status flip only — NO hard delete (the row stays in review_items, auditable).
    "dismissed" is distinct from "rejected" (which means an operator discarded an
    extracted quote/contact). 404 unknown; 422 if the row is not an unmatched_reply
    (this path must never touch quote/contact rows); 409 if already resolved."""
    from utils import supplier_registry
    item = supplier_registry.get_review_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Review item not found")
    if item.get("kind") != "unmatched_reply":
        raise HTTPException(status_code=422, detail="Item is not an unmatched_reply")
    if item.get("status") != "needs_human_review":
        raise HTTPException(status_code=409, detail=f"Item already {item.get('status')}")
    supplier_registry.set_review_item_status(item_id, "dismissed")
    return supplier_registry.get_review_item(item_id)


@app.get("/api/admin/orders")
def admin_orders(role: str = Depends(require_admin)):
    """orders — full lifecycle state + every captured field."""
    from utils import orders
    rows = orders.get_orders()
    return {"count": len(rows), "orders": rows}


@app.get("/api/admin/fulfilment-queue")
def admin_fulfilment_queue(role: str = Depends(require_admin)):
    """The operator fulfilment queue: orders awaiting a manual marketplace/supplier
    purchase (status="pending_manual_fulfilment"). Operator-only / global (require_admin),
    not tenant-scoped — admin is a cross-tenant ops surface. Mark one purchased via
    POST /api/admin/orders/{id}/mark-purchased (it then drops out of this list)."""
    from utils import orders
    rows = orders.get_orders(status=orders.STATUS_PENDING_FULFILMENT)
    return {"count": len(rows), "orders": rows}


class MarkPurchasedRequest(BaseModel):
    reference: Optional[str] = None


@app.post("/api/admin/orders/{order_id}/mark-purchased")
def admin_mark_purchased(order_id: str, body: MarkPurchasedRequest,
                         role: str = Depends(require_admin)):
    """Operator records that a pending_manual_fulfilment order was bought on the
    marketplace/supplier: appends the reference to notes and advances pending -> placed
    (the widened, price-gated place_order). Forward-only, auditable, no delete.

    A dedicated path because /api/admin/orders/{id}/status refuses the 'placed' target by
    design. placed_by is the constant "operator" — the admin surface has no authenticated
    per-user identity (bearer only), so we don't accept a body-supplied operator name.
    404 unknown; 422 if not pending_manual_fulfilment (won't touch drafts/placed/etc.);
    409 if place_order rejects (e.g. no price)."""
    from utils import orders
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.get("status") != orders.STATUS_PENDING_FULFILMENT:
        raise HTTPException(
            status_code=422,
            detail=f"Order is '{order.get('status')}', not pending_manual_fulfilment",
        )
    # Compose-don't-clobber: append the ref to any existing notes (mirror cancel_order).
    note = order.get("notes")
    ref = (body.reference or "").strip()
    if ref:
        ref_note = f"marketplace ref: {ref}"
        note = f"{note} | {ref_note}" if note else ref_note
    placed = orders.place_order(order_id, placed_by="operator", note=note)
    if placed is None:
        raise HTTPException(status_code=409,
                            detail="Cannot mark purchased (order has no price)")
    return placed


@app.get("/api/admin/prices")
def admin_prices(role: str = Depends(require_admin)):
    """price_db entries flattened (key, vendor, price, source live|rfq, lead, ...),
    plus the raw nested map."""
    from utils import price_db
    db = price_db.all_entries()
    items = []
    for key, vendors in (db or {}).items():
        for vendor, data in (vendors or {}).items():
            items.append({"key": key, "vendor": vendor, **(data or {})})
    return {"count": len(items), "prices": items, "raw": db}


# ---------------------------------------------------------------------------
# Night 2 — LABELING SURFACE (admin-gated, RUN_CAPTURE-gated, inert when off).
#
# Turns captured runs (Night 1 run_capture store) into labeled eval cases in
# <1 min of human time. Three endpoints, all require_admin (same 401/403/503
# semantics as the rest of /api/admin/*) AND gated on RUN_CAPTURE — when the
# flag is off they 503 (dormant), so the flag-off API is byte-identical (T6).
#
#   GET  /api/admin/labeling/queue        — failures-first queue via run_outcomes
#   GET  /api/admin/labeling/runs/{id}    — a run's input/intake/candidates for labeling
#   POST /api/admin/labeling/label        — append a label (run or candidate scope)
#   POST /api/admin/labeling/export       — export labeled runs → eval datasets
#   GET  /api/admin/labeling/provenance   — T5 % real vs synthetic per suite
# ---------------------------------------------------------------------------

def _require_labeling_enabled():
    """RUN_CAPTURE must be on for the labeling surface to be live. Mirrors the
    require_admin fail-closed pattern: flag off -> 503 (dormant), never 404-ish
    pretend-not-here. Returns None on success, else raises."""
    from utils import run_labels as _run_labels
    if not _run_labels.RUN_CAPTURE:
        raise HTTPException(status_code=503,
                            detail="Labeling disabled (RUN_CAPTURE off)")
    return _run_labels


# Failures-first ordering of run_outcomes (I4). Outcome statuses from
# run_capture.py:324-329. We deprioritize completed_with_action; everything else
# (abandoned / zero_results / all_rejected / incomplete / rephrased) is a
# labeling candidate (the run is where the model most needs ground truth).
_OUTCOME_RANK = {
    "abandoned_after_results": 0,
    "all_rejected": 1,
    "zero_results": 2,
    "incomplete": 3,
    "rephrased": 4,
    "completed_with_action": 5,
}


@app.get("/api/admin/labeling/queue")
def admin_labeling_queue(role: str = Depends(require_admin)):
    """Failures-first queue of captured runs awaiting labels.

    Joins run_outcomes (run_capture) with the run record (persistence) for a
    compact queue row: run_id, outcome, part summary, phase, created_at, and
    whether the run already has a run-scope label. Failures-first: outcomes
    other than completed_with_action sort first.
    """
    _require_labeling_enabled()
    from utils import run_capture as rc, run_labels as rl
    from utils.procurement_agent.state import persistence
    runs = persistence.list_runs(limit=500)
    out = []
    for r in runs:
        rid = r["id"]
        outcome_row = rc.read_outcome(rid)
        outcome = outcome_row["outcome"] if outcome_row else rc.compute_outcome(rid)
        specs = r.get("asset_specs_json") or {}
        part = " ".join(
            str(specs.get(k)) for k in ("manufacturer", "model", "part_number") if specs.get(k)
        ) or None
        has_intake_label = rl.current_label(rid, rl.SCOPE_RUN) is not None
        out.append({
            "id": rid,
            "outcome": outcome,
            "part": part,
            "phase": r.get("current_phase"),
            "created_at": r.get("created_at"),
            "labeled": has_intake_label,
        })
    # Failures-first: lower rank sorts first; ties break by created_at desc.
    # created_at is an ISO string — reverse-sort by negating the string only
    # when present (None sorts last via a sentinel).
    out.sort(key=lambda x: (_OUTCOME_RANK.get(x["outcome"], 9),
                            0 if x.get("created_at") else 1,
                            tuple(-ord(c) for c in (x.get("created_at") or ""))))
    return {"count": len(out), "queue": out}


@app.get("/api/admin/labeling/runs/{run_id}")
def admin_labeling_run_detail(run_id: str, role: str = Depends(require_admin)):
    """A run's labeling view: the first user turn (intake input), the intake
    result, and every captured candidate with its score + verdict + the current
    label (if any). This is what the labeling UI renders."""
    _require_labeling_enabled()
    from utils import run_capture as rc, run_labels as rl
    from utils.procurement_agent.state import persistence
    run = persistence.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    events = rc.read_events(run_id)
    # First user turn = the input the live intake classifier consumed.
    first_user = None
    for e in events:
        if e.get("event_type") == "turn_user":
            first_user = (e.get("payload") or {}).get("content")
            break
    intake_result = None
    for e in events:
        if e.get("event_type") == "intake_result":
            intake_result = e.get("payload")
            break
    # Candidates — scored + rejected, with the current per-candidate label.
    candidates = []
    for e in events:
        et = e.get("event_type")
        if et in ("candidate_scored", "candidate_rejected"):
            p = e.get("payload") or {}
            ref = p.get("candidate_id")
            cur = rl.current_label(run_id, rl.SCOPE_CANDIDATE,
                                   candidate_ref=ref) if ref else None
            candidates.append({
                "candidate_id": ref,
                "tier": p.get("tier"),
                "vendor_name": p.get("vendor_name"),
                "suitability_score": p.get("suitability_score"),
                "rejection_reason": p.get("rejection_reason"),
                "pn_match_status": p.get("pn_match_status"),
                "match_type": p.get("match_type"),
                "verdict": "rejected" if et == "candidate_rejected" else "scored",
                "label": (cur or {}).get("label"),
            })
    run_label = rl.current_label(run_id, rl.SCOPE_RUN)
    return {
        "run_id": run_id,
        "asset_specs": run.get("asset_specs_json"),
        "phase": run.get("current_phase"),
        "first_user_turn": first_user,
        "intake_result": intake_result,
        "candidates": candidates,
        "run_label": (run_label or {}).get("label"),
        "provenance": f"real:{run_id}",
    }


class LabelRequest(BaseModel):
    run_id: str
    scope: str            # "run" | "candidate"
    candidate_ref: Optional[str] = None
    label: Dict[str, Any]
    note: Optional[str] = None
    labeled_by: Optional[str] = None


@app.post("/api/admin/labeling/label")
def admin_labeling_label(body: LabelRequest, role: str = Depends(require_admin)):
    """Append a label (run or candidate scope). Append-only — a relabel adds a
    new row; current_label returns the latest. The role label is the labeler
    identity (admin token possession IS the admin role — §6)."""
    _require_labeling_enabled()
    from utils import run_labels as rl
    if body.scope not in (rl.SCOPE_RUN, rl.SCOPE_CANDIDATE):
        raise HTTPException(status_code=422, detail="scope must be 'run' or 'candidate'")
    if body.scope == rl.SCOPE_CANDIDATE and not body.candidate_ref:
        raise HTTPException(status_code=422,
                            detail="candidate_ref required for candidate scope")
    payload = dict(body.label)
    if body.note is not None:
        payload["note"] = body.note
    rl.write_label(
        body.run_id, body.scope, payload,
        candidate_ref=body.candidate_ref,
        labeled_by=body.labeled_by or role,
    )
    cur = rl.current_label(body.run_id, body.scope, candidate_ref=body.candidate_ref)
    return {"ok": True, "current": (cur or {}).get("label"),
            "label_id": (cur or {}).get("label_id")}


@app.post("/api/admin/labeling/export")
def admin_labeling_export(role: str = Depends(require_admin)):
    """Export labeled runs into the eval datasets (intake + scoring). Live-faithful:
    intake input = first user turn; scoring cases withheld when snippet/title
    not captured (Night 1 gap — logged). Returns per-suite counts + withholds."""
    _require_labeling_enabled()
    from utils import run_capture as rc, run_labels as rl
    from utils import eval_export as ex
    from utils.procurement_agent.state import persistence

    run_ids = rl.labeled_run_ids()
    intake_emitted = 0
    intake_withheld = 0
    scoring_emitted = 0
    scoring_withheld = 0
    withhold_reasons: list[str] = []

    for rid in run_ids:
        events = rc.read_events(rid)
        run = persistence.get_run(rid) or {}
        specs = run.get("asset_specs_json") or {}
        # Intake case from the run-scope label.
        run_label_row = rl.current_label(rid, rl.SCOPE_RUN)
        if run_label_row:
            first_user = None
            for e in events:
                if e.get("event_type") == "turn_user":
                    first_user = (e.get("payload") or {}).get("content")
                    break
            case = ex.build_intake_case(rid, first_user or "",
                                        run_label_row["label"])
            if case:
                ex.append_intake_case(ex.INTAKE_REAL_DATASET, case)
                intake_emitted += 1
            else:
                intake_withheld += 1
                withhold_reasons.append(
                    f"intake:{rid} — missing first user turn or invalid ground-truth label")
        # Scoring cases from candidate-scope labels.
        for e in events:
            if e.get("event_type") != "candidate_scored":
                continue
            p = e.get("payload") or {}
            ref = p.get("candidate_id")
            if not ref:
                continue
            crow = rl.current_label(rid, rl.SCOPE_CANDIDATE, candidate_ref=ref)
            if not crow:
                continue
            # Reconstruct the request (AssetSpecs) from the run's specs.
            request = {
                "manufacturer": specs.get("manufacturer") or "",
                "model": specs.get("model") or "",
                "part_number": specs.get("part_number") or "UNKNOWN-PN",
                "voltage": specs.get("voltage") or "N/A",
                "category": specs.get("category") or "Part",
                "detected_type": specs.get("detected_type") or "",
                "hp": specs.get("hp"),
            }
            # result: url + found_pn are recoverable from sourcing_results_json;
            # snippet + title are NOT durable (Night 1 gap) -> build_scoring_case
            # withholds unless a future capture extension supplies them.
            cand = _find_candidate_in_results(run.get("sourcing_results_json"), ref)
            result = {
                "snippet": (cand or {}).get("snippet"),
                "url": (cand or {}).get("source_url") or (cand or {}).get("url"),
                "title": (cand or {}).get("title"),
                "found_pn": (cand or {}).get("found_part_number")
                            or (cand or {}).get("found_pn"),
            }
            case = ex.build_scoring_case(rid, ref, request, result, crow["label"])
            if case:
                ex.append_scoring_case(ex.SCORING_REAL_DATASET, case)
                scoring_emitted += 1
            else:
                scoring_withheld += 1
                withhold_reasons.append(
                    f"scoring:{rid}:{ref} — snippet/title not captured (Night 1 gap)")

    return {
        "ok": True,
        "runs": len(run_ids),
        "intake": {"emitted": intake_emitted, "withheld": intake_withheld},
        "scoring": {"emitted": scoring_emitted, "withheld": scoring_withheld},
        "withhold_reasons": withhold_reasons,
        "intake_dataset": ex.INTAKE_REAL_DATASET,
        "scoring_dataset": ex.SCORING_REAL_DATASET,
    }


def _find_candidate_in_results(sourcing_results_json, candidate_ref: str):
    """Locate a candidate by its candidate_id in the persisted sourcing_results
    blob. The candidate_id is '<vendor_name>-t<n>-<idx>' (api_server:1264)."""
    if not sourcing_results_json:
        return None
    try:
        result = (sourcing_results_json if isinstance(sourcing_results_json, dict)
                  else json.loads(sourcing_results_json))
    except Exception:
        return None
    for tier_key, tier_n in (("tier_1", 1), ("tier_2", 2), ("tier_3", 3)):
        for idx, c in enumerate(result.get(tier_key, {}).get("results", []) or []):
            cid = f"{c.get('vendor_name', '')}-t{tier_n}-{idx}"
            if cid == candidate_ref:
                return c
    return None


@app.get("/api/admin/labeling/provenance")
def admin_labeling_provenance(role: str = Depends(require_admin)):
    """T5 — % real vs synthetic per eval suite (intake + scoring)."""
    _require_labeling_enabled()
    from utils import eval_export as ex
    return ex.provenance_report()


# ---------------------------------------------------------------------------
# Order lifecycle endpoints (wires utils/orders.py via ProcurementAgent).
#
# Buyer/run-scoped actions (execute, mark-delivered, list) follow the existing
# ungated run-endpoint convention (no user auth yet — CLEANUP §4.1). The ops-only
# mutations (arbitrary status transition, cancel) are admin-gated (require_admin) so
# they aren't open. No external actions (no payment/PO/email/Apollo).
# ---------------------------------------------------------------------------

class OrderStatusRequest(BaseModel):
    status: str


class OrderCancelRequest(BaseModel):
    reason: Optional[str] = None


def _run_model_for(run_id: str):
    """Build a SourcingRun dataclass from the persisted run, or None if absent."""
    import dataclasses
    from utils.procurement_agent.state import persistence
    d = persistence.get_run(run_id)
    if d is None:
        return None
    fields = {f.name for f in dataclasses.fields(SourcingRun)}
    return SourcingRun(**{k: v for k, v in d.items() if k in fields})


def _persist_order_on_run(run_id: str, order: Optional[dict]) -> None:
    """Persist the captured order's id/status back onto the run (vendor_order_id /
    fulfillment_status — the existing placeholder columns)."""
    if not order:
        return
    with _SessionFactory() as session:
        row = session.get(SourcingRunORM, run_id)
        if row:
            row.vendor_order_id = order.get("id")
            row.fulfillment_status = order.get("status")
            row.updated_at = datetime.now(timezone.utc)
            session.commit()


@app.post("/api/runs/{run_id}/execute")
def execute_order(run_id: str):
    """Confirmed commit (post-approval): capture + place a durable order from the
    approved selection. Run-scoped (matches approve/select). No external actions."""
    from utils.procurement_agent.agents.procurement_agent import ProcurementAgent
    run_model = _run_model_for(run_id)
    if run_model is None:
        raise HTTPException(status_code=404, detail="Run not found")
    result = ProcurementAgent().run(run_model, "execute")
    _persist_order_on_run(run_id, result.get("order"))
    return result


@app.post("/api/runs/{run_id}/mark-delivered")
def mark_delivered(run_id: str):
    """Confirm receipt — advances the run's order to received via the state machine."""
    from utils.procurement_agent.agents.procurement_agent import ProcurementAgent
    run_model = _run_model_for(run_id)
    if run_model is None:
        raise HTTPException(status_code=404, detail="Run not found")
    result = ProcurementAgent().run(run_model, "mark_delivered")
    _persist_order_on_run(run_id, result.get("order"))
    return result


@app.get("/api/runs/{run_id}/orders")
def list_run_orders(run_id: str):
    """Orders captured for this run (run-scoped view)."""
    from utils import orders
    rows = orders.get_orders(run_id=run_id)
    return {"run_id": run_id, "count": len(rows), "orders": rows}


@app.get("/api/orders")
def list_all_orders():
    """All captured orders — the customer History feed (orders table, spend, supplier
    reliability, price intelligence). Ungated like the run-scoped buyer-loop endpoints
    (CLEANUP §4.1); binds to the tenant/buyer when real auth lands. Distinct from the
    admin-gated /api/admin/orders."""
    from utils import orders
    rows = orders.get_orders()
    return {"count": len(rows), "orders": rows}


@app.get("/api/reorder")
def list_reorder():
    """Reorder intelligence — parts due to be reordered, forecast from the customer's OWN
    order history (cadence from repeat purchases; never external data). Ungated like the
    History feed."""
    from utils import reorder
    items = reorder.gather_reorder()
    return {"count": len(items), "reorder": items}


# ---------------------------------------------------------------------------
# Derived notification feed (read-only) — GET /api/events
#
# V1 surfaces REAL state changes the viewer can see, DERIVED from existing persisted rows
# — there is NO notifications table and NO migration. It is UNTARGETED: no reliable
# per-user identity exists on a run/order yet (initiated_by_user_id is never populated;
# company_id is NULL in the no-auth demo), so this lists events across ALL runs and never
# claims a specific person was notified. Per-user targeting + read-state + email are later
# increments gated on auth (pairs with D2). Honesty: every event reflects the actual
# current row (an order shows "shipped" only when status == shipped) — no optimistic notice.
# ---------------------------------------------------------------------------

class EventOut(BaseModel):
    id: str                          # stable per (source row, state, timestamp)
    type: str                        # "order_status" | "approval" | "quote_confirmed"
    run_id: Optional[str] = None
    order_id: Optional[str] = None
    title: str
    timestamp: Optional[str] = None  # the real updated_at / acted_at / resolved_at


class EventsResponse(BaseModel):
    count: int
    events: List[EventOut]


# Order status -> human notice. "draft" is intentionally absent (a no-price placeholder,
# not a user-facing notice) so it is skipped.
_ORDER_STATUS_TITLES: dict[str, str] = {
    "pending_manual_fulfilment": "Order is being purchased",
    "placed":    "Order placed",
    "confirmed": "Order confirmed by supplier",
    "shipped":   "Order shipped",
    "received":  "Order delivered",
    "cancelled": "Order cancelled",
}


def _derive_events(limit: int = 50) -> list[dict]:
    """Derive a newest-first event list from existing persisted state — order statuses, run
    approval phase/history, and confirmed quotes. Read-only; no new table. Untargeted (all
    runs). REAL-state-only. Fail-soft PER SOURCE so one bad read can't sink the feed."""
    import logging
    from utils import orders as orders_mod
    from utils.procurement_agent.state import persistence
    from utils import supplier_registry

    log = logging.getLogger(__name__)
    events: list[dict] = []

    # 1) Orders — one event per order at its CURRENT status (V1 tracks current status +
    #    updated_at, not per-status history).
    try:
        all_orders = orders_mod.get_orders()
    except Exception as exc:
        log.warning("[events] get_orders failed: %s", exc)
        all_orders = []
    # Runs that already have a (non-draft) order — used to suppress a now-redundant
    # "Approved" approval event once the spend has moved on to a real order.
    ordered_run_ids = {
        o.get("run_id") for o in all_orders
        if o.get("status") and o.get("status") != orders_mod.STATUS_DRAFT
    }
    for o in all_orders:
        title = _ORDER_STATUS_TITLES.get(o.get("status"))
        if not title:
            continue
        ts = o.get("updated_at")
        events.append({
            "id": f"order:{o.get('id')}:{o.get('status')}:{ts}",
            "type": "order_status",
            "run_id": o.get("run_id"),
            "order_id": o.get("id"),
            "title": title,
            "timestamp": ts,
        })

    # 2) Runs — one approval-lifecycle event per run, from the CURRENT phase, plus the last
    #    approval_history entry for a rejection (which phase can't show: reject returns the
    #    run to comparison). At most one per run; always the honest current state.
    try:
        runs = persistence.list_runs(limit=200)
    except Exception as exc:
        log.warning("[events] list_runs failed: %s", exc)
        runs = []
    for r in runs:
        rid = r.get("id")
        phase = r.get("current_phase")
        ts = r.get("updated_at")
        title = None
        if phase == Phase.PENDING_FIRST_APPROVAL.value:
            title = "Awaiting approval"
        elif phase == Phase.PENDING_SECOND_APPROVAL.value:
            title = "Awaiting second approval"
        elif phase == Phase.APPROVED.value and rid not in ordered_run_ids:
            title = "Approved"
        else:
            hist = r.get("approval_history_json") or []
            last = hist[-1] if isinstance(hist, list) and hist else None
            if isinstance(last, dict) and last.get("action") == "rejected":
                title = "Rejected — re-pick"
                ts = last.get("acted_at") or ts
        if title:
            events.append({
                "id": f"approval:{rid}:{phase}:{ts}",
                "type": "approval",
                "run_id": rid,
                "order_id": None,
                "title": title,
                "timestamp": ts,
            })

    # 3) Confirmed quotes (State C) — a supplier's quote a human confirmed.
    try:
        quotes = supplier_registry.get_review_items(kind="quote", status="confirmed")
    except Exception as exc:
        log.warning("[events] get_review_items failed: %s", exc)
        quotes = []
    for q in quotes:
        ts = q.get("resolved_at") or q.get("created_at")
        vendor = q.get("vendor_name")
        events.append({
            "id": f"quote:{q.get('id')}:{ts}",
            "type": "quote_confirmed",
            "run_id": q.get("run_id"),
            "order_id": None,
            "title": f"{vendor} quoted your part" if vendor else "Supplier quoted your part",
            "timestamp": ts,
        })

    # Newest-first. Timestamps are ISO-8601 UTC (same format across sources), so a string
    # sort is chronological; a missing timestamp sorts last.
    events.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
    return events[:limit]


@app.get("/api/events", response_model=EventsResponse)
def list_events(limit: int = 50):
    """Derived, untargeted notification feed (read-only): REAL state changes (order
    statuses, approval decisions, confirmed quotes) shaped from existing rows. No table, no
    per-user targeting (no verified identity exists yet), no writes. Fail-soft: returns an
    empty list rather than 500-ing the shell."""
    import logging
    try:
        events = _derive_events(limit=limit)
    except Exception as exc:
        logging.getLogger(__name__).warning("[events] derive failed: %s", exc)
        events = []
    return {"count": len(events), "events": events}


# ---------------------------------------------------------------------------
# Basket status rollup (read-only) — GET /api/groups/{group_id}
#
# Aggregates the N runs sharing a group_id into one basket view. Mirrors the _derive_events
# discipline: computed on read, no writes, no new table, fail-soft per row. Single-part runs
# (group_id NULL) are never returned — they are not baskets. The amount helpers below are the
# SINGLE source of a run's/basket's money: Stage 5's approval gate ROUTES on exactly what
# this endpoint DISPLAYS, by calling the same helpers on the same field.
# ---------------------------------------------------------------------------

_UNIDENTIFIED_PART = "Unidentified — intake in progress"


def _run_selected_amount(run: dict) -> float:
    """A run's selected line amount (USD) — THE field Stage 5 routes on:
    selected_candidate_json._approval_path.grand_total_usd. No selection yet, or anything
    malformed, contributes 0.0 — never a fabricated amount. Fully defensive (never raises)."""
    sel = run.get("selected_candidate_json")
    if not isinstance(sel, dict):
        return 0.0
    path = sel.get("_approval_path")
    if not isinstance(path, dict):
        return 0.0
    try:
        return float(path.get("grand_total_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _basket_total(runs: list[dict]) -> float:
    """The basket total — the sum Stage 5's gate routes on. Defined as the sum of the SAME
    per-run helper this endpoint displays, so display and routing can never diverge."""
    return sum(_run_selected_amount(r) for r in runs)


def _run_part_label(run: dict) -> Optional[str]:
    """Human part label from the run's asset_specs — NEVER invented. Returns None when no
    part has been identified yet (intake in progress), so the caller shows a placeholder."""
    specs = run.get("asset_specs_json")
    if not isinstance(specs, dict):
        return None
    mfr = (specs.get("manufacturer") or "").strip()
    model = (specs.get("model") or "").strip()
    pn = (specs.get("part_number") or "").strip()
    label = " ".join(p for p in (mfr, model or pn) if p).strip()
    return label or None


def _basket_status(child_phases: list[str]) -> str:
    """A small, honest basket-level status derived from the children's phases. Mixed phases
    are reported as 'mixed' — never papered over."""
    phases = {p for p in child_phases if p}
    if not phases:
        return "empty"
    if Phase.ERROR.value in phases:
        return "has_errors"
    if phases <= {Phase.PENDING_INTAKE.value, Phase.INTAKE.value}:
        return "all_intake"
    if phases <= {Phase.INVENTORY.value, Phase.SOURCING.value, Phase.COMPARISON.value}:
        return "sourcing_in_progress"
    if phases <= {Phase.PENDING_FIRST_APPROVAL.value, Phase.PENDING_SECOND_APPROVAL.value}:
        return "all_awaiting_approval"
    if phases <= {Phase.APPROVED.value, Phase.EXECUTING.value, Phase.FULFILLING.value, Phase.COMPLETED.value}:
        return "all_committed"
    return "mixed"


class BasketRunRow(BaseModel):
    run_id: Optional[str] = None
    part: Optional[str] = None            # label or placeholder; None only on a degraded row
    phase: Optional[str] = None
    selected_amount: float = 0.0          # 0.0 until a candidate is selected (never faked)
    error: Optional[str] = None           # set when this row degraded (fail-soft), else None


class BasketRollup(BaseModel):
    group_id: str
    status: str
    basket_total: float
    run_count: int
    runs: List[BasketRunRow]


@app.get("/api/groups/{group_id}", response_model=BasketRollup)
def get_group(group_id: str, request: Request):
    """Read-only basket rollup over the runs sharing `group_id`: per-run part/phase/selected
    amount, a derived basket status, and the basket_total (the exact figure Stage 5 routes
    on). No writes. Fail-soft: a malformed child degrades to an error row, never 500-ing the
    basket. Unknown group -> 404.

    DEMO_MODE session isolation: under DEMO_MODE the basket is scoped to the requesting
    visitor's session by FILTERING the group's runs down to those whose session_id matches
    the X-Session-Id. A visitor therefore only ever sees their OWN runs in any basket — an
    attacker who learns a victim's group_id and even injects their own run into it still
    sees only their own run, never the victim's (no leak, no DoS: the victim's own basket
    view excludes the attacker's injected run). Empty after filtering -> 404, NOT 403 (no
    existence oracle). Inert when DEMO_MODE is off (no filtering — today's behaviour)."""
    import logging
    from utils.procurement_agent.state import persistence
    log = logging.getLogger(__name__)
    demo_sid = _demo_session_id_from_request(request)
    try:
        runs = persistence.list_runs(group_id=group_id, limit=500)
    except Exception as exc:
        log.warning("[groups] list_runs failed for %s: %s", group_id, exc)
        runs = []
    if DEMO_MODE:
        # Scope to this visitor's runs only. A NULL-session run (seeded/legacy) is never
        # owned by a demo visitor, so it is filtered out too.
        runs = [r for r in runs if r.get("session_id") and r.get("session_id") == demo_sid]
    if not runs:
        raise HTTPException(status_code=404, detail="Basket not found")

    rows: List[BasketRunRow] = []
    for run in runs:
        try:
            rows.append(BasketRunRow(
                run_id=run.get("id"),
                part=_run_part_label(run) or _UNIDENTIFIED_PART,
                phase=run.get("current_phase"),
                selected_amount=_run_selected_amount(run),
            ))
        except Exception as exc:  # one bad row degrades, the basket still returns
            log.warning("[groups] row degraded for run %s: %s", run.get("id"), exc)
            rows.append(BasketRunRow(run_id=run.get("id"), phase=run.get("current_phase"), error=str(exc)))

    # basket_total comes from the shared helper over the raw runs (the Stage-5 routing figure),
    # so it stays correct even if a display row degraded.
    return BasketRollup(
        group_id=group_id,
        status=_basket_status([r.get("current_phase") for r in runs]),
        basket_total=_basket_total(runs),
        run_count=len(rows),
        runs=rows,
    )


# ---------------------------------------------------------------------------
# Basket-total approval rollup (multi-part Increment 1, Stage 5)
#
# The crux: the basket routes approval on the BASKET TOTAL, ONCE. A sum that crosses a
# threshold requires that threshold's approver count even when every individual line is
# sub-threshold (3x$2k = $6k needs 2 approvers though each $2k line alone needs 1). This is
# NOT N per-run approvals — a sub-threshold line cannot self-approve out of the basket gate.
# Children advance only via the legal transition under the ONE basket decision.
# ---------------------------------------------------------------------------

def _advance_run_to_approved(
    run: SourcingRunORM, *, basket_approval_id: str, basket_total: float, approver_names: list[str],
) -> None:
    """Advance ONE child run pending_first_approval -> approved through the LEGAL transition
    (validate_transition), under the single basket decision. The child's OWN
    _approval_path.approvers_required does NOT govern here — the basket total already did.
    The child records an approval_history entry that REFERENCES the basket decision rather
    than fabricating an independent per-child human approval."""
    from utils.procurement_agent.state.phases import validate_transition
    current = Phase(run.current_phase)
    if not validate_transition(current, Phase.APPROVED):
        raise HTTPException(status_code=409, detail=f"run {run.id} not awaiting approval (phase {current.value})")
    now = datetime.now(timezone.utc)
    history = json.loads(run.approval_history_json) if run.approval_history_json else []
    history.append({
        "sequence": len(history) + 1,
        "action": "approved",
        "approver_name": "(basket decision)",
        "approver_role": "basket",
        "notes": f"Authorised by basket approval {basket_approval_id} — basket total "
                 f"${basket_total:,.2f}; approver(s): {', '.join(approver_names)}",
        "basket_approval_id": basket_approval_id,
        "acted_at": now.isoformat(),
    })
    run.approval_history_json = json.dumps(history)
    run.current_phase = Phase.APPROVED.value
    run.updated_at = now


@app.post("/api/groups/{group_id}/approve")
def approve_group(group_id: str, body: ApproveRequest, caller: Optional[Caller] = Depends(get_caller)):
    """Approve a basket on the BASKET TOTAL — routed ONCE via determine_approval_path. Gathers
    the required number of approvals against the basket record; only when met does it advance
    EVERY child pending_first_approval -> approved via the legal transition. Children never go
    through the per-run /approve, and no child's own _approval_path governs the basket gate."""
    from utils.procurement_agent.state import persistence
    from utils.procurement_agent.state.approval_rules import determine_approval_path

    runs = persistence.list_runs(group_id=group_id, limit=500)
    if not runs:
        raise HTTPException(status_code=404, detail="Basket not found")
    if not all(r.get("current_phase") == Phase.PENDING_FIRST_APPROVAL.value for r in runs):
        raise HTTPException(status_code=409, detail="Basket not ready — every part must be selected and awaiting approval.")

    # Route ONCE on the basket total, via the SAME helper Stage 4 displays (display == gate).
    basket_total = _basket_total(runs)
    facility_id = runs[0].get("facility_id") or "00000000-0000-0000-0000-000000000000"
    approvers_required, _roles = determine_approval_path(facility_id, basket_total)
    approver_id = caller.user_id if caller else None

    with _SessionFactory() as session:
        rec = session.query(RequestGroupApprovalORM).filter_by(group_id=group_id).first()
        if rec is None:
            rec = RequestGroupApprovalORM(
                id=str(uuid.uuid4()), group_id=group_id, facility_id=facility_id,
                basket_total=basket_total, approvers_required=approvers_required,
                approvals_received_json="[]", status="pending_first",
            )
            session.add(rec)
        if rec.status in ("approved", "rejected"):
            raise HTTPException(status_code=409, detail=f"Basket already {rec.status}.")

        received = json.loads(rec.approvals_received_json or "[]")
        # M1-style distinct approver: enforced only on a verified identity (no-auth demo skips).
        if approver_id is not None and approver_id in {a.get("approver_id") for a in received}:
            raise HTTPException(status_code=409, detail="A second, distinct approver is required — you have already approved this basket.")
        received.append({
            "approver_id": approver_id,
            "approver_name": body.approver_name,
            "approver_role": body.approver_role,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        # Re-route on the current total each call (a child's selection may have changed).
        rec.basket_total = basket_total
        rec.approvers_required = approvers_required
        rec.approvals_received_json = json.dumps(received)

        if len(received) >= approvers_required:
            rec.status = "approved"
            approver_names = [a.get("approver_name") for a in received]
            for r in runs:
                child = session.get(SourcingRunORM, r["id"])
                if child is not None:
                    _advance_run_to_approved(
                        child, basket_approval_id=rec.id, basket_total=basket_total, approver_names=approver_names,
                    )
        elif approvers_required >= 2 and len(received) == 1:
            rec.status = "pending_second"
        else:
            rec.status = "pending_first"
        rec.updated_at = datetime.now(timezone.utc)
        session.commit()
        result = {
            "group_id": group_id,
            "status": rec.status,
            "approvals_received": len(received),
            "approvers_required": approvers_required,
            "basket_total": basket_total,
        }
    return result


@app.post("/api/groups/{group_id}/reject")
def reject_group(group_id: str, body: RejectRequest, caller: Optional[Caller] = Depends(get_caller)):
    """Reject a basket — non-terminal: returns EVERY child to comparison (re-pick), clears
    each selection, commits NO order. Mirrors the per-run reject (a backward reset) at basket
    scope, and records the rejection on the basket record + each child's history."""
    from utils.procurement_agent.state import persistence

    runs = persistence.list_runs(group_id=group_id, limit=500)
    if not runs:
        raise HTTPException(status_code=404, detail="Basket not found")
    now = datetime.now(timezone.utc)

    with _SessionFactory() as session:
        rec = session.query(RequestGroupApprovalORM).filter_by(group_id=group_id).first()
        if rec is None:  # rejecting a basket never approve-touched — still record the decision
            facility_id = runs[0].get("facility_id") or "00000000-0000-0000-0000-000000000000"
            rec = RequestGroupApprovalORM(
                id=str(uuid.uuid4()), group_id=group_id, facility_id=facility_id,
                basket_total=_basket_total(runs), approvers_required=0,
                approvals_received_json="[]", status="pending_first",
            )
            session.add(rec)
        if rec.status in ("approved", "rejected"):
            raise HTTPException(status_code=409, detail=f"Basket already {rec.status}.")
        rec.status = "rejected"
        rec.updated_at = now

        for r in runs:
            child = session.get(SourcingRunORM, r["id"])
            if child is None:
                continue
            history = json.loads(child.approval_history_json) if child.approval_history_json else []
            history.append({
                "sequence": len(history) + 1,
                "action": "rejected",
                "approver_name": body.approver_name,
                "approver_role": body.approver_role,
                "notes": f"Basket rejected ({rec.id}): {body.notes}",
                "basket_approval_id": rec.id,
                "acted_at": now.isoformat(),
            })
            child.approval_history_json = json.dumps(history)
            child.selected_candidate_json = None
            child.current_phase = Phase.COMPARISON.value
            child.updated_at = now
        session.commit()
    return {"group_id": group_id, "status": "rejected", "phase": Phase.COMPARISON.value}


# ---------------------------------------------------------------------------
# RFQ drafts — create / review / approve / reject endpoints (RFQ wiring A1)
#
# The separated HITL flow: an endpoint creates a reviewable draft (server-side hydrated,
# frozen), a human reads it back (GET), then approves or rejects it against the STORED draft.
# NONE of these endpoints send: there is no send_rfq call, no email, no Apollo anywhere here.
# The send (A2) is a separate endpoint that consumes an approved draft.
# ---------------------------------------------------------------------------

def _draft_recipients(draft: dict, *, seed: bool) -> dict:
    """Resolve the draft's recipient set from the FROZEN snapshot's source_url via the FREE
    path only (cache -> constructed generic inbox -> human-flag). NEVER Apollo. seed=True (at
    draft-create) constructs+writes a generic inbox if the domain has none; seed=False (review/
    GET) is read-only and reflects the current store — both compute via recipient_set, so what
    the human reviews matches what send_rfq will use at send-time."""
    from utils import supplier_registry
    snap = draft.get("candidate_snapshot") or {}
    source_url = snap.get("source_url")
    if seed:
        return supplier_registry.assemble_recipient_set(source_url)
    domain = supplier_registry._normalize_domain(source_url or "")
    rs = supplier_registry.recipient_set(supplier_registry.lookup_by_domain(domain) if domain else None)
    return {"to": rs["to"], "cc": rs["cc"], "status": "resolved" if rs["to"] else "needs_human"}


class RfqDraftCreateRequest(BaseModel):
    candidate_id: str
    tier: int


class RfqDraftApproveRequest(BaseModel):
    approved_by: str       # becomes Approval.approved_by at the A2 send


class RfqDraftRejectRequest(BaseModel):
    rejected_by: str


@app.post("/api/runs/{run_id}/rfq-draft", status_code=201)
def create_rfq_draft(run_id: str, body: RfqDraftCreateRequest):
    """Create a reviewable RFQ draft for one sourced candidate. The candidate is reconstructed
    SERVER-SIDE from the run's stored sourcing results (authoritative — a client-supplied
    candidate is never trusted) and frozen as the draft's snapshot; the body is generated from
    the run's asset specs. status=drafted. NO send — that is A2."""
    from utils.procurement_agent.state import persistence
    from utils.procurement_agent.outreach import _make_draft

    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        # Authoritative source: reconstruct from the stored raw results (needs the JSON string).
        cand = _reconstruct_candidate(run.sourcing_results_json, body.candidate_id, body.tier)
        if cand is None:
            raise HTTPException(status_code=404, detail="Candidate not found in this run")
        specs = json.loads(run.asset_specs_json) if run.asset_specs_json else {}

    vendor_name = cand.get("vendor_name") or "supplier"
    draft_body = _make_draft(vendor_name, specs)
    draft = persistence.create_draft(
        run_id=run_id,
        candidate_id=body.candidate_id,
        candidate_snapshot=cand,     # send-sufficient: carries vendor_name + source_url
        draft_body=draft_body,
    )
    # Assemble the recipient set via the FREE path (seeds a generic inbox if needed), so the
    # human reviews who it goes to before approving. No Apollo, no escalation.
    recipients = _draft_recipients(draft, seed=True)
    return {
        "draft_id": draft["id"],
        "status": draft["status"],
        "candidate_id": draft["candidate_id"],
        "draft_body": draft["draft_body"],
        "recipients": recipients,
    }


@app.get("/api/rfq-drafts/{draft_id}")
def get_rfq_draft(draft_id: str):
    """Read one stored draft (status, body, frozen candidate snapshot, approval state) — this
    is what makes a later approval a real review of what the human can see."""
    from utils.procurement_agent.state import persistence
    draft = persistence.get_draft(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    # Surface the CURRENT recipient set (read-only) so the approver sees who it goes to — the
    # same recipient_set send_rfq uses, so review matches send.
    draft["recipients"] = _draft_recipients(draft, seed=False)
    return draft


@app.get("/api/runs/{run_id}/rfq-drafts")
def list_rfq_drafts(run_id: str):
    """All RFQ drafts for a run, newest first."""
    from utils.procurement_agent.state import persistence
    drafts = persistence.list_drafts(run_id)
    return {"run_id": run_id, "count": len(drafts), "drafts": drafts}


@app.post("/api/rfq-drafts/{draft_id}/approve")
def approve_rfq_draft(draft_id: str, body: RfqDraftApproveRequest):
    """Record a human approval against a STORED draft. The approval is stamped on the A0
    lifecycle transition (drafted -> approved). NO send. 404 unknown draft; 409 illegal
    transition (re-approve / already sent / rejected) — surfaced honestly, never a 500."""
    from utils.procurement_agent.state import persistence
    if persistence.get_draft(draft_id) is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    try:
        draft = persistence.transition_draft(draft_id, "approved", approved_by=body.approved_by)
    except persistence.DraftTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"draft_id": draft["id"], "status": draft["status"], "approved_by": draft["approved_by"]}


@app.post("/api/rfq-drafts/{draft_id}/reject")
def reject_rfq_draft(draft_id: str, body: RfqDraftRejectRequest):
    """Reject a STORED draft (drafted -> rejected, terminal). NO send. 404 unknown; 409 illegal."""
    from utils.procurement_agent.state import persistence
    if persistence.get_draft(draft_id) is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    try:
        draft = persistence.transition_draft(draft_id, "rejected", rejected_by=body.rejected_by)
    except persistence.DraftTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"draft_id": draft["id"], "status": draft["status"], "rejected_by": draft["rejected_by"]}


@app.post("/api/rfq-drafts/{draft_id}/send")
def send_rfq_draft(draft_id: str):
    """Send an APPROVED RFQ draft (RFQ wiring A2 — the first path that can send a real email).

    Consumes the stored approval + frozen candidate snapshot and calls send_rfq behind
    EMAIL_SEND_ENABLED. CLAIM-MATCHES-REALITY: the draft is marked 'sent' ONLY on a genuine
    send (a real message went). With the flag OFF (default) send_rfq returns 'stubbed' without
    touching the network, and on stubbed/no_recipients/error the draft stays 'approved' and
    re-sendable — its 'sent' state always means a message actually went. The send is gated to
    approved drafts (409 otherwise, and send_rfq is NEVER called for a non-approved draft).
    Recipients come only from send_rfq's own local lookup_by_domain — NO contact resolution,
    NO _escalate_contact, NO Apollo: this endpoint cannot spend an Apollo credit."""
    from utils.procurement_agent.state import persistence
    from utils import rfq_send

    draft = persistence.get_draft(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    # SEND_GOVERNANCE_V1 (T4): with governance active, an approved draft is delivered
    # ONLY via the concierge release queue (releaser identity recorded) — this direct
    # send endpoint refuses, making the release step structurally unbypassable.
    # Flag OFF: byte-identical legacy behavior below.
    from utils import send_governance as _sg
    if _sg.send_governance_active():
        raise HTTPException(
            status_code=409,
            detail="Send governance is active - approved drafts are released via "
                   "/api/admin/send-governance/release-queue, not sent directly",
        )
    # Gate BEFORE send: only 'approved' -> 'sent' is legal. A drafted/rejected/sent draft 409s
    # and send_rfq is never reached.
    if not persistence.can_transition_draft(draft["status"], "sent"):
        raise HTTPException(
            status_code=409,
            detail=f"draft is '{draft['status']}' — only an approved draft can be sent",
        )

    # SEND_GOVERNANCE_V1 (T2): stamp the run's canonical part identity onto the send
    # so the per-supplier-per-part open-RFQ cap can count it. Governance-active only
    # (flag-off sent_messages rows stay byte-identical); fail-soft — a part_key
    # derivation error must not break the send flow (the daily cap still binds).
    # Governance-off: the send_rfq call below is BYTE-IDENTICAL to before (no
    # part_key kwarg at all — parity holds at the call site, not just the flag).
    send_kwargs: dict = {}
    from utils import send_governance
    if send_governance.send_governance_active():
        try:
            from utils import known_parts
            with _SessionFactory() as session:
                run = session.get(SourcingRunORM, draft["run_id"])
                specs = json.loads(run.asset_specs_json) if run and run.asset_specs_json else {}
            send_kwargs["part_key"] = known_parts.canonical_part_key(
                specs.get("manufacturer"), specs.get("part_number")) or None
        except Exception:
            send_kwargs["part_key"] = None

    approval = rfq_send.Approval(approved_by=draft["approved_by"], approved_at=draft["approved_at"])
    result = rfq_send.send_rfq(
        draft["candidate_snapshot"],   # the frozen snapshot the human approved (vendor_name + source_url)
        draft["draft_body"],
        approval,
        run_id=draft["run_id"],
        **send_kwargs,
    )

    # Mark 'sent' ONLY on a genuine send. send_rfq returns a sent_message_id even when stubbed
    # (it records a stubbed row), so key on result["sent"], never on the id's presence.
    if result.get("sent"):
        persistence.transition_draft(draft_id, "sent", sent_message_id=result.get("sent_message_id"))
        draft_status = "sent"
        sent_message_id = result.get("sent_message_id")
    else:
        draft_status = draft["status"]   # unchanged (approved) — re-sendable
        sent_message_id = None

    return {
        "draft_id": draft_id,
        "draft_status": draft_status,
        "send_status": result.get("status"),   # sent | stubbed | no_recipients | error | not_sent_no_approval
        "sent": bool(result.get("sent")),
        "sent_message_id": sent_message_id,
        "recipients": result.get("recipients"),
    }


class ShipToBody(BaseModel):
    company: str = ""
    address: str = ""
    city: str = ""
    attention: str = ""
    hours: str = ""
    instructions: str = ""


@app.get("/api/sites/{site_id}/ship-to")
def get_site_shipto(site_id: str):
    """A site's delivery ship-to (the durable store behind Delivery Settings + the
    graduated disclosure at order placement). null when nothing is saved yet — the UI
    falls back to its seeded default. Ungated like the other customer endpoints."""
    from utils import site_settings
    return {"site_id": site_id, "ship_to": site_settings.get_shipto(site_id)}


@app.put("/api/sites/{site_id}/ship-to")
def put_site_shipto(site_id: str, body: ShipToBody):
    """Save (upsert) a site's ship-to. One row per site. Ungated; binds to the buyer/
    admin role when real auth lands (CLEANUP §4.1)."""
    from utils import site_settings
    site_settings.upsert_shipto(site_id, body.model_dump())
    return {"site_id": site_id, "ship_to": site_settings.get_shipto(site_id)}


# ---------------------------------------------------------------------------
# Buyer-loop endpoints — inbound quote review (the comparison table) + confirm/reject.
#
# Run-scoped reads/triggers follow the ungated run-endpoint convention (no user auth
# yet — CLEANUP §4.1). confirm writes price_db, so it is CONSEQUENTIAL: it binds to the
# buyer role when real auth lands (flagged in CLEANUP). process-replies performs a live
# Gmail READ when configured; it is fail-soft (clear "unavailable" result, no call) and
# NEVER sends (gmail.readonly).
# ---------------------------------------------------------------------------

@app.get("/api/runs/{run_id}/review-items")
def list_run_review_items(run_id: str):
    """Run-scoped inbound quotes/contacts queued for review (the comparison-table feed).
    Includes sent_count (RFQs sent for this run) and quote_count so the UI can show
    partial state ("2 of 3 suppliers responded"). Unknown run -> empty (it's a read)."""
    from utils import supplier_registry
    items = supplier_registry.get_review_items(run_id=run_id)
    sent = supplier_registry.get_sent_messages(run_id=run_id)
    quote_count = sum(1 for i in items if i.get("kind") == "quote")
    return {"run_id": run_id, "review_items": items,
            "sent_count": len(sent), "quote_count": quote_count}


@app.post("/api/runs/{run_id}/process-replies")
def process_run_replies(run_id: str):
    """Trigger inbound reply ingestion: live-read the Arkim inbox, match replies to sent
    RFQs, extract quotes/contacts, and QUEUE them for review. The Gmail read is
    inbox-global (it cannot be scoped to one run); the response reports this run's queued
    count. Fail-soft: returns available=False with NO live call when Gmail isn't
    configured or sending is disabled. Read-only — never sends."""
    if _run_model_for(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    from utils import reply_processor, supplier_registry
    from utils.inbox_reader import GmailInboxReader
    reader = GmailInboxReader()
    if not reader.configured:
        return {"run_id": run_id, "available": False, "summary": None,
                "message": "Inbox read unavailable — Gmail not configured or sending disabled."}
    summary = reply_processor.process_replies(reader=reader)
    items = supplier_registry.get_review_items(run_id=run_id)
    queued = sum(1 for i in items if i.get("status") in ("pending", "needs_human_review"))
    return {"run_id": run_id, "available": True, "summary": summary, "queued_for_run": queued}


@app.post("/api/review-items/{item_id}/confirm")
def confirm_review_item(item_id: str):
    """Human-confirm a queued quote/contact. quote -> price_db (source="rfq"); contact ->
    supplier primary contact. The ONLY UI path that writes price_db — consequential, so
    it binds to the buyer role when real auth lands (CLEANUP §4.1)."""
    from utils import reply_processor, supplier_registry
    item = supplier_registry.get_review_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Review item not found")
    kind = item.get("kind")
    if kind == "quote":
        confirmed = reply_processor.confirm_quote(item_id)
    elif kind == "contact":
        confirmed = reply_processor.confirm_contact(item_id)
    else:
        raise HTTPException(status_code=422, detail=f"Cannot confirm item of kind {kind!r}")
    return {"item_id": item_id, "kind": kind, "confirmed": confirmed,
            "item": supplier_registry.get_review_item(item_id)}


@app.post("/api/review-items/{item_id}/reject")
def reject_review_item(item_id: str):
    """Human-reject a queued item -> discard (no platform change, no price write)."""
    from utils import reply_processor, supplier_registry
    item = supplier_registry.get_review_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Review item not found")
    rejected = reply_processor.reject(item_id)
    return {"item_id": item_id, "rejected": rejected,
            "item": supplier_registry.get_review_item(item_id)}


@app.post("/api/review-items/{item_id}/place-order")
def place_order_from_quote(item_id: str):
    """Place a durable order directly from a CONFIRMED quote (the RFQ path: Tier 3 RFQ ->
    quote -> confirm -> order). The double gate holds: the quote must already be
    human-confirmed (its price is written), AND this is a separate deliberate action —
    no accidental placement, and no placement without a confirmed price. Ungated like the
    other review-item endpoints (binds to the buyer role when real auth lands; CLEANUP §4.1)."""
    from utils import orders, supplier_registry
    item = supplier_registry.get_review_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Review item not found")
    if item.get("kind") != "quote":
        raise HTTPException(status_code=422, detail="Only a quote can be placed as an order")
    if item.get("status") != "confirmed":
        raise HTTPException(status_code=409, detail="Confirm the quote before placing the order")
    payload = item.get("payload") or {}
    selection = {
        "run_id": item.get("run_id"),
        "vendor_name": item.get("vendor_name"),
        "supplier_domain": item.get("supplier_domain"),
        "manufacturer": item.get("manufacturer"),
        "part_number": item.get("part_number"),
        "unit_price": payload.get("unit_price"),
        "currency": payload.get("currency") or "USD",
        "lead_time": payload.get("lead_time"),
        "source": "rfq",
    }
    # D2 prereq #1: stamp the order with its run's tenant key (company PIN), transitively.
    order_company_id = None
    if item.get("run_id"):
        with _SessionFactory() as session:
            _run = session.get(SourcingRunORM, item["run_id"])
            order_company_id = _run.company_id if _run else None
    order = orders.create_order(selection, quantity=int(payload.get("quantity") or 1),
                                placed_by="buyer", company_id=order_company_id)
    if not order:
        raise HTTPException(status_code=500, detail="Order capture failed")
    placed = orders.place_order(order["id"], placed_by="buyer")
    final = placed or order
    _persist_order_on_run(item.get("run_id"), final)
    return {
        "success": placed is not None,
        "order": final,
        "placed": placed is not None,
        "message": "Order placed." if placed else "Captured as draft — no resolvable price.",
        "item_id": item_id,
    }


# ---------------------------------------------------------------------------
# "Your Arkim impact" — all arithmetic lives in utils.impact (one versioned module).
# These endpoints only expose its output; the frontend renders, never re-computes.
# Savings are MEASURED from the customer's own transactions (no external baseline);
# action counts are COUNTED; time saved is an ESTIMATE labelled with its model version.
# ---------------------------------------------------------------------------

@app.get("/api/runs/{run_id}/impact")
def run_impact(run_id: str):
    """Per-decision impact for one run (measured saving | None, real counts, labelled
    time estimate)."""
    if _run_model_for(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    from utils import impact
    return impact.gather_run_decision(run_id)


@app.get("/api/impact")
def cumulative_impact():
    """Cumulative impact over the customer's real orders (drillable: per-month + ids)."""
    from utils import impact
    return impact.gather_cumulative()


@app.post("/api/admin/orders/{order_id}/status")
def admin_update_order_status(order_id: str, body: OrderStatusRequest,
                              role: str = Depends(require_admin)):
    """Ops lifecycle transition (confirmed/shipped/received), state-machine enforced.
    Placement and cancellation have dedicated paths (execute / cancel) -> rejected here."""
    from utils import orders
    updated = orders.update_order_status(order_id, body.status)
    if updated is None:
        existing = orders.get_order(order_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Order not found")
        raise HTTPException(
            status_code=409,
            detail=f"Illegal transition to '{body.status}' from '{existing.get('status')}' "
                   f"(use /execute to place, /cancel to cancel)",
        )
    return updated


@app.post("/api/admin/orders/{order_id}/cancel")
def admin_cancel_order(order_id: str, body: OrderCancelRequest,
                       role: str = Depends(require_admin)):
    """Ops off-ramp — cancel from any pre-received state (state-machine enforced)."""
    from utils import orders
    cancelled = orders.cancel_order(order_id, reason=body.reason)
    if cancelled is None:
        existing = orders.get_order(order_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Order not found")
        raise HTTPException(status_code=409,
                            detail=f"Cannot cancel from '{existing.get('status')}'")
    return cancelled


# ---------------------------------------------------------------------------
# Night 4 — ONBOARDING AGENT (admin-gated, TIER1_V2-gated, inert when off).
#
# URL → harvest → extract → prepopulate supplier profile → concierge
# review/approve → writes an onboarded supplier via Night 3's TIER1_V2
# supplier-scope registry. Every endpoint is require_admin (401/403/503) AND
# gated on TIER1_V2 — when the flag is off they 503 (dormant), so the flag-off
# API is byte-identical (the inertness wall). These routes are NOT on the
# DEMO_MODE allowlist, so a public demo 403s them fail-closed (the harvester
# fetches arbitrary URLs server-side — SSRF caution, I1; it must not be
# reachable unauthenticated). The SSRF guard itself lives in the harvester.
#
#   POST /api/admin/onboarding/harvest        — URL → draft (harvest + extract)
#   GET  /api/admin/onboarding/drafts         — list pending drafts
#   GET  /api/admin/onboarding/drafts/{id}    — load a draft for the inspector
#   POST /api/admin/onboarding/drafts/{id}/approve  — approve ⇒ registry write
#   POST /api/admin/onboarding/drafts/{id}/reject   — discard (no write)
# ---------------------------------------------------------------------------

def _require_onboarding_enabled():
    """TIER1_V2 must be on for the onboarding surface to be live. Mirrors the
    require_admin / _require_labeling_enabled fail-closed pattern: flag off ->
    503 (dormant), never 404-ish pretend-not-here."""
    from utils.procurement_agent.onboarding import flags as _obf
    if not _obf.is_enabled():
        raise HTTPException(status_code=503,
                            detail="Onboarding disabled (TIER1_V2 off)")
    return _obf


class OnboardingHarvestRequest(BaseModel):
    url: str
    model: Optional[str] = None


@app.post("/api/admin/onboarding/harvest", status_code=200)
def admin_onboarding_harvest(body: OnboardingHarvestRequest,
                             role: str = Depends(require_admin)):
    """URL → harvest a bounded set of same-domain pages → extract a structured
    supplier-scope draft → persist it as a PENDING review item.

    Admin-gated (the harvester fetches arbitrary URLs server-side — SSRF
    caution). Fail-soft: unreachable/blocked pages are skipped; a draft is
    still created from whatever fetched. The draft is NOT applied to the
    registry here — approve is the only writer. Returns the draft id + view."""
    _require_onboarding_enabled()
    from utils.procurement_agent.onboarding.harvester import harvest_site
    from utils.procurement_agent.onboarding.extractor import extract_scope
    from utils.procurement_agent.onboarding import concierge
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(status_code=422, detail="url required")
    if not _url_scheme_ok(url):
        raise HTTPException(status_code=422, detail="url must be http/https")
    harvest = harvest_site(url)  # SSRF guard inside the harvester
    draft = extract_scope(harvest, model=body.model)
    draft_id = concierge.create_draft(draft, source_url=url, set_by=role)
    if draft_id is None:
        raise HTTPException(status_code=503,
                            detail="Onboarding disabled or draft could not be created")
    view = concierge.get_draft(draft_id) or {}
    return {
        "draft_id": draft_id,
        "draft": view,
        "harvested_pages": [p.url for p in harvest.pages],
        "skipped": harvest.skipped,
    }


@app.get("/api/admin/onboarding/drafts")
def admin_onboarding_drafts(role: str = Depends(require_admin)):
    """List pending onboarding drafts (the concierge queue)."""
    _require_onboarding_enabled()
    from utils.procurement_agent.onboarding import concierge
    return {"count": len(concierge.list_drafts()),
            "drafts": concierge.list_drafts()}


@app.get("/api/admin/onboarding/drafts/{draft_id}")
def admin_onboarding_draft_detail(draft_id: str, role: str = Depends(require_admin)):
    """Load one draft for the concierge inspector (load → edit/confirm)."""
    _require_onboarding_enabled()
    from utils.procurement_agent.onboarding import concierge
    view = concierge.get_draft(draft_id)
    if not view:
        raise HTTPException(status_code=404, detail="Draft not found")
    return view


class OnboardingApproveRequest(BaseModel):
    # Optional concierge edits applied INSTEAD of the stored draft's fields
    # (present keys win; absent keys keep the stored value). approve is still
    # the single registry-write point — the editor never writes directly.
    name: Optional[str] = None
    vertical: Optional[str] = None
    brands: Optional[List[dict]] = None
    classes: Optional[List[dict]] = None
    ship_area_guess: Optional[dict] = None
    locations: Optional[List[dict]] = None


@app.post("/api/admin/onboarding/drafts/{draft_id}/approve")
def admin_onboarding_approve(draft_id: str, body: OnboardingApproveRequest,
                             role: str = Depends(require_admin)):
    """Approve a draft → apply its scope to the Night 3 registry + drive
    lifecycle onboarding→onboarded. The ONLY path that writes an onboarding
    draft into the registry. Double-approve idempotent. 404 unknown draft;
    returns the updated supplier record on success."""
    _require_onboarding_enabled()
    from utils.procurement_agent.onboarding import concierge
    view = concierge.get_draft(draft_id)
    if not view:
        raise HTTPException(status_code=404, detail="Draft not found")
    revisions = {k: v for k, v in body.model_dump().items() if v is not None}
    record = concierge.approve_draft(draft_id, revisions=revisions or None,
                                     set_by=role)
    if record is None:
        raise HTTPException(status_code=409,
                            detail="Approve failed (registry write rejected)")
    return {"ok": True, "supplier": record,
            "draft": concierge.get_draft(draft_id)}


@app.post("/api/admin/onboarding/drafts/{draft_id}/reject")
def admin_onboarding_reject(draft_id: str, role: str = Depends(require_admin)):
    """Discard a draft — nothing is applied to the registry. 404 unknown."""
    _require_onboarding_enabled()
    from utils.procurement_agent.onboarding import concierge
    view = concierge.get_draft(draft_id)
    if not view:
        raise HTTPException(status_code=404, detail="Draft not found")
    out = concierge.reject_draft(draft_id, set_by=role)
    return {"ok": True, "draft": out}


# ---------------------------------------------------------------------------
# Night 6 — Supplier claim-portal: admin "Generate claim link" (T1).
# ---------------------------------------------------------------------------
# A concierge-generated magic link for a single supplier. The admin picks a
# supplier (by domain) and mints a token; the link is RETURNED to the concierge
# (who sends it manually — the portal NEVER sends email, guardrail 4). The token
# is single-supplier-scoped, expiring (7d default), regenerable; stored HASHED
# at rest (see utils/claim_tokens.py). Admin-gated (require_admin) AND gated on
# SUPPLIER_PORTAL_V1 (503 dormant when off — mirrors the onboarding fail-closed
# pattern; NOT 404, because this is the admin surface, not the public route).
# NOT on the DEMO_MODE allowlist (a public demo must not mint supplier claim
# links).
# ---------------------------------------------------------------------------

def _require_portal_enabled():
    """SUPPLIER_PORTAL_V1 must be on for the portal admin surface to be live.
    Mirrors the require_admin / _require_onboarding_enabled fail-closed pattern:
    flag off -> 503 (dormant), never 404-ish pretend-not-here on the ADMIN path
    (the PUBLIC route returns 404 when flag-off — that is the inertness wall)."""
    if not _portal_enabled():
        raise HTTPException(status_code=503,
                            detail="Supplier portal disabled (SUPPLIER_PORTAL_V1 off)")
    return True


class ClaimLinkRequest(BaseModel):
    supplier_domain: str
    expiry_days: Optional[int] = None  # default 7 (utils/claim_tokens._DEFAULT_EXPIRY_DAYS)


@app.post("/api/admin/suppliers/claim-link", status_code=200)
def admin_generate_claim_link(body: ClaimLinkRequest,
                              role: str = Depends(require_admin)):
    """Mint a claim-portal magic link for a supplier. Returns the link + its raw
    token ONCE (the token is hashed at rest; this is the only time the raw token
    leaves the server). Does NOT send email — the concierge sends the link
    manually (guardrail 4: "Generate claim link" produces a link; it does not
    send). 422 on an empty/invalid domain; 503 when the flag is off."""
    _require_portal_enabled()
    domain = (body.supplier_domain or "").strip()
    if not domain:
        raise HTTPException(status_code=422, detail="supplier_domain required")
    # Confirm the supplier exists in the registry (the link is meaningless for
    # an unknown supplier). Read-only lookup — no registry write.
    from utils import supplier_registry
    rec = supplier_registry.lookup_by_domain(domain)
    if not rec:
        raise HTTPException(status_code=404, detail="Supplier not found")
    kwargs: dict = {}
    if body.expiry_days is not None and body.expiry_days > 0:
        kwargs["expiry_days"] = body.expiry_days
    out = claim_tokens.generate_for(rec["domain"], **kwargs)
    if out is None:
        raise HTTPException(status_code=503,
                            detail="Claim link could not be created (portal disabled or store failure)")
    # The link path the concierge sends: /portal/<token>. The base is the
    # frontend origin (the concierge prepends the deployed URL when sending).
    return {
        "ok": True,
        "supplier_domain": out["supplier_domain"],
        "supplier_name": rec.get("name"),
        "token": out["token"],
        "token_id": out["token_id"],
        "expires_at": out["expires_at"],
        "link_path": f"/portal/{out['token']}",
    }


@app.post("/api/admin/suppliers/claim-link/regenerate", status_code=200)
def admin_regenerate_claim_link(body: ClaimLinkRequest,
                                role: str = Depends(require_admin)):
    """Regenerate a supplier's claim link — revokes every prior live token for
    the supplier and mints a new one (the prior token is rejected from this
    point on). Admin-gated + flag-gated. Returns the new link."""
    _require_portal_enabled()
    domain = (body.supplier_domain or "").strip()
    if not domain:
        raise HTTPException(status_code=422, detail="supplier_domain required")
    from utils import supplier_registry
    rec = supplier_registry.lookup_by_domain(domain)
    if not rec:
        raise HTTPException(status_code=404, detail="Supplier not found")
    kwargs: dict = {}
    if body.expiry_days is not None and body.expiry_days > 0:
        kwargs["expiry_days"] = body.expiry_days
    out = claim_tokens.regenerate(rec["domain"], **kwargs)
    if out is None:
        raise HTTPException(status_code=503,
                            detail="Claim link could not be regenerated (portal disabled or store failure)")
    return {
        "ok": True,
        "supplier_domain": out["supplier_domain"],
        "supplier_name": rec.get("name"),
        "token": out["token"],
        "token_id": out["token_id"],
        "expires_at": out["expires_at"],
        "link_path": f"/portal/{out['token']}",
    }


@app.get("/api/admin/suppliers/{supplier_domain}/claim-tokens")
def admin_list_claim_tokens(supplier_domain: str, role: str = Depends(require_admin)):
    """List a supplier's claim tokens (metadata only — never the raw token or
    hash). Admin/diagnostic surface for the concierge to see active/expired/
    revoked tokens. Flag-gated."""
    _require_portal_enabled()
    from utils import supplier_registry
    rec = supplier_registry.lookup_by_domain(supplier_domain)
    if not rec:
        raise HTTPException(status_code=404, detail="Supplier not found")
    rows = claim_tokens.list_for_supplier(rec["domain"])
    return {"count": len(rows), "supplier_domain": rec["domain"], "tokens": rows}


def _url_scheme_ok(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        return urlparse(url).scheme.lower() in ("http", "https")
    except Exception:
        return False



# ===========================================================================
# Night 6 - Supplier claim-portal: PUBLIC route (T2) - the app's FIRST PUBLIC
# ROUTE. Security posture is the highest-risk part of this build.
# ===========================================================================
# Route prefix: /api/portal/{token}/... - a fresh, non-admin prefix. These
# handlers do NOT declare Depends(require_admin); their ONLY auth is the claim
# token (validated by utils.claim_tokens.validate_token, lookup-by-hash). The
# admin-session/auth boundary is require_admin (I3): anything not declaring it
# is public. A portal token never satisfies require_admin (asserted in T5).
#
# Flag gating (guardrail 3): SUPPLIER_PORTAL_V1 off -> these routes are ABSENT.
# Implemented as a 404 raised at handler entry matching FastAPI's unknown-route
# body ({"detail":"Not Found"}) so flag-off is byte-identical to an unknown
# route (T5 inertness). The flag is read LIVE (_portal_enabled, honors
# monkeypatched os.environ).
#
# Security (T1/T2/T5):
#   - Token validated by hash, never string compare; raw token never logged.
#   - Strict Referrer-Policy: no-referrer on every portal response (token kept
#     out of downstream referrer headers).
#   - NO session cookies issued (token-only auth; no session to hijack).
#   - Uniform rejection: invalid / expired / reused-after-regeneration tokens
#     all -> the SAME 404 (byte-identical to an unknown route), so no oracle
#     distinguishes wrong from expired (T5). A 404 (not 401) is deliberate:
#     the route must not confirm whether a token existed.
#   - Rate-limit keyed on IP + token-prefix (T5) - see _portal_rate_check.
#   - No admin surface reachable (no require_admin here; the property test
#     proves a portal token reaches no /api/admin/* path).
#
# DEMO_MODE: the portal route is NOT on _DEMO_ALLOWLIST, so under DEMO_MODE the
# allowlist middleware 403s it fail-closed (a public no-login demo must not
# expose the supplier claim portal). The flag-off 404 below is the gate in
# normal dev/prod.
# ---------------------------------------------------------------------------

# Rate-limit caps for the public portal route (in-process, mirrors the DEMO_MODE
# _DemoRateCounter pattern). Keyed on (client IP, token-prefix) so an attacker
# hammering garbage tokens from one IP is throttled, while a valid token (distinct
# prefix) from the same IP is not penalized by the attacker's noise. Caps are
# generous for a concierge-distributed link (a real supplier hits the route a
# handful of times); tight enough to stop a token-guessing spray.
_PORTAL_RATE_CAP_PER_BUCKET: int = _env_int("SUPPLIER_PORTAL_RATE_CAP", 20)
_PORTAL_RATE_WINDOW_SEC: int = _env_int("SUPPLIER_PORTAL_RATE_WINDOW_SEC", 60)

# A fixed-window limiter keyed on (ip, token_prefix): dict of bucket ->
# [count, window_start]. Thread-safe via a lock (mirrors _DemoRateCounter). NOT
# a sliding window - sufficient for a public route's anti-spray posture; a
# production deploy would use Redis.
_portal_rate_lock = threading.Lock()
_portal_rate_buckets: Dict[tuple, list] = {}


def _client_ip(request: Request) -> str:
    """The client IP for rate-limiting. Trusts X-Forwarded-For's first hop only
    when the request came through localhost (a dev proxy); otherwise the direct
    client host. Conservative: a misconfigured proxy cannot spoof an arbitrary
    IP to bypass the limiter unless it's the localhost dev proxy."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff and request.client and request.client.host in ("127.0.0.1", "localhost"):
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _portal_rate_check(request: Request, token: str) -> None:
    """Raise 429 (with Retry-After) if the (IP, token-prefix) bucket has exceeded
    the cap within the window. Inert when the cap is <= 0. The bucket key is
    (ip, prefix) so a valid token (distinct prefix) is not throttled by an
    attacker's garbage-token noise on the same IP."""
    if _PORTAL_RATE_CAP_PER_BUCKET <= 0:
        return
    import time
    ip = _client_ip(request)
    prefix = (token or "")[:8]
    bucket = (ip, prefix)
    now = time.monotonic()
    with _portal_rate_lock:
        entry = _portal_rate_buckets.get(bucket)
        if not entry or (now - entry[1]) >= _PORTAL_RATE_WINDOW_SEC:
            entry = [0, now]
            _portal_rate_buckets[bucket] = entry
        entry[0] += 1
        count = entry[0]
    if count > _PORTAL_RATE_CAP_PER_BUCKET:
        raise HTTPException(
            status_code=429,
            detail="Too many requests.",
            headers={"Retry-After": str(_PORTAL_RATE_WINDOW_SEC)},
        )


def _portal_flag_off_404():
    """Raise the byte-identical-to-unknown-route 404 when SUPPLIER_PORTAL_V1 is
    off. FastAPI's unknown-route body is {"detail":"Not Found"} - we match it
    exactly so flag-off is indistinguishable from a route that never existed
    (T5 inertness)."""
    raise HTTPException(status_code=404, detail="Not Found")


def _portal_reject_404():
    """The uniform rejection for invalid / expired / reused tokens. A 404 with
    FastAPI's unknown-route body - deliberately NOT 401, so the route does not
    confirm whether a token existed (no oracle). Identical across all three
    rejection cases (T5)."""
    raise HTTPException(status_code=404, detail="Not Found")


def _validate_portal_token(request: Request, token: str) -> dict:
    """The shared portal-token gate. Returns the validated token row (with
    supplier_domain) on success. On flag-off OR any token failure, raises a
    404 byte-identical to an unknown route (flag-off = route absent; bad token
    = uniform rejection). Applies the rate-limit BEFORE the token check so a
    garbage-token spray is throttled regardless of validity."""
    if not _portal_enabled():
        _portal_flag_off_404()
    _portal_rate_check(request, token)
    row = claim_tokens.validate_token(token)
    if not row:
        _portal_reject_404()
    return row


def _portal_response_headers(headers: dict) -> dict:
    """Strict security headers for every portal response (T2 [REVIEW-ADD])."""
    headers["Referrer-Policy"] = "no-referrer"
    # The token is in the path, not a query string, but a no-store cache-control
    # keeps the token-bearing URL out of any shared/proxy cache.
    headers["Cache-Control"] = "no-store"
    return headers


@app.get("/api/portal/{token}/profile")
def portal_profile(token: str, request: Request):
    """The public supplier claim page contract: the read-only demand teaser
    (HERO, first element) + the editable profile (brands/classes/ship-area +
    aftermarket disclosure). Token-validated; flag-gated inert. Never exposes
    lifecycle / performance / other suppliers. Zero-state teaser -> honest
    category/network framing (never a "0" hero, never a fabricated count)."""
    row = _validate_portal_token(request, token)
    from utils import supplier_portal
    profile = supplier_portal.read_profile(row["supplier_domain"])
    if profile is None:
        # The token is valid but the supplier vanished (deleted mid-session) -
        # uniform rejection (do not reveal the supplier existed).
        _portal_reject_404()
    teaser = supplier_portal.demand_teaser(row["supplier_domain"])
    # HERO first in the contract (the research's demand-as-hero placement).
    body = {
        "teaser": teaser,
        "supplier_domain": profile["supplier_domain"],
        "name": profile["name"],
        "brands": profile["brands"],
        "classes": profile["classes"],
        "ship_area": profile["ship_area"],
        "aftermarket_disclosure": profile["aftermarket_disclosure"],
    }
    return JSONResponse(content=body, headers=_portal_response_headers({}))



class PortalProposeRevisionRequest(BaseModel):
    brands: Optional[List[dict]] = None
    classes: Optional[List[dict]] = None
    ship_area: Optional[dict] = None


@app.post("/api/portal/{token}/propose-revision")
def portal_propose_revision(token: str, body: PortalProposeRevisionRequest,
                            request: Request):
    """A supplier-proposed profile edit -> a PENDING revision (review_items
    kind=supplier_revision) via Night 4's review machinery. NOTHING writes the
    registry here - the concierge approve is the only writer (decision 1).
    Returns the revision id + status. Token-validated; flag-gated inert. 422 on
    a malformed brand relationship (the tri-state relationship is the
    highest-value field and must be well-formed)."""
    row = _validate_portal_token(request, token)
    from utils import supplier_portal
    revisions = {k: v for k, v in body.model_dump().items() if v is not None}
    # Validate brand relationships up front (422, not a silent drop) - the
    # tri-state relationship is the highest-value field and must be well-formed.
    for b in (revisions.get("brands") or []):
        rel = (b.get("relationship") or "").upper().strip()
        from utils import supplier_registry
        if rel and rel not in supplier_registry.BRAND_RELATIONSHIPS:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid brand relationship: {rel}",
            )
    revision_id = supplier_portal.propose_revision(
        row["supplier_domain"], revisions, proposed_by="supplier")
    if revision_id is None:
        _portal_reject_404()
    return JSONResponse(
        content={"ok": True, "revision_id": revision_id, "status": "pending"},
        headers=_portal_response_headers({}),
    )



# ---------------------------------------------------------------------------
# Night 6 - Concierge review of supplier-proposed revisions (T4, admin path).
# Admin-gated + flag-gated. Approve applies via the four scope setters WITHOUT
# a lifecycle drive (the supplier is already onboarded); reject discards.
# ---------------------------------------------------------------------------

@app.post("/api/admin/portal/revisions/{revision_id}/approve")
def admin_approve_revision(revision_id: str, role: str = Depends(require_admin)):
    """Approve a supplier-proposed revision -> apply its scope to the registry
    via the Night 4 setters (no lifecycle drive). 404 unknown revision; 409 on
    a write failure. Admin-gated + SUPPLIER_PORTAL_V1-gated."""
    _require_portal_enabled()
    from utils import supplier_portal
    record = supplier_portal.apply_revision(revision_id, set_by=role)
    if record is None:
        raise HTTPException(status_code=404, detail="Revision not found or apply failed")
    return {"ok": True, "supplier": record}


@app.post("/api/admin/portal/revisions/{revision_id}/reject")
def admin_reject_revision(revision_id: str, role: str = Depends(require_admin)):
    """Reject a supplier-proposed revision - nothing is applied. 404 unknown."""
    _require_portal_enabled()
    from utils import supplier_portal
    out = supplier_portal.reject_revision(revision_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    return {"ok": True, "revision": out}


# ===========================================================================
# Night 8 — Multi-channel intake surface (INTAKE_CHANNELS_V1-gated).
# ===========================================================================
# The channel-agnostic intake endpoints: the email adapter (fully working in
# tests) + SMS/voice contract-stubs (T3) + the unknown-sender confirm step.
# Every endpoint feeds the SAME single intake consumer (utils/intake_channels)
# which maps a valid event into the existing intake pipeline seam and fires the
# sourcing run exactly as an in-app request does. A transport, never a parallel
# pipeline, never an auto-purchase trigger (the no-order property is pinned in
# test_intake_channels + test_api_server).
#
# Flag gating (guardrail 3): INTAKE_CHANNELS_V1 off -> these routes are ABSENT
# (404 byte-identical to an unknown route -- {"detail":"Not Found"}), proven by
# an inertness test. The flag is read LIVE (_intake_enabled, honors monkey-
# patched os.environ).
#
# DEMO_MODE: the intake routes are NOT on _DEMO_ALLOWLIST, so under DEMO_MODE
# the allowlist middleware 403s them fail-closed (a public no-login demo must
# not accept inbound intake -- it's a tenant feature, not a demo feature). The
# flag-off 404 below is the gate in normal dev/prod.
#
# Zero live sends: the ack/clarify/confirm replies go through the email_sender
# GmailSender, stubbed under the EMAIL_SEND_ENABLED double-gate (conftest
# forces it off) -- never a live send. SMS/voice stubs record replies, no
# transport (no SMS sender exists; live SMS provisioning is out of scope).
# ---------------------------------------------------------------------------


def _intake_flag_off_404():
    """Raise the byte-identical-to-unknown-route 404 when INTAKE_CHANNELS_V1 is
    off (mirrors the portal's flag-off inertness)."""
    raise HTTPException(status_code=404, detail="Not Found")


def _require_intake_enabled():
    """The shared intake-route gate. Flag-off -> 404 (route absent). Called at
    every intake handler entry."""
    if not _intake_enabled():
        _intake_flag_off_404()


def _intake_reply_sink(reply):
    """The reply sink wired into the consumer: send the reply via the
    email_sender GmailSender (stubbed under the EMAIL_SEND_ENABLED double-gate)
    and record the SendResult. Never a live send in tests/without creds."""
    from utils.email_sender import EmailMessage, GmailSender
    msg = EmailMessage(
        to=[reply.to],
        subject=reply.subject,
        body=reply.body,
        metadata={"intake_kind": reply.kind, **reply.metadata},
    )
    sender = GmailSender()
    try:
        result = sender.send(msg)
        print(f"[Intake] reply ({reply.kind}) -> {reply.to}: {result.status}")
    except Exception as exc:
        # Fail-soft: a reply send failure never raises into the intake path.
        print(f"[Intake] reply send failed ({reply.kind}): {exc}")


class IntakeEmailInbound(BaseModel):
    """An inbound email payload delivered to the intake adapter (mocked
    transport per I2 patterns). The adapter accepts the parsed envelope so the
    build exercises the real consumer + firer; a live path would feed raw RFC822
    through utils.inbox_reader._parse_reply first (the MIME parser is reused).

    `to` -- the intake address carrying the tenant (intake+<tenant>@arkim.ai).
    `from_address` -- the sender (customer side).
    `body` -- the plain-text body.
    `attachments` -- base64-encoded nameplate photos (filename, content_type, data_b64).
    `message_id` -- the RFC822 Message-ID (channel metadata, for correlation)."""
    to: str
    from_address: str = Field(..., alias="from")
    body: str = ""
    attachments: List[Dict[str, Any]] = Field(default_factory=list)
    message_id: Optional[str] = None

    model_config = {"populate_by_name": True}


def _attachments_from_payload(items):
    """Decode base64 attachment payloads into IntakeAttachment (raw bytes -- the
    same image-handling the in-app upload path uses, I4). Fail-soft per item."""
    import base64 as _b64
    out = []
    for it in items or []:
        try:
            data = _b64.b64decode(it.get("data_b64") or "")
            if not data:
                continue
            out.append(intake_channels.IntakeAttachment(
                filename=it.get("filename") or "attachment",
                content_type=it.get("content_type") or "application/octet-stream",
                data=data,
            ))
        except Exception as exc:
            print(f"[Intake] attachment decode failed: {exc}")
    return out


@app.post("/api/intake/email")
def intake_email(body: IntakeEmailInbound, request: Request, background_tasks: BackgroundTasks):
    """The email adapter -- inbound email -> tenant resolution (per-tenant
    plus-address) -> sender check -> parse to proposed specs (propose-don't-
    invent) -> intake event -> consumer -> sourcing run (or NEEDS_CLARIFICATION
    / confirm step / safe reject). Attachments (nameplate photos) carried
    through to the IntakeAgent (I4). Stubbed ack + clarify + confirm replies
    under the send double-gate. Flag-gated: off -> 404 byte-identical to an
    unknown route.

    The intake mail is kept cleanly separate from RFQ-reply mail: this adapter
    keys on the `to` address (intake+<tenant>@arkim.ai) which the RFQ reply path
    (inbox_reader.fetch_replies, which reads procurement@ with NO recipient
    filter) never targets as an outbound -- and the live path would use a
    distinct intake mailbox/label. The adapter only processes intake-addressed
    mail, so cross-stream is structurally impossible at the adapter boundary."""
    _require_intake_enabled()

    # Tenant resolution from the to-address (I3 plus-addressing). An address
    # that resolves to no tenant -> TENANT_UNKNOWN (no run, safe).
    tenant_key = intake_channels.resolve_tenant_from_address(body.to)
    if tenant_key is None:
        return {"status": "TENANT_UNKNOWN", "run_id": None,
                "detail": f"no tenant for address {body.to!r}"}

    event = intake_channels.IntakeEvent(
        tenant_key=tenant_key,
        channel=intake_channels.IntakeChannel.EMAIL,
        sender=body.from_address,
        text_body=body.body or "",
        attachments=_attachments_from_payload(body.attachments),
        channel_metadata={"message_id": body.message_id, "to": body.to},
    )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    outcome = intake_channels.consume_intake_event(
        event,
        fire_sourcing_run=lambda specs, tk: _fire_sourcing_run_for_intake(
            specs, tk, background_tasks=background_tasks, is_test=True),
        reply_sink=_intake_reply_sink,
        anthropic_api_key=api_key,
    )
    return {
        "status": outcome.status.value,
        "run_id": outcome.run_id,
        "reason": outcome.reason,
        "clarify_attrs": outcome.clarify_attrs,
    }


@app.post("/api/intake/confirm/{token}")
def intake_confirm_sender(token: str, request: Request, background_tasks: BackgroundTasks):
    """The unknown-sender confirm step. A held event advances to a CONFIRMED
    sender and the consumer re-runs (parse -> fire sourcing run, or
    NEEDS_CLARIFICATION). A bad/reused token -> 404 byte-identical to an unknown
    route (no oracle -- mirrors the supplier portal's uniform rejection). Flag-
    gated: off -> 404."""
    _require_intake_enabled()
    payload = intake_channels.consume_held(token)
    if payload is None:
        # Uniform rejection: invalid / reused / expired all look identical.
        raise HTTPException(status_code=404, detail="Not Found")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    outcome = intake_channels.consume_confirmed_event(
        payload,
        fire_sourcing_run=lambda specs, tk: _fire_sourcing_run_for_intake(
            specs, tk, background_tasks=background_tasks, is_test=True),
        reply_sink=_intake_reply_sink,
        anthropic_api_key=api_key,
    )
    return {
        "status": outcome.status.value,
        "run_id": outcome.run_id,
        "reason": outcome.reason,
        "clarify_attrs": outcome.clarify_attrs,
    }



class IntakeSmsInbound(BaseModel):
    """An inbound SMS/MMS webhook payload (contract-stub — T3). The adapter
    accepts the normalized envelope; a live path would receive a Twilio webhook
    and map it to this shape. Tenant resolution is from the inbound number
    (number→tenant map, same shape as email plus-addressing).

    `from_number` -- the sender (customer side, E.164).
    `to_number`   -- the tenant's dedicated inbound number (tenant signal).
    `body`        -- the SMS text.
    `media`       -- MMS media (base64-encoded images, same shape as email
                     attachments — carried through to the IntakeAgent, I4).
    `message_sid` -- the provider message id (channel metadata)."""
    from_number: str = Field(..., alias="from")
    to_number: str = Field(..., alias="to")
    body: str = ""
    media: List[Dict[str, Any]] = Field(default_factory=list)
    message_sid: Optional[str] = None

    model_config = {"populate_by_name": True}


@app.post("/api/intake/sms")
def intake_sms(body: IntakeSmsInbound, request: Request, background_tasks: BackgroundTasks):
    """The SMS adapter (contract-stub, T3) -- inbound SMS/MMS -> tenant
    resolution (number->tenant map) -> sender check -> parse -> intake event ->
    consumer -> sourcing run (or NEEDS_CLARIFICATION / confirm / safe reject).
    Same consumer, same seam, same gates as the email adapter. MMS media
    carried through to the IntakeAgent (I4). Flag-gated: off -> 404.

    Thin on purpose: the point is the CONTRACT (every channel produces the same
    IntakeEvent + feeds the same consumer), not the channel. Live SMS
    provisioning (Twilio creds, webhook auth) is out of scope; the reply is
    recorded, not sent (no SMS transport exists yet)."""
    _require_intake_enabled()
    tenant_key = intake_channels.resolve_tenant_from_number(body.to_number)
    if tenant_key is None:
        return {"status": "TENANT_UNKNOWN", "run_id": None,
                "detail": f"no tenant for number {body.to_number!r}"}

    event = intake_channels.IntakeEvent(
        tenant_key=tenant_key,
        channel=intake_channels.IntakeChannel.SMS,
        sender=body.from_number,
        text_body=body.body or "",
        attachments=_attachments_from_payload(body.media),
        channel_metadata={"message_sid": body.message_sid, "to": body.to_number},
    )
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    outcome = intake_channels.consume_intake_event(
        event,
        fire_sourcing_run=lambda specs, tk: _fire_sourcing_run_for_intake(
            specs, tk, background_tasks=background_tasks, is_test=True),
        reply_sink=_intake_reply_sink,
        anthropic_api_key=api_key,
    )
    return {
        "status": outcome.status.value,
        "run_id": outcome.run_id,
        "reason": outcome.reason,
        "clarify_attrs": outcome.clarify_attrs,
    }


class IntakeVoiceInbound(BaseModel):
    """An inbound voice webhook payload (contract-stub -- T3). Voice = a
    transcript + metadata, NOT the conversational agent (explicitly out of
    scope). The transcript is the text the parser proposes specs from; the
    caller's number is the sender; the tenant's inbound number resolves the
    tenant.

    `from_number`  -- the caller (customer side, E.164).
    `to_number`    -- the tenant's dedicated inbound number (tenant signal).
    `transcript`   -- the call transcript (the text body for parsing).
    `call_sid`     -- the provider call id (channel metadata).
    `recording_url`-- optional recording ref (channel metadata, not fetched)."""
    from_number: str = Field(..., alias="from")
    to_number: str = Field(..., alias="to")
    transcript: str = ""
    call_sid: Optional[str] = None
    recording_url: Optional[str] = None

    model_config = {"populate_by_name": True}


@app.post("/api/intake/voice")
def intake_voice(body: IntakeVoiceInbound, request: Request, background_tasks: BackgroundTasks):
    """The voice adapter (contract-stub, T3) -- inbound call transcript + metadata
    -> tenant resolution (number->tenant) -> sender check -> parse -> intake event
    -> consumer -> sourcing run (or NEEDS_CLARIFICATION / confirm / safe reject).
    Same consumer, same seam, same gates. The CONVERSATIONAL voice agent is
    explicitly out of scope; this is a transcript-in, structured-request-out
    contract stub. Flag-gated: off -> 404. Thin on purpose (the contract, not
    the channel)."""
    _require_intake_enabled()
    tenant_key = intake_channels.resolve_tenant_from_number(body.to_number)
    if tenant_key is None:
        return {"status": "TENANT_UNKNOWN", "run_id": None,
                "detail": f"no tenant for number {body.to_number!r}"}

    event = intake_channels.IntakeEvent(
        tenant_key=tenant_key,
        channel=intake_channels.IntakeChannel.VOICE,
        sender=body.from_number,
        text_body=body.transcript or "",
        attachments=[],  # voice carries no image attachments (transcript only)
        channel_metadata={"call_sid": body.call_sid, "to": body.to_number,
                          "recording_url": body.recording_url},
    )
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    outcome = intake_channels.consume_intake_event(
        event,
        fire_sourcing_run=lambda specs, tk: _fire_sourcing_run_for_intake(
            specs, tk, background_tasks=background_tasks, is_test=True),
        reply_sink=_intake_reply_sink,
        anthropic_api_key=api_key,
    )
    return {
        "status": outcome.status.value,
        "run_id": outcome.run_id,
        "reason": outcome.reason,
        "clarify_attrs": outcome.clarify_attrs,
    }


# ===========================================================================
# Night 11 — Supplier structured quote submission (QUOTE_SUBMIT_V1-gated).
# ===========================================================================
# The three entry paths of QUOTE_SUBMISSION_SPEC.md §2, one store
# (utils/quote_store), one promotion seam (_build_quote_index merges active
# quotes as confirmation records — the I1 decision; see that docstring):
#   A. PUBLIC /api/quote/{token}    — quote-token-auth'd form (NO account, NO
#      claim: quoting is unconditional; signup is the upgrade, pitched only
#      AFTER submission on the unclaimed path).
#   B. PORTAL /api/portal/{token}/quotes — claimed suppliers, claim-token-auth'd
#      (both SUPPLIER_PORTAL_V1 and QUOTE_SUBMIT_V1 must be on).
#   C. ADMIN  /api/admin/quotes    — concierge keys in an emailed/phoned quote
#      (works from day one, before live sends).
# Review queue (flag-not-block sanity checks land here): admin list/approve/
# reject. Approval activates; a pn_differs quote stays labelled as the QUOTED
# PN (equivalent-alternative framing) — never silently the requested PN.
#
# Flag gating: QUOTE_SUBMIT_V1 off ⇒ every route here is ABSENT (404
# byte-identical to an unknown route — {"detail":"Not Found"}), proven by a
# parity test. The flag is read LIVE (_quote_submit_enabled).
#
# Token security (spec §3 / criterion 7): the portal-token posture re-applied —
# hashed at rest (utils/quote_tokens), uniform 404 on unknown tokens (no
# oracle), rate-limit keyed (IP, token-prefix), strict no-referrer/no-store
# headers, single-RFQ scope. A KNOWN token whose RFQ closed renders the honest
# closed state (never an error page, never a live form on a dead request).
#
# NO path to order placement exists from any endpoint below (spec principle 4,
# property-tested): a quote promotes a card; a human buyer orders through the
# existing approval flow. NO sends: the quote ack is T6's, stubbed under the
# EMAIL_SEND_ENABLED + governance stack.
# ---------------------------------------------------------------------------

_QUOTE_RATE_CAP_PER_BUCKET: int = _env_int("QUOTE_SUBMIT_RATE_CAP", 20)
_QUOTE_RATE_WINDOW_SEC: int = _env_int("QUOTE_SUBMIT_RATE_WINDOW_SEC", 60)

# Fixed-window limiter keyed on (ip, token_prefix) — mirrors the portal's
# _portal_rate_buckets exactly, in a SEPARATE bucket space (a spray against the
# quote surface never throttles the claim portal and vice versa).
_quote_rate_lock = threading.Lock()
_quote_rate_buckets: Dict[tuple, list] = {}


def _quote_flag_off_404():
    """Byte-identical-to-unknown-route 404 when QUOTE_SUBMIT_V1 is off."""
    raise HTTPException(status_code=404, detail="Not Found")


def _quote_reject_404():
    """Uniform rejection for unknown quote tokens — identical to the flag-off
    and unknown-route body (no oracle; the portal-token posture)."""
    raise HTTPException(status_code=404, detail="Not Found")


def _quote_rate_check(request: Request, token: str) -> None:
    """429 when the (IP, token-prefix) bucket exceeds the cap in the window.
    Same shape as _portal_rate_check; separate buckets (see above)."""
    if _QUOTE_RATE_CAP_PER_BUCKET <= 0:
        return
    import time
    bucket = (_client_ip(request), (token or "")[:8])
    now = time.monotonic()
    with _quote_rate_lock:
        entry = _quote_rate_buckets.get(bucket)
        if not entry or (now - entry[1]) >= _QUOTE_RATE_WINDOW_SEC:
            entry = [0, now]
            _quote_rate_buckets[bucket] = entry
        entry[0] += 1
        count = entry[0]
    if count > _QUOTE_RATE_CAP_PER_BUCKET:
        raise HTTPException(
            status_code=429,
            detail="Too many requests.",
            headers={"Retry-After": str(_QUOTE_RATE_WINDOW_SEC)},
        )


def _validate_quote_token(request: Request, token: str) -> dict:
    """The shared quote-token gate. Returns the validated token context (with
    state "live" | "closed") on a KNOWN token. Flag-off OR unknown token ⇒
    uniform 404. Rate-limit applied BEFORE the token check so a garbage-token
    spray is throttled regardless of validity (the portal discipline)."""
    if not _quote_submit_enabled():
        _quote_flag_off_404()
    _quote_rate_check(request, token)
    from utils import quote_tokens
    row = quote_tokens.validate_token(token)
    if not row:
        _quote_reject_404()
    return row


def _supplier_is_claimed(domain: str) -> bool:
    """Whether a supplier already has the claimed/onboarded relationship — used
    ONLY to decide whether the post-submit claim pitch renders (path A,
    unclaimed only). Claimed ⇔ registry onboarding_status is an onboarded
    status OR the TIER1_V2 lifecycle reached onboarding/onboarded. Fail-soft:
    an unknown/unreadable supplier counts as UNCLAIMED (the pitch is an offer,
    not a disclosure — showing it to an edge-case claimed supplier is
    harmless; hiding it from a real unclaimed one loses the loop)."""
    try:
        from utils import supplier_registry
        rec = supplier_registry.lookup_by_domain(domain)
        if not rec:
            return False
        if rec.get("onboarding_status") in supplier_registry._ONBOARDED_STATUSES:
            return True
        return rec.get("tier1_lifecycle") in ("onboarding", "onboarded")
    except Exception:
        return False


def _run_specs_for_quote(run_id: str) -> Optional[dict]:
    """The run's asset specs (manufacturer / part_number / quantity ...) for
    quote validation context, or None when the run doesn't exist. Internal
    `_`-prefixed ledger keys stripped (the run-detail discipline)."""
    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if run is None:
            return None
        specs = json.loads(run.asset_specs_json) if run.asset_specs_json else {}
    return {k: v for k, v in specs.items() if not str(k).startswith("_")}


class QuoteSubmissionBody(BaseModel):
    """The five required fields (spec §4) + the two optional. `part_number` is
    the PN-CONFIRMATION field: prefilled with the requested PN client-side;
    blank/omitted counts as confirming the requested PN, an EDIT trips the
    wrong-part gate (pn_differs → review)."""
    quote_number: str
    unit_price: float
    quantity: float
    lead_time: str
    part_number: Optional[str] = None
    freight: Optional[str] = None
    valid_until: Optional[str] = None
    notes: Optional[str] = None


class PortalQuoteBody(QuoteSubmissionBody):
    run_id: str


class AdminQuoteBody(QuoteSubmissionBody):
    run_id: str
    supplier_domain: str
    vendor_name: Optional[str] = None


def _validate_quote_fields(body: QuoteSubmissionBody) -> None:
    """422 on semantically-invalid required fields (types are pydantic's job)."""
    if not (body.quote_number or "").strip():
        raise HTTPException(status_code=422, detail="quote_number is required")
    if body.unit_price is None or body.unit_price <= 0:
        raise HTTPException(status_code=422, detail="unit_price must be positive")
    if body.quantity is None or body.quantity <= 0:
        raise HTTPException(status_code=422, detail="quantity must be positive")
    if not (body.lead_time or "").strip():
        raise HTTPException(status_code=422, detail="lead_time is required")


def _record_structured_quote(
    body: QuoteSubmissionBody,
    *,
    submitted_via: str,
    submitted_by: Optional[str],
    supplier_domain: str,
    vendor_name: Optional[str],
    run_id: Optional[str],
    rfq_id: Optional[str],
    part_key: Optional[str],
    manufacturer: Optional[str],
    requested_part_number: Optional[str],
    requested_quantity: Optional[float],
    default_valid_until: Optional[str] = None,
) -> dict:
    """The shared submission core all three paths call: sanity-band resolution
    (price_db median — band-absent skips), the store write (supersede + the
    wrong-part gate + flag-not-block checks live in quote_store.submit_quote),
    and the response shape. Raises 500 on a store failure."""
    from utils import quote_store
    band = quote_store.price_band_median(manufacturer, requested_part_number)
    quote = quote_store.submit_quote(
        supplier_domain=supplier_domain,
        vendor_name=vendor_name,
        unit_price=body.unit_price,
        submitted_via=submitted_via,
        submitted_by=submitted_by,
        run_id=run_id,
        rfq_id=rfq_id,
        part_key=part_key,
        manufacturer=manufacturer,
        requested_part_number=requested_part_number,
        quoted_part_number=body.part_number,
        quote_number=body.quote_number.strip(),
        quantity=body.quantity,
        requested_quantity=requested_quantity,
        lead_time=body.lead_time.strip(),
        freight=body.freight,
        valid_until=body.valid_until or default_valid_until,
        notes=body.notes,
        band_median=band,
    )
    if quote is None:
        raise HTTPException(status_code=500, detail="Quote could not be recorded")
    if submitted_via in ("rfq_link", "portal"):
        _send_quote_ack(quote)  # stubbed under the full send stack; fail-soft
    return {
        "ok": True,
        "quote_id": quote["id"],
        "status": quote["status"],           # "active" (promotes now) | "review"
        "review_reasons": quote["review_reasons"],
        "pn_differs": quote["pn_differs"],
        "valid_until": quote["valid_until"],
    }


def _send_quote_ack(quote: dict) -> None:
    """The "quote received" supplier ack (spec §8) — routed through the SAME
    GmailSender seam as every outbound (the governance stack + the
    EMAIL_SEND_ENABLED gate run INSIDE sender.send, so this path structurally
    cannot bypass them; it is stubbed until sends go live). Concierge entries
    get no ack (the supplier already corresponded with a human). No recipient
    on file ⇒ skip silently. Fail-soft: an ack failure never surfaces into the
    submission response."""
    try:
        from utils import supplier_registry
        from utils.email_sender import EmailMessage, GmailSender
        recipients = supplier_registry.assemble_recipient_set(
            quote.get("supplier_domain"))
        to = recipients.get("to") or []
        if not to:
            return
        pn = quote.get("quoted_part_number") or quote.get("requested_part_number")
        msg = EmailMessage(
            to=to,
            subject=f"Quote received — {pn or 'your quote'}",
            body=(
                f"Hello {quote.get('vendor_name') or ''},\n\n"
                f"Thanks — we received your quote"
                f"{f' for {pn}' if pn else ''} "
                f"(${quote.get('unit_price')} / unit, ref "
                f"{quote.get('quote_number') or quote.get('id')}).\n\n"
                f"It is now in front of the buyer. If they proceed, we'll be "
                f"in touch at this address.\n\n"
                f"Regards,\nArkim Procurement\nprocurement@arkim.ai"
            ),
            metadata={"quote_ack": quote.get("id"),
                      "run_id": quote.get("run_id"),
                      "supplier_domain": quote.get("supplier_domain")},
        )
        result = GmailSender().send(msg)
        print(f"[QuoteAck] {result.status} -> {to} (quote {quote.get('id')})")
    except Exception as exc:
        print(f"[QuoteAck] failed (non-fatal): {exc}")


# ---------------------------------------------------------------------------
# Path A — the PUBLIC quote form (token-auth'd; the RFQ email's /quote/{token})
# ---------------------------------------------------------------------------

@app.get("/api/quote/{token}")
def quote_form_context(token: str, request: Request):
    """The token page contract (spec §3): the request (part identity, qty,
    need-by), the supplier's own name/domain as addressee, and the form
    prefills. NOTHING else — no buyer identity beyond the RFQ email, no other
    suppliers, no pricing signals. A closed RFQ renders state="closed"
    honestly (200, never an error page). Unknown token ⇒ uniform 404."""
    row = _validate_quote_token(request, token)
    if row["state"] == "closed":
        return JSONResponse(content={"state": "closed"},
                            headers=_portal_response_headers({}))
    from utils import quote_store
    existing = None
    for q in quote_store.get_quotes(run_id=row.get("run_id"),
                                    supplier_domain=row.get("supplier_domain")):
        if q["effective_status"] in ("active", "review"):
            existing = {"status": q["effective_status"],
                        "unit_price": q["unit_price"],
                        "submitted_at": q["submitted_at"]}
        break  # newest first — only the latest submission matters
    body = {
        "state": "live",
        "request": {
            "manufacturer": row.get("manufacturer"),
            "part_number": row.get("part_number"),
            "quantity": row.get("quantity"),
            "need_by": row.get("need_by"),
        },
        "supplier": {
            "name": row.get("vendor_name"),
            "domain": row.get("supplier_domain"),
        },
        "expires_at": row.get("expires_at"),
        "existing_quote": existing,
    }
    return JSONResponse(content=body, headers=_portal_response_headers({}))


@app.post("/api/quote/{token}")
def quote_submit(token: str, body: QuoteSubmissionBody, request: Request):
    """Path-A submission: NO account, NO claim — quoting is unconditional
    (spec §1). The claim pitch is returned AFTER a successful submission, and
    only for an unclaimed supplier; it is an invitation into the EXISTING
    claim flow (concierge-issued link), never an inline token mint. A closed
    RFQ ⇒ 409 (the form never writes to a dead request)."""
    row = _validate_quote_token(request, token)
    if row["state"] == "closed":
        raise HTTPException(status_code=409, detail="This request has closed")
    _validate_quote_fields(body)
    out = _record_structured_quote(
        body,
        submitted_via="rfq_link",
        submitted_by=row.get("token_id"),
        supplier_domain=row["supplier_domain"],
        vendor_name=row.get("vendor_name"),
        run_id=row.get("run_id"),
        rfq_id=row.get("rfq_id"),
        part_key=row.get("part_key"),
        manufacturer=row.get("manufacturer"),
        requested_part_number=row.get("part_number"),
        requested_quantity=row.get("quantity"),
        default_valid_until=row.get("expires_at"),  # the RFQ validity window
    )
    out["claim_pitch"] = not _supplier_is_claimed(row["supplier_domain"])
    return JSONResponse(content=out, headers=_portal_response_headers({}))


# ---------------------------------------------------------------------------
# Path B — the claimed-supplier portal: open requests + quote history + submit
# (claim-token-auth'd; every route additionally gated on QUOTE_SUBMIT_V1)
# ---------------------------------------------------------------------------

@app.get("/api/portal/{token}/open-requests")
def portal_open_requests(token: str, request: Request):
    """T5: the claimed supplier's OPEN requests — the runs with an un-resolved
    RFQ addressed to THEIR domain (sent_messages status in OPEN_RFQ_STATUSES),
    deduped per run, each with the request identity (from the run's specs) and
    their own quote state on it. THEIR view only: the domain comes from the
    validated claim token; no other supplier's RFQs, quotes, or existence are
    visible. Runs that no longer resolve are skipped (never a fabricated
    row). Fail-soft on the stores ([]), never a 500 on the public surface."""
    if not _quote_submit_enabled():
        _quote_flag_off_404()
    prow = _validate_portal_token(request, token)
    dom = prow["supplier_domain"]
    from utils import quote_store, supplier_registry
    requests_out: list = []
    seen_runs: set = set()
    try:
        for m in supplier_registry.get_sent_messages(domain=dom):  # newest first
            rid = m.get("run_id")
            if not rid or rid in seen_runs:
                continue
            if m.get("status") not in supplier_registry.OPEN_RFQ_STATUSES:
                continue
            seen_runs.add(rid)
            specs = _run_specs_for_quote(rid)
            if specs is None:
                continue  # run vanished — never fabricate a request row
            quoted = None
            for q in quote_store.get_quotes(run_id=rid, supplier_domain=dom):
                if q["effective_status"] in ("active", "review"):
                    quoted = {"status": q["effective_status"],
                              "unit_price": q["unit_price"],
                              "submitted_at": q["submitted_at"]}
                break  # newest first — only the latest submission matters
            requests_out.append({
                "run_id": rid,
                "manufacturer": specs.get("manufacturer"),
                "part_number": specs.get("part_number"),
                "quantity": specs.get("quantity"),
                "sent_at": m.get("sent_at"),
                "quoted": quoted,
            })
    except Exception as exc:  # public surface: degrade, never 500
        import logging
        logging.getLogger(__name__).warning(
            "portal open-requests read failed for %s: %s", dom, exc)
        requests_out = []
    return JSONResponse(content={"requests": requests_out},
                        headers=_portal_response_headers({}))


@app.get("/api/portal/{token}/quotes")
def portal_quote_history(token: str, request: Request):
    """T5: the supplier's OWN quote history — every quote from their domain
    (all lifecycles, effective status shown honestly incl. read-time expiry),
    newest first. Nothing cross-supplier: the domain is the token's; the rows
    expose no buyer identity and no other suppliers. Fail-soft ([])."""
    if not _quote_submit_enabled():
        _quote_flag_off_404()
    prow = _validate_portal_token(request, token)
    from utils import quote_store
    history = [
        {
            "quote_id": q["id"],
            "run_id": q.get("run_id"),
            "part_number": q.get("requested_part_number"),
            "quoted_part_number": q.get("quoted_part_number"),
            "unit_price": q["unit_price"],
            "quantity": q.get("quantity"),
            "lead_time": q.get("lead_time"),
            "status": q["effective_status"],
            "submitted_at": q["submitted_at"],
            "submitted_via": q["submitted_via"],
            "valid_until": q.get("valid_until"),
        }
        for q in quote_store.get_quotes(supplier_domain=prow["supplier_domain"])
    ]
    return JSONResponse(content={"quotes": history},
                        headers=_portal_response_headers({}))


@app.post("/api/portal/{token}/quotes")
def portal_quote_submit(token: str, body: PortalQuoteBody, request: Request):
    """Path-B submission from the portal's open-requests section (T5). Gated
    by BOTH flags: the claim token authenticates the supplier
    (SUPPLIER_PORTAL_V1 + the portal's own rate limit / uniform 404), and the
    quote surface must be on (QUOTE_SUBMIT_V1 — off ⇒ this route is absent,
    404). The supplier may quote only a run with an OPEN RFQ addressed to
    them; quotes are their own store — NEVER a profile revision (no
    propose→approve coupling)."""
    if not _quote_submit_enabled():
        _quote_flag_off_404()
    prow = _validate_portal_token(request, token)
    dom = prow["supplier_domain"]
    _validate_quote_fields(body)
    from utils import supplier_registry
    open_rfqs = [
        m for m in supplier_registry.get_sent_messages(run_id=body.run_id,
                                                       domain=dom)
        if m.get("status") in supplier_registry.OPEN_RFQ_STATUSES
    ]
    if not open_rfqs:
        raise HTTPException(status_code=404,
                            detail="No open request for this supplier")
    specs = _run_specs_for_quote(body.run_id) or {}
    rec = supplier_registry.lookup_by_domain(dom)
    out = _record_structured_quote(
        body,
        submitted_via="portal",
        submitted_by=prow.get("token_id"),
        supplier_domain=dom,
        vendor_name=(rec or {}).get("name"),
        run_id=body.run_id,
        rfq_id=open_rfqs[0].get("id"),       # newest open RFQ row id
        part_key=open_rfqs[0].get("part_key"),
        manufacturer=specs.get("manufacturer"),
        requested_part_number=specs.get("part_number"),
        requested_quantity=specs.get("quantity"),
    )
    return JSONResponse(content=out, headers=_portal_response_headers({}))


# ---------------------------------------------------------------------------
# Path C — concierge entry + the review queue (admin-gated)
# ---------------------------------------------------------------------------

# NOTE on gate ordering for the admin quote routes: the FLAG check runs BEFORE
# require_admin (which is therefore invoked in the handler body, not as a
# Depends) — flag-off must render these routes ABSENT (byte-identical 404) for
# ANY caller, whereas a Depends(require_admin) would answer 401/403 first and
# reveal the route exists. Flag-on keeps the full require_admin contract
# (401 no header / 403 wrong token / 503 secret unset).

@app.post("/api/admin/quotes")
def admin_quote_entry(body: AdminQuoteBody,
                      authorization: Optional[str] = Header(default=None)):
    """Path-C: concierge keys in a quote received by email/phone. Authoritative
    (a human already reviewed it) but the SAME sanity checks apply — a typo'd
    price flags for a second look regardless of who typed it. Records
    provenance submitted_via="concierge". Works before live sends exist."""
    if not _quote_submit_enabled():
        _quote_flag_off_404()
    role = require_admin(authorization)
    _validate_quote_fields(body)
    specs = _run_specs_for_quote(body.run_id)
    if specs is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _record_structured_quote(
        body,
        submitted_via="concierge",
        submitted_by=role,
        supplier_domain=body.supplier_domain,
        vendor_name=body.vendor_name,
        run_id=body.run_id,
        rfq_id=None,
        part_key=None,
        manufacturer=specs.get("manufacturer"),
        requested_part_number=specs.get("part_number"),
        requested_quantity=specs.get("quantity"),
    )


@app.get("/api/admin/quotes")
def admin_list_quotes(run_id: Optional[str] = None, status: Optional[str] = None,
                      authorization: Optional[str] = Header(default=None)):
    """Admin quote listing. `status` filters on the EFFECTIVE status (read-time
    expiry applied) — status=review is the concierge review queue (spec §6),
    each row carrying its reasons."""
    if not _quote_submit_enabled():
        _quote_flag_off_404()
    require_admin(authorization)
    from utils import quote_store
    return {"quotes": quote_store.get_quotes(run_id=run_id, status=status)}


@app.post("/api/admin/quotes/{quote_id}/approve")
def admin_approve_quote(quote_id: str,
                        authorization: Optional[str] = Header(default=None)):
    """Approve a review-flagged quote → active (it promotes from the next
    read). A pn_differs quote promotes labelled as the QUOTED PN
    (equivalent-alternative framing — criterion 4); approval never relabels
    it as the requested PN. 404 unknown; 409 when not in review."""
    if not _quote_submit_enabled():
        _quote_flag_off_404()
    role = require_admin(authorization)
    from utils import quote_store
    if quote_store.get_quote(quote_id) is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    out = quote_store.approve_review(quote_id, resolved_by=role)
    if out is None:
        raise HTTPException(status_code=409, detail="Quote is not in review")
    return {"ok": True, "quote": out}


@app.post("/api/admin/quotes/{quote_id}/reject")
def admin_reject_quote(quote_id: str,
                       authorization: Optional[str] = Header(default=None)):
    """Reject a review-flagged quote → withdrawn (never promotes). 404
    unknown; 409 when not in review."""
    if not _quote_submit_enabled():
        _quote_flag_off_404()
    role = require_admin(authorization)
    from utils import quote_store
    if quote_store.get_quote(quote_id) is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    out = quote_store.reject_review(quote_id, resolved_by=role)
    if out is None:
        raise HTTPException(status_code=409, detail="Quote is not in review")
    return {"ok": True, "quote": out}

