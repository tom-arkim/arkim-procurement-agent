"""
Tests for the order capture/lifecycle flow (utils/orders.py, Commit 2).

Invariants asserted: no auto-place (create -> draft; only place_order -> placed);
illegal transitions rejected by the machine; an order can't be PLACED without a price;
orders feed from both the buy-path and the Layer-3-confirmed rfq price; get_orders
filters. Orders DB + price_db isolated to tmp.
"""

import pytest

from utils import orders, price_db
from utils.orders import (
    create_order, place_order, update_order_status, cancel_order, get_orders, get_order,
    STATUS_DRAFT, STATUS_PLACED, STATUS_CONFIRMED, STATUS_SHIPPED, STATUS_RECEIVED,
    STATUS_CANCELLED,
)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(orders, "_DB_PATH", str(tmp_path / "orders.sqlite"))
    monkeypatch.setattr(price_db, "_DB_PATH", str(tmp_path / "price_db.json"))
    return orders


def _buy_selection(**over):
    sel = {
        "run_id": "run1", "manufacturer": "Baldor", "part_number": "EM3770T",
        "vendor_name": "Global Industrial", "source_url": "https://www.globalindustrial.com/p/x",
        "unit_price": 1799.0, "lead_time": "in stock", "source": "buy",
    }
    sel.update(over)
    return sel


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

class TestCreateOrder:
    def test_buy_path_creates_draft(self, isolated):
        o = create_order(_buy_selection(), quantity=2, placed_by="Maintenance Director")
        assert o["status"] == STATUS_DRAFT          # NOT placed on creation
        assert o["placed_by"] is None               # placement is a separate action
        assert o["unit_price"] == 1799.0 and o["currency"] == "USD"
        assert o["quantity"] == 2 and o["source"] == "buy"
        assert o["manufacturer"] == "Baldor" and o["part_number"] == "EM3770T"
        assert o["supplier_domain"] == "globalindustrial.com"   # derived from source_url

    def test_rfq_path_pulls_confirmed_price_from_price_db(self, isolated):
        # Layer-3 confirm_quote writes an rfq price; capture pulls it when the
        # selection carries no explicit price.
        price_db.save_price("Baldor", "EM3770T", "Bay Power", 1650.0, source="rfq")
        sel = {"run_id": "run1", "manufacturer": "Baldor", "part_number": "EM3770T",
               "vendor_name": "Bay Power", "supplier_domain": "baypower.com"}  # no price
        o = create_order(sel, quantity=1)
        assert o["unit_price"] == 1650.0
        assert o["source"] == "rfq"
        assert o["status"] == STATUS_DRAFT

    def test_no_price_creates_draft_with_null_price(self, isolated):
        sel = {"run_id": "run1", "manufacturer": "X", "part_number": "Y",
               "vendor_name": "Nobody"}  # no price anywhere
        o = create_order(sel)
        assert o["unit_price"] is None and o["status"] == STATUS_DRAFT


class TestCompanyIdKey:
    """D2 prereq #1 — orders carry a nullable tenant key (company PIN). Keys only;
    no enforcement. The caller passes the run's company_id; NULL in the demo."""

    def test_company_id_column_exists_post_migrate(self, isolated):
        conn = isolated._get_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
        conn.close()
        assert "company_id" in cols

    def test_create_order_stores_company_id_param(self, isolated):
        o = create_order(_buy_selection(), company_id="PIN-XYZ")
        assert o["company_id"] == "PIN-XYZ"
        assert get_order(o["id"])["company_id"] == "PIN-XYZ"   # round-trips on read

    def test_create_order_company_id_from_selection_fallback(self, isolated):
        o = create_order(_buy_selection(company_id="PIN-SEL"))   # no param, in selection
        assert o["company_id"] == "PIN-SEL"

    def test_create_order_company_id_null_when_absent(self, isolated):
        o = create_order(_buy_selection())   # neither param nor selection -> NULL (demo)
        assert o["company_id"] is None

    def test_migrate_idempotent(self, isolated):
        conn = isolated._get_conn()
        isolated._migrate(conn)   # second + third runs must not raise (column-exists guard)
        isolated._migrate(conn)
        conn.close()


class TestPendingManualFulfilment:
    """Increment 1 groundwork: a marketplace order is created POST-approval in
    pending_manual_fulfilment; an operator later moves it pending -> placed (inc 2)."""

    def test_create_order_defaults_draft_backcompat(self, isolated):
        # Every existing caller (no initial_status) still gets a draft.
        assert create_order(_buy_selection())["status"] == STATUS_DRAFT

    def test_create_order_honors_initial_status(self, isolated):
        from utils.orders import STATUS_PENDING_FULFILMENT
        o = create_order(_buy_selection(), initial_status=STATUS_PENDING_FULFILMENT)
        assert o["status"] == STATUS_PENDING_FULFILMENT

    def test_transitions_table_has_pending(self, isolated):
        from utils import orders as o
        assert o.ALLOWED_TRANSITIONS[o.STATUS_PENDING_FULFILMENT] == {o.STATUS_PLACED, o.STATUS_CANCELLED}

    def test_place_order_accepts_pending_manual_fulfilment(self, isolated):
        from utils.orders import STATUS_PENDING_FULFILMENT
        o = create_order(_buy_selection(), initial_status=STATUS_PENDING_FULFILMENT)
        placed = place_order(o["id"], placed_by="Operator")   # inc-2 "mark purchased"
        assert placed is not None and placed["status"] == STATUS_PLACED

    def test_place_order_from_pending_still_price_gated(self, isolated):
        from utils.orders import STATUS_PENDING_FULFILMENT
        sel = {"run_id": "r", "manufacturer": "X", "part_number": "Y", "vendor_name": "V"}  # no price
        o = create_order(sel, initial_status=STATUS_PENDING_FULFILMENT)
        assert place_order(o["id"], placed_by="Op") is None   # no price -> cannot place

    def test_pending_can_be_cancelled(self, isolated):
        from utils.orders import STATUS_PENDING_FULFILMENT
        o = create_order(_buy_selection(), initial_status=STATUS_PENDING_FULFILMENT)
        assert cancel_order(o["id"], reason="changed mind")["status"] == STATUS_CANCELLED

    def test_pending_cannot_skip_to_shipped(self, isolated):
        from utils.orders import STATUS_PENDING_FULFILMENT, STATUS_SHIPPED
        o = create_order(_buy_selection(), initial_status=STATUS_PENDING_FULFILMENT)
        assert update_order_status(o["id"], STATUS_SHIPPED) is None   # illegal transition rejected


