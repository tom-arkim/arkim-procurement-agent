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

    # Isolate supplier_registry too — the run-detail path (State C 3b) now reads it for
    # confirmed quotes, so point its store at the temp dir (seed still runs into tmp).
    from utils import supplier_registry
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))

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


def _read_selected(client, run_id) -> dict:
    """Read the raw selected_candidate_json (incl. _approval_path) straight from the
    temp DB — RunDetail re-serializes it, so read the column to assert what was stored."""
    SF = client._api_server._SessionFactory
    ORM = client._api_server.SourcingRunORM
    with SF() as session:
        run = session.get(ORM, run_id)
        return json.loads(run.selected_candidate_json) if run.selected_candidate_json else {}


def _read_history(client, run_id) -> list:
    """Read the raw approval_history_json rows (incl. approver_id) from the temp DB."""
    SF = client._api_server._SessionFactory
    ORM = client._api_server.SourcingRunORM
    with SF() as session:
        run = session.get(ORM, run_id)
        return json.loads(run.approval_history_json) if run.approval_history_json else []


def _read_run_company(client, run_id):
    """Read the run's company_id column straight from the temp DB."""
    SF = client._api_server._SessionFactory
    ORM = client._api_server.SourcingRunORM
    with SF() as session:
        run = session.get(ORM, run_id)
        return run.company_id if run else "<<missing>>"


def _mk_caller(user_id, company="PIN1"):
    """A minimal authenticated Caller for injecting identity via dependency_overrides
    (no JWT/JWKS needed to exercise the endpoint's M1 behaviour)."""
    from utils.auth import Caller, parse_cognito_user_from_claims
    return Caller(
        user_id=user_id, email=f"{user_id}@x.com", company_id=company,
        roles=frozenset(), is_admin=False, service_authenticated=False,
        service_name=None, cognito_user=parse_cognito_user_from_claims({"sub": user_id}),
    )


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
# H1 — threshold-driven approval routing (auth-independent half).
# select-candidate persists the _approval_path from determine_approval_path; the
# approve endpoint routes a >= 2-approver purchase through pending_second_approval
# instead of jumping straight to approved. Mirrors the Orchestrator's logic.
# (Distinct-approver enforcement — M1 — is the auth-dependent half, NOT here.)
# ---------------------------------------------------------------------------

class TestApprovalThresholdRouting:
    def _select(self, api, price):
        """Create a comparison-phase run with one priced candidate and select it."""
        rid = _create_run(api)
        raw = {"tier_2": {"results": [
            {"vendor_name": "Acme", "base_price": price, "source_url": "https://acme.com/x"}]}}
        _set_run(api, rid, sourcing_results_json=json.dumps(raw), current_phase="comparison")
        resp = api.post(f"/api/runs/{rid}/select-candidate",
                        json={"candidate_id": "Acme-t2-0", "tier": 2})
        assert resp.status_code == 200
        return rid

    def test_select_persists_single_approver_path_below_threshold(self, api):
        rid = self._select(api, 1000.0)
        path = _read_selected(api, rid)["_approval_path"]
        assert path["approvers_required"] == 1
        assert path["grand_total_usd"] == 1000.0

    def test_select_persists_dual_approver_path_at_threshold(self, api):
        rid = self._select(api, 6000.0)  # >= $5k default rule -> 2 approvers
        assert _read_selected(api, rid)["_approval_path"]["approvers_required"] == 2

    def test_single_approver_one_approval_reaches_approved(self, api):
        rid = self._select(api, 1000.0)
        resp = api.post(f"/api/runs/{rid}/approve",
                        json={"approver_name": "A", "approver_role": "Director"})
        assert resp.json() == {"run_id": rid, "phase": "approved"}

    def test_dual_approver_routes_through_second_then_approved(self, api):
        rid = self._select(api, 6000.0)
        r1 = api.post(f"/api/runs/{rid}/approve",
                      json={"approver_name": "A", "approver_role": "Director"})
        assert r1.json() == {"run_id": rid, "phase": "pending_second_approval"}
        r2 = api.post(f"/api/runs/{rid}/approve",
                      json={"approver_name": "B", "approver_role": "Ops Manager"})
        assert r2.json() == {"run_id": rid, "phase": "approved"}

    def test_price_tbd_candidate_routes_single_approver(self, api):
        # A quote-required (price-hidden) selection has no known spend -> $0 -> 1 approver.
        rid = _create_run(api)
        raw = {"tier_3": {"results": [
            {"vendor_name": "RFQ Co", "price_tbd": True, "requires_rfq": True,
             "source_url": "https://rfqco.com/x"}]}}
        _set_run(api, rid, sourcing_results_json=json.dumps(raw), current_phase="comparison")
        api.post(f"/api/runs/{rid}/select-candidate",
                 json={"candidate_id": "RFQ Co-t3-0", "tier": 3})
        assert _read_selected(api, rid)["_approval_path"]["approvers_required"] == 1


