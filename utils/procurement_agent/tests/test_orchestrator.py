"""
Tests for Orchestrator: state loading, persistence, transitions, audit log.
"""

import pytest
from utils.procurement_agent.orchestrator.core import Orchestrator, start_new_run
from utils.procurement_agent.state.persistence import create_run, get_run
from utils.procurement_agent.state.phases import Phase
from utils.audit_log import recent_entries


# ---------------------------------------------------------------------------
# Basic load / state round-trip
# ---------------------------------------------------------------------------

def test_start_new_run_returns_orchestrator(db_url):
    orch = start_new_run(db_url=db_url)
    assert isinstance(orch, Orchestrator)
    assert orch.run_id is not None


def test_load_returns_procurement_run_model(db_url):
    from utils.models import SourcingRun
    orch = start_new_run(db_url=db_url)
    run = orch.load()
    assert isinstance(run, SourcingRun)
    assert run.current_phase == Phase.INTAKE.value


def test_get_state_returns_dict(db_url):
    orch = start_new_run(db_url=db_url)
    state = orch.get_state()
    assert isinstance(state, dict)
    assert state["current_phase"] == Phase.INTAKE.value


def test_load_raises_for_missing_run_id(db_url):
    orch = Orchestrator("00000000-0000-0000-0000-000000000000", db_url=db_url)
    with pytest.raises(ValueError, match="not found"):
        orch.load()


# ---------------------------------------------------------------------------
# Transition validation
# ---------------------------------------------------------------------------

def test_transition_to_valid_phase(db_url):
    orch = start_new_run(db_url=db_url)
    orch.transition_to(Phase.INVENTORY)
    state = orch.get_state()
    assert state["current_phase"] == Phase.INVENTORY.value


def test_transition_to_invalid_phase_raises(db_url):
    orch = start_new_run(db_url=db_url)
    # INTAKE → SOURCING skips INVENTORY — invalid
    with pytest.raises(ValueError, match="Invalid transition"):
        orch.transition_to(Phase.SOURCING)


def test_transition_to_cancelled_from_any_phase(db_url):
    orch = start_new_run(db_url=db_url)
    orch.transition_to(Phase.CANCELLED)
    state = orch.get_state()
    assert state["current_phase"] == Phase.CANCELLED.value


# ---------------------------------------------------------------------------
# Full stub run through all phases
# ---------------------------------------------------------------------------

def test_execute_current_phase_advances_through_all_phases(db_url):
    orch = start_new_run(db_url=db_url)

    # Intake → Inventory → Sourcing → Comparison (comparison runs agents, stays in COMPARISON)
    orch.execute_current_phase()  # INTAKE → INVENTORY
    orch.execute_current_phase()  # INVENTORY → SOURCING
    orch.execute_current_phase()  # SOURCING → COMPARISON
    orch.execute_current_phase()  # COMPARISON: runs agents, stays in COMPARISON

    state = orch.get_state()
    assert state["current_phase"] == Phase.COMPARISON.value

    # Phase 3: user selects a candidate → PENDING_FIRST_APPROVAL
    orch.select_candidate({"vendor_name": "Test Vendor", "grand_total_usd": 500.0})
    state = orch.get_state()
    assert state["current_phase"] == Phase.PENDING_FIRST_APPROVAL.value

    # Single-approver path ($500 < $5000 with default rules)
    orch.submit_approval("approved", approver_role="maintenance_director")
    state = orch.get_state()
    assert state["current_phase"] == Phase.APPROVED.value

    # Remaining phases use execute_current_phase (stubs)
    orch.execute_current_phase()  # APPROVED → EXECUTING
    orch.execute_current_phase()  # EXECUTING → FULFILLING
    orch.execute_current_phase()  # FULFILLING → COMPLETED

    final = orch.get_state()
    assert final["current_phase"] == Phase.COMPLETED.value


def test_execute_phase_populates_inventory_result(db_url):
    orch = start_new_run(db_url=db_url)
    orch.execute_current_phase()  # INTAKE → INVENTORY
    orch.execute_current_phase()  # INVENTORY → SOURCING
    state = orch.get_state()
    assert state["inventory_result_json"] is not None
    assert state["inventory_result_json"]["status"] == "no_data"


def test_execute_phase_populates_sourcing_results(db_url):
    orch = start_new_run(db_url=db_url)
    orch.execute_current_phase()  # INTAKE → INVENTORY
    orch.execute_current_phase()  # INVENTORY → SOURCING
    orch.execute_current_phase()  # SOURCING → COMPARISON
    state = orch.get_state()
    assert state["sourcing_results_json"] is not None
    sr = state["sourcing_results_json"]
    # Phase 2: SourcingAgent output uses tier_1/tier_2/tier_3 keys
    assert "tier_1" in sr
    assert "tier_2" in sr
    assert "tier_3" in sr
    assert "urgency_applied" in sr


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def test_transitions_write_audit_log_entries(db_url):
    orch = start_new_run(db_url=db_url)
    # Run through three phases to generate entries.
    orch.execute_current_phase()  # INTAKE → INVENTORY
    orch.execute_current_phase()  # INVENTORY → SOURCING

    entries = recent_entries(limit=50)
    run_entries = [e for e in entries if e.get("sourcing_run_id") == orch.run_id]
    # One entry per transition (create_run doesn't write one; each execute does).
    assert len(run_entries) >= 2
    summaries = [e["input_summary"] for e in run_entries]
    assert any("intake" in s.lower() for s in summaries)
    assert any("inventory" in s.lower() for s in summaries)


def test_audit_log_entries_contain_workflow_mode(db_url):
    orch = start_new_run(db_url=db_url)
    orch.execute_current_phase()
    entries = recent_entries(limit=10)
    run_entries = [e for e in entries if e.get("sourcing_run_id") == orch.run_id]
    assert all(e["workflow_mode"] == "procurement_run_v2" for e in run_entries)
