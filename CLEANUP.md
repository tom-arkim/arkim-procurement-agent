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
| **Auth-dependent cluster — H1 routing / M1 / D2 (Phase R)** | The `_execute` phase guard is **in** (Phase R, commit `20df65c`): an order places only from an `approved`/`executing` run. **(a) dual-approver routing (H1) — LANDED:** `select-candidate` persists `_approval_path` via `determine_approval_path(facility, total)`; `approve` routes a `>= 2`-approver purchase through `pending_second_approval` (threshold routing, auth-independent). **(b) distinct-approver (M1) — LANDED (demo-safe):** `approve` now takes the optional `get_caller` dependency; when a verified identity is present it records the authenticated `sub` as `approver_id` and rejects a second approval from the SAME `sub` (409), enforcing on the verified claim ONLY — never the body `approver_name`/`role`. With no token (today's no-auth demo) `approver_id` is null and distinctness is not enforced — unchanged behaviour until the frontend forwards Cognito identity. **(c) tenant-scoping (D2) — prereq #1 KEYS LANDED; enforcement still deferred:** the tenant key now exists. `sourcing_runs` and `orders` gained a **nullable `company_id` (PIN)** — added to the ORM/DDL + idempotent ALTER (`_migrate_schema` for runs; `orders._migrate` PRAGMA-guarded), populated from the verified `Caller.company_id` on `POST /api/runs` and `from-maintenance` (the validated `X-Arkim-CompanyId`, NEVER the body) and copied transitively onto orders (run → `run.company_id`). `facility_id` is UNCHANGED — it stays the separate site label (the `facility_id ↔ Site.id` reconciliation is still open; current values are demo labels like `fac-stockton`, not `Site.id`). All NULL in the no-auth demo → zero behaviour change, no enforcement. **D2 enforcement remains blocked on the other two prereqs:** (2) core's `assigned_sites` for site-scoping; (3) the frontend forwarding the Cognito JWT + `X-Arkim-CompanyId` (`request()` has no auth header today; tenant/site is a client fixture in `proc-config.ts`). When those land, scope each customer endpoint by `Caller.company_id` (fail-open in dev / fail-closed when Cognito is configured) — NEVER on body-supplied identity. **Still-keyless stores (by design, not gaps):** `site_settings` (keyed by caller-supplied `site_id` — scope needs prereq #2 `assigned_sites`); `review_items`/`sent_messages` (scope transitively via `run_id`); `known_parts`/`price_db`/`suppliers` (global shared graph — **shared-vs-per-tenant is Tom's product call**, not a schema gap). See `PHASE_R_RESOLUTION.md`. |
| **Client-supplied `group_id` on `POST /api/runs` — unvalidated (NEW, per-item basket / Stage C)** | `CreateRunRequest.group_id` is **client-supplied and unvalidated** — a caller can pass an arbitrary `group_id`, including **another tenant's**, attaching a run to a basket it shouldn't see. **No new gap today:** RBAC is unenforced everywhere (this §4.1), and `get_group`/`approve_group` already query by `group_id` with **no tenant filter** — so this trusts the client no more than the rest of the surface does. **When auth lands, two things are required:** (1) the group endpoints (`get_group`/`approve_group`) MUST verify the caller may access the group (scope by `Caller.company_id`, like the other D2 stores); and (2) the server should **validate or mint** the `group_id` (e.g. mint server-side, or check it against the caller's groups) rather than trusting the body. Introduced by the per-item basket flow (mint-up-front, client-supplies-the-UUID) so single-item lists can proceed to sourcing as a basket. `_new_run_orm` already carried `group_id`; only the entry point is new. |
| **Client-supplied `asset_specs` seed — `POST /api/runs` + `PUT /api/runs/{id}/asset-specs` — unvalidated (NEW, multi-part auto-split / Stage 2)** | The multi-part fan-out seeds each card from already-parsed specs: `CreateRunRequest.asset_specs` (birth-seed) and **`PUT /api/runs/{id}/asset-specs`** (post-birth seed onto an existing run) both write **client-supplied specs, unvalidated**, onto a run. The PUT is scoped to the seed purpose only — it sets `asset_specs_json` and nothing else (no phase advance, no re-extraction, no sufficiency). **No new gap today:** RBAC is unenforced everywhere (this §4.1), so this trusts the client no more than `send_message`/`upload` (which already write specs from client input). **When auth lands:** scope both by `Caller.company_id` — a caller may seed only **their own** run's specs (the PUT additionally 404s an unknown run, but does not yet check ownership). Mirrors the `group_id` birth-seed debt above; same posture. |
| **Identity binding LANDED (`utils/auth`) — not yet enforced** | `utils/auth/` ports core's Cognito JWT auth (RS256/JWKS verify, issuer/aud/exp/token_use, `CognitoUser` + `custom:companies_procurement` as the NEW procurement role, admin-superset `check_company_role`, `X-Arkim-CompanyId` tenant binding validated against token claims, and `INTERNAL_REQUEST_SIGNATURE` s2s elevation that bypasses ROLE only). It exposes `get_caller()` (pure dependency) + `require_role()`. **Boundary flag (clean new code abutting old):** this is house-standard typed/tested/fail-soft code sitting beside the ungated `api_server.py` — **no endpoint depends on it yet** (deliberate). Next: **H1 threshold routing** lands independently (auth-free, from `determine_approval_path`); then **M1** (authenticated `sub` as `approver_id` + distinctness) and **D2** (scope each store by `Caller.company_id`) land together atop `get_caller`, with the `orders` tenant-key + the global-cache shared-vs-per-tenant decision as sub-prerequisites. Env: `COGNITO_ISSUER_URL` / `COGNITO_CLIENT_ID` / `INTERNAL_REQUEST_SIGNATURE`. Site-scoping deferred (needs core's `assigned_sites`; fails closed later). |

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
| **Status** | ✅ **RESOLVED IN CODE.** `core._stub_sourcing` now routes a sourcing **FAILURE** (the agent raised) to `Phase.ERROR` — matching `api_server._run_sourcing_background` — and keeps `COMPARISON` only for a **successful** search (incl. a genuine zero-candidate result, which is not an error). The masking pattern (failure → `COMPARISON`) **no longer exists anywhere in the codebase**, tests-only or not. Both paths now propagate sourcing failure honestly. Covered by `test_sourcing_failure_advances_to_error` + `test_sourcing_empty_but_successful_stays_comparison`. (The separate Orchestrator-fate decision — keep / wire FastAPI onto it / retire — is unrelated to this fix; see `docs/ORCHESTRATOR_DECISION.md`.) |

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

### 5.3 `purchaseChannel` is transform-derived (display-only) — deferred model-field decision

| Field | Detail |
|---|---|
| **File** | `api_server.py` `_transform_option` (`purchaseChannel`); `utils/marketplace_registry.py` (curated allowlist) |
| **Kind** | Deliberate deferred decision (State M / increment 2), recorded so it isn't mistaken for an oversight. |
| **Why it exists** | State M ("marketplace / buy now") is **display-only** this increment — a label + a coming-soon button, no real purchase behaviour. So `purchaseChannel` ("marketplace" \| "reference") is **derived in the transform** from `is_marketplace(source_url)` + a real price, not stored on the model. Marketplace detection is the **curated registry only** (no commerce-signal / add-to-cart parsing — a deliberate non-goal). |
| **Risk / impact** | None today (display label). The registry is a manually-curated allowlist — a wrong entry would mis-label a row "buy direct"; kept obviously editable in `marketplace_registry.py`. |
| **Recommended action** | When manual-fulfilment **"buy now" becomes a real ACTION** (not just a label), promote `purchase_channel` to a `SourcingOption` model field set during sourcing — at that point it drives behaviour, not just display, and should be persisted/auditable rather than re-derived per request. Revisit registry-vs-commerce-signal detection then too. |

---

### 5.4 State C — backend landed (3a key-carry + 3b quote overlay); frontend rendering pending

| Field | Detail |
|---|---|
| **File** | `utils/supplier_registry.py` (`review_items` DDL + `_migrate` + `record_review_item`); `utils/reply_processor.py` (`process_replies`); `api_server.py` (`_index_quotes` / `_build_quote_index` / `_resolve_quote` / `_quote_overlay` / `_transform_sourcing_results` / `_orm_to_detail`) |
| **Kind** | Increment landed (3a prerequisite + 3b backend assembly/overlay). The **frontend rendering** of State C is the remaining half — recorded so the half-wired state is visible. |
| **What landed (3a)** | `review_items` gained nullable `thread_id` / `sent_message_id` / `message_id`. `process_replies` carries the matched `sent_messages` row's keys onto the queued quote/contact (previously dropped), so a returned quote ties to the **exact** outbound, not just the domain. An unmatched reply (domain never emailed) is queued `kind="unmatched_reply"`, `needs_human_review`, **un-attributed** (was logged-and-dropped). |
| **What landed (3b)** | The run-GET path (`_orm_to_detail`) builds a quote index from **confirmed** quotes for the run and overlays each candidate: thread-precise join (3a key) with a domain fallback for legacy/NULL-thread quotes. A matched candidate gets `evidenceState="quoted"`, `quoteConfirmed`, `supplierConfirmed`, the quote's price/lead/terms (override the listing), and `quoteUnverified` on the **0–1** `Quote.confidence` scale (`_QUOTE_CONFIDENCE_FLOOR=0.4`, NOT the 0–100 `_PRICE_CONFIDENCE_FLOOR`) — so a shaky extraction stays flagged. Fail-soft: a registry error degrades to no overlay, never breaks the read. |
| **Back-compat (create_all-only, §3.2)** | No Alembic. Fresh DBs get the columns from the DDL; existing DBs get them via the idempotent `ALTER TABLE ADD COLUMN` in `_migrate`. Pre-3a quote rows have NULL link keys → resolved by the domain fallback. |
| **Risk / impact** | Low today: `evidenceState="quoted"` only arises after a real RFQ→reply→confirm cycle, which needs `EMAIL_SEND_ENABLED` (False) + live Gmail — so no live candidate is "quoted" in the current demo. The candidate dict now carries the State-C fields; the React `options-screen` does **not** yet branch on them (a "quoted" row would render via the priced path, showing the quote price without the supplier-confirmed framing/badge). |
| **Recommended action** | Build the frontend half: extend the `Candidate` type (`evidenceState` gains `"quoted"`; add `quoteConfirmed`/`quoteUnverified`/`terms`/`quoteCurrency`), and render State C in `options-screen` — the supplier-confirmed claim + quote price/lead/terms, with `quoteUnverified` composing alongside the existing marketplace/`priceUnverified` treatments. Also add a human-review surface for the `unmatched_reply` queue in the admin UI. |

---

### 5.5 Search-provider interface + Parallel.ai adapter — built; live sourcing path NOT yet routed

| Field | Detail |
|---|---|
| **File** | `utils/search_providers.py` (`SearchProvider` / `TavilyProvider` / `ParallelProvider` / `get_search_provider`); `scripts/parallel_ab_probe.py` (opt-in live A/B) |
| **Kind** | §9 search-provider foundation landed; the live sourcing-path rewire is a deliberate, **separately-reviewed** follow-up (touches §6 archived code). |
| **What landed** | The swappable interface + a Tavily wrapper (behaviour-identical, late-bound to `_arch._tavily`) + the Parallel.ai `/v1/search` adapter (x-api-key, objective + search_queries, multi-excerpt results preserved, fail-soft/no-op without a key) + `get_search_provider(SEARCH_PROVIDER, default tavily)`. The opt-in probe runs both live for comparison. Unit suite fully mocked; `PARALLEL_API_KEY` added to the conftest key-neutralizer. |
| **NOT done (the deferred bit)** | The live sourcing path still calls Tavily **directly** at **five sites in the §6 archived zone** — `tavily_client._search_vendor_prices` (×2), `enterprise_search.py:424` & `:701`, and `sourcing_agent._capability_search:1000` — each with per-site params (`search_depth="advanced"` on four, omitted on one; `max_results` 5/8/10/15; `include_domains` on one). Nothing routes through `get_search_provider()` yet, so **`SEARCH_PROVIDER=parallel` does NOT change real sourcing** — only the probe uses Parallel. |
| **Risk / impact** | None today (additive; default Tavily; sourcing untouched, byte-identical). The gap: provider selection is real at the factory but not consumed by the pipeline. |
| **Recommended action** | Route the five call sites through `get_search_provider().search(...)`, passing each site's existing params verbatim (so Tavily stays byte-identical) — a careful pass per §6 (audit `SourcingAgent` call sites; keep `test_tavily_client` / `test_sourcing_agent` green). Then `SEARCH_PROVIDER=parallel` switches real sourcing. Consider the §9 "query-both, route-by-quality" dual-run only after the single-swap lands. |
| **Primitives ready (built, not yet wired)** | Two A/B-probe-driven helpers exist for the eventual dual-run/dedupe: `utils/url_normalize.normalize_url` (full-URL canonicalization — lowercases scheme/host, strips tracking params incl. `srsltid`/`utm_*`, trims trailing slash; complements the host-only `supplier_registry._normalize_domain`), and the ParallelProvider anti-bot/empty **excerpt filter**. When the dual-run dedupe lands, key candidates on `normalize_url`; `known_parts` edge-keying could also adopt it (it currently keys edges on `_normalize_domain` host only). |

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

## 7. Post-Launch Debt (redesigns + observability)

Recorded after the intake/scoring redesigns landed behind flags (`INTAKE_TYPE_AWARE`, `SCORING_V2`, both default-OFF — see CLAUDE.md §4). Non-blocking; captured so they aren't lost.

### 7.1 Cache-hit path skips suitability re-validation — a stale/wrong cached result can surface

| Field | Detail |
|---|---|
| **File** | `utils/procurement_agent/agents/sourcing_agent.py` (`_result_from_cached_edges`) — the `known_parts` edge cache-hit path |
| **Kind** | Correctness gap — the cache-hit path returns cached candidates without re-running the suitability check the live path applies |
| **Why it exists** | The cache short-circuits the pipeline for speed/cost; suitability re-validation was not threaded onto the cache-hit branch (only the live-search branch validates). |
| **Risk / impact** | A candidate cached from an earlier (looser or wrong) run can surface again without being re-scored against the current request — a wrong result the live path would now reject can still appear via cache. |
| **Status** | ✅ RESOLVED (Phase 1, the cache-revalidation fix). Three unconditional (not flag-gated) fixes landed on `feature/run-capture-overnight`: **T1** `price_db._make_key` returns `""` for null-mfg/PN specs so the shared `unknown\|UNKNOWN-PN` bucket is never read or written (commit `a922458`) — phase3 backport candidate. **T2** `_cache_type_gate` (`scoring.py`) drops a confirmed-different noun-class candidate at BOTH cache seams — `enterprise_search._call_enterprise_api` (price_db serve) and `api_server._result_from_cached_edges` (known_parts serve, closes the `:987-990` wrong-PART-TYPE gap) — commit `b9f18f8`. **T3** the price_db serve path sets `match_type="Functional Alternative"` instead of the `SourcingOption` model default `"Exact OEM"` (no PN was re-verified on a cache hit) — commit `27c8913`; phase3 backport candidate. **PURGE** the poisoned `unknown\|UNKNOWN-PN` bucket was deleted from `utils/price_db.json` (backup in `audit/t4-cache-backup/`); T1 prevents re-creation. **T4** regression: a VAGUE-tier harness re-run (12 `vague-*` parts, `INTAKE_TYPE_AWARE=1 SCORING_V2=1 RUN_CAPTURE=1`, caches cleared first) confirmed **zero** motor/pump-URL contamination on valve/solenoid/starter/hose/level/transmitter/gasket/belting/chain/gearbox-oil (the baseline 50-run had 4 motor URLs + 1 pump URL at suit=50.0 on each). See `audit/t4-cache-backup/T4_REPORT.md` for the before/after table. |
| **Recommended action** | Resolved. Backport T1 + T3 (unconditional correctness) to phase3 once validated; T2 is the same kind but review the gate verdict table first. |

### 7.2 `SCORING_V2` sub-type gap — gate-valve-on-ball-valve (and similar) not distinguished

| Field | Detail |
|---|---|
| **File** | `utils/sourcing_archieved/scoring.py` + `part_type_classes.py` (`SCORING_V2` TypeGate) |
| **Kind** | Known coverage gap in the redesigned scorer — noun-class TypeGate separates broad part classes but not fine sub-types within a class |
| **Why it exists** | The TypeGate matches on part-type class from snippet/URL text; distinguishing sub-types (e.g. a gate valve vs a ball valve) needs a page-fetch + LLM read the current text-only gate doesn't do. Deferred to Stage 3. |
| **Risk / impact** | A wrong sub-type within the right class can pass the gate (e.g. a ball valve surfacing for a gate-valve request). Better than the pre-`SCORING_V2` behavior, but not exact at sub-type granularity. |
| **Recommended action** | Stage 3: add a page-fetch + LLM sub-type disambiguation step for gate-passed candidates where sub-type matters; cover with a labeled fixture. |

### 7.3 LangSmith tracing built but not landing — 403 on writes at the current plan

| Field | Detail |
|---|---|
| **File** | `utils/procurement_agent/langsmith_client.py` + the intake-agent `ls.trace` wiring (commit `c39fa6c`) |
| **Kind** | Observability instrumentation built and fail-soft, but no traces reach LangSmith |
| **Why it exists** | Instrumentation follows messaging's `ls.trace` pattern (sibling project). Two blockers: (1) run-posting needs the SDK **enable flag** set (`LANGSMITH_TRACING` / `LANGCHAIN_TRACING_V2 = true`) — `LANGSMITH_API_KEY` authenticates but does NOT enable; without it `ls.trace` builds the run in memory and never posts (silent). (2) `create_dataset` returns **403** — the key lacks dataset-write scope at the current plan level. |
| **Risk / impact** | None to the pipeline (fail-soft, no-op without traces). But the observability the instrumentation was built for is unavailable until resolved. |
| **Recommended action** | Set the enable flag to turn on run-posting; resolve the 403 (plan upgrade / support / scoped key) for dataset writes. Instrumentation itself needs no code change. |

### 7.4 `SCORING_V2` gate + weights are informed defaults — need real-data calibration
| **Kind** | Uncalibrated magic numbers — same character as §4.2 (cache suitability defaults) |
| **Why it exists** | The redesigned gate/weights were set from reasoning + the labeled eval set, not from a calibration pass against real live-sourcing outcome data. |
| **Risk / impact** | Ranking/gating may be off at the margins until tuned against real results; defaults are informed but not validated at scale. |
| **Recommended action** | Once live sourcing data accumulates, calibrate the gate threshold + weights against real outcomes (precision/recall on labeled results); lock in with a regression fixture. |

### 7.5a <=5-scored candidates bypass the suitability floor via cache/Apollo paths

| Field | Detail |
|---|---|
| **File** | `utils/procurement_agent/agents/sourcing_agent.py` (`_apply_suitability_floor`) + `api_server._result_from_cached_edges` |
| **Kind** | Coverage gap (separate from the PN-aware floor calibration) |
| **Why it exists** | `_apply_suitability_floor` skips any option already carrying a `rejection_reason`, and cached/Apollo-confirmed candidates bypass the scorer (so they carry no score floor check). 6 captured candidates scored <=5.5 yet passed the 30 floor via these paths. |
| **Risk / impact** | A near-zero-suitability result can surface because it never re-enters the floor. Not caused by floor height — lowering/raising the floor does not touch it. |
| **Status** | Open. Out of scope for the PN-aware floor fix (`fix(scoring): PN-aware suitability floor`) — do not bundle. Likely a `_cache_type_gate` coverage gap; investigate separately. |

### 7.5 Component-of context is seal-only � non-seal components get no parent-context query

| Field | Detail |
|---|---|
| **File** | `utils/procurement_agent/part_type_classifier.py` + `part_type_registry.py` (ANCHORED regime is `mechanical_seal` only) |
| **Kind** | Classifier/registry scope decision � not a bug. The ANCHORED regime (which sets `_component_of`) covers just `mechanical_seal`, so non-seal components (impeller, wear ring, shaft sleeve, diaphragm kit, drive chain, etc.) never get `_component_of` set and never reach the component-aware "[component] for [parent]" sourcing query. They stay clean only because Fix A (commit `1363b6f`) makes `_build_search_query` lead with `detected_type` unconditionally � so the query is component-led ("impeller ...") but carries no parent-machine context ("... for Goulds 3196"). |
| **Why it exists** | The overnight intake redesign scoped the registry to 5 types with `mechanical_seal` as the sole ANCHORED type (the priority pair). Non-seal component-of detection was intentionally out of scope. |
| **Risk / impact** | A non-seal component's sourcing query lacks the parent-machine anchor that disambiguates the right variant (e.g. an impeller for a Goulds 3196 vs a Goulds 3175). Sourcing still works via the component term, but is less precise than a seal query. |
| **Recommended action** | Do NOT fix as a bug. Extending ANCHORED-regime component detection to other component types is a classifier/registry scope decision � evaluate against real trial demand for non-seal components before investing. If pursued, add the component types to the registry with `REGIME_ANCHORED` and update the classifier's `component_of` rules + the eval dataset's `test_component_of_only_for_anchored` invariant. |

### 7.6 Test conftest does not isolate `brand_intelligence._DB_PATH` � live runs pollute test outcomes

| Field | Detail |
|---|---|
| **File** | `utils/procurement_agent/tests/conftest.py` (isolates persistence + supplier_registry, but NOT `brand_intelligence`) |
| **Kind** | Test isolation gap � on-disk store state leaks into tests |
| **Why it exists** | `conftest.py` was built before `brand_intelligence.sqlite` carried test-relevant state. `_seeded_tier3_candidates` reads `get_brand_relationships`, which reads the real DB; a row written by a live harness/dev run makes seeding fire and prepends non-pivot candidates, flipping `test_capability_pivot_tags_results` red regardless of test order. |
| **Risk / impact** | A test that asserts on seeding/sourcing behavior is non-deterministic in any working copy that has run live sourcing (harness, dev UI). Masked as "green" only on a pristine DB. |
| **Recommended action** | Structural: add `brand_intelligence._DB_PATH` (and audit other non-isolated on-disk stores tests may read � `run_capture.sqlite`, `price_db.json`) to the conftest isolation set alongside `supplier_registry`/`known_parts`, so the suite is hermetic regardless of dev working-copy state. The per-test monkeypatch in `test_capability_pivot_tags_results` is a band-aid for one test; the conftest fix covers the class. |

---

*Items are ordered by section, not by priority. All items are prototype-era technical debt �
none block the current demo or the post-seed milestone.*
