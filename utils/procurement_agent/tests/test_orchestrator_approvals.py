"""
Tests for Phase 3 orchestrator approval flow:
select_candidate(), submit_approval(), dual-approval path, rejection, history capture.
"""

import pytest
from utils.procurement_agent.orchestrator.core import Orchestrator, start_new_run
from utils.procurement_agent.state.persistence import (
    create_run, get_run, update_run, upsert_approval_rule,
)
from utils.procurement_agent.state.phases import Phase


_FACILITY_DEFAULT = "00000000-0000-0000-0000-000000000000"
_FACILITY_DUAL    = "dddddddd-0000-0000-0000-000000000000"


def _run_to_comparison(db_url: str, facility_id: str = _FACILITY_DEFAULT) -> Orchestrator:
    """Create a new run and advance it to COMPARISON phase (with stubs for intake/inventory/sourcing)."""
    orch = start_new_run(facility_id=facility_id, db_url=db_url)
    orch.execute_current_phase()  # INTAKE → INVENTORY
    orch.execute_current_phase()  # INVENTORY → SOURCING
    orch.execute_current_phase()  # SOURCING → COMPARISON
    orch.execute_current_phase()  # COMPARISON: runs agents, stays in COMPARISON
    return orch


def _cheap_candidate() -> dict:
    return {"vendor_name": "National Seal", "base_price": 340.0, "grand_total_usd": 340.0}


def _expensive_candidate() -> dict:
    return {"vendor_name": "Gulf Coast Motor", "base_price": 10_000.0, "grand_total_usd": 10_000.0}


# ---------------------------------------------------------------------------
# select_candidate() — transition from COMPARISON to PENDING_FIRST_APPROVAL
# ---------------------------------------------------------------------------

class TestSelectCandidate:
    def test_select_candidate_transitions_to_pending_first(self, db_url):
        orch = _run_to_comparison(db_url)
        orch.select_candidate(_cheap_candidate())
        state = orch.get_state()
        assert state["current_phase"] == Phase.PENDING_FIRST_APPROVAL.value

    def test_select_candidate_stores_vendor_name(self, db_url):
        orch = _run_to_comparison(db_url)
        orch.select_candidate(_cheap_candidate())
        state = orch.get_state()
        selected = state["selected_candidate_json"]
        assert selected["vendor_name"] == "National Seal"

    def test_select_candidate_stores_approval_path(self, db_url):
        orch = _run_to_comparison(db_url)
        orch.select_candidate(_cheap_candidate())
        state    = orch.get_state()
        selected = state["selected_candidate_json"]
        path     = selected["_approval_path"]
        assert "approvers_required" in path
        assert "approver_roles" in path

    def test_select_candidate_wrong_phase_raises(self, db_url):
        orch = start_new_run(db_url=db_url)  # INTAKE phase
        with pytest.raises(ValueError, match="COMPARISON"):
            orch.select_candidate(_cheap_candidate())

    def test_select_candidate_uses_grand_total_for_routing(self, db_url):
        """$340 candidate uses default $0 rule → single approver."""
        orch = _run_to_comparison(db_url)
        orch.select_candidate(_cheap_candidate())
        state = orch.get_state()
        path  = state["selected_candidate_json"]["_approval_path"]
        assert path["approvers_required"] == 1


# ---------------------------------------------------------------------------
# Single-approver path — PENDING_FIRST_APPROVAL → APPROVED
# ---------------------------------------------------------------------------

class TestSingleApproverPath:
    def test_single_approval_reaches_approved(self, db_url):
        orch = _run_to_comparison(db_url)
        orch.select_candidate(_cheap_candidate())
        orch.submit_approval("approved", notes="Looks good", approver_role="maintenance_director")
        state = orch.get_state()
        assert state["current_phase"] == Phase.APPROVED.value

    def test_approval_history_captured(self, db_url):
        orch = _run_to_comparison(db_url)
        orch.select_candidate(_cheap_candidate())
        orch.submit_approval("approved", notes="OK", approver_role="maintenance_director")
        state   = orch.get_state()
        history = state["approval_history_json"]
        assert len(history) == 1
        assert history[0]["action"] == "approved"
        assert history[0]["approver_role"] == "maintenance_director"
        assert history[0]["sequence"] == 1

    def test_approval_notes_captured(self, db_url):
        orch = _run_to_comparison(db_url)
        orch.select_candidate(_cheap_candidate())
        orch.submit_approval("approved", notes="Emergency purchase pre-approved", approver_role="vp")
        state   = orch.get_state()
        history = state["approval_history_json"]
        assert history[0]["notes"] == "Emergency purchase pre-approved"


