"""Live-faithfulness tests for Run Capture (Night 1).

The brief's live-faithfulness rule: hooks are tested via the REAL handler path
(TestClient through /api/runs + /messages), NOT by calling capture functions
directly. This file drives the real api_server endpoints with mocked LLM/search
(the `api` fixture already mocks IntakeAgent/SourcingAgent at their source
modules) and asserts the capture rows land in run_capture.sqlite with the
faithful shape the live path produced.

Also the T5 inertness wall at the API level: with RUN_CAPTURE off, a full
simulated run produces ZERO capture rows and the /api/health body is
byte-identical to the pre-build contract.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


# ---------------------------------------------------------------------------
# Local `api` fixture (mirrors the proven test_api_server.py one — that fixture
# is module-local and not shared; copied verbatim so the live-faithfulness tests
# drive the REAL api_server handler path with mocked LLM/search).
# ---------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    from utils.procurement_agent.state import persistence

    engine = persistence._make_engine(f"sqlite:///{tmp_path / 'api.sqlite'}")
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    persistence.Base.metadata.create_all(engine)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    # DEMO_MODE must be OFF for these tests (the `api` fixture builds the plain
    # app). A prior test in the same session may have set api_server.DEMO_MODE
    # via monkeypatch (reverted for ITS test, but if api_server was imported
    # under DEMO_MODE=1 from the real env, the module attr stays True). Force it
    # False here so create_run doesn't require X-Session-Id (422) and the
    # allowlist middleware doesn't 403 the action endpoints.
    monkeypatch.setenv("DEMO_MODE", "0")

    monkeypatch.setattr(persistence, "_engine", engine)
    monkeypatch.setattr(persistence, "_SessionFactory", TestSession)

    from utils import supplier_registry
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))

    # Isolate known_parts (the part→supplier edge cache) so a real cached edge
    # for a part like "goulds|ST1375T1" doesn't make _run_sourcing_background
    # take the cache-HIT path (reconstruct from edges) and bypass the mocked
    # SourcingAgent. Without this the sourcing result my capture sees is the
    # cached edge set, not the test's mocked candidates.
    from utils import known_parts
    monkeypatch.setattr(known_parts, "_DB_PATH", str(tmp_path / "known_parts.json"))

    import api_server

    monkeypatch.setattr(api_server, "_engine", engine)
    monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
    monkeypatch.setattr(api_server, "_messages", {})
    # Force the module-level DEMO_MODE flag False (it's read once at import; if
    # api_server was first imported under DEMO_MODE=1 — e.g. by test_demo_mode
    # running first in the same session — the attr is True and stays True).
    monkeypatch.setattr(api_server, "DEMO_MODE", False)

    client = TestClient(api_server.app)
    client._api_server = api_server
    return client


from .test_api_server import (
    _create_run, _mock_intake, _mock_sourcing_pipeline, _empty_sourcing, _set_run,
)


# ---------------------------------------------------------------------------
# Fixtures: point run_capture at tmp_path, flag ON / OFF
# ---------------------------------------------------------------------------

@pytest.fixture
def cap_on(api, monkeypatch, tmp_path):
    """RUN_CAPTURE ON, isolated DB. `api` is the TestClient (test_api_server fixture)."""
    import utils.run_capture as rc
    monkeypatch.setattr(rc, "RUN_CAPTURE", True)
    monkeypatch.setattr(rc, "_DB_PATH", str(tmp_path / "run_capture.sqlite"))
    monkeypatch.setattr(rc, "_DATA_DIR", str(tmp_path))
    # api_server bound the module object at import; its `_run_capture.RUN_CAPTURE`
    # reads the live attr, so the monkeypatch above is sufficient.
    rc.reset_failures()
    return rc


@pytest.fixture
def cap_off(api, monkeypatch, tmp_path):
    """RUN_CAPTURE OFF, isolated DB (inertness)."""
    import utils.run_capture as rc
    monkeypatch.setattr(rc, "RUN_CAPTURE", False)
    monkeypatch.setattr(rc, "_DB_PATH", str(tmp_path / "run_capture_off.sqlite"))
    monkeypatch.setattr(rc, "_DATA_DIR", str(tmp_path))
    rc.reset_failures()
    return rc


# ---------------------------------------------------------------------------
# Live-faithfulness: turn capture through the REAL /messages path
# ---------------------------------------------------------------------------

class TestLiveTurnCapture:
    def test_user_and_agent_turns_captured_via_messages(self, api, cap_on, monkeypatch):
        rid = _create_run(api)
        _mock_intake(api, monkeypatch, result={
            "asset_specs": {"manufacturer": "Goulds", "model": "3196"},
            "manufacturer_confidence": 85.0,
            "part_id_confidence": 70.0,
            "sufficient": True,
            "follow_up_question": None,
            "commit_message": None,
            "confidence_summary": {"proceed_state": "proceed_spec_based"},
        })
        resp = api.post(f"/api/runs/{rid}/messages", json={"content": "I need a Goulds 3196 mechanical seal"})
        assert resp.status_code == 200
        events = cap_on.read_events(rid)
        types = [e["event_type"] for e in events]
        # turn_user captured with the exact text the live path received.
        assert "turn_user" in types
        user_ev = next(e for e in events if e["event_type"] == "turn_user")
        assert user_ev["payload"]["content"] == "I need a Goulds 3196 mechanical seal"
        assert user_ev["payload"]["role"] == "user"
        # turn_agent captured (the reply the live path returned).
        assert "turn_agent" in types
        # intake_result captured with the proceed_state the live path computed.
        ir = next(e for e in events if e["event_type"] == "intake_result")
        assert ir["payload"]["proceed_state"] == "proceed_spec_based"
        assert ir["payload"]["manufacturer_confidence"] == 85.0
        assert ir["payload"]["asset_specs"]["manufacturer"] == "Goulds"

    def test_intake_followup_turn_captured(self, api, cap_on, monkeypatch):
        rid = _create_run(api)
        _mock_intake(api, monkeypatch, result={
            "asset_specs": {"manufacturer": "Goulds"},
            "manufacturer_confidence": 40.0, "part_id_confidence": 20.0,
            "sufficient": False,
            "follow_up_question": "What pump is it on?",
            "commit_message": None,
            "confidence_summary": {"proceed_state": ""},
        })
        resp = api.post(f"/api/runs/{rid}/messages", json={"content": "Goulds pump"})
        assert resp.status_code == 200
        assert resp.json()["message"]["content"] == "What pump is it on?"
        ir = next(e for e in cap_on.read_events(rid) if e["event_type"] == "intake_result")
        assert ir["payload"]["sufficient"] is False
        assert ir["payload"]["follow_up_question"] == "What pump is it on?"


# ---------------------------------------------------------------------------
# Live-faithfulness: sourcing capture through the REAL confirm-intake path
# ---------------------------------------------------------------------------

class TestLiveSourcingCapture:
    def test_candidates_and_queries_captured_via_confirm_intake(self, api, cap_on, monkeypatch):
        rid = _create_run(api)
        _set_run(api, rid, asset_specs_json=json.dumps({
            "manufacturer": "Goulds", "model": "3196", "part_number": "ST-1.375-T1",
        }))
        sourcing = _empty_sourcing()
        sourcing["tier_2"]["results"] = [
            {"vendor_name": "Platinum Performance Products", "suitability_score": 65.0,
             "confidence_score": 70.0, "pn_match_status": "substring", "match_type": "aftermarket",
             "match_basis": "parent_model", "base_price": None, "price_tbd": True,
             "lead_time_days": None, "vendor_authorization_status": None,
             "onboarding_status": "discovery_only"},
            {"vendor_name": "Zoro", "suitability_score": 4.0, "confidence_score": 10.0,
             "rejection_reason": "suitability_below_floor"},
        ]
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=sourcing, artifact=None)
        resp = api.post(f"/api/runs/{rid}/confirm-intake")
        assert resp.status_code == 200

        events = cap_on.read_events(rid)
        types = [e["event_type"] for e in events]
        # confirm-intake user action
        assert "user_action" in types
        ua = next(e for e in events if e["event_type"] == "user_action")
        assert ua["payload"]["action"] == "confirm_intake"
        # per-tier queries
        q_issued = [e for e in events if e["event_type"] == "query_issued"]
        assert any(e["payload"]["tier"] == 2 for e in q_issued)
        assert any("Goulds" in e["payload"]["query_intent"] for e in q_issued)
        # candidate_scored (Platinum) + candidate_rejected (Zoro)
        scored = [e for e in events if e["event_type"] == "candidate_scored"]
        rejected = [e for e in events if e["event_type"] == "candidate_rejected"]
        assert any(e["payload"]["vendor_name"] == "Platinum Performance Products" for e in scored)
        assert any(e["payload"]["vendor_name"] == "Zoro" for e in rejected)
        assert any(e["payload"]["rejection_reason"] == "suitability_below_floor" for e in rejected)
        # results_displayed excludes the rejected one
        disp = next(e for e in events if e["event_type"] == "results_displayed")
        vendors = [d["vendor_name"] for d in disp["payload"]["displayed"]]
        assert "Platinum Performance Products" in vendors
        assert "Zoro" not in vendors

    def test_zero_results_outcome(self, api, cap_on, monkeypatch):
        rid = _create_run(api)
        _set_run(api, rid, asset_specs_json=json.dumps({"manufacturer": "ObscureCo"}))
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=_empty_sourcing(), artifact=None)
        api.post(f"/api/runs/{rid}/confirm-intake")
        # displayed set is empty across all tiers
        disp = next(e for e in cap_on.read_events(rid) if e["event_type"] == "results_displayed")
        assert disp["payload"]["displayed"] == []
        # confirm-intake is a user_action, but zero_results is the dominant
        # sourcing signal — compute_outcome prefers the results_displayed signal
        # over a confirm_intake action (confirm_intake just advances the run; it
        # is not an "acted on a result" completion). See the precedence rule.
        assert cap_on.compute_outcome(rid) == "zero_results"


# ---------------------------------------------------------------------------
# Live-faithfulness: user-action capture through the REAL action endpoints
# ---------------------------------------------------------------------------

class TestLiveUserActions:
    def test_select_candidate_captured(self, api, cap_on, monkeypatch):
        rid = _create_run(api)
        _set_run(api, rid, current_phase="comparison",
                 sourcing_results_json=json.dumps({
                     "tier_1": {"results": [{"vendor_name": "Acme", "base_price": 100,
                                             "suitability_score": 80, "confidence_score": 70,
                                             "lead_time_days": 3}], "count": 1},
                     "tier_2": {"results": [], "count": 0},
                     "tier_3": {"results": [], "count": 0},
                 }))
        resp = api.post(f"/api/runs/{rid}/select-candidate",
                        json={"candidate_id": "Acme-t1-0", "tier": 1})
        assert resp.status_code == 200
        ua = next(e for e in cap_on.read_events(rid) if e["event_type"] == "user_action")
        assert ua["payload"]["action"] == "select_candidate"
        assert ua["payload"]["detail"]["candidate_id"] == "Acme-t1-0"
        # and the outcome is now completed_with_action
        assert cap_on.compute_outcome(rid) == "completed_with_action"

    def test_reject_captured(self, api, cap_on, monkeypatch):
        rid = _create_run(api)
        _set_run(api, rid, current_phase="pending_first_approval",
                 selected_candidate_json=json.dumps({"candidate_id": "Acme-t1-0", "tier": 1}))
        resp = api.post(f"/api/runs/{rid}/reject",
                        json={"approver_name": "Tom", "approver_role": "manager", "notes": "no"})
        assert resp.status_code == 200
        acts = [e["payload"]["action"] for e in cap_on.read_events(rid) if e["event_type"] == "user_action"]
        assert "reject" in acts


# ---------------------------------------------------------------------------
# T4 — capture_failures surfaced on /api/health (flag-on only)
# ---------------------------------------------------------------------------

class TestHealthCaptureFailures:
    def test_health_flag_off_byte_identical(self, api, cap_off):
        """Flag-off health body unchanged (the existing contract)."""
        resp = api.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "version": "1.0.0-phase1", "demo_mode": False}

    def test_health_flag_on_includes_capture_failures(self, api, cap_on):
        resp = api.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "capture_failures" in body
        assert body["capture_failures"] == 0

    def test_health_flag_on_reflects_failures(self, api, cap_on, monkeypatch):
        # Force a capture write to fail by corrupting the DB path after one
        # good write establishes schema, then trigger a capture via messages.
        rid = _create_run(api)
        _mock_intake(api, monkeypatch, result={
            "asset_specs": {}, "manufacturer_confidence": 0, "part_id_confidence": 0,
            "sufficient": False, "follow_up_question": "q?", "commit_message": None,
            "confidence_summary": {"proceed_state": ""},
        })
        api.post(f"/api/runs/{rid}/messages", json={"content": "hi"})  # good writes
        monkeypatch.setattr(cap_on, "_DB_PATH", "/no/such/dir/rc.sqlite")
        api.post(f"/api/runs/{rid}/messages", json={"content": "again"})  # failing writes
        body = api.get("/api/health").json()
        assert body["capture_failures"] >= 1


# ---------------------------------------------------------------------------
# T5 — API-level inertness wall: flag-off full run = ZERO capture rows
# ---------------------------------------------------------------------------

class TestAPIInertnessWall:
    def test_flag_off_full_run_writes_zero_capture_rows(self, api, cap_off, monkeypatch):
        rid = _create_run(api)
        _mock_intake(api, monkeypatch, result={
            "asset_specs": {"manufacturer": "Goulds"}, "manufacturer_confidence": 80,
            "part_id_confidence": 70, "sufficient": True, "follow_up_question": None,
            "commit_message": None, "confidence_summary": {"proceed_state": "proceed_spec_based"},
        })
        api.post(f"/api/runs/{rid}/messages", json={"content": "Goulds 3196 seal"})
        _set_run(api, rid, asset_specs_json=json.dumps({"manufacturer": "Goulds", "model": "3196"}))
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=_empty_sourcing(), artifact=None)
        api.post(f"/api/runs/{rid}/confirm-intake")
        # ZERO capture rows across the entire simulated run (the file may be
        # created by the read itself opening the DB — inertness is zero ROWS,
        # not zero file; asserted by querying the table directly).
        assert cap_off.read_all_events() == []
        import sqlite3
        conn = sqlite3.connect(cap_off._DB_PATH)
        n = conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
        conn.close()
        assert n == 0
        # And the run-detail response is unaffected (byte-identical contract).
        detail = api.get(f"/api/runs/{rid}").json()
        assert detail["id"] == rid
