# Arkim Procurement — Cowork Overnight Audit Brief

**Status:** Internal — v1.2
**For:** an unattended overnight Claude Cowork run.
**Goal:** produce a complete, structured **review-and-propose report** across the codebase — findings + proposed diffs, **nothing applied** — that Tom reviews and approves in the morning. This is the read-only analysis half of Phase R; the approve-and-apply half stays attended.

**Changelog**
- **v1.2** — Production-deployment-readiness lens added. Per-module header gains `loc` + `loc_verdict` (lean / acceptable / bloated / egregious — thresholds in §6.1). Per-module protocol §3 gains an explicit **bloat pass** (dead code, unused imports/exports, cross-module duplicated logic, over-abstracted indirection, commented-out code, copy-paste). Bloat findings carry a distinct severity letter **B** (separate from H/M/L for security/correctness). `audit/INDEX.md` columns enriched to **Tier · Module · ★ · LOC · LOC verdict · H · M · L · B · Module verdict · Link** (with totals row). New top-of-stack file **`audit/PRODUCTION_READINESS.md`** — a single ready/ready-with-caveats/blocked verdict with blockers, pre-prod cleanup, post-prod nice-to-haves, coverage attestation, security attestation, and test-suite state.
- **v1.1** — Output format changed from a single consolidated `PHASE_R_AUDIT_REPORT.md` to **one Markdown file per module** under a new `audit/` subfolder, plus `audit/INDEX.md`, `audit/SECURITY.md`, `audit/KNOWN_INPUTS.md`, and `audit/SUMMARY.md`. Filesystem-write rule and §6 updated accordingly. The prior consolidated report (`PHASE_R_AUDIT_REPORT.md`) and its addendum (`PHASE_R_AUDIT_ADDENDUM.md`) **must not be modified or deleted** — they remain the historical record of the v1.0 run.
- **v1.0** — Initial brief.

---

## 0. The single most important rule

**PROPOSE, DO NOT APPLY.** This run produces a report and proposed diffs. It must not commit, must not modify code that ships, must not "fix" anything. Every finding is a recommendation for Tom to approve later. If at any point the only way to proceed is to change code and move on, **stop and write it up instead.**

If a hard constraint below would be violated, **halt and report** rather than working around it.

---

## 1. Hard constraints (non-negotiable for an unattended run)

1. **No code changes applied.** Output is findings + proposed diffs in the report files under `audit/`. Do not edit source that ships; do not commit; do not push. **Creating files inside `audit/` is the only filesystem write permitted; no source-code changes; do not modify or delete `PHASE_R_AUDIT_REPORT.md` or `PHASE_R_AUDIT_ADDENDUM.md`** (those are the v1.0 record and stay frozen).
2. **No live or paid external API calls — ever.** Do NOT call Apollo, Gmail, Anthropic-paid, Tavily, or any network endpoint that costs money or takes a real-world action. Reviewing *reads code*; it does not *execute* paid endpoints. The test suite already mocks these — running `pytest` is fine; invoking a real client is not.
3. **Behaviour-preserving lens only.** Propose nothing that changes external behaviour without flagging it loudly as a behaviour change requiring explicit sign-off. The 686-test suite is the definition of current behaviour.
4. **Suite stays green.** If `pytest` is run to confirm a baseline, it must be green before and after any *exploratory* local change — and any such change must be reverted before moving on (the working tree ends clean). If the suite is red at the start, **halt and report** — do not audit on top of a broken baseline.
5. **Read-only on secrets.** Do not read, print, copy, or move `.env`, the Gmail service-account key, or any credential. If a security finding concerns them, describe the *issue* (e.g. "key path is logged at line N") without reproducing the secret.
6. **Prioritise by impact; no trivia.** A long list of nitpicks is noise. Surface what matters. If a module is sound, say "sound — no action" and move on.
7. **If a module is already good, say so.** Do not manufacture changes to look productive. "No issues found" is a valid and valuable result.

---

## 2. Module inventory — review in dependency order (leaf-first)

Work through these in order. **Consequential-action modules get the deepest scrutiny** (marked ★ — a hidden bug there costs real money, a real email, or corrupted shared data).

**Tier 1 — leaf utilities (deepest on the ★):**
- ★ `utils/email_sender.py` / `GmailSender` — the send path + the double gate.
- ★ `utils/inbox_reader.py` / `GmailInboxReader` — the read/match path.
- `utils/bounce_parser.py`, `utils/bounce_processor.py`
- ★ `utils/quote_extractor.py` — extraction + the ocr_text seam + abstention.
- `utils/contact_extractor.py`
- ★ `utils/orders.py` — the order state machine.
- ★ `utils/price_db.py` — the price write path.
- ★ `utils/impact.py` — the savings/time calculator (the measured-vs-estimated tiers).
- `utils/supplier_registry.py`

