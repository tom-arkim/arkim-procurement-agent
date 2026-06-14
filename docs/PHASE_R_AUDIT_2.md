# PHASE_R_AUDIT_2.md

Phase R — second pass: **verification of the applied fixes + new-ground audit.**
Generated 2026-06-13 · branch `feature/phase3-comparison-approval` · HEAD `3938d6b`
(first apply batch `20df65c..cad7207`, docs relocation `3938d6b`).

**Mode: PROPOSE, DO NOT APPLY.** No shipping code changed. Nothing committed/pushed for the
audit. No live/paid external call (Apollo/Gmail/Anthropic/Tavily) — the suite mocks them.
`.env` never read/printed/moved. The only write is this report.

**Baseline (verified):** `uv run pytest` on the canonical `.venv` (Python 3.11) → **691 passed,
1 benign warning. GREEN.** Prerequisite 2 satisfied; audit proceeds on a green baseline.

---

## Part A — Verification of the applied fixes (regression check)

| ID | Verified? | Evidence |
|---|---|---|
| **H1** — `_execute` phase guard | ✅ **Verified, no regression** | `procurement_agent._execute` returns `placed:False`/no order unless `current_phase ∈ {approved, executing}` (checked **before** selection resolution). The bypass `select-candidate → execute` is closed: select-candidate lands `pending_first_approval`, which the guard rejects. `test_execute_blocked_when_not_approved` asserts nothing is captured; the approved-path placement tests run with `current_phase="approved"` and still place. API-level placement tests already route through `_make_approved_run`. |
| **D1** — date-aware `last_paid` | ✅ **Verified, no regression** | `_last_paid_price(..., before=created_at)` skips orders with `created_at >= before` (strictly-preceding; equal timestamps are excluded, so an order is never its own comparator), over newest-first rows → the most-recent preceding purchase. `gather_cumulative` threads each order's `created_at`. `before=None` preserves most-recent-prior for live-`now` scoring. No off-by-one; sign is `last_paid - chosen` (correct). `TestLastPaidDateAware` proves +20-vs-prior not −20-vs-later, and the `before=None` default. |
| **C1** — despaced name match | ✅ **Verified, no regression** | `_names_plausibly_match` adds `"".join(sorted(a)) == "".join(sorted(b))` on the suffix-stripped token sets. Matches "MROSupply"/"MRO Supply"; near-miss "MRP Supply" still rejected (char composition differs); gross-mismatch and empty/None cases unchanged (existing tests green). Reject path untouched; exclusion still removes nothing. **Residual (theoretical, not a regression):** the check keys on the multiset of *tokens*, so two genuinely-different orgs with a token-boundary collision (e.g. `{ab,c}` vs `{a,bc}`) would both fold to the same string — no realistic pair observed; the near-miss test guards the realistic case. Acceptable for a rescue-gate loosening (a false match only *withholds-less*, never excludes). |
| **L1** — orders.py conn close | ✅ **Verified** | `contextlib.closing` wraps all four `_get_conn()` sites; queries/returns unchanged. **See N1 — the sibling raw-sqlite3 stores still leak.** |
| **L2** — price_db None-guard | ✅ **Verified** | `_make_key` now `(part_number or '')` — `None` PN no longer raises. |
| **L3** — outreach canonical flag | ✅ **Verified** | `initiate_outreach_campaign` returns `email_sender.EMAIL_SEND_ENABLED` (read at call time, reflects monkeypatch/runtime), not a literal. Drafts only; never sends. |
| **D1-docs** — stale stub docstrings | ✅ **Verified** | `GmailSender` + `rfq_send` docstrings now describe the wired (default-off, double-gated) reality; the no-creds path is "error", not "stub". Consistent with the code. |

**Part A conclusion: all seven fixes are correct and introduced no regression** (suite 686→691,
the +5 being the new H1/D1/C1 tests).

---

## Part B — New ground (modules the first two passes did not line-by-line audit)

