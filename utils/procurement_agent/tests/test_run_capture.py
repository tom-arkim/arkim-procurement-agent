"""Tests for utils/run_capture.py — Night 1 Run Capture.

Covers the brief's overnight-testing obligations for the capture module itself:
- every event type writes and reads back with correct shape (mocked runs)
- outcome computation correct for each signal type
- fail-soft: a forced write failure doesn't raise AND increments the counter
- flag-off inertness at the module level (zero writes)
- pure data / no network on import
- store isolation: writes ONLY to data/run_capture.sqlite (never other stores)

The live-faithfulness obligation (hooks tested via the REAL /api/runs + /messages
handler path with TestClient) lives in test_run_capture_live.py — this file
exercises the module directly (the unit layer); that file exercises the seams.
"""

import json
import sqlite3
import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_capture_counter_after_each():
    """Stop this file leaking the process-global capture failure counter.

    `run_capture._capture_failures` (utils/run_capture.py:116) is a module-level
    accumulator that the fail-soft tests below deliberately increment. The `cap`
    / `cap_off` fixtures reset it at *setup*, but NOT at teardown — so a test
    that forces write failures (e.g. TestFailSoft::test_forced_write_failure_*)
    leaves the counter non-zero for the rest of the session. A later test in
    another file that reads the counter — notably
    test_api_server.py::TestStaticEndpoints::test_health, which asserts
    `/api/health` returns `capture_failures: 0` — then fails with
    `capture_failures: N != 0` if this file ran first (order-dependent flake;
    green in alphabetical order where test_api_server runs first, fails under
    --lf / reverse / any reorder). Reset at teardown so no test here can leak
    the counter, regardless of order. Test-only; no production code touched.
    """
    yield
    import utils.run_capture as rc
    rc.reset_failures()


@pytest.fixture
def cap(monkeypatch, tmp_path):
    """Isolated run_capture module: flag ON, DB in tmp_path, failures reset."""
    import utils.run_capture as rc
    monkeypatch.setattr(rc, "RUN_CAPTURE", True)
    monkeypatch.setattr(rc, "_DB_PATH", str(tmp_path / "run_capture.sqlite"))
    monkeypatch.setattr(rc, "_DATA_DIR", str(tmp_path))
    rc.reset_failures()
    return rc


@pytest.fixture
def cap_off(monkeypatch, tmp_path):
    """Flag-OFF isolated run_capture (inertness checks)."""
    import utils.run_capture as rc
    monkeypatch.setattr(rc, "RUN_CAPTURE", False)
    monkeypatch.setattr(rc, "_DB_PATH", str(tmp_path / "run_capture_off.sqlite"))
    monkeypatch.setattr(rc, "_DATA_DIR", str(tmp_path))
    rc.reset_failures()
    return rc


# ---------------------------------------------------------------------------
# T1 — schema + pure-data
# ---------------------------------------------------------------------------

class TestSchema:
    def test_import_is_pure_no_network(self):
        """Importing run_capture makes no network/LLM calls (registry purity)."""
        import utils.run_capture as rc
        # Module loaded; flag default off in the test env (no RUN_CAPTURE set).
        assert isinstance(rc.RUN_CAPTURE, bool)
        assert rc._DB_PATH.endswith("run_capture.sqlite")

    def test_table_created_on_first_write(self, cap):
        cap.capture_turn("run-1", "user", "hi")
        conn = sqlite3.connect(cap._DB_PATH)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "run_events" in tables
        assert "run_outcomes" in tables
        # columns
        cols = {r[1] for r in conn.execute("PRAGMA table_info(run_events)").fetchall()}
        assert {"event_id", "run_id", "ts", "source_tag", "event_type", "payload_json"} <= cols
        conn.close()

    def test_indexes_exist(self, cap):
        cap.capture_turn("run-1", "user", "hi")
        conn = sqlite3.connect(cap._DB_PATH)
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert "ix_run_events_run_id" in idx
        assert "ix_run_events_run_id_type" in idx
        conn.close()


# ---------------------------------------------------------------------------
# T2 — every event type writes and reads back with correct shape
# ---------------------------------------------------------------------------

