"""
Arkim Sourcing Engine — FastAPI server.

Exposes the existing SQLAlchemy-backed sourcing pipeline as REST endpoints
consumed by the React frontend (the shipping front end). SQLite DB via WAL mode.

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
from utils.marketplace_registry import is_marketplace

import secrets

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
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
            # D2 prereq #1 — nullable tenant key (company PIN). Keys only, no enforcement.
            "ALTER TABLE sourcing_runs ADD COLUMN company_id VARCHAR(36)",
            # Multi-part Increment 1 — nullable basket grouping label. NULL for every
            # single-part run (indistinguishable from a pre-migration run); set only when
            # an intake fans one request into N runs. The CREATE INDEX is separate (below)
            # because the bare ALTER does not index the column on already-migrated DBs.
            "ALTER TABLE sourcing_runs ADD COLUMN group_id VARCHAR(36)",
            "CREATE INDEX IF NOT EXISTS ix_sourcing_runs_group_id ON sourcing_runs (group_id)",
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
    facility_id: str = "00000000-0000-0000-0000-000000000000"
    urgency_factor: float = Field(0.3, ge=0.0, le=1.0)
    warranty_status: str = "unknown"


class CreateRunResponse(BaseModel):
    id: str
    phase: str
    created_at: str


class IntakeRequest(BaseModel):
    """Front-door intake body (multi-part Increment 1, Stage 2). Only the COUNT of `parts`
    routes single-vs-multi; the part CONTENTS are not parsed here — intake yields one
    asset_specs per run today, and multi-part extraction is a separate later concern. An
    empty or single-element `parts` takes the existing single-run path verbatim."""
    parts: List[Dict[str, Any]] = []
    facility_id: str = "00000000-0000-0000-0000-000000000000"
    urgency_factor: float = Field(0.3, ge=0.0, le=1.0)
    warranty_status: str = "unknown"


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
        "leadTime":              _lead_time_label(int(opt.get("lead_time_days") or 0)),
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
        "priceUnverified":       price is not None and 0 < float(opt.get("confidence_score") or 0) < _PRICE_CONFIDENCE_FLOOR,
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
    breaks the run-detail read."""
    try:
        from utils import supplier_registry
        quotes = supplier_registry.get_review_items(run_id=run_id, kind="quote")
        sent = supplier_registry.get_sent_messages(run_id=run_id)
        return _index_quotes(quotes, sent)
    except Exception as exc:  # never let a quote-store hiccup break the run view
        import logging
        logging.getLogger(__name__).warning(
            "[%s] _build_quote_index failed, no quote overlay: %s", run_id, exc)
        return {"by_thread": {}, "by_domain": {}, "domain_threads": {}}


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
        overlay["leadTime"] = payload["lead_time"]   # quote free-text lead overrides label
    if payload.get("terms"):
        overlay["terms"] = payload["terms"]
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

