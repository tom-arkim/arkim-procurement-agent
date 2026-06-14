# CLEANUP.md

Prototype-era technical debt inventory — canonical backlog for post-seed hardening.  
Generated 2026-05-13. **Supersedes `WHATS_NEXT.md`** (see §6).

Excluded from this inventory: unbuilt features with no existing code, active bugs
tracked elsewhere, and frontend UI polish / design iteration items.

**Highest-risk items in this file:**
- **§3.3** — ✅ RESOLVED (Phase R). PN cache now keyed on `(manufacturer, part_number)`; the `part_number` None-guard (L2) is in. Kept for history.
- **§4.1** — RBAC enforcement deferred; any caller can supply any approver role. Acceptable for prototype; not for production. Now also tracks the H1/M1/D2 auth-dependent cluster (see §4.1).

---

## 1. Dead / Archived Code Still in Active Import Path

### 1.1 `utils/sourcing_archieved/` — imported by `SourcingAgent`

| Field | Detail |
|---|---|
| **File** | `utils/sourcing_archieved/` (whole directory: `enterprise_search.py`, `scoring.py`, `constants.py`, `tier3_outreach.py`) |
| **Kind** | Archived pipeline module still live-imported by `SourcingAgent` |
| **Why it exists** | Pre-FastAPI sourcing pipeline preserved during architecture migration; not yet excised |
| **Risk / impact** | Directory name contains a typo (`archieved`). Any change to the calling code must account for archived module behavior. Increases cognitive load when debugging the live pipeline. |
| **Recommended action** | Post-seed: audit all `SourcingAgent` call sites, migrate required logic to the new pipeline, then delete the directory. |

---

## 2. Disabled / Stub Features

### 2.1 `_VERIFIED_PARTNERS` always empty — "Direct Buy via Arkim" badge never fires

| Field | Detail |
|---|---|
| **File** | `utils/sourcing_archieved/constants.py:66` |
| **Kind** | Feature gated on data that doesn't exist yet. The badge logic is correctly wired; the partner dataset is empty. The fix is to load data, not to build the feature. |
| **Why it exists** | Placeholder for onboarded network partners; no partners have been loaded into seed data yet |
| **Risk / impact** | Silent: the badge is never displayed. If a user expects this badge for known partners, it won't appear with no error or warning. |
| **Recommended action** | Post-seed: populate `_VERIFIED_PARTNERS` from the partner onboarding table, or replace with a DB-backed lookup keyed by vendor name. |

### 2.2 `EMAIL_SEND_ENABLED = False` — outreach emails never sent

| Field | Detail |
|---|---|
| **File** | `utils/email_sender.py` (canonical), `utils/sourcing_archieved/tier3_outreach.py` (DEAD), `utils/procurement_agent/outreach.py` (returns `email_send_enabled=False` literal) |
| **Kind** | Hard-coded prototype guard; email send is permanently suppressed at module level |
| **Why it exists** | Prevents accidental emails to real vendors during prototyping and demos |
| **Risk / impact** | The Tier 3 outreach flow completes and marks vendors "Awaiting" without any real communication. **Flag now resolves from the single canonical source for live code** — Phase R (L3) removed the `outreach.py` literal; it reads `email_sender.EMAIL_SEND_ENABLED`. Only the **dead-but-imported** archived copy (`sourcing_archieved/tier3_outreach.py`, §1.1) still re-declares it; that expression goes when the archived dir is retired. |
| **Recommended action** | ✅ Partly done (Phase R, L3): `outreach.py` now reads the canonical flag. Remaining: delete the archived copy when `sourcing_archieved/` is retired. Post-seed: back the canonical flag with an env var (done — opt-in via `EMAIL_SEND_ENABLED`) and add an integration test that stubs the provider and asserts `EmailSender.send` is called when the flag is true. |
| **Status** | The REAL Gmail API is now wired behind `GmailSender` (send) and `GmailInboxReader` (bounces/replies) via `utils/gmail_client.py` (google libs lazy-imported; creds from env; fail-soft). The flag stays **False** and the suite makes **no** real Gmail call (service mocked, no creds). The double gate (`EMAIL_SEND_ENABLED` AND the per-draft approval in `rfq_send`) is unchanged. |

