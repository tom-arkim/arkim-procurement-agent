"""
SQLAlchemy persistence layer for ProcurementRun.

Design notes:
- SQLite for prototype; Postgres-ready schema (UUID PKs, JSON columns, indexed FKs).
- Sessions are scoped per-call — no leaked sessions.
- ApprovalHistory rows are separate ORM objects linked to ProcurementRun via FK,
  but also mirrored in the parent's approval_history_json for single-query reads.
- Brief reference: Section 5.
"""

import json
import os
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, create_engine, event,
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
_DB_PATH = os.path.join(_DATA_DIR, "procurement_runs.sqlite")


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


class ProcurementRunORM(Base):
    __tablename__ = "procurement_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    facility_id = Column(String(36), nullable=False, index=True)
    initiated_by_user_id = Column(String(36), nullable=True)
    initiated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    current_phase = Column(String(40), nullable=False, default=Phase.INTAKE.value, index=True)
    urgency_factor = Column(Float, nullable=False, default=0.3)
    warranty_status = Column(String(20), nullable=False, default="unknown")

    asset_specs_json = Column(Text, nullable=True)       # JSON blob
    inventory_result_json = Column(Text, nullable=True)
    sourcing_results_json = Column(Text, nullable=True)
    selected_candidate_json = Column(Text, nullable=True)
    approval_history_json = Column(Text, nullable=False, default="[]")

    vendor_order_id = Column(String(200), nullable=True)
    fulfillment_status = Column(String(30), nullable=True)
    inventory_update_json = Column(Text, nullable=True)
    work_order_link = Column(String(500), nullable=True)

    audit_log_run_id = Column(String(36), nullable=True)
    agent_version = Column(String(40), nullable=False, default="2.0.0-phase1")

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    approval_history = relationship(
        "ApprovalHistoryORM",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="ApprovalHistoryORM.acted_at",
    )

    __table_args__ = (
        Index("ix_procurement_runs_facility_phase", "facility_id", "current_phase"),
    )


class ApprovalHistoryORM(Base):
    __tablename__ = "approval_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), ForeignKey("procurement_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    approver_id = Column(String(36), nullable=True)
    approver_role = Column(String(80), nullable=True)
    action = Column(String(20), nullable=False)   # "approved" | "rejected" | "requested_changes"
    notes = Column(Text, nullable=True)
    acted_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    run = relationship("ProcurementRunORM", back_populates="approval_history")


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


def _orm_to_dict(row: ProcurementRunORM) -> dict:
    return {
        "id": row.id,
        "facility_id": row.facility_id,
        "initiated_by_user_id": row.initiated_by_user_id,
        "initiated_at": row.initiated_at.isoformat() if row.initiated_at else None,
        "current_phase": row.current_phase,
        "urgency_factor": row.urgency_factor,
        "warranty_status": row.warranty_status,
        "asset_specs_json": _pj(row.asset_specs_json),
        "inventory_result_json": _pj(row.inventory_result_json),
        "sourcing_results_json": _pj(row.sourcing_results_json),
        "selected_candidate_json": _pj(row.selected_candidate_json),
        "approval_history_json": _pj(row.approval_history_json) or [],
        "vendor_order_id": row.vendor_order_id,
        "fulfillment_status": row.fulfillment_status,
        "inventory_update_json": _pj(row.inventory_update_json),
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
    db_url: Optional[str] = None,
) -> dict:
    """Insert a new ProcurementRun and return it as a dict."""
    session = _get_session(db_url)
    try:
        row = ProcurementRunORM(
            id=str(uuid.uuid4()),
            facility_id=facility_id,
            initiated_by_user_id=initiated_by_user_id,
            initiated_at=datetime.utcnow(),
            current_phase=Phase.INTAKE.value,
            urgency_factor=urgency_factor,
            warranty_status=warranty_status,
            asset_specs_json=_j(asset_specs),
            approval_history_json="[]",
            agent_version=agent_version,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
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
        row = session.get(ProcurementRunORM, run_id)
        return _orm_to_dict(row) if row else None
    finally:
        session.close()


def update_run(run_id: str, updates: dict, db_url: Optional[str] = None) -> Optional[dict]:
    """Apply a dict of field updates to an existing run. Returns updated dict or None."""
    session = _get_session(db_url)
    try:
        row = session.get(ProcurementRunORM, run_id)
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

        row.updated_at = datetime.utcnow()
        session.commit()
        return _orm_to_dict(row)
    finally:
        session.close()


def list_runs(
    facility_id: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    db_url: Optional[str] = None,
) -> list[dict]:
    """Return recent runs ordered by created_at DESC."""
    session = _get_session(db_url)
    try:
        q = session.query(ProcurementRunORM).order_by(ProcurementRunORM.created_at.desc())
        if facility_id:
            q = q.filter(ProcurementRunORM.facility_id == facility_id)
        rows = q.offset(offset).limit(limit).all()
        return [_orm_to_dict(r) for r in rows]
    finally:
        session.close()


def append_approval(
    run_id: str,
    approver_id: Optional[str],
    approver_role: Optional[str],
    action: str,
    notes: Optional[str] = None,
    db_url: Optional[str] = None,
) -> bool:
    """Add an ApprovalHistory row and keep the JSON mirror in sync. Returns True on success."""
    session = _get_session(db_url)
    try:
        row = session.get(ProcurementRunORM, run_id)
        if row is None:
            return False
        entry = ApprovalHistoryORM(
            run_id=run_id,
            approver_id=approver_id,
            approver_role=approver_role,
            action=action,
            notes=notes,
            acted_at=datetime.utcnow(),
        )
        session.add(entry)

        # Keep JSON mirror up to date for single-query UI reads.
        history = _pj(row.approval_history_json) or []
        history.append({
            "approver_id": approver_id,
            "approver_role": approver_role,
            "action": action,
            "notes": notes,
            "acted_at": datetime.utcnow().isoformat(),
        })
        row.approval_history_json = _j(history)
        row.updated_at = datetime.utcnow()
        session.commit()
        return True
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
