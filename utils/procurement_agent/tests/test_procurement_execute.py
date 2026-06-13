"""
Tests for the ProcurementAgent "execute"/"mark_delivered" wiring into utils/orders.py
(replacing the old {"stub": True} return). Wiring only — orders.py logic is unchanged.

Invariants: "execute" produces a DURABLE order (not a stub); placement is deliberate
and records placed_by; can't-place-without-a-price holds through the wiring (no price
-> stays draft); "mark_delivered" goes through the state machine (received only from
shipped). orders + price_db isolated to tmp; no external actions.
"""

import pytest

from utils import orders, price_db
from utils.models import SourcingRun
from utils.procurement_agent.agents.procurement_agent import ProcurementAgent

_SPECS = {"manufacturer": "Baldor", "part_number": "EM3770T", "model": "EM3770T"}
_APPROVED = [{"sequence": 1, "action": "approved", "approver_name": "Maintenance Director"}]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(orders, "_DB_PATH", str(tmp_path / "orders.sqlite"))
    monkeypatch.setattr(price_db, "_DB_PATH", str(tmp_path / "price_db.json"))
    return orders


def _run(candidate, **over):
    kw = dict(
        asset_specs_json=_SPECS,
        selected_candidate_json=candidate,
        approval_history_json=_APPROVED,
        current_phase="approved",          # execute is now gated on an approved run (H1)
    )
    kw.update(over)
    return SourcingRun(**kw)


class TestExecute:
    def test_buy_path_creates_durable_placed_order(self, isolated):
        run = _run({"vendor_name": "Global Industrial", "base_price": 1799.0,
                    "source_url": "https://www.globalindustrial.com/p/x", "lead_time_days": 3})
        res = ProcurementAgent().run(run, "execute")

        assert res["success"] and res["placed"] is True
        assert "stub" not in res                      # the stub is gone
        order = res["order"]
        assert order["status"] == "placed"
        assert order["unit_price"] == 1799.0 and order["source"] == "buy"
        assert order["manufacturer"] == "Baldor" and order["part_number"] == "EM3770T"
        assert order["placed_by"] == "Maintenance Director"   # from the approval
        # Durable: a real row exists.
        rows = isolated.get_orders(run_id=run.id)
        assert len(rows) == 1 and rows[0]["status"] == "placed"

    def test_rfq_path_pulls_confirmed_price(self, isolated):
        # Layer-3-confirmed rfq price in price_db; candidate carries no price (price_tbd).
        price_db.save_price("Baldor", "EM3770T", "Bay Power", 1650.0, source="rfq")
        run = _run({"vendor_name": "Bay Power", "price_tbd": True, "requires_rfq": True,
                    "supplier_domain": "baypower.com"})
        res = ProcurementAgent().run(run, "execute")
        assert res["placed"] is True
        assert res["order"]["unit_price"] == 1650.0 and res["order"]["source"] == "rfq"

    def test_no_price_stays_draft_not_placed(self, isolated):
        run = _run({"vendor_name": "Nobody", "price_tbd": True})  # no price, no price_db
        res = ProcurementAgent().run(run, "execute")
        assert res["success"] and res["placed"] is False        # can't place without a price
        assert res["order"]["status"] == "draft"
        assert res["order"]["unit_price"] is None
        assert isolated.get_orders(run_id=run.id)[0]["status"] == "draft"

    def test_thin_selection_resolves_from_sourcing_results(self, isolated):
        # api_server stores {candidate_id, tier}; resolve against sourcing_results_json.
        run = _run(
            {"candidate_id": "Standard Electric-t3-0", "tier": 3},
            sourcing_results_json={"tier_3": {"results": [
                {"vendor_name": "Standard Electric", "base_price": 1450.0,
                 "source_url": "https://standardelectricsupply.com"},
            ]}},
        )
        res = ProcurementAgent().run(run, "execute")
        assert res["placed"] is True
        assert res["order"]["vendor_name"] == "Standard Electric"
        assert res["order"]["unit_price"] == 1450.0

    def test_no_selection_returns_error_no_order(self, isolated):
        run = _run({})  # nothing selected
        res = ProcurementAgent().run(run, "execute")
        assert res["success"] is False and res["order"] is None
        assert isolated.get_orders() == []

    def test_execute_blocked_when_not_approved(self, isolated):
        # H1 gate: select-candidate advances to pending_first_approval; calling execute
        # straight away (skipping approval) must NOT capture or place any order.
        run = _run({"vendor_name": "Global Industrial", "base_price": 1799.0,
                    "source_url": "https://www.globalindustrial.com/p/x"},
                   current_phase="pending_first_approval")
        res = ProcurementAgent().run(run, "execute")
        assert res["success"] is False and res["placed"] is False
        assert res["order"] is None
        assert isolated.get_orders() == []          # nothing captured, nothing placed


class TestMarkDelivered:
    def _shipped_order(self, isolated, run):
        o = isolated.create_order({"run_id": run.id, "manufacturer": "Baldor",
                                   "part_number": "EM3770T", "vendor_name": "V",
                                   "unit_price": 10.0})
        isolated.place_order(o["id"], placed_by="A")
        isolated.update_order_status(o["id"], "confirmed")
        isolated.update_order_status(o["id"], "shipped")
        return o["id"]

    def test_mark_delivered_from_shipped_receives(self, isolated):
        run = _run({"vendor_name": "V"})
        oid = self._shipped_order(isolated, run)
        run.vendor_order_id = oid
        res = ProcurementAgent().run(run, "mark_delivered")
        assert res["success"] and res["order"]["status"] == "received"

    def test_mark_delivered_rejected_when_not_shipped(self, isolated):
        run = _run({"vendor_name": "V"})
        o = isolated.create_order({"run_id": run.id, "manufacturer": "M", "part_number": "P",
                                   "vendor_name": "V", "unit_price": 10.0})
        isolated.place_order(o["id"], placed_by="A")   # only 'placed', not shipped
        run.vendor_order_id = o["id"]
        res = ProcurementAgent().run(run, "mark_delivered")
        assert res["success"] is False                 # machine rejects placed->received
        assert isolated.get_order(o["id"])["status"] == "placed"

    def test_mark_delivered_without_order_errs(self, isolated):
        res = ProcurementAgent().run(_run({"vendor_name": "V"}), "mark_delivered")
        assert res["success"] is False and res["order"] is None


class TestUnknownActionStillNoOp:
    def test_other_action_acknowledged(self, isolated):
        res = ProcurementAgent().run(_run({"vendor_name": "V"}), "approve")
        assert res["success"] and res.get("stub") is True   # approval flow lives elsewhere
