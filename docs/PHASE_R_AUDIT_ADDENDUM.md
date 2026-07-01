# Phase R — Audit Addendum (independent second pass)

**Companion to `PHASE_R_AUDIT_REPORT.md`** (the prior unattended run). This addendum does
not restate that report — it **validates its headline finding, resolves its environment
caveats, and adds the items it explicitly skipped.** Read the original first.

**Mode:** read-only. No shipping code changed; nothing committed or pushed; no live/paid
external calls (Apollo/Gmail/Anthropic/Tavily); `.env` and the Gmail key never read.
**Baseline:** `feature/phase3-comparison-approval` @ `a059177`.

---

## A. Canonical-env attestation (resolves the prior report's §1.1/§1.2 caveats)

The original ran on a *copied source tree under Python 3.10* and flagged: "a final
confirmation run on 3.11+ in the canonical env is advisable," and described the tree as
"already dirty — 30 modified tracked files."

In **this** environment, on the canonical `.venv` (Python 3.11):

- `uv run pytest` → **686 passed, 1 benign warning. Green.** This is the canonical 3.11
  confirmation the prior report asked for — the strong-but-not-canonical 3.10 result is now
  confirmed on the real interpreter.
- Working tree at start: **clean** except two pre-existing untracked probe scripts
  (`scripts/outreach_self_test.py`, `scripts/process_replies_self_test.py`) — **not** the
  "30 modified files" the prior run saw (that dirty state has since been committed/cleaned).
  So the original's §1.1 dirty-tree reconciliation no longer applies; the end state here is
  simply the clean tree **plus** the original report **plus** this addendum.

No exploratory local change was made; the suite is untouched.

---

## B. Validation of the headline finding (H1) — CONFIRMED, with independent citations

The original's **H1** (durable FastAPI surface places orders without enforcing approval) is
**real**. Verified directly:

- **`api_server.py:1099`** — `approve_run` appends one approval row then unconditionally
  `run.current_phase = Phase.APPROVED.value`. No `_approval_path` lookup, no approver
  count/threshold check, no distinctness, any `approver_name`/`approver_role` accepted from
  the body. Docstring concedes *"Phase 3 will implement the dual-approver routing."*
- **`procurement_agent.py:66`** (`_execute`, behind `POST /api/runs/{id}/execute`) — has
  **no phase/approval guard**. It resolves the selection and `place_order`s. So
  `select-candidate → execute` (skipping `approve`) places a durable order with **no
  approval**; and even via `approve`, a $25k order needs only one approver.
- **M1 (single identity approving twice)** also confirmed — advancement is count-based with
  no distinctness check.

**Agreement:** the original's HIGH rating and its proposed `_execute` phase-guard + the
distinct-approver routing diff are the right direction. **Blast radius is bounded** —
`orders.py` is capture-and-track only (no payment/PO/email/ERP), and this is the in-flight
phase-3 branch — but the gate must be closed before `execute` performs any external action.
This is the single most important pre-ship item. *(My own first read under-weighted this;
the original caught it — noted for the record.)*

I also corroborate the original's other findings I re-touched: **L2** (`price_db._make_key`
no None-guard on `part_number` — latent, real), **admin gate** sound (constant-time
`secrets.compare_digest`, 503 fail-closed), **no eval/exec/os.system/subprocess**, **SQL
parameterized** (the f-string sites use whitelisted columns/DDL constants, not input),
**upload `file.filename` only echoed** (no path traversal).

---

## C. §5 known-inputs the original explicitly skipped — re-verified

The original noted `sourcing_agent.py` was "not deep-audited beyond the consequential
clarifier path." Those quality items were re-verified here by exercising the pure functions
offline (no network):

| Item | Status | Evidence |
|---|---|---|
| **Dedup cross-name aliases** (OTC vs Great Lakes) | **Still reproduces** | `_normalize_vendor_name("OTC")` = `otc` ≠ `greatlakes`. Lexical key can't catch aliases. Surface variants (All Seals Inc/Inc./Incorporated) **do** merge → that part fixed. |
| **MROSupply despaced mismatch** | **Still reproduces** | `_names_plausibly_match("MROSupply","MRO Supply")` → `False`; token sets `{mrosupply}` vs `{mro,supply}` don't overlap. **Clean 2-line fix — Diff C1 below.** |
| **Tiering by lane, not signal** | **Still reproduces** | `_run_tier2` is domain-restricted, `_run_tier3` general; no commerce-signal promotion of a priced marketplace from T3→T2. |

---

## D. Additions the original did not flag

