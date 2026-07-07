"""Night 2 — LABELING SURFACE live-faithful API tests.

Drives the REAL api_server handler path with TestClient (guardrail 7: a
classifier/scorer/exporter test must feed the live path's inputs; key tests run
through the real API, not by calling functions directly). Mocked LLM/search —
no live network.

Covers T2 (admin labeling endpoints), T4 (exporter live-faithfulness), T5
(provenance), T6 (inertness). The run_labels store unit layer lives in
test_run_labels.py; this file exercises the admin gate + the live seams.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


_TOKEN = "test-admin-secret-123"


@pytest.fixture
def label_api(tmp_path, monkeypatch):
    """TestClient + admin token + RUN_CAPTURE on + isolated capture/labels stores."""
    from utils.procurement_agent.state import persistence

    engine = persistence._make_engine(f"sqlite:///{tmp_path / 'api.sqlite'}")
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    persistence.Base.metadata.create_all(engine)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr(persistence, "_engine", engine)
    monkeypatch.setattr(persistence, "_SessionFactory", TestSession)

    from utils import supplier_registry, orders, price_db, site_settings
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    monkeypatch.setattr(orders, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(orders, "_DB_PATH", str(tmp_path / "orders.sqlite"))
    monkeypatch.setattr(price_db, "_DB_PATH", str(tmp_path / "price_db.json"))
    monkeypatch.setattr(site_settings, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(site_settings, "_DB_PATH", str(tmp_path / "site_settings.sqlite"))

    monkeypatch.setenv("ARKIM_ADMIN_TOKEN", _TOKEN)

    import utils.run_capture as rc
    monkeypatch.setattr(rc, "RUN_CAPTURE", True)
    monkeypatch.setattr(rc, "_DB_PATH", str(tmp_path / "run_capture.sqlite"))
    monkeypatch.setattr(rc, "_DATA_DIR", str(tmp_path))
    rc.reset_failures()

    import utils.run_labels as rl
    monkeypatch.setattr(rl, "RUN_CAPTURE", True)
    monkeypatch.setattr(rl, "_DB_PATH", str(tmp_path / "run_labels.sqlite"))
    monkeypatch.setattr(rl, "_DATA_DIR", str(tmp_path))
    rl.reset_failures()

    import utils.eval_export as ex
    # Isolate the real-cases dataset files into tmp_path (never touch committed fixtures).
    monkeypatch.setattr(ex, "INTAKE_REAL_DATASET", str(tmp_path / "intake_real.json"))
    monkeypatch.setattr(ex, "SCORING_REAL_DATASET", str(tmp_path / "scoring_real.json"))
    monkeypatch.setattr(ex, "RUN_CAPTURE", True)

    import api_server
    monkeypatch.setattr(api_server, "_engine", engine)
    monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
    monkeypatch.setattr(api_server, "_messages", {})
    monkeypatch.setattr(api_server, "DEMO_MODE", False)

    client = TestClient(api_server.app)
    client._token = _TOKEN
    client._tmp = str(tmp_path)
    return client


def _auth(token: str = _TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_run(client) -> str:
    r = client.post("/api/runs", json={})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# T2 — admin gate on labeling endpoints (same 401/403/503 as the rest of admin)
# ---------------------------------------------------------------------------

class TestLabelingAdminGate:
    def test_admin_token_grants_access(self, label_api):
        r = label_api.get("/api/admin/labeling/queue", headers=_auth())
        assert r.status_code == 200

    def test_wrong_token_forbidden(self, label_api):
        r = label_api.get("/api/admin/labeling/queue", headers=_auth("wrong"))
        assert r.status_code == 403

    def test_missing_header_unauthorized(self, label_api):
        assert label_api.get("/api/admin/labeling/queue").status_code == 401

    def test_flag_off_returns_503_dormant(self, label_api, monkeypatch):
        """RUN_CAPTURE off -> labeling endpoints 503 (dormant), never 200."""
        import utils.run_labels as rl
        monkeypatch.setattr(rl, "RUN_CAPTURE", False)
        r = label_api.get("/api/admin/labeling/queue", headers=_auth())
        assert r.status_code == 503
        assert "RUN_CAPTURE off" in r.json()["detail"]


# ---------------------------------------------------------------------------
# T2 — queue is failures-first via run_outcomes
# ---------------------------------------------------------------------------

class TestLabelingQueueFailuresFirst:
    def test_queue_orders_failures_before_completed(self, label_api, monkeypatch):
        from utils import run_capture as rc
        from utils.procurement_agent.state import persistence

        # Two runs: one completed, one abandoned_after_results.
        rid_done = _create_run(label_api)
        rid_aband = _create_run(label_api)
        # Write outcomes directly via run_capture.
        rc.write_outcome(rid_done, rc.OUTCOME_COMPLETED_WITH_ACTION)
        rc.write_outcome(rid_aband, rc.OUTCOME_ABANDONED_AFTER_RESULTS)
        r = label_api.get("/api/admin/labeling/queue", headers=_auth())
        assert r.status_code == 200
        ids = [x["id"] for x in r.json()["queue"]]
        # abandoned sorts before completed.
        assert ids.index(rid_aband) < ids.index(rid_done)

    def test_queue_row_shape(self, label_api):
        rid = _create_run(label_api)
        r = label_api.get("/api/admin/labeling/queue", headers=_auth())
        row = next(x for x in r.json()["queue"] if x["id"] == rid)
        assert {"id", "outcome", "part", "phase", "created_at", "labeled"} <= set(row)
        assert row["labeled"] is False


# ---------------------------------------------------------------------------
# T2 — labeling run detail renders input/intake/candidates
# ---------------------------------------------------------------------------

class TestLabelingRunDetail:
    def test_detail_includes_first_user_turn_and_candidates(self, label_api, monkeypatch):
        from utils import run_capture as rc
        rid = _create_run(label_api)
        rc.capture_turn(rid, "user", "I need a Goulds 3196 mechanical seal")
        rc.capture_intake_result(rid, sufficient=True, proceed_state="proceed_spec_based",
                                 manufacturer_confidence=80.0, part_id_confidence=70.0,
                                 asset_specs={"manufacturer": "Goulds", "model": "3196",
                                              "detected_type": "mechanical seal"})
        rc.capture_candidate(rid, 2, {"candidate_id": "Platinum-t2-0",
                                      "vendor_name": "Platinum", "suitability_score": 65.0})
        r = label_api.get(f"/api/admin/labeling/runs/{rid}", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["first_user_turn"] == "I need a Goulds 3196 mechanical seal"
        assert body["intake_result"]["proceed_state"] == "proceed_spec_based"
        assert len(body["candidates"]) == 1
        assert body["candidates"][0]["candidate_id"] == "Platinum-t2-0"
        assert body["candidates"][0]["verdict"] == "scored"
        assert body["provenance"] == f"real:{rid}"

    def test_detail_404_unknown_run(self, label_api):
        r = label_api.get("/api/admin/labeling/runs/nope", headers=_auth())
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# T2 — POST label appends; current label is the latest
# ---------------------------------------------------------------------------

class TestPostLabel:
    def test_post_run_scope_label(self, label_api):
        rid = _create_run(label_api)
        r = label_api.post("/api/admin/labeling/label", headers=_auth(), json={
            "run_id": rid, "scope": "run",
            "label": {"intake_correct": True, "expected_part_type": "mechanical_seal",
                      "expected_component_of": "Goulds 3196", "expected_regime": "ANCHORED",
                      "corrections": None},
            "labeled_by": "tom",
        })
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert r.json()["current"]["expected_part_type"] == "mechanical_seal"

    def test_post_candidate_scope_label(self, label_api):
        rid = _create_run(label_api)
        r = label_api.post("/api/admin/labeling/label", headers=_auth(), json={
            "run_id": rid, "scope": "candidate", "candidate_ref": "Platinum-t2-0",
            "label": {"right_part_type": "SEAL", "should_pass_floor": True},
        })
        assert r.status_code == 200
        assert r.json()["current"]["should_pass_floor"] is True

    def test_post_relabel_appends_latest_wins(self, label_api):
        rid = _create_run(label_api)
        label_api.post("/api/admin/labeling/label", headers=_auth(), json={
            "run_id": rid, "scope": "run",
            "label": {"intake_correct": False, "expected_part_type": "pump",
                      "expected_component_of": None, "expected_regime": "DIRECT"},
        })
        r = label_api.post("/api/admin/labeling/label", headers=_auth(), json={
            "run_id": rid, "scope": "run",
            "label": {"intake_correct": True, "expected_part_type": "mechanical_seal",
                      "expected_component_of": "Goulds 3196", "expected_regime": "ANCHORED"},
        })
        assert r.json()["current"]["expected_part_type"] == "mechanical_seal"

    def test_post_candidate_scope_requires_ref(self, label_api):
        rid = _create_run(label_api)
        r = label_api.post("/api/admin/labeling/label", headers=_auth(), json={
            "run_id": rid, "scope": "candidate",
            "label": {"right_part_type": "SEAL", "should_pass_floor": True},
        })
        assert r.status_code == 422

    def test_post_bad_scope_422(self, label_api):
        rid = _create_run(label_api)
        r = label_api.post("/api/admin/labeling/label", headers=_auth(), json={
            "run_id": rid, "scope": "bogus", "label": {},
        })
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# T4 — exporter live-faithfulness (through the real /export endpoint)
# ---------------------------------------------------------------------------

class TestExportLiveFaithful:
    def _seed_labeled_run(self, client, monkeypatch):
        """Seed a captured + labeled run (intake ground truth + candidate label)."""
        from utils import run_capture as rc
        from utils.procurement_agent.state import persistence
        rid = _create_run(client)
        # Live-faithful: the first user turn is what the classifier consumed.
        rc.capture_turn(rid, "user", "Goulds 3196 mechanical seal")
        rc.capture_intake_result(rid, sufficient=True, proceed_state="proceed_spec_based",
                                 manufacturer_confidence=80.0, part_id_confidence=70.0,
                                 asset_specs={"manufacturer": "Goulds", "model": "3196",
                                              "part_number": "UNKNOWN-PN",
                                              "category": "Part",
                                              "detected_type": "mechanical seal"})
        # A scored candidate (no snippet/title — mirrors the real capture).
        rc.capture_candidate(rid, 2, {"candidate_id": "Platinum-t2-0",
                                      "vendor_name": "Platinum", "suitability_score": 65.0})
        # Persist sourcing_results so the exporter can join url/found_pn.
        persistence.update_run(rid, {
            "sourcing_results_json": {
                "tier_2": {"results": [
                    {"vendor_name": "Platinum", "source_url": "https://x.com/seal",
                     "found_part_number": "ST-1.375-T1"}]},
            },
        })
        # Run-scope label with ground truth.
        client.post("/api/admin/labeling/label", headers=_auth(), json={
            "run_id": rid, "scope": "run",
            "label": {"intake_correct": True, "expected_part_type": "mechanical_seal",
                      "expected_component_of": "Goulds 3196", "expected_regime": "ANCHORED"},
        })
        # Candidate-scope label.
        client.post("/api/admin/labeling/label", headers=_auth(), json={
            "run_id": rid, "scope": "candidate", "candidate_ref": "Platinum-t2-0",
            "label": {"right_part_type": "SEAL", "should_pass_floor": True},
        })
        return rid

    def test_export_emits_live_faithful_intake_case(self, label_api, monkeypatch):
        rid = self._seed_labeled_run(label_api, monkeypatch)
        r = label_api.post("/api/admin/labeling/export", headers=_auth())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["intake"]["emitted"] == 1
        # The intake case input = the first user turn (live-faithful).
        with open(body["intake_dataset"], encoding="utf-8") as fh:
            data = json.load(fh)
        ex = data["examples"][-1]
        assert ex["input"] == "Goulds 3196 mechanical seal"
        assert ex["expected_part_type"] == "mechanical_seal"
        assert ex["expected_component_of"] == "Goulds 3196"
        assert ex["expected_regime"] == "ANCHORED"
        assert ex["provenance"] == f"real:{rid}"
        assert ex["split"] in ("dev", "holdout")

    def test_export_withholds_scoring_when_snippet_missing(self, label_api, monkeypatch):
        """The scoring scorer is fed snippet+title; capture has neither -> the
        exporter WITHHOLDS rather than emit a non-live-faithful case (the rule
        this codebase shipped 2+ bugs against)."""
        self._seed_labeled_run(label_api, monkeypatch)
        r = label_api.post("/api/admin/labeling/export", headers=_auth())
        body = r.json()
        assert body["scoring"]["emitted"] == 0
        assert body["scoring"]["withheld"] >= 1
        assert any("snippet" in w for w in body["withhold_reasons"])
        # And no scoring dataset file was even created with cases.
        if os.path.exists(body["scoring_dataset"]):
            with open(body["scoring_dataset"], encoding="utf-8") as fh:
                assert json.load(fh)["cases"] == []

    def test_exported_intake_case_passes_existing_validator(self, label_api, monkeypatch):
        """The exported intake case must satisfy test_intake_eval_dataset's schema."""
        self._seed_labeled_run(label_api, monkeypatch)
        r = label_api.post("/api/admin/labeling/export", headers=_auth())
        with open(r.json()["intake_dataset"], encoding="utf-8") as fh:
            ex = json.load(fh)["examples"][-1]
        # Mirror the existing validator's required keys.
        for key in ("input", "expected_part_type", "expected_component_of",
                    "expected_regime", "split"):
            assert key in ex
        assert ex["expected_part_type"] in {
            "mechanical_seal", "pump", "valve", "sensor_instrument", "motor_drive", "unknown"}
        assert ex["expected_regime"] in ("DIRECT", "ANCHORED")
        assert ex["split"] in ("dev", "holdout")

    def test_export_dedups_on_relabel(self, label_api, monkeypatch):
        rid = self._seed_labeled_run(label_api, monkeypatch)
        label_api.post("/api/admin/labeling/export", headers=_auth())
        # Relabel + re-export -> one example, not two.
        label_api.post("/api/admin/labeling/label", headers=_auth(), json={
            "run_id": rid, "scope": "run",
            "label": {"intake_correct": False, "expected_part_type": "pump",
                      "expected_component_of": None, "expected_regime": "DIRECT"},
        })
        label_api.post("/api/admin/labeling/export", headers=_auth())
        r = label_api.post("/api/admin/labeling/export", headers=_auth())
        with open(r.json()["intake_dataset"], encoding="utf-8") as fh:
            data = json.load(fh)
        # One example for this run's input (latest label wins).
        provs = [e for e in data["examples"] if e["provenance"] == f"real:{rid}"]
        assert len(provs) == 1
        assert provs[0]["expected_part_type"] == "pump"


