# Morning Report — Night 2 (Labeling Surface + Eval Export)

**Branch:** `feature/labeling-surface-overnight` (branched from `test/flag-on-integration` @ `3ada322`, guardrail 1 satisfied)
**Final suite:** `uv run pytest -q` → **1599 passed, 73 skipped, 0 failed** (baseline was 1557 passed / 73 skipped / 0 failed → +42 tests, zero regressions)
**Push:** none (guardrail 10 / NO PUSH honored).

## Commits (per-task, Conventional Commits, suite green each)

| Task | Commit | Summary |
|------|--------|---------|
| T1 | `aaad97e` | `feat(labels): T1 append-only run_labels store` |
| T2 + T4 + T5 + T6 (backend) | `f0197aa` | `feat(labeling): T2 admin labeling endpoints + T4 exporter + T5 provenance` |
| T3 | `5609873` | `feat(labeling): T3 minimal labeling UI in the admin inspector` |
| T6 (strengthened) | `311b7e1` | `test(labeling): T6 strengthen inertness — full flag-off run path, no leakage` |

Single committer: yes — only my commits on this branch (guardrail 2 satisfied).

## Investigation findings (I1–I4) vs EXPECTED

**I1 — run_capture store** (`utils/run_capture.py`). EXPECTED *append-only events keyed by run_id, readable per-run*. **MET.** `run_events` table (`:77-87`), read interface `read_events(run_id)` (`:436`), `read_all_events()` (`:441`), `read_outcome(run_id)` (`:461`), `compute_outcome(run_id)` (`:352`). Exact read interface reported above.

**I2 — admin inspector + auth** (`api_server.py:2817-2920`). EXPECTED *read-only admin views + server-enforced auth exist to extend; reuse that exact auth for label-WRITE endpoints*. **MET.** `require_admin(authorization)` (`:2828`): bearer token vs `ARKIM_ADMIN_TOKEN`; 503 unset / 401 missing-bearer / 403 wrong-token (constant-time compare). Read-only admin views at `/api/admin/runs`, `/api/admin/runs/{id}`, `/api/admin/suppliers`, etc. (`:2859-2920`). Labeling endpoints reuse `require_admin` + add a `RUN_CAPTURE` 503-dormant layer.

**I3 — eval schemas**. EXPECTED *schemas exist; required fields per schema + where provenance attaches*. **MET — with a blocker on the scoring suite (below).**
- Intake (`fixtures/intake_eval_dataset.json`, validator `test_intake_eval_dataset.py`): example = `{input, expected_part_type, expected_component_of, expected_regime, split}`. Validator checks required keys PRESENT (not absence of others) → `provenance` attaches cleanly as an extra key.
- Scoring (`fixtures/scoring_eval_dataset.json`, validator `test_scoring_eval_dataset.py`): case = `{id, split, request{manufacturer,model,part_number,voltage,category,detected_type,hp?}, result{snippet,url,title,found_pn}, expected{should_pass_floor,rationale}}`. Same — `provenance` attaches cleanly.

**I4 — outcome signals** (`run_capture.py:324-329`). EXPECTED *run_outcomes can drive a failures-first queue*. **MET.** Statuses: `completed_with_action`, `abandoned_after_results`, `zero_results`, `all_rejected`, `rephrased`, `incomplete`. Failures-first = deprioritize `completed_with_action` (rank 5); the rest sort first (`api_server.py` `_OUTCOME_RANK`).

## ⚠ Blocker — T4 scoring export live-faithfulness (guardrail 8: logged, dependent path partial)

The scoring eval feeds the scorer `_compute_suitability_score(specs, snippet, url, found_pn, title)` — `snippet` is a positional arg (`test_scoring_eval_run.py:107`). **`snippet` and `title` are not durably captured anywhere**: `snippet_map` is local to `enterprise_search.py:142-145`; the persisted `SourcingOption` candidate (`enterprise_search.py:229-250`) carries `source_url` + `found_part_number` but NOT `snippet`/`title`; `run_capture.capture_candidate` (`run_capture.py:251-291`) likewise omits them.

