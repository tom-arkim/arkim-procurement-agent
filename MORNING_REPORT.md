# MORNING REPORT — Overnight Build Program, NIGHT 1 (Run Capture + Outcome Signals)

**Brief:** `arkim-overnight-build-program.md` (repo root, untracked) — Night 1 only; Nights 2–5 are context, not scope (not built, not scaffolded).
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
| 4 | `feature/run-capture-overnight` does NOT exist locally or on origin | PASS |
| 5 | Working tree clean (only known untracked: briefs, `audit/`, `intake_eval_result.json`, `scripts/*_self_test.py`) | PASS |
| 6 | `_env_truthy` convention present; `/api/health` exists | PASS |

## Baseline vs final

- **Baseline (base `d0e0ee2`):** `uv run pytest -q` → **1489 passed, 73 skipped** (80.8s)
- **Final:** `uv run pytest -q` → **1524 passed, 73 skipped** (+35 tests: 25 unit `test_run_capture.py` + 10 live `test_run_capture_live.py`)
- Every commit verified green before the next task. No push performed.

## Task status

| Task | Status | Commit | Notes |
|------|--------|--------|-------|
| T0 — Setup + baseline + report scaffold | ✅ | `6cee9fa` | pre-flight green; baseline 1489/73 |
| T1 — Schema (`run_events` + `run_outcomes`) | ✅ | `899c946` | `data/run_capture.sqlite` only; raw sqlite3 WAL; 25 unit tests |
| T2 — Capture hooks at I2 seams (behind `RUN_CAPTURE`) | ✅ | `c2177f7` | turns, intake_result, query_issued, candidate_scored/rejected, results_displayed, user actions; all flag-gated + fail-soft |
| T3 — Outcome computation | ✅ | `899c946`+`d4b7921` | completed_with_action / abandoned_after_results / zero_results / all_rejected / incomplete; completion-action refinement (confirm_intake is a transition, not a completion) |
| T4 — Fail-soft + `/api/health` `capture_failures` | ✅ | `c2177f7` | thread-safe counter; surfaced on health ONLY when flag on (flag-off body byte-identical); fail-soft asserted |
| T5 — Inertness wall (flag-off = zero writes, byte-identical) | ✅ | `d4b7921` | module-level (25 unit) + API-level (live TestClient) inertness; flag-off full run = ZERO capture rows, byte-identical health + run-detail |

**No task hit the iteration cap. No reverts needed.**

---

## Investigation findings (I1–I4) — vs EXPECTED

### I1 — Persistence inventory · EXPECTED met (with a nuance)
Every store written during a run enumerated (file:line evidence in the run):

| Store | Location | Durable? | Scope |
|---|---|---|---|
| runs | `data/sourcing_runs.sqlite` (SQLAlchemy, WAL) | Yes | per-run + cross-run config |
| **user/agent turns** | **in-memory `_messages` (api_server.py:322)** — "cleared on server restart" | **NO** | per-run, ephemeral |
| per-candidate scores/verdicts | inside `sourcing_runs.sourcing_results_json` (api_server.py:1182) | Yes | per-run blob |
| price_db / supplier_registry / orders / audit_log / brand_intelligence / known_parts / site_settings |各自 sqlite/json | Yes | cross-run |

**EXPECTED verdict:** "user turns and per-candidate scores/verdicts are NOT durably persisted (console prints only)." → **Half-confirmed.** User/agent turns are genuinely ephemeral (the real gap — capture is NEW durable storage, not duplication). Per-candidate scores/verdicts ARE durable inside `sourcing_results_json` + stdout `[Sourcing]` prints; capture duplicates them as append-only queryable event rows (intentional — event log ≠ state blob).