#### Go-live checklist (NOT executed in the repo — Tom runs this on his machine)
1. `uv add google-api-python-client google-auth` (the libs are lazy-imported; not yet in `pyproject`).
2. Provision creds for **procurement@arkim.ai** (Workspace, warmed-up): a service account with domain-wide delegation (scopes `gmail.send`, `gmail.readonly`) → set `GMAIL_SERVICE_ACCOUNT_FILE` (or `_JSON`) and `GMAIL_SENDER=procurement@arkim.ai`. (Or an authorized-user token via `GMAIL_OAUTH_TOKEN_FILE`.) **Never commit creds.**
3. Flip `EMAIL_SEND_ENABLED = True`.
4. **First real send is to SELF** — procurement@arkim.ai → Tom's own inboxes across providers (Gmail/Outlook/etc.) — to verify auth + deliverability (SPF/DKIM/DMARC land **in-inbox, not spam**), and that `message_id`/`thread_id` populate so a self-reply matches via `fetch_replies`.
5. Only after the self-send checks out: send to a real supplier.

---

## 3. Data Layer Placeholders

### 3.1 `audit_log.py` uses raw `sqlite3` — SQLAlchemy migration deferred

| Field | Detail |
|---|---|
| **File** | `utils/audit_log.py:6` (module docstring) |
| **Kind** | Prototype implementation note; module explicitly promises a SQLAlchemy migration |
| **Why it exists** | Quick prototype implementation; no ORM session management wired in |
| **Risk / impact** | Audit log bypasses the SQLAlchemy session lifecycle. Connection handling, transactions, and test isolation are inconsistent with the rest of the stack. |
| **Recommended action** | Post-seed: rewrite using `SessionLocal` from `database.py`; add audit log assertions to existing integration tests. |

### 3.2 `persistence.py` — no migration path, "prototype only"

| Field | Detail |
|---|---|
| **File** | `utils/procurement_agent/state/persistence.py:11` |
| **Kind** | Explicit prototype disclaimer — existing data not migrated on schema changes |
| **Why it exists** | SQLite + Alembic not set up; prototype uses `create_all()` only |
| **Risk / impact** | Any schema change drops and recreates tables; seed data is lost on restart when a schema diff is present. |
| **Recommended action** | Post-seed: add Alembic, write an initial migration from the current schema, replace `create_all()` with `alembic upgrade head` in the startup path. |

### 3.3 `price_db.py` — cache keyed by PN only, no manufacturer cross-validation

| Field | Detail |
|---|---|
| **File** | `utils/price_db.py:46` (`get_cached_prices`) |
| **Kind** | Correctness gap — cache lookup may return prices for a different manufacturer's part with the same PN |
| **Why it exists** | Simple prototype cache; manufacturer-level disambiguation not yet implemented |
| **Risk / impact** | Part number collisions across manufacturers could silently serve the wrong cached price. Low probability in the current 4-brand seed dataset but undetectable if it occurs. |
| **Recommended action** | Post-seed: key the cache on `(manufacturer.lower(), part_number.upper())` composite; add a unit test with a deliberate PN-collision fixture. Becomes higher-risk when the manufacturer count grows beyond the current 4 seeded brands — address before adding new manufacturers with potentially overlapping prefix conventions. |
| **Status** | ✅ RESOLVED (Phase R). `_make_key` composites `(manufacturer.lower(), part_number.upper())`; PN-collision fixture covered in `test_price_db.py`. The `part_number` None-guard (audit L2) is also in. Legacy PN-only on-disk keys cleanly miss and re-populate (no migration). |

---

## 4. Backend Prototype Guards

### 4.1 RBAC enforcement deferred in `approval_rules.py`

