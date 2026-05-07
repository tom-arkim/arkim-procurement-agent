"""
Tests for SourcingRun CRUD operations.
"""

import pytest
from utils.procurement_agent.state.persistence import (
    create_run, get_run, update_run, list_runs, append_approval,
)
from utils.procurement_agent.state.phases import Phase


def test_create_run_returns_dict_with_id(db_url):
    run = create_run(db_url=db_url)
    assert isinstance(run["id"], str)
    assert len(run["id"]) == 36   # UUID format


def test_create_run_defaults(db_url):
    run = create_run(db_url=db_url)
    assert run["current_phase"] == Phase.INTAKE.value
    assert run["urgency_factor"] == 0.3
    assert run["warranty_status"] == "unknown"
    assert run["approval_history_json"] == []
    assert run["asset_specs_json"] is None


def test_create_run_with_asset_specs(db_url):
    specs = {"manufacturer": "ABB", "model": "M2AA-112M", "part_number": "3GBP112030-ADG"}
    run = create_run(asset_specs=specs, db_url=db_url)
    assert run["asset_specs_json"]["manufacturer"] == "ABB"


def test_get_run_returns_correct_row(db_url):
    created = create_run(facility_id="fac-001", db_url=db_url)
    fetched = get_run(created["id"], db_url=db_url)
    assert fetched is not None
    assert fetched["id"] == created["id"]
    assert fetched["facility_id"] == "fac-001"


def test_get_run_missing_returns_none(db_url):
    result = get_run("00000000-0000-0000-0000-000000000000", db_url=db_url)
    assert result is None


def test_update_run_changes_phase(db_url):
    run = create_run(db_url=db_url)
    updated = update_run(run["id"], {"current_phase": Phase.INVENTORY.value}, db_url=db_url)
    assert updated["current_phase"] == Phase.INVENTORY.value


def test_update_run_json_field(db_url):
    run = create_run(db_url=db_url)
    inv_result = {"found": False, "status": "no_data"}
    updated = update_run(run["id"], {"inventory_result_json": inv_result}, db_url=db_url)
    assert updated["inventory_result_json"]["found"] is False


def test_update_run_missing_id_returns_none(db_url):
    result = update_run("bad-id", {"current_phase": "sourcing"}, db_url=db_url)
    assert result is None


def test_list_runs_returns_recent_first(db_url):
    for i in range(3):
        create_run(facility_id="fac-list", urgency_factor=float(i) * 0.3, db_url=db_url)
    runs = list_runs(facility_id="fac-list", db_url=db_url)
    assert len(runs) == 3
    # newest first
    assert runs[0]["created_at"] >= runs[1]["created_at"]


def test_list_runs_respects_limit(db_url):
    for _ in range(5):
        create_run(db_url=db_url)
    runs = list_runs(limit=2, db_url=db_url)
    assert len(runs) == 2


def test_list_runs_filters_by_facility(db_url):
    create_run(facility_id="fac-A", db_url=db_url)
    create_run(facility_id="fac-B", db_url=db_url)
    runs_a = list_runs(facility_id="fac-A", db_url=db_url)
    assert all(r["facility_id"] == "fac-A" for r in runs_a)
    assert len(runs_a) == 1


def test_append_approval_adds_history(db_url):
    run = create_run(db_url=db_url)
    ok = append_approval(
        run["id"],
        approver_id="user-1",
        approver_role="maintenance_director",
        action="approved",
        notes="Looks good.",
        db_url=db_url,
    )
    assert ok is True
    fetched = get_run(run["id"], db_url=db_url)
    history = fetched["approval_history_json"]
    assert len(history) == 1
    assert history[0]["action"] == "approved"
    assert history[0]["approver_role"] == "maintenance_director"


def test_append_approval_missing_run_returns_false(db_url):
    ok = append_approval("bad-id", approver_id=None, approver_role=None, action="approved", db_url=db_url)
    assert ok is False