**Tier 2 — processors:**
- ★ `utils/rfq_send.py` — the RFQ send + record path.
- ★ `utils/reply_processor.py` — reply → match → extract → queue → confirm.
- `utils/bounce_processor.py` (if not covered above)

**Tier 3 — agents / orchestration:**
- `sourcing_agent.py` — the sourcing/suitability/dedup/tiering pipeline.
- `procurement_agent.py` — the orchestrator (`execute`, etc.).
- intake / spec-extraction modules.

**Tier 4 — surface:**
- ★ `api_server.py` — endpoints, the admin-token gate, CORS, the new impact/review endpoints.

**Tier 5 — frontend (lighter pass; correctness + the invariants, not deep optimisation):**
- The `proc-*` customer components — confirm they drive *real endpoints* (no local-state shortcut that skips a `price_db` write or an order placement); confirm impact comes from the endpoint, never recomputed.

---

## 3. Per-module protocol (apply identically to each)

For each module, in the report:

1. **What it does** (1–2 lines) — confirm understanding.
2. **Correctness pass** — silent failures, swallowed exceptions, unhandled edges, wrong-attribution / wrong-action risks. *Highest priority on ★ modules.* Specifically hunt the session's recurring bug class: errors caught and discarded, a destructive action on uncertain data, a write that should be gated.
3. **Efficiency pass** — redundant **external calls** (= cost: Apollo credits, LLM tokens, search) and repeated computation. **Identify before optimising; do not propose micro-optimisations on unmeasured paths.** Flag the redundancy; let Tom decide.
4. **Clarity pass** — unclear names, oversized functions, "a senior dev would rewrite this" smells. Keep brief.
5. **Bloat pass** — surface what's dead weight before production: **dead code** (unreachable branches, functions with no call sites), **unused imports / unused exports**, **duplicated logic** that ought to live in one place (cross-reference the other module's path explicitly), **over-abstracted indirection** (single-call-site factories, one-implementation interfaces, wrappers that just forward), **commented-out code**, **copy-paste patterns** (near-identical blocks across files). Findings here carry severity **B** (distinct from H/M/L). Default disposition is *propose removal or consolidation* with the diff written out. Do not flag stylistic preference as bloat — only things that are objectively unused or objectively duplicated.
6. **Finding entries** — for each issue: `module:line` · what · why it matters · severity (`high` / `med` / `low` for security/correctness, **`B` for bloat**) · **proposed diff** (written out, not applied) · whether it's behaviour-preserving or a behaviour change.

If nothing: "**No issues found — sound.**"

---

## 4. Dedicated security / secrets pass (one section, after the modules)

