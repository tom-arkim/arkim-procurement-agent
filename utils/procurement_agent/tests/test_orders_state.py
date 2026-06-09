"""
Tests for utils/orders.py — schema + the status state machine (Commit 1).

The machine ENFORCES legal transitions: forward lifecycle + cancel off-ramp from any
pre-received state; received/cancelled terminal; no skip-ahead, backward, un-cancel.
"""

import pytest

from utils import orders
from utils.orders import (
    can_transition, ALLOWED_TRANSITIONS,
    STATUS_DRAFT, STATUS_PLACED, STATUS_CONFIRMED, STATUS_SHIPPED,
    STATUS_RECEIVED, STATUS_CANCELLED,
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(orders, "_DB_PATH", str(tmp_path / "orders.sqlite"))
    return orders


class TestSchema:
    def test_orders_table_created(self, isolated_db):
        conn = isolated_db._get_conn()
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
        finally:
            conn.close()
        assert "orders" in names
        for c in ("id", "run_id", "manufacturer", "part_number", "vendor_name",
                  "supplier_domain", "unit_price", "currency", "quantity", "lead_time",
                  "source", "status", "created_at", "updated_at", "placed_by", "notes"):
            assert c in cols


class TestStateMachine:
    def test_legal_forward_lifecycle(self):
        assert can_transition(STATUS_DRAFT, STATUS_PLACED)
        assert can_transition(STATUS_PLACED, STATUS_CONFIRMED)
        assert can_transition(STATUS_CONFIRMED, STATUS_SHIPPED)
        assert can_transition(STATUS_SHIPPED, STATUS_RECEIVED)

    def test_cancel_from_any_pre_received_state(self):
        for s in (STATUS_DRAFT, STATUS_PLACED, STATUS_CONFIRMED, STATUS_SHIPPED):
            assert can_transition(s, STATUS_CANCELLED), f"{s} should be cancellable"

    def test_received_and_cancelled_are_terminal(self):
        assert ALLOWED_TRANSITIONS[STATUS_RECEIVED] == set()
        assert ALLOWED_TRANSITIONS[STATUS_CANCELLED] == set()
        assert not can_transition(STATUS_RECEIVED, STATUS_DRAFT)
        assert not can_transition(STATUS_RECEIVED, STATUS_CANCELLED)  # can't cancel received
        assert not can_transition(STATUS_CANCELLED, STATUS_SHIPPED)   # can't un-cancel

    def test_no_skip_ahead(self):
        assert not can_transition(STATUS_DRAFT, STATUS_CONFIRMED)
        assert not can_transition(STATUS_PLACED, STATUS_SHIPPED)
        assert not can_transition(STATUS_PLACED, STATUS_RECEIVED)

    def test_no_backward(self):
        assert not can_transition(STATUS_SHIPPED, STATUS_CONFIRMED)
        assert not can_transition(STATUS_CONFIRMED, STATUS_PLACED)
        assert not can_transition(STATUS_RECEIVED, STATUS_SHIPPED)

    def test_unknown_state_has_no_transitions(self):
        assert can_transition("bogus", STATUS_PLACED) is False
