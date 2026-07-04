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

## Investigation findings (I1–I4) — vs EXPECTED

### I1 — Persistence inventory · EXPECTED met (with a nuance)
Enumerated every store written during a run. Evidence (file:line):

| Store | Location | Durable? | Scope | Key writer |
|---|---|---|---|---|
| runs | `data/sourcing_runs.sqlite` (SQLAlchemy ORM, WAL) | Yes | per-run + cross-run config | `persistence.create_run` (persistence.py:288); state transitions via direct ORM in api_server (1602/1628/1749/1126/1183/…) |
| **user/agent turns** | **in-memory `_messages` dict (api_server.py:322)** — "Phase 3 prototype — cleared on server restart" | **NO** | per-run, ephemeral | send_message 1657-1659 (user), 1753-1759 (agent); upload 1814-1825; read-back at 1584 |
| per-candidate scores/verdicts | inside `sourcing_runs.sourcing_results_json` (api_server.py:1182) | Yes | per-run blob | `_run_sourcing_background` write-back; also stdout `[Sourcing]` prints |
| price_db | `utils/price_db.json` | Yes | cross-run | `price_db.save_price:45` |
| supplier_registry | `data/supplier_registry.sqlite` (suppliers + sent_messages + review_items) | Yes | cross-run (+ per-run-attributed) | `supplier_registry.create_stub:349`, `record_sent_message:704`, `record_review_item:823` |
| orders | `data/orders.sqlite` | Yes | per-run (`run_id`) | `orders.create_order:177` |
| audit_log / brand_intelligence / known_parts / site_settings |各自 sqlite/json | Yes | cross-run | (not per-run) |

**EXPECTED verdict:** "user turns and per-candidate scores/verdicts are NOT durably persisted (console prints only)." → **Half-confirmed.** User/agent turns are genuinely ephemeral (the real gap this night fills — capture is NEW durable storage, not duplication). Per-candidate scores/verdicts ARE durable inside `sourcing_results_json` (api_server.py:1182) plus stdout `[Sourcing]` prints in `enterprise_search.py` (97/230/240/499/547…). The capture store will duplicate candidate data as append-only queryable event rows — intentional (event log ≠ state blob).

### I2 — Hook points · EXPECTED met (run_id threading confirmed)
- **(a) Intake turn boundary:** `send_message` handler (api_server.py:1634). User turn → `_messages` at 1657-1659; `IntakeAgent.run` at 1683; agent reply at 1753-1759. `run_id` in scope (path param). `intake_result` derivable from `result` (proceed_state/sufficient/confidence at 1701/1571-582). Upload-nameplate "system" turn at 1772.
- **(b) Sourcing collect/score/reject:** the `[Sourcing]` print sites live in `utils/sourcing_archieved/enterprise_search.py` (97/230/240/499/547…) — **NO `run_id` in scope** there (enterprise_search functions take specs, not run context; `SourcingAgent._run_tier1/2/3` don't thread run_id either). **EXPECTED confirmed:** "scoring/rejection happens in a place with no run_id in scope — capture needs run_id threading." **Decision:** do NOT thread run_id into the load-bearing `sourcing_archieved` query builders (CLAUDE.md §6 — audit all call sites before touching; out of scope). Instead capture `candidate_scored`/`candidate_rejected`/`query_issued`/`results_displayed` from the `result` dict at `_run_sourcing_background` (api_server.py:1107-1186) where `run_id` IS in scope. The literal Tavily query string is built deeper and not captured — `query_issued` captures the per-tier query INTENT derived from specs (the useful flywheel signal); the literal provider query is logged as a not-captured gap.
- **(c) Result assembly:** `_transform_sourcing_results` (api_server.py:945) filters `rejection_reason` to produce the displayed set; full set (incl. rejected) persisted at 1182. `results_displayed` captured from `result` at write-back.
- **(d) Frontend→backend action events (run_id in scope at all):** `select-candidate` (2056), `order-now` (2149), `approve` (2271), `reject` (2346), `confirm-intake` (2379), `outreach` (2445), `save-outreach` (2475), `rfq-draft` (3481), `mark-delivered` (2953). **No backend event source:** "report click" is frontend-only navigation (no endpoint) → logged as a gap.

### I3 — PII path · EXPECTED met; default decision applied (with flagged risk)
- **No PII redaction pipeline exists today.** Grep for `redact|pii|scrub|anonymize|sanitize` in `utils/`+`api_server.py`: only hits are `_redact_sourcing_error` (api_server.py:1251 — redacts *error detail*, not user PII) and test comments. User free text is deliberately kept OUT of stdout/logs (api_server.py:1670-1675) precisely because "structured interaction capture + a deliberate PII policy are a separate (deferred) pass."
- **Default decision (per kickoff I3):** store **post-redaction** text. Since no redaction exists, "post-redaction" = **the text as the intake path sees it** (as-is). Fidelity cost: **none** (no redaction transform to lose). 
- **Flagged risk (Tom must verify):** capture into `run_capture.sqlite` is a NEW durable PII surface — under `RUN_CAPTURE` (default OFF) the demo is unaffected, but flag-ON capture stores visitor free text (names, facility addresses, real part numbers) durably. For a public no-login demo with no consent gate this is a deliberate privacy step; it is exactly the "deferred structured-capture pass" the code comment anticipated. A real redaction pipeline (and consent gate) is a future supervised follow-up; adding redaction later would cost eval fidelity (loss of original phrasing). The flag gate + morning review is the mitigation tonight.

### I4 — Async write option · EXPECTED met
- `/api/health` (api_server.py:2600) returns `{"status","version","demo_mode"}`. The route is on `_DEMO_ALLOWLIST` (api_server.py:171). Adding a `capture_failures` field touches only the **handler body**, NOT the allowlist set or the security middleware (do-not-touch surface) — safe.
- **Existing test pins the exact health body:** `test_api_server.py:837` asserts `resp.json() == {"status":"ok","version":"1.0.0-phase1","demo_mode":False}`. To preserve **flag-off byte-identical inertness** (T5), `capture_failures` is included in the health body **ONLY when `RUN_CAPTURE` is on**. Flag-off → unchanged body → existing test stays green untouched (not weakened).
- `BackgroundTasks` is the house async pattern (api_server.py:2020, 2439) but the brief EXPECTED ("simple try/except + counter sufficient at demo volume") holds — capture writes are one cheap INSERT; **inline synchronous try/except + thread-safe failure counter** is the chosen fail-soft mechanism (not BackgroundTasks, which would hide failures from the request path and complicate the counter).
- **SQLite convention:** raw `sqlite3` module (mirrors `orders.py:39-113`, `supplier_registry.py:72`, `audit_log.py:65`): `_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")`, `_DB_PATH`, `CREATE TABLE IF NOT EXISTS` DDL, idempotent `_migrate`, `_get_conn()` returning `sqlite3.Connection`. `run_capture.py` follows this exact pattern (NOT SQLAlchemy). `data/run_capture.sqlite` only — never writes any other store.

**No investigation finding contradicted a stated assumption.** All four EXPECTED results met. Proceeding to T1.

---