# ---------------------------------------------------------------------------
# D2 prereq #1 — the tenant key (company PIN) is stamped on a run from the VERIFIED
# Caller (never the body), and stays NULL in the no-auth demo. Keys only — no scoping.
# ---------------------------------------------------------------------------

class TestTenantKeyOnRuns:
    def _override(self, api, company):
        from utils.auth import get_caller
        api._api_server.app.dependency_overrides[get_caller] = lambda: _mk_caller("u", company)

    def _clear(self, api):
        from utils.auth import get_caller
        api._api_server.app.dependency_overrides.pop(get_caller, None)

    def test_manual_create_no_identity_company_null(self, api):
        # The current demo: no token -> get_caller None -> company_id NULL (unchanged).
        assert _read_run_company(api, _create_run(api)) is None

    def test_manual_create_with_caller_sets_company(self, api):
        self._override(api, "PIN1")
        try:
            assert _read_run_company(api, _create_run(api)) == "PIN1"
        finally:
            self._clear(api)

    def test_from_maintenance_no_header_company_null(self, api):
        assert _read_run_company(api, _create_pending(api, submission_id="t-d2-1")) is None

    def test_from_maintenance_with_caller_sets_company(self, api):
        self._override(api, "PIN2")
        try:
            assert _read_run_company(api, _create_pending(api, submission_id="t-d2-2")) == "PIN2"
        finally:
            self._clear(api)


# ---------------------------------------------------------------------------
# M1 — distinct-approver enforcement on the AUTHENTICATED identity (never the body).
# Wired via the optional get_caller dependency: real when a verified Caller is present
# (injected here through dependency_overrides), inert in the no-token demo path.
# ---------------------------------------------------------------------------

class TestDistinctApprover:
    def _dual(self, api):
        """Seed a >= $5k selected run sitting at pending_first_approval (2 approvers)."""
        rid = _create_run(api)
        raw = {"tier_2": {"results": [
            {"vendor_name": "Acme", "base_price": 6000.0, "source_url": "https://acme.com/x"}]}}
        _set_run(api, rid, sourcing_results_json=json.dumps(raw), current_phase="comparison")
        assert api.post(f"/api/runs/{rid}/select-candidate",
                        json={"candidate_id": "Acme-t2-0", "tier": 2}).status_code == 200
        return rid

    def _override(self, api, user_id):
        from utils.auth import get_caller
        api._api_server.app.dependency_overrides[get_caller] = lambda: _mk_caller(user_id)

    def _clear(self, api):
        from utils.auth import get_caller
        api._api_server.app.dependency_overrides.pop(get_caller, None)

    def test_same_identity_blocked_on_second_approval(self, api):
        rid = self._dual(api)
        self._override(api, "alice")
        try:
            r1 = api.post(f"/api/runs/{rid}/approve",
                          json={"approver_name": "Alice", "approver_role": "Director"})
            assert r1.json()["phase"] == "pending_second_approval"
            # Same authenticated sub tries the second approval -> rejected.
            r2 = api.post(f"/api/runs/{rid}/approve",
                          json={"approver_name": "Alice", "approver_role": "Director"})
            assert r2.status_code == 409
        finally:
            self._clear(api)

    def test_distinct_identities_reach_approved(self, api):
        rid = self._dual(api)
        try:
            self._override(api, "alice")
            r1 = api.post(f"/api/runs/{rid}/approve",
                          json={"approver_name": "Alice", "approver_role": "Director"})
            assert r1.json()["phase"] == "pending_second_approval"
            self._override(api, "bob")  # a DISTINCT authenticated approver
            r2 = api.post(f"/api/runs/{rid}/approve",
                          json={"approver_name": "Bob", "approver_role": "Ops Manager"})
            assert r2.json()["phase"] == "approved"
        finally:
            self._clear(api)

    def test_authenticated_sub_persisted_as_approver_id(self, api):
        rid = self._dual(api)
        self._override(api, "alice")
        try:
            api.post(f"/api/runs/{rid}/approve",
                     json={"approver_name": "Alice", "approver_role": "Director"})
            assert _read_history(api, rid)[0]["approver_id"] == "alice"
        finally:
            self._clear(api)

    def test_no_token_demo_distinctness_not_enforced(self, api):
        # No override -> get_caller returns None (no token). Demo path is unchanged: two
        # approvals advance the run, approver_id is null, no distinctness check fires.
        rid = self._dual(api)
        r1 = api.post(f"/api/runs/{rid}/approve", json={"approver_name": "X", "approver_role": "D"})
        assert r1.json()["phase"] == "pending_second_approval"
        r2 = api.post(f"/api/runs/{rid}/approve", json={"approver_name": "X", "approver_role": "D"})
        assert r2.json()["phase"] == "approved"
        assert _read_history(api, rid)[0]["approver_id"] is None


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

    def test_approval_rules_default_buy_tiers(self, api):
        # No custom rules persisted yet -> the GET surfaces the DEFAULT_RULES buy tiers
        # (the same source determine_approval_path falls back to), ascending, with the
        # display-only derived cap and empty ids (not-yet-persisted).
        resp = api.get("/api/approval-rules/fac-stockton")
        assert resp.status_code == 200
        rules = resp.json()
        assert len(rules) == 3
        assert {"id", "facility_id", "threshold", "cap", "approvers_required",
                "approver_roles", "applies_to"} == set(rules[0])
        thresholds = [r["threshold"] for r in rules]
        assert thresholds == sorted(thresholds)          # ascending
        assert thresholds[0] == 0
        assert all(r["applies_to"] == "buy" for r in rules)
        assert all(r["id"] == "" for r in rules)         # fallback => not persisted
        assert rules[0]["cap"] == rules[1]["threshold"] - 1   # cap derived from next tier
        assert rules[-1]["cap"] is None                  # top tier uncapped


