"""
SQLAlchemy persistence layer for SourcingRun.

Design notes:
- SQLite for prototype; Postgres-ready schema (UUID PKs, JSON columns, indexed FKs).
- Sessions are scoped per-call — no leaked sessions.
- ApprovalHistory rows are separate ORM objects linked to SourcingRun via FK,
  but also mirrored in the parent's approval_history_json for single-query reads.
- Brief reference: Section 5.
- DB file: data/sourcing_runs.sqlite (renamed from procurement_runs.sqlite in
  Rectification Sprint — existing data not migrated; prototype only).
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, create_engine, event, text,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

from utils.procurement_agent.state.phases import Phase

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "data",
)
_DB_PATH = os.path.join(_DATA_DIR, "sourcing_runs.sqlite")


def _db_url(path: str = _DB_PATH) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return f"sqlite:///{path}"


def _make_engine(db_url: str):
    engine = create_engine(db_url, echo=False, future=True)
    # Enable WAL mode for SQLite so reads don't block writes.
    @event.listens_for(engine, "connect")
    def _set_wal(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
    return engine


_engine = _make_engine(_db_url())
_SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class SourcingRunORM(Base):
    __tablename__ = "sourcing_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    facility_id = Column(String(36), nullable=False, index=True)
    # Tenant key (company PIN) — D2 prereq #1. NULLABLE and unused for enforcement this
    # increment: populated from the validated X-Arkim-CompanyId / Caller.company_id when
    # identity lands, NULL in the current no-auth demo. facility_id stays the SEPARATE
    # site-level label (no rename; facility_id != Site.id reconciliation still open).
    company_id = Column(String(36), nullable=True, index=True)
    # Multi-part Increment 1 — nullable basket grouping label. NULL for single-part runs
    # (a NULL-group run is byte-for-byte a today-run); set only when an intake fans one
    # request into N independent runs. Groups many runs; never unique. The one-run-one-part
    # invariant is unchanged — the basket groups runs, it does not make a run hold N parts.
    group_id = Column(String(36), nullable=True, index=True)
    # DEMO_MODE session isolation — an ephemeral per-visitor id (the X-Session-Id header)
    # stamped on a run at birth so the public no-login demo can scope reads/writes to the
    # visitor who created the run (IDOR fix). NULL for every non-demo run and for seeded
    # maintenance runs (which are therefore unreadable by demo visitors). Deliberately a
    # SEPARATE column from company_id: company_id is the validated Cognito tenant PIN
    # (auth-derived, Arc 1); session_id is an untrusted client-generated demo token. Mixing
    # them would let a demo session id masquerade as a real tenant PIN later (false-tenant
    # hazard). Inert when DEMO_MODE is off — the column is simply never populated then.
    session_id = Column(String(64), nullable=True, index=True)
    initiated_by_user_id = Column(String(36), nullable=True)
    initiated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    current_phase = Column(String(40), nullable=False, default=Phase.INTAKE.value, index=True)
    urgency_factor = Column(Float, nullable=False, default=0.3)
    warranty_status = Column(String(20), nullable=False, default="unknown")
    # Phase B0 — the run's document knowledge (state only). "none" = today's default
    # (document-less direct sourcing). Per-candidate match_basis/match_wave live in
    # sourcing_results_json (no column needed — it's a JSON blob).
    document_status = Column(String(20), nullable=False, default="none", server_default="none")

    asset_specs_json = Column(Text, nullable=True)       # JSON blob
    inventory_result_json = Column(Text, nullable=True)
    sourcing_results_json = Column(Text, nullable=True)
    selected_candidate_json = Column(Text, nullable=True)
    approval_history_json = Column(Text, nullable=False, default="[]")
    tier3_selection_json = Column(Text, nullable=True)
    tier3_outreach_sent_json = Column(Text, nullable=True)   # JSON: {candidateId: sentAt ISO}
    maintenance_handoff_json = Column(Text, nullable=True)

    vendor_order_id = Column(String(200), nullable=True)
    fulfillment_status = Column(String(30), nullable=True)
    inventory_update_json = Column(Text, nullable=True)
    work_order_link = Column(String(500), nullable=True)

    audit_log_run_id = Column(String(36), nullable=True)
    agent_version = Column(String(40), nullable=False, default="2.0.0-phase1")

    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    approval_history = relationship(
        "ApprovalHistoryORM",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="ApprovalHistoryORM.acted_at",
    )

    __table_args__ = (
        Index("ix_sourcing_runs_facility_phase", "facility_id", "current_phase"),
    )


class ApprovalHistoryORM(Base):
    __tablename__ = "approval_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), ForeignKey("sourcing_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence = Column(Integer, nullable=False, default=1)  # 1 = first approver, 2 = second
    approver_id = Column(String(36), nullable=True)
    approver_role = Column(String(80), nullable=True)
    action = Column(String(20), nullable=False)   # "approved" | "rejected"
    notes = Column(Text, nullable=True)
    acted_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    run = relationship("SourcingRunORM", back_populates="approval_history")


class ApprovalRuleORM(Base):
    __tablename__ = "approval_rules"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    facility_id = Column(String(36), nullable=False, index=True)
    threshold_usd = Column(Float, nullable=False)
    approvers_required = Column(Integer, nullable=False, default=1)
    approver_roles = Column(Text, nullable=False, default="[]")  # JSON array as text
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_approval_rules_facility_threshold", "facility_id", "threshold_usd"),
    )


class RequestGroupApprovalORM(Base):
    """The basket-level approval decision (multi-part Increment 1, Stage 5). One row per
    basket (group_id). It holds the decision routed on the BASKET TOTAL — the human(s)
    authorise the whole cart once; the per-run approval_history of each child references
    this decision rather than recording N independent human approvals. Created on first
    approve (new table, auto-created by create_all — no ALTER)."""
    __tablename__ = "request_group_approvals"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id = Column(String(36), nullable=False, index=True)
    facility_id = Column(String(36), nullable=False)
    basket_total = Column(Float, nullable=False, default=0.0)
    approvers_required = Column(Integer, nullable=False, default=1)
    # JSON array of {approver_id, approver_name, approver_role, at} — mirrors approval_history.
    approvals_received_json = Column(Text, nullable=False, default="[]")
    # pending_first | pending_second | approved | rejected
    status = Column(String(30), nullable=False, default="pending_first")
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class RfqDraftORM(Base):
    """A persisted, reviewable RFQ draft (RFQ wiring A0). The HITL spine: a draft is created
    (drafted), a human reviews the STORED draft and records a real approval (approved) or
    rejects it (rejected), and only an approved draft can be sent (sent, A2). The lifecycle is
    enforced — a draft can NEVER reach 'sent' without passing through 'approved' (see
    ALLOWED_DRAFT_TRANSITIONS / transition_draft). No endpoints, no send live yet.

    candidate_snapshot_json freezes the hydrated candidate at draft time, so the human reviews
    exactly what was sourced. approved_by/approved_at mirror the Approval dataclass so A2 can
    build the Approval from the row with no re-keying; they are stored as the ISO STRING the
    Approval carries. sent_message_id is a SOFT reference to sent_messages.id (that table lives
    in a separate sqlite DB — no cross-DB SQL FK), populated only at the A2 send."""
    __tablename__ = "rfq_drafts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String(36), ForeignKey("sourcing_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    candidate_id = Column(String(200), nullable=False)        # display id {vendor}-t{tier}-{idx}
    candidate_snapshot_json = Column(Text, nullable=False)    # frozen hydrated candidate dict
    draft_body = Column(Text, nullable=False)                 # what _make_draft produced
    status = Column(String(20), nullable=False, default="drafted")  # drafted|approved|rejected|sent
    approved_by = Column(String(120), nullable=True)
    approved_at = Column(String(40), nullable=True)           # ISO str — mirrors Approval.approved_at
    rejected_by = Column(String(120), nullable=True)
    rejected_at = Column(String(40), nullable=True)
    sent_message_id = Column(String(36), nullable=True)       # soft ref to sent_messages.id (cross-DB)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


def migrate_run_state(engine) -> None:
    """Phase B0 additive migration: ensure the document_status column exists on
    sourcing_runs for pre-B0 databases. Idempotent (guarded ALTER) and additive —
    existing rows read 'none' (today's behavior). Per-candidate match_basis/match_wave
    live in the sourcing_results_json blob and need no schema change.
    """
    with engine.connect() as conn:
        try:
            conn.execute(text(
                "ALTER TABLE sourcing_runs ADD COLUMN document_status TEXT DEFAULT 'none'"))
            conn.commit()
        except Exception:
            pass  # column already exists — idempotent no-op


# Create tables on first import.
Base.metadata.create_all(_engine)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _j(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _pj(value: Optional[str]):
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return value


def _orm_to_dict(row: SourcingRunORM) -> dict:
    return {
        "id": row.id,
        "facility_id": row.facility_id,
        "company_id": getattr(row, "company_id", None),
        "group_id": getattr(row, "group_id", None),
        "session_id": getattr(row, "session_id", None),
        "initiated_by_user_id": row.initiated_by_user_id,
        "initiated_at": row.initiated_at.isoformat() if row.initiated_at else None,
        "current_phase": row.current_phase,
        "urgency_factor": row.urgency_factor,
        "warranty_status": row.warranty_status,
        "document_status": getattr(row, "document_status", None) or "none",
        "asset_specs_json": _pj(row.asset_specs_json),
        "inventory_result_json": _pj(row.inventory_result_json),
        "sourcing_results_json": _pj(row.sourcing_results_json),
        "selected_candidate_json": _pj(row.selected_candidate_json),
        "approval_history_json": _pj(row.approval_history_json) or [],
        "vendor_order_id": row.vendor_order_id,
        "fulfillment_status": row.fulfillment_status,
        "inventory_update_json": _pj(row.inventory_update_json),
        "tier3_selection_json": _pj(row.tier3_selection_json),
        "maintenance_handoff_json": _pj(row.maintenance_handoff_json),
        "work_order_link": row.work_order_link,
        "audit_log_run_id": row.audit_log_run_id,
        "agent_version": row.agent_version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_run(
    facility_id: str = "00000000-0000-0000-0000-000000000000",
    initiated_by_user_id: Optional[str] = None,
    urgency_factor: float = 0.3,
    warranty_status: str = "unknown",
    asset_specs: Optional[dict] = None,
    agent_version: str = "2.0.0-phase1",
    company_id: Optional[str] = None,
    db_url: Optional[str] = None,
) -> dict:
    """Insert a new SourcingRun and return it as a dict. company_id is the tenant PIN
    (nullable; NULL until identity forwards it — D2 prereq #1, keys only)."""
    session = _get_session(db_url)
    try:
        row = SourcingRunORM(
            id=str(uuid.uuid4()),
            facility_id=facility_id,
            company_id=company_id,
            initiated_by_user_id=initiated_by_user_id,
            initiated_at=datetime.now(timezone.utc),
            current_phase=Phase.INTAKE.value,
            urgency_factor=urgency_factor,
            warranty_status=warranty_status,
            asset_specs_json=_j(asset_specs),
            approval_history_json="[]",
            agent_version=agent_version,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.commit()
        return _orm_to_dict(row)
    finally:
        session.close()


def get_run(run_id: str, db_url: Optional[str] = None) -> Optional[dict]:
    """Fetch a single run by ID. Returns None if not found."""
    session = _get_session(db_url)
    try:
        row = session.get(SourcingRunORM, run_id)
        return _orm_to_dict(row) if row else None
    finally:
        session.close()


def update_run(run_id: str, updates: dict, db_url: Optional[str] = None) -> Optional[dict]:
    """Apply a dict of field updates to an existing run. Returns updated dict or None."""
    session = _get_session(db_url)
    try:
        row = session.get(SourcingRunORM, run_id)
        if row is None:
            return None

        _JSON_FIELDS = {
            "asset_specs_json", "inventory_result_json", "sourcing_results_json",
            "selected_candidate_json", "approval_history_json", "inventory_update_json",
        }
        for key, value in updates.items():
            if not hasattr(row, key):
                continue
            if key in _JSON_FIELDS:
                setattr(row, key, _j(value))
            else:
                setattr(row, key, value)

        row.updated_at = datetime.now(timezone.utc)
        session.commit()
        return _orm_to_dict(row)
    finally:
        session.close()


def list_runs(
    facility_id: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    db_url: Optional[str] = None,
    group_id: Optional[str] = None,
) -> list[dict]:
    """Return recent runs ordered by created_at DESC. `group_id` returns only one basket's
    runs (additive; absent -> unchanged)."""
    session = _get_session(db_url)
    try:
        q = session.query(SourcingRunORM).order_by(SourcingRunORM.created_at.desc())
        if facility_id:
            q = q.filter(SourcingRunORM.facility_id == facility_id)
        if group_id:
            q = q.filter(SourcingRunORM.group_id == group_id)
        rows = q.offset(offset).limit(limit).all()
        return [_orm_to_dict(r) for r in rows]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# RFQ drafts — persistence + enforced lifecycle (RFQ wiring A0)
#
# The integrity spine: a draft can NEVER reach 'sent' without passing through 'approved'.
# Mirrors the orders.py state-machine discipline (illegal transitions are rejected, not
# recorded) — but here the helper RAISES on an illegal transition.
# ---------------------------------------------------------------------------

ALLOWED_DRAFT_TRANSITIONS: dict[str, set[str]] = {
    "drafted":  {"approved", "rejected"},
    "approved": {"sent"},
    "rejected": set(),   # terminal
    "sent":     set(),   # terminal
}


class DraftTransitionError(ValueError):
    """Raised when an RFQ-draft status transition is illegal (skip-approval, backward,
    resurrect a rejected/sent draft, re-approve, or advance a terminal draft)."""


def can_transition_draft(current: str, new: str) -> bool:
    """True iff `current -> new` is a legal draft-lifecycle transition (pure; no I/O)."""
    return new in ALLOWED_DRAFT_TRANSITIONS.get(current, set())


def _draft_to_dict(row: RfqDraftORM) -> dict:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "candidate_id": row.candidate_id,
        "candidate_snapshot": _pj(row.candidate_snapshot_json),
        "draft_body": row.draft_body,
        "status": row.status,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at,
        "rejected_by": row.rejected_by,
        "rejected_at": row.rejected_at,
        "sent_message_id": row.sent_message_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def create_draft(
    run_id: str,
    candidate_id: str,
    candidate_snapshot: dict,
    draft_body: str,
    db_url: Optional[str] = None,
) -> dict:
    """Create an RFQ draft at status='drafted'. candidate_snapshot is frozen as JSON so the
    human later reviews exactly what was sourced. No approval, no send."""
    session = _get_session(db_url)
    try:
        row = RfqDraftORM(
            id=str(uuid.uuid4()),
            run_id=run_id,
            candidate_id=candidate_id,
            candidate_snapshot_json=_j(candidate_snapshot),
            draft_body=draft_body,
            status="drafted",
        )
        session.add(row)
        session.commit()
        return _draft_to_dict(row)
    finally:
        session.close()


def get_draft(draft_id: str, db_url: Optional[str] = None) -> Optional[dict]:
    """Fetch one draft by id. None if not found."""
    session = _get_session(db_url)
    try:
        row = session.get(RfqDraftORM, draft_id)
        return _draft_to_dict(row) if row else None
    finally:
        session.close()


def list_drafts(run_id: str, db_url: Optional[str] = None) -> list[dict]:
    """All drafts for a run, newest first."""
    session = _get_session(db_url)
    try:
        rows = (
            session.query(RfqDraftORM)
            .filter(RfqDraftORM.run_id == run_id)
            .order_by(RfqDraftORM.created_at.desc())
            .all()
        )
        return [_draft_to_dict(r) for r in rows]
    finally:
        session.close()


def transition_draft(
    draft_id: str,
    new_status: str,
    *,
    approved_by: Optional[str] = None,
    rejected_by: Optional[str] = None,
    sent_message_id: Optional[str] = None,
    db_url: Optional[str] = None,
) -> dict:
    """Move a draft to `new_status`, ENFORCING the legal lifecycle (drafted -> approved |
    rejected; approved -> sent; terminals are terminal). Raises DraftTransitionError on an
    illegal transition (KeyError-free) and on an unknown draft_id. The approval/rejection is
    recorded AS PART OF the transition (approved_at/rejected_at stamped here, ISO), so a
    recorded approval is real — tied to the act, never preset while drafted."""
    session = _get_session(db_url)
    try:
        row = session.get(RfqDraftORM, draft_id)
        if row is None:
            raise DraftTransitionError(f"draft {draft_id} not found")
        if not can_transition_draft(row.status, new_status):
            raise DraftTransitionError(
                f"illegal draft transition: {row.status} -> {new_status}"
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        if new_status == "approved":
            row.approved_by = approved_by
            row.approved_at = now_iso
        elif new_status == "rejected":
            row.rejected_by = rejected_by
            row.rejected_at = now_iso
        elif new_status == "sent":
            row.sent_message_id = sent_message_id
        row.status = new_status
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
        return _draft_to_dict(row)
    finally:
        session.close()


def append_approval(
    run_id: str,
    approver_id: Optional[str],
    approver_role: Optional[str],
    action: str,
    notes: Optional[str] = None,
    sequence: int = 1,
    db_url: Optional[str] = None,
) -> bool:
    """Add an ApprovalHistory row and keep the JSON mirror in sync. Returns True on success."""
    session = _get_session(db_url)
    try:
        row = session.get(SourcingRunORM, run_id)
        if row is None:
            return False
        entry = ApprovalHistoryORM(
            run_id=run_id,
            sequence=sequence,
            approver_id=approver_id,
            approver_role=approver_role,
            action=action,
            notes=notes,
            acted_at=datetime.now(timezone.utc),
        )
        session.add(entry)

        # Keep JSON mirror up to date for single-query UI reads.
        history = _pj(row.approval_history_json) or []
        history.append({
            "sequence": sequence,
            "approver_id": approver_id,
            "approver_role": approver_role,
            "action": action,
            "notes": notes,
            "acted_at": datetime.now(timezone.utc).isoformat(),
        })
        row.approval_history_json = _j(history)
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
        return True
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Approval rules CRUD
# ---------------------------------------------------------------------------

def _rule_to_dict(row: ApprovalRuleORM) -> dict:
    return {
        "id":                 row.id,
        "facility_id":        row.facility_id,
        "threshold_usd":      row.threshold_usd,
        "approvers_required": row.approvers_required,
        "approver_roles":     _pj(row.approver_roles) or [],
        "created_at":         row.created_at.isoformat() if row.created_at else None,
        "updated_at":         row.updated_at.isoformat() if row.updated_at else None,
    }


def list_approval_rules(
    facility_id: str,
    db_url: Optional[str] = None,
) -> list[dict]:
    """Return all approval rules for a facility, ordered by threshold ascending."""
    session = _get_session(db_url)
    try:
        rows = (
            session.query(ApprovalRuleORM)
            .filter(ApprovalRuleORM.facility_id == facility_id)
            .order_by(ApprovalRuleORM.threshold_usd)
            .all()
        )
        return [_rule_to_dict(r) for r in rows]
    finally:
        session.close()


def upsert_approval_rule(
    facility_id: str,
    threshold_usd: float,
    approvers_required: int,
    approver_roles: list,
    rule_id: Optional[str] = None,
    db_url: Optional[str] = None,
) -> dict:
    """Insert or update an approval rule. Returns the rule dict."""
    session = _get_session(db_url)
    try:
        if rule_id:
            row = session.get(ApprovalRuleORM, rule_id)
        else:
            row = None

        now = datetime.now(timezone.utc)
        if row is None:
            row = ApprovalRuleORM(
                id=rule_id or str(uuid.uuid4()),
                facility_id=facility_id,
                threshold_usd=threshold_usd,
                approvers_required=approvers_required,
                approver_roles=_j(approver_roles),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.threshold_usd      = threshold_usd
            row.approvers_required = approvers_required
            row.approver_roles     = _j(approver_roles)
            row.updated_at         = now

        session.commit()
        return _rule_to_dict(row)
    finally:
        session.close()


def delete_approval_rule(rule_id: str, db_url: Optional[str] = None) -> bool:
    """Delete an approval rule by ID. Returns True if found and deleted."""
    session = _get_session(db_url)
    try:
        row = session.get(ApprovalRuleORM, rule_id)
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True
    finally:
        session.close()


def delete_facility_rules(facility_id: str, db_url: Optional[str] = None) -> int:
    """Delete all rules for a facility. Returns the count deleted."""
    session = _get_session(db_url)
    try:
        deleted = (
            session.query(ApprovalRuleORM)
            .filter(ApprovalRuleORM.facility_id == facility_id)
            .delete()
        )
        session.commit()
        return deleted
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Session factory helper — accepts optional db_url for test isolation
# ---------------------------------------------------------------------------

def _get_session(db_url: Optional[str] = None) -> Session:
    if db_url is None:
        return _SessionFactory()
    engine = _make_engine(db_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return factory()
