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
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from utils.models import ProcurementRun
from utils.procurement_agent.state.phases import Phase, validate_transition
from utils.procurement_agent.state.persistence import (
    create_run, get_run, update_run, list_runs, append_approval,
)
from utils.procurement_agent.state.approval_rules import determine_approval_path
from utils.audit_log import write_audit_log

logger = logging.getLogger(__name__)

# Build version written to every audit log entry produced by this orchestrator.
_AGENT_VERSION = "2.0.0-phase2"

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

    def select_candidate(self, candidate: dict) -> None:
        """Record the user's selected candidate and route to the appropriate approval path.

        Evaluates the facility's approval rules against the candidate's grand_total_usd
        and transitions to PENDING_FIRST_APPROVAL, storing the required approver count
        and roles in the selected_candidate_json.

        Args:
            candidate: dict from sourcing_results_json (any tier). Must include at
                       minimum 'vendor_name'. 'grand_total_usd' or 'base_price' is
                       used to look up the applicable approval rule.
        """
        data = get_run(self.run_id, db_url=self._db_url)
        if data is None:
            raise ValueError(f"ProcurementRun not found: {self.run_id}")

        current = Phase(data["current_phase"])
        if current != Phase.COMPARISON:
            raise ValueError(
                f"select_candidate() requires COMPARISON phase, got {current.value}"
            )

        total_usd = float(
            candidate.get("grand_total_usd")
            or candidate.get("base_price")
            or 0.0
        )
        facility_id = data.get("facility_id") or "00000000-0000-0000-0000-000000000000"

        approvers_required, approver_roles = determine_approval_path(
            facility_id, total_usd, db_url=self._db_url,
        )

        enriched = dict(candidate)
        enriched["_approval_path"] = {
            "approvers_required": approvers_required,
            "approver_roles":     approver_roles,
            "grand_total_usd":    total_usd,
        }

        update_run(
            self.run_id,
            {
                "selected_candidate_json": enriched,
                "current_phase": Phase.PENDING_FIRST_APPROVAL.value,
            },
            db_url=self._db_url,
        )
        self._write_audit(
            run_data=data,
            input_summary=(
                f"Candidate selected: {candidate.get('vendor_name', 'Unknown')} "
                f"${total_usd:.2f} — {approvers_required} approver(s) required"
            ),
        )
        logger.info(
            "[%s] Candidate selected → PENDING_FIRST_APPROVAL (%d approver(s))",
            self.run_id, approvers_required,
        )

    def submit_approval(
        self,
        action: str,
        notes: str = "",
        approver_role: str = "any_authorized_user",
        approver_id: Optional[str] = None,
    ) -> None:
        """Record an approval or rejection action and advance the phase.

        Args:
            action:       "approved" or "rejected"
            notes:        Optional approver notes
            approver_role: Display role label (no RBAC enforcement in prototype)
            approver_id:  Optional user ID (nullable for prototype)
        """
        if action not in ("approved", "rejected"):
            raise ValueError(f"action must be 'approved' or 'rejected', got {action!r}")

        data = get_run(self.run_id, db_url=self._db_url)
        if data is None:
            raise ValueError(f"ProcurementRun not found: {self.run_id}")

        current = Phase(data["current_phase"])
        if current not in (Phase.PENDING_FIRST_APPROVAL, Phase.PENDING_SECOND_APPROVAL):
            raise ValueError(
                f"submit_approval() requires a pending approval phase, got {current.value}"
            )

        history = data.get("approval_history_json") or []
        sequence = len(history) + 1

        # Persist the approval history row + JSON mirror
        append_approval(
            run_id=self.run_id,
            approver_id=approver_id,
            approver_role=approver_role,
            action=action,
            notes=notes or None,
            sequence=sequence,
            db_url=self._db_url,
        )

        if action == "rejected":
            next_phase = Phase.CANCELLED
        elif current == Phase.PENDING_SECOND_APPROVAL:
            next_phase = Phase.APPROVED
        else:
            # First approval — check if dual approval is required
            selected    = data.get("selected_candidate_json") or {}
            path        = selected.get("_approval_path") or {}
            req_count   = int(path.get("approvers_required", 1))
            next_phase  = Phase.PENDING_SECOND_APPROVAL if req_count >= 2 else Phase.APPROVED

        if not validate_transition(current, next_phase):
            raise ValueError(
                f"Invalid approval transition: {current.value} → {next_phase.value}"
            )

        update_run(
            self.run_id,
            {"current_phase": next_phase.value},
            db_url=self._db_url,
        )
        self._write_audit(
            run_data=data,
            input_summary=(
                f"Approval action [{sequence}]: {action} by {approver_role} "
                f"→ {next_phase.value}"
            ),
        )
        logger.info(
            "[%s] Approval [%d] %s → %s", self.run_id, sequence, action, next_phase.value
        )

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
        """Phase 2: calls the real SourcingAgent — three-tier parallel search."""
        import os
        from utils.procurement_agent.agents.sourcing_agent import SourcingAgent

        run   = self._dict_to_model(data)
        agent = SourcingAgent(
            tavily_api_key=os.environ.get("TAVILY_API_KEY"),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        )
        try:
            result = agent.run(run)
        except Exception as exc:
            logger.error("[%s] SourcingAgent failed: %s", self.run_id, exc)
            result = {
                "tier_1": {"results": [], "count": 0, "status": "error"},
                "tier_2": {"results": [], "count": 0, "status": "error"},
                "tier_3": {"results": [], "count": 0, "status": "error"},
                "warranty_banner": None,
                "urgency_applied": "unknown",
                "filters_applied": [],
                "error": str(exc),
            }

        self._advance(
            data,
            Phase.COMPARISON,
            output_key="sourcing_results_json",
            output_value=result,
        )

    def _stub_comparison(self, data: dict) -> None:
        """Phase 3: run SpecComparisonAgent for all candidates, then stay in COMPARISON.

        Does NOT auto-advance. The UI drives the transition to PENDING_FIRST_APPROVAL
        when the user selects a candidate via select_candidate().
        """
        import os
        from utils.procurement_agent.agents.spec_comparison_agent import SpecComparisonAgent

        run   = self._dict_to_model(data)
        agent = SpecComparisonAgent(
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        )
        sr = data.get("sourcing_results_json") or {}
        if not sr:
            logger.info("[%s] Comparison: no sourcing results — skipping.", self.run_id)
            return  # stay in COMPARISON

        enriched_sr = dict(sr)
        for tier_key, tier_num in [("tier_1", 1), ("tier_2", 2), ("tier_3", 3)]:
            tier_data = sr.get(tier_key) or {}
            results   = list(tier_data.get("results") or [])
            for idx, candidate in enumerate(results):
                candidate["_candidate_id"] = f"{tier_key}_{idx}"
                try:
                    artifact = agent.run(run, candidate, tier=tier_num)
                    candidate["comparison_artifact"] = artifact
                except Exception as exc:
                    logger.warning("[%s] Comparison failed for %s[%d]: %s",
                                   self.run_id, tier_key, idx, exc)
                    candidate["comparison_artifact"] = None
            enriched_sr[tier_key] = dict(tier_data, results=results)

        update_run(
            self.run_id,
            {"sourcing_results_json": enriched_sr},
            db_url=self._db_url,
        )
        self._write_audit(
            run_data=data,
            input_summary="Comparison phase: spec comparison artifacts generated for all candidates",
        )
        logger.info("[%s] Comparison artifacts attached — awaiting candidate selection.", self.run_id)

    def _stub_pending_first_approval(self, data: dict) -> None:
        """Stub: auto-approves for test suite compatibility.

        In production the UI calls submit_approval() directly.
        """
        self._advance(data, Phase.APPROVED, output_key=None, output_value=None)

    def _stub_pending_second_approval(self, data: dict) -> None:
        """Stub: auto-approves second approval for test suite compatibility."""
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
                return datetime.now(timezone.utc)
            if isinstance(v, datetime):
                return v
            try:
                return datetime.fromisoformat(v)
            except Exception:
                return datetime.now(timezone.utc)

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