class TestApprovalRulesPersistence:
    """The approval-rules endpoints persist to the approval_rules table and drive routing
    (they are no longer Phase-1 stubs that echo)."""

    def test_post_persists_and_get_reflects(self, api):
        body = {"facility_id": "fac-x", "threshold": 1000, "approvers_required": 2,
                "approver_roles": ["ops"], "applies_to": "buy"}
        resp = api.post("/api/approval-rules", json=body)
        assert resp.status_code == 201
        created = resp.json()
        assert created["id"]                              # a real persisted id, not ""
        assert created["threshold"] == 1000
        assert created["approvers_required"] == 2

        rules = api.get("/api/approval-rules/fac-x").json()
        assert [r["threshold"] for r in rules] == [1000]  # the persisted rule, not defaults
        assert rules[0]["id"] == created["id"]

    def test_post_updates_in_place_by_id(self, api):
        created = api.post("/api/approval-rules", json={
            "facility_id": "fac-y", "threshold": 2000, "approvers_required": 1,
            "approver_roles": [], "applies_to": "buy"}).json()
        # Re-POST with the id -> update in place (no duplicate row).
        updated = api.post("/api/approval-rules", json={
            "id": created["id"], "facility_id": "fac-y", "threshold": 2000,
            "approvers_required": 3, "approver_roles": [], "applies_to": "buy"}).json()
        assert updated["id"] == created["id"]
        rules = api.get("/api/approval-rules/fac-y").json()
        assert len(rules) == 1
        assert rules[0]["approvers_required"] == 3

    def test_post_validation(self, api):
        base = {"facility_id": "fac-z", "approvers_required": 1, "approver_roles": [],
                "applies_to": "buy"}
        assert api.post("/api/approval-rules", json={**base, "threshold": -1}).status_code == 422
        assert api.post("/api/approval-rules",
                        json={**base, "threshold": 0, "approvers_required": -1}).status_code == 422
        assert api.post("/api/approval-rules",
                        json={**base, "threshold": 0, "applies_to": "outreach"}).status_code == 422

    def test_persisted_rules_drive_routing(self, api):
        # The same function order placement calls reads what the editor saves.
        from utils.procurement_agent.state.approval_rules import determine_approval_path
        fac = "fac-routing"
        api.post("/api/approval-rules", json={
            "facility_id": fac, "threshold": 0, "approvers_required": 1,
            "approver_roles": ["a"], "applies_to": "buy"})
        api.post("/api/approval-rules", json={
            "facility_id": fac, "threshold": 500, "approvers_required": 2,
            "approver_roles": ["a", "b"], "applies_to": "buy"})
        # A $600 order now matches the $500 tier -> 2 approvers (the saved rule, live).
        approvers, _roles = determine_approval_path(fac, 600)
        assert approvers == 2
        # A $100 order falls to the $0 tier -> 1 approver.
        assert determine_approval_path(fac, 100)[0] == 1


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


