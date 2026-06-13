"""
Procurement Agent — approval workflow, transaction execution, fulfillment tracking.

Brief reference: Section 3.5, Section 8.5.

Phase 1: run() is a stub that acknowledges the action without executing anything.
Phase 4 implements:
  - Approval Rules Engine: config-driven routing by dollar threshold
  - Dual-approver state transitions (pending_first → pending_second → approved)
  - Simulated vendor order execution (EMAIL_SEND_ENABLED = False enforced)
  - Fulfillment status tracking
  - Inventory reconciliation via chat-driven location capture
  - Work order linkage (placeholder until Maintenance Assistant integration)
  - RFQ email draft generation for Tier 3 vendors

Note: This agent class is named ProcurementAgent to match the brief's terminology.
The module is named procurement_agent.py to match the file convention.
There is no naming collision with the parent package because Python resolves
`from utils.procurement_agent.agents.procurement_agent import ProcurementAgent`
unambiguously.
"""

from typing import Optional

from utils.models import SourcingRun


class ProcurementAgent:
    """Manages the approval workflow and executes the vendor transaction.

    Order capture is wired to utils/orders.py (built + tested separately — this only
    WIRES it). "execute" — the confirmed commit downstream of approval — captures a
    durable order from the approved selection and PLACES it (the deliberate step,
    recording placed_by); a price-less selection stays a draft (can't-place-without-
    price). "mark_delivered" advances the order to received via the order state
    machine. NO external actions (no payment, PO transmission, email, or Apollo) —
    capture-and-track only. The approval actions (approve/reject/request_changes)
    remain acknowledged no-ops here; the live approval flow runs through the
    orchestrator / api_server.
    """

    def run(self, run: SourcingRun, action: str) -> dict:
        """Process an action against the run.

          "execute"        -> create_order (draft) + place_order (placed) from the
                              approved selection; returns the durable order.
          "mark_delivered" -> update_order_status(order, received) via the machine.
          other actions    -> acknowledged no-op (the approval flow lives elsewhere).
        """
        if action == "execute":
            return self._execute(run)
        if action == "mark_delivered":
            return self._mark_delivered(run)
        return {
            "success":    True,
            "action":     action,
            "next_phase": None,
            "message":    f"Action '{action}' acknowledged (no-op in this agent).",
            "stub":       True,
        }

    # ------------------------------------------------------------------
    # "execute" — order capture (create + deliberate place)
    # ------------------------------------------------------------------

    def _execute(self, run: SourcingRun) -> dict:
        from utils import orders
        from utils.procurement_agent.state.phases import Phase

        # Pre-ship gate (H1): an order may only be PLACED from an APPROVED run.
        # Without this, /execute can be called straight after /select-candidate,
        # bypassing approval entirely. EXECUTING is allowed so a re-entrant execute
        # is idempotent. (Dual-approver routing + distinct-approver + tenant scoping
        # remain deferred pending the auth layer — see CLEANUP.md §4.1.)
        phase = getattr(run, "current_phase", None)
        if phase not in (Phase.APPROVED.value, Phase.EXECUTING.value):
            return {"success": False, "action": "execute", "order": None, "placed": False,
                    "message": f"Run is not approved (phase={phase}); cannot place order.",
                    "next_phase": None}

        selection = self._selection_for_order(run)
        if not selection or not selection.get("vendor_name"):
            return {"success": False, "action": "execute", "order": None, "placed": False,
                    "message": "No selected candidate to order.", "next_phase": None}

        placed_by = self._latest_approver(run)
        order = orders.create_order(selection, quantity=selection.get("quantity", 1),
                                    placed_by=placed_by)
        if not order:
            return {"success": False, "action": "execute", "order": None, "placed": False,
                    "message": "Order capture failed.", "next_phase": None}

        # Deliberate placement (the confirmed commit). place_order refuses without a
        # price, so a price-less selection stays a draft — can't-place-without-price.
        placed = orders.place_order(order["id"], placed_by=placed_by)
        if placed is None:
            return {"success": True, "action": "execute", "order": order, "placed": False,
                    "message": "Order captured as draft; not placed (no resolvable price).",
                    "next_phase": None}
        return {"success": True, "action": "execute", "order": placed, "placed": True,
                "message": f"Order {placed['id']} placed.", "next_phase": None}

    # ------------------------------------------------------------------
    # "mark_delivered" — received (state-machine enforced)
    # ------------------------------------------------------------------

    def _mark_delivered(self, run: SourcingRun) -> dict:
        from utils import orders

        order_id = getattr(run, "vendor_order_id", None)
        if not order_id:
            return {"success": False, "action": "mark_delivered", "order": None,
                    "message": "No order on this run to mark delivered.", "next_phase": None}
        updated = orders.update_order_status(order_id, orders.STATUS_RECEIVED)
        if updated is None:
            current = (orders.get_order(order_id) or {}).get("status")
            return {"success": False, "action": "mark_delivered",
                    "order": orders.get_order(order_id),
                    "message": f"Cannot mark delivered from '{current}' (order must be shipped first).",
                    "next_phase": None}
        return {"success": True, "action": "mark_delivered", "order": updated,
                "message": "Order marked received.", "next_phase": None}

    # ------------------------------------------------------------------
    # Helpers — assemble the order selection from the run. Handles BOTH selection
    # shapes: the orchestrator-enriched full candidate, and the thin
    # {candidate_id, tier} the api_server select endpoint stores.
    # ------------------------------------------------------------------

    @staticmethod
    def _latest_approver(run: SourcingRun) -> Optional[str]:
        history = getattr(run, "approval_history_json", None) or []
        for entry in reversed(history):
            if isinstance(entry, dict) and entry.get("action") == "approved":
                return entry.get("approver_name") or entry.get("approver_role")
        return None

    @staticmethod
    def _resolve_candidate(run: SourcingRun) -> Optional[dict]:
        sc = getattr(run, "selected_candidate_json", None) or {}
        if not isinstance(sc, dict):
            return None
        # Orchestrator path: the full candidate is stored directly.
        if sc.get("vendor_name") or sc.get("vendorName"):
            return sc
        # api_server thin path: {candidate_id, tier} -> resolve against sourcing results.
        cid, tier = sc.get("candidate_id"), sc.get("tier")
        if not cid or tier is None:
            return None
        results = getattr(run, "sourcing_results_json", None) or {}
        rows = (results.get(f"tier_{tier}") or {}).get("results") or []
        # candidate_id format: "{vendor_name}-t{tier}-{index}".
        try:
            idx = int(str(cid).rsplit("-", 1)[-1])
            if 0 <= idx < len(rows):
                return rows[idx]
        except (ValueError, TypeError):
            pass
        for i, row in enumerate(rows):
            vn = row.get("vendor_name") or row.get("vendorName") or ""
            if f"{vn}-t{tier}-{i}" == cid:
                return row
        return None

    @staticmethod
    def _selection_for_order(run: SourcingRun) -> Optional[dict]:
        candidate = ProcurementAgent._resolve_candidate(run)
        if not candidate:
            return None
        specs = getattr(run, "asset_specs_json", None) or {}
        vendor = candidate.get("vendor_name") or candidate.get("vendorName")
        # A real price only when the candidate actually has one; price_tbd -> leave it
        # to orders.create_order's price_db fallback (e.g. a Layer-3-confirmed rfq price).
        price = None
        if not candidate.get("price_tbd"):
            price = (candidate.get("base_price") or candidate.get("basePrice")
                     or candidate.get("price"))
        lt = candidate.get("lead_time_days")
        if lt is None:
            lt = candidate.get("leadTime")
        is_rfq = bool(candidate.get("requires_rfq") or candidate.get("price_tbd"))
        return {
            "run_id": getattr(run, "id", None),
            "manufacturer": specs.get("manufacturer"),
            "part_number": specs.get("part_number"),
            "vendor_name": vendor,
            "source_url": candidate.get("source_url") or candidate.get("sourceUrl"),
            "unit_price": price,
            "lead_time": str(lt) if lt is not None else None,
            "source": "rfq" if is_rfq else "buy",
            "quantity": 1,
        }