### I2 — Hook points · EXPECTED met (run_id threading confirmed)
- **(a) Intake turn boundary:** `send_message` (api_server.py:1634). `run_id` in scope. → turn_user/turn_agent/intake_result.
- **(b) Sourcing collect/score/reject:** the `[Sourcing]` print sites in `enterprise_search.py` (97/230/240/499/547…) have **NO `run_id` in scope** (EXPECTED confirmed — capture needs run_id threading). **Decision:** do NOT thread run_id into the load-bearing `sourcing_archieved` query builders (CLAUDE.md §6 — audit all call sites; out of scope). Capture `candidate_scored`/`candidate_rejected`/`query_issued`/`results_displayed` from the `result` dict at `_run_sourcing_background` (api_server ~1107-1186) where `run_id` IS in scope. `query_issued` captures the per-tier INTENT derived from specs; the **literal Tavily query string is built deeper and is a flagged not-captured gap.**
- **(c) Result assembly:** `_transform_sourcing_results` (api_server.py:945) filters `rejection_reason` → displayed set. Captured at write-back.
- **(d) Action events (run_id in scope):** confirm_intake, select_candidate, order_now, approve, reject, outreach. **No backend event source:** "report click" is frontend-only navigation → logged as a gap.

### I3 — PII path · EXPECTED met; default decision applied (with flagged risk)
- **No PII redaction pipeline exists today** (grep: only `_redact_sourcing_error` — redacts *error detail*, not user PII; + test comments). User free text is deliberately kept OUT of stdout (api_server.py:1670-1675) — "structured interaction capture + a deliberate PII policy are a separate (deferred) pass."
- **Default decision (per kickoff I3):** store **post-redaction** text. Since no redaction exists, "post-redaction" = **the text as the intake path sees it** (as-is). **Fidelity cost = none** (no redaction transform to lose).
- **Flagged risk (Tom must verify):** capture into `run_capture.sqlite` is a NEW durable PII surface — under `RUN_CAPTURE` (default OFF) the demo is unaffected, but flag-ON capture stores visitor free text (names, facility addresses, real part numbers) durably. For a public no-login demo with no consent gate this is the deliberate "deferred structured-capture pass" the code comment anticipated. A real redaction pipeline + consent gate is a flagged supervised follow-up; adding redaction later would cost eval fidelity. Flag gate + morning review is the mitigation tonight.

### I4 — Async write option · EXPECTED met
- `/api/health` (api_server.py:2600) returns `{"status","version","demo_mode"}`. Route on `_DEMO_ALLOWLIST` (do-not-touch surface). Adding `capture_failures` touches only the **handler body** — safe.
- **Existing `test_health` pins the exact flag-off body** (test_api_server.py:837). To preserve byte-identical inertness, `capture_failures` appears ONLY when `RUN_CAPTURE` is on → existing test stays green untouched (not weakened).
- `BackgroundTasks` is the house async pattern but the brief EXPECTED ("simple try/except + counter sufficient at demo volume") holds — capture writes are one cheap INSERT; **inline synchronous try/except + thread-safe counter** is the chosen fail-soft mechanism (BackgroundTasks would hide failures from the request path).
- **SQLite convention:** raw `sqlite3` (mirrors `orders.py:39-113` / `supplier_registry.py:72` / `audit_log.py:65`). `run_capture.py` follows this exactly (NOT SQLAlchemy). `data/run_capture.sqlite` only.

**No investigation finding contradicted a stated assumption.** All four EXPECTED results met. Proceeded to T1–T5.

---

## VERIFIED tonight (by test)

