"""
Arkim Sourcing Engine — FastAPI server.

Exposes the existing SQLAlchemy-backed sourcing pipeline as REST endpoints
consumed by the React frontend. Runs alongside the Streamlit app without
conflict (separate port, same SQLite DB via WAL mode).

Start with:
    uvicorn api_server:app --reload --port 8001
"""

import json
import os
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
load_dotenv()

from utils.procurement_agent.agents.intake_agent import IntakeAgent
from utils.models import SourcingRun

import secrets

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Import the existing persistence layer
from utils.procurement_agent.state.persistence import (
    SourcingRunORM,
    _SessionFactory,
    Base,
    _engine,
)
from sqlalchemy import text
from utils.procurement_agent.state.phases import Phase

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Arkim Sourcing Engine API",
    version="1.0.0",
    description="REST API for the Arkim production frontend.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",    # Next.js dev server
        "http://127.0.0.1:3000",
        "http://localhost:8000",    # same-origin health checks
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
# Pydantic I/O models
# ---------------------------------------------------------------------------

class RunListItem(BaseModel):
    id: str
    phase: str
    urgency: str          # "Stocking" | "Predictive" | "Emergency"
    warranty: str         # "Active" | "Expired" | "Unknown"
    facility_id: str
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
    asset_specs: Optional[Dict[str, Any]] = None
    inventory_result: Optional[Dict[str, Any]] = None
    sourcing_results: Optional[Dict[str, Any]] = None
    selected_candidate: Optional[Dict[str, Any]] = None
    approval_history: List[Dict[str, Any]] = []
    messages: List[Dict[str, Any]] = []
    tier3_selection: Optional[List[str]] = None
    tier3_outreach_sent: Optional[Dict[str, str]] = None  # candidateId → sentAt ISO
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
    facility_id: str = "00000000-0000-0000-0000-000000000000"
    urgency_factor: float = Field(0.3, ge=0.0, le=1.0)
    warranty_status: str = "unknown"


class CreateRunResponse(BaseModel):
    id: str
    phase: str
    created_at: str


class SendMessageRequest(BaseModel):
    content: str
    role: str = "user"


class SendMessageResponse(BaseModel):
    run_id: str
    message: Dict[str, Any]
    updated_phase: str


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

def _lead_time_label(days: int) -> str:
    if days <= 1:  return "Next day"
    if days <= 5:  return f"{days} days"
    if days <= 14: return "1–2 weeks"
    if days <= 30: return "2–4 weeks"
    return "4+ weeks"


def _vendor_type(merchant_type: str) -> str:
    return {
        "Enterprise":           "NetworkPartner",
        "Direct Buy via Arkim": "NetworkPartner",
        "National Specialist":  "NationalDistributor",
        "Quote Request":        "RegionalSpecialist",
        "Local":                "RegionalSpecialist",
    }.get(merchant_type, "NationalDistributor")


def _pn_match_level(opt: dict, tier: int) -> str:
    if tier == 1:
        return "exact" if opt.get("found_part_number") else "none"
    return {
        "exact_match":   "exact",
        "partial_match": "normalized",
        "no_match":      "none",
        "not_visible":   "none",
    }.get(opt.get("pn_match_status") or "", "none")


def _transform_option(opt: dict, tier: int, idx: int) -> dict:
    price_hidden = opt.get("price_tbd", False) or opt.get("requires_rfq", False)
    return {
        "id":                    f"{opt.get('vendor_name','')}-t{tier}-{idx}",
        "vendorName":            opt.get("vendor_name") or "Unknown",
        "vendorType":            _vendor_type(opt.get("merchant_type") or ""),
        "tier":                  tier,
        "price":                 None if price_hidden else opt.get("base_price"),
        "leadTime":              _lead_time_label(int(opt.get("lead_time_days") or 0)),
        "url":                   opt.get("source_url") or "",
        "suitability":           float(opt.get("suitability_score") or 0),
        "confidence":            float(opt.get("confidence_score") or 0),
        "pnMatchLevel":          _pn_match_level(opt, tier),
        "loc":                   opt.get("ship_from_country") or "",
        "isExactMatch":          opt.get("match_type") == "Exact OEM",
        "isAftermarket":         opt.get("match_type") == "Aftermarket Compatible",
        "isOemDirect":           bool(opt.get("is_oem_direct")),
        "isAuthorizedDistributor": opt.get("vendor_authorization_status") == "Authorized",
        "priceVerified":         not opt.get("limited_price_data", False),
        "shipFrom":              opt.get("ship_from_country"),
        "contact":               opt.get("contact_email"),
        "relationship":          opt.get("suitability_tier") or None,
        "comparisonArtifact":    opt.get("comparison_artifact"),
        # Tier 1 two-mode display: all Tier 1 candidates start in confirmation-needed mode.
        # After POST /request-confirmation fires and mock response arrives (3-8 s),
        # confirmation_needed is set False in the raw data and this flips to False,
        # causing the frontend card to switch from "Request Confirmation" to "Buy Now".
        "confirmationPending":   tier == 1 and bool(opt.get("confirmation_needed", True)),
    }


