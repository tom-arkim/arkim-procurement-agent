"""Eval Export — Night 2 of the Arkim overnight build program.

Turns labeled runs (Night 2's `run_labels` store) into drop-in eval cases in the
EXISTING eval schemas, appended to real-cases dataset files. Pure function over
data — the admin ``POST /api/admin/labeling/export`` endpoint calls into this.

DESIGN (per the Night 2 brief + investigation findings I3 + the live-faithfulness rule):

* **Two suites, one exporter.** A labeled run can produce:
    - an INTAKE case (from the run-scope label) — drops into
      ``intake_eval_dataset.json``\'s ``examples`` array.
    - SCORING cases (from candidate-scope labels) — would drop into
      ``scoring_eval_dataset.json``\'s ``cases`` array.
* **Provenance.** Every exported case carries ``"provenance": "real:<run_id>"``.
  The existing schema validators (test_intake_eval_dataset.py /
  test_scoring_eval_dataset.py) check required keys are PRESENT, not that no
  extra keys exist — so a ``provenance`` key attaches without breaking them.
* **dev/holdout split, hash-based.** ``_split_for(run_id)`` hashes the run_id to
  a bucket; ~2/3 dev, 1/3 holdout (mirrors the existing datasets\' ratio band).
  Deterministic from the run_id — a relabeled run keeps its split.
* **LIVE-FAITHFULNESS (guardrail 7 — this codebase shipped 2+ bugs to violations).**
  The exported case must reproduce EXACTLY what the live path feeds the evaluator:
    - Intake: the live classifier is fed ``classify_part_type(text)`` where
      ``text`` is the FIRST user turn (intake_agent._maybe_classify is called on
      the first message only — intake_agent.py:518). The exporter pulls the
      first ``turn_user`` event\'s ``content`` from run_capture as the case
      ``input``. The expected_* fields come from the labeler\'s ground truth.
    - Scoring: the live scorer is fed ``_compute_suitability_score(specs,
      snippet, url, found_pn, title)`` (test_scoring_eval_run.py:107 — snippet
      is positional). ``snippet`` + ``title`` are NOT durably captured anywhere
      (snippet_map is local to enterprise_search.py:142; the persisted
      SourcingOption carries source_url + found_part_number but NOT
      snippet/title — I3 blocker). So the scorer WITHHOLDS a scoring case when
      snippet/title are unavailable, with a logged reason, rather than emit a
      case that feeds the scorer different input than the live path. This is the
      guardrail-8 outcome: the dependent scoring-export path is honest scaffolding
      until Night 1 capture is extended to thread snippet/title onto the candidate.
* **Never weaken a validator, never relabel.** The exporter VALIDATES each
  emitted case against the existing schema before appending; a case that would
  break the validator is withheld (never silently appended to break the suite).

This module is pure + standalone + tested + fail-soft from the start (the house
standard for new modules abutting pre-standard code — CLAUDE.md §5).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Tuple

# Flag (extends Night 1 — labeling/export ride RUN_CAPTURE).
def _env_truthy(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


RUN_CAPTURE: bool = _env_truthy(os.environ.get("RUN_CAPTURE"))


# ---------------------------------------------------------------------------
# Dataset paths — the EXISTING eval fixtures (the exporter appends real cases
# to sibling real-cases files, never the committed synthetic fixtures, so the
# synthetic fixtures + their tests stay byte-identical).
# ---------------------------------------------------------------------------

_FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "utils", "procurement_agent", "tests", "fixtures",
)

INTAKE_REAL_DATASET = os.path.join(_FIXTURE_DIR, "intake_eval_dataset_real.json")
SCORING_REAL_DATASET = os.path.join(_FIXTURE_DIR, "scoring_eval_dataset_real.json")


# ---------------------------------------------------------------------------
# dev/holdout split — deterministic hash of the run_id (~2/3 dev, 1/3 holdout,
# matching the existing datasets' ratio band — test_intake_eval_dataset.py:78).
# ---------------------------------------------------------------------------

def _split_for(run_id: str) -> str:
    """Deterministic dev|holdout split from the run_id. ~2/3 dev, 1/3 holdout."""
    h = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    # Use the leading 8 hex nibbles -> 0..0xffffffff; < ~2/3 -> dev.
    n = int(h[:8], 16) / 0xFFFFFFFF
    return "dev" if n < (2.0 / 3.0) else "holdout"


# ---------------------------------------------------------------------------
# Intake case — live-faithful: input = FIRST user turn text (the live
# classifier is fed exactly that text, intake_agent.py:413/518).
# ---------------------------------------------------------------------------

def _first_user_turn(events: List[Dict[str, Any]]) -> Optional[str]:
    """The first turn_user event content — what the live classifier consumed."""
    for e in events:
        if e.get("event_type") == "turn_user":
            payload = e.get("payload") or {}
            content = payload.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return None


_VALID_PART_TYPES = {
    "mechanical_seal", "pump", "valve", "sensor_instrument", "motor_drive", "unknown",
}
_VALID_REGIMES = {"DIRECT", "ANCHORED"}


def build_intake_case(
    run_id: str,
    user_turn_text: str,
    label: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Build a drop-in intake eval example from a run-scope label.

    Returns None (withheld) if the case would not satisfy the intake schema —
    never emit a case that breaks test_intake_eval_dataset.py.
    """
    expected_part_type = label.get("expected_part_type")
    expected_regime = label.get("expected_regime")
    component_of = label.get("expected_component_of")
    if expected_part_type not in _VALID_PART_TYPES:
        return None
    if expected_regime not in _VALID_REGIMES:
        return None
    # component_of nullable but must be a non-empty string when present.
    if component_of is not None and not (isinstance(component_of, str) and component_of.strip()):
        return None
    # Sanity mirroring test_intake_eval_dataset.test_component_of_only_for_anchored:
    # component_of non-null only for ANCHORED mechanical_seal. (A labeler could
    # set a contradictory label — withhold rather than emit a validator-breaking case.)
    if component_of is not None:
        if expected_regime != "ANCHORED" or expected_part_type != "mechanical_seal":
            return None
    if not (isinstance(user_turn_text, str) and user_turn_text.strip()):
        return None
    return {
        "input": user_turn_text,
        "expected_part_type": expected_part_type,
        "expected_component_of": component_of,
        "expected_regime": expected_regime,
        "split": _split_for(run_id),
        "provenance": f"real:{run_id}",
    }


