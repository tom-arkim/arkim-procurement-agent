"""
Orchestrator — coordinates agents and owns ProcurementRun state transitions.

Brief reference: Section 4 (Orchestrator pattern) and Section 5 (state model).

Phase 1: agent calls in execute_current_phase() are stubs that transition to the
next phase immediately. Phases 2-4 replace these with real agent logic.

The orchestrator never contains business logic — it delegates to agents and
persists state. If logic is being added here that isn't pure coordination,
it belongs in an agent class instead.
"""

import logging
import time
from datetime import datetime
from typing import Optional
from uuid import UUID

from utils.models import ProcurementRun
from utils.procurement_agent.state.phases import Phase, validate_transition
from utils.procurement_agent.state.persistence import (
    create_run, get_run, update_run, list_runs,
)
from utils.audit_log import write_audit_log

logger = logging.getLogger(__name__)

# Build version written to every audit log entry produced by this orchestrator.
_AGENT_VERSION = "2.0.0-phase1"

# Mapping from current phase to the stub function that handles it.
# Phase 2-4 replace these stubs with imports from the real agent classes.
_PHASE_HANDLERS: dict[str, str] = {
    Phase.INTAKE.value:                  "_stub_intake",
    Phase.INVENTORY.value:               "_stub_inventory",
    Phase.SOURCING.value:                "_stub_sourcing",
    Phase.COMPARISON.value:              "_stub_comparison",
    Phase.PENDING_FIRST_APPROVAL.value:  "_stub_pending_first_approval",
    Phase.PENDING_SECOND_APPROVAL.value: "_stub_pending_second_approval",
    Phase.APPROVED.value:                "_stub_approved",
    Phase.EXECUTING.value:               "_stub_executing",
    Phase.FULFILLING.value:              "_stub_fulfilling",
}

# Default next phase for each stub (happy path only).
_NEXT_PHASE: dict[str, Phase] = {
    Phase.INTAKE.value:                  Phase.INVENTORY,
    Phase.INVENTORY.value:               Phase.SOURCING,
    Phase.SOURCING.value:                Phase.COMPARISON,
    Phase.COMPARISON.value:              Phase.PENDING_FIRST_APPROVAL,
    Phase.PENDING_FIRST_APPROVAL.value:  Phase.APPROVED,   # skip second approval in stubs
    Phase.PENDING_SECOND_APPROVAL.value: Phase.APPROVED,
    Phase.APPROVED.value:                Phase.EXECUTING,
    Phase.EXECUTING.value:               Phase.FULFILLING,
    Phase.FULFILLING.value:              Phase.COMPLETED,
}