Reconstructing a scoring case from the capture store would feed the scorer different input than the live path — exactly the live-faithfulness violation this codebase has shipped twice (guardrail 7). **Decision per guardrail 8:** the intake export is built fully (live-faithful — `input` = the first `turn_user` text, which the live classifier consumes at `intake_agent.py:413`/`:518`); the scoring export machinery is built but **WITHHOLDS** scoring cases with a logged reason (`"snippet/title not captured (Night 1 gap)"`) when `snippet`/`title` are unavailable (currently always). Extending capture to thread `snippet`/`title` onto the candidate would touch `sourcing_archieved` (the "audit all SourcingAgent call sites before touching" constraint, CLAUDE.md §6) — out of scope for an unmanned night.

**This is the single blocker.** The exporter, the labeling endpoints, the UI, and the intake export all work end-to-end; only the scoring *case emission* is gated to zero today (by design, honestly).

## Per-task status

- **T1 — run_labels store** (`utils/run_labels.py`): DONE. Own sqlite store (`data/run_labels.sqlite`, WAL, mirrors `run_capture`), flag-gated on `RUN_CAPTURE`, fail-soft with `label_failures` counter. Append-only — each POST inserts; `current_label` returns the latest by ts. Two scopes: `run` (intake ground truth + verdict) and `candidate` (right part type + floor verdict). 18 unit tests (`test_run_labels.py`).
- **T2 — admin labeling endpoints** (`api_server.py`): DONE. Five endpoints, all `require_admin` + `RUN_CAPTURE`-gated: `GET /api/admin/labeling/queue` (failures-first), `GET /api/admin/labeling/runs/{id}`, `POST /api/admin/labeling/label`, `POST /api/admin/labeling/export`, `GET /api/admin/labeling/provenance`. Flag off → 503 dormant.
- **T3 — labeling UI** (`frontend/src/app/admin/page.tsx`): DONE. A "Labeling" tab in the admin inspector: failures-first queue (left), a run's input/intake/candidates with score+verdict (right), one-click + keyboard label controls (1–9 open rows, `s` saves run label, `x` exports, `Esc` clears). Function over polish. `tsc --noEmit` clean. Degrades gracefully (503) when the flag is off.
- **T4 — exporter** (`utils/eval_export.py`): DONE (intake live-faithful; scoring honestly withheld — see blocker). Intake case `input` = first user turn; `provenance:"real:<run_id>"`; dev/holdout = deterministic sha256(run_id) ~2/3·1/3; validates each case against the existing schema before appending; dedups on relabel. Appends to **real-cases dataset files** (`fixtures/intake_eval_dataset_real.json` / `scoring_eval_dataset_real.json`) — the committed synthetic fixtures are never touched, so their tests stay byte-identical.
- **T5 — provenance metric** (`eval_export.provenance_report`): DONE. Reports `% real vs synthetic` per suite (intake + scoring), combining the synthetic fixture + the real-cases file, with per-split breakdown. Surfaced at `GET /api/admin/labeling/provenance`.
- **T6 — inertness** (`test_labeling_api.py::TestInertness`): DONE. Flag off → `/api/health` byte-identical to pre-Night-2 (`{"status":"ok","version":"1.0.0-phase1","demo_mode":false}`); labeling endpoints 503; existing admin (`/api/admin/ping`) unchanged; a full run+messages+confirm-intake cycle writes ZERO capture/label rows and leaks no Night-2 artifacts; the label store DB file is never created.

## Unspecified decisions (every one logged)