# ---------------------------------------------------------------------------
# Placement — deliberate, price-gated, once
# ---------------------------------------------------------------------------

class TestPlaceOrder:
    def test_place_moves_draft_to_placed_and_records_who(self, isolated):
        o = create_order(_buy_selection())
        placed = place_order(o["id"], placed_by="Ops Manager")
        assert placed["status"] == STATUS_PLACED
        assert placed["placed_by"] == "Ops Manager"

    def test_cannot_place_twice(self, isolated):
        o = create_order(_buy_selection())
        assert place_order(o["id"], placed_by="A")["status"] == STATUS_PLACED
        assert place_order(o["id"], placed_by="B") is None       # already placed
        assert get_order(o["id"])["placed_by"] == "A"            # unchanged

    def test_cannot_place_without_price(self, isolated):
        o = create_order({"run_id": "r", "manufacturer": "X", "part_number": "Y",
                          "vendor_name": "Nobody"})
        assert o["unit_price"] is None
        assert place_order(o["id"], placed_by="A") is None       # price required
        assert get_order(o["id"])["status"] == STATUS_DRAFT      # stays draft


# ---------------------------------------------------------------------------
# Lifecycle transitions — enforced
# ---------------------------------------------------------------------------

class TestTransitions:
    def _placed(self, isolated):
        o = create_order(_buy_selection())
        place_order(o["id"], placed_by="A")
        return o["id"]

    def test_legal_forward_path(self, isolated):
        oid = self._placed(isolated)
        assert update_order_status(oid, STATUS_CONFIRMED)["status"] == STATUS_CONFIRMED
        assert update_order_status(oid, STATUS_SHIPPED)["status"] == STATUS_SHIPPED
        assert update_order_status(oid, STATUS_RECEIVED)["status"] == STATUS_RECEIVED

    def test_skip_ahead_rejected(self, isolated):
        oid = self._placed(isolated)
        assert update_order_status(oid, STATUS_SHIPPED) is None   # placed->shipped skips
        assert get_order(oid)["status"] == STATUS_PLACED

    def test_backward_rejected(self, isolated):
        oid = self._placed(isolated)
        update_order_status(oid, STATUS_CONFIRMED)
        update_order_status(oid, STATUS_SHIPPED)
        update_order_status(oid, STATUS_RECEIVED)
        assert update_order_status(oid, STATUS_DRAFT) is None     # received->draft
        assert update_order_status(oid, STATUS_SHIPPED) is None   # received->shipped
        assert get_order(oid)["status"] == STATUS_RECEIVED

    def test_update_refuses_placed_and_cancelled_targets(self, isolated):
        o = create_order(_buy_selection())
        assert update_order_status(o["id"], STATUS_PLACED) is None     # use place_order
        assert update_order_status(o["id"], STATUS_CANCELLED) is None  # use cancel_order
        assert get_order(o["id"])["status"] == STATUS_DRAFT


# ---------------------------------------------------------------------------
# Cancel off-ramp
# ---------------------------------------------------------------------------

class TestCancel:
    def test_cancel_from_pre_received_records_reason(self, isolated):
        o = create_order(_buy_selection())
        place_order(o["id"], placed_by="A")
        c = cancel_order(o["id"], reason="supplier backordered")
        assert c["status"] == STATUS_CANCELLED
        assert "supplier backordered" in (c["notes"] or "")

    def test_cancel_from_draft_ok(self, isolated):
        o = create_order(_buy_selection())
        assert cancel_order(o["id"])["status"] == STATUS_CANCELLED

    def test_cannot_cancel_received(self, isolated):
        o = create_order(_buy_selection())
        place_order(o["id"], placed_by="A")
        update_order_status(o["id"], STATUS_CONFIRMED)
        update_order_status(o["id"], STATUS_SHIPPED)
        update_order_status(o["id"], STATUS_RECEIVED)
        assert cancel_order(o["id"], reason="too late") is None
        assert get_order(o["id"])["status"] == STATUS_RECEIVED


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

class TestGetOrders:
    def test_filters_by_run_status_vendor(self, isolated):
        a = create_order(_buy_selection(run_id="runA", vendor_name="Global Industrial"))
        b = create_order(_buy_selection(run_id="runB", vendor_name="Bay Power"))
        place_order(b["id"], placed_by="A")

        assert {o["id"] for o in get_orders(run_id="runA")} == {a["id"]}
        assert {o["id"] for o in get_orders(status=STATUS_PLACED)} == {b["id"]}
        assert {o["id"] for o in get_orders(vendor="Bay Power")} == {b["id"]}
        assert len(get_orders()) == 2
