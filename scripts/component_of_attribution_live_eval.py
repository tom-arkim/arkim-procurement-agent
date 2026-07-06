"""
T3 LIVE eval — component-of parent-attribution: the T1 proof.

Runs the REAL intake text-extraction (Sonnet, temperature 0) on the
component-of-parent cases from the run-capture diagnosis and asserts the
extracted manufacturer/model are EMPTY (the T1 fix), while detected_type,
category, and the parent identity (in use_case/description) are preserved.

A mocked-LLM unit test CANNOT prove the T1 prompt change — the mock payload is
author-controlled, so it can't fail-pre-T1 / pass-post-T1. Only the real LLM
can. This script runs the real extraction and reports the per-case result.

Run BEFORE and AFTER the T1 extraction-prompt fix to get the before/after:
  # post-T1 (current code):
  uv run python scripts/component_of_attribution_live_eval.py

  # pre-T1 (revert just the prompt edit):
  git stash push -- utils/procurement_agent/agents/intake_agent.py
  uv run python scripts/component_of_attribution_live_eval.py
  git stash pop

LLM extraction is non-deterministic. Report the ACTUAL per-case result; do not
smooth. A prompt fix that only works 70% of the time is a 70% fix — flag any
borderline case rather than hiding it.

Output: a JSON report + a per-case table to stderr.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

# Ensure project root is importable.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

from utils.procurement_agent.agents.intake_agent import IntakeAgent


# The component-of cases from the run-capture diagnosis (the 8 that went
# parent-led + the 2 that worked by accident). Each carries the expected
# detected_type, the expected parent (for the _component_of / use_case
# preservation check), and the expected category.
CASES: List[Dict[str, Any]] = [
    {"input": "impeller for a Goulds 3196",
     "expected_detected_type": "impeller", "expected_parent": "Goulds 3196",
     "expected_category": "Part"},
    {"input": "wear ring for a Goulds 3196",
     "expected_detected_type": "wear ring", "expected_parent": "Goulds 3196",
     "expected_category": "Part"},
    {"input": "shaft sleeve for a Waukesha 060",
     "expected_detected_type": "shaft sleeve", "expected_parent": "Waukesha 060",
     "expected_category": "Part"},
    {"input": "diaphragm kit for a Graco Husky 1050",
     "expected_detected_type": "diaphragm", "expected_parent": "Graco Husky 1050",
     "expected_category": "Part"},
    {"input": "seal kit for an Alfa Laval LKH-10",
     "expected_detected_type": "seal", "expected_parent": "Alfa Laval LKH-10",
     "expected_category": "Part"},
    {"input": "shaft seal for a SEW R47 gearbox",
     "expected_detected_type": "seal", "expected_parent": "SEW R47",
     "expected_category": "Part"},
    {"input": "drive chain for a Hytrol conveyor",
     "expected_detected_type": "chain", "expected_parent": "Hytrol",
     "expected_category": "Part"},
    # The 2 that worked by accident (seal-goulds, baldor-brushes):
    {"input": "mechanical seal for a Goulds 3196 pump",
     "expected_detected_type": "mechanical seal", "expected_parent": "Goulds 3196",
     "expected_category": "Part"},
    {"input": "carbon brushes for a Baldor motor",
     "expected_detected_type": "brush", "expected_parent": "Baldor",
     "expected_category": "Part"},
]

_NULL = {None, "", "null", "N/A", "Unknown", "UNKNOWN-PN", "none", "unknown"}


def _is_empty(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() in _NULL)


def run_eval() -> Dict[str, Any]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"skipped": True, "reason": "ANTHROPIC_API_KEY absent", "cases": []}
    agent = IntakeAgent(anthropic_api_key=api_key)
    results: List[Dict[str, Any]] = []
    for case in CASES:
        try:
            extracted = agent._extract_text(case["input"], prior_specs={})
        except Exception as exc:
            results.append({**case, "error": f"raised: {exc}",
                            "manufacturer": None, "model": None,
                            "detected_type": None, "category": None,
                            "use_case": None, "description": None})
            continue
        if not isinstance(extracted, dict):
            results.append({**case, "error": "non-dict extraction",
                            "manufacturer": None, "model": None,
                            "detected_type": None, "category": None,
                            "use_case": None, "description": None})
            continue
        mfg = extracted.get("manufacturer")
        mdl = extracted.get("model")
        det = (extracted.get("detected_type") or "").lower()
        cat = extracted.get("category")
        use_case = extracted.get("use_case")
        desc = extracted.get("description")
        blob = json.dumps(extracted).lower()
        parent_preserved = case["expected_parent"].lower() in blob
        mfg_model_empty = _is_empty(mfg) and _is_empty(mdl)
        # The T1 assertion: mfg/model EMPTY (component's own make unknown at
        # intake), parent preserved elsewhere (use_case/description/_component_of).
        t1_ok = mfg_model_empty and parent_preserved
        results.append({
            "input": case["input"],
            "expected_detected_type": case["expected_detected_type"],
            "expected_parent": case["expected_parent"],
            "expected_category": case["expected_category"],
            "manufacturer": mfg,
            "model": mdl,
            "detected_type": extracted.get("detected_type"),
            "category": cat,
            "use_case": use_case,
            "description": desc,
            "parent_preserved": parent_preserved,
            "mfg_model_empty": mfg_model_empty,
            "t1_ok": t1_ok,
            "detected_type_ok": case["expected_detected_type"].lower() in det,
            "category_ok": (cat or "").lower() == case["expected_category"].lower(),
        })
    n = len(results)
    t1_pass = sum(1 for r in results if r.get("t1_ok"))
    return {
        "skipped": False,
        "n": n,
        "t1_pass": t1_pass,
        "t1_pass_rate": (t1_pass / n) if n else 0.0,
        "cases": results,
    }


def _print_table(report: Dict[str, Any]) -> None:
    if report.get("skipped"):
        print(f"[SKIP] {report.get('reason')}", file=sys.stderr)
        return
    print("\n=== Component-of attribution: T1 extraction live eval ===", file=sys.stderr)
    print(f"T1 pass rate (mfg/model EMPTY + parent preserved): "
          f"{report['t1_pass']}/{report['n']} "
          f"({report['t1_pass_rate']*100:.0f}%)", file=sys.stderr)
    print("-" * 100, file=sys.stderr)
    hdr = f"{'input':<42} {'det':<16} {'mfg':<14} {'model':<10} {'m/m_empty':<10} {'parent':<8} {'T1':<4}"
    print(hdr, file=sys.stderr)
    print("-" * 100, file=sys.stderr)
    for r in report["cases"]:
        if "error" in r:
            print(f"{r['input']:<42} ERROR: {r['error']}", file=sys.stderr)
            continue
        mme = "Y" if r["mfg_model_empty"] else "N"
        par = "Y" if r["parent_preserved"] else "N"
        t1 = "OK" if r["t1_ok"] else "FAIL"
        mfg = (r["manufacturer"] or "")[:13]
        mdl = (r["model"] or "")[:9]
        det = (r["detected_type"] or "")[:15]
        flag = "  <<<" if not r["t1_ok"] else ""
        print(f"{r['input']:<42} {det:<16} {mfg:<14} {mdl:<10} {mme:<10} {par:<8} {t1:<4}{flag}",
              file=sys.stderr)
    print("-" * 100, file=sys.stderr)


if __name__ == "__main__":
    report = run_eval()
    _print_table(report)
    out = os.path.join(_ROOT, "component_of_attribution_live_eval_result.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"\n[eval] result written to {out}", file=sys.stderr)