def _transform_sourcing_results(raw: dict) -> dict:
    """Convert SourcingAgent output dict to the shape expected by the React frontend."""
    def _tier(key: str, n: int) -> list:
        return [
            _transform_option(o, n, i)
            for i, o in enumerate(raw.get(key, {}).get("results", []))
            if not o.get("rejection_reason")
        ]
    return {
        "tier1":               _tier("tier_1", 1),
        "tier2":               _tier("tier_2", 2),
        "tier3":               _tier("tier_3", 3),
        "warrantyBanner":      raw.get("warranty_banner"),
        "tier3CapabilityPivot": raw.get("tier3_capability_pivot", False),
    }


# ---------------------------------------------------------------------------
# Background sourcing task
# ---------------------------------------------------------------------------

def _run_sourcing_background(
    run_id: str,
    specs_dict: dict,
    urgency_factor: float,
    warranty_status: str,
) -> None:
    """Run SourcingAgent then ComparisonAgent in a single background thread.

    Order of operations is load-bearing:
    1. SourcingAgent runs → raw results held in memory, phase stays SOURCING (polling active).
    2. ComparisonAgent runs in parallel over all candidates (phase still SOURCING).
    3. Artifacts embedded into results, phase advances to COMPARISON, commit.

    Phase advances only after artifacts exist. Polling stops at COMPARISON, so the
    frontend always receives a complete payload on the first post-transition poll.
    """
    import logging
    from concurrent.futures import ThreadPoolExecutor
    log = logging.getLogger(__name__)

    # ── Step 1: Sourcing ────────────────────────────────────────────────────
    try:
        from utils.procurement_agent.agents.sourcing_agent import SourcingAgent
        from utils.models import SourcingRun as _SourcingRunModel
        agent = SourcingAgent(
            tavily_api_key=os.environ.get("TAVILY_API_KEY"),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        )
        run_model = _SourcingRunModel(
            id=run_id,
            current_phase="sourcing",
            asset_specs_json=specs_dict,
            urgency_factor=urgency_factor,
            warranty_status=warranty_status,
        )
        result = agent.run(run_model)
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
                # The error detail above is retained for debugging.
                orm.current_phase = Phase.ERROR.value
                orm.updated_at = datetime.now(timezone.utc)
                session.commit()
        return

    # ── Step 2: Comparison (parallel, phase stays SOURCING) ─────────────────
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

    # ── Step 3: Persist final results and advance phase ──────────────────────
    with _SessionFactory() as session:
        orm = session.get(SourcingRunORM, run_id)
        if orm and orm.current_phase == Phase.SOURCING.value:
            orm.sourcing_results_json = json.dumps(result)
            orm.current_phase = Phase.COMPARISON.value
            orm.updated_at = datetime.now(timezone.utc)
            session.commit()
    log.info("[%s] Sourcing + comparison complete → comparison", run_id)


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
        asset_summary=asset_summary,
        amount=None,
        maintenance_submission_id=handoff.get("submission_id"),
        created_at=run.initiated_at.isoformat() if run.initiated_at else "",
        updated_at=run.updated_at.isoformat() if run.updated_at else "",
    )


def _orm_to_detail(run: SourcingRunORM) -> RunDetail:
    def _parse(col): return json.loads(col) if col else None

    raw_sourcing = _parse(run.sourcing_results_json)
    # Transform if we have real sourcing data (not an error stub)
    sourcing: Optional[Dict[str, Any]] = None
    if raw_sourcing and "error" not in raw_sourcing:
        sourcing = _transform_sourcing_results(raw_sourcing)
    elif raw_sourcing:
        sourcing = raw_sourcing  # pass through error dict so frontend can surface it

    # Derive no_exact_match: fires when T2+T3 combined have at least one candidate
    # but none have pnMatchLevel=="exact" (mapped from pn_match_status=="exact_match").
    # Suppressed when spec_based_sourcing=True or part_number is absent/null-equivalent
    # (spec-based and no-PN scenarios are "by design," not typo cases).
    _null_pn_vals = {"", "N/A", "n/a", "null", "None", "UNKNOWN-PN", "Unknown", "unknown"}
    _asset_specs = _parse(run.asset_specs_json)
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
        asset_specs=_asset_specs,
        inventory_result=_parse(run.inventory_result_json),
        sourcing_results=sourcing,
        selected_candidate=_parse(run.selected_candidate_json),
        approval_history=json.loads(run.approval_history_json)
            if isinstance(run.approval_history_json, str)
            else (run.approval_history_json or []),
        tier3_selection=json.loads(run.tier3_selection_json) if run.tier3_selection_json else None,
        tier3_outreach_sent=json.loads(run.tier3_outreach_sent_json) if run.tier3_outreach_sent_json else None,
        maintenance_handoff=_parse(run.maintenance_handoff_json),
        no_exact_match=no_exact_match,
        created_at=run.initiated_at.isoformat() if run.initiated_at else "",
        updated_at=run.updated_at.isoformat() if run.updated_at else "",
    )