# ---------------------------------------------------------------------------
# Dual-approver path — PENDING_FIRST_APPROVAL → PENDING_SECOND_APPROVAL → APPROVED
# ---------------------------------------------------------------------------

class TestDualApproverPath:
    def _setup_dual_approval_facility(self, db_url: str) -> None:
        """Configure FACILITY_DUAL with a $0 baseline requiring 2 approvers."""
        upsert_approval_rule(
            facility_id=_FACILITY_DUAL,
            threshold_usd=0,
            approvers_required=2,
            approver_roles=["maintenance_director", "operations_manager"],
            db_url=db_url,
        )

    def test_dual_approval_first_goes_to_pending_second(self, db_url):
        self._setup_dual_approval_facility(db_url)
        orch = _run_to_comparison(db_url, facility_id=_FACILITY_DUAL)
        orch.select_candidate(_cheap_candidate())

        state = orch.get_state()
        assert state["selected_candidate_json"]["_approval_path"]["approvers_required"] == 2

        orch.submit_approval("approved", approver_role="maintenance_director")
        state = orch.get_state()
        assert state["current_phase"] == Phase.PENDING_SECOND_APPROVAL.value

    def test_dual_approval_second_goes_to_approved(self, db_url):
        self._setup_dual_approval_facility(db_url)
        orch = _run_to_comparison(db_url, facility_id=_FACILITY_DUAL)
        orch.select_candidate(_cheap_candidate())
        orch.submit_approval("approved", approver_role="maintenance_director")
        orch.submit_approval("approved", approver_role="operations_manager")
        state = orch.get_state()
        assert state["current_phase"] == Phase.APPROVED.value

    def test_dual_approval_history_has_two_entries(self, db_url):
        self._setup_dual_approval_facility(db_url)
        orch = _run_to_comparison(db_url, facility_id=_FACILITY_DUAL)
        orch.select_candidate(_cheap_candidate())
        orch.submit_approval("approved", approver_role="maintenance_director")
        orch.submit_approval("approved", approver_role="operations_manager")
        state   = orch.get_state()
        history = state["approval_history_json"]
        assert len(history) == 2
        assert history[0]["sequence"] == 1
        assert history[1]["sequence"] == 2
        assert history[1]["approver_role"] == "operations_manager"

    def test_expensive_candidate_default_rules_dual_approval(self, db_url):
        """$10k candidate with default rules → dual approval required."""
        orch = _run_to_comparison(db_url)
        orch.select_candidate(_expensive_candidate())
        state = orch.get_state()
        path  = state["selected_candidate_json"]["_approval_path"]
        assert path["approvers_required"] == 2


# ---------------------------------------------------------------------------
# Rejection — transitions to CANCELLED from any pending approval state
# ---------------------------------------------------------------------------

class TestRejection:
    def test_reject_from_pending_first_transitions_to_cancelled(self, db_url):
        orch = _run_to_comparison(db_url)
        orch.select_candidate(_cheap_candidate())
        orch.submit_approval("rejected", notes="Budget exceeded", approver_role="director")
        state = orch.get_state()
        assert state["current_phase"] == Phase.CANCELLED.value

    def test_rejection_captured_in_history(self, db_url):
        orch = _run_to_comparison(db_url)
        orch.select_candidate(_cheap_candidate())
        orch.submit_approval("rejected", notes="Wrong part", approver_role="engineer")
        state   = orch.get_state()
        history = state["approval_history_json"]
        assert history[0]["action"] == "rejected"
        assert history[0]["notes"] == "Wrong part"

    def test_reject_from_pending_second_transitions_to_cancelled(self, db_url):
        upsert_approval_rule(
            facility_id=_FACILITY_DUAL,
            threshold_usd=0,
            approvers_required=2,
            approver_roles=["director", "vp"],
            db_url=db_url,
        )
        orch = _run_to_comparison(db_url, facility_id=_FACILITY_DUAL)
        orch.select_candidate(_cheap_candidate())
        orch.submit_approval("approved", approver_role="director")
        orch.submit_approval("rejected", notes="Risk too high", approver_role="vp")
        state = orch.get_state()
        assert state["current_phase"] == Phase.CANCELLED.value

    def test_invalid_action_raises(self, db_url):
        orch = _run_to_comparison(db_url)
        orch.select_candidate(_cheap_candidate())
        with pytest.raises(ValueError, match="action"):
            orch.submit_approval("maybe", approver_role="director")

    def test_submit_approval_wrong_phase_raises(self, db_url):
        orch = start_new_run(db_url=db_url)  # INTAKE phase
        with pytest.raises(ValueError, match="pending approval"):
            orch.submit_approval("approved", approver_role="director")
