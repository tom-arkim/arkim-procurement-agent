"""T4 evals — family-level variant disambiguation, live-faithful.

Exercises the REAL IntakeAgent.run() path (mocked extraction `requests.post` +
mocked `classify_part_type`, INTAKE_TYPE_AWARE on) to assert the variant ask
fires with the right shape, AND the real confirm_intake endpoint against specs
a real run() produced — proving the run()-produced state drives the 422/200
guard end-to-end (not just hand-crafted specs).

Cases (per the T4 brief):
  1. PowerFlex 40 (model-no-PN, motor_drive) -> intake asks for the RATING via
     the variant-disambiguation question (not just voltage); missing_field is
     variant_disambig; the follow-up names the family + the variant-selecting
     attrs.
  2/3. confirm_intake on that run (no answer, no opt-in) -> 422; open_family=true
     -> 200 (honest markers); after the user answers "5HP 480V" -> proceeds;
     PN-present (22B-D010N104) -> proceeds (escape hatch intact).
  4. Spec-described no-model -> unaffected at both gates.
  5. Generalization via dual-source: a bearing model-no-PN (classified unknown ->
     legacy CATEGORY_REQUIRED_FIELDS -> bore_diameter) and a pump model-no-PN
     (hydraulic_duty) both trigger the variant ask.
  6. attr->field mapping: "480V" satisfies the motor_drive guard
     (voltage_phase answered via voltage).
  7. Hallucination guard: extractor fills but pending ask -> block-regardless;
     confirm with pending -> 422 even though attrs filled.
  8. typing-bypass (T3b) -> already covered by TestConfirmIntakeFamilyVariantGuard
     (test_api_server.py); not duplicated here.

Plus the SCOPE invariant the T4 fix introduced: the no-identity q2 flow
(_next_clarification's `_q2_asked` branch) returns its template VERBATIM — only
the variant-disambiguation question prepends the family fact.
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

from utils.models import SourcingRun
from utils.procurement_agent.agents.intake_agent import (
    IntakeAgent,
    family_disambig_block,
)
from utils.procurement_agent.part_type_classifier import Classification
from utils.procurement_agent.part_type_registry import (
    get_profile,
    variant_attr_answered,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run(specs: dict | None = None) -> SourcingRun:
    return SourcingRun(asset_specs_json=specs)


def _mock_anthropic_response(payload: dict) -> MagicMock:
    """A mocked requests.post return value whose body is the given extraction
    payload (the shape IntakeAgent._extract_text parses)."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"content": [{"text": json.dumps(payload)}]}
    return mock_resp


def _clf(part_type: str, confidence: int = 95, component_of=None) -> Classification:
    """A Classification for the given registry part_type (regime/sourcing resolved
    from the registry so it matches what the real classifier would store)."""
    p = get_profile(part_type)
    return Classification(
        part_type=part_type,
        regime=p.regime,
        sourcing=p.sourcing,
        component_of=component_of,
        confidence=confidence,
    )


def _run(agent: IntakeAgent, prior: dict | None, text: str,
         payload: dict, clf: Classification | None):
    """One IntakeAgent.run() turn with extraction + classification mocked.

    `clf=None` skips the classifier mock (use on follow-up turns where the
    classifier is NOT re-invoked, or when INTAKE_TYPE_AWARE is off)."""
    prior = prior or {}
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_anthropic_response(payload)):
        if clf is not None:
            with patch("utils.procurement_agent.part_type_classifier.classify_part_type",
                       return_value=clf):
                return agent.run(_make_run(prior), {"text": text, "images": []})
        # No classifier mock — classify_part_type may still be called on a first
        # turn; stub it to UNKNOWN so it never reaches the network by accident.
        with patch("utils.procurement_agent.part_type_classifier.classify_part_type",
                   return_value=_clf("unknown", confidence=0)):
            return agent.run(_make_run(prior), {"text": text, "images": []})


