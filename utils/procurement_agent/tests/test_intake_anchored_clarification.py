"""Night 7 fix-night acceptance tests — ANCHORED-component over-clarification.

Wires the labelled eval cases from ``audit/night7_matching_eval_cases.json``
(family ``B_goulds_no_redundant_clarification``) as REAL tests against the
live ``IntakeAgent.run()`` path (extraction ``requests.post`` + classifier
mocked, ``INTAKE_TYPE_AWARE`` on). The audit's ``_meta`` states these cases
are diagnosis artefacts NOT yet wired into pytest — this module is that wiring.

Root cause it locks down (audit §B1 / H1, confirmed by reproduction): for an
ANCHORED component (mechanical seal) the classifier captures the PARENT
identity in ``_component_of`` ("Goulds 3196" @ conf 95), but
``_next_clarification`` gated the registry ``q2_template`` on
``not _has_identity(merged)`` and ``_has_identity`` checks ONLY
manufacturer/model/part_number — null by design for an ANCHORED component — so
the q2 fired verbatim, asking "What pump make/model is it on?" (the parent
that is already stated).

The fix: for an ANCHORED component whose parent is already captured at
reasonable confidence, suppress the parent-identity half of the q2 and lead
with the genuinely-undetermined component dims (the registry
``q2_component_clause``). DIRECT types and the no-parent ANCHORED case keep
the verbatim ``q2_template``. The ``blocked_need_either`` / proceed-state bar
is unchanged — the turn still returns a question, never a commit.

Acceptance (per FIX_GOULDS_ANCHORED_CLARIFICATION_BRIEF.md):
  1. Goulds 3196 seal kit, turn 1 → does NOT ask "what pump make/model is it on?".
  2. Same, turn 1 → first clarification is a genuine component dim (shaft size /
     cartridge-vs-component / single-vs-double), NOT the parent, NOT "is it OEM?".
  3. Legit dims (face material / elastomer / size) still fire when genuinely
     underdetermined — the fix does not over-suppress into proceeding blind.
  4. Parent NOT stated ("mechanical seal" with no pump) → the parent question
     STILL fires (guards against over-suppression).
  5. DIRECT family-level (PowerFlex 40) variant-disambig behaviour unchanged —
     covered by test_intake_variant_disambig.py (asserted here as a smoke guard
     that the DIRECT path is untouched by this fix's registry additions).
  6. Full suite ≥ 1864/73 + these tests, green (verified by the run, not asserted
     in-file).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from utils.models import SourcingRun
from utils.procurement_agent.agents.intake_agent import IntakeAgent
from utils.procurement_agent.part_type_classifier import Classification
from utils.procurement_agent.part_type_registry import (
    REGIME_ANCHORED,
    get_profile,
)


# ---------------------------------------------------------------------------
# Eval-cases fixture — load the Night 7 labelled cases (the file the audit
# produced). Its _meta states these are NOT yet wired into pytest; this module
# is that wiring. We index by case id so a missing/mislabelled case fails loudly.
# ---------------------------------------------------------------------------

def _eval_cases() -> dict:
    repo_root = Path(__file__).resolve().parents[3]   # tests/ -> procurement_agent -> utils -> root
    path = repo_root / "audit" / "night7_matching_eval_cases.json"
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _eval_case(case_id: str) -> dict:
    cases = {c["id"]: c for c in _eval_cases()["cases"]}
    assert case_id in cases, f"eval case {case_id!r} not found in night7_matching_eval_cases.json"
    return cases[case_id]


# ---------------------------------------------------------------------------
# Run-path helpers (mirror the proven test_intake_variant_disambig.py pattern).
# ---------------------------------------------------------------------------

def _make_run(specs: dict | None = None) -> SourcingRun:
    return SourcingRun(asset_specs_json=specs)


def _mock_anthropic_response(payload: dict) -> MagicMock:
    """A mocked requests.post return value whose body is the given extraction
    payload (the shape IntakeAgent._extract_text parses). The SAME mock serves
    _generate_clarification on the generic-walk turns — its text is then
    mock-polluted, so generic-walk turns are asserted via the asked-ledger /
    missing_field, not follow_up text (the anchored q2 path returns its
    template directly, so turn-1 text IS assertable)."""
    mock_resp = MagicMock()
    mock_resp.raise_d_status = MagicMock()
    mock_resp.json.return_value = {"content": [{"text": json.dumps(payload)}]}
    return mock_resp


def _clf(part_type: str, confidence: int = 95, component_of=None) -> Classification:
    """A Classification for the given registry part_type (regime/sourcing
    resolved from the registry so it matches what the real classifier stores)."""
    p = get_profile(part_type)
    return Classification(
        part_type=part_type,
        regime=p.regime,
        sourcing=p.sourcing,
        component_of=component_of,
        confidence=confidence,
    )


def _run(agent: IntakeAgent, prior: dict | None, text: str,
         payload: dict, clf: Classification | None) -> dict:
    """One IntakeAgent.run() turn with extraction + classification mocked."""
    prior = prior or {}
    with patch("utils.procurement_agent.agents.intake_agent.requests.post",
               return_value=_mock_anthropic_response(payload)):
        if clf is not None:
            with patch("utils.procurement_agent.part_type_classifier.classify_part_type",
                       return_value=clf):
                return agent.run(_make_run(prior), {"text": text, "images": []})
        # Follow-up turn: classifier is not re-invoked (prior_specs non-empty);
        # stub it to UNKNOWN so it never reaches the network by accident.
        with patch("utils.procurement_agent.part_type_classifier.classify_part_type",
                   return_value=_clf("unknown", confidence=0)):
            return agent.run(_make_run(prior), {"text": text, "images": []})


# A Goulds-3196 seal-kit extraction payload. The COMPONENT-OF prompt rule means
# the extractor does NOT attribute the parent's OEM to the component, so
# manufacturer/model/part_number are null by design (the parent is in
# _component_of). part_id_confidence=55 mirrors the audit's observed turn-1.
def _goulds_seal_kit_payload(**over) -> dict:
    base = {
        "manufacturer":            None,
        "model":                   None,
        "part_number":             None,
        "detected_type":           "mechanical seal",
        "category":                "Part",
        "shaft_size":              None,
        "material_spec":           None,
        "manufacturer_confidence": 0,
        "part_id_confidence":      55,
        "confidence_reasoning":    "seal for Goulds 3196; component-of rule -> mfg null",
    }
    base.update(over)
    return base


_GOULDS_3196_CLF = _clf("mechanical_seal", confidence=95, component_of="Goulds 3196")


# ---------------------------------------------------------------------------
# Acceptance cases 1 & 2 — turn-1 of "Goulds 3196 mechanical seal kit".
# ---------------------------------------------------------------------------

class TestAnchoredComponentTurnOne:
    """Cases 1 + 2: the primary founder-reported regression and the
    lead-with-a-dim requirement."""

    def test_goulds_3196_turn1_does_not_ask_parent(self, monkeypatch):
        """Case 1: turn-1 must NOT ask 'what pump make/model is it on?' — the
        parent is already captured in _component_of. Loads the eval case to
        bind the assertion to the audit's labelled input."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        case = _eval_case("B-goulds-3196-no-redundant-clarification")
        agent = IntakeAgent(anthropic_api_key="test-key")
        result = _run(agent, {}, case["input_text"],
                      _goulds_seal_kit_payload(), _GOULDS_3196_CLF)

        assert result["sufficient"] is False
        # The parent is captured — the precondition for suppression.
        assert result["asset_specs"]["_component_of"] == "Goulds 3196"
        assert result["asset_specs"]["_classified_regime"] == REGIME_ANCHORED
        follow_up = result["follow_up_question"]
        assert follow_up is not None
        low = follow_up.lower()
        # The redundant parent ask must NOT appear.
        assert "pump make/model" not in low
        assert "what pump is it on" not in low
        assert "what pump" not in low

    def test_goulds_3196_turn1_leads_with_component_dim(self, monkeypatch):
        """Case 2: turn-1's first clarification is a genuinely-undetermined
        COMPONENT dim (shaft size / cartridge-vs-component / single-vs-double),
        NOT the parent, NOT 'is it OEM?'."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        case = _eval_case("B-goulds-3196-no-redundant-clarification")
        agent = IntakeAgent(anthropic_api_key="test-key")
        result = _run(agent, {}, case["input_text"],
                      _goulds_seal_kit_payload(), _GOULDS_3196_CLF)
        follow_up = result["follow_up_question"]
        assert follow_up is not None
        low = follow_up.lower()
        # Leads with a genuine component dim — shaft size is the lead dim in the
        # registry q2_component_clause.
        assert "shaft size" in low
        # The cartridge-vs-component and single-vs-double dims are present.
        assert "cartridge" in low and "component seal" in low
        assert "single" in low and "double" in low
        # NOT an OEM ask (the brief's explicit exclusion).
        assert "is it oem" not in low
        # NOT a parent ask (re-checked here independent of case 1's phrasings).
        assert "pump make/model" not in low
        # The anchored q2 was issued this turn (de-dup ledger), not the generic walk.
        assert "_q2_asked" in result["asset_specs"]["_asked_fields"]
        # The proceed-state bar is unchanged — still blocked, still asking.
        assert result["confidence_summary"]["proceed_state"] == "blocked_need_either"


# ---------------------------------------------------------------------------
# Acceptance case 3 — legit dims still fire when genuinely underdetermined.
# ---------------------------------------------------------------------------

class TestAnchoredComponentLegitDimsStillAsked:
    """Case 3: suppressing the redundant parent ask must NOT over-suppress into
    proceeding blind. The genuinely-undetermined component dims (here
    material_spec — a CATEGORY_REQUIRED_FIELDS dim the q2_component_clause does
    NOT cover) must still fire when missing. Proven via the asked-ledger: from a
    prior state where the anchored q2 + manufacturer are already asked and
    shaft_size is known but material_spec is missing, the next turn (under the
    INTAKE_TURN_CAP) advances the never-re-ask picker to material_spec."""

    def test_material_spec_still_asked_when_missing(self, monkeypatch):
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        agent = IntakeAgent(anthropic_api_key="test-key")
        # Prior: turn 1 already ran (anchored q2 issued + manufacturer asked),
        # shaft_size answered, material_spec still genuinely missing. this_turn
        # will be 2 — under the INTAKE_TURN_CAP (3) so it asks, doesn't commit.
        prior = {
            "_classified_type":       "mechanical_seal",
            "_classified_regime":     REGIME_ANCHORED,
            "_component_of":          "Goulds 3196",
            "_classified_confidence": 95,
            "_asked_fields":          ["_q2_asked", "manufacturer"],
            "_intake_turns":          1,
            "detected_type":          "mechanical seal",
            "category":               "Part",
            "shaft_size":             "1-5/8",
            "material_spec":          None,
            "manufacturer":           None,
            "model":                  None,
            "part_number":            None,
            "manufacturer_confidence": 0,
            "part_id_confidence":     70,
        }
        result = _run(agent, prior, "not sure on the face material",
                      _goulds_seal_kit_payload(
                          shaft_size="1-5/8",
                          part_id_confidence=70,
                      ),
                      _clf("mechanical_seal", confidence=95, component_of="Goulds 3196"))
        # Still blocked — did not proceed blind.
        assert result["sufficient"] is False
        # The never-re-ask picker advanced to the genuinely-missing dim.
        assert "material_spec" in result["asset_specs"]["_asked_fields"]

    def test_turn1_anchored_question_covers_blocking_dims(self, monkeypatch):
        """Case 3 (complement): the turn-1 anchored question itself carries the
        genuinely-undetermined component dims (shaft / cartridge / single) — the
        fix leads with dims, it does not suppress them. material_class is left
        for the generic walk (above), matching the audit's 'across the
        sequence' union."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        case = _eval_case("B-goulds-3196-legit-clarifications-still-asked")
        agent = IntakeAgent(anthropic_api_key="test-key")
        result = _run(agent, {}, case["input_text"],
                      _goulds_seal_kit_payload(), _GOULDS_3196_CLF)
        low = (result["follow_up_question"] or "").lower()
        assert "shaft size" in low
        assert ("cartridge" in low and "single" in low)


