"""
Characterization tests for api_server.py FastAPI endpoints.

These lock in the CURRENT contract (status codes + response shapes) of the
endpoints before any refactor (decomposition, DI, exception hierarchy,
CamelModel). They capture what IS, not what SHOULD BE — so a later refactor can
be proven behavior-preserving and any intentional change shows up as a diff
here.

Isolation: the DB is redirected to a throwaway SQLite file (persistence globals
patched BEFORE importing api_server, so the module's import-time
create_all/seed never touch the real data/sourcing_runs.sqlite). The only live
externals any covered route reaches — IntakeAgent (Anthropic) — are mocked, so
the suite runs fully offline.

Documented current inconsistencies (targets for the refactor, NOT fixed here):
  * Action endpoints (approve/reject/outreach/select/etc.) return ad-hoc
    snake_case dicts with no response_model, while create/list/get use typed
    models. Error envelope is the FastAPI default {"detail": ...}.
  * 409 responses (open-from-pending, reject-submission, confirm-intake) put a
    *dict* in `detail` ({"message", "current_phase"}) — non-standard vs the
    string `detail` used elsewhere.
  * RunDetail's envelope is snake_case but its nested sourcing_results payload
    is camelCase (vendorName, leadTime, ...) — mixed casing in one response.
  * send_message swallows ALL IntakeAgent exceptions and returns 200 with a
    synthetic agent reply (masks failures behind a success status).
  * approve always advances to "approved" regardless of approval-rule
    thresholds, and accepts any approver_role from the body unchecked
    (no RBAC — CLEANUP.md §4.1).
"""
import json
import pytest
from unittest.mock import Mock

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def api(tmp_path, monkeypatch):
    """TestClient over api_server with the DB pointed at a temp SQLite file."""
    from utils.procurement_agent.state import persistence

    engine = persistence._make_engine(f"sqlite:///{tmp_path / 'api.sqlite'}")
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    persistence.Base.metadata.create_all(engine)

    # Neutralize api_server's import-time load_dotenv(): with override=False it
    # skips already-set vars, so setting these empty here prevents real keys from
    # leaking into os.environ for the rest of the session (which would make other
    # tests, e.g. the orchestrator suite, fire live Tavily/Anthropic calls).
    # monkeypatch reverts them after the test, keeping the wider suite key-less.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")

    # Patch persistence globals before import so api_server's module-level
    # create_all/_migrate_schema/_seed run against the temp DB, not the real one.
    monkeypatch.setattr(persistence, "_engine", engine)
    monkeypatch.setattr(persistence, "_SessionFactory", TestSession)

    import api_server

    # Redirect the names api_server bound at its own import time too (covers the
    # case where the module was already imported earlier in the session).
    monkeypatch.setattr(api_server, "_engine", engine)
    monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
    monkeypatch.setattr(api_server, "_messages", {})  # isolate in-memory chat store

    client = TestClient(api_server.app)
    client._api_server = api_server  # expose module for per-test mocking
    return client


def _create_run(client, **body) -> str:
    """Create a run via the API and return its id (also exercises POST /api/runs)."""
    resp = client.post("/api/runs", json=body)
    assert resp.status_code == 201
    return resp.json()["id"]


def _mock_intake(client, monkeypatch, result):
    """Replace IntakeAgent so .run() returns a fixed result dict (offline)."""
    agent = Mock()
    agent.run.return_value = result
    monkeypatch.setattr(client._api_server, "IntakeAgent", Mock(return_value=agent))


def _set_run(client, run_id, **fields):
    """Mutate a run's columns directly in the temp DB (characterization setup)."""
    SF = client._api_server._SessionFactory
    ORM = client._api_server.SourcingRunORM
    with SF() as session:
        run = session.get(ORM, run_id)
        for key, value in fields.items():
            setattr(run, key, value)
        session.commit()


def _create_pending(client, submission_id="sub-1", facility_id="fac-stockton",
                    summary="Pump leaking at seal — needs replacement"):
    """Create a pending_intake run via /from-maintenance; return its run id."""
    resp = client.post("/api/runs/from-maintenance", json={
        "submission_id": submission_id,
        "facility_id": facility_id,
        "submitted_by": "tech-1",
        "context": {"chat_thread_summary": summary},
    })
    assert resp.status_code == 201
    return resp.json()["run_id"]


