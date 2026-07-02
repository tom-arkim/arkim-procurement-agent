# Arkim Scoring Redesign — Morning Report

**Branch:** `feature/scoring-v2` (branched from `ecfeaf9` on `feature/phase3-comparison-approval`)
**Flag:** `SCORING_V2` (strict truthy `1/true/yes/on`, matching `_env_truthy`) — default OFF.
**Status:** COMPLETE. T0–T9 done; suite green at 1263; NOT pushed; flag stays OFF in all shipped configs.

---

## Baseline → final

- **Baseline (before any change):** `uv run pytest -q` → **1116 passed** (commit `ecfeaf9`).
  - NOTE: `CLAUDE.md` §4 states "360 passing" — that figure is stale. The real green baseline on this branch is **1116**.
- **Final (after T0–T8):** `uv run pytest -q` → **1263 passed** (+147 new tests across T1–T8).
- Every commit was verified green before the next task started. No push performed.

---

## Task status

| Task | Status | Commit | Notes |
|------|--------|--------|-------|
| T0 — Setup + baseline | DONE | `85f953f` | branch + report scaffold |
| T1 — MRO noun-class dictionary | DONE | `31c3d39` | 10 classes; data-only; 35 tests; URL-wins-on-disagreement + path-only + longest-synonym-first |
| T2 — Stage 0 placeholder-penalty fix | DONE | `aab7d88` | toggle `scoring.py:53` (GATED default); 4-way flag×toggle tests; clean-PN + genuine-mismatch no-regress |
| T3 — noun-class detection (query+result) | DONE | `66bab96` | detection+storage only; flag-off never invokes; `_last_noun_classes` store for T4; 16 tests |
| T4 — multiplicative TypeGate | DONE | `bfc7d05` | ANCHOR: Zoro pump fails (≤10), Platinum seal passes; undetectable→0.45 floor; flag-off byte-identical; auth capped ≤10 inside gate; 12 tests |
| T5 — graded Fit | DONE | `9aa9e01` | exact-PN demoted 40→20 (bonus within Fit); parent/size-type/interchange first-class; anchor re-verified; 11 tests |
| T6 — inertness wall | DONE | `61b5568` | 14-fixture battery vs baseline ecfeaf9 (all byte-identical); falsy-token parity; Stage 0 toggle-default audit; 31 tests |
| T7 — labeled eval dataset | DONE | `adad2b4` | 24 cases (17 dev / 7 holdout); schema-validated; anchors present; 12 tests |
| T8 — eval run | DONE | `20d557f` | 15/15 should-pass pass flag-on; 7/7 should-fail rejected; 1 known-gap mismatch (valve-subtype, Stage 3); artifact `scoring_eval_results.json`; 10 tests |
| T9 — morning report (this file) | DONE | _pending_ | finalizes the report — per-task hashes, decision summary, NEEDS-VERIFICATION |

---

## The key decision for Tom (T2 / Stage 0)

**Stage 0 (placeholder-penalty fix)** is arguably a pure correctness bug: `UNKNOWN-PN` was never a real PN, so a real `found_pn` should not be penalized for "mismatching" it. As a bug fix it could ship **unconditional (flag-OFF)** — which means it **changes the launch demo's scoring for component cases** (the Goulds seal goes 25→55 and clears the 30 floor; previously-cut component results resurface).

