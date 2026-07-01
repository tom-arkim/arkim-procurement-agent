"""
utils/reorder.py
Reorder intelligence — forecast when a part is due to be reordered, from the customer's
OWN order history only (no external/market data).

A forecast is made ONLY for parts with a real repeat cadence (>= 2 purchases): the
average interval between the customer's past orders projects the next-due date. A single
purchase has no cadence and is never forecast. Pure derivation; the gather layer reads
the orders store and hands plain data to the pure function (the API calls gather; tests
exercise the pure function directly).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

# Orders in these states count as real purchases (mirror utils/impact._PURCHASED_STATUSES).
_PURCHASED_STATUSES = ("placed", "confirmed", "shipped", "received")

# Within this many days of the projected next-due date -> "due_soon".
_DUE_SOON_DAYS = 14


def _parse(when: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp to a naive datetime (tz dropped for stable day math)."""
    if not when:
        return None
    try:
        dt = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def reorder_forecast(orders: list[dict], now: Optional[datetime] = None) -> list[dict]:
    """Per-part reorder forecast from order history. Parts with < 2 purchases are
    omitted (no cadence). Returned most-urgent first (soonest/over-due). Pure."""
    now = now or datetime.utcnow()

    by_part: dict[tuple, list[dict]] = {}
    for o in orders:
        if o.get("status") not in _PURCHASED_STATUSES:
            continue
        mfg, pn = o.get("manufacturer"), o.get("part_number")
        if not pn:
            continue
        by_part.setdefault((mfg, pn), []).append(o)

    out: list[dict] = []
    for (mfg, pn), os in by_part.items():
        dated = sorted(
            ((_parse(o.get("created_at")), o) for o in os if _parse(o.get("created_at"))),
            key=lambda t: t[0],
        )
        if len(dated) < 2:
            continue
        dates = [d for d, _ in dated]
        intervals = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
        avg = round(sum(intervals) / len(intervals))
        last = dates[-1]
        last_order = dated[-1][1]
        days_since = (now - last).days
        next_due = last + timedelta(days=avg)
        days_until = (next_due - now).days
        status = "overdue" if days_until < 0 else "due_soon" if days_until <= _DUE_SOON_DAYS else "ok"

        part_label = " ".join(str(x) for x in (mfg, pn) if x) or str(pn)
        weeks = max(1, round(avg / 7))
        out.append({
            "manufacturer": mfg,
            "part_number": pn,
            "part": part_label,
            "vendor_name": last_order.get("vendor_name"),
            "order_count": len(dated),
            "avg_interval_days": avg,
            "avg_interval_weeks": weeks,
            "last_ordered": last.isoformat(),
            "days_since": days_since,
            "next_due": next_due.isoformat(),
            "days_until": days_until,
            "status": status,
            "note": _note(weeks, days_until, status),
        })

    out.sort(key=lambda f: f["days_until"])  # most urgent first
    return out


def _note(weeks: int, days_until: int, status: str) -> str:
    cadence = f"You reorder these about every {weeks} week{'s' if weeks != 1 else ''}."
    if status == "overdue":
        return f"{cadence} It's {abs(days_until)} day{'s' if abs(days_until) != 1 else ''} past due — worth reordering."
    if status == "due_soon":
        return f"{cadence} Due in about {days_until} day{'s' if days_until != 1 else ''}."
    return f"{cadence} Still have time — due in about {days_until} days."


def gather_reorder() -> list[dict]:
    """Build the reorder forecast from the orders store. Store-only; no external calls."""
    from utils import orders as orders_store
    return reorder_forecast(orders_store.get_orders())