class Orchestrator:
    """Coordinates a single ProcurementRun through its lifecycle.

    Usage:
        orch = Orchestrator(run_id)
        run  = orch.load()
        orch.execute_current_phase()   # advances to next phase (stub in Phase 1)
        state = orch.get_state()
    """

    def __init__(self, run_id: str, db_url: Optional[str] = None):
        self.run_id = str(run_id)
        self._db_url = db_url  # None = production DB; override for tests

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self) -> ProcurementRun:
        """Load the current ProcurementRun from the database."""
        data = get_run(self.run_id, db_url=self._db_url)
        if data is None:
            raise ValueError(f"ProcurementRun not found: {self.run_id}")
        return self._dict_to_model(data)

    def transition_to(self, next_phase: Phase) -> None:
        """Validate and apply a phase transition, then persist and write audit log."""
        data = get_run(self.run_id, db_url=self._db_url)
        if data is None:
            raise ValueError(f"ProcurementRun not found: {self.run_id}")

        current = Phase(data["current_phase"])
        if not validate_transition(current, next_phase):
            raise ValueError(
                f"Invalid transition: {current.value} → {next_phase.value}"
            )

        update_run(
            self.run_id,
            {"current_phase": next_phase.value},
            db_url=self._db_url,
        )

        self._write_audit(
            run_data=data,
            input_summary=f"Phase transition: {current.value} -> {next_phase.value}",
        )
        logger.info("[%s] Transitioned %s → %s", self.run_id, current.value, next_phase.value)

    def execute_current_phase(self) -> None:
        """Run the handler for the current phase and advance to the next phase.

        In Phase 1 all handlers are stubs. Phase 2-4 replace them with real agent logic.
        """
        data = get_run(self.run_id, db_url=self._db_url)
        if data is None:
            raise ValueError(f"ProcurementRun not found: {self.run_id}")

        current_phase = data["current_phase"]
        handler_name = _PHASE_HANDLERS.get(current_phase)
        if handler_name is None:
            logger.warning("[%s] No handler for phase %s — no-op", self.run_id, current_phase)
            return

        handler = getattr(self, handler_name)
        handler(data)

    def get_state(self) -> dict:
        """Return the current run state as a plain dict (for UI consumption)."""
        data = get_run(self.run_id, db_url=self._db_url)
        if data is None:
            raise ValueError(f"ProcurementRun not found: {self.run_id}")
        return data

    # ------------------------------------------------------------------
    # Phase 1 stubs — each advances to the next phase after a no-op
    # ------------------------------------------------------------------

    def _stub_intake(self, data: dict) -> None:
        """Stub: Phase 2 replaces with IntakeAgent.run() — multimodal extraction + sufficiency check."""
        self._advance(data, Phase.INVENTORY, output_key=None, output_value=None)

    def _stub_inventory(self, data: dict) -> None:
        """Stub: Phase 5 replaces with InventoryAgent.run() — inventory lookup."""
        self._advance(
            data,
            Phase.SOURCING,
            output_key="inventory_result_json",
            output_value={"status": "no_data", "message": "Inventory agent not yet connected (Phase 5)."},
        )

    def _stub_sourcing(self, data: dict) -> None:
        """Stub: Phase 2 replaces with SourcingAgent.run() — three-tier parallel search."""
        self._advance(
            data,
            Phase.COMPARISON,
            output_key="sourcing_results_json",
            output_value={
                "tier1": [], "tier2": [], "tier3": [],
                "stub": True, "message": "Sourcing agent not yet implemented (Phase 2).",
            },
        )

    def _stub_comparison(self, data: dict) -> None:
        """Stub: Phase 3 replaces with SpecComparisonAgent.run() — per-candidate comparison."""
        self._advance(data, Phase.PENDING_FIRST_APPROVAL, output_key=None, output_value=None)

    def _stub_pending_first_approval(self, data: dict) -> None:
        """Stub: Phase 3 replaces with ApprovalRulesEngine — auto-approves in stub mode."""
        self._advance(data, Phase.APPROVED, output_key=None, output_value=None)

    def _stub_pending_second_approval(self, data: dict) -> None:
        """Stub: Phase 3 replaces with second-approver workflow."""
        self._advance(data, Phase.APPROVED, output_key=None, output_value=None)

    def _stub_approved(self, data: dict) -> None:
        """Stub: Phase 4 replaces with ProcurementAgent.run() — vendor order execution."""
        self._advance(data, Phase.EXECUTING, output_key=None, output_value=None)

    def _stub_executing(self, data: dict) -> None:
        """Stub: Phase 4 replaces with fulfillment initiation."""
        self._advance(data, Phase.FULFILLING, output_key=None, output_value=None)

    def _stub_fulfilling(self, data: dict) -> None:
        """Stub: Phase 4 replaces with fulfillment tracking and inventory reconciliation."""
        self._advance(data, Phase.COMPLETED, output_key=None, output_value=None)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _advance(
        self,
        data: dict,
        next_phase: Phase,
        output_key: Optional[str],
        output_value,
    ) -> None:
        updates: dict = {"current_phase": next_phase.value}
        if output_key and output_value is not None:
            updates[output_key] = output_value

        current_phase = data["current_phase"]
        current = Phase(current_phase)
        if not validate_transition(current, next_phase):
            raise ValueError(
                f"Stub tried invalid transition: {current.value} → {next_phase.value}"
            )

        update_run(self.run_id, updates, db_url=self._db_url)
        self._write_audit(
            run_data=data,
            input_summary=f"Phase transition: {current_phase} -> {next_phase.value}",
        )
        logger.info("[%s] %s → %s", self.run_id, current_phase, next_phase.value)

    def _write_audit(self, run_data: dict, input_summary: str) -> None:
        try:
            write_audit_log({
                "sourcing_run_id":     self.run_id,
                "agent_version":       _AGENT_VERSION,
                "user_id":             run_data.get("initiated_by_user_id"),
                "asset_specs_json":    run_data.get("asset_specs_json"),
                "input_summary":       input_summary,
                "urgency_factor_used": run_data.get("urgency_factor", 0.3),
                "warranty_status_used": run_data.get("warranty_status"),
                "workflow_mode":       "procurement_run_v2",
                "llm_calls_made":      0,
                "estimated_llm_cost_usd": 0.0,
            })
        except Exception as exc:
            logger.warning("[%s] Audit log write failed: %s", self.run_id, exc)

    @staticmethod
    def _dict_to_model(data: dict) -> ProcurementRun:
        def _dt(v):
            if v is None:
                return datetime.utcnow()
            if isinstance(v, datetime):
                return v
            try:
                return datetime.fromisoformat(v)
            except Exception:
                return datetime.utcnow()

        return ProcurementRun(
            id=data["id"],
            facility_id=data["facility_id"],
            initiated_by_user_id=data.get("initiated_by_user_id"),
            initiated_at=_dt(data.get("initiated_at")),
            current_phase=data["current_phase"],
            urgency_factor=data.get("urgency_factor", 0.3),
            warranty_status=data.get("warranty_status", "unknown"),
            asset_specs_json=data.get("asset_specs_json"),
            inventory_result_json=data.get("inventory_result_json"),
            sourcing_results_json=data.get("sourcing_results_json"),
            selected_candidate_json=data.get("selected_candidate_json"),
            approval_history_json=data.get("approval_history_json") or [],
            vendor_order_id=data.get("vendor_order_id"),
            fulfillment_status=data.get("fulfillment_status"),
            inventory_update_json=data.get("inventory_update_json"),
            work_order_link=data.get("work_order_link"),
            audit_log_run_id=data.get("audit_log_run_id"),
            agent_version=data.get("agent_version", _AGENT_VERSION),
            created_at=_dt(data.get("created_at")),
            updated_at=_dt(data.get("updated_at")),
        )


# ---------------------------------------------------------------------------
# Convenience factory — create a new run and return a ready Orchestrator
# ---------------------------------------------------------------------------

def start_new_run(
    facility_id: str = "00000000-0000-0000-0000-000000000000",
    urgency_factor: float = 0.3,
    warranty_status: str = "unknown",
    asset_specs: Optional[dict] = None,
    initiated_by_user_id: Optional[str] = None,
    db_url: Optional[str] = None,
) -> "Orchestrator":
    """Create a persisted ProcurementRun and return its Orchestrator."""
    data = create_run(
        facility_id=facility_id,
        initiated_by_user_id=initiated_by_user_id,
        urgency_factor=urgency_factor,
        warranty_status=warranty_status,
        asset_specs=asset_specs,
        db_url=db_url,
    )
    return Orchestrator(data["id"], db_url=db_url)