**Decision taken in this build (conservative default, pending Tom's call):** Stage 0 is built **gated behind `SCORING_V2`** — the launch demo stays byte-identical to today. The T2 test is written to assert the fix works under `SCORING_V2=1` AND that the old penalizing behavior is preserved under flag-OFF (so the inertness wall stays clean). Tom can flip Stage 0 to unconditional before merge by moving one line out of the gate; the tests already cover both paths.

> **Stage 0 toggle — exact location:** `utils/sourcing_archieved/scoring.py:58`
> ```python
> STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL: bool = False   # GATED (default). Flip to True to ship unconditional.
> ```
> Flip to `True` to fix launch-demo component scoring (seal 25→55, clears floor). Currently `False` (GATED) pending Tom's stress-test decision. The consuming branch is at `scoring.py:575`: `_stage0_active = SCORING_V2 or STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL`. Tests in `utils/procurement_agent/tests/test_scoring_stage0.py` cover all four (flag × toggle) combinations + the genuine-mismatch and clean-PN no-regression cases. T6's `TestStage0ToggleDefaultAudit` guards the default: if the placeholder case ever drifts from 25→~45 under flag-off, the toggle was flipped (a launch-demo change that must be deliberate).

**Why this matters for launch:** if Tom wants the shipped demo to show the seal clearing the floor (the headline quality fix), Stage 0 must be unconditional. If he wants the demo frozen at current scoring, it stays gated. The rest (Stage 1 TypeGate, Stage 2 Fit) is always gated and never affects launch.

---

## Gate / weight values chosen (informed defaults — NEED real-data calibration)

**T4 TypeGate** (`scoring.py` `_type_gate`):
- `_TYPE_GATE_MATCH_HIGH = 1.0` — same noun-class, URL slug AND text both agree
- `_TYPE_GATE_MATCH_LOW = 0.7` — same noun-class, only one signal agrees
- `_TYPE_GATE_RESULT_UNDETECTABLE = 0.45` — result type undetectable (ESCI floor: never zero)
- `_TYPE_GATE_QUERY_UNDETECTABLE = 1.0` — query type undetectable (neutral; fall back to additive)
- `_TYPE_GATE_DIFFERENT = 0.1` — confirmed different noun-class (pump on a seal request)
- `_V2_AUTH_CAP = 10.0` — supplier/authority capped at 10 pts inside the gate

**Anchor behavior under `SCORING_V2=1`:**
- Platinum seal (SEAL/SEAL, high-conf) → gate 1.0 → ~45 → **passes** (floor 30).
- Zoro pump (SEAL/PUMP, different) → gate 0.1 → ~4 → **fails** (crushed, not borderline).
- Undetectable result (SEAL/None) → gate 0.45 → ~20 → not zeroed, but does not blindly pass.
- Clean-PN bearing (BEARING/BEARING, high-conf) → gate 1.0 → 95→85 (auth cap 20→10) → still passes strongly.

These are informed defaults derived from the research spec, NOT tuned against real sourcing data. The gate values (especially 0.7, 0.45, 0.1) and the auth cap (10) need calibration against live Tier 2/3 results before flipping `SCORING_V2` on in any shipped config.

**T5 graded Fit** (`scoring.py` `_fit_signal`, 0-40 scale replacing the legacy `pn_pts` slot):
- `_FIT_EXACT_PN = 20.0` — exact/normalized OEM-PN match (the bonus, demoted from the legacy 40)
- `_FIT_STEM_PN = 12.0` — same PN family stem
- `_FIT_SUBSTRING_PN = 8.0` — searched PN appears in snippet
- `_FIT_PARENT_MODEL = 10.0` — `specs.model` token in snippet (the core aftermarket signal)
- `_FIT_SIZE_TYPE = 5.0` — decimal size / "Type N" token from specs in snippet
- `_FIT_INTERCHANGE = 10.0` — cross-reference / interchange / replaces / fits / replacement-for language
- `_FIT_MAX = 40.0` — cap (mirrors the old `pn_pts` ceiling)

The "no PN confirmed" 45-cap now keys on `fit_pts == 0` (no Fit evidence at all) rather than `pn_pts == 0`, so a strong aftermarket-Fit result is no longer capped just for lacking an exact OEM PN. The same calibration caveat applies — these weights are informed defaults, not tuned.

**Anchor behavior under `SCORING_V2=1` after T5:**
- Platinum seal: fit=25 (parent 10 + size 5 + interchange 10), gate 1.0 → ~70 → **passes**.
- Zoro pump: fit=10 (parent 3196 only), gate 0.1 → ~4 → **fails**.
- Clean SKF bearing: fit=30 (exact 20 + parent 10), gate 1.0 → ~75 (was 85 at T4, 95 legacy) → still **passes strongly**; the drift is the intended exact-PN demotion.

---

## Eval results (T8)

**Run:** SCORING_V2=1, Stage 0 GATED, floor=30. Deterministic scoring (no LLM/network). 24 cases (22 asserted + 2 known-gap). Artifact: `utils/procurement_agent/tests/scoring_eval_results.json` (regenerated by `test_scoring_eval_run.py`).

### Headline metrics
- **should-pass: 15/15 pass flag-on (100%)** — zero regression on right-part cases.
- **should-fail: 7/7 rejected flag-on (100%)** — every wrong-part/collection/undetectable case correctly cut.
- **Clean-PN no-regression:** every should-pass case with a real (non-placeholder) searched PN passes flag-off too (the byte-identical guarantee). Placeholder-PN component cases (UNKNOWN-PN) legitimately fail flag-off under GATED Stage 0 (legacy −30 penalty, 25 < floor) and pass flag-on — the intended launch-decision behavior, not a regression.

### The anchor (verified)
| Case | flag-on | flag-off | verdict |
|------|---------|----------|---------|
| goulds_seal_platinum (right specialist) | 65.0 | 25.0 | **PASS** ✅ |
| goulds_pump_zoro (wrong marketplace) | 4.0 | 40.0 | **FAIL** ✅ (flag-off 40 → flag-on 4: the TypeGate fix) |
| skf_bearing_clean (no-regression) | 75.0 | 95.0 | **PASS** ✅ (95→75: intended auth-cap + exact-PN demotion; still passes) |

seal_score (65) > pump_score (4) — the headline invariant holds.

### Known-gap (Stage 3, out of scope — reported, not asserted)
- **valve_gate_on_ball_request**: flag-on 48 (PASS), desired FAIL — **MISMATCH**. A gate valve on a ball-valve request classifies same- noun-class (both VALVE), so the TypeGate is SAME-class and can't separate the sub-type. This is the documented Stage-3 gap (sub-type gating via page-fetch/LLM). The case is labeled `should_pass_floor=false` as the *desired* verdict and flagged here, not relabeled to pass.
- **motor_starter_on_vfd_request**: flag-on 0 (FAIL), desired FAIL — OK. `starter` is a DRIVE synonym so it classifies same-class as the VFD request, but no-Fit (no parent-model `PowerFlex 525` in snippet, no interchange) drove it to 0 regardless. Reported as known-gap for completeness; verdict happened to match.

### Per-case table (dev)
| Case | flag-on | flag-off | expected | got | |
|------|---------|----------|----------|-----|---|
| goulds_seal_platinum | 65.0 | 25.0 | pass | PASS | OK |
| goulds_pump_zoro | 4.0 | 40.0 | fail | FAIL | OK |
| skf_bearing_clean | 75.0 | 95.0 | pass | PASS | OK |
| goulds_seal_specialist_thin_url | 45.5 | 25.0 | pass | PASS | OK |
| goulds_seal_marketplace_correct | 65.0 | 25.0 | pass | PASS | OK (anti-inversion) |
| goulds_pump_on_seal_marketplace | 4.0 | 10.0 | fail | FAIL | OK |
| valve_ball_correct | 65.0 | 25.0 | pass | PASS | OK |
| valve_gate_on_ball_request | 48.0 | 18.0 | fail | PASS | MISMATCH [KNOWN-GAP] |
| bearing_clean_pn_mcmaster | 52.5 | 95.0 | pass | PASS | OK |
| motor_correct_oem | 47.6 | 88.0 | pass | PASS | OK |
| vfd_correct | 70.0 | 80.0 | pass | PASS | OK |
| motor_starter_on_vfd_request | 0.0 | 10.0 | fail | FAIL | OK [KNOWN-GAP] |
| goulds_seal_collection_page | 5.0 | 5.0 | fail | FAIL | OK |
| goulds_seal_undetectable_result | 22.5 | 40.0 | fail | FAIL | OK (0.45 floor, not zeroed) |
| marketplace_bearing_counterfeit_risk | 35.0 | 45.0 | pass | PASS | OK |
| gusher_seal_clean | 85.0 | 95.0 | pass | PASS | OK |
| endress_sensor_correct | 75.0 | 95.0 | pass | PASS | OK (undetectable query → neutral gate) |

### Per-case table (holdout)
| Case | flag-on | flag-off | expected | got | |
|------|---------|----------|----------|-----|---|
| goulds_impeller_correct | 65.0 | 25.0 | pass | PASS | OK |
| goulds_impeller_on_seal_request | 4.0 | 10.0 | fail | FAIL | OK (generalizes beyond pump) |
| coupling_correct | 58.0 | 18.0 | pass | PASS | OK |
| gasket_correct_aftermarket | 65.0 | 25.0 | pass | PASS | OK (graded-Fit thesis) |
| goulds_pump_correct_on_pump_request | 75.0 | 95.0 | pass | PASS | OK (gate doesn't penalize pumps generally) |
| goulds_seal_wrong_pn_mismatch | 25.0 | 25.0 | fail | FAIL | OK (genuine −30 still fires) |
| goulds_seal_marketplace_wrong_pn | 5.0 | 5.0 | fail | FAIL | OK (collection page) |

### Conclusion
The redesign achieves the measurable target: the specialist seal clears the floor, the Zoro pump does not, clean-PN cases are unchanged in verdict (pass flag-on AND flag-off), and the gate generalizes (impeller-on-seal also crushed to 4). The one mismatch is the documented Stage-3 sub-type gap, not a scorer bug. Flag-on absolute scores are generally lower than flag-off (auth capped, exact-PN demoted) — as expected; judge on relative ordering + floor-clearance, both of which hold.

---

## NEEDS-VERIFICATION

- [ ] **Live end-to-end re-test of the Goulds seal-vs-pump case with `SCORING_V2=1`** — the T8 eval is deterministic scoring against synthetic fixtures, NOT a live sourcing run. Before flipping the flag on in any shipped config, run a real Tier 2/3 sourcing pass on a Goulds-3196-style component request and confirm the seal surfaces / the pump is cut end-to-end (detection → scoring → floor → UI). The eval validates the MECHANISM on synthetic cases; it does not validate the live pipeline integration.
- [ ] **Tom's call on whether Stage 0 ships unconditional at launch** (the toggle at `scoring.py:58`). Pending Tom's flag-off stress test, which decides whether component scoring is launch-relevant. Default is GATED (launch demo byte-identical).
- [ ] **Gate/weight calibration against real sourcing data.** The chosen values (`MATCH_HIGH=1.0`, `MATCH_LOW=0.7`, `RESULT_UNDETECTABLE=0.45`, `DIFFERENT=0.1`, `_V2_AUTH_CAP=10`, and the Fit weights) are informed defaults derived from the research spec, NOT tuned against live Tier 2/3 results. This is the flag-gated fast-follow's whole point — tune post-launch against real data before flipping `SCORING_V2` on in production.
- [ ] **The 3 enterprise_search.py call sites were NOT updated to pass `title`.** `_compute_suitability_score` gained an optional `title` param (default `None`); the call sites still pass `(specs, snippet, url, found_pn)` and rely on the snippet as the text signal. Result noun-class detection works (the URL slug is the high-leverage signal and the snippet carries title-like content), but passing the real result title would improve MATCH_HIGH confidence. Wiring `title` through `enterprise_search.py` is a small follow-up — out of scope for this build (the brief scoped the scorer itself); flagged here so it isn't lost.

---

## Unspecified decisions made (everything not in the brief, flagged for review)

1. **Stage 0 default = GATED** (not unconditional). The brief left this open; the conservative default preserves launch-demo byte-identical scoring pending Tom's stress test. One-line toggle at `scoring.py:58`.
2. **`_last_noun_classes` module-level debug store** for T3→T4 handoff, rather than changing `_compute_suitability_score`'s return shape (a float). Changing the return shape would ripple to `enterprise_search.py` + FastAPI response models + React types; the module-level store avoids that with no contract break. It is cleared to `None` on the flag-off path so it never leaks detection state.
3. **Optional `title` param** on `_compute_suitability_score` (default `None`) rather than a required new arg — keeps all 3 existing call sites working unchanged. Call sites not yet updated to pass it (see NEEDS-VERIFICATION #4).
4. **T4/T5 test reframings**: several earlier tests pinned flag-on *absolute* scores (bearing 95/85/75) that drifted as T4 (auth cap) and T5 (exact-PN demotion) landed. I moved those assertions from "pin the flag-on absolute" to "assert the invariant (flag-off byte-identical to baseline) + the verdict (floor-clearance)" — matching how the eval is judged. T6's 14-fixture byte-identical wall (vs baseline `ecfeaf9`) is the independent proof that the reframings were honest: all 14 flag-off fixtures match the baseline.
5. **T8 clean-PN no-regression test is placeholder-aware**: it asserts flag-off parity only for cases with a real (non-placeholder) searched PN. Placeholder-PN component cases legitimately fail flag-off under GATED Stage 0 (25 < floor) and pass flag-on — the intended launch-decision behavior, audited separately by T6's Stage 0 toggle-default test.
6. **The two same-class-wrong-subtype eval cases** (`valve_gate_on_ball_request`, `motor_starter_on_vfd_request`) labeled `should_pass_floor=false` as the *desired* verdict but flagged as known-gap and excluded from the asserted metrics — sub-type gating is Stage 3 (out of scope). Reported, not hidden. The valve case is a real mismatch (passes at 48 when desired fail); the starter case happened to fail on no-Fit.
7. **Gate values are noun-class-level only.** Sub-type separation (gate-vs-ball valve, starter-vs-VFD) is explicitly out of scope — the brief scoped Stage 0+1+2, not Stage 3. Documented in the eval known-gap cases.
8. **`SCORING_V2` flag read once at import** (mirroring `EMAIL_SEND_ENABLED` / `DEMO_MODE`). Tests monkeypatch the module attr per-test. A live flip requires a process restart, not a runtime toggle — consistent with the codebase's other env flags.

---

## Out of scope (per the brief, not done)

- Stage 3 (page-fetch, spec tables, LLM type-classifier / sub-type gating).
- Touching the intake/query builders (a prior build).
- Deploy; flipping `SCORING_V2` on in any shipped config.
- Tuning weights beyond the informed defaults (a real-data post-launch job).
- Wiring `title` through `enterprise_search.py` call sites (flagged in NEEDS-VERIFICATION).

---

## Summary

The two scoring defects behind "seal loses to pump" are fixed behind `SCORING_V2` (default OFF): the placeholder-penalty inversion (Stage 0, with a one-line gated/unconditional toggle for Tom's launch decision) and the missing part-type gate (Stage 1 multiplicative TypeGate + Stage 2 graded Fit). The Goulds 3196 anchor holds (specialist seal passes at 65, Zoro pump crushed to 4), clean-PN cases are unchanged in verdict, and the gate generalizes across noun-classes on the holdout set. Flag-off is byte-identical to the pre-redesign baseline (proven independently by T6 against commit `ecfeaf9`). The mechanism is validated on synthetic cases; the weights need real-data calibration before the flag goes on in production.