The consequential surface grew this arc — give it a focused section:
- **Credential handling** — how the Gmail key + `ARKIM_ADMIN_TOKEN` + API keys are loaded, whether any is logged/printed/echoed, whether any reaches an error message or response body.
- **The admin gate** — `require_admin` / the bearer-token mechanism: fail-closed correctness, constant-time compare, any path that bypasses it. (Already flagged interim in CLEANUP — confirm it's at least correctly fail-closed.)
- **The customer surface** — any endpoint that should be tenant/site-scoped but isn't; any data leak across tenant/site; the new impact/review/order endpoints' scoping.
- **Input handling** — anywhere external input (email bodies, PDF bytes, quote text, form fields) reaches something dangerous (eval, SQL string-building, path construction).
- Describe issues without reproducing any secret value.

---

## 5. Known inputs to confirm (from prior sessions — re-verify, propose, don't fix)

These are already-flagged items; confirm whether each still reproduces on current code and propose the fix as a diff (do not apply):
- **Dedup cross-name aliases** under-merge (OTC vs Great Lakes) — `sourcing_agent.py` `_normalize_vendor_name` / `_dedup_across_tiers`. (Lexical key can't catch aliases — needs an entity-resolution layer; propose the approach.)
- **Tiering by lane not signal** — a priced marketplace stuck Tier 3 — `sourcing_agent.py` `_run_tier2`/`_run_tier3`. Propose commerce-signal promotion.
- **MROSupply despaced name match** — `sourcing_agent.py` `_normalize_org_name`/`_names_plausibly_match` — add a despaced/concatenated comparison.
- **`impact.py` cumulative `last_paid`** — uses most-recent purchase across runs, not strictly the one preceding each order's date. Propose the date-aware refinement.
- **`replies_chased` = 0** — no chase-action log yet; confirm it's sourced honestly (counted, not estimated) and propose where a real chase log would wire in.
- **The `<img>` → `next/image`** ESLint warning in `message-bubble.tsx` — propose the swap.

---

## 6. Output format (per-module files under `audit/`)

Create a new `audit/` subfolder at the repo root. Write **one Markdown file per module** plus four cross-cutting files. This is the only filesystem write permitted. Do **not** modify or delete the prior `PHASE_R_AUDIT_REPORT.md` or `PHASE_R_AUDIT_ADDENDUM.md` — they are the v1.0 record and stay frozen.

### 6.1 Per-module files

One file per module reviewed in §2 — backend Python and reviewed frontend files alike. **Filename = module path with `/` → `_` and `.py` → `.md`** (frontend uses `.tsx`/`.ts` → `.md` analogously). Examples:

- `utils/models.py` → `audit/utils_models.md`
- `utils/email_sender.py` → `audit/utils_email_sender.md`
- `utils/procurement_agent/agents/procurement_agent.py` → `audit/utils_procurement_agent_agents_procurement_agent.md`
- `api_server.py` → `audit/api_server.md`
- `frontend/src/components/proc/request-screen.tsx` → `audit/frontend_src_components_proc_request-screen.md` (preserve hyphens; only `/` and the extension are transformed)

Each per-module file follows the per-module protocol from §3 exactly:

1. **What it does** (1–2 lines).
2. **Correctness pass** — silent failures, swallowed exceptions, unhandled edges, wrong-attribution / wrong-action risks. Deepest scrutiny on ★ modules.
3. **Efficiency pass** — redundant external calls, repeated computation. Flag, don't micro-optimise.
4. **Clarity pass** — naming, oversized functions, "a senior dev would rewrite this" smells.
5. **Bloat pass** — dead code, unused imports/exports, cross-module duplicated logic, over-abstracted indirection, commented-out code, copy-paste. Severity letter **B**. See §3.5 for the full description.
6. **Finding entries** — for each issue: `module:line` · what · why it matters · severity (`high` / `med` / `low` / **`B`**) · **proposed diff** (written out, not applied) · whether it's behaviour-preserving or a behaviour change. If nothing: "**No issues found — sound.**"

Each file carries a short YAML-ish header so `INDEX.md` can be assembled mechanically:

```
---
module: utils/email_sender.py
star: yes
loc: 312
loc_verdict: acceptable
verdict: sound
findings: { high: 0, med: 0, low: 1, bloat: 0 }
---
```

**LOC verdict thresholds** (count non-blank, non-comment source lines; round if a project convention contradicts these — note the contradiction and the convention used):

- `lean` — < 200 LOC
- `acceptable` — 200–500 LOC
- `bloated` — 501–900 LOC
- `egregious` — > 900 LOC

`loc_verdict` is informational — it does **not** by itself create a finding. A bloated module with no extractable units stays "sound" with `loc_verdict: bloated`. A bloated module that *does* have extractable units gets a **B** finding proposing the split, and `loc_verdict` stays as-counted. Frontend files use the same thresholds.

### 6.2 `audit/INDEX.md`

Table of every per-module file, in the Tier order from §2. Columns:

**Tier · Module · ★ · LOC · LOC verdict · H · M · L · B · Module verdict · Link**

- `LOC` is the count from the per-module file's header.
- `LOC verdict` is `lean` / `acceptable` / `bloated` / `egregious` per §6.1 thresholds.
- `H` / `M` / `L` are HIGH / MED / LOW security/correctness finding counts.
- `B` is the bloat-finding count.
- `Module verdict` is the one-line summary (e.g. `sound`, `1 HIGH — H1`, `bloated — extract X`).
- `Link` is the relative path to the per-module file (e.g. `./utils_email_sender.md`).

Include a **totals row** at the bottom summing LOC, H, M, L, B across all modules. The totals row is the at-a-glance "how big is the codebase, how many findings of each kind." The single place to scan everything at a glance.

### 6.3 `audit/SECURITY.md`

The dedicated security / secrets pass (§4). Same content the §4 spec already calls for: credential handling, the admin gate, the customer-surface tenant/site scoping, input handling (eval / SQL string-building / path construction / upload paths). Findings here follow the same severity + proposed-diff format. Describe issues without reproducing any secret value.

### 6.4 `audit/KNOWN_INPUTS.md`

The §5 known-inputs re-verification — each prior-flagged item marked **still-reproduces / changed / moot**, with the proposed-diff in place if still reproducing. One section per item.

### 6.5 `audit/SUMMARY.md`

- **Executive summary** — top findings by severity; overall codebase health; "what to fix first."
- **Proposed-diffs appendix** — the actual diffs, grouped, each labelled behaviour-preserving or behaviour-change, so Tom can approve-and-apply selectively in the morning. Pull diffs from the per-module files into one consolidated appendix here.
- **"Did NOT do" note** — anything skipped, any halt, anything that needed attended judgement.

### 6.6 End state

- Working tree clean **except** the new `audit/` subfolder and its contents; nothing committed; nothing pushed.
- `PHASE_R_AUDIT_REPORT.md` and `PHASE_R_AUDIT_ADDENDUM.md` are byte-identical to before the run (the v1.0 record is preserved).
- No live or paid external call was made.
- The suite is green.

If any of those can't be guaranteed, **halt and report why** — write the halt note into `audit/SUMMARY.md` under "Did NOT do."

### 6.7 `audit/PRODUCTION_READINESS.md` — the top-of-stack file

This is the file Tom reads first. Strict structure, in this order:

**1. Verdict** — exactly one of:
- `ready` — no blockers; no unaddressed HIGH findings; security attestations all pass; no critical bugs.
- `ready-with-caveats` — no blockers, but pre-prod cleanup is recommended (MEDIUM findings, bloat that affects maintainability, non-critical attestations partial).
- `blocked` — at least one HIGH finding, failing security attestation, or critical bug that must be fixed before ship.

State the verdict on line one. One sentence of plain-language rationale.

**2. Blockers table** — anything that must be fixed before prod ship. Typically every HIGH finding from any module, plus any failing security attestation, plus any critical correctness bug. Columns: **ID · Source file · Finding · Why it blocks ship · Proposed diff link**. If empty, write "**None.**"

**3. Pre-prod cleanup table** — recommended-but-not-blocking before ship. Columns: **ID · Source file · Finding · Severity (M / B) · Effort estimate (S/M/L) · Proposed diff link**. Includes all MEDIUM findings and all bloat findings that meaningfully affect maintainability or shipped surface area. If empty, write "**None.**"

**4. Post-prod nice-to-have table** — defer until after ship. Columns: **ID · Source file · Finding · Severity (L / B) · Proposed diff link**. Includes LOW findings and stylistic / cleanup bloat that's low-impact. If empty, write "**None.**"

**5. Coverage attestation** — confirm every module in the §2 inventory was reviewed. Format:

| Tier | Module | Reviewed? | Per-module file |
|---|---|---|---|
| 1 | `utils/email_sender.py` | yes | `./utils_email_sender.md` |
| … | … | … | … |

If anything was **skipped**, mark `skipped — <reason>` (e.g. file did not exist, deferred because out-of-scope, etc.). Skipping silently is not allowed.

**6. Security attestation** — explicit verdict per area. One row each:

| Area | Verdict | Notes |
|---|---|---|
| Secrets handling (`.env`, Gmail key, API keys) | pass / fail | … |
| Auth / tenant scoping (admin gate, customer-surface scoping) | pass / fail | … |
| Input validation (email bodies, uploads, form fields, SQL/path/eval reach) | pass / fail | … |
| External API safety (paid-call gating, fail-soft, key-presence checks) | pass / fail | … |

`pass` = no HIGH finding in this area and the area was actually examined. `fail` = HIGH finding present or area not adequately reviewable from the code. Cross-link each row to the relevant finding in `SECURITY.md` or the per-module file.

**7. Test-suite state at audit time** — exact result from running the suite, plus lint state if checked:

- `pytest`: `<N> passed, <M> failed, <K> warnings` (paste the actual summary line).
- Frontend lint / type-check, if run: state the command and result.
- Note the Python version, the interpreter path, and whether it was the canonical `.venv` or a copy.

If the suite is red, **the verdict cannot be `ready` or `ready-with-caveats`** — it must be `blocked` with the suite failure as a blocker.

**8. "What to fix first" priority list** — the top 3–5 items in order. This is the morning to-do list. Each item: one line, finding ID, source file.

This file's structure is fixed — Tom will read it in this order every audit.

---

## 7. The one-line standing order

Read everything, change nothing, spend nothing, propose precisely, and tell Tom honestly what you found — including "this is sound" where it is.