# ---------------------------------------------------------------------------
# State C (increment 3b): a human-CONFIRMED quote overlays its candidate with the
# strongest "supplier-confirmed" claim. The join is the deterministic thread key
# (3a) with a domain fallback for legacy/out-of-thread quotes; the quote's
# price/lead/terms override the listing, and the extraction-quality honesty
# (quoteUnverified, 0–1 scale) COMPOSES — "supplier-confirmed" never masks a shaky
# extraction. Only confirmed quotes drive it; unmatched replies are never joined.
# ---------------------------------------------------------------------------

class TestStateCQuoteOverlay:
    def _quote(self, *, domain, thread_id=None, unit_price=120.0, confidence=0.9,
               status="confirmed", lead_time="2 weeks", terms="Net 30"):
        return {"kind": "quote", "status": status, "supplier_domain": domain,
                "thread_id": thread_id, "sent_message_id": "sm-1", "message_id": "m-1",
                "confidence": confidence,
                "payload": {"unit_price": unit_price, "currency": "USD",
                            "lead_time": lead_time, "terms": terms, "confidence": confidence}}

    def _sent(self, *, domain, thread_id):
        return {"supplier_domain": domain, "thread_id": thread_id, "id": "sm-1"}

    def _raw(self, *candidates):
        return {"tier_3": {"results": list(candidates)}}

    def test_thread_join_overlays_correct_candidate_only(self):
        # Two outbounds (two domains, same run); one confirmed quote on thread B ->
        # overlays candidate B ONLY; candidate A is untouched.
        from api_server import _index_quotes, _transform_sourcing_results
        sent = [self._sent(domain="aco.com", thread_id="tA"),
                self._sent(domain="bco.com", thread_id="tB")]
        index = _index_quotes([self._quote(domain="bco.com", thread_id="tB", unit_price=200.0)], sent)
        raw = self._raw(
            {"vendor_name": "A Co", "base_price": 50.0, "source_url": "https://aco.com/x"},
            {"vendor_name": "B Co", "base_price": 75.0, "source_url": "https://bco.com/x"},
        )
        a, b = _transform_sourcing_results(raw, index)["tier3"]
        assert a["vendorName"] == "A Co" and a["evidenceState"] == "priced"
        assert "quoteConfirmed" not in a                       # not crossed onto A
        assert b["evidenceState"] == "quoted" and b["quoteConfirmed"] is True
        assert b["price"] == 200.0                             # quote overrides listing 75.0
        assert b["leadTime"] == "2 weeks" and b["terms"] == "Net 30"

    def test_domain_fallback_when_thread_absent(self):
        # A legacy/out-of-thread confirmed quote (NULL thread_id) joins by domain only,
        # and must not cross to a different domain.
        from api_server import _index_quotes, _transform_sourcing_results
        index = _index_quotes([self._quote(domain="bco.com", thread_id=None, unit_price=200.0)], sent=[])
        raw = self._raw(
            {"vendor_name": "A Co", "base_price": 50.0, "source_url": "https://aco.com/x"},
            {"vendor_name": "B Co", "base_price": 75.0, "source_url": "https://bco.com/x"},
        )
        a, b = _transform_sourcing_results(raw, index)["tier3"]
        assert a["evidenceState"] == "priced"                  # not crossed
        assert b["evidenceState"] == "quoted" and b["price"] == 200.0

    def test_low_confidence_confirmed_quote_composes_unverified(self):
        from api_server import _index_quotes, _transform_sourcing_results, _QUOTE_CONFIDENCE_FLOOR
        assert _QUOTE_CONFIDENCE_FLOOR == 0.4   # 0–1 scale (Quote.confidence), NOT the 0–100 floor
        index = _index_quotes([self._quote(domain="bco.com", thread_id="tB", confidence=0.3)],
                              [self._sent(domain="bco.com", thread_id="tB")])
        raw = self._raw({"vendor_name": "B Co", "base_price": 75.0, "source_url": "https://bco.com/x"})
        b = _transform_sourcing_results(raw, index)["tier3"][0]
        assert b["evidenceState"] == "quoted"        # still the strongest state...
        assert b["quoteUnverified"] is True          # ...but a shaky extraction stays flagged

    def test_high_confidence_quote_not_unverified(self):
        from api_server import _index_quotes, _transform_sourcing_results
        index = _index_quotes([self._quote(domain="bco.com", thread_id="tB", confidence=0.99)],
                              [self._sent(domain="bco.com", thread_id="tB")])
        raw = self._raw({"vendor_name": "B Co", "base_price": 75.0, "source_url": "https://bco.com/x"})
        b = _transform_sourcing_results(raw, index)["tier3"][0]
        assert b["quoteUnverified"] is False

    def test_only_confirmed_quotes_drive_state_c(self):
        # A pending (un-reviewed) quote must NOT flip a candidate to "quoted".
        from api_server import _index_quotes, _transform_sourcing_results
        index = _index_quotes([self._quote(domain="bco.com", thread_id="tB", status="pending")],
                              [self._sent(domain="bco.com", thread_id="tB")])
        raw = self._raw({"vendor_name": "B Co", "base_price": 75.0, "source_url": "https://bco.com/x"})
        b = _transform_sourcing_results(raw, index)["tier3"][0]
        assert b["evidenceState"] == "priced" and "quoteConfirmed" not in b

    def test_quote_with_no_matching_candidate_leaves_set_unchanged(self):
        from api_server import _index_quotes, _transform_sourcing_results
        index = _index_quotes([self._quote(domain="zzz.com", thread_id="tZ")],
                              [self._sent(domain="zzz.com", thread_id="tZ")])
        raw = self._raw({"vendor_name": "B Co", "base_price": 75.0, "source_url": "https://bco.com/x"})
        out = _transform_sourcing_results(raw, index)["tier3"]
        assert len(out) == 1 and out[0]["evidenceState"] == "priced"   # no overlay, no crash

    def test_no_index_is_backcompat_noop(self):
        from api_server import _transform_sourcing_results
        raw = self._raw({"vendor_name": "B Co", "base_price": 75.0, "source_url": "https://bco.com/x"})
        b = _transform_sourcing_results(raw)["tier3"][0]   # quote_index defaults None
        assert b["evidenceState"] == "priced" and "quoteConfirmed" not in b

    def test_run_detail_overlays_confirmed_quote_end_to_end(self, api):
        # Full wiring: a confirmed quote (carrying the 3a thread key) overlays its
        # candidate through GET /api/runs/{id}; an unmatched reply is never joined.
        from utils import supplier_registry
        rid = _create_run(api)
        raw = {"tier_3": {"results": [{"vendor_name": "B Co", "base_price": 75.0,
                                       "source_url": "https://bco.com/x"}]}}
        _set_run(api, rid, sourcing_results_json=json.dumps(raw),
                 asset_specs_json=json.dumps({"manufacturer": "Acme", "part_number": "PN-1"}))
        sm_id = supplier_registry.record_sent_message(
            run_id=rid, supplier_domain="bco.com", vendor_name="B Co",
            to=["sales@bco.com"], message_id="m-1", thread_id="t-1")
        supplier_registry.record_review_item(
            "quote", {"unit_price": 222.0, "currency": "USD", "lead_time": "1 week"},
            status="confirmed", run_id=rid, supplier_domain="bco.com", vendor_name="B Co",
            manufacturer="Acme", part_number="PN-1", confidence=0.95,
            thread_id="t-1", sent_message_id=sm_id, message_id="m-1")
        supplier_registry.record_review_item(   # un-attributed unmatched reply -> never joined
            "unmatched_reply", {"sender": "x@nope.com"}, status="needs_human_review",
            run_id=None, raw_source="reply")

        cand = api.get(f"/api/runs/{rid}").json()["sourcing_results"]["tier3"][0]
        assert cand["evidenceState"] == "quoted" and cand["quoteConfirmed"] is True
        assert cand["price"] == 222.0            # quote overrode the listing 75.0
        assert cand["quoteUnverified"] is False


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


