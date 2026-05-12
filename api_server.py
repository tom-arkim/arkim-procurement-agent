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

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile, File
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
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
                log.info("_migrate_schema: %s", stmt)
            except Exception:
                pass  # column already exists


_migrate_schema()


def _seed_demo_maintenance_run() -> None:
    """Create one demo pending_intake run if none exists (idempotent)."""
    with _SessionFactory() as session:
        existing = (
            session.query(SourcingRunORM)
            .filter(SourcingRunORM.current_phase == Phase.PENDING_INTAKE.value)
            .first()
        )
        if existing:
            return
        handoff = {
            "submission_id": "maint-sub-demo-001",
            "facility_id": "fac-stockton",
            "submitted_by": "Jake Martinez",
            "asset_specs": {
                "manufacturer": "Endress+Hauser",
                "model": "Promag 10W",
                "part_number": "10W40-AA2B1AA0AAAA",
                "description": "Electromagnetic flow meter",
            },
            "context": {
                "chat_thread_summary": (
                    "Jake reported intermittent flow reading failures on the Promag 10W "
                    "electromagnetic flow meter (tag PUMP-BL-042) in the bottling line. "
                    "Unit shows E:731 error code and readings drop to zero for 5–10 s "
                    "every 2–3 hours. Likely coil or transmitter fault. Urgency: emergency — "
                    "line cannot run at full capacity until resolved."
                ),
                "urgency": "emergency",
                "work_order_id": "WO-2024-0892",
                "asset_tag": "PUMP-BL-042",
            },
        }
        now = datetime.now(timezone.utc)
        run = SourcingRunORM(
            id=str(uuid.uuid4()),
            facility_id="fac-stockton",
            current_phase=Phase.PENDING_INTAKE.value,
            urgency_factor=0.9,
            warranty_status="unknown",
            asset_specs_json=json.dumps(handoff["asset_specs"]),
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
    maintenance_handoff: Optional[Dict[str, Any]] = None
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
}

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
    }


def _transform_sourcing_results(raw: dict) -> dict:
    """Convert SourcingAgent output dict to the shape expected by the React frontend."""
    def _tier(key: str, n: int) -> list:
        return [
            _transform_option(o, n, i)
            for i, o in enumerate(raw.get(key, {}).get("results", []))
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

    return RunDetail(
        id=run.id,
        phase=run.current_phase,
        urgency=_urgency_label(run.urgency_factor),
        warranty=_warranty_label(run.warranty_status),
        facility_id=run.facility_id,
        facility_state=_FACILITY_STATES.get(run.facility_id, "unknown"),
        asset_specs=_parse(run.asset_specs_json),
        inventory_result=_parse(run.inventory_result_json),
        sourcing_results=sourcing,
        selected_candidate=_parse(run.selected_candidate_json),
        approval_history=json.loads(run.approval_history_json)
            if isinstance(run.approval_history_json, str)
            else (run.approval_history_json or []),
        tier3_selection=json.loads(run.tier3_selection_json) if run.tier3_selection_json else None,
        maintenance_handoff=_parse(run.maintenance_handoff_json),
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
        traceback.print_exc()
        print(f"[send_message] IntakeAgent error for run={run_id} — returning synthetic reply")
        err_reply: Dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "role": "agent",
            "content": "I hit an error processing your message. Please try rephrasing or restart the run.",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        thread.append(err_reply)
        return SendMessageResponse(run_id=run_id, message=err_reply, updated_phase=current_phase)
    print(f"[send_message] sufficient={result['sufficient']} mfg_conf={result['manufacturer_confidence']} part_conf={result['part_id_confidence']}")

    # Determine reply — do NOT auto-advance on sufficient=True.
    # The confirm-intake endpoint owns the intake → sourcing transition.
    proceed_state = result.get("confidence_summary", {}).get("proceed_state", "")
    if result["sufficient"]:
        if proceed_state == "proceed_with_manufacturer_caveat":
            reply_text = (
                "Specs extracted but the manufacturer could not be confirmed. "
                "Verify the manufacturer in the panel before confirming."
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

    Phase 1: validates the run exists and returns a stub extraction result.
    Phase 2+: pipes to utils/vision.py.
    """
    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

    contents = await file.read()

    upload_msg: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "role": "system",
        "content": f"Nameplate uploaded: {file.filename}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "attachment": {
            "type": "image",
            "filename": file.filename,
            "size_bytes": len(contents),
        },
    }
    agent_msg: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "role": "agent",
        "content": (
            f"I've received the nameplate image ({file.filename}, "
            f"{len(contents):,} bytes). Vision extraction will run in Phase 4 — "
            "for now, please type the key specs (manufacturer, model, part number) "
            "so I can start searching."
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    thread = _messages.setdefault(run_id, [])
    thread.append(upload_msg)
    thread.append(agent_msg)

    return {
        "run_id": run_id,
        "filename": file.filename,
        "size_bytes": len(contents),
        "extraction": {
            "status": "stub",
            "message": "Vision extraction will run in Phase 4.",
            "fields_extracted": 0,
        },
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
    """
    Initiate Tier 3 outreach after approval.

    Phase 1: records the selected vendors and returns stub status.
    Phase 2+: triggers actual email dispatch.
    """
    with _SessionFactory() as session:
        run = session.get(SourcingRunORM, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        run.updated_at = datetime.now(timezone.utc)
        session.commit()

    return {
        "run_id": run_id,
        "candidates_contacted": len(body.candidate_ids),
        "status": "stub — outreach dispatch will run in Phase 2",
        "sent_at": datetime.now(timezone.utc).isoformat(),
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