- **Schema (T1):** `run_events` append-only + `run_outcomes`; `data/run_capture.sqlite` only (raw sqlite3, WAL); pure-data import (no network); indexes on run_id / (run_id, event_type) / event_type.
- **Every event type** writes + reads back with correct shape (mocked): turn_user, turn_agent, intake_result, query_issued, candidate_scored, candidate_rejected, results_displayed, user_action, outcome.
- **Full simulated run** produces the complete expected event sequence (11-event chain).
- **Outcome computation (T3):** completed_with_action / abandoned_after_results / zero_results / all_rejected / incomplete — each fixture-asserted. `confirm_intake` correctly treated as a transition, not a completion.
- **Fail-soft (T4):** a forced write failure does NOT raise into the request path AND increments the counter (asserted at both unit and live-API levels); counter surfaced on `/api/health` only when flag on.
- **Inertness wall (T5):** flag OFF → ZERO capture rows across a full simulated run (asserted by direct table COUNT), byte-identical `/api/health` body, byte-identical run-detail response. Falsy-token parity (`0/false/no/""/junk/None → inert`).
- **Live-faithfulness (guardrail-7):** hooks tested via TestClient through the REAL `/api/runs` + `/messages` + `/confirm-intake` + `/select-candidate` + `/reject` + `/api/health` paths — NOT by calling capture functions directly. The `api` fixture mocks IntakeAgent/SourcingAgent at their source modules (the existing pattern); the real api_server handler path runs.
- **No do-not-touch surface modified:** `.env`, `audit/`, `scripts/*_self_test.py`, seed fixtures, `known_parts.json`, `price_db.json`, DEMO_MODE gates, the security/allowlist surface, the SpecComparisonAgent base_url pin — all untouched. The only change to `/api/health` is inside the handler body (a conditional field); the allowlist set + middleware are unchanged.
- **Capture reads seams, never mutates them:** `run_capture.py` writes ONLY `data/run_capture.sqlite`. No write to price_db / known_parts / supplier_registry / orders / sent_messages / review_items.

## NEEDS LIVE VERIFICATION by Tom (mandatory morning checklist)

1. **Flag-on backend, run 3 real parts through the UI** (one clean PN, one component, one vague):
   ```powershell
   $env:RUN_CAPTURE = "1"
   uvicorn api_server:app --reload --port 8001
   cd frontend; npm run dev
   ```
   Then inspect `data/run_capture.sqlite` (snippet below) — confirm turns, queries, candidates+scores, displayed set are all there and match what you saw on screen.
2. **Abandon a run mid-way** (describe a part, see results, do NOT select/order) → confirm `compute_outcome(run_id)` returns `abandoned_after_results` (or `zero_results` if no candidates).
3. **`/api/health`** with the flag ON shows `capture_failures: 0`; with the flag OFF the body is unchanged (no `capture_failures` key).
4. **Flag-off restart** → run a part → confirm NO new rows written to `run_capture.sqlite` (the morning iterate-trigger).
5. **The Goulds run END-TO-END with the flag ON** — confirm the candidate rows in capture match the on-screen cards (score + verdict), and that a rejected candidate carries `rejection_reason`.
6. **PII surface check** — with the flag on, open `run_capture.sqlite` and eyeball a `turn_user` row: it contains the visitor's free text verbatim. This is the deliberate capture (I3 default); confirm you're OK with it for the demo posture, or queue the redaction follow-up before flipping the flag on in any shared config.