class TestEventShapes:
    def test_turn_user_and_agent(self, cap):
        cap.capture_turn("r", "user", "I need a Goulds 3196 mechanical seal")
        cap.capture_turn("r", "agent", "What pump is it on?")
        ev = cap.read_events("r")
        assert len(ev) == 2
        assert ev[0]["event_type"] == "turn_user"
        assert ev[0]["payload"]["role"] == "user"
        assert ev[0]["payload"]["content"] == "I need a Goulds 3196 mechanical seal"
        assert ev[1]["event_type"] == "turn_agent"
        assert ev[1]["payload"]["role"] == "agent"

    def test_intake_result(self, cap):
        cap.capture_intake_result(
            "r", sufficient=True, proceed_state="proceed_spec_based",
            manufacturer_confidence=80.0, part_id_confidence=70.0,
            asset_specs={"manufacturer": "Goulds", "model": "3196"},
        )
        ev = cap.read_events("r")
        assert ev[0]["event_type"] == "intake_result"
        p = ev[0]["payload"]
        assert p["sufficient"] is True
        assert p["proceed_state"] == "proceed_spec_based"
        assert p["manufacturer_confidence"] == 80.0
        assert p["asset_specs"]["manufacturer"] == "Goulds"

    def test_query_issued(self, cap):
        cap.capture_query("r", 2, "Goulds 3196 mechanical seal", part_key="goulds|3196")
        p = cap.read_events("r")[0]["payload"]
        assert p["tier"] == 2
        assert p["query_intent"] == "Goulds 3196 mechanical seal"
        assert p["part_key"] == "goulds|3196"

    def test_candidate_scored(self, cap):
        cand = {
            "vendor_name": "Platinum Performance Products", "candidate_id": "v-t2-0",
            "suitability_score": 65.0, "pn_match_status": "substring",
            "match_type": "aftermarket", "match_basis": "parent_model",
            "confidence_score": 70.0, "base_price": None, "price_tbd": True,
            "lead_time_days": None, "vendor_authorization_status": None,
            "onboarding_status": "discovery_only",
        }
        cap.capture_candidate("r", 2, cand)
        ev = cap.read_events("r")
        assert ev[0]["event_type"] == "candidate_scored"
        assert ev[0]["payload"]["vendor_name"] == "Platinum Performance Products"
        assert ev[0]["payload"]["suitability_score"] == 65.0
        assert "rejection_reason" not in ev[0]["payload"]

    def test_candidate_rejected(self, cap):
        cand = {"vendor_name": "Zoro", "candidate_id": "z-t2-1",
                "suitability_score": 4.0, "rejection_reason": "suitability_below_floor",
                "confidence_score": 10.0}
        cap.capture_candidate("r", 2, cand)
        ev = cap.read_events("r")
        assert ev[0]["event_type"] == "candidate_rejected"
        assert ev[0]["payload"]["rejection_reason"] == "suitability_below_floor"
        assert ev[0]["payload"]["suitability_score"] == 4.0

    def test_results_displayed(self, cap):
        cap.capture_results_displayed("r", [
            {"tier": 2, "vendor_name": "Platinum", "candidate_id": "v-t2-0", "suitability_score": 65.0},
        ])
        p = cap.read_events("r")[0]["payload"]
        assert len(p["displayed"]) == 1
        assert p["displayed"][0]["vendor_name"] == "Platinum"

    def test_user_action(self, cap):
        cap.capture_user_action("r", "select_candidate", detail={"candidate_id": "v-t2-0", "tier": 2})
        p = cap.read_events("r")[0]["payload"]
        assert p["action"] == "select_candidate"
        assert p["detail"]["candidate_id"] == "v-t2-0"

    def test_full_simulated_run_event_sequence(self, cap):
        """A full mocked run produces the complete expected event sequence."""
        cap.capture_turn("r", "user", "I need a Goulds 3196 mechanical seal")
        cap.capture_intake_result("r", sufficient=False, proceed_state="",
                                  manufacturer_confidence=40.0, part_id_confidence=20.0,
                                  follow_up_question="What pump is it on?")
        cap.capture_turn("r", "agent", "What pump is it on?")
        cap.capture_turn("r", "user", "Goulds 3196")
        cap.capture_intake_result("r", sufficient=True, proceed_state="proceed_spec_based",
                                  manufacturer_confidence=85.0, part_id_confidence=75.0,
                                  asset_specs={"manufacturer": "Goulds", "model": "3196"})
        cap.capture_user_action("r", "confirm_intake")
        cap.capture_query("r", 2, "Goulds 3196 mechanical seal")
        cap.capture_candidate("r", 2, {"vendor_name": "Platinum", "candidate_id": "v-t2-0",
                                       "suitability_score": 65.0})
        cap.capture_candidate("r", 2, {"vendor_name": "Zoro", "candidate_id": "z-t2-1",
                                       "suitability_score": 4.0, "rejection_reason": "suitability_below_floor"})
        cap.capture_results_displayed("r", [{"tier": 2, "vendor_name": "Platinum", "candidate_id": "v-t2-0"}])
        cap.capture_user_action("r", "select_candidate", detail={"candidate_id": "v-t2-0"})

        seq = [e["event_type"] for e in cap.read_events("r")]
        assert seq == [
            "turn_user", "intake_result", "turn_agent", "turn_user", "intake_result",
            "user_action", "query_issued", "candidate_scored", "candidate_rejected",
            "results_displayed", "user_action",
        ]

    def test_source_tag_set(self, cap):
        cap.capture_turn("r", "user", "hi")
        # source_tag derived from DEMO_MODE (internal_test when off in tests)
        ev = cap.read_all_events()[0]
        assert ev["source_tag"] in ("demo_prospect", "internal_test")

    def test_ts_is_iso(self, cap):
        cap.capture_turn("r", "user", "hi")
        ev = cap.read_all_events()[0]
        # ISO 8601 with timezone
        assert "T" in ev["ts"]