def _mock_sourcing_pipeline(monkeypatch, sourcing_result=None, artifact=None,
                            sourcing_exc=None):
    """Mock the background SourcingAgent + SpecComparisonAgent at their source
    modules (api_server imports them function-locally inside
    _run_sourcing_background, so patching the source module is the seam)."""
    import utils.procurement_agent.agents.sourcing_agent as sa_mod
    import utils.procurement_agent.agents.spec_comparison_agent as sca_mod

    sourcing_agent = Mock()
    if sourcing_exc is not None:
        sourcing_agent.run.side_effect = sourcing_exc
    else:
        sourcing_agent.run.return_value = sourcing_result
    monkeypatch.setattr(sa_mod, "SourcingAgent", Mock(return_value=sourcing_agent))

    comp_agent = Mock()
    comp_agent.run.return_value = artifact
    monkeypatch.setattr(sca_mod, "SpecComparisonAgent", Mock(return_value=comp_agent))


def _empty_sourcing():
    return {
        "tier_1": {"results": [], "count": 0},
        "tier_2": {"results": [], "count": 0},
        "tier_3": {"results": [], "count": 0},
    }


# ---------------------------------------------------------------------------
# POST /api/runs  (create)
# ---------------------------------------------------------------------------

class TestCreateRun:
    def test_happy_path_returns_201_and_shape(self, api):
        resp = api.post("/api/runs", json={"facility_id": "fac-stockton"})
        assert resp.status_code == 201
        body = resp.json()
        assert set(body) == {"id", "phase", "created_at"}
        assert body["phase"] == "intake"
        assert isinstance(body["id"], str) and body["id"]

    def test_defaults_apply_with_empty_body(self, api):
        resp = api.post("/api/runs", json={})
        assert resp.status_code == 201
        assert resp.json()["phase"] == "intake"

    def test_validation_urgency_out_of_range_422(self, api):
        # urgency_factor has Field(ge=0.0, le=1.0)
        resp = api.post("/api/runs", json={"urgency_factor": 5.0})
        assert resp.status_code == 422
        assert "detail" in resp.json()


# ---------------------------------------------------------------------------
# GET /api/runs  (list)
# ---------------------------------------------------------------------------