### D1 — `impact.py` cumulative `last_paid` is not date-aware (MED, correctness)
The original rated `impact.py` "Pure/forecast; sound." It is sound in structure, but
`_last_paid_price` / `gather_cumulative` use the customer's *most recent* purchase of the
part across runs, **not** the purchase chronologically *preceding* the order being scored.
If orders aren't processed in date order, a decision can be compared against a *later*
purchase → wrong saving sign/magnitude on the CFO-facing headline. (Already noted in code
comments as a simplification.) **Behaviour-change to fix** — Diff D2 below.

### D2 — Customer endpoints ungated + un-tenant-scoped (MED, security)
The original's security pass covered secrets/SQL/admin-gate but didn't call this out. The
**new customer endpoints** — `/api/orders`, `/api/impact`, `/api/reorder`,
`/api/sites/{id}/ship-to`, `/api/review-items/*` (confirm/reject/place-order), and the
run endpoints — are **ungated and global** (no tenant/site/user scoping). Correct for the
single-tenant prototype (consistent with CLEANUP §4.1 / the ungated run-endpoint
convention), but a **cross-tenant data leak / cross-tenant action** the moment multi-tenant
lands. **Behaviour-change — needs the auth layer**; bind these to authenticated
tenant/buyer claims and scope every store read/write by tenant. Same root cause as H1's
"any approver" gap — all roads lead to the auth layer.

---

## Proposed diffs (NOT applied)

### Diff C1 — `sourcing_agent.py` `_names_plausibly_match`: despaced equality **[behaviour-change — loosens the Apollo-rescue name gate]**
```diff
     a = _normalize_org_name(vendor_name)
     b = _normalize_org_name(apollo_org_name)
     if not a or not b:
         return False
+    if "".join(sorted(a)) == "".join(sorted(b)):   # despaced match: MROSupply == MRO Supply
+        return True
     if a <= b or b <= a:
         return True
     overlap = a & b
     return bool(overlap) and len(overlap) / min(len(a), len(b)) >= 0.5
```
Verified: `sorted({'mrosupply'})`→`"mrosupply"` == `sorted({'mro','supply'})`→`"mrosupply"`.
This gate controls whether an Apollo **rescue** proceeds — loosening it allows more rescues;
intended here, but confirm it doesn't wave through a genuinely different org whose tokens
happen to concatenate alike (rare).

### Diff D2 — `impact.py`: date-aware `last_paid` **[behaviour-change — alters savings numbers]**
```python
def _last_paid_price(manufacturer, part_number, exclude_run_id, before=None):
    ...
    for o in rows:  # newest-first
        if o.get("run_id") == exclude_run_id:
            continue
        if before and (o.get("created_at") or "") >= before:
            continue  # only purchases strictly before the scored order
        if (o.get("manufacturer") == manufacturer and o.get("part_number") == part_number
                and o.get("status") in _PURCHASED_STATUSES and o.get("unit_price") is not None):
            return float(o["unit_price"])
    return None
# gather_cumulative: pass each scored order's created_at as `before`.
```
Needs sign-off (changes the headline) and a refresh of any test pinning cumulative numbers.

### Diff (approach) — tenant scoping & approval gate **[behaviour-change — needs auth]**
No drop-in diff: introduce the auth layer (CLEANUP §4.1), then (a) gate `approve`/`execute`
on the computed approval path with distinct approvers (the original's H1/M1 diffs), and
(b) scope every customer endpoint's store reads/writes by the authenticated tenant. Land
together — both are the same missing primitive.

### Tiering (§5b) and dedup aliases (§5a)
Architecture-level (commerce-signal tier promotion; entity-resolution/alias layer) — see the
original report and the earlier session notes. No clean one-liner; propose approach only.

### Housekeeping
- `<img>` → `next/image` in `frontend/.../message-bubble.tsx:48` (the lone `next build`
  warning) — **[behaviour-preserving]**.
- The original's **D1** (stale "Gmail STUBBED" docstrings) and its CLEANUP §3.3-now-resolved
  note both still stand.

---

## End-state attestation
- Shipping code changed: **none.** Files added by this addendum: **one** (`PHASE_R_AUDIT_ADDENDUM.md`).
- The original `PHASE_R_AUDIT_REPORT.md`: **left untouched.**
- Commits/pushes: **none.** Live/paid external calls: **none.** `.env`/credentials: **never read.**
- Suite: **686 passed (canonical .venv, Python 3.11), green at start and unchanged.**
- All diffs above are **PROPOSED, not applied.**