| Field | Detail |
|---|---|
| **File** | `utils/procurement_agent/state/approval_rules.py:84` |
| **Kind** | Deferred security enforcement — role-based access check described as post-seed |
| **Why it exists** | Prototype uses fixed roles from the request payload; no auth middleware exists yet |
| **Risk / impact** | Any caller can supply any `approver_role` value. Approval rules are enforced by logic but not by identity verification. Acceptable for prototype demos; not for production. |
| **Recommended action** | Post-seed: integrate with identity provider; validate `approver_role` against authenticated user claims rather than the request body. |
| **Interim** | The internal admin/inspector endpoints (`/api/admin/*`) DO have real enforcement: `require_admin` checks an admin bearer token against the server secret `ARKIM_ADMIN_TOKEN` (401 no header / 403 mismatch / 503 fail-closed when unset; constant-time compare). Since there's no login/session yet, possession of the token == admin. This is the smallest real server-side gate; replace with proper auth claims when the identity provider lands. The admin endpoints are read-only. |
| **Buyer-loop confirm (NEW, ungated)** | The customer-facing buyer-loop mutations — `POST /api/review-items/{id}/confirm` and `/reject`, and `POST /api/runs/{id}/process-replies` — follow the existing **ungated** run-endpoint convention (like `approve`/`select`). **`confirm` is consequential: a quote-confirm writes `price_db` (source="rfq").** When real auth lands, bind these to the **buyer role** (authenticated user claims), not the request path. `process-replies` is read-only (live Gmail `gmail.readonly`, fail-soft without creds, never sends). No price is ever written without an explicit `confirm` — there is no auto-confirm anywhere. |
| **Auth-dependent cluster — H1 routing / M1 / D2 (Phase R)** | The `_execute` phase guard is **in** (Phase R, commit `20df65c`): an order places only from an `approved`/`executing` run, closing H1's select→execute bypass. Three coupled gaps remain and **land together** when the identity/auth layer arrives, because all three require authenticated caller claims (a count/role taken from the request body is not enforcement): **(a) dual-approver routing** — the FastAPI `approve` endpoint advances to `APPROVED` on a single approval regardless of the `$5k`/`$25k` rule (`api_server.py:1099`); **(b) distinct-approver (M1)** — `submit_approval` (`core.py`) counts approval rows but never checks the two approvals come from different identities, so one person can satisfy "2 approvers"; **(c) customer-endpoint tenant-scoping (D2)** — `/api/orders`, `/api/impact`, `/api/reorder`, `/api/sites/{id}/ship-to`, `/api/review-items/*`, and the run endpoints are global/ungated (correct for single-tenant prototype; a cross-tenant leak the moment multi-tenant lands). Do **not** implement any of the three without binding to authenticated claims. Tracked as a single auth-dependent item; see `PHASE_R_RESOLUTION.md`. |

### 4.2 Cache suitability defaults hardcoded — `70.0` (rfq) / `50.0` (live)

| Field | Detail |
|---|---|
| **File** | `utils/sourcing_archieved/enterprise_search.py:96` |
| **Kind** | Magic-number defaults with no documented calibration rationale |
| **Why it exists** | Initial values chosen heuristically; no scoring calibration pass has been done against real data |
| **Risk / impact** | Uncalibrated defaults may over- or under-rank cached results relative to live results, causing suboptimal vendor ordering in the sourcing view. |
| **Recommended action** | Once Tavily quota resolves, capture suitability distribution from live Tavily results, then set defaults at a calibrated percentile (likely 50th). |

### 4.3 `_detect_equip_type` keyword list — consistency audit TODO

| Field | Detail |
|---|---|
| **File** | `utils/sourcing_archieved/scoring.py:83` (function docstring) |
| **Kind** | Incomplete keyword coverage — explicit TODO comment in the docstring |
| **Why it exists** | Keyword list built reactively as new equipment types were encountered; no systematic audit has been done |
| **Risk / impact** | Unknown equipment types silently fall through to the generic scoring path; suitability scores may be less accurate for novel equipment categories. |
| **Recommended action** | Post-seed: audit `_detect_equip_type` against the full set of equipment categories in the seed dataset; add missing keywords and cover with a regression test. |

### 4.4 `_search_vendor_prices` soft-filter behavior diverges from brief §8.3 hard-gate language

| Field | Detail |
|---|---|
| **File** | `utils/sourcing_archieved/enterprise_search.py:73` (function docstring) |
| **Kind** | Documented divergence between brief language and implementation behavior |
| **Why it exists** | Brief §8.3 describes Tier 2 as "Tavily search restricted to known marketplace domains." Implementation uses authority scoring (≥30, +60 for `_VENDOR_DOMAINS` hosts) as a soft filter rather than `include_domains` as a hard gate. The soft-filter behavior is intentional and arguably better than the brief's strict version. |
| **Risk / impact** | The brief is the source-of-truth document; an unreconciled divergence produces confusion during onboarding and design reviews. A vendor not in `_VENDOR_DOMAINS` can surface in Tier 2 if their page has strong commerce signals — behavior the brief language doesn't anticipate. |
| **Recommended action** | Amend brief §8.3 language to match implementation: "Tavily search weighted toward known marketplace domains via authority scoring, falling back to domain-restricted search if too few results surface." Code does not change. The brief is the document that updates. |

