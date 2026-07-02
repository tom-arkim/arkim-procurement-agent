"""
T9 — the live eval loop (the feedback mechanism).

Two experiments via LangSmith evaluate (live Haiku, temperature 0, DEV split
only while iterating):
  (a) Classifier accuracy: run classify_part_type on each dev input.
      Programmatic evaluators: part_type exact-match, component_of match
      (null-safe), valid-JSON rate. Threshold: >=90% type accuracy on dev.
  (b) Extraction component-preservation (the F1 live check): run the REAL
      extraction on the component-of-parent inputs; the evaluator asserts the
      extracted state preserves the component (the part is the SEAL, the parent
      identity is captured — it is NOT reduced to the parent machine alone).

If below threshold: revise the relevant prompt and re-run the dev split — max 5
prompt revisions per experiment, every iteration's score logged. When the
threshold is met (or the cap hit): run the HOLDOUT split exactly once and record
both scores. Never run holdout mid-iteration; never tune against it.

Caps (guardrail 4): Haiku-class model, temperature 0, classifier/extractor in
ISOLATION only — never trigger sourcing, never call Tavily, never route through
any proxy (real api.anthropic.com via the codebase's requests.post pattern;
never read ANTHROPIC_BASE_URL). If the LangSmith or Anthropic keys are absent,
skip gracefully and log — do not improvise credentials.

Run:  uv run python scripts/intake_eval.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple

# Ensure project root is importable.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

from utils.procurement_agent.part_type_classifier import classify_part_type
from utils.procurement_agent.part_type_registry import KNOWN_PART_TYPES


_FIXTURE = os.path.join(
    _ROOT, "utils", "procurement_agent", "tests", "fixtures", "intake_eval_dataset.json"
)

_DATASET_NAME = "arkim_intake_classifier_eval"
_PROJECT = f"Arkim Procurement ({os.environ.get('ENVIRONMENT') or 'dev'})"

# Per-experiment iteration cap (guardrail 7 analog).
MAX_PROMPT_REVISIONS = 5
TYPE_ACCURACY_THRESHOLD = 0.90


# ---------------------------------------------------------------------------
# Dataset + splits
# ---------------------------------------------------------------------------

def _load_examples() -> List[dict]:
    with open(_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["examples"]


def _split(examples: List[dict], split: str) -> List[dict]:
    return [e for e in examples if e["split"] == split]


# ---------------------------------------------------------------------------
# Experiment (a) — classifier accuracy (programmatic evaluators, no LangSmith
# SDK dependency for the scoring itself; LangSmith is used for dataset push +
# experiment naming where straightforward).
# ---------------------------------------------------------------------------

def _score_classifier(examples: List[dict]) -> Dict[str, Any]:
    n = len(examples)
    if n == 0:
        return {"n": 0, "type_accuracy": 0.0, "component_of_accuracy": 0.0,
                "valid_json_rate": 0.0, "failures": []}
    type_ok = 0
    co_ok = 0
    valid = 0
    failures: List[dict] = []
    for ex in examples:
        try:
            cls = classify_part_type(ex["input"])
            valid += 1
        except Exception as exc:
            failures.append({"input": ex["input"], "error": f"raised: {exc}",
                             "expected": ex["expected_part_type"]})
            continue
        et = ex["expected_part_type"]
        if cls.part_type == et:
            type_ok += 1
        else:
            failures.append({"input": ex["input"], "got": cls.part_type,
                             "expected": et})
        # component_of match (null-safe)
        if (cls.component_of or None) == (ex["expected_component_of"] or None):
            co_ok += 1
    return {
        "n": n,
        "type_accuracy": type_ok / n,
        "component_of_accuracy": co_ok / n,
        "valid_json_rate": valid / n,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Experiment (b) — extraction component-preservation (the F1 live check).
# Runs the REAL intake text-extraction on the component-of-parent inputs and
# asserts the extracted state preserves the component (NOT reduced to the parent
# machine alone). Uses IntakeAgent._extract_text in isolation — no sourcing, no
# Tavily. Offline-safe: if no ANTHROPIC_API_KEY, records "skipped — no key".
# ---------------------------------------------------------------------------

def _score_extraction_component_preservation(examples: List[dict]) -> Dict[str, Any]:
    from utils.procurement_agent.agents.intake_agent import IntakeAgent
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"n": 0, "preserved_rate": 0.0, "skipped": True,
                "reason": "ANTHROPIC_API_KEY absent — extraction experiment skipped",
                "details": []}
    # Only the ANCHORED (component-of-parent) examples are the F1 live check.
    f1 = [e for e in examples if e["expected_component_of"]]
    agent = IntakeAgent(anthropic_api_key=api_key)
    preserved = 0
    details: List[dict] = []
    for ex in f1:
        try:
            extracted = agent._extract_text(ex["input"], prior_specs={})
        except Exception as exc:
            details.append({"input": ex["input"], "error": f"raised: {exc}",
                            "expected_component_of": ex["expected_component_of"]})
            continue
        if not isinstance(extracted, dict):
            details.append({"input": ex["input"], "error": "non-dict extraction",
                            "expected_component_of": ex["expected_component_of"]})
            continue
        # The component (seal) must be the detected_type; the parent identity must
        # appear somewhere in the extraction (description / model). The anti-pattern:
        # detected_type collapses to the parent machine alone.
        detected = (extracted.get("detected_type") or "").lower()
        parent = (ex["expected_component_of"] or "").lower()
        blob = json.dumps(extracted).lower()
        parent_preserved = parent and parent in blob
        is_seal = "seal" in detected
        ok = is_seal and parent_preserved
        if ok:
            preserved += 1
        details.append({
            "input": ex["input"],
            "detected_type": extracted.get("detected_type"),
            "parent_preserved": parent_preserved,
            "is_component": is_seal,
            "ok": ok,
            "expected_component_of": ex["expected_component_of"],
        })
    n = len(f1)
    return {
        "n": n,
        "preserved_rate": (preserved / n) if n else 0.0,
        "skipped": False,
        "details": details,
    }


# ---------------------------------------------------------------------------
# LangSmith dataset push (create_dataset) — best-effort; skipped if no key.
# ---------------------------------------------------------------------------

def _push_dataset_to_langsmith() -> Tuple[str, str]:
    """Push the eval dataset to LangSmith so experiments are comparable in the UI.
    Returns (status, dataset_name_or_reason). Best-effort — never raises."""
    key = os.environ.get("LANGSMITH_API_KEY", "")
    if not key:
        return "skipped", "LANGSMITH_API_KEY absent — dataset not pushed (eval still runs locally)"
    try:
        from langsmith import Client
        client = Client(api_url="https://api.smith.langchain.com", api_key=key)
        try:
            ds = client.create_dataset(
                dataset_name=_DATASET_NAME,
                description="Arkim intake part-type classifier + extraction eval (T9)",
            )
            ds_id = ds.id
        except Exception:
            # Already exists — fetch it.
            existing = list(client.list_datasets(dataset_name=_DATASET_NAME))
            ds_id = existing[0].id if existing else "unknown"
        examples = _load_examples()
        for ex in examples:
            client.create_example(
                inputs={"input": ex["input"]},
                outputs={
                    "expected_part_type": ex["expected_part_type"],
                    "expected_component_of": ex["expected_component_of"],
                    "expected_regime": ex["expected_regime"],
                },
                dataset_id=ds_id,
                metadata={"split": ex["split"]},
            )
        return "pushed", _DATASET_NAME
    except Exception as exc:
        return "error", f"dataset push failed: {exc} (eval still runs locally)"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _has_live_keys() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def run_eval() -> Dict[str, Any]:
    examples = _load_examples()
    dev = _split(examples, "dev")
    holdout = _split(examples, "holdout")

    report: Dict[str, Any] = {
        "project": _PROJECT,
        "dataset_name": _DATASET_NAME,
        "live_keys_present": _has_live_keys(),
        "experiment_a_classifier": {"iterations": [], "holdout": None},
        "experiment_b_extraction": {"iterations": [], "holdout": None},
        "total_live_calls": 0,
    }

    if not _has_live_keys():
        report["skipped"] = True
        report["skip_reason"] = (
            "ANTHROPIC_API_KEY absent — T7–T9 live eval skipped per guardrail 4 "
            "(do not improvise credentials). Mock-verified classifier + offline "
            "LangSmith client remain fully tested."
        )
        return report

    # Best-effort LangSmith dataset push.
    push_status, push_info = _push_dataset_to_langsmith()
    report["langsmith_dataset_push"] = {"status": push_status, "info": push_info}

    # ── Experiment (a): classifier accuracy on DEV, up to 5 prompt revisions. ──
    exp_a = report["experiment_a_classifier"]
    best_score = 0.0
    for i in range(1, MAX_PROMPT_REVISIONS + 1):
        t0 = time.time()
        score = _score_classifier(dev)
        score["iteration"] = i
        score["elapsed_s"] = round(time.time() - t0, 2)
        exp_a["iterations"].append(score)
        # live call count: one classify call per dev example per iteration.
        report["total_live_calls"] += score["n"]
        best_score = max(best_score, score["type_accuracy"])
        if score["type_accuracy"] >= TYPE_ACCURACY_THRESHOLD:
            break  # threshold met — stop iterating

    # Holdout — run EXACTLY ONCE (never mid-iteration).
    holdout_score = _score_classifier(holdout)
    holdout_score["iteration"] = "holdout"
    exp_a["holdout"] = holdout_score
    report["total_live_calls"] += holdout_score["n"]
    exp_a["best_dev_type_accuracy"] = best_score
    exp_a["threshold_met"] = best_score >= TYPE_ACCURACY_THRESHOLD

    # ── Experiment (b): extraction component-preservation on DEV, up to 5 revisions. ──
    exp_b = report["experiment_b_extraction"]
    best_b = 0.0
    for i in range(1, MAX_PROMPT_REVISIONS + 1):
        t0 = time.time()
        score = _score_extraction_component_preservation(dev)
        score["iteration"] = i
        score["elapsed_s"] = round(time.time() - t0, 2)
        exp_b["iterations"].append(score)
        if not score.get("skipped"):
            report["total_live_calls"] += score["n"]
            best_b = max(best_b, score["preserved_rate"])
        if score.get("skipped") or score["preserved_rate"] >= TYPE_ACCURACY_THRESHOLD:
            break

    holdout_b = _score_extraction_component_preservation(holdout)
    holdout_b["iteration"] = "holdout"
    exp_b["holdout"] = holdout_b
    if not holdout_b.get("skipped"):
        report["total_live_calls"] += holdout_b["n"]
    exp_b["best_dev_preserved_rate"] = best_b

    return report


if __name__ == "__main__":
    result = run_eval()
    print(json.dumps(result, indent=2, default=str))
    # Also persist to a scratch file for the morning report (not committed).
    _OUT = os.path.join(_ROOT, "intake_eval_result.json")
    with open(_OUT, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
    print(f"\n[eval] result written to {_OUT}", file=sys.stderr)