# PowerFlex 40 — model-no-PN, motor_drive. The canonical family-level case.
def _pf40_payload(**over) -> dict:
    base = {
        "manufacturer":            "Allen-Bradley",
        "model":                   "PowerFlex 40",
        "part_number":             None,          # family-level (no PN)
        "detected_type":           "Variable Frequency Drive (VFD)",
        "category":                "Part",
        "hp":                      None,
        "voltage":                 None,
        "phase":                   None,
        "frame":                   None,
        "rpm":                     None,
        "manufacturer_confidence": 92,
        "part_id_confidence":      88,
        "confidence_reasoning":    "manufacturer + model stated, no catalog number",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Run-path: the variant ask fires with the right shape (cases 1, 4, 5, 7).
# ---------------------------------------------------------------------------

class TestVariantDisambigRunPath:
    """Live-faithful run() assertions — the specs + question the chat produces
    BEFORE any confirm. INTAKE_TYPE_AWARE on; extraction + classifier mocked."""

    def test_pf40_motor_drive_asks_variant_names_family_and_attrs(self, monkeypatch):
        """Case 1: PowerFlex 40, motor_drive -> missing_field=variant_disambig,
        follow-up names the family (PowerFlex 40) AND the variant-selecting attrs
        (hp + voltage/phase), pending set on the specs."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        agent = IntakeAgent(anthropic_api_key="test-key")
        result = _run(agent, {}, "Need an Allen-Bradley PowerFlex 40",
                      _pf40_payload(), _clf("motor_drive"))

        assert result["sufficient"] is False
        assert result["confidence_summary"]["missing_field"] == "variant_disambig"
        follow_up = result["follow_up_question"]
        assert follow_up is not None
        # Names the family (the model the user named)...
        assert "PowerFlex 40" in follow_up
        # ...and the variant-selecting attrs (the motor_drive q2_template carries
        # HP and voltage/phase).
        assert "HP" in follow_up
        assert "voltage/phase" in follow_up
        # The binding flag is set so confirm_intake (T3) can block a silent bypass.
        specs = result["asset_specs"]
        assert specs["_variant_disambig_pending"] is True
        assert "_q2_variant" in specs["_asked_fields"]

    def test_pf40_hallucination_guard_extractor_fills_but_pending_blocks(self, monkeypatch):
        """Case 7: the extractor filled hp+voltage (a hallucinated rating) but the
        variant ask is issued THIS turn (pending set) -> still blocked
        (block-regardless-of-extracted — no provenance). pending stays True and
        confirm_intake will 422 even though the attrs are filled."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        agent = IntakeAgent(anthropic_api_key="test-key")
        # Extractor "hallucinated" a rating on turn 1, before the user engaged.
        result = _run(agent, {}, "Need an Allen-Bradley PowerFlex 40",
                      _pf40_payload(hp="5", voltage="480V"), _clf("motor_drive"))

        assert result["sufficient"] is False
        assert result["confidence_summary"]["missing_field"] == "variant_disambig"
        specs = result["asset_specs"]
        # The ask is in flight — pending True — so the attrs are NOT trusted.
        assert specs["_variant_disambig_pending"] is True
        assert specs["hp"] == "5" and specs["voltage"] == "480V"
        # The family-variant block (the confirm_intake guard) fires on pending:
        block = family_disambig_block(specs)
        assert block is not None
        assert block["pending"] is True

    def test_pf40_answer_resolves_pending_and_proceeds(self, monkeypatch):
        """Cases 2/3 + 6 (run side): after the user answers "5HP 480V" (turn 2,
        prior pending + _q2_variant asked), pending clears, the attrs fill, and
        the run is sufficient. '480V' answers voltage_phase via the attr->field
        mapping (voltage_phase -> voltage OR phase)."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        agent = IntakeAgent(anthropic_api_key="test-key")

        # Turn 1 — the variant ask fires.
        t1 = _run(agent, {}, "Need an Allen-Bradley PowerFlex 40",
                  _pf40_payload(), _clf("motor_drive"))
        assert t1["asset_specs"]["_variant_disambig_pending"] is True

        # Turn 2 — the user answers the rating. Extraction fills hp+voltage.
        t2 = _run(agent, t1["asset_specs"], "5HP 480V",
                  _pf40_payload(hp="5", voltage="480V"), clf=None)
        specs = t2["asset_specs"]
        assert t2["sufficient"] is True
        # The user engaged -> pending cleared.
        assert specs["_variant_disambig_pending"] is False
        # The attrs are filled and ANSWER the variant-selecting attrs via the
        # mapping (case 6): voltage answers voltage_phase; hp answers hp.
        assert variant_attr_answered(specs, "voltage_phase") is True
        assert variant_attr_answered(specs, "hp") is True
        # And the confirm guard now passes (no block).
        assert family_disambig_block(specs) is None

    def test_bearing_model_no_pn_legacy_bore_diameter_ask(self, monkeypatch):
        """Case 5a (dual-source generalization): a bearing is OFF the 5-profile
        registry -> classified unknown -> the legacy CATEGORY_REQUIRED_FIELDS
        fallback yields bore_diameter. The variant ask still fires (constructed
        question, names the family)."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        agent = IntakeAgent(anthropic_api_key="test-key")
        payload = {
            "manufacturer":            "SKF",
            "model":                   "6205",        # family-level (no PN)
            "part_number":             None,
            "detected_type":           "bearing",
            "category":                "Part",
            "bore_diameter":           None,
            "manufacturer_confidence": 90,
            "part_id_confidence":      82,
            "confidence_reasoning":    "model stated, no catalog number",
        }
        result = _run(agent, {}, "Need an SKF 6205 bearing",
                      payload, _clf("unknown", confidence=20))
        assert result["sufficient"] is False
        assert result["confidence_summary"]["missing_field"] == "variant_disambig"
        follow_up = result["follow_up_question"]
        # Constructed path names the family + the legacy dim (bore diameter).
        assert "6205" in follow_up
        assert "bore diameter" in follow_up
        assert result["asset_specs"]["_variant_disambig_pending"] is True

    def test_pump_model_no_pn_hydraulic_duty_ask(self, monkeypatch):
        """Case 5b (dual-source generalization): a pump model-no-PN, classified
        pump -> registry variant_selecting_attrs=[hydraulic_duty]. The variant
        ask fires with the q2_template path (family fact prepended)."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        agent = IntakeAgent(anthropic_api_key="test-key")
        payload = {
            "manufacturer":            "Goulds",
            "model":                   "3196",        # family-level (no PN)
            "part_number":             None,
            "detected_type":           "centrifugal pump",
            "category":                "Equipment",
            "gpm":                     None,
            "psi":                     None,
            "hp":                      None,
            "manufacturer_confidence": 90,
            "part_id_confidence":      82,
            "confidence_reasoning":    "manufacturer + model stated, no catalog number",
        }
        result = _run(agent, {}, "Need a Goulds 3196 pump",
                      payload, _clf("pump"))
        assert result["sufficient"] is False
        assert result["confidence_summary"]["missing_field"] == "variant_disambig"
        follow_up = result["follow_up_question"]
        # q2_template path: family fact prepended + the pump template (names duty).
        assert "3196" in follow_up
        assert "duty" in follow_up
        assert result["asset_specs"]["_variant_disambig_pending"] is True

    def test_spec_described_no_model_unaffected_at_run_gate(self, monkeypatch):
        """Case 4 (run gate): a spec-described request with NO model is NOT
        family-level -> the variant ask never fires, pending is not set, and the
        family-variant block returns None (the spec-based path is untouched)."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        agent = IntakeAgent(anthropic_api_key="test-key")
        payload = {
            "manufacturer":            None,
            "model":                   None,          # spec-described (no identity)
            "part_number":             None,
            "detected_type":           "ball valve",
            "category":                "Part",
            "connection_size":         "2 inch",
            "manufacturer_confidence": 0,
            "part_id_confidence":      75,
            "confidence_reasoning":    "type + dims, no identity",
        }
        result = _run(agent, {}, "Need a 2 inch ball valve",
                      payload, _clf("valve"))
        specs = result["asset_specs"]
        # No variant ask.
        assert result["confidence_summary"]["missing_field"] != "variant_disambig"
        assert specs.get("_variant_disambig_pending") is not True
        # The confirm guard sees a non-family-level request -> no block.
        assert family_disambig_block(specs) is None

    # -- flag-off constructed path (the deploy branch's default) -------------

    def test_pf40_flag_off_constructed_path_names_family_and_attrs(self, monkeypatch):
        """Flag-off is what the deploy branch runs (INTAKE_TYPE_AWARE default
        OFF). Deliberate coverage — not a mock accident: with the flag OFF the
        classifier never runs (no _classified_type), so _variant_selecting_attrs_for
        falls to the legacy CATEGORY_REQUIRED_FIELDS fallback (VFD -> voltage, hp)
        and _variant_disambiguation_question takes the CONSTRUCTED path (no
        q2_template). The constructed question names the family + the
        variant-selecting attrs using _VARIANT_ATTR_LABELS (hp -> "HP/kW rating")."""
        monkeypatch.delenv("INTAKE_TYPE_AWARE", raising=False)
        agent = IntakeAgent(anthropic_api_key="test-key")
        result = _run(agent, {}, "Need an Allen-Bradley PowerFlex 40",
                      _pf40_payload(), _clf("motor_drive"))
        # Flag-off -> classifier never invoked -> no classification keys; the
        # mock classifier is inert here (the gate `if _intake_type_aware()` is False).
        assert result["sufficient"] is False
        assert result["confidence_summary"]["missing_field"] == "variant_disambig"
        follow_up = result["follow_up_question"]
        assert "PowerFlex 40" in follow_up            # names the family
        assert "product family" in follow_up.lower()  # the family-fact phrasing
        # The constructed path uses _VARIANT_ATTR_LABELS (hp -> "HP/kW rating",
        # voltage -> "voltage") — the variant-selecting attrs, named.
        assert "HP/kW" in follow_up
        assert "voltage" in follow_up.lower()
        assert result["asset_specs"]["_variant_disambig_pending"] is True
        assert "_q2_variant" in result["asset_specs"]["_asked_fields"]

    # -- SCOPE invariant (the T4 fix) ----------------------------------------

    def test_no_identity_q2_flow_returns_template_verbatim(self, monkeypatch):
        """SCOPE: the original no-identity q2 flow (_next_clarification's
        `_q2_asked` branch) returns the registry q2_template VERBATIM — no
        'is a product family' prepend, no model name. Proves the T4 fix touched
        ONLY the variant-disambiguation usage, not this path."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        agent = IntakeAgent(anthropic_api_key="test-key")
        # No identity, known type (valve), but missing a dimension (connection_size)
        # -> needs_clarification, no identity -> the `_q2_asked` branch fires and
        # returns the valve q2_template verbatim.
        payload = {
            "manufacturer":            None,
            "model":                   None,
            "part_number":             None,
            "detected_type":           "ball valve",
            "category":                "Part",
            "connection_size":         None,          # missing -> not spec-based-ready
            "manufacturer_confidence": 0,
            "part_id_confidence":      75,            # <80 -> blocked_need_either
            "confidence_reasoning":    "type only, no dims, no identity",
        }
        result = _run(agent, {}, "Need a ball valve",
                      payload, _clf("valve"))
        follow_up = result["follow_up_question"]
        valve_template = get_profile("valve").q2_template
        assert follow_up == valve_template
        # The variant-disambiguation prepend is NOT present on this path.
        assert "product family" not in follow_up
        assert result["asset_specs"].get("_variant_disambig_pending") is not True


# ---------------------------------------------------------------------------
# Registry-level: the attr->field mapping (case 6).
# ---------------------------------------------------------------------------

class TestVariantAttrFieldMapping:
    """Case 6: a user answering '480V' satisfies the motor_drive guard because
    voltage_phase is answered via `voltage` (the VARIANT_ATTR_TO_SPEC_FIELDS
    table maps voltage_phase -> (voltage, phase))."""

    def test_voltage_answers_voltage_phase(self):
        assert variant_attr_answered({"voltage": "480V"}, "voltage_phase") is True

    def test_phase_answers_voltage_phase(self):
        assert variant_attr_answered({"phase": "3-phase"}, "voltage_phase") is True

    def test_null_voltage_does_not_answer(self):
        assert variant_attr_answered({"voltage": None}, "voltage_phase") is False
        assert variant_attr_answered({"voltage": "Unknown"}, "voltage_phase") is False

    def test_hp_answered(self):
        assert variant_attr_answered({"hp": "5"}, "hp") is True

    def test_hydraulic_duty_any_one_signal_answers(self):
        # gpm OR psi OR head OR hp answers hydraulic_duty.
        assert variant_attr_answered({"gpm": "32"}, "hydraulic_duty") is True
        assert variant_attr_answered({"hp": "15"}, "hydraulic_duty") is True
        assert variant_attr_answered({"gpm": None, "psi": None}, "hydraulic_duty") is False


# ---------------------------------------------------------------------------
# Confirm-intake integration: specs from a REAL run() -> the REAL endpoint.
# ---------------------------------------------------------------------------

# Local `api` fixture (mirrors the proven test_api_server.py one — that fixture
# is module-local and not shared; copied verbatim so these tests drive the REAL
# api_server confirm_intake handler).
@pytest.fixture
def api(tmp_path, monkeypatch):
    from utils.procurement_agent.state import persistence

    engine = persistence._make_engine(f"sqlite:///{tmp_path / 'api.sqlite'}")
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    persistence.Base.metadata.create_all(engine)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("DEMO_MODE", "0")

    monkeypatch.setattr(persistence, "_engine", engine)
    monkeypatch.setattr(persistence, "_SessionFactory", TestSession)

    from utils import supplier_registry
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))

    import api_server

    monkeypatch.setattr(api_server, "_engine", engine)
    monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
    monkeypatch.setattr(api_server, "_messages", {})
    monkeypatch.setattr(api_server, "DEMO_MODE", False)

    from fastapi.testclient import TestClient
    client = TestClient(api_server.app)
    client._api_server = api_server
    return client


from .test_api_server import (
    _create_run, _set_run, _mock_sourcing_pipeline, _empty_sourcing,
    _read_asset_specs,
)


def _pf40_run_specs(monkeypatch, *, answered=False, with_pn=False) -> dict:
    """Produce PowerFlex 40 specs via a REAL IntakeAgent.run() flow (extraction +
    classifier mocked), the live-faithful way. One turn by default (the variant
    ask issued, unanswered -> pending + missing attrs); two turns when
    `answered` (the user answers 5HP 480V -> resolved + attrs filled); `with_pn`
    seeds a real catalog number (escape hatch -> not family-level)."""
    monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
    agent = IntakeAgent(anthropic_api_key="test-key")
    if with_pn:
        payload = _pf40_payload(part_number="22B-D010N104",
                                hp="5", voltage="480V")
        t1 = _run(agent, {}, "PowerFlex 40 22B-D010N104",
                  payload, _clf("motor_drive"))
        return t1["asset_specs"]
    t1 = _run(agent, {}, "Need an Allen-Bradley PowerFlex 40",
              _pf40_payload(), _clf("motor_drive"))
    if not answered:
        return t1["asset_specs"]
    # Turn 2 — the user answers the rating.
    t2 = _run(agent, t1["asset_specs"], "5HP 480V",
              _pf40_payload(hp="5", voltage="480V"), clf=None)
    return t2["asset_specs"]


class TestVariantDisambigConfirmIntakeLive:
    """Cases 2/3 (+ escape hatch, spec-described): the REAL confirm_intake
    endpoint against specs a real run() produced."""

    def test_run_pending_then_confirm_422(self, api, monkeypatch):
        """Case 2: run() produced the variant ask (pending, attrs empty) -> the
        real confirm_intake endpoint 422s with the FULL frontend-consumable shape
        (the contract T5 builds against): reason, model, missing_attrs (registry
        labels), missing_labels (human labels), pending, message."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        rid = _create_run(api)
        specs = _pf40_run_specs(monkeypatch, answered=False)
        _set_run(api, rid, asset_specs_json=json.dumps(specs))

        resp = api.post(f"/api/runs/{rid}/confirm-intake")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["reason"] == "family_variant_unconfirmed"
        assert detail["model"] == "PowerFlex 40"
        # missing_attrs: the registry-side variant-selecting attrs.
        assert "hp" in detail["missing_attrs"]
        assert "voltage_phase" in detail["missing_attrs"]
        # missing_labels: the human-readable labels T5 renders (the same
        # _VARIANT_ATTR_LABELS the question uses: hp -> "HP/kW rating",
        # voltage_phase -> "voltage/phase").
        assert "HP/kW rating" in detail["missing_labels"]
        assert "voltage/phase" in detail["missing_labels"]
        assert detail["pending"] is True
        # message: a plain string that names the family (T5's follow-up text).
        assert isinstance(detail["message"], str)
        assert "PowerFlex 40" in detail["message"]
        # Run stays in intake (the guard is before the phase mutate).
        assert api.get(f"/api/runs/{rid}").json()["phase"] == "intake"

    def test_run_pending_then_open_family_200_honest_markers(self, api, monkeypatch):
        """Case 3: open_family=true on the pending run -> 200 and the honest
        open-family markers (family_open_commit + spec_based_sourcing) recorded."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        rid = _create_run(api)
        specs = _pf40_run_specs(monkeypatch, answered=False)
        _set_run(api, rid, asset_specs_json=json.dumps(specs))
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=_empty_sourcing())

        resp = api.post(f"/api/runs/{rid}/confirm-intake?open_family=true")
        assert resp.status_code == 200
        saved = _read_asset_specs(api, rid)
        assert saved.get("family_open_commit") is True
        assert saved.get("spec_based_sourcing") is True

    def test_run_answered_then_confirm_200(self, api, monkeypatch):
        """After the user answers 5HP 480V (run() turn 2), confirm_intake
        proceeds 200 — the variant-selecting attrs are answered via the mapping."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        rid = _create_run(api)
        specs = _pf40_run_specs(monkeypatch, answered=True)
        _set_run(api, rid, asset_specs_json=json.dumps(specs))
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=_empty_sourcing())

        resp = api.post(f"/api/runs/{rid}/confirm-intake")
        assert resp.status_code == 200
        assert resp.json()["phase"] == "sourcing"

    def test_run_pn_present_then_confirm_200_escape_hatch(self, api, monkeypatch):
        """The PN escape hatch: run() with a real catalog number (22B-D010N104)
        is NOT family-level -> confirm proceeds 200 byte-identically (the guard
        never fires even though a variant ask might be pending)."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        rid = _create_run(api)
        specs = _pf40_run_specs(monkeypatch, with_pn=True)
        _set_run(api, rid, asset_specs_json=json.dumps(specs))
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=_empty_sourcing())

        resp = api.post(f"/api/runs/{rid}/confirm-intake")
        assert resp.status_code == 200
        assert resp.json()["phase"] == "sourcing"

    def test_run_hallucinated_then_confirm_422_pending(self, api, monkeypatch):
        """Case 7 (confirm side): run() with an extractor-filled rating that the
        user never engaged (pending True) -> confirm 422 even though the attrs
        are filled (the anti-hallucination property at the endpoint)."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        rid = _create_run(api)
        agent = IntakeAgent(anthropic_api_key="test-key")
        # Turn 1 — extractor hallucinated hp+voltage; the variant ask still fires
        # (block-regardless-of-extracted) with pending=True.
        t1 = _run(agent, {}, "Need an Allen-Bradley PowerFlex 40",
                  _pf40_payload(hp="5", voltage="480V"), _clf("motor_drive"))
        specs = t1["asset_specs"]
        assert specs["_variant_disambig_pending"] is True
        _set_run(api, rid, asset_specs_json=json.dumps(specs))

        resp = api.post(f"/api/runs/{rid}/confirm-intake")
        assert resp.status_code == 422
        assert resp.json()["detail"]["pending"] is True

    def test_run_spec_described_then_confirm_200_unaffected(self, api, monkeypatch):
        """Case 4 (confirm gate): a spec-described run (no model) -> confirm 200
        (the guard never fires — not family-level)."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        agent = IntakeAgent(anthropic_api_key="test-key")
        payload = {
            "manufacturer":            None,
            "model":                   None,
            "part_number":             None,
            "detected_type":           "ball valve",
            "category":                "Part",
            "connection_size":         "2 inch",
            "manufacturer_confidence": 0,
            "part_id_confidence":      78,
            "confidence_reasoning":    "type + dims, no identity",
        }
        specs = _run(agent, {}, "Need a 2 inch ball valve",
                     payload, _clf("valve"))["asset_specs"]
        rid = _create_run(api)
        _set_run(api, rid, asset_specs_json=json.dumps(specs))
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=_empty_sourcing())

        resp = api.post(f"/api/runs/{rid}/confirm-intake")
        assert resp.status_code == 200
