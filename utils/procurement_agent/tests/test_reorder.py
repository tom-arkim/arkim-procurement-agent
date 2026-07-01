"""
Tests for utils/reorder.py — reorder-intelligence forecast from the customer's own
order history. Pure function (no DB, no network): given orders, derive per-part cadence
and a next-due forecast. Only parts with a real repeat cadence (>= 2 purchases) qualify;
nothing is predicted from a single order.
"""

from datetime import datetime

from utils.reorder import reorder_forecast

_NOW = datetime(2026, 6, 1)


def _order(mfg, pn, when, vendor="Acme", status="received"):
    return {"manufacturer": mfg, "part_number": pn, "vendor_name": vendor,
            "status": status, "created_at": when}


class TestReorderForecast:
    def test_repeat_part_gets_cadence_and_status(self):
        # Feb 6 -> Apr 3 = 56 days; next due ~ May 29; now Jun 1 -> overdue.
        orders = [
            _order("SKF", "6205", "2026-02-06T00:00:00"),
            _order("SKF", "6205", "2026-04-03T00:00:00"),
        ]
        out = reorder_forecast(orders, now=_NOW)
        assert len(out) == 1
        f = out[0]
        assert f["manufacturer"] == "SKF" and f["part_number"] == "6205"
        assert f["order_count"] == 2
        assert f["avg_interval_days"] == 56
        assert f["status"] == "overdue" and f["days_until"] < 0

    def test_due_soon_window(self):
        # 30-day cadence, last ordered 20 days ago -> due in ~10 days -> due_soon.
        orders = [
            _order("X", "P1", "2026-04-12T00:00:00"),
            _order("X", "P1", "2026-05-12T00:00:00"),
        ]
        out = reorder_forecast(orders, now=_NOW)
        assert out[0]["status"] == "due_soon" and 0 <= out[0]["days_until"] <= 14

    def test_ok_when_far_out(self):
        # 60-day cadence, last ordered a few days ago -> plenty of time -> ok.
        orders = [
            _order("Y", "P2", "2026-03-30T00:00:00"),
            _order("Y", "P2", "2026-05-29T00:00:00"),
        ]
        out = reorder_forecast(orders, now=_NOW)
        assert out[0]["status"] == "ok" and out[0]["days_until"] > 14

    def test_single_order_excluded(self):
        # One purchase = no cadence -> no forecast (never predicted from a single order).
        assert reorder_forecast([_order("Z", "P3", "2026-05-01T00:00:00")], now=_NOW) == []

    def test_drafts_and_cancelled_ignored(self):
        orders = [
            _order("A", "P4", "2026-02-01T00:00:00", status="draft"),
            _order("A", "P4", "2026-03-01T00:00:00", status="cancelled"),
            _order("A", "P4", "2026-04-01T00:00:00", status="received"),
        ]
        # Only one PURCHASED order survives -> no cadence.
        assert reorder_forecast(orders, now=_NOW) == []

    def test_sorted_most_urgent_first(self):
        orders = [
            # part B: overdue
            _order("B", "PB", "2026-02-01T00:00:00"), _order("B", "PB", "2026-03-01T00:00:00"),
            # part C: ok (far out)
            _order("C", "PC", "2026-04-01T00:00:00"), _order("C", "PC", "2026-05-30T00:00:00"),
        ]
        out = reorder_forecast(orders, now=_NOW)
        assert [f["part_number"] for f in out] == ["PB", "PC"]  # urgent (lower days_until) first