1. **Label payload carries structured ground truth, not just a verdict.** The brief's T1 names `intake_correct + corrections` and `right_part_type + should_pass_floor`. To export a drop-in *intake* eval case, the label must carry the ground truth `expected_part_type / expected_component_of / expected_regime` (the eval case's required fields), not just a bool. I included those explicitly in the run-scope label payload alongside `intake_correct` + `corrections`/`note`. The `corrections` field is folded into `note`. This is the minimal extension that makes export possible without re-reading the brief's mind.
2. **Real-cases dataset files are siblings of the synthetic fixtures, not the fixtures themselves.** Appending to `intake_eval_dataset.json` would change the committed fixture and break its validator's count assertions (`20 <= len(cases) <= 30`, `len(examples) >= 24`). New files `intake_eval_dataset_real.json` / `scoring_eval_dataset_real.json` keep the synthetic fixtures byte-identical; `provenance_report` combines both for the headline `% real`.
3. **Scoring export = honest scaffolding (withholds cases) rather than a fake live-faithful export.** Per guardrail 8 + the live-faithfulness rule. See blocker.
4. **`label_failures` surfaces on `/api/health` only when `RUN_CAPTURE` is on**, mirroring Night 1's `capture_failures` (byte-identical flag-off health).
5. **Queue failures-first ordering** ranks `completed_with_action` last; ties break by `created_at` desc (string-safe sort).
6. **Labeling UI tab is always present** (static React); the backend 503 is the real dormant gate. "Byte-identical API" (T6) is about the API, which holds.
7. **`labeled_by` defaults to the admin role label** (`"admin"`) since the admin-token-possession model has no per-user identity yet (§6 RBAC gap).

## Blockers

- **Scoring eval export cannot emit cases yet** (snippet/title not in the capture store). Morning decision needed: extend Night 1 capture to thread `snippet`/`title` onto the candidate (touches `sourcing_archieved` — audit call sites first), OR accept that the scoring flywheel starts once that lands. Intake export is unblocked and live-faithful today.

## Morning-verification inputs (what to check)

1. **Branch base:** `git log --oneline -1 3ada322` → should be the merge commit; `git log --oneline feature/labeling-surface-overnight ^3ada322` → the 4 Night-2 commits only.
2. **Suite:** `uv sync --group dev && uv run pytest -q` → expect **1599 passed, 73 skipped, 0 failed**.
3. **Inertness:** `uv run pytest utils/procurement_agent/tests/test_labeling_api.py::TestInertness -q` → 5 passed. With `RUN_CAPTURE` unset, `GET /api/health` is `{"status":"ok","version":"1.0.0-phase1","demo_mode":false}` (no `label_failures`/`capture_failures`).
4. **Live-faithful intake export:** `uv run pytest utils/procurement_agent/tests/test_labeling_api.py::TestExportLiveFaithful -q` → 4 passed. The exported intake case's `input` equals the first user turn (not a reconstruction).
5. **Scoring withhold:** `TestExportLiveFaithful::test_export_withholds_scoring_when_snippet_missing` proves scoring emission is gated to zero today, with a logged reason — not a silent fake.
6. **Flag-on smoke (manual, requires admin token):** `ARKIM_ADMIN_TOKEN=x RUN_CAPTURE=1 uvicorn api_server:app --port 8001`, then `GET /api/admin/labeling/queue` (Bearer x) → 200 + failures-first queue; `POST /api/admin/labeling/export` → `{"intake":{"emitted":N,...},"scoring":{"emitted":0,"withheld":M,...}}`.
7. **UI:** `cd frontend && next dev` → `/admin` → "Labeling" tab. With the flag off it shows HTTP 503; with it on, the queue + run labeling view render.
8. **Do-not-touch honored:** `.env`, `audit/`, `scripts/*_self_test.py`, `seed/demo fixtures`, `known_parts.json`, `price_db.json`, `DEMO_MODE` gates, the security/allowlist surface, `phase3` branch — all untouched (verify `git diff 3ada322 --stat` lists only `utils/run_labels.py`, `utils/eval_export.py`, `api_server.py`, `frontend/src/app/admin/page.tsx`, `utils/procurement_agent/tests/test_run_labels.py`, `utils/procurement_agent/tests/test_labeling_api.py`).