### 4.5 api_server ↔ Streamlit divergence on sourcing-failure handling

| Field | Detail |
|---|---|
| **File** | `utils/procurement_agent/orchestrator/core.py:296-315` (`_stub_sourcing`) vs `api_server.py` `_run_sourcing_background` (error branch) |
| **Kind** | Two front ends handle an identical background sourcing failure differently. The api_server/React path advances the run to `Phase.ERROR` (honest failure state; commit `99b48b5`). The Streamlit/orchestrator path catches the same `SourcingAgent` exception and advances to `Phase.COMPARISON` with an empty/error result — masking the failure as "no candidates found." |
| **Why it exists** | The api_server failure-masking was fixed (commit `99b48b5`); the same anti-pattern remains on the Streamlit surface, which was not in scope for that fix. |
| **Risk / impact** | On the Streamlit harness, a real sourcing failure (e.g. Tavily/Anthropic error) presents as a successful run that simply found no vendors — indistinguishable from a genuine zero-result search. This is a debugging trap: it looks like a sourcing bug but is error-masking. The React surface no longer has this problem. |
| **Recommended action** | When reconciling, make `core.py` also advance to `Phase.ERROR` on a hard sourcing failure (matching api_server). Or accept the divergence until Streamlit is retired — Streamlit is the throwaway build/demo harness and the React/FastAPI path is the durable surface (per CLAUDE.md §2/§8). Low urgency if Streamlit retirement is near. |
| **Status** | ⚠️ **RESOLVED AT THE PRODUCT LEVEL, NOT IN THE CODE** (Streamlit surface retired — `app.py`/`pages/` deleted, `streamlit` dep dropped). No user can reach the masking path anymore: `api_server._run_sourcing_background` (the only shipping caller) already advances to `Phase.ERROR`. **HONEST CAVEAT — do not let "moot for the product" become "forgotten in the code":** the masking code (`core._stub_sourcing` → `COMPARISON` instead of `Phase.ERROR`) **still lives in `core.py`**, which is now tests-only (imported by `test_orchestrator*.py`, not by the shipping stack). Fold "`_stub_sourcing` reconcile-to-`Phase.ERROR` **or** retire" into the **Orchestrator-fate decision** (keep as reference / wire FastAPI onto it / retire) — tracked in `docs/PHASE_R_RESOLUTION.md`. |

### 4.6 Apollo suitability clarifier — annotate-only; exclusion ungated; wrong-org risk; `_is_us` hardening deferred

