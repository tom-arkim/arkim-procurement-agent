# Decision memo — the Orchestrator's fate post-Streamlit

**Status:** decision input for Tom. No code changes proposed here — this is analysis.
**Date:** 2026-06-13 · branch `feature/phase3-comparison-approval` @ `b636a13` (Streamlit retired).
**Companion:** CLEANUP.md §4.5 · `docs/PHASE_R_RESOLUTION.md` (the "Orchestrator fate + §4.5" open item).

## TL;DR / recommendation

**Do not retire `core.py` (option 3).** It holds the one correct implementation of approval-path
routing — the exact logic the FastAPI `approve` endpoint is missing — so deleting it throws away
the foundation the auth/H1 work will need to rebuild. **Recommend option 1 (keep) now, explicitly
reframed as "the approval-routing asset staged for the auth work," and fold option 2 (wire FastAPI
onto it) into the *opening move* of the auth/H1/M1/D2 cluster when that is scheduled** — not as a
separate project. Separately, reconcile `_stub_sourcing → Phase.ERROR` (a tiny, tests-only cleanup)
so the masking code doesn't linger regardless of when the bigger decision lands.

---

## 1. Investigation — the real state (confirmed against code, not assumed)

**The divergence is real.** The Orchestrator and the FastAPI path do the *same* select→approve→execute
arc differently, and the difference is concentrated in approval routing:

| Step | Orchestrator (`core.py`) | FastAPI (`api_server.py`) — the shipping path |
|---|---|---|
| **select-candidate** | `select_candidate` (core.py:135-193) **computes `_approval_path`** via `determine_approval_path(facility_id, total_usd)` and stores `{approvers_required, approver_roles, grand_total_usd}` on the candidate. | `select_candidate` (api_server.py:1051-1068) stores **only the thin `{candidate_id, tier, selected_at}`** — `determine_approval_path` is **never called**; no `_approval_path` is persisted. |
| **approve** | `submit_approval` (core.py:195-267) routes on the stored path: `approvers_required >= 2` → `PENDING_SECOND_APPROVAL`, else `APPROVED`. | `approve_run` (api_server.py:1072-1103) sets `current_phase = APPROVED` **unconditionally** on one approval, for any amount; the rule is never consulted. |
| **execute** | stub chain (`_stub_approved` → EXECUTING …). | `_execute` (procurement_agent.py) has the **H1 phase guard** — places only from `approved`/`executing` (landed `20df65c`). |
| **sourcing failure** | `_stub_sourcing` (core.py:286-315) catches the `SourcingAgent` exception and `_advance(… COMPARISON …)` — **masks failure as "no candidates."** | `_run_sourcing_background` (api_server.py:426-443) sets `Phase.ERROR` — **honest**. |

**Two honest caveats about how far the Orchestrator actually gets:**
- It computes the **routing** (how many approvers a dollar amount requires) but **does not enforce
  distinctness** — `submit_approval` never checks the second approver differs from the first, and
  `approver_role` is a display label ("no RBAC enforcement in prototype", core.py:207). So the
  Orchestrator carries the **M1 gap too**. It has the *routing half*, not the *enforcement half*.
- `_stub_sourcing`'s masking (→ COMPARISON not `Phase.ERROR`) **still lives in the code**, but is now
  **tests-only**: post-Streamlit, the only importers of `core.py` are `test_orchestrator.py` and
  `test_orchestrator_approvals.py` (confirmed — nothing on the shipping path imports it;
  `api_server.py` calls `SourcingAgent` / `SpecComparisonAgent` / `ProcurementAgent` directly).

**Dependents (post-removal, confirmed):** `core.py` + `start_new_run` are imported **only** by the two
`test_orchestrator*.py` files. It is tested-but-not-on-the-shipping-path.

---

## 2. The three options

### Option 1 — KEEP `core.py` as the reference coordinator, as-is
- **Cost:** carrying weight every audit must account for ("tested-but-unshipped"); the `_stub_sourcing`
  masking persists in the codebase (tests-only, zero product reach).
- **Benefit:** zero work; preserves the only correct `_approval_path` computation and documents the
  intended orchestration pattern (brief §4).
- **Honest size of the tax:** modest — ~488 lines + 2 green, fail-soft test files. The real cost is
  the recurring "why is this here / is it dead?" question, which a one-line note in §7/§8 defuses.

### Option 2 — WIRE FastAPI onto the Orchestrator (make it the real coordinator)
- Replace `api_server`'s bespoke `select-candidate`/`approve` (and possibly `_run_sourcing_background`)
  with calls into the Orchestrator, so the durable surface gets the `_approval_path` routing it lacks.
