"""
Night 11 T1 — utils/quote_store.py: the structured-quote store + lifecycle
(QUOTE_SUBMIT_V1).

Covers (QUOTE_SUBMISSION_SPEC.md §5/§6, brief T1):
  - flag-off dormancy (writes no-op → None; conftest pins the flag off),
  - submit → instant active (the default path), field/provenance recording,
  - the wrong-part gate: an EDITED PN ⇒ pn_differs ⇒ review, never active,
  - flag-not-block sanity checks: price vs band (band-absent SKIPS), qty,
  - supersede-on-resubmit (same run + supplier: newest wins, history kept),
  - read-time expiry (no cron): active past valid_until reads as expired,
  - review approve/reject + withdraw transitions (guarded),
  - the I1 promotion adapter: as_confirmation_record matches the record shape
    api_server._index_quotes consumes (status "confirmed" + payload.unit_price).

Isolated store: each test points quote_store at a temp sqlite file and opts in
to QUOTE_SUBMIT_V1 explicitly (the conftest autouse pin keeps it off otherwise).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from utils import quote_store as qs


@pytest.fixture()
def store(monkeypatch, tmp_path):
    """Isolated quotes store + QUOTE_SUBMIT_V1 ON."""
    monkeypatch.setenv("QUOTE_SUBMIT_V1", "1")
    monkeypatch.setattr(qs, "_DB_PATH", str(tmp_path / "quotes.sqlite"))
    return qs


def _submit(store, **overrides):
    """A valid default submission (instant-active path) with overrides."""
    kwargs = dict(
        supplier_domain="dxpe.com",
        vendor_name="DXP Enterprises",
        unit_price=189.0,
        submitted_via="rfq_link",
        run_id="run-1",
        manufacturer="Gusher Pumps",
        requested_part_number="84004-28-C238CBC",
        quantity=2,
        requested_quantity=2,
        lead_time="3 days",
    )
    kwargs.update(overrides)
    return store.submit_quote(**kwargs)


# ---------------------------------------------------------------------------
# Flag gating (defense-in-depth; the route gate is the boundary)
# ---------------------------------------------------------------------------

class TestFlagGating:
    def test_flag_off_submit_noops(self, monkeypatch, tmp_path):
        monkeypatch.setattr(qs, "_DB_PATH", str(tmp_path / "quotes.sqlite"))
        # conftest autouse pin keeps QUOTE_SUBMIT_V1 = "" (off).
        assert _submit(qs) is None
        # Nothing was written — the store file was never even created.
        assert qs.get_quotes(run_id="run-1") == []

    def test_flag_off_transitions_noop(self, store, monkeypatch):
        q = _submit(store, quoted_part_number="OTHER-PN")  # review row
        monkeypatch.setenv("QUOTE_SUBMIT_V1", "")
        assert store.approve_review(q["id"]) is None
        assert store.reject_review(q["id"]) is None
        assert store.withdraw(q["id"]) is None

    def test_strict_truthy_parse(self, monkeypatch):
        for v in ("0", "false", "off", "no", "", "  "):
            monkeypatch.setenv("QUOTE_SUBMIT_V1", v)
            assert qs.quote_submit_active() is False
        for v in ("1", "true", "YES", "On"):
            monkeypatch.setenv("QUOTE_SUBMIT_V1", v)
            assert qs.quote_submit_active() is True


# ---------------------------------------------------------------------------
# Submission — the default is INSTANT activation (spec §6)
# ---------------------------------------------------------------------------

class TestSubmit:
    def test_clean_submission_is_instantly_active(self, store):
        q = _submit(store)
        assert q["status"] == "active"
        assert q["effective_status"] == "active"
        assert q["review_reasons"] == []
        assert q["pn_confirmed"] is True and q["pn_differs"] is False

    def test_fields_recorded(self, store):
        q = _submit(store, quote_number="DXP-0091", freight="included",
                    notes="ships from Houston", submitted_by="tok-abc",
                    rfq_id="rfq-9", part_key="gusher pumps|84004-28-C238CBC")
        assert q["quote_number"] == "DXP-0091"
        assert q["freight"] == "included"
        assert q["notes"] == "ships from Houston"
        assert q["submitted_by"] == "tok-abc"
        assert q["rfq_id"] == "rfq-9"
        assert q["part_key"] == "gusher pumps|84004-28-C238CBC"
        assert q["submitted_via"] == "rfq_link"
        assert q["currency"] == "USD"
        assert q["unit_price"] == 189.0
        assert q["is_test"] is True  # provenance default in tests

    def test_domain_normalized(self, store):
        q = _submit(store, supplier_domain="https://www.DXPE.com/contact")
        assert q["supplier_domain"] == "dxpe.com"

    def test_valid_until_defaults_to_14_day_window(self, store):
        q = _submit(store)
        until = datetime.fromisoformat(q["valid_until"])
        delta = until - datetime.now(timezone.utc)
        assert timedelta(days=13) < delta <= timedelta(days=14)

    def test_invalid_inputs_rejected(self, store):
        assert _submit(store, supplier_domain="") is None
        assert _submit(store, submitted_via="carrier_pigeon") is None
        assert _submit(store, unit_price=0) is None
        assert _submit(store, unit_price=-5) is None
        assert _submit(store, unit_price="not-a-price") is None

    def test_all_three_vias_accepted(self, store):
        for via in ("rfq_link", "portal", "concierge"):
            assert _submit(store, submitted_via=via)["submitted_via"] == via


# ---------------------------------------------------------------------------
# The wrong-part gate (spec §4 / criterion 4)
# ---------------------------------------------------------------------------

class TestWrongPartGate:
    def test_edited_pn_lands_in_review_never_active(self, store):
        q = _submit(store, quoted_part_number="84004-28SP")
        assert q["status"] == "review"
        assert q["pn_differs"] is True and q["pn_confirmed"] is False
        assert "pn_differs" in q["review_reasons"]
        assert q["quoted_part_number"] == "84004-28SP"
        assert q["requested_part_number"] == "84004-28-C238CBC"

    def test_blank_quoted_pn_keeps_requested_and_confirms(self, store):
        # The form prefills the requested PN; leaving it untouched confirms it.
        q = _submit(store, quoted_part_number="")
        assert q["pn_differs"] is False
        assert q["quoted_part_number"] == "84004-28-C238CBC"
        assert q["status"] == "active"

    def test_delimiter_variant_is_not_an_edit(self, store):
        # Normalized compare: retyping the PN with different delimiters/case is
        # a confirmation, not an alternative part.
        q = _submit(store, quoted_part_number="84004 28 c238cbc")
        assert q["pn_differs"] is False
        assert q["status"] == "active"


# ---------------------------------------------------------------------------
# Sanity checks — FLAG, never block (spec §6 / criterion 5)
# ---------------------------------------------------------------------------

class TestSanityChecks:
    def test_price_100x_band_lands_in_review(self, store):
        q = _submit(store, unit_price=5325.0, band_median=53.25)
        assert q["status"] == "review"
        assert "price_out_of_band" in q["review_reasons"]

    def test_price_far_below_band_lands_in_review(self, store):
        q = _submit(store, unit_price=5.0, band_median=53.25)
        assert q["status"] == "review"
        assert "price_out_of_band" in q["review_reasons"]

    def test_price_within_band_is_active(self, store):
        q = _submit(store, unit_price=60.0, band_median=53.25)
        assert q["status"] == "active"

    def test_band_absent_skips_price_check(self, store):
        # Absence of data must not flag every quote (brief I5).
        q = _submit(store, unit_price=5325.0, band_median=None)
        assert q["status"] == "active"

    def test_qty_wildly_off_lands_in_review(self, store):
        q = _submit(store, quantity=500, requested_quantity=2)
        assert q["status"] == "review"
        assert "qty_out_of_band" in q["review_reasons"]

    def test_partial_qty_is_real_not_flagged(self, store):
        # Partial quotes are real (spec §4): 1 of 2 requested is fine.
        q = _submit(store, quantity=1, requested_quantity=2)
        assert q["status"] == "active"

    def test_qty_check_skips_without_requested_qty(self, store):
        q = _submit(store, quantity=500, requested_quantity=None)
        assert q["status"] == "active"

    def test_reasons_compose(self, store):
        q = _submit(store, quoted_part_number="OTHER", unit_price=5325.0,
                    band_median=53.25)
        assert set(q["review_reasons"]) == {"pn_differs", "price_out_of_band"}

    def test_compute_review_reasons_pure(self):
        f = qs.compute_review_reasons
        assert f(100.0, pn_differs=False) == []
        assert f(100.0, pn_differs=True) == ["pn_differs"]
        assert f(500.0, pn_differs=False, band_median=100.0) == ["price_out_of_band"]
        assert f(10.0, pn_differs=False, band_median=100.0) == ["price_out_of_band"]
        assert f(250.0, pn_differs=False, band_median=100.0) == []  # 2.5x ok
        assert f(100.0, pn_differs=False, band_median=0) == []      # degenerate band skips
        assert f(100.0, pn_differs=False, quantity=30, requested_quantity=2) == \
            ["qty_out_of_band"]


class TestPriceBandMedian:
    def test_median_from_price_db_entries(self, monkeypatch):
        from utils import price_db
        monkeypatch.setattr(price_db, "get_cached_prices", lambda m, p: {
            "A": {"price": 40.0}, "B": {"price": 50.0}, "C": {"price": 90.0}})
        assert qs.price_band_median("Gusher Pumps", "84004-28") == 50.0

    def test_even_count_averages_middle_pair(self, monkeypatch):
        from utils import price_db
        monkeypatch.setattr(price_db, "get_cached_prices", lambda m, p: {
            "A": {"price": 40.0}, "B": {"price": 60.0}})
        assert qs.price_band_median("Gusher Pumps", "84004-28") == 50.0

    def test_no_entries_means_no_band(self, monkeypatch):
        from utils import price_db
        monkeypatch.setattr(price_db, "get_cached_prices", lambda m, p: {})
        assert qs.price_band_median("Gusher Pumps", "84004-28") is None

    def test_missing_identity_means_no_band(self):
        assert qs.price_band_median(None, "84004-28") is None
        assert qs.price_band_median("Gusher Pumps", None) is None

    def test_store_error_fails_soft(self, monkeypatch):
        from utils import price_db
        def _boom(m, p):
            raise RuntimeError("disk fell off")
        monkeypatch.setattr(price_db, "get_cached_prices", _boom)
        assert qs.price_band_median("Gusher Pumps", "84004-28") is None


# ---------------------------------------------------------------------------
# Supersede-on-resubmit (spec §5 / criterion 6)
# ---------------------------------------------------------------------------

class TestSupersede:
    def test_resubmit_supersedes_prior(self, store):
        q1 = _submit(store, unit_price=189.0)
        q2 = _submit(store, unit_price=175.0)
        assert store.get_quote(q1["id"])["status"] == "superseded"
        assert store.get_quote(q2["id"])["status"] == "active"
        # History kept: both rows exist.
        assert len(store.get_quotes(run_id="run-1")) == 2

    def test_resubmit_supersedes_review_rows_too(self, store):
        q1 = _submit(store, quoted_part_number="OTHER")  # review
        q2 = _submit(store)                              # clean
        assert store.get_quote(q1["id"])["status"] == "superseded"
        assert store.get_quote(q2["id"])["status"] == "active"

    def test_other_supplier_not_superseded(self, store):
        q1 = _submit(store)
        q2 = _submit(store, supplier_domain="sealit.example",
                     vendor_name="Seal It")
        assert store.get_quote(q1["id"])["status"] == "active"
        assert store.get_quote(q2["id"])["status"] == "active"

    def test_other_run_not_superseded(self, store):
        q1 = _submit(store, run_id="run-1")
        q2 = _submit(store, run_id="run-2")
        assert store.get_quote(q1["id"])["status"] == "active"
        assert store.get_quote(q2["id"])["status"] == "active"


# ---------------------------------------------------------------------------
# Read-time expiry (spec §5/§6 — no cron, no zombie confirmations)
# ---------------------------------------------------------------------------

def _force_valid_until(store, quote_id: str, dt: datetime) -> None:
    conn = sqlite3.connect(store._DB_PATH)
    conn.execute("UPDATE quotes SET valid_until = ? WHERE id = ?",
                 (dt.isoformat(), quote_id))
    conn.commit()
    conn.close()


class TestReadTimeExpiry:
    def test_lapsed_active_reads_as_expired(self, store):
        q = _submit(store)
        _force_valid_until(store, q["id"],
                           datetime.now(timezone.utc) - timedelta(days=1))
        got = store.get_quote(q["id"])
        assert got["status"] == "active"            # stored status untouched
        assert got["effective_status"] == "expired"  # read-time verdict

    def test_expired_quote_stops_driving_promotion(self, store):
        q = _submit(store)
        assert [x["id"] for x in store.get_active_quotes("run-1")] == [q["id"]]
        _force_valid_until(store, q["id"],
                           datetime.now(timezone.utc) - timedelta(minutes=1))
        assert store.get_active_quotes("run-1") == []

    def test_status_filter_finds_expired(self, store):
        q = _submit(store)
        _force_valid_until(store, q["id"],
                           datetime.now(timezone.utc) - timedelta(days=1))
        assert [x["id"] for x in store.get_quotes(run_id="run-1",
                                                  status="expired")] == [q["id"]]

    def test_non_active_statuses_never_expire(self, store):
        q = _submit(store, quoted_part_number="OTHER")  # review
        _force_valid_until(store, q["id"],
                           datetime.now(timezone.utc) - timedelta(days=1))
        assert store.get_quote(q["id"])["effective_status"] == "review"

    def test_naive_valid_until_treated_as_utc(self, store):
        q = _submit(store)
        conn = sqlite3.connect(store._DB_PATH)
        conn.execute("UPDATE quotes SET valid_until = ? WHERE id = ?",
                     ("2020-01-01T00:00:00", q["id"]))  # naive, long past
        conn.commit()
        conn.close()
        assert store.get_quote(q["id"])["effective_status"] == "expired"


# ---------------------------------------------------------------------------
# Review transitions (spec §6) + withdraw
# ---------------------------------------------------------------------------

class TestTransitions:
    def test_approve_activates_review_quote(self, store):
        q = _submit(store, quoted_part_number="OTHER")
        out = store.approve_review(q["id"], resolved_by="admin")
        assert out["status"] == "active"
        assert out["resolved_by"] == "admin"
        assert out["resolved_at"]
        # Approval never relabels: the quoted PN stays the quoted PN.
        assert out["quoted_part_number"] == "OTHER"
        assert out["pn_differs"] is True

    def test_approve_guards_on_current_status(self, store):
        q = _submit(store)  # already active
        assert store.approve_review(q["id"]) is None
        assert store.approve_review("no-such-id") is None

    def test_reject_withdraws(self, store):
        q = _submit(store, quoted_part_number="OTHER")
        assert store.reject_review(q["id"])["status"] == "withdrawn"
        assert store.get_active_quotes("run-1") == []

    def test_withdraw_active_quote(self, store):
        q = _submit(store)
        assert store.withdraw(q["id"])["status"] == "withdrawn"
        assert store.get_active_quotes("run-1") == []

    def test_withdraw_guards(self, store):
        q = _submit(store)
        store.withdraw(q["id"])
        assert store.withdraw(q["id"]) is None  # already withdrawn


# ---------------------------------------------------------------------------
# The I1 promotion adapter — must match api_server._index_quotes consumption
# ---------------------------------------------------------------------------

class TestConfirmationRecordAdapter:
    def test_shape_matches_the_existing_promotion_reader(self, store):
        q = _submit(store, lead_time="3 days")
        rec = store.as_confirmation_record(q)
        # What _index_quotes filters on and _resolve_quote/_quote_overlay read:
        assert rec["status"] == "confirmed"
        assert rec["supplier_domain"] == "dxpe.com"
        assert rec["thread_id"] is None
        assert rec["confidence"] is None      # supplier-authored: no extraction
        assert rec["payload"]["unit_price"] == 189.0
        assert rec["payload"]["currency"] == "USD"
        assert rec["payload"]["lead_time"] == "3 days"

    def test_overlay_accepts_the_adapted_record(self, store):
        # Drive the REAL overlay function with the adapted record: the price
        # must overlay, supplier-confirmed must claim, and the absent
        # confidence must read as "no signal" (never quoteUnverified).
        import api_server
        q = _submit(store)
        overlay = api_server._quote_overlay(store.as_confirmation_record(q))
        assert overlay["price"] == 189.0
        assert overlay["quoteConfirmed"] is True
        assert overlay["supplierConfirmed"] is True
        assert overlay["quoteUnverified"] is False
        assert overlay["evidenceState"] == "quoted"

    def test_card_labelling_fields_ride_in_payload(self, store):
        q = _submit(store, quoted_part_number="84004-28SP")
        store.approve_review(q["id"])
        rec = store.as_confirmation_record(store.get_quote(q["id"]))
        assert rec["payload"]["pn_differs"] is True
        assert rec["payload"]["quoted_part_number"] == "84004-28SP"
        assert rec["payload"]["quote_id"] == q["id"]