# ---------------------------------------------------------------------------
# GET /api/events — derived, untargeted notification feed (read-only, no table)
# ---------------------------------------------------------------------------

class TestDerivedEvents:
    """_derive_events shapes existing persisted rows (orders + runs + confirmed quotes)
    into a normalized, newest-first event list. Sources are mocked so the derivation logic
    is asserted directly (orders.py uses its own DB, not the temp one)."""

    def _patch_sources(self, api, monkeypatch, *, orders=(), runs=(), quotes=()):
        from utils import orders as orders_mod
        from utils.procurement_agent.state import persistence
        from utils import supplier_registry
        monkeypatch.setattr(orders_mod, "get_orders", lambda *a, **k: list(orders))
        monkeypatch.setattr(persistence, "list_runs", lambda *a, **k: list(runs))
        monkeypatch.setattr(supplier_registry, "get_review_items", lambda *a, **k: list(quotes))
        return api._api_server

    def test_order_status_titles_and_draft_skipped(self, api, monkeypatch):
        mod = self._patch_sources(api, monkeypatch, orders=[
            {"id": "o1", "run_id": "r1", "status": "shipped", "updated_at": "2026-06-24T10:00:00+00:00"},
            {"id": "o2", "run_id": "r2", "status": "draft", "updated_at": "2026-06-24T09:00:00+00:00"},
            {"id": "o3", "run_id": "r3", "status": "pending_manual_fulfilment", "updated_at": "2026-06-24T08:00:00+00:00"},
            {"id": "o4", "run_id": "r4", "status": "cancelled", "updated_at": "2026-06-24T07:00:00+00:00"},
        ])
        evs = mod._derive_events()
        titles = {e["order_id"]: e["title"] for e in evs}
        assert titles == {
            "o1": "Order shipped",
            "o3": "Order is being purchased",   # draft (o2) skipped — not a notice
            "o4": "Order cancelled",
        }
        assert all(e["type"] == "order_status" for e in evs)

    def test_approval_events_from_phase_and_rejection_from_history(self, api, monkeypatch):
        mod = self._patch_sources(api, monkeypatch, runs=[
            {"id": "r1", "current_phase": "pending_first_approval", "updated_at": "t3", "approval_history_json": []},
            {"id": "r2", "current_phase": "pending_second_approval", "updated_at": "t2b", "approval_history_json": [{"action": "approved", "acted_at": "t2a"}]},
            {"id": "r3", "current_phase": "approved", "updated_at": "t2", "approval_history_json": [{"action": "approved", "acted_at": "t2"}]},
            {"id": "r4", "current_phase": "comparison", "updated_at": "t1", "approval_history_json": [{"action": "rejected", "acted_at": "t1b"}]},
        ])
        evs = {e["run_id"]: e for e in mod._derive_events()}
        assert evs["r1"]["title"] == "Awaiting approval"
        assert evs["r2"]["title"] == "Awaiting second approval"
        assert evs["r3"]["title"] == "Approved"
        assert evs["r4"]["title"] == "Rejected — re-pick"
        assert evs["r4"]["timestamp"] == "t1b"          # uses the rejection's acted_at
        assert all(e["type"] == "approval" for e in evs.values())

    def test_plain_comparison_run_yields_no_approval_event(self, api, monkeypatch):
        mod = self._patch_sources(api, monkeypatch, runs=[
            {"id": "r1", "current_phase": "comparison", "updated_at": "t1", "approval_history_json": []},
        ])
        assert mod._derive_events() == []

    def test_approved_suppressed_when_order_exists(self, api, monkeypatch):
        mod = self._patch_sources(api, monkeypatch,
            orders=[{"id": "o1", "run_id": "r1", "status": "placed", "updated_at": "t2"}],
            runs=[{"id": "r1", "current_phase": "approved", "updated_at": "t1", "approval_history_json": []}],
        )
        evs = mod._derive_events()
        assert any(e["type"] == "order_status" and e["title"] == "Order placed" for e in evs)
        assert not any(e["type"] == "approval" for e in evs)   # redundant "Approved" suppressed

    def test_confirmed_quote_event(self, api, monkeypatch):
        mod = self._patch_sources(api, monkeypatch, quotes=[
            {"id": "q1", "run_id": "r9", "vendor_name": "Acme", "status": "confirmed",
             "kind": "quote", "resolved_at": "t5", "created_at": "t4"},
        ])
        evs = mod._derive_events()
        assert len(evs) == 1
        assert evs[0]["type"] == "quote_confirmed"
        assert evs[0]["title"] == "Acme quoted your part"
        assert evs[0]["timestamp"] == "t5"               # resolved_at
        assert evs[0]["run_id"] == "r9"

    def test_newest_first_sort(self, api, monkeypatch):
        mod = self._patch_sources(api, monkeypatch, orders=[
            {"id": "o1", "run_id": "r1", "status": "placed", "updated_at": "2026-06-01T00:00:00+00:00"},
            {"id": "o2", "run_id": "r2", "status": "shipped", "updated_at": "2026-06-20T00:00:00+00:00"},
        ])
        assert [e["order_id"] for e in mod._derive_events()] == ["o2", "o1"]

    def test_failsoft_one_source_raises(self, api, monkeypatch):
        from utils import orders as orders_mod
        from utils.procurement_agent.state import persistence
        from utils import supplier_registry
        def boom(*a, **k):
            raise RuntimeError("db down")
        monkeypatch.setattr(orders_mod, "get_orders", boom)
        monkeypatch.setattr(persistence, "list_runs", lambda *a, **k: [
            {"id": "r1", "current_phase": "pending_first_approval", "updated_at": "t1", "approval_history_json": []}])
        monkeypatch.setattr(supplier_registry, "get_review_items", lambda *a, **k: [])
        evs = api._api_server._derive_events()          # must not raise
        assert [e["title"] for e in evs] == ["Awaiting approval"]

    def test_endpoint_returns_events(self, api, monkeypatch):
        self._patch_sources(api, monkeypatch, orders=[
            {"id": "o1", "run_id": "r1", "status": "shipped", "updated_at": "2026-06-24T10:00:00+00:00"}])
        resp = api.get("/api/events")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["events"][0]["title"] == "Order shipped"
        assert body["events"][0]["type"] == "order_status"

    def test_endpoint_failsoft_never_500s(self, api, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(api._api_server, "_derive_events", boom)
        resp = api.get("/api/events")
        assert resp.status_code == 200
        assert resp.json() == {"count": 0, "events": []}


# ---------------------------------------------------------------------------
# Multi-part Increment 1 — nullable group_id basket label (additive; a NULL-group
# run is byte-for-byte a today-run). Column round-trip + the ?group_id= read filter.
# ---------------------------------------------------------------------------

class TestGroupIdColumn:
    def test_group_id_round_trips_via_list_and_detail(self, api):
        rid = _create_run(api)
        _set_run(api, rid, group_id="grp-1")

        detail = api.get(f"/api/runs/{rid}").json()
        assert detail["group_id"] == "grp-1"

        item = next(r for r in api.get("/api/runs").json() if r["id"] == rid)
        assert item["group_id"] == "grp-1"

    def test_legacy_run_group_id_null(self, api):
        # A run created the normal (single-part) way carries no group — NULL — and serializes
        # exactly as before: group_id is the ONLY new field, present and None.
        rid = _create_run(api)

        detail = api.get(f"/api/runs/{rid}").json()
        assert detail["group_id"] is None
        # Spot-check the rest of the envelope is intact (byte-for-byte a today-run).
        assert detail["id"] == rid
        assert detail["phase"] == "intake"
        assert detail["facility_id"] == "00000000-0000-0000-0000-000000000000"

        item = next(r for r in api.get("/api/runs").json() if r["id"] == rid)
        assert item["group_id"] is None

    def test_filter_returns_only_the_group_and_no_param_unchanged(self, api):
        a1, a2, b1 = _create_run(api), _create_run(api), _create_run(api)
        legacy = _create_run(api)  # NULL group — must never appear in a group filter
        _set_run(api, a1, group_id="grp-A")
        _set_run(api, a2, group_id="grp-A")
        _set_run(api, b1, group_id="grp-B")

        # No param: unchanged — every run present (same set the un-filtered query returns).
        all_ids = [r["id"] for r in api.get("/api/runs").json()]
        assert set(all_ids) >= {a1, a2, b1, legacy}

        # Filter: only the matching group, and in the SAME relative order as the no-param list
        # (the filter narrows, it does not reorder).
        group_a = [r["id"] for r in api.get("/api/runs", params={"group_id": "grp-A"}).json()]
        assert set(group_a) == {a1, a2}
        assert group_a == [i for i in all_ids if i in {a1, a2}]

        # A NULL-group run is invisible to a group filter; an unknown group is empty.
        assert api.get("/api/runs", params={"group_id": "grp-B"}).json()[0]["id"] == b1
        assert api.get("/api/runs", params={"group_id": "nope"}).json() == []

    def test_create_index_migration_idempotent(self, api):
        # The guarded ALTER + CREATE INDEX IF NOT EXISTS must survive re-running (the column
        # and index already exist on the temp DB) without raising.
        api._api_server._migrate_schema()
        api._api_server._migrate_schema()


# ---------------------------------------------------------------------------
# Multi-part Increment 1, Stage 2 — single-vs-multi routing front door (additive).
# Routes on a PROVIDED part count; the single branch delegates to the unchanged path,
# the multi branch hits the Stage-3 fan-out seam. No extraction here.
# ---------------------------------------------------------------------------

class TestIntakeRouter:
    def test_route_intake_is_deterministic(self, api):
        route = api._api_server.route_intake
        # <=1 -> single ; >=2 -> multi ; same input -> same decision, repeatedly.
        assert route(0) == "single"
        assert route(1) == "single"
        assert route(2) == "multi"
        assert route(5) == "multi"
        assert [route(2) for _ in range(5)] == ["multi"] * 5
        assert [route(1) for _ in range(5)] == ["single"] * 5

    def test_single_part_routes_to_existing_path_unchanged(self, api):
        # One part -> the existing single-run path. Shape + phase identical to a direct
        # POST /api/runs (the front door delegates to the unchanged create_run).
        direct = api.post("/api/runs", json={})
        viafd = api.post("/api/requests", json={"parts": [{"any": "spec"}]})
        assert direct.status_code == viafd.status_code == 201
        assert set(viafd.json().keys()) == set(direct.json().keys())
        assert viafd.json()["phase"] == direct.json()["phase"] == "intake"

    def test_zero_parts_routes_single(self, api):
        # Empty parts is N<=1 -> single (a bare run, exactly as POST /api/runs today).
        resp = api.post("/api/requests", json={"parts": []})
        assert resp.status_code == 201
        assert resp.json()["phase"] == "intake"

    def test_single_part_creates_ungrouped_run(self, api):
        # The single branch -> create_run -> group_id NULL. A single is NEVER a basket.
        resp = api.post("/api/requests", json={"parts": [{"a": 1}]})
        assert resp.status_code == 201
        detail = api.get(f"/api/runs/{resp.json()['id']}").json()
        assert detail["group_id"] is None


# ---------------------------------------------------------------------------
# Multi-part Increment 1, Stage 3 — fan-out: one request -> N independent grouped runs.
# The runs are joined ONLY by a shared group_id; each is a normal run_id-scoped run.
# ---------------------------------------------------------------------------

class TestFanOut:
    def test_fanout_creates_n_grouped_independent_runs(self, api):
        resp = api.post("/api/requests", json={"parts": [{"p": 1}, {"p": 2}, {"p": 3}]})
        assert resp.status_code == 201
        body = resp.json()
        gid, run_ids = body["group_id"], body["run_ids"]
        assert gid
        assert len(run_ids) == 3
        assert len(set(run_ids)) == 3                       # distinct run_ids
        for rid in run_ids:
            detail = api.get(f"/api/runs/{rid}").json()
            assert detail["group_id"] == gid                # all share the one label
            assert detail["phase"] == "intake"              # normal fresh runs

    def test_fanout_runs_advance_independently(self, api):
        run_ids = api.post("/api/requests", json={"parts": [{}, {}, {}]}).json()["run_ids"]
        # Advance ONE sibling directly; the others must be untouched (no shared state).
        _set_run(api, run_ids[0], current_phase="sourcing")
        assert api.get(f"/api/runs/{run_ids[0]}").json()["phase"] == "sourcing"
        assert api.get(f"/api/runs/{run_ids[1]}").json()["phase"] == "intake"
        assert api.get(f"/api/runs/{run_ids[2]}").json()["phase"] == "intake"

    def test_fanout_group_filter_returns_exactly_the_siblings(self, api):
        # Ties Stage 1's ?group_id= filter to real fan-out output.
        body = api.post("/api/requests", json={"parts": [{}, {}, {}]}).json()
        gid = body["group_id"]
        filtered = api.get("/api/runs", params={"group_id": gid}).json()
        assert {r["id"] for r in filtered} == set(body["run_ids"])

    def test_fanout_is_all_or_nothing_on_partial_failure(self, api, monkeypatch):
        # All-or-nothing: if the 2nd run fails to build, ZERO runs are persisted.
        before = len(api.get("/api/runs").json())
        real_orm = api._api_server.SourcingRunORM
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated insert failure")
            return real_orm(*a, **k)

        monkeypatch.setattr(api._api_server, "SourcingRunORM", flaky)
        with pytest.raises(RuntimeError):
            api.post("/api/requests", json={"parts": [{}, {}, {}]})
        # No partial basket — count unchanged.
        monkeypatch.setattr(api._api_server, "SourcingRunORM", real_orm)
        assert len(api.get("/api/runs").json()) == before

    def test_maintenance_single_path_unchanged_fanout_deferred(self, api):
        # Maintenance-path fan-out is DEFERRED (needs MaintenanceSubmission to carry N parts).
        # The existing single from-maintenance path is byte-for-byte: one submission -> one
        # run, NULL group.
        sub = {
            "submission_id": "sub-fanout-doc-1",
            "facility_id": "fac-x",
            "submitted_by": "tech@plant",
            "asset_specs": {"manufacturer": "Acme", "part_number": "PN-1"},
            "context": {"chat_thread_summary": "pump seal leaking", "urgency": "standard"},
        }
        resp = api.post("/api/runs/from-maintenance", json=sub)
        assert resp.status_code == 201
        detail = api.get(f"/api/runs/{resp.json()['run_id']}").json()
        assert detail["group_id"] is None
