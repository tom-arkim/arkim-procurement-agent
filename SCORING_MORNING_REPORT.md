# Arkim Scoring Redesign — Morning Report

**Branch:** `feature/scoring-v2` (branched from `ecfeaf9` on `feature/phase3-comparison-approval`)
**Flag:** `SCORING_V2` (strict truthy `1/true/yes/on`, matching `_env_truthy`) — default OFF.
**Status:** IN PROGRESS.

---

## Baseline

- **Test count (before any change):** `uv run pytest -q` → **1116 passed**.
  - NOTE: `CLAUDE.md` §4 states "360 passing" — that figure is stale. The real green baseline on this branch is **1116**. Every commit below is verified against 1116.
- Baseline commit hash: (recorded at T0 commit)

---

## Task status

| Task | Status | Commit | Notes |
|------|--------|--------|-------|
| T0 — Setup + baseline | DONE | _pending_ | branch + report scaffold |
| T1 — MRO noun-class dictionary | | | |
| T2 — Stage 0 placeholder-penalty fix | DONE | _pending_ | toggle `scoring.py:53` (GATED default); tests cover all 4 flag×toggle paths; clean-PN + genuine-mismatch no-regress |
| T3 — noun-class detection (query+result) | DONE | _pending_ | detection+storage only; flag-off never invokes; clean-PN score unchanged flag-on vs off; `_last_noun_classes` store for T4 |
| T4 — multiplicative TypeGate | DONE | _pending_ | ANCHOR: Zoro pump fails (≤10), Platinum seal passes; undetectable→0.45 floor; flag-off byte-identical legacy; auth capped ≤10 inside gate |
| T5 — graded Fit | DONE | _pending_ | exact-PN demoted 40→20 (bonus within Fit); parent-model/size-type/interchange first-class; anchor re-verified (seal passes, pump fails, bearing still passes); flag-off byte-identical |
| T6 — inertness wall | DONE | _pending_ | 14-fixture battery vs baseline ecfeaf9 (all byte-identical); falsy-token parity (8 tokens→OFF, 8→ON); Stage 0 toggle-default audit (placeholder stays 25 flag-off) |
| T7 — labeled eval dataset | DONE | _pending_ | 24 cases (17 dev / 7 holdout); schema-validated; anchors present (seal pass, pump fail, SKF pass); spans SEAL/PUMP/BEARING/VALVE/MOTOR/DRIVE/IMPELLER/COUPLING/GASKET + undetectable + collection + marketplace |
| T8 — eval run | | | |
| T9 — morning report (this file) | | | |

---

## The key decision for Tom (T2 / Stage 0)

**Stage 0 (placeholder-penalty fix)** is arguably a pure correctness bug: `UNKNOWN-PN` was never a real PN, so a real `found_pn` should not be penalized for "mismatching" it. As a bug fix it could ship **unconditional (flag-OFF)** — which means it **changes the launch demo's scoring for component cases** (the Goulds seal goes 25→55 and clears the 30 floor; previously-cut component results resurface).

**Decision taken in this build (conservative default, pending Tom's call):** Stage 0 is built **gated behind `SCORING_V2`** — the launch demo stays byte-identical to today. The T2 test is written to assert the fix works under `SCORING_V2=1` AND that the old penalizing behavior is preserved under flag-OFF (so the inertness wall stays clean). Tom can flip Stage 0 to unconditional before merge by moving one line out of the gate; the tests already cover both paths.

> **Stage 0 toggle — exact location:** `utils/sourcing_archieved/scoring.py:53`
> ```python
> STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL: bool = False   # GATED (default). Flip to True to ship unconditional.
> ```
> Flip to `True` to fix launch-demo component scoring (seal 25→55, clears floor). Currently `False` (GATED) pending Tom's stress-test decision. The consuming branch is at `scoring.py:368`: `_stage0_active = SCORING_V2 or STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL`. Tests in `utils/procurement_agent/tests/test_scoring_stage0.py` cover all four (flag × toggle) combinations + the genuine-mismatch and clean-PN no-regression cases.

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

_Filled in at T8._

---

## NEEDS-VERIFICATION

- [ ] Live end-to-end re-test of the Goulds seal-vs-pump case with `SCORING_V2=1` (eval is deterministic scoring, not a live run).
- [ ] Tom's call on whether Stage 0 ships unconditional at launch (above).
- [ ] Gate/weight calibration against real sourcing data (the chosen values are informed defaults, not tuned).

---

## Unspecified decisions made

_Filled in as the build progresses._