### `agents/sourcing_agent.py` (1190 lines) — **sound; one low note + the known backlog**
- **Error handling is uniformly fail-soft.** Each tier runner (`_run_tier1/2/3`) wraps its
  external work in `try/except → []`; `_collect` catches `TimeoutError`/`Exception` and returns
  `{results:[], count:0, status:"error: …"}`. The Apollo clarifier runs *outside* the per-tier
  timeout future (latency can't discard Tier 3) and no-ops without a key. Quality filters
  (`_dedup_across_tiers`, `_apply_suitability_floor`, `_reconcile_suitability`,
  `_rank_and_select_tier3`) are **annotate-don't-remove** — first-set-wins on `rejection_reason`,
  nothing dropped. No wrong-attribution or ungated-write risk found.
- **N3 (LOW)** — *per-tier failure is captured but not surfaced as failure.* A tier exception is
  caught in `_collect` and returned as `status:"error: …"` with empty results; the run still
  completes to COMPARISON. This is the finer-grained sibling of CLEANUP §4.5 (Streamlit
  failure-masking): a Tavily/LLM error in one tier presents downstream as "0 results found,"
  indistinguishable from a genuine empty search. Low urgency. *Proposed:* surface
  `tier.status` in the run payload / UI (the data is already there) and log at WARN; no
  behaviour change to the pipeline.
- **Known backlog, still reproduces (no new diff):** §5a dedup cross-name aliases
  (`_normalize_vendor_name` is lexical — "OTC" ≠ "Great Lakes"); §5b tiering-by-lane
  (`_run_tier2` domain-restricted vs `_run_tier3` general; no commerce-signal promotion).
  Approaches are in `docs/PHASE_R_RESOLUTION.md`. Note `data/` seed and `brand_intelligence`
  carry alias tables (`MANUFACTURER_ALIASES`, `*_AUTHORIZED_DISTRIBUTORS`) that the eventual
  entity-resolution layer can build on.

### `supplier_registry.py` (881 lines) — **sound writes; the L1 leak recurs (N1)**
- **Write integrity is good.** All dynamic `UPDATE … SET {set_clause}` builders whitelist
  columns against fixed sets (`allowed` in `update_supplier`, `_APOLLO_COLUMNS`,
  `_CONTACT_WRITABLE`) with named-param values → **no SQL-injection vector** (column names are
  never caller-controlled). Upserts create a `discovery_only` row before writing so cache
  write-backs always land. Bounce handling nulls only the matched contact. JSON/bool coercion
  is explicit.
- **Consequential write (`price_db` via quote-confirm) is correctly gated.** The only
  `price_db` write on the customer path is an explicit `/review-items/{id}/confirm`
  (→ `reply_processor.confirm_quote`); there is no auto-confirm. Verified end-to-end with the
  frontend (below).
- **N1 (LOW, security/resource)** — *every function opens `_get_conn()` and never closes it*
  (no `closing()`/`try-finally`), across ~16 call sites. Identical class to the audit's **L1**
  (fixed in `orders.py`); here it's the **higher-traffic** store — a single sourcing run calls
  `lookup_*`/`enrich_option`/`create_stub` many times. Harmless for scripts/tests; in the
  long-lived FastAPI process it leaks file handles. *Proposed diff below.*

### `brand_intelligence.py` (637 lines) — **sound; same N1 leak + one trivial note**
- LLM discovery is fail-soft (returns `None` on no-key/error → callers get an empty structure,
  never a raise); cost tracking silent-fails by design; TTL + `_touch` on read are intentional.
  Seeded data takes priority over LLM. No fabrication.
- **N1 (LOW)** — same unclosed-`_get_conn()` pattern in `get_brand_relationships`,
  `all_cached_entries`, `invalidate`.
- **N2 (trivial / B)** — `get_brand_relationships` line ~283 reads
  `payload.get("competitors") or payload.get("common_competitors")`, but the prompt only
  defines `common_competitors`; the `"competitors"` probe is dead (always falsy). Harmless
  (falls through). *Proposed:* drop the `"competitors"` probe. Behaviour-preserving.

### `intake_agent.py` / `spec_comparison_agent.py` — **sound, abstention-correct**
- Both are key-gated LLM agents with fail-soft `except` paths. **No fabrication:**
  `intake._fallback_extract` returns prior specs with `confidence:0` + "extraction skipped";
  `_parse_llm_json` returns `confidence:0` on parse failure.
  `spec_comparison._extract_specs_from_snippet` returns `{field:None}` on failure; `_low_fidelity`
  emits `candidate_value:None`, `match:"unknown"`, `compatibility_summary:"verification_required"`
  — it explicitly does **not** invent a match (matches the module's "No fabricated matches").
  No consequential side effects (no send/price/order write). **No action.**

### Frontend `proc-*` customer components — **sound; real endpoints, impact never recomputed**
- **Impact comes only from the endpoint.** `home-impact.tsx`, `impact-screen.tsx`,
  `history-screen.tsx` all consume `useImpact()` (`GET /api/impact`) and render
  `total_savings` / `time_estimate_minutes` / `savings_by_month` / `contributing_order_ids`
  verbatim. The only client-side math is **presentational** (bar pixel heights); no saving is
  recomputed in the UI.
- **The buyer loop drives real mutations — no local-state shortcut.** `quotes-section.tsx` uses
  `useProcessReplies` / `useConfirmReviewItem` (the server-side `price_db` write) /
  `useRejectReviewItem` / `usePlaceOrderFromQuote` via `.mutate(id)`; `order-section.tsx` uses
  `useExecuteOrder` / `useMarkDelivered`. Prices shown are server values
  (`order.unit_price`, `run.selected_candidate.price`) — none fabricated. No path confirms a
  quote or places an order without calling the gated endpoint. **No action.**

---

## Part C — Deferred-cluster confirmation (not accidentally half-built)

**Confirmed still correctly DEFERRED — no body-trusting pseudo-enforcement crept in:**
- **H1 dual-approver routing** — `approve_run` (`api_server.py:1099`) records the approval
  (incl. `approver_name`/`approver_role` for the audit trail) and **unconditionally** sets
  `Phase.APPROVED`. No count check, no role/threshold gate, no distinctness. This is *absence of
  enforcement* (honest deferral), not a fake gate that trusts the request body. Docstring still
  says "Phase 3 will implement the dual-approver routing."
- **M1 distinct-approver** — `Orchestrator.submit_approval` advancement remains count-based;
  the H1 commit did not touch `core.py`. No distinctness check was half-added.
- **D2 tenant-scoping** — the customer endpoints (`/api/orders`, `/api/impact`, `/api/reorder`,
  `/api/sites/{id}/ship-to`, `/api/review-items/*`, run endpoints) remain global/ungated. No
  partial tenant filter was introduced that would create a false sense of isolation.

All three remain a single auth-dependent cluster (CLEANUP §4.1 / `PHASE_R_RESOLUTION.md`). The
one piece that did **not** need auth — the `_execute` phase guard — is done (H1, `20df65c`).

---

## Proposed diffs (NOT applied)

### N1 — close SQLite connections in `supplier_registry.py` and `brand_intelligence.py` *(behaviour-preserving; same fix as L1)*
Wrap every `_get_conn()` use in `contextlib.closing`, exactly as L1 did for `orders.py`.
Representative shape (apply uniformly):
```diff
+from contextlib import closing
 ...
-        conn = _get_conn()
-        conn.row_factory = sqlite3.Row
-        row = conn.execute("SELECT * FROM suppliers WHERE domain = ?", (norm,)).fetchone()
-        return dict(row) if row else None
+        with closing(_get_conn()) as conn:
+            conn.row_factory = sqlite3.Row
+            row = conn.execute("SELECT * FROM suppliers WHERE domain = ?", (norm,)).fetchone()
+            return dict(row) if row else None
```
- `supplier_registry.py` call sites: `load_registry`, `lookup_by_domain`, `lookup_supplier`,
  `create_stub`, `update_supplier`, `upsert_apollo_data`, `upsert_contact`,
  `upsert_primary_contact`, `mark_contact_bounced`, `record_sent_message`, `get_sent_messages`,
  `record_review_item`, `get_review_items`, `get_review_item`, `set_review_item_status`,
  `all_entries`.
- `brand_intelligence.py` call sites: `get_brand_relationships`, `all_cached_entries`,
  `invalidate`.
- Suite must stay green; no return shapes change. Recommend landing as one
  "N1: close store connections" commit (the natural completion of L1).

### N2 — drop the dead `"competitors"` probe in `brand_intelligence.py` *(behaviour-preserving)*
```diff
-                "common_competitors": payload.get("competitors") or payload.get("common_competitors") or [],
+                "common_competitors": payload.get("common_competitors") or [],
```

### N3 — surface per-tier sourcing status *(behaviour change — flag)*
No drop-in diff: include each tier's `status` (already computed in `_collect`) in the run/UI
payload and log a WARN when `status` starts with `"error:"`, so a tier failure is
distinguishable from a genuine zero-result search. Ties to CLEANUP §4.5; low urgency while
Streamlit is retiring. Behaviour change (new surfaced field/logging) → needs sign-off.

---

## End-state attestation
- Shipping code changed: **none.** Files added by this audit: **one** (`docs/PHASE_R_AUDIT_2.md`).
- Prior reports/brief/ledger (now under `docs/`): **untouched** by the audit.
- Commits/pushes for the audit: **none.** (The two prerequisites were separate, explicit
  changes: docs relocation `3938d6b`; no source code touched.)
- Live/paid external calls: **none.** `.env`/credentials: **never read.**
- Suite: **691 passed (canonical .venv, Python 3.11), green at start; unchanged by this audit.**
- All diffs above are **PROPOSED, not applied.**

### Headline
The first apply batch is **verified correct and regression-free**. New ground is **largely
sound** — fail-soft, abstention-correct, whitelisted writes, real-endpoint frontend. The one
substantive new finding is **N1**: the connection-close fix (L1) was applied to `orders.py` but
its two sibling raw-sqlite3 stores (`supplier_registry.py`, `brand_intelligence.py`) carry the
same leak — a clean, low-risk follow-up batch. The deferred auth cluster is correctly deferred,
not half-built.