### Read snippet for `run_capture.sqlite` (morning inspection)
```bash
# From the repo root (flag ON backend, after a real run):
sqlite3 data/run_capture.sqlite "SELECT event_type, run_id, source_tag, substr(payload_json,1,80) FROM run_events ORDER BY ts DESC LIMIT 20;"
# Per-run outcome:
sqlite3 data/run_capture.sqlite "SELECT run_id, outcome, computed_at FROM run_outcomes ORDER BY computed_at DESC LIMIT 10;"
# Or with uv (no sqlite3 CLI):
uv run python -c "import utils.run_capture as rc; [print(e['event_type'], e['run_id'][:8], e['source_tag'], str(e['payload'])[:90]) for e in rc.read_all_events()[-20:]]"
```
(Note: `utils/run_capture.py` reads the flag at import; a live flip requires a process restart, matching the codebase's other env flags.)

---

## Every decision I made that wasn't specified by the brief

1. **`run_capture.py` is a standalone raw-sqlite3 module** (mirrors `orders.py`/`supplier_registry.py`/`audit_log.py`), NOT SQLAlchemy. Keeps it off the `persistence.py` ORM stack and the run-row contract — capture is a side-car event log, not run state. (`utils/run_capture.py`)
2. **Capture functions no-op when `RUN_CAPTURE` is off (flag check inside each fn)** rather than gating at every call site — keeps api_server changes to thin one-liners AND guarantees inertness regardless of caller. The flag is read once at import (mirrors `EMAIL_SEND_ENABLED`/`DEMO_MODE`/`SCORING_V2`); tests monkeypatch the module attr.
3. **`capture_failures` appears on `/api/health` ONLY when flag on** — preserves byte-identical flag-off health body (the existing `test_health` exact-equality assertion stays green untouched). (`api_server.py:2600`)
4. **Outcome classifier: `confirm_intake` is a transition, not a completion.** The brief lists `completed_with_action` as an outcome; I treat only result-actions (select/order/approve/reject/outreach/save_outreach/rfq_draft/mark_delivered) as completions. Otherwise a zero-results run with a confirm-intake would mis-classify as completed. (`run_capture.py:_COMPLETION_ACTIONS`)
5. **`query_issued` captures the query INTENT derived from specs** (manufacturer/model/PN), not the literal Tavily query string — the latter is built deep in `enterprise_search.py` where no `run_id` is in scope (I2 EXPECTED). Flagged as a not-captured gap; the intent is the useful flywheel signal.
6. **`rephrased` outcome is a flagged placeholder** (`detect_rephrase` returns False) — cross-run similarity is a heuristic that needs the session/run-specs map; not forced into the single-run classifier. Reported, not asserted as deterministic.
7. **`source_tag` derivation is `demo_prospect` under DEMO_MODE else `internal_test`** — `customer:<tenant>` awaits tenant identity infra (Arc 1). Flagged as a placeholder.
8. **Capture calls in `_run_sourcing_background` are wrapped in a try/except** (in addition to each capture fn's internal fail-soft) — belt-and-suspenders so a bug in the capture loop can never break the sourcing write-back. (`api_server.py` post-write-back block.)
9. **Test isolation additions in the live test's local `api` fixture:** (a) pin `known_parts._DB_PATH` to tmp — without it, a real cached edge for a part like `goulds|ST1375T1` makes `_run_sourcing_background` take the cache-HIT path and bypass the mocked SourcingAgent (the captured candidates would be the cached edge set, not the test's mocks); (b) force `api_server.DEMO_MODE=False` — a prior `test_demo_mode` run in the same session can leave the imported module attr True, making `create_run` 422 on X-Session-Id. Both are isolation fixes, not behavior changes.

## Blockers (with diagnosis)

1. **None.** All five tasks completed; suite green at every commit (1489 → 1524, +35); no revert needed (iteration cap never hit).

## Out of scope tonight (per the brief, not done — Nights 2–5 not scaffolded)

- Night 2 (labeling surface + eval export), Night 3 (supplier record + Tier 1 registry), Night 4 (onboarding agent), Night 5 (Tier 1 runtime). The program rule applies: one night per session, never combine.
- Literal Tavily query-string capture (needs run_id threading into `sourcing_archieved` — load-bearing, out of scope).
- A real PII redaction pipeline + consent gate (I3 flagged follow-up).
- "report click" frontend action has no backend event source (I2(d) gap) — logged, not wired.

## State I left the repo in

- On branch `feature/run-capture-overnight` (off `d0e0ee2`), **NOT pushed**, never touched `main` or `feature/phase3-comparison-approval`.
- 4 commits, suite green at each: `6cee9fa` (T0) → `899c946` (T1) → `c2177f7` (T2/T4 wiring) → `d4b7921` (T2–T5 tests + outcome refinement).
- New files: `utils/run_capture.py`, `utils/procurement_agent/tests/test_run_capture.py`, `utils/procurement_agent/tests/test_run_capture_live.py`. Modified: `api_server.py` (import + thin capture one-liners at the seams + conditional health field). `MORNING_REPORT.md` at repo root.
- No source code on the do-not-touch list was modified. `data/run_capture.sqlite` is created at runtime only when `RUN_CAPTURE` is on (not committed; not present on disk in flag-off default).

*The flag-off inertness wall is green; the worst case tonight is zero — the branch reviews, fixes, or deletes, and the demo never noticed.*