class TestListRuns:
    def test_empty_returns_empty_list(self, api):
        resp = api.get("/api/runs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_lists_created_run_with_list_item_shape(self, api):
        rid = _create_run(api, facility_id="fac-modesto")
        resp = api.get("/api/runs")
        assert resp.status_code == 200
        items = resp.json()
        assert isinstance(items, list) and len(items) == 1
        item = items[0]
        assert item["id"] == rid
        assert {"id", "phase", "urgency", "warranty", "facility_id",
                "created_at", "updated_at"} <= set(item)
        assert item["phase"] == "intake"


# ---------------------------------------------------------------------------
# GET /api/runs/{id}  (detail)
# ---------------------------------------------------------------------------

class TestGetRun:
    def test_happy_path_detail_shape(self, api):
        rid = _create_run(api, facility_id="fac-stockton")
        resp = api.get(f"/api/runs/{rid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == rid
        assert body["phase"] == "intake"
        assert body["facility_state"] == "CA"   # fac-stockton mapped
        assert body["messages"] == []
        assert body["approval_history"] == []
        assert body["no_exact_match"] is False
        for key in ("id", "phase", "urgency", "warranty", "facility_id",
                    "created_at", "updated_at"):
            assert key in body

    def test_not_found_404_string_detail(self, api):
        resp = api.get("/api/runs/does-not-exist")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Run not found"}


# ---------------------------------------------------------------------------
# POST /api/runs/{id}/messages
# ---------------------------------------------------------------------------

class TestSendMessage:
    def test_happy_path_insufficient_returns_followup(self, api, monkeypatch):
        rid = _create_run(api)
        _mock_intake(api, monkeypatch, {
            "sufficient": False,
            "manufacturer_confidence": 40,
            "part_id_confidence": 40,
            "asset_specs": {"manufacturer": "Goulds"},
            "follow_up_question": "What is the model number?",
            "confidence_summary": {"proceed_state": ""},
        })
        resp = api.post(f"/api/runs/{rid}/messages", json={"content": "It's a Goulds pump"})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"run_id", "message", "updated_phase"}
        assert body["run_id"] == rid
        assert body["updated_phase"] == "intake"   # messages never auto-advances
        assert body["message"]["role"] == "agent"
        assert body["message"]["content"] == "What is the model number?"

    def test_not_found_404(self, api):
        # 404 is raised before IntakeAgent is touched, so no mock needed.
        resp = api.post("/api/runs/missing/messages", json={"content": "hi"})
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Run not found"}

    def test_validation_missing_content_422(self, api):
        # Body validation happens before the route body, so any run_id triggers 422.
        resp = api.post("/api/runs/any/messages", json={})
        assert resp.status_code == 422
        assert "detail" in resp.json()

    def test_intake_agent_failure_returns_error_response(self, api, monkeypatch):
        # An IntakeAgent failure now surfaces as a non-2xx error envelope instead
        # of a fake-200 synthetic agent reply that masked the failure as success.
        rid = _create_run(api)
        agent = Mock()
        agent.run.side_effect = RuntimeError("anthropic boom")
        monkeypatch.setattr(api._api_server, "IntakeAgent", Mock(return_value=agent))

        resp = api.post(f"/api/runs/{rid}/messages", json={"content": "hello"})
        assert resp.status_code == 502
        assert "detail" in resp.json()


# ---------------------------------------------------------------------------
# POST /api/runs/{id}/upload
# ---------------------------------------------------------------------------

class TestUpload:
    def test_happy_path_high_confidence_shape(self, api, monkeypatch):
        rid = _create_run(api)
        _mock_intake(api, monkeypatch, {
            "sufficient": True,
            "manufacturer_confidence": 90,
            "part_id_confidence": 90,
            "asset_specs": {"manufacturer": "Goulds", "model": "3196", "part_number": "ABC123"},
        })
        resp = api.post(
            f"/api/runs/{rid}/upload",
            files={"file": ("plate.jpg", b"\xff\xd8fakejpeg", "image/jpeg")},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == rid
        assert body["filename"] == "plate.jpg"
        assert body["size_bytes"] == len(b"\xff\xd8fakejpeg")
        assert body["message"]["role"] == "agent"
        assert body["extraction"] == {
            "status": "ok",
            "sufficient": True,
            "mfg_confidence": 90,
            "part_confidence": 90,
        }

    def test_not_found_404(self, api):
        # File still required to reach the route (else 422); 404 raised before read.
        resp = api.post(
            "/api/runs/missing/upload",
            files={"file": ("x.jpg", b"x", "image/jpeg")},
        )
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Run not found"}

    def test_validation_missing_file_422(self, api):
        rid = _create_run(api)
        resp = api.post(f"/api/runs/{rid}/upload")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/runs/{id}/approve
# ---------------------------------------------------------------------------

class TestApprove:
    def test_happy_path_advances_to_approved(self, api):
        rid = _create_run(api)
        resp = api.post(f"/api/runs/{rid}/approve", json={
            "approver_name": "Dana", "approver_role": "Maintenance Director",
        })
        assert resp.status_code == 200
        assert resp.json() == {"run_id": rid, "phase": "approved"}

    def test_not_found_404(self, api):
        resp = api.post("/api/runs/missing/approve", json={
            "approver_name": "Dana", "approver_role": "Maintenance Director",
        })
        assert resp.status_code == 404

    def test_validation_missing_role_422(self, api):
        resp = api.post("/api/runs/any/approve", json={"approver_name": "Dana"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/runs/{id}/reject
# ---------------------------------------------------------------------------

class TestReject:
    def test_happy_path_returns_to_comparison(self, api):
        rid = _create_run(api)
        resp = api.post(f"/api/runs/{rid}/reject", json={
            "approver_name": "Dana", "approver_role": "Maintenance Director",
            "notes": "Price too high",
        })
        assert resp.status_code == 200
        assert resp.json() == {"run_id": rid, "phase": "comparison"}

    def test_not_found_404(self, api):
        resp = api.post("/api/runs/missing/reject", json={
            "approver_name": "Dana", "approver_role": "Director", "notes": "x",
        })
        assert resp.status_code == 404

    def test_validation_missing_notes_422(self, api):
        # notes is required on RejectRequest (unlike ApproveRequest).
        resp = api.post("/api/runs/any/reject", json={
            "approver_name": "Dana", "approver_role": "Director",
        })
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/runs/{id}/outreach
# ---------------------------------------------------------------------------

class TestOutreach:
    def test_happy_path_records_sent_timestamps(self, api):
        rid = _create_run(api)
        resp = api.post(f"/api/runs/{rid}/outreach", json={
            "candidate_ids": ["VendorA-t3-0", "VendorB-t3-1"],
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == rid
        assert body["candidates_contacted"] == 2
        assert set(body["tier3_outreach_sent"]) == {"VendorA-t3-0", "VendorB-t3-1"}
        assert "sent_at" in body

    def test_not_found_404(self, api):
        resp = api.post("/api/runs/missing/outreach", json={"candidate_ids": ["x"]})
        assert resp.status_code == 404

    def test_validation_missing_candidate_ids_422(self, api):
        resp = api.post("/api/runs/any/outreach", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Static / trivial endpoints (cheap coverage of the simple contract)
# ---------------------------------------------------------------------------

class TestStaticEndpoints:
    def test_health(self, api):
        resp = api.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "version": "1.0.0-phase1"}

    def test_facilities_shape(self, api):
        resp = api.get("/api/facilities")
        assert resp.status_code == 200
        facilities = resp.json()
        assert len(facilities) == 5
        assert {"id", "name", "state"} == set(facilities[0])

    def test_approval_rules_returns_four_rules(self, api):
        resp = api.get("/api/approval-rules/fac-stockton")
        assert resp.status_code == 200
        rules = resp.json()
        assert len(rules) == 4
        assert {"id", "facility_id", "threshold", "cap", "approvers_required",
                "approver_roles", "applies_to"} == set(rules[0])


# ---------------------------------------------------------------------------
# POST /api/runs/{id}/confirm-intake   (BackgroundTask: SourcingAgent + Comparison)
# ---------------------------------------------------------------------------

class TestConfirmIntake:
    def test_not_found_404_string_detail(self, api):
        resp = api.post("/api/runs/missing/confirm-intake")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Run not found"}

    def test_wrong_phase_409_with_DICT_detail(self, api):
        # INCONSISTENCY: 409 detail is a dict, not a string (refactor target).
        rid = _create_run(api)
        _set_run(api, rid, current_phase="approved")
        resp = api.post(f"/api/runs/{rid}/confirm-intake")
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert isinstance(detail, dict)
        assert detail["current_phase"] == "approved"
        assert detail["message"] == "Run is not in intake phase"

    def test_no_specs_422_with_STRING_detail(self, api):
        # INCONSISTENCY: 422 detail is a string here (vs dict for 409 above).
        rid = _create_run(api)  # intake, but no asset_specs captured
        resp = api.post(f"/api/runs/{rid}/confirm-intake")
        assert resp.status_code == 422
        assert resp.json()["detail"] == \
            "No asset specs captured yet — complete intake chat first"

    def test_happy_sync_response_then_advances_to_comparison(self, api, monkeypatch):
        rid = _create_run(api)
        _set_run(api, rid, asset_specs_json=json.dumps({"manufacturer": "Goulds"}))
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=_empty_sourcing())

        # (i) Synchronous response: phase reported as "sourcing".
        resp = api.post(f"/api/runs/{rid}/confirm-intake")
        assert resp.status_code == 200
        assert resp.json() == {"run_id": rid, "phase": "sourcing"}

        # (ii) Post-background state: phase advanced to "comparison", inventory stub
        # written, sourcing_results present. (TestClient runs the BackgroundTask
        # synchronously before returning from the POST above.)
        detail = api.get(f"/api/runs/{rid}").json()
        assert detail["phase"] == "comparison"
        assert detail["inventory_result"] == {
            "status": "no_data",
            "message": "Inventory agent not yet connected (Phase 5).",
        }
        assert detail["sourcing_results"] is not None
        assert detail["sourcing_results"]["tier1"] == []

    def test_happy_attaches_comparison_artifact_to_candidate(self, api, monkeypatch):
        rid = _create_run(api)
        _set_run(api, rid, asset_specs_json=json.dumps({"manufacturer": "Goulds"}))
        sourcing = _empty_sourcing()
        sourcing["tier_1"]["results"] = [{
            "vendor_name": "Acme", "base_price": 100, "lead_time_days": 3,
            "suitability_score": 80, "confidence_score": 70,
        }]
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=sourcing,
                                artifact={"fidelity": "high", "compatibility_summary": "confirmed",
                                          "comparison": [{"field": "seal", "field_label": "Seal type",
                                                          "asset_value": "Type 21", "candidate_value": "Type 21",
                                                          "match": "exact"}],
                                          "verification_required_fields": [], "engineer_notes": "ok"})

        api.post(f"/api/runs/{rid}/confirm-intake")
        detail = api.get(f"/api/runs/{rid}").json()
        assert detail["phase"] == "comparison"
        tier1 = detail["sourcing_results"]["tier1"]
        assert len(tier1) == 1
        # SpecComparisonAgent artifact is camelCased for the frontend (was raw before).
        art = tier1[0]["comparisonArtifact"]
        assert art["fidelity"] == "high" and art["engineerNotes"] == "ok"
        assert art["comparison"][0]["fieldLabel"] == "Seal type"
        assert art["comparison"][0]["candidateValue"] == "Type 21"

    def test_exact_only_filters_aftermarket_from_tier2(self, api, monkeypatch):
        # The no-spec-sheet honesty branch: confirm-intake?exact_only=true -> background
        # sourcing drops aftermarket/equivalent Tier 2 candidates, keeps exact OEM.
        rid = _create_run(api)
        _set_run(api, rid, asset_specs_json=json.dumps({"manufacturer": "Goulds", "part_number": "3196"}))
        sourcing = _empty_sourcing()
        sourcing["tier_2"]["results"] = [
            {"vendor_name": "Exact Co", "match_type": "Exact OEM", "base_price": 100,
             "suitability_score": 80, "confidence_score": 70},
            {"vendor_name": "Aftermarket Co", "match_type": "Aftermarket Compatible", "base_price": 80,
             "suitability_score": 70, "confidence_score": 60},
        ]
        sourcing["tier_2"]["count"] = 2
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=sourcing, artifact=None)

        api.post(f"/api/runs/{rid}/confirm-intake?exact_only=true")
        tier2 = api.get(f"/api/runs/{rid}").json()["sourcing_results"]["tier2"]
        names = [c["vendorName"] for c in tier2]
        assert names == ["Exact Co"]   # aftermarket dropped

    def test_default_keeps_equivalents(self, api, monkeypatch):
        rid = _create_run(api)
        _set_run(api, rid, asset_specs_json=json.dumps({"manufacturer": "Goulds", "part_number": "3196"}))
        sourcing = _empty_sourcing()
        sourcing["tier_2"]["results"] = [
            {"vendor_name": "Exact Co", "match_type": "Exact OEM", "base_price": 100,
             "suitability_score": 80, "confidence_score": 70},
            {"vendor_name": "Aftermarket Co", "match_type": "Aftermarket Compatible", "base_price": 80,
             "suitability_score": 70, "confidence_score": 60},
        ]
        sourcing["tier_2"]["count"] = 2
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=sourcing, artifact=None)

        api.post(f"/api/runs/{rid}/confirm-intake")  # no exact_only -> equivalents kept
        tier2 = api.get(f"/api/runs/{rid}").json()["sourcing_results"]["tier2"]
        assert len(tier2) == 2

    def test_sourcing_failure_advances_to_error_phase(self, api, monkeypatch):
        # A background SourcingAgent failure advances the run to phase="error"
        # (the React frontend renders this state and stops polling) rather than
        # stranding it at "sourcing" forever. The sync response is unchanged
        # (200 {phase:"sourcing"}); the failure surfaces via the polled phase.
        # Error detail is retained in sourcing_results.error for debugging.
        rid = _create_run(api)
        _set_run(api, rid, asset_specs_json=json.dumps({"manufacturer": "Goulds"}))
        _mock_sourcing_pipeline(monkeypatch, sourcing_exc=RuntimeError("tavily boom"))

        resp = api.post(f"/api/runs/{rid}/confirm-intake")
        assert resp.status_code == 200          # sync response unchanged
        assert resp.json()["phase"] == "sourcing"

        detail = api.get(f"/api/runs/{rid}").json()
        assert detail["phase"] == "error"        # advanced to error, not stuck
        assert "error" in detail["sourcing_results"]


# ---------------------------------------------------------------------------
# POST /api/runs/{id}/request-confirmation   (BackgroundTask: mock vendor reply)
# ---------------------------------------------------------------------------

class TestRequestConfirmation:
    def test_not_found_404(self, api):
        resp = api.post("/api/runs/missing/request-confirmation",
                        json={"candidate_ids": ["x"]})
        assert resp.status_code == 404

    def test_validation_missing_candidate_ids_422(self, api):
        resp = api.post("/api/runs/any/request-confirmation", json={})
        assert resp.status_code == 422

    def test_happy_sync_response_echoes_delay_range(self, api, monkeypatch):
        # Neutralize the 3-8s sleep so the BackgroundTask is instant.
        monkeypatch.setattr(api._api_server, "_MOCK_CONFIRMATION_DELAY_RANGE", (0, 0))
        rid = _create_run(api)
        raw = _empty_sourcing()
        raw["tier_1"]["results"] = [{"vendor_name": "Acme", "confirmation_needed": True}]
        _set_run(api, rid, sourcing_results_json=json.dumps(raw))
        resp = api.post(f"/api/runs/{rid}/request-confirmation",
                        json={"candidate_ids": ["Acme-t1-0"]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == rid
        assert body["candidates"] == ["Acme-t1-0"]
        # The field echoes the configured delay range (default in prod is "3-8 seconds").
        assert body["mock_response_in"] == "0-0 seconds"

    def test_post_background_flips_confirmation_pending(self, api, monkeypatch):
        monkeypatch.setattr(api._api_server, "_MOCK_CONFIRMATION_DELAY_RANGE", (0, 0))
        rid = _create_run(api)
        raw = _empty_sourcing()
        raw["tier_1"]["results"] = [{"vendor_name": "Acme", "confirmation_needed": True}]
        _set_run(api, rid, sourcing_results_json=json.dumps(raw))

        before = api.get(f"/api/runs/{rid}").json()
        assert before["sourcing_results"]["tier1"][0]["confirmationPending"] is True

        resp = api.post(f"/api/runs/{rid}/request-confirmation",
                        json={"candidate_ids": ["Acme-t1-0"]})
        assert resp.status_code == 200

        after = api.get(f"/api/runs/{rid}").json()
        assert after["sourcing_results"]["tier1"][0]["confirmationPending"] is False

    def test_unmatched_candidate_returns_404(self, api, monkeypatch):
        # An id that matches no Tier 1 candidate is now rejected synchronously
        # (404) so the caller can distinguish a real confirmation from a no-op,
        # rather than getting 200 for work that never happens.
        monkeypatch.setattr(api._api_server, "_MOCK_CONFIRMATION_DELAY_RANGE", (0, 0))
        rid = _create_run(api)
        raw = _empty_sourcing()
        raw["tier_1"]["results"] = [{"vendor_name": "Acme", "confirmation_needed": True}]
        _set_run(api, rid, sourcing_results_json=json.dumps(raw))

        resp = api.post(f"/api/runs/{rid}/request-confirmation",
                        json={"candidate_ids": ["Ghost-t1-0"]})
        assert resp.status_code == 404
        # Run is untouched.
        after = api.get(f"/api/runs/{rid}").json()
        assert after["sourcing_results"]["tier1"][0]["confirmationPending"] is True

    def test_no_sourcing_results_returns_404(self, api, monkeypatch):
        # A run with no Tier 1 candidates at all has nothing to confirm → 404.
        monkeypatch.setattr(api._api_server, "_MOCK_CONFIRMATION_DELAY_RANGE", (0, 0))
        rid = _create_run(api)
        resp = api.post(f"/api/runs/{rid}/request-confirmation",
                        json={"candidate_ids": ["Acme-t1-0"]})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Plain DB endpoints (no background work)
# ---------------------------------------------------------------------------

class TestSelectCandidate:
    def test_happy_advances_to_pending_first_approval(self, api):
        rid = _create_run(api)
        resp = api.post(f"/api/runs/{rid}/select-candidate",
                        json={"candidate_id": "Acme-t1-0", "tier": 1})
        assert resp.status_code == 200
        assert resp.json() == {"run_id": rid, "phase": "pending_first_approval"}

    def test_not_found_404(self, api):
        resp = api.post("/api/runs/missing/select-candidate",
                        json={"candidate_id": "x", "tier": 1})
        assert resp.status_code == 404

    def test_validation_missing_tier_422(self, api):
        resp = api.post("/api/runs/any/select-candidate", json={"candidate_id": "x"})
        assert resp.status_code == 422


class TestFromMaintenance:
    def test_happy_creates_pending_intake(self, api):
        resp = api.post("/api/runs/from-maintenance", json={
            "submission_id": "sub-9", "facility_id": "fac-modesto",
            "submitted_by": "tech", "context": {"chat_thread_summary": "Bearing noise"},
        })
        assert resp.status_code == 201
        body = resp.json()
        assert set(body) == {"run_id", "phase", "maintenance_submission_id"}
        assert body["phase"] == "pending_intake"
        assert body["maintenance_submission_id"] == "sub-9"

    def test_validation_missing_context_422(self, api):
        resp = api.post("/api/runs/from-maintenance", json={
            "submission_id": "s", "facility_id": "f", "submitted_by": "t",
        })
        assert resp.status_code == 422


class TestOpenFromPending:
    def test_happy_transitions_to_intake_and_seeds_summary(self, api):
        rid = _create_pending(api, summary="Seal is leaking")
        resp = api.post(f"/api/runs/{rid}/open-from-pending")
        assert resp.status_code == 200
        assert resp.json() == {"run_id": rid, "phase": "intake"}
        # Summary seeded as an agent chat message.
        detail = api.get(f"/api/runs/{rid}").json()
        assert detail["phase"] == "intake"
        assert any(m["role"] == "agent" and m["content"] == "Seal is leaking"
                   for m in detail["messages"])

    def test_not_found_404(self, api):
        resp = api.post("/api/runs/missing/open-from-pending")
        assert resp.status_code == 404

    def test_wrong_phase_409_dict_detail(self, api):
        rid = _create_run(api)  # phase=intake, not pending_intake
        resp = api.post(f"/api/runs/{rid}/open-from-pending")
        assert resp.status_code == 409
        assert isinstance(resp.json()["detail"], dict)
        assert resp.json()["detail"]["current_phase"] == "intake"


class TestKnownPartsCacheFirst:
    """Increment: a previously-seen part reads its remembered supplier set from
    known_parts (deterministic) instead of re-rolling variable discovery."""

    def test_second_run_returns_cached_suppliers_and_skips_discovery(self, api, monkeypatch, tmp_path):
        from utils import known_parts
        monkeypatch.setattr(known_parts, "_DB_PATH", str(tmp_path / "kp.json"))
        specs = json.dumps({"manufacturer": "Gusher Pumps", "part_number": "84004-28-C238CBC"})

        # Run 1 — discovery yields a candidate set; written back to known_parts.
        rid1 = _create_run(api)
        _set_run(api, rid1, asset_specs_json=specs)
        s1 = _empty_sourcing()
        s1["tier_2"]["results"] = [{
            "vendor_name": "Seal It 123", "base_price": 53.25,
            "source_url": "https://sealit123.com/x", "suitability_score": 75,
            "match_type": "Exact OEM",
        }]
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=s1, artifact=None)
        assert api.post(f"/api/runs/{rid1}/confirm-intake").status_code == 200

        pk = known_parts.canonical_part_key("Gusher Pumps", "84004-28-C238CBC")
        assert any(e["supplier_id"] == "sealit123.com" for e in known_parts.get_edges(pk))

        # Run 2 — same part. Discovery (if it ran) would now return a DIFFERENT set;
        # cache-first must return the remembered suppliers and skip discovery.
        rid2 = _create_run(api)
        _set_run(api, rid2, asset_specs_json=specs)
        s2 = _empty_sourcing()
        s2["tier_2"]["results"] = [{
            "vendor_name": "Different Vendor", "base_price": 99.0,
            "source_url": "https://othersite.com/x", "suitability_score": 60,
        }]
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=s2, artifact=None)
        assert api.post(f"/api/runs/{rid2}/confirm-intake").status_code == 200

        detail = api.get(f"/api/runs/{rid2}").json()
        vendors = [c["vendorName"] for c in detail["sourcing_results"]["tier2"]]
        assert "Seal It 123" in vendors          # the cached set was returned
        assert "Different Vendor" not in vendors  # discovery was skipped


class TestRejectSubmission:
    def test_happy_transitions_to_cancelled(self, api):
        rid = _create_pending(api, submission_id="sub-rej")
        resp = api.post(f"/api/runs/{rid}/reject-submission")
        assert resp.status_code == 200
        assert resp.json() == {"run_id": rid, "phase": "cancelled"}

    def test_not_found_404(self, api):
        resp = api.post("/api/runs/missing/reject-submission")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Candidate transform — evidence state + verifiable-data fields (increment 1).
# The frontend's "why"/claims branch on these; the strength of a claim must
# match the strength of the evidence on the candidate.
# ---------------------------------------------------------------------------

class TestTransformOptionEvidenceState:
    def test_uncontacted_when_price_hidden(self):
        # The live All Seals case: a Tavily-discovered Tier 3 row with pn_match present
        # but NO price/quote. evidenceState must be "uncontacted" so the UI makes no
        # part-match claim — a discovery PN match is not contact/quote evidence.
        from api_server import _transform_option
        out = _transform_option({"vendor_name": "All Seals Inc.", "price_tbd": True,
                                 "requires_rfq": True, "pn_match_status": "exact_match",
                                 "match_type": "Exact OEM", "found_part_number": "84004-28"}, 3, 0)
        assert out["price"] is None
        assert out["evidenceState"] == "uncontacted"

    def test_priced_passes_found_pn_and_url(self):
        from api_server import _transform_option
        out = _transform_option({"vendor_name": "sealit123", "base_price": 53.25,
                                 "source_url": "https://sealit123.com/x",
                                 "found_part_number": "84004-28-C238CBC"}, 2, 0)
        assert out["price"] == 53.25 and out["evidenceState"] == "priced"
        assert out["foundPartNumber"] == "84004-28-C238CBC"   # surfaced so the claim is verifiable
        assert out["url"] == "https://sealit123.com/x"        # source link present

    def test_artifact_camelcased_and_stock_wired(self):
        from api_server import _transform_option
        out = _transform_option({"vendor_name": "v", "base_price": 10.0, "in_stock": True,
            "comparison_artifact": {"fidelity": "low", "compatibility_summary": "verification_required",
                "comparison": [{"field": "seal", "field_label": "Seal type", "asset_value": "Type 21",
                                "candidate_value": None, "match": "unknown"}],
                "verification_required_fields": ["seal"], "engineer_notes": "spec sheet required"}}, 2, 0)
        assert out["stock"] == "In stock"
        art = out["comparisonArtifact"]
        assert art["engineerNotes"] == "spec sheet required"   # was a dead snake_case read before
        assert art["comparison"][0]["fieldLabel"] == "Seal type"
        assert art["comparison"][0]["assetValue"] == "Type 21"

    def test_stock_none_when_not_reported(self):
        from api_server import _transform_option
        assert _transform_option({"vendor_name": "v", "base_price": 1.0}, 2, 0)["stock"] is None

    def test_purchase_channel_marketplace_requires_price_and_registered_domain(self):
        # State M = a buyable price at a registered marketplace. Anything else = reference.
        from api_server import _transform_option
        m = _transform_option({"vendor_name": "Seal It 123", "base_price": 53.25,
                               "source_url": "https://sealit123.com/x"}, 2, 0)
        assert m["price"] == 53.25 and m["purchaseChannel"] == "marketplace"
        # Priced but NOT a registered marketplace -> reference.
        ref = _transform_option({"vendor_name": "Industrial Pump Parts", "base_price": 173.0,
                                 "source_url": "https://industrialpumpparts.com/x"}, 3, 0)
        assert ref["purchaseChannel"] == "reference"
        # Registered marketplace but no buyable price (RFQ/price_tbd) -> NOT marketplace.
        nob = _transform_option({"vendor_name": "Seal It 123", "price_tbd": True,
                                 "requires_rfq": True, "source_url": "https://sealit123.com/x"}, 3, 0)
        assert nob["price"] is None and nob["purchaseChannel"] == "reference"
        # Uncontacted (no price, no url) -> reference (State M requires a price).
        unc = _transform_option({"vendor_name": "Phoenix Pumps", "price_tbd": True}, 3, 0)
        assert unc["purchaseChannel"] == "reference"

    def test_price_unverified_below_confidence_floor(self):
        # Live case: Industrial Pump Parts $173 @ conf 28% — a low-confidence extracted
        # price must be flagged unverified (kept, not suppressed, not fabricated).
        from api_server import _transform_option, _PRICE_CONFIDENCE_FLOOR
        assert _PRICE_CONFIDENCE_FLOOR == 40.0
        low = _transform_option({"vendor_name": "Industrial Pump Parts", "base_price": 173.0,
                                 "confidence_score": 28.0}, 3, 0)
        assert low["price"] == 173.0 and low["priceUnverified"] is True
        high = _transform_option({"vendor_name": "sealit123", "base_price": 53.25,
                                  "confidence_score": 75.0}, 2, 0)
        assert high["priceUnverified"] is False
        # A 0/absent confidence score is "no signal" (e.g. the Tier 2 lane), NOT low
        # confidence — must NOT be flagged (otherwise every priced row cries wolf).
        zero = _transform_option({"vendor_name": "sealit123", "base_price": 53.25,
                                  "confidence_score": 0.0}, 2, 0)
        assert zero["priceUnverified"] is False
        # No price → nothing to qualify as unverified.
        nohp = _transform_option({"vendor_name": "v", "price_tbd": True,
                                  "confidence_score": 10.0}, 3, 0)
        assert nohp["priceUnverified"] is False

    def test_wrong_phase_409_dict_detail(self, api):
        rid = _create_run(api)  # intake, not pending_intake
        resp = api.post(f"/api/runs/{rid}/reject-submission")
        assert resp.status_code == 409
        assert isinstance(resp.json()["detail"], dict)


class TestSaveOutreach:
    def test_happy_persists_selection_keeps_phase(self, api):
        rid = _create_run(api)
        resp = api.post(f"/api/runs/{rid}/save-outreach",
                        json={"candidate_ids": ["a-t3-0", "b-t3-1"]})
        assert resp.status_code == 200
        assert resp.json() == {"run_id": rid, "saved_count": 2, "phase": "intake"}

    def test_not_found_404(self, api):
        resp = api.post("/api/runs/missing/save-outreach",
                        json={"candidate_ids": ["x"]})
        assert resp.status_code == 404

    def test_validation_missing_candidate_ids_422(self, api):
        resp = api.post("/api/runs/any/save-outreach", json={})
        assert resp.status_code == 422