def _result_from_cached_edges(edges: list) -> dict:
    """Reconstruct the SourcingAgent result shape from cached known_parts edges, so a
    cache hit flows through the same transform/persist path without re-discovery.

    A STALE cached price is marked low-confidence so the transform flags it
    priceUnverified (a stale price shown as current is an overclaim — same honesty
    discipline as the unverified-price work). No comparison artifact on cache hits —
    the spec comparison is recomputed only on a fresh discovery."""
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
        t = e.get("tier") if e.get("tier") in (1, 2, 3) else (3 if price is None else 2)
        tiers[t].append(cand)

    def _t(n: int) -> dict:
        return {"results": tiers[n], "count": len(tiers[n]), "status": "ok"}

    return {
        "tier_1": _t(1), "tier_2": _t(2), "tier_3": _t(3),
        "warranty_banner": None, "urgency_applied": "cached",
        "filters_applied": ["known_parts_cache"], "tier3_capability_pivot": False,
    }


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

    # ── Step 0: Cache-first (known_parts) ────────────────────────────────────
    # Canonical part-key (aliases + PN-normalize) so the cache doesn't fork on
    # "Gusher" vs "Gusher Pumps". exact_only runs bypass the cache (filtered set).
    part_key = ""
    try:
        from utils import known_parts
        part_key = known_parts.canonical_part_key(
            specs_dict.get("manufacturer"), specs_dict.get("part_number"))
        if part_key and not specs_dict.get("exact_only"):
            edges = known_parts.get_edges(part_key)
            if edges:
                result = _result_from_cached_edges(edges)
                log.info("[%s] known_parts cache HIT (%d suppliers) — skipping discovery", run_id, len(edges))
    except Exception as exc:
        log.warning("[%s] known_parts cache read failed: %s", run_id, exc)
        result = None

    if result is None:
        # ── Step 1: Sourcing (discovery) ─────────────────────────────────────
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
                written = known_parts.upsert_edges(part_key, cands)
                log.info("[%s] known_parts write-back: %d supplier edge(s) for %r", run_id, written, part_key)
        except Exception as exc:
            log.warning("[%s] known_parts write-back failed: %s", run_id, exc)

    # ── Step 3: Persist final results and advance phase (both paths) ─────────
    with _SessionFactory() as session:
        orm = session.get(SourcingRunORM, run_id)
        if orm and orm.current_phase == Phase.SOURCING.value:
            orm.sourcing_results_json = json.dumps(result)
            orm.current_phase = Phase.COMPARISON.value
            orm.updated_at = datetime.now(timezone.utc)
            session.commit()
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
) -> SourcingRunORM:
    """Build (do not persist) a fresh run at phase=intake — the SINGLE construction the
    create path uses. Shared by create_run (commits one) and _fan_out_intake (commits N in
    one transaction), so fan-out reuses the create path instead of reimplementing it.
    group_id is NULL for a single run, the shared basket label for a fanned one."""
    now = datetime.now(timezone.utc)
    return SourcingRunORM(
        id=str(uuid.uuid4()),
        facility_id=facility_id,
        company_id=company_id,
        group_id=group_id,
        current_phase=Phase.INTAKE.value,
        urgency_factor=urgency_factor,
        warranty_status=warranty_status,
        initiated_at=now,
        updated_at=now,
    )


@app.post("/api/runs", response_model=CreateRunResponse, status_code=201)
def create_run(body: CreateRunRequest, caller: Optional[Caller] = Depends(get_caller)):
    """Create a new sourcing run and return it in intake phase.

    D2 prereq #1 (keys only): stamp the run's tenant key (company PIN) from the verified
    Caller when one is present — NEVER from the body. No token (today's demo) -> NULL."""
    run = _new_run_orm(
        facility_id=body.facility_id,
        urgency_factor=body.urgency_factor,
        warranty_status=body.warranty_status,
        company_id=caller.company_id if caller else None,
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
def create_request(body: IntakeRequest, caller: Optional[Caller] = Depends(get_caller)):
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

    return {"run_id": run_id, "phase": Phase.COMPARISON.value}


@app.post("/api/runs/{run_id}/confirm-intake")
def confirm_intake(run_id: str, background_tasks: BackgroundTasks, exact_only: bool = False):
    """
    Confirm intake specs and atomically advance to sourcing.

    Writes the inventory stub and transitions phase in a single DB commit so
    there is no window where the run is phase=sourcing without inventory_result.
    Idempotent: returns 409 if the run is already past intake.

    exact_only=true ("find exact replacements only" — the no-spec-sheet honesty
    branch): records the flag so the background sourcing drops aftermarket/equivalent
    Tier 2/3 candidates, surfacing only exact OEM matches (Tier 1 network unaffected).
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
        if exact_only:
            specs_dict["exact_only"] = True
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
def get_group(group_id: str):
    """Read-only basket rollup over the runs sharing `group_id`: per-run part/phase/selected
    amount, a derived basket status, and the basket_total (the exact figure Stage 5 routes
    on). No writes. Fail-soft: a malformed child degrades to an error row, never 500-ing the
    basket. Unknown group -> 404."""
    import logging
    from utils.procurement_agent.state import persistence
    log = logging.getLogger(__name__)
    try:
        runs = persistence.list_runs(group_id=group_id, limit=500)
    except Exception as exc:
        log.warning("[groups] list_runs failed for %s: %s", group_id, exc)
        runs = []
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
    # Gate BEFORE send: only 'approved' -> 'sent' is legal. A drafted/rejected/sent draft 409s
    # and send_rfq is never reached.
    if not persistence.can_transition_draft(draft["status"], "sent"):
        raise HTTPException(
            status_code=409,
            detail=f"draft is '{draft['status']}' — only an approved draft can be sent",
        )

    approval = rfq_send.Approval(approved_by=draft["approved_by"], approved_at=draft["approved_at"])
    result = rfq_send.send_rfq(
        draft["candidate_snapshot"],   # the frozen snapshot the human approved (vendor_name + source_url)
        draft["draft_body"],
        approval,
        run_id=draft["run_id"],
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