# ---------------------------------------------------------------------------
# Acceptance case 4 — parent NOT stated → the parent question STILL fires.
# ---------------------------------------------------------------------------

class TestAnchoredComponentNoParentStillAsksParent:
    """Case 4: the suppression is gated on _component_of being set. When the
    parent is NOT stated, the verbatim q2_template (which asks for the parent)
    must still fire — guards against over-suppression."""

    def test_bare_mechanical_seal_asks_for_parent(self, monkeypatch):
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        agent = IntakeAgent(anthropic_api_key="test-key")
        # No parent named -> component_of is None (the classifier's rule).
        result = _run(agent, {}, "mechanical seal",
                      _goulds_seal_kit_payload(),
                      _clf("mechanical_seal", confidence=90, component_of=None))
        follow_up = result["follow_up_question"]
        assert follow_up is not None
        low = follow_up.lower()
        # The parent ask fires (the verbatim q2_template).
        assert "pump make/model" in low or "what pump" in low
        # No suppression happened — _component_of is absent.
        assert result["asset_specs"].get("_component_of") in (None, "")

    def test_low_confidence_parent_capture_still_asks(self, monkeypatch):
        """Case 4 (guard breadth): a parent captured only at LOW confidence
        (below the ANCHORED_PARENT_KNOWN_CONF floor) must still ask for it — an
        uncertain parent capture should not be trusted to suppress the ask."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        agent = IntakeAgent(anthropic_api_key="test-key")
        result = _run(agent, {}, "some pump mechanical seal",
                      _goulds_seal_kit_payload(),
                      _clf("mechanical_seal", confidence=40, component_of="Acme Pump"))
        follow_up = result["follow_up_question"]
        assert follow_up is not None
        low = follow_up.lower()
        assert "pump make/model" in low or "what pump" in low


# ---------------------------------------------------------------------------
# Acceptance case 5 — DIRECT family-level path unchanged (smoke guard).
# ---------------------------------------------------------------------------

class TestDirectVariantDisambigUntouched:
    """Case 5: the DIRECT / family-level variant_disambig path
    (PowerFlex 40) is a separate code path and must be unaffected by this fix.
    The full behavioural coverage lives in test_intake_variant_disambig.py; this
    is a smoke guard that the registry additions (q2_component_clause) did not
    perturb the DIRECT q2 flow — a DIRECT type's q2_template is still returned
    verbatim, and the new field is empty for DIRECT types."""

    def test_direct_type_q2_component_clause_empty(self):
        # DIRECT types do not carry the ANCHORED component clause.
        for ptype in ("pump", "valve", "sensor_instrument", "motor_drive"):
            profile = get_profile(ptype)
            assert profile.regime != REGIME_ANCHORED
            assert profile.q2_component_clause == ""

    def test_direct_valve_q2_template_still_verbatim(self, monkeypatch):
        """Mirrors test_intake_variant_disambig.test_no_identity_q2_flow_
        returns_template_verbatim: a DIRECT type with no identity returns its
        q2_template VERBATIM (no anchored suppression, no prepend)."""
        monkeypatch.setenv("INTAKE_TYPE_AWARE", "1")
        agent = IntakeAgent(anthropic_api_key="test-key")
        payload = {
            "manufacturer":            None,
            "model":                   None,
            "part_number":             None,
            "detected_type":           "ball valve",
            "category":                "Part",
            "connection_size":         None,
            "manufacturer_confidence": 0,
            "part_id_confidence":      75,   # <80 -> blocked_need_either
            "confidence_reasoning":    "type only, no dims, no identity",
        }
        result = _run(agent, {}, "Need a ball valve", payload, _clf("valve"))
        assert result["follow_up_question"] == get_profile("valve").q2_template
        # No anchored suppression markers on the DIRECT path.
        assert result["asset_specs"].get("_classified_regime") != REGIME_ANCHORED
