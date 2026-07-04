"""
Detection-fix eval — BEFORE state (catches the bug) and AFTER state (the fix).

T1 built the BEFORE: a query-contaminated eval dataset that reproduces the live
bug (Pumpman pump page ranked #1 at ~46% above seals because the opaque URL
forced detection to the snippet, which echoes the query noun 'mechanical seal',
and longest-synonym-first let the echo win -> SEAL -> same-class gate ~1.0).

T2/T3 apply the fix (dominant-class detection: structural signals — vendor name
+ registered domain — override a snippet echo on the opaque-URL path) and wire
the vendor into the live call sites.

T4 (this file's AFTER section) re-runs the eval and asserts the before->after
delta: the 2 Pumpman cases flip to correctly-fail, true positives unchanged, and
the residual uncorroborated-opaque-URL case is a documented known-gap that STILL
wrongly-passes (reported, not relabeled, not stretched).

The BEFORE tests deliberately run the scorer WITHOUT vendor (mirroring the
pre-fix live path that passed no vendor) and assert the bug reproduces. The
AFTER tests run WITH vendor (mirroring the post-T3 live path) and assert the fix.

The before-state load-bearing assertion (the bug MUST wrongly-pass before the
fix) is preserved so the eval can never silently go green-before-fix again.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from utils.sourcing_archieved import scoring as scoring_mod
from utils.sourcing_archieved.scoring import _compute_suitability_score
from utils.sourcing_archieved.part_type_classes import (
    classify_result_noun_class,
    classify_result_noun_class_dominant,
)
from utils.models import AssetSpecs

_FIXTURE = Path(__file__).parent / "fixtures" / "scoring_detection_eval.json"
_BEFORE = Path(__file__).parent / "scoring_detection_eval_before.json"
_AFTER = Path(__file__).parent / "scoring_detection_eval_after.json"
FLOOR = 30.0


def _load() -> dict:
    with open(_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _known_gap_ids() -> set[str]:
    return set(_load().get("_meta", {}).get("known_gap_ids", []))


def _to_specs(req: dict) -> AssetSpecs:
    return AssetSpecs(
        manufacturer=req["manufacturer"],
        model=req["model"],
        part_number=req["part_number"],
        voltage=req["voltage"],
        category=req.get("category", "Part"),
        hp=req.get("hp"),
        detected_type=req.get("detected_type"),
    )


def _run(pass_vendor: bool) -> list[dict[str, Any]]:
    """Run the eval against the current code. pass_vendor=False mirrors the
    PRE-fix live path (no vendor -> legacy text verdict); pass_vendor=True
    mirrors the POST-T3 live path (vendor -> dominant-class detection)."""
    dataset = _load()
    rows: list[dict[str, Any]] = []
    saved_v2 = scoring_mod.SCORING_V2
    saved_stage0 = scoring_mod.STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL
    try:
        for c in dataset["cases"]:
            res = c["result"]
            specs = _to_specs(c["request"])
            scoring_mod.SCORING_V2 = True
            scoring_mod.STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL = False
            score = _compute_suitability_score(
                specs, res["snippet"], res["url"],
                found_pn=res.get("found_pn"),
                title=None,  # live-faithful: the live path passes no title
                vendor=res.get("vendor") if pass_vendor else None,
            )
            result_cls = scoring_mod._last_noun_classes["result"]
            query_cls = scoring_mod._last_noun_classes["query"]
            # The detector verdict the scorer actually used.
            if pass_vendor and res.get("vendor"):
                detected = classify_result_noun_class_dominant(
                    res["vendor"], None, res["snippet"], res["url"])
            else:
                detected = classify_result_noun_class(res["snippet"], res["url"])
            rows.append({
                "id": c["id"],
                "kind": c["kind"],
                "expected_pass": c["expected"]["should_pass_floor"],
                "expected_noun_class": c["expected"]["expected_noun_class"],
                "detected_noun_class": detected,
                "scorer_result_cls": result_cls,
                "scorer_query_cls": query_cls,
                "score": score,
                "passes_floor": score >= FLOOR,
                "rationale": c["expected"]["rationale"],
            })
    finally:
        scoring_mod.SCORING_V2 = saved_v2
        scoring_mod.STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL = saved_stage0

    for r in rows:
        r["verdict_correct"] = (r["passes_floor"] == r["expected_pass"])
        r["wrongly_passes"] = (r["expected_pass"] is False and r["passes_floor"] is True)
        r["wrongly_fails"] = (r["expected_pass"] is True and r["passes_floor"] is False)
        r["noun_class_correct"] = (r["detected_noun_class"] == r["expected_noun_class"])
    return rows


@pytest.fixture(scope="module")
def before_rows() -> list[dict[str, Any]]:
    rows = _run(pass_vendor=False)  # pre-fix live path: no vendor
    with open(_BEFORE, "w", encoding="utf-8") as fh:
        json.dump({"floor": FLOOR, "rows": rows}, fh, indent=2)
    return rows


@pytest.fixture(scope="module")
def after_rows() -> list[dict[str, Any]]:
    rows = _run(pass_vendor=True)  # post-T3 live path: vendor -> dominant-class
    with open(_AFTER, "w", encoding="utf-8") as fh:
        json.dump({"floor": FLOOR, "rows": rows}, fh, indent=2)
    return rows


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------

class TestDatasetWellFormed:
    def test_loads_with_cases(self):
        d = _load()
        assert "cases" in d and isinstance(d["cases"], list)
        assert len(d["cases"]) >= 11

    def test_live_failure_and_residual_present(self):
        d = _load()
        ids = {c["id"] for c in d["cases"]}
        assert "pumpman_pump_seal_kit_page" in ids
        assert "residual_uncorroborated_opaque_pump" in ids

    def test_known_gap_ids_declared(self):
        gaps = _known_gap_ids()
        assert "residual_uncorroborated_opaque_pump" in gaps


# ---------------------------------------------------------------------------
# BEFORE state — the eval must CATCH the bug (load-bearing, never green-before-fix)
# ---------------------------------------------------------------------------

class TestBeforeStateReproducesBug:
    def test_live_failure_case_wrongly_passes_before_fix(self, before_rows):
        """The Pumpman live_failure case MUST wrongly-pass against the pre-fix
        path (no vendor). If it does NOT, the eval stopped catching the bug."""
        r = next(x for x in before_rows if x["id"] == "pumpman_pump_seal_kit_page")
        assert r["wrongly_passes"], (
            f"BUG NOT REPRODUCED in before-state: live_failure scored {r['score']} "
            f"(passes={r['passes_floor']}) but should wrongly-pass. detected="
            f"{r['detected_noun_class']}. If it's already crushed, the eval isn't "
            f"testing the real bug — fix the eval."
        )

    def test_before_state_has_wrongly_passing_contaminated_cases(self, before_rows):
        """At least the live_failure case + one more contaminated case wrongly
        pass before the fix (the 2 Pumpman opaque-URL cases)."""
        wrongly = [r for r in before_rows if r["wrongly_passes"]]
        ids = {r["id"] for r in wrongly}
        assert "pumpman_pump_seal_kit_page" in ids
        assert "pumpman_pump_with_mechanical_seal" in ids


# ---------------------------------------------------------------------------
# AFTER state — the fix flips the Pumpman cases, true positives unchanged
# ---------------------------------------------------------------------------

class TestAfterStateFix:
    def test_live_failure_case_now_crushed(self, after_rows):
        r = next(x for x in after_rows if x["id"] == "pumpman_pump_seal_kit_page")
        assert not r["passes_floor"], (
            f"FIX FAILED: live_failure still passes at {r['score']} "
            f"(detected={r['detected_noun_class']}, expected PUMP)"
        )
        assert r["detected_noun_class"] == "PUMP", (
            f"dominant-class detector should classify Pumpman pump as PUMP, got {r['detected_noun_class']}"
        )

    def test_pumpman_with_mechanical_seal_now_crushed(self, after_rows):
        r = next(x for x in after_rows if x["id"] == "pumpman_pump_with_mechanical_seal")
        assert not r["passes_floor"], f"second Pumpman case still passes at {r['score']}"
        assert r["detected_noun_class"] == "PUMP"

    def test_true_positives_still_pass(self, after_rows):
        """No over-correction: real seals/bearing/correct-pump still clear the floor."""
        tp_ids = {
            "real_seal_specialist_must_pass", "real_seal_kit_aftermarket_must_pass",
            "real_bearing_clean_must_pass", "clean_pump_on_pump_request_must_pass",
        }
        for r in after_rows:
            if r["id"] in tp_ids:
                assert r["passes_floor"], (
                    f"OVER-CORRECTION: true positive {r['id']} now fails at {r['score']} "
                    f"(detected={r['detected_noun_class']})"
                )

    def test_no_true_positive_wrongly_fails(self, after_rows):
        wrongly_fail = [r for r in after_rows if r["wrongly_fails"]]
        assert not wrongly_fail, (
            f"true positives regressed: {[r['id'] for r in wrongly_fail]}"
        )

    def test_contaminated_cases_with_slug_or_longer_phrase_still_crushed(self, after_rows):
        """The cases that already crushed before (via URL slug or longer text
        phrase) must still crush after — the fix must not disturb them."""
        for cid in ("zoro_pump_seal_replacement_text",
                    "valve_on_bearing_request_echoed_bearing",
                    "motor_on_seal_request_echoed_seal",
                    "impeller_on_seal_request_echoed_seal",
                    "opaque_pump_no_contamination_clean_negative"):
            r = next(x for x in after_rows if x["id"] == cid)
            assert not r["passes_floor"], f"{cid} should still fail, got {r['score']}"


# ---------------------------------------------------------------------------
# The residual known-gap — reported, NOT fixed
# ---------------------------------------------------------------------------

class TestResidualKnownGap:
    def test_residual_still_wrongly_passes_after_fix(self, after_rows):
        """The uncorroborated-opaque-URL pump (MB Glick, no noun-class in
        vendor/domain) STILL wrongly passes after the fix — by design. The
        structural-signal fix cannot reach it (no corroboration). This asserts
        the residual is HONEST: we did not stretch the fix to catch it and did
        not relabel it. The flywheel sizes this residual with real data."""
        r = next(x for x in after_rows if x["id"] == "residual_uncorroborated_opaque_pump")
        assert r["wrongly_passes"], (
            f"RESIDUAL DISAPPEARED: {r['id']} now correctly fails at {r['score']} "
            f"(detected={r['detected_noun_class']}). If the fix caught this, it was "
            f"stretched beyond the structural-signal rule — check for over-correction."
        )

    def test_residual_excluded_from_asserted_metrics(self, after_rows):
        gaps = _known_gap_ids()
        asserted = [r for r in after_rows if r["id"] not in gaps]
        # every non-gap case reaches its expected verdict after the fix
        wrong = [r for r in asserted if not r["verdict_correct"]]
        assert not wrong, (
            f"non-gap cases with wrong verdict after fix: "
            f"{[(r['id'], r['score'], r['passes_floor'], r['expected_pass']) for r in wrong]}"
        )


# ---------------------------------------------------------------------------
# BEFORE -> AFTER delta report
# ---------------------------------------------------------------------------

class TestBeforeAfterDelta:
    def test_delta_report(self, before_rows, after_rows, capsys):
        before_by_id = {r["id"]: r for r in before_rows}
        after_by_id = {r["id"]: r for r in after_rows}
        gaps = _known_gap_ids()
        print("\n========== DETECTION-FIX EVAL — BEFORE -> AFTER DELTA ==========")
        print(f"{'id':44s} {'det_before':10s} {'det_after':10s} {'score_before':>12s} {'score_after':>11s} {'verdict_before':>14s} {'verdict_after':>13s}")
        print("-" * 118)
        flipped, fixed, same, residual = [], [], [], []
        for r in after_rows:
            b = before_by_id[r["id"]]
            det_b = str(b["detected_noun_class"])
            det_a = str(r["detected_noun_class"])
            vb = "WRONG" if not b["verdict_correct"] else "OK"
            va = "WRONG" if not r["verdict_correct"] else ("GAP" if r["id"] in gaps else "OK")
            mark = ""
            if r["id"] in gaps:
                mark = " [KNOWN-GAP]"
                residual.append(r["id"])
            elif b["wrongly_passes"] and not r["wrongly_passes"]:
                mark = " [FLIPPED->FIXED]"
                flipped.append(r["id"])
            elif not b["verdict_correct"] and r["verdict_correct"]:
                fixed.append(r["id"])
            else:
                same.append(r["id"])
            print(f"{r['id']:44s} {det_b:10s} {det_a:10s} {b['score']:>12.1f} {r['score']:>11.1f} {vb:>14s} {va:>13s}{mark}")
        print("-" * 118)
        print(f"FLIPPED to correctly-fail: {len(flipped)} -> {flipped}")
        print(f"Other fixed: {len(fixed)} -> {fixed}")
        print(f"Unchanged (already correct): {len(same)} -> {same}")
        print(f"Residual known-gap (still wrongly-passes, by design): {len(residual)} -> {residual}")
        # Load-bearing delta: the 2 Pumpman cases must flip; true positives unchanged.
        assert "pumpman_pump_seal_kit_page" in flipped
        assert "pumpman_pump_with_mechanical_seal" in flipped
        assert "residual_uncorroborated_opaque_pump" in residual
        print("=================================================================\n")
