# Phase R — Resolution Tracking

**Status ledger for the findings in PHASE_R_AUDIT_REPORT.md + PHASE_R_AUDIT_ADDENDUM.md.**
The reports are the dated point-in-time findings; this ledger tracks their disposition.
Do not delete the reports — update this ledger as items are addressed. CLEANUP.md remains the
canonical debt inventory; this ledger cross-references it.

Disposition key: ✅ RESOLVED (commit) · 🔒 DEFERRED-PENDING-AUTH · 📋 BACKLOG (architecture) · ⏸️ WONTFIX (with reason)

Branch: `feature/phase3-comparison-approval`. Suite green throughout (686 baseline → 691 after Phase R).

| ID | Finding | Severity | Disposition | Commit / Note |
|----|---------|----------|-------------|---------------|
| H1 | `/execute` places order without approval gate | HIGH | ✅ / 🔒 | `_execute` phase guard: `20df65c` (places only from approved/executing). Dual-approver routing on the API `approve` endpoint → 🔒 DEFERRED-PENDING-AUTH (CLEANUP §4.1). |
| M1 | Dual approval satisfiable by one identity twice | MED | 🔒 DEFERRED-PENDING-AUTH | Distinct-approver check lands with the auth layer; CLEANUP §4.1 (auth-dependent cluster). |
| D1 (addendum) | `impact.py` cumulative `last_paid` not date-aware | MED | ✅ | Date-aware `before` cutoff: `dbe4865`. Changed figure: part bought Jan @100 then Mar @60, scoring the Feb order (chosen 80) now yields **+20** (vs preceding Jan 100) instead of **−20** (vs later Mar 60). Diverges only on cross-run repurchase replayed out of date order; one-off parts and live-`now` scoring unchanged. |
| D2 (addendum) | Customer endpoints ungated/un-tenant-scoped | MED | 🔒 DEFERRED-PENDING-AUTH | Tenant scoping lands with the auth layer; CLEANUP §4.1 (auth-dependent cluster). |
| L1 | `orders.py` SQLite connections not closed | LOW | ✅ | `732b725` — `contextlib.closing` on all four `_get_conn()` sites. |
| L2 | `price_db._make_key` no None-guard on part_number | LOW | ✅ | `732b725` — `(part_number or '')`. |
| L3 | `outreach.py` hardcoded email_send_enabled literal | LOW | ✅ | `732b725` — reads `email_sender.EMAIL_SEND_ENABLED`; archived copy still dead-but-imported. |
| C1 (addendum) | MROSupply despaced name-match | LOW | ✅ | `3662e2a` (loosens rescue gate — near-miss edge test added). |
| D1-docs | Stale "Gmail STUBBED / not wired" docstrings | DOC | ✅ | `732b725` — GmailSender + rfq_send docstrings corrected to the wired (default-off, double-gated) reality. |
| — | `<img>` → `next/image` (message-bubble) | DOC | ✅ | `b9a6b39` — `unoptimized` + CSS sizing; lint warning cleared. |
| §5a | Dedup cross-name aliases (OTC/Great Lakes) | — | 📋 BACKLOG | Needs entity-resolution/alias layer; approach in report. |
| §5b | Tiering by lane not signal | — | 📋 BACKLOG | Needs commerce-signal promotion; approach in report. |
| N1 (audit 2) | `supplier_registry.py` + `brand_intelligence.py` connections not closed | LOW | ✅ | `4aa1df3` — `contextlib.closing` on all `_get_conn()` sites (16 + 3). Completion of L1. |
| N2 (audit 2) | `brand_intelligence.py` dead `"competitors"` probe | B | ✅ | `4aa1df3` — `common_competitors` only. |
| N3 (audit 2) | Per-tier sourcing failure not surfaced (status captured but presents as "0 results") | LOW | 🔒 DEFERRED | Low urgency; finer-grained sibling of CLEANUP §4.5. Surface `tier.status` + WARN log when the durable surface is hardened. |
| Streamlit retirement | Retire the Streamlit front end (React/FastAPI is the shipping surface) | — | ✅ | `chore: retire Streamlit surface` — deleted `app.py`, `pages/`, `.streamlit/`; dropped the `streamlit` dep (`uv.lock` regenerated); docs updated (README, CLAUDE §2/§3/§5/§6/§7/§8, CLEANUP §4.5). `pandas` kept (used by `scripts/manage_suppliers.py`). `core.py` **kept** (tests-only after removal). Suite green (691). |
| Orchestrator fate + §4.5 code | `core.py` is tested-but-not-on-the-shipping-path; `_stub_sourcing` failure-masking still lives in code | — | 🔒 DECISION-PENDING | **Do not forget:** Streamlit removal made §4.5 moot *for the product* but the masking code (`_stub_sourcing` → `COMPARISON` not `Phase.ERROR`) remains in `core.py` (tests-only). Decide the Orchestrator's fate (keep as reference per brief §4 / wire FastAPI onto it / retire it + `test_orchestrator*.py`) and, with it, reconcile `_stub_sourcing → Phase.ERROR` **or** delete. Tracked here + CLEANUP §4.5 so "moot for the product" doesn't silently become "forgotten in the code." |

## Auth-layer dependency note
H1 (dual-approver routing), M1 (distinct approver), and D2 (tenant scoping) share one root
cause: the auth/identity primitive doesn't exist yet. They MUST land together when it's built —
do not implement partial enforcement without auth (a count/role from the request body is not
enforcement). Tracked as a single auth-dependent item in CLEANUP.md §4.1. The `_execute` phase
guard (H1, `20df65c`) is the one part that did NOT need auth and is done.

## Backlog approaches (§5 — propose-only, not implemented)
- **§5a — dedup cross-name aliases (OTC / Great Lakes).** A lexical normalize key can't catch
  aliases that share no tokens. Needs an entity-resolution / alias layer: a canonical-identity
  map (domain + known-alias table, optionally embedding similarity) consulted during
  `_dedup_across_tiers`, so "OTC" and "Great Lakes" resolve to one supplier. Link-don't-merge:
  record the alias relationship, don't collapse the records. Cross-repo (onboarding + core), Arc 2.
- **§5b — tiering by lane not signal.** Tiers are assigned by search lane (`_run_tier2`
  domain-restricted vs `_run_tier3` general), so a priced marketplace discovered in Tier 3 stays
  Tier 3. Needs commerce-signal promotion: detect a priced/in-stock buy signal on a candidate and
  promote it across the tier boundary independent of which lane found it. Score-then-tier, not
  lane-then-tier.

## CLEANUP.md sync (done in Phase R)
- §3.3 (PN-collision price cache) → ✅ RESOLVED (composite key + L2 None-guard).
- §2.2 (triple EMAIL_SEND_ENABLED) → L3 removed the outreach literal; archived copy still dead-but-imported.
- §4.1 → added the H1/M1/D2 auth-dependent cluster.