- **Cost:** real integration work; the Orchestrator's persistence/transition model must be reconciled
  with api_server's ORM-direct writes; `_run_sourcing_background` already does the *better* thing on
  failure (Phase.ERROR), so adopting the Orchestrator's sourcing path means **fixing `_stub_sourcing
  → Phase.ERROR` as part of it** (otherwise you'd regress to masking).
- **Benefit:** the durable surface gains dollar-threshold approval routing (the H1 routing half)
  **without** needing auth — using the same display-role labels the Orchestrator uses today. Removes
  the divergence by making one path the source of truth.
- **Critical observation:** the Orchestrator **already computes `_approval_path`**, which is *exactly*
  the logic the FastAPI `approve` endpoint is missing for H1's dual-approver routing. So this option
  is **the routing half of H1**, deliverable independently of the identity primitive.

### Option 3 — RETIRE `core.py` + `test_orchestrator*.py`
- **Cost:** lose the reference coordinator **and the only correct `_approval_path` computation** — the
  auth/H1 work would then have to **rebuild** that routing from scratch.
- **Benefit:** smallest codebase; the `_stub_sourcing` masking code is gone entirely; no more
  "tested-but-unshipped" line in audits.

---

## 3. The key question, answered explicitly

> **Is "wire FastAPI onto the Orchestrator" (option 2) the same work as building the auth layer to
> close H1/M1/D2 — or two separate efforts?**

**They overlap but are not the same, and the split is clean:**

- **The Orchestrator IS the asset the auth work builds on — for the routing half.** It already
  computes `_approval_path` (approvers-required + roles from the rules engine). H1's dual-approver
  *routing* is precisely this, and it is **auth-independent**: you can route a $25k order to a second
  approval using dollar thresholds and display-role labels with no identity provider at all. Option 2
  delivers that half.

- **The auth layer is the *enforcement* half, which the Orchestrator does NOT provide:**
  - **H1 (the "two *different* people" guarantee)** and **M1 (distinct approver)** need *authenticated
    identities* to compare — the Orchestrator counts phases, not distinct approvers, so it cannot
    enforce this. Auth supplies it.
  - **Role validation** (is this caller actually a `plant_manager`?) needs authenticated claims —
    today both paths trust the request body's label. Auth supplies it.
  - **D2 (tenant-scoping the customer endpoints)** is **entirely orthogonal** to the Orchestrator —
    it's about scoping every endpoint's reads/writes by authenticated tenant. The Orchestrator does
    nothing for it either way.

**So:** option 2 is the **front half of the H1 work and a stepping-stone**, not a separate project and
not the whole auth cluster. The right sequencing is *routing first (option 2, reusing
`determine_approval_path`), enforcement second (auth claims layered on top)* — done as **one cluster**,
with option 2 as its opening move. Crucially, this means **option 3 (retire) is the wrong call**: it
would delete the routing foundation the auth work then has to recreate.

---

## 4. Recommendation & sequencing

1. **Now — keep (option 1), but stop it being silent debt.** Reframe `core.py` in the docs as *"the
   approval-routing reference / staging ground for the auth-layer work,"* not "Streamlit leftover."
   (One line in CLAUDE.md §7/§8 — already partly done.)
2. **Now (cheap, independent) — reconcile `_stub_sourcing → Phase.ERROR`.** It's a tests-only change
   that erases the §4.5 masking from the codebase regardless of the bigger decision, and pre-aligns
   the Orchestrator's sourcing path with the shipping path so option 2 doesn't have to. Update the two
   `test_orchestrator*` expectations if any assert COMPARISON-on-failure. Low risk; do it whenever
   convenient, or bundle into step 3.
3. **When auth is scheduled (Arc 1) — adopt option 2 as the first step of the auth/H1/M1/D2 cluster.**
   Wire `select-candidate` to persist `_approval_path` (reuse `determine_approval_path`, exactly as the
   Orchestrator does) and `approve` to route on it; *then* layer authenticated-claims enforcement
   (distinct approvers, role validation, tenant scoping) on top. Reuse the Orchestrator's logic; don't
   rebuild it.

**Do not pick option 3.** The only thing it buys over option 1 is deleting ~488 lines and a masking
function that no user can reach — at the cost of throwing away the one correct approval-routing
implementation the auth work depends on. The carrying cost of keeping is a documentation line; the
cost of retiring is rework.