# ---------------------------------------------------------------------------
# Sourcing run endpoints
# ---------------------------------------------------------------------------

@app.post("/api/runs", response_model=CreateRunResponse, status_code=201)
def create_run(body: CreateRunRequest):
    """Create a new sourcing run and return it in intake phase."""
    now = datetime.now(timezone.utc)
    run = SourcingRunORM(
        id=str(uuid.uuid4()),
        facility_id=body.facility_id,
        current_phase=Phase.INTAKE.value,
        urgency_factor=body.urgency_factor,
        warranty_status=body.warranty_status,
        initiated_at=now,
        updated_at=now,
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


@app.get("/api/runs", response_model=List[RunListItem])
def list_runs(
    facility_id: Optional[str] = None,
    phase: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """List sourcing runs with optional filtering."""
    with _SessionFactory() as session:
        q = session.query(SourcingRunORM)
        if facility_id:
            q = q.filter(SourcingRunORM.facility_id == facility_id)
        if phase:
            q = q.filter(SourcingRunORM.current_phase == phase)
        runs = q.order_by(SourcingRunORM.initiated_at.desc()).offset(offset).limit(limit).all()
        return [_orm_to_list_item(r) for r in runs]


@app.post("/api/runs/from-maintenance", status_code=201)
def create_run_from_maintenance(body: MaintenanceSubmission):
    """Create a sourcing run in pending_intake from a maintenance handoff payload."""
    now = datetime.now(timezone.utc)
    urgency_factor = _URGENCY_FACTORS.get(body.context.urgency, 0.3)
    handoff_dict = body.model_dump()
    run = SourcingRunORM(
        id=str(uuid.uuid4()),
        facility_id=body.facility_id,
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
def get_run(run_id: str):
    """Fetch full run state by ID."""
    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run:
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
def send_message(run_id: str, body: SendMessageRequest):
    """
    Send a chat message to the live IntakeAgent.
    Extracts specs, updates asset_specs_json on the run, and returns the
    agent's clarification question (or a transition message when sufficient).
    """
    with _SessionFactory() as session:
        orm = session.get(SourcingRunORM, run_id)
        if not orm:
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

    # Find the last agent clarification question for context
    prior_question: Optional[str] = None
    for msg in reversed(thread[:-1]):
        if msg["role"] == "agent":
            prior_question = msg["content"]
            break

    # Run IntakeAgent
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    print(f"[send_message] run={run_id} api_key_present={bool(api_key)} text={body.content[:60]!r}")
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
        print(f"[send_message] IntakeAgent error for run={run_id} — returning 502")
        raise HTTPException(
            status_code=502,
            detail="Intake processing failed — please retry.",
        )
    print(f"[send_message] sufficient={result['sufficient']} mfg_conf={result['manufacturer_confidence']} part_conf={result['part_id_confidence']}")

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
        if _emit_caveat:
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

    return SendMessageResponse(
        run_id=run_id,
        message=agent_reply,
        updated_phase=new_phase,
    )


@app.post("/api/runs/{run_id}/upload")
async def upload_nameplate(run_id: str, file: UploadFile = File(...)):
    """
    Upload a nameplate image for vision extraction.

    Pipes image bytes to IntakeAgent multimodal extraction, updates asset_specs_json,
    and returns a three-case agent reply:
    (a) high confidence  — specs complete, confirm to proceed
    (b) low confidence   — partial extraction, ask user to verify
    (c) failed           — nothing readable, offer three recovery paths
    """
    with _SessionFactory() as session:
        orm = session.get(SourcingRunORM, run_id)
        if not orm:
            raise HTTPException(status_code=404, detail="Run not found")
        current_phase = orm.current_phase
        prior_specs: Dict[str, Any] = (
            json.loads(orm.asset_specs_json) if orm.asset_specs_json else {}
        )

    contents = await file.read()
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
        result = agent.run(run_obj, {"text": "", "images": [contents]})
    except Exception:
        traceback.print_exc()
        result = None

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

        # (a) High confidence — both thresholds met
        if result.get("sufficient"):
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

    return {
        "run_id":   run_id,
        "filename": file.filename,
        "size_bytes": len(contents),
        "message":  agent_reply,
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


@app.post("/api/runs/{run_id}/select-candidate")
def select_candidate(run_id: str, body: SelectCandidateRequest):
    """Lock in a candidate and advance the run to pending_first_approval."""
    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        run.selected_candidate_json = json.dumps({
            "candidate_id": body.candidate_id,
            "tier": body.tier,
            "selected_at": datetime.now(timezone.utc).isoformat(),
        })
        run.current_phase = Phase.PENDING_FIRST_APPROVAL.value
        run.updated_at = datetime.now(timezone.utc)
        session.commit()

    return {"run_id": run_id, "phase": Phase.PENDING_FIRST_APPROVAL.value}


@app.post("/api/runs/{run_id}/approve")
def approve_run(run_id: str, body: ApproveRequest):
    """
    Record an approval action.

    Advances: pending_first_approval → approved (single-approver path).
    Phase 3 will implement the dual-approver routing.
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
            "action": "approved",
            "notes": body.notes,
            "acted_at": datetime.now(timezone.utc).isoformat(),
        })
        run.approval_history_json = json.dumps(history)
        run.current_phase = Phase.APPROVED.value
        run.updated_at = datetime.now(timezone.utc)
        session.commit()

    return {"run_id": run_id, "phase": Phase.APPROVED.value}


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

    return {"run_id": run_id, "phase": Phase.COMPARISON.value}


@app.post("/api/runs/{run_id}/confirm-intake")
def confirm_intake(run_id: str, background_tasks: BackgroundTasks):
    """
    Confirm intake specs and atomically advance to sourcing.

    Writes the inventory stub and transitions phase in a single DB commit so
    there is no window where the run is phase=sourcing without inventory_result.
    Idempotent: returns 409 if the run is already past intake.
    """
    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run:
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
        specs_dict = json.loads(run.asset_specs_json)
        urgency_factor = run.urgency_factor
        warranty_status = run.warranty_status
        run.inventory_result_json = json.dumps({
            "status": "no_data",
            "message": "Inventory agent not yet connected (Phase 5).",
        })
        run.current_phase = Phase.SOURCING.value
        run.updated_at = datetime.now(timezone.utc)
        session.commit()

    background_tasks.add_task(
        _run_sourcing_background, run_id, specs_dict, urgency_factor, warranty_status
    )
    return {"run_id": run_id, "phase": Phase.SOURCING.value}


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
    Get approval rules for a facility.

    Phase 1: returns canonical Bay Foods Stockton rules as mock data.
    Phase 2+: reads from the approval_rules DB table.
    """
    return [
        ApprovalRuleOut(
            id="rule-1",
            facility_id=facility_id,
            threshold=0,
            cap=5000,
            approvers_required=1,
            approver_roles=["Maintenance Director"],
            applies_to="buy",
        ),
        ApprovalRuleOut(
            id="rule-2",
            facility_id=facility_id,
            threshold=5001,
            cap=24999,
            approvers_required=2,
            approver_roles=["Maintenance Director", "Operations Manager"],
            applies_to="buy",
        ),
        ApprovalRuleOut(
            id="rule-3",
            facility_id=facility_id,
            threshold=25000,
            cap=None,
            approvers_required=3,
            approver_roles=["Maintenance Director", "Operations Manager", "Plant Manager"],
            applies_to="buy",
        ),
        ApprovalRuleOut(
            id="rule-4",
            facility_id=facility_id,
            threshold=0,
            cap=None,
            approvers_required=1,
            approver_roles=["Maintenance Director"],
            applies_to="outreach",
        ),
    ]


@app.post("/api/approval-rules", response_model=ApprovalRuleOut, status_code=201)
def upsert_approval_rule(body: ApprovalRuleIn):
    """
    Create or update an approval rule.

    Phase 1: echoes the request back as a confirmed rule.
    Phase 2+: persists to the approval_rules DB table.
    """
    return ApprovalRuleOut(
        id=str(uuid.uuid4()),
        facility_id=body.facility_id,
        threshold=body.threshold,
        cap=body.cap,
        approvers_required=body.approvers_required,
        approver_roles=body.approver_roles,
        applies_to=body.applies_to,
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.0.0-phase1"}


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


@app.get("/api/admin/review-queue")
def admin_review_queue(role: str = Depends(require_admin)):
    """review_items — extracted quotes/contacts, confidence, status (incl.
    needs_human_review), raw source, the matched RFQ's run/domain/vendor."""
    from utils import supplier_registry
    rows = supplier_registry.get_review_items()
    return {"count": len(rows), "review_items": rows}


@app.get("/api/admin/orders")
def admin_orders(role: str = Depends(require_admin)):
    """orders — full lifecycle state + every captured field."""
    from utils import orders
    rows = orders.get_orders()
    return {"count": len(rows), "orders": rows}


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