| Field | Detail |
|---|---|
| **File** | `utils/procurement_agent/agents/sourcing_agent.py` (`_apollo_clarify`, `_requirement_match`, `_is_us`); verdict (`suitability_status`) consumed downstream by: nothing yet |
| **Kind** | Intentional prototype boundary + a deferred hardening, recorded so the eventual exclusion step is built safely. See CLAUDE.md §9. |
| **Why it exists** | The Tier 3 clarifier annotates `suitability_status` (`confirmed` / `unconfirmed_flag_human` / `rejected_unsuitable`) but **nothing excludes on it** (annotate-don't-remove). Exclusion was deliberately not built pending verification of the signal's reliability. |
| **Risk / impact** | Apollo can resolve the **wrong org** from a discovered domain and return a self-consistent but wrong verdict — in **both** directions. Reject side: `ibtinc.com` (Tavily vendor "IBT Industrial Solutions", a US/Kansas distributor) → Apollo org = a Lahore, Pakistan "professional training & coaching" company (country/state/address/industry all Pakistani) → `rejected_unsuitable`. Rescue side: J&D Manufacturing was rescued via `qcsupply.com`'s `confirmed` verdict — a *different* org (benign, but the mechanism is wrong). Separately, `suitability_status` and the `suitability_floor` (§4.2 / §8.3) are independent and can disagree (Apollo-`confirmed` US supplier dropped by a sub-floor `suitability_score`). |
| **Recommended action** | (1) **Rescue is now gated** on a name-consistency check (`_names_plausibly_match`: discovery vendor name vs persisted `apollo_org_name`); confirmed-but-name-mismatch (or missing org name) withholds the rescue and annotates `rescue_withheld_name_mismatch`. The reject path stays flag-only. Still removes nothing. (2) Before gating *exclusion* (the future step that actually drops candidates), keep the same discipline — corroboration / human-in-the-loop, see CLAUDE.md §9. (3) **Deferred / not built (was option a1):** a defensive `_is_us` fallback to accept a US state / `…, US` raw_address when Apollo's `country` is blank/wrong. **Demoted after verification** — it addresses an *unobserved* case and would **not** have caught the IBT match (that org is genuinely Pakistani; `_is_us` read `country` correctly, verified reliable across 5 live samples: US/CA, US/IL → confirmed; CN, UK, PK → rejected). Revisit only if a real wrong-country/right-address case appears. |

---

## 5. Frontend / API Calibration

### 5.1 30 s `staleTime` + `refetchOnWindowFocus: false` — cold-navigation gap

| Field | Detail |
|---|---|
| **File** | `frontend/src/lib/query-client.ts:21` |
| **Kind** | Cache calibration concern — not an active bug in the primary confirm-intake → sourcing flow |
| **Why it exists** | 30 s stale time is the global TanStack Query default; `refetchOnWindowFocus: false` avoids noisy refetches during demos |
| **Risk / impact** | The primary flow is unaffected: `confirmIntake` mutation calls `invalidateQueries` on success, triggering an immediate refetch regardless of stale time. The gap only appears when a user navigates cold to a run URL that already has a valid cached response, and does not switch tabs. Stale snapshot can persist up to 30 s in that scenario. |
| **Recommended action** | Post-seed: scope a tighter `staleTime` (≤5 s) to run-detail queries specifically, or enable `refetchOnWindowFocus: true` for run-detail only while keeping it false for list queries. |

### 5.2 `useRunLive` — 5 s polling, TODO comment to migrate to push

| Field | Detail |
|---|---|
| **File** | `frontend/src/lib/queries.ts` (`useRunLive` hook) |
| **Kind** | Prototype polling approach with an explicit TODO to migrate to push / websocket |
| **Why it exists** | 5 s polling is sufficient at prototype scale; SSE/WebSocket adds infrastructure complexity not yet warranted |
| **Risk / impact** | Per commit a721479, polling now extends through the comparison phase in addition to sourcing — active polling phases are broader than a pre-a721479 estimate would suggest. At prototype scale this is fine; at production scale, every open tab polling every 5 s across sourcing and comparison generates meaningful API load on the FastAPI backend. |
| **Recommended action** | Post-seed: replace `useRunLive` polling with an SSE or WebSocket subscription pushed from the FastAPI backend; remove the polling interval. |

---

## 6. Documentation

### 6.1 `WHATS_NEXT.md` — stale Streamlit-era document, superseded by this file

| Field | Detail |
|---|---|
| **File** | `WHATS_NEXT.md` (repo root) |
| **Kind** | Stale planning document generated 2026-04-28 |
| **Why it exists** | Created during the Streamlit phase; the architecture it describes no longer exists |
| **Risk / impact** | Misleading to new contributors: lists "Streamlit replacement (FastAPI/Next.js)" as deliberately deferred — that work is complete. Several of its items are captured in §§2–4 above. Three items unique to it are not covered here: brand intelligence warm-up (performance, feature work), spec enrichment confidence-penalty wiring (dead field in live path, debt), dynamic discovery benchmark and `_AUTHORITY_VIABLE_THRESHOLD` calibration (same character as §4.2, calibration debt). |
| **Recommended action** | `WHATS_NEXT.md` body has been replaced with a redirect to this file. The two debt items (spec enrichment wiring, discovery threshold) can be absorbed into §§4–5 above at the next CLEANUP.md revision. The feature item (brand intelligence warm-up) belongs in a separate roadmap if tracked at all. |

---

*Items are ordered by section, not by priority. All items are prototype-era technical debt —
none block the current demo or the post-seed milestone.*