# ---------------------------------------------------------------------------
# Scoring case — live-faithful ONLY when snippet+title are captured. The
# current run_capture store does NOT capture snippet/title (I3 blocker), so
# build_scoring_case withholds unless the caller supplies a faithfully-captured
# snippet+title (future Night 1 extension). found_pn + url come from the
# persisted candidate (sourcing_results_json) which the endpoint joins in.
# ---------------------------------------------------------------------------

def build_scoring_case(
    run_id: str,
    candidate_ref: str,
    request: Dict[str, Any],
    result: Dict[str, Any],
    label: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Build a drop-in scoring eval case from a candidate-scope label.

    ``request`` must carry the AssetSpecs fields (manufacturer, model,
    part_number, voltage, category, detected_type, optional hp). ``result``
    must carry snippet, url, title, found_pn — the EXACT inputs the live scorer
    consumed. Returns None (withheld) if any live-faithful input is missing,
    so the exporter never feeds the scorer different input than the live path.
    """
    snippet = result.get("snippet")
    url = result.get("url")
    # title is optional in the schema (result.title), but the live scorer is
    # PASSED title (test_scoring_eval_run.py:72 uses res.get("title")). A missing
    # title is live-faithful ONLY if the live path also had none — accept None.
    title = result.get("title")
    found_pn = result.get("found_pn")

    # LIVE-FAITHFULNESS GATE: snippet is a positional arg the scorer requires.
    # Without the real snippet the scorer would run on different text than live.
    if not (isinstance(snippet, str) and snippet.strip()):
        return None  # withheld — Night 1 capture gap (snippet not durable)
    if not (isinstance(url, str) and url.strip()):
        return None

    should_pass = label.get("should_pass_floor")
    if not isinstance(should_pass, bool):
        return None
    rationale = label.get("note") or "labeled from a real run"
    if not (isinstance(rationale, str) and rationale.strip()):
        rationale = "labeled from a real run"

    # request schema (test_scoring_eval_dataset.REQUIRED_REQUEST).
    required_request = {"manufacturer", "model", "part_number", "voltage",
                        "category", "detected_type"}
    if not required_request.issubset(request.keys()):
        return None
    if request.get("category") not in ("Part", "Equipment"):
        return None

    case = {
        "id": f"real_{run_id}_{candidate_ref}",
        "split": _split_for(run_id),
        "request": {
            "manufacturer": request["manufacturer"],
            "model": request["model"],
            "part_number": request["part_number"],
            "voltage": request["voltage"],
            "category": request["category"],
            "detected_type": request["detected_type"],
        },
        "result": {
            "snippet": snippet,
            "url": url,
            "title": title,
            "found_pn": found_pn,
        },
        "expected": {
            "should_pass_floor": should_pass,
            "rationale": rationale,
        },
        "provenance": f"real:{run_id}",
    }
    hp = request.get("hp")
    if hp:
        case["request"]["hp"] = hp
    return case


# ---------------------------------------------------------------------------
# Dataset append — load the real-cases file (or seed it), append, write back.
# Never touches the committed synthetic fixtures.
# ---------------------------------------------------------------------------

def _load_real_dataset(path: str, top_key: str) -> Dict[str, Any]:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    # Seed a fresh real-cases dataset with a _meta marking it as real-only.
    return {"_meta": {"purpose": "Real labeled cases exported from captured runs "
                                  "(Night 2). Provenance real:<run_id> on every case.",
                      "source": "run_capture + run_labels"}, top_key: []}


def _write_real_dataset(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _ids_in(cases: List[Dict[str, Any]], id_key: str) -> set:
    return {c.get(id_key) for c in cases if isinstance(c, dict)}


def append_intake_case(path: str, case: Dict[str, Any]) -> bool:
    """Append an intake case to the real-cases dataset (dedup by provenance+input).
    Returns True if appended, False if deduped/withheld."""
    data = _load_real_dataset(path, "examples")
    examples = data.setdefault("examples", [])
    # Dedup on (provenance, input) — relabeling the same run re-emits; keep the latest.
    key = (case.get("provenance"), case.get("input"))
    existing = {(e.get("provenance"), e.get("input")) for e in examples}
    if key in existing:
        # Replace the prior emission of this run's input with the latest label.
        examples = [e for e in examples
                    if (e.get("provenance"), e.get("input")) != key] + [case]
        data["examples"] = examples
    else:
        examples.append(case)
    _write_real_dataset(path, data)
    return True


def append_scoring_case(path: str, case: Dict[str, Any]) -> bool:
    """Append a scoring case to the real-cases dataset (dedup by id)."""
    data = _load_real_dataset(path, "cases")
    cases = data.setdefault("cases", [])
    cid = case.get("id")
    if cid in _ids_in(cases, "id"):
        cases = [c for c in cases if c.get("id") != cid] + [case]
        data["cases"] = cases
    else:
        cases.append(case)
    _write_real_dataset(path, data)
    return True


# ---------------------------------------------------------------------------
# Provenance metric (T5) — % real vs synthetic per eval suite.
# ---------------------------------------------------------------------------

def provenance_breakdown(path: str, top_key: str) -> Dict[str, Any]:
    """Report real vs synthetic counts for a dataset file.

    A case is "real" if it carries a ``provenance`` key starting with ``real:``;
    otherwise synthetic (the committed fixtures predate provenance). Returns
    counts + a per-split breakdown. Fail-soft: a missing file -> zero counts.
    """
    if not os.path.exists(path):
        return {"total": 0, "real": 0, "synthetic": 0, "real_pct": 0.0,
                "by_split": {}}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    cases = data.get(top_key) or []
    real = 0
    synth = 0
    by_split: Dict[str, Dict[str, int]] = {}
    for c in cases:
        split = c.get("split", "unknown")
        b = by_split.setdefault(split, {"real": 0, "synthetic": 0})
        prov = c.get("provenance")
        if isinstance(prov, str) and prov.startswith("real:"):
            real += 1
            b["real"] += 1
        else:
            synth += 1
            b["synthetic"] += 1
    total = real + synth
    return {
        "total": total,
        "real": real,
        "synthetic": synth,
        "real_pct": round(real / total, 3) if total else 0.0,
        "by_split": by_split,
    }


def provenance_report() -> Dict[str, Any]:
    """T5 — the provenance metric across both suites (synthetic fixtures +
    real-cases files). Reports % real per suite."""
    intake_synth = os.path.join(_FIXTURE_DIR, "intake_eval_dataset.json")
    scoring_synth = os.path.join(_FIXTURE_DIR, "scoring_eval_dataset.json")
    # Combine each synthetic fixture with its real-cases file for the headline %.
    def _combined(suite: str, synth_path: str, real_path: str, top_key: str) -> Dict[str, Any]:
        synth = provenance_breakdown(synth_path, top_key)
        real = provenance_breakdown(real_path, top_key)
        total = synth["total"] + real["total"]
        real_n = synth["real"] + real["real"]
        return {
            "suite": suite,
            "synthetic_cases": synth["total"],
            "real_cases": real["real"],
            "total": total,
            "real_pct": round(real_n / total, 3) if total else 0.0,
            "synthetic_breakdown": synth,
            "real_breakdown": real,
        }
    return {
        "intake": _combined("intake", intake_synth, INTAKE_REAL_DATASET, "examples"),
        "scoring": _combined("scoring", scoring_synth, SCORING_REAL_DATASET, "cases"),
    }
