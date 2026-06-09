"""
utils/orders.py
Order capture & lifecycle tracking — the durable post-selection back-end.

When a user commits to buying a selected part from a selected supplier, an order is
CAPTURED here and tracked through a status lifecycle. This is capture-and-track ONLY:
no payment processing, no PO transmission, no ERP/fulfillment execution (those are
later layers). Order placement is a deliberate user action, never automatic.

An order originates from either:
  - the BUY-PATH  (Tier 1/2 known in-stock price), or
  - the RFQ-PATH  (Tier 3 RFQ -> quote -> Layer-3 confirm_quote -> price_db rfq price).
Both resolve a price through price_db, so order capture sits downstream of both.

Status lifecycle (state machine — see ALLOWED_TRANSITIONS):
  draft -> placed -> confirmed -> shipped -> received
  cancelled is the off-ramp from any PRE-received state; received/cancelled are terminal.

Raw-sqlite3 module (mirrors supplier_registry): own data/orders.sqlite, idempotent
CREATE TABLE, fail-soft, bracket-prefixed logging.

Flow:
  create_order(selection, quantity, placed_by)  -> CAPTURE (status="draft")
  place_order(order_id, placed_by)               -> deliberate commit (draft->placed)
  update_order_status(order_id, new_status)      -> forward lifecycle (enforced)
  cancel_order(order_id, reason)                 -> off-ramp (pre-received only)
  get_orders(run_id|status|vendor)               -> retrieval
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DB_PATH = os.path.join(_DATA_DIR, "orders.sqlite")

# Order status vocabulary.
STATUS_DRAFT = "draft"
STATUS_PLACED = "placed"
STATUS_CONFIRMED = "confirmed"
STATUS_SHIPPED = "shipped"
STATUS_RECEIVED = "received"
STATUS_CANCELLED = "cancelled"

# The allowed forward lifecycle + cancel off-ramp. A transition is legal ONLY if the
# target is in ALLOWED_TRANSITIONS[current]. received/cancelled are terminal (empty
# sets). This ENFORCES the machine — illegal transitions (skip-ahead, backward,
# un-cancel, re-place) are rejected, not merely recorded.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    STATUS_DRAFT:     {STATUS_PLACED, STATUS_CANCELLED},
    STATUS_PLACED:    {STATUS_CONFIRMED, STATUS_CANCELLED},
    STATUS_CONFIRMED: {STATUS_SHIPPED, STATUS_CANCELLED},
    STATUS_SHIPPED:   {STATUS_RECEIVED, STATUS_CANCELLED},
    STATUS_RECEIVED:  set(),   # terminal
    STATUS_CANCELLED: set(),   # terminal
}

_DDL = """
CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,
    run_id          TEXT,
    manufacturer    TEXT,
    part_number     TEXT,
    vendor_name     TEXT,
    supplier_domain TEXT,
    unit_price      REAL,
    currency        TEXT,
    quantity        INTEGER,
    lead_time       TEXT,
    source          TEXT,           -- "buy" | "rfq"
    status          TEXT NOT NULL,  -- draft|placed|confirmed|shipped|received|cancelled
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    placed_by       TEXT,
    notes           TEXT
);
"""


def _get_conn() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(_DDL)
    conn.commit()
    return conn


def can_transition(current: str, new: str) -> bool:
    """True iff `current -> new` is a legal status transition (pure; no I/O)."""
    return new in ALLOWED_TRANSITIONS.get(current, set())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_order(row: sqlite3.Row) -> dict:
    return dict(row)


def get_order(order_id: str) -> Optional[dict]:
    """Return one order by id, or None."""
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return _row_to_order(r) if r else None
    except Exception:
        return None


def _resolve_price(selection: dict, manufacturer: Optional[str],
                   part_number: Optional[str], vendor: Optional[str]) -> tuple:
    """Resolve (unit_price, currency, source) for a selection.

    Price precedence: an explicit price on the selection (buy-path candidate), else a
    price_db entry for (manufacturer, part_number) + vendor — which holds BOTH buy
    ('live') and Layer-3-confirmed RFQ ('rfq') prices. Returns (price|None, currency,
    source). Never raises.
    """
    price = selection.get("unit_price")
    if price is None:
        price = selection.get("price")  # sourcing candidates carry 'price'/'base_price'
    if price is None:
        price = selection.get("base_price")
    currency = selection.get("currency") or "USD"
    source = selection.get("source")

    if price is None and manufacturer and part_number and vendor:
        try:
            from utils import price_db
            entry = (price_db.get_cached_prices(manufacturer, part_number) or {}).get(vendor)
        except Exception:
            entry = None
        if entry:
            price = entry.get("price")
            # Map price_db source -> order source: 'rfq' stays rfq; anything else is a buy.
            if source is None:
                source = "rfq" if entry.get("source") == "rfq" else "buy"

    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    return price, currency, (source or "buy")


def create_order(selection: dict, quantity: int = 1,
                 placed_by: Optional[str] = None) -> Optional[dict]:
    """CAPTURE an order from a selected candidate. status='draft' — NOT placed.

    Pulls price from the selection, else price_db (buy 'live' or rfq 'rfq'). A draft
    may be created even if no price resolves; place_order then refuses to place it
    (an order can't be PLACED without a price). placed_by is recorded but placement
    is a separate deliberate action. Returns the order dict, or None on write failure.
    """
    from utils import supplier_registry

    manufacturer = selection.get("manufacturer")
    part_number = selection.get("part_number")
    vendor = selection.get("vendor_name")
    domain = selection.get("supplier_domain")
    if not domain and selection.get("source_url"):
        domain = supplier_registry._normalize_domain(selection["source_url"])
    lead_time = selection.get("lead_time")
    if lead_time is None and selection.get("lead_time_days") is not None:
        lead_time = str(selection["lead_time_days"])
    unit_price, currency, source = _resolve_price(selection, manufacturer, part_number, vendor)

    now = _now()
    order = {
        "id": str(uuid.uuid4()),
        "run_id": selection.get("run_id"),
        "manufacturer": manufacturer,
        "part_number": part_number,
        "vendor_name": vendor,
        "supplier_domain": domain,
        "unit_price": unit_price,
        "currency": currency,
        "quantity": int(quantity) if quantity is not None else 1,
        "lead_time": lead_time,
        "source": source,
        "status": STATUS_DRAFT,
        "created_at": now,
        "updated_at": now,
        "placed_by": None,           # set on place_order, not on capture
        "notes": selection.get("notes"),
    }
    try:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO orders
               (id, run_id, manufacturer, part_number, vendor_name, supplier_domain,
                unit_price, currency, quantity, lead_time, source, status,
                created_at, updated_at, placed_by, notes)
               VALUES (:id,:run_id,:manufacturer,:part_number,:vendor_name,:supplier_domain,
                       :unit_price,:currency,:quantity,:lead_time,:source,:status,
                       :created_at,:updated_at,:placed_by,:notes)""",
            order,
        )
        conn.commit()
        print(f"[Orders] Captured draft {order['id']} — {vendor} {manufacturer} "
              f"{part_number} x{order['quantity']} @ {unit_price} ({source})")
        return order
    except Exception as exc:
        print(f"[Orders] create_order failed: {exc}")
        return None