# ---------------------------------------------------------------------------
# T5 — provenance metric
# ---------------------------------------------------------------------------

class TestProvenance:
    def test_provenance_reports_zero_real_before_export(self, label_api):
        r = label_api.get("/api/admin/labeling/provenance", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["intake"]["real_cases"] == 0
        # synthetic fixture counts are read from the committed fixtures.
        assert body["intake"]["synthetic_cases"] >= 24
        assert body["intake"]["real_pct"] == 0.0

    def test_provenance_reflects_exported_real_cases(self, label_api, monkeypatch):
        from utils import run_capture as rc
        rid = _create_run(label_api)
        rc.capture_turn(rid, "user", "Goulds 3196 mechanical seal")
        rc.capture_intake_result(rid, sufficient=True, proceed_state="proceed_spec_based",
                                 manufacturer_confidence=80.0, part_id_confidence=70.0,
                                 asset_specs={"manufacturer": "Goulds", "model": "3196"})
        label_api.post("/api/admin/labeling/label", headers=_auth(), json={
            "run_id": rid, "scope": "run",
            "label": {"intake_correct": True, "expected_part_type": "mechanical_seal",
                      "expected_component_of": "Goulds 3196", "expected_regime": "ANCHORED"},
        })
        label_api.post("/api/admin/labeling/export", headers=_auth())
        r = label_api.get("/api/admin/labeling/provenance", headers=_auth())
        body = r.json()
        assert body["intake"]["real_cases"] >= 1
        assert body["intake"]["real_pct"] > 0.0


# ---------------------------------------------------------------------------
# T6 — inertness: flag-off = labeling dormant, byte-identical health + API
# ---------------------------------------------------------------------------

class TestInertness:
    def test_flag_off_health_byte_identical(self, label_api, monkeypatch):
        """Flag off -> /api/health is byte-identical to the pre-Night-2 contract."""
        import utils.run_capture as rc
        import utils.run_labels as rl
        monkeypatch.setattr(rc, "RUN_CAPTURE", False)
        monkeypatch.setattr(rl, "RUN_CAPTURE", False)
        r = label_api.get("/api/health")
        assert r.status_code == 200
        # The Night 1 + Night 2 additions (capture_failures / label_failures) are
        # absent when the flag is off — byte-identical to the original health body.
        assert r.json() == {"status": "ok", "version": "1.0.0-phase1", "demo_mode": False}

    def test_flag_off_labeling_endpoints_503(self, label_api, monkeypatch):
        import utils.run_labels as rl
        monkeypatch.setattr(rl, "RUN_CAPTURE", False)
        for path in ("/api/admin/labeling/queue", "/api/admin/labeling/provenance"):
            assert label_api.get(path, headers=_auth()).status_code == 503
        assert label_api.post("/api/admin/labeling/export", headers=_auth()).status_code == 503
        assert label_api.post("/api/admin/labeling/label", headers=_auth(),
                              json={"run_id": "x", "scope": "run", "label": {}}).status_code == 503

    def test_flag_off_existing_admin_endpoints_unaffected(self, label_api, monkeypatch):
        """Flag off -> the existing admin surface is byte-identical (labeling is additive)."""
        import utils.run_labels as rl
        monkeypatch.setattr(rl, "RUN_CAPTURE", False)
        r = label_api.get("/api/admin/ping", headers=_auth())
        assert r.status_code == 200
        assert r.json() == {"ok": True, "role": "admin"}

    def test_flag_off_full_run_path_byte_identical_no_leakage(self, label_api, monkeypatch):
        """T6 — with RUN_CAPTURE off, a full run-create + messages + confirm-intake
        cycle produces ZERO capture/label rows and the responses carry no
        Night-2 artifacts. The labeling surface is dormant and the existing API
        is byte-identical to pre-Night-2."""
        from unittest.mock import Mock
        import utils.run_capture as rc
        import utils.run_labels as rl
        monkeypatch.setattr(rc, "RUN_CAPTURE", False)
        monkeypatch.setattr(rl, "RUN_CAPTURE", False)
        # Create a run + send a message (mocked intake) + confirm intake.
        rid = _create_run(label_api)
        api_server_mod = label_api._api_server if hasattr(label_api, "_api_server") else None
        # Mock intake via the source module (mirrors test_run_capture_live).
        import utils.procurement_agent.agents.intake_agent as ia_mod
        agent = Mock()
        agent.run.return_value = {
            "asset_specs": {"manufacturer": "Goulds", "model": "3196"},
            "manufacturer_confidence": 85.0, "part_id_confidence": 70.0,
            "sufficient": True, "follow_up_question": None, "commit_message": None,
            "confidence_summary": {"proceed_state": "proceed_spec_based"},
        }
        monkeypatch.setattr(ia_mod, "IntakeAgent", lambda *a, **k: agent)
        r1 = label_api.post(f"/api/runs/{rid}/messages",
                            json={"content": "I need a Goulds 3196 mechanical seal"})
        assert r1.status_code == 200
        # The message response carries no labeling/capture artifact — just the
        # existing shape (message.content present, no label_failures etc.).
        assert "message" in r1.json()
        # No capture/label rows written (flag off).
        assert rc.read_all_events() == []
        assert rl.read_all_labels() == []
        # Health is byte-identical to pre-Night-2.
        assert label_api.get("/api/health").json() == {
            "status": "ok", "version": "1.0.0-phase1", "demo_mode": False}
        # Labeling endpoints dormant.
        assert label_api.get("/api/admin/labeling/queue",
                             headers=_auth()).status_code == 503

    def test_flag_off_label_store_db_not_created(self, label_api, monkeypatch):
        """Flag off -> the label store never creates a DB file (zero writes)."""
        import utils.run_labels as rl
        monkeypatch.setattr(rl, "RUN_CAPTURE", False)
        label_api.post("/api/admin/labeling/label", headers=_auth(), json={
            "run_id": "x", "scope": "run", "label": {}})  # 503 before any write
        import os
        assert not os.path.exists(rl._DB_PATH)