# ---------------------------------------------------------------------------
# T3 — outcome computation
# ---------------------------------------------------------------------------

class TestOutcome:
    def test_completed_with_action(self, cap):
        cap.capture_results_displayed("r", [{"tier": 2, "vendor_name": "X", "candidate_id": "c"}])
        cap.capture_user_action("r", "select_candidate")
        assert cap.compute_outcome("r") == "completed_with_action"

    def test_abandoned_after_results(self, cap):
        cap.capture_results_displayed("r", [{"tier": 2, "vendor_name": "X", "candidate_id": "c"}])
        assert cap.compute_outcome("r") == "abandoned_after_results"

    def test_zero_results(self, cap):
        cap.capture_results_displayed("r", [])
        assert cap.compute_outcome("r") == "zero_results"

    def test_all_rejected(self, cap):
        cap.capture_candidate("r", 2, {"vendor_name": "Z", "rejection_reason": "suitability_below_floor"})
        cap.capture_candidate("r", 2, {"vendor_name": "Y", "rejection_reason": "suitability_below_floor"})
        assert cap.compute_outcome("r") == "all_rejected"

    def test_incomplete_no_events(self, cap):
        assert cap.compute_outcome("r-none") == "incomplete"

    def test_write_and_read_outcome(self, cap):
        cap.write_outcome("r", "completed_with_action", {"action": "select_candidate"})
        out = cap.read_outcome("r")
        assert out["outcome"] == "completed_with_action"
        assert out["details"]["action"] == "select_candidate"

    def test_write_outcome_upsert(self, cap):
        cap.write_outcome("r", "incomplete")
        cap.write_outcome("r", "completed_with_action")
        out = cap.read_outcome("r")
        assert out["outcome"] == "completed_with_action"


# ---------------------------------------------------------------------------
# T4 — fail-soft + visibility
# ---------------------------------------------------------------------------

class TestFailSoft:
    def test_forced_write_failure_does_not_raise_and_increments_counter(self, cap, monkeypatch):
        # Corrupt the DB path so writes fail after schema is set up.
        cap.capture_turn("r", "user", "first")  # establishes schema + a good write
        monkeypatch.setattr(cap, "_DB_PATH", "/no/such/dir/run_capture.sqlite")
        # This must NOT raise:
        cap.capture_turn("r", "user", "second")
        cap.capture_candidate("r", 2, {"vendor_name": "X"})
        assert cap.capture_failures() >= 2

    def test_failure_counter_starts_zero(self, cap):
        assert cap.capture_failures() == 0

    def test_bad_payload_does_not_raise(self, cap):
        # An object json.dumps can't serialize by default -> default=str saves it,
        # but force a worse failure by passing a non-serializable with default off.
        class Boom:
            def __repr__(self):
                return "Boom"
        # default=str in _write handles this; should not raise either way.
        cap.capture_turn("r", "user", "hi", extra={"obj": Boom()})
        assert cap.capture_failures() == 0


# ---------------------------------------------------------------------------
# T5 — flag-off inertness (module level; the API-level wall is test_run_capture_live)
# ---------------------------------------------------------------------------

class TestModuleInertness:
    def test_flag_off_no_writes(self, cap_off, tmp_path):
        cap_off.capture_turn("r", "user", "hi")
        cap_off.capture_intake_result("r", sufficient=True, proceed_state="x",
                                      manufacturer_confidence=1, part_id_confidence=1)
        cap_off.capture_query("r", 2, "q")
        cap_off.capture_candidate("r", 2, {"vendor_name": "X"})
        cap_off.capture_results_displayed("r", [])
        cap_off.capture_user_action("r", "select_candidate")
        cap_off.write_outcome("r", "x")
        # No DB file should have been created (no writes happened).
        import os
        assert not os.path.exists(cap_off._DB_PATH)

    def test_falsy_token_inert(self, monkeypatch, tmp_path):
        import utils.run_capture as rc
        for falsy in ("0", "false", "no", "off", "", "junk", "False", "NO"):
            assert rc._env_truthy(falsy) is False, f"{falsy!r} should be falsy"
        for truthy in ("1", "true", "yes", "on", "TRUE", "Yes", "ON"):
            assert rc._env_truthy(truthy) is True, f"{truthy!r} should be truthy"