def _set_status(order_id: str, new_status: str, *, placed_by: Optional[str] = None,
                note: Optional[str] = None) -> Optional[dict]:
    """Persist a status change (+ optional placed_by / appended note). Returns the
    updated order, or None on failure."""
    updates = {"status": new_status, "updated_at": _now(), "_id": order_id}
    sets = ["status = :status", "updated_at = :updated_at"]
    if placed_by is not None:
        updates["placed_by"] = placed_by
        sets.append("placed_by = :placed_by")
    if note is not None:
        updates["notes"] = note
        sets.append("notes = :notes")
    try:
        conn = _get_conn()
        conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id = :_id", updates)
        conn.commit()
    except Exception as exc:
        print(f"[Orders] _set_status failed: {exc}")
        return None
    return get_order(order_id)


def place_order(order_id: str, placed_by: Optional[str] = None) -> Optional[dict]:
    """The deliberate HITL commitment: draft -> placed. The ONLY path to 'placed'.

    Refuses if the order isn't a draft (no re-placing) or has no price (an order
    can't be placed without a price). Records placed_by + timestamp. Returns the
    placed order, or None on rejection.
    """
    order = get_order(order_id)
    if not order:
        print(f"[Orders] place_order: {order_id!r} not found")
        return None
    if order["status"] != STATUS_DRAFT:
        print(f"[Orders] place_order rejected: {order_id} is '{order['status']}', not draft")
        return None
    if order.get("unit_price") is None:
        print(f"[Orders] place_order rejected: {order_id} has no price (cannot place)")
        return None
    return _set_status(order_id, STATUS_PLACED, placed_by=placed_by)


def update_order_status(order_id: str, new_status: str) -> Optional[dict]:
    """Drive the forward lifecycle (placed->confirmed->shipped->received), ENFORCING
    the state machine. Rejects illegal transitions. Placement and cancellation have
    dedicated functions (place_order / cancel_order) — this refuses those targets so
    the price/placed_by gate and cancel-reason aren't bypassed.
    """
    if new_status == STATUS_PLACED:
        print("[Orders] update_order_status: use place_order to place an order")
        return None
    if new_status == STATUS_CANCELLED:
        print("[Orders] update_order_status: use cancel_order to cancel an order")
        return None
    order = get_order(order_id)
    if not order:
        print(f"[Orders] update_order_status: {order_id!r} not found")
        return None
    if not can_transition(order["status"], new_status):
        print(f"[Orders] illegal transition rejected: {order['status']} -> {new_status}")
        return None
    return _set_status(order_id, new_status)


def cancel_order(order_id: str, reason: Optional[str] = None) -> Optional[dict]:
    """Off-ramp: cancel from any pre-received state. Rejected once received/cancelled
    (terminal). Records the reason in notes. Returns the cancelled order, or None."""
    order = get_order(order_id)
    if not order:
        print(f"[Orders] cancel_order: {order_id!r} not found")
        return None
    if not can_transition(order["status"], STATUS_CANCELLED):
        print(f"[Orders] cancel rejected: {order['status']} cannot be cancelled")
        return None
    note = f"cancelled: {reason}" if reason else "cancelled"
    if order.get("notes"):
        note = f"{order['notes']} | {note}"
    return _set_status(order_id, STATUS_CANCELLED, note=note)


def get_orders(run_id: Optional[str] = None, status: Optional[str] = None,
               vendor: Optional[str] = None) -> list[dict]:
    """Return orders (newest first), optionally filtered by run_id / status / vendor.
    Fail-soft: [] on error."""
    clauses: list[str] = []
    params: list = []
    for col, val in (("run_id", run_id), ("status", status), ("vendor_name", vendor)):
        if val is not None:
            clauses.append(f"{col} = ?")
            params.append(val)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM orders{where} ORDER BY created_at DESC", params
        ).fetchall()
        return [_row_to_order(r) for r in rows]
    except Exception as exc:
        print(f"[Orders] get_orders failed: {exc}")
        return []
