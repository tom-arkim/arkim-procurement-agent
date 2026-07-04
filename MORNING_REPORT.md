# MORNING REPORT — Overnight Build Program, NIGHT 1 (Run Capture + Outcome Signals)

**Brief:** `arkim-overnight-build-program.md` (repo root, untracked) — Night 1 only; Nights 2–5 are context, not scope.
**Branch:** `feature/run-capture-overnight` (off `test/flag-on-integration` @ `d0e0ee2`; NEVER pushed)
**Flag:** `RUN_CAPTURE` — default OFF; strict `_env_truthy` (`1/true/yes/on`). Flag-off = byte-identical behavior (T5 inertness wall).
**Date:** 2026-07-04

> Status legend: ✅ done · 🟡 blocked-and-logged · ⏭️ skipped (with diagnosis)

---

## Pre-flight (all PASS)

| # | Check | Result |
|---|-------|--------|
| 1 | Brief `arkim-overnight-build-program.md` present at repo root | PASS |
| 2 | Base = `test/flag-on-integration` @ `d0e0ee2b...` (exact) | PASS |
| 3 | `RUN_CAPTURE` flag does NOT exist anywhere in `utils/`/`api_server.py`/`frontend/` | PASS (grep empty) |
| 4 | `feature/run-capture-overnight` does NOT exist locally or on origin | PASS (local: fatal; origin: empty) |
| 5 | Working tree clean (only known untracked: briefs, `audit/`, `intake_eval_result.json`, `scripts/*_self_test.py`) | PASS |
| 6 | `_env_truthy` convention present (`api_server.py:24`); `/api/health` exists (`api_server.py:2600`) | PASS |

## Baseline (on the base, before any change)

- `uv run pytest -q` → **1489 passed, 73 skipped, 1 warning** (80.8s, Python 3.11, `.venv`)
- Base hash: `d0e0ee2b21429b4ac798dfe1e5f414b3ab283459`

## Task status

| Task | Status | Commit | Notes |
|------|--------|--------|-------|
| T0 — Setup + baseline + report scaffold | ✅ | (this commit) | pre-flight green, baseline 1489/73 |
| T1 — Schema (`run_events` + `run_outcomes`) | ⏳ | — | |
| T2 — Capture hooks at I2 seams (behind `RUN_CAPTURE`) | ⏳ | — | |
| T3 — Outcome computation | ⏳ | — | |
| T4 — Fail-soft + `/api/health` `capture_failures` | ⏳ | — | |
| T5 — Inertness wall (flag-off = zero writes, byte-identical) | ⏳ | — | |

## Investigation findings (I1–I4) — populated below as agents report

_Pending — four parallel investigation agents dispatched (I1 persistence inventory, I2 hook points, I3 PII path, I4 async-write/health). Findings vs EXPECTED will be written here before T1._

---
