# PHASE_R_AUDIT_REPORT.md

Unattended code audit — Arkim Procurement Agent Prototype
Generated 2026-06-13 · branch `feature/phase3-comparison-approval` · HEAD `a059177`

**Mode: PROPOSE, DO NOT APPLY.** No shipping code was changed. Nothing was committed
or pushed. No live or paid external API call was made (Apollo / Gmail / Anthropic /
Tavily). `.env` was never read, printed, or moved. The only file this audit writes is
this report.

---

## 0. Authoritative-brief caveat (read first)

The instruction was to follow `arkim_cowork_overnight_audit_brief.md` **exactly** as the
authoritative document. **That file does not exist anywhere accessible** — not in the
connected repo, its parent folder, or the session uploads. The repo contains only
`docs/arkim_procurement_agent_brief.md` (the product brief) and
`docs/arkim_procurement_code_standard.md` (the code standard); neither is the overnight
audit brief.

Rather than halt outright, I proceeded on the **essential rules the request restated
inline**, all of which are safety-preserving and were honored in full:

- propose-don't-apply; findings + proposed diffs in this single report file;
- change no shipping code; commit nothing; push nothing;
- no live/paid external API calls (mocked pytest is fine; invoking a real client is not);
- never read/print/move `.env` or any credential;
- work the module inventory in dependency order, deepest scrutiny on consequential-action
  (★) modules; plus a security/secrets pass and a known-inputs re-verification;
- halt if the suite is red at start or a hard constraint can't be honored.

Because the brief's *specific* module inventory, its ★ markings, and its "known inputs"
list were unavailable, I **reconstructed** them from the codebase and from `CLEANUP.md`
(the repo's own technical-debt inventory, which is plainly the source of the "known
inputs" to re-verify). Where the missing brief might have defined scope differently, that
is called out. **No safety constraint was relaxed to compensate for the missing brief.**

---

## 1. Safety baseline

| Check | Result |
|---|---|
| Starting branch / HEAD | `feature/phase3-comparison-approval` @ `a059177` |
| Working tree at start | **Already dirty** — 30 modified tracked files + 2 untracked scripts, **none created by this audit** (see §1.1) |
| `.git/index.lock` present | Yes — unremovable in this environment; irrelevant since no git write was attempted |
| Test suite at start | **GREEN — 686 passed, 1 benign warning** (see §1.2) |
| `.env` handling | Never read/printed/moved; excluded from the test copy |
| External calls during audit | None (suite is structurally network-isolated; see §1.2) |

### 1.1 Pre-existing dirty tree — constraint reconciliation

The requested end state is "working tree clean except the one new report file." That is
**not literally achievable and must not be forced**: the tree was already dirty before the
audit began (modified `main.py`, `utils/models.py`, the orchestrator, several agents,
state, tests, the whole `utils/sourcing_archieved/` tree, frontend files, plus two
untracked `scripts/*_self_test.py`). Reverting or stashing those would *modify shipping
code* and destroy uncommitted work — a direct violation of "change no shipping code."

**Action taken:** left every pre-existing change untouched; added only this report.
**End state is therefore:** the pre-existing dirty set (unchanged) **plus** one new
untracked file, `PHASE_R_AUDIT_REPORT.md`. This is the correct reading of the constraint
given the starting condition, and is flagged here rather than worked around.

### 1.2 Running the suite safely

`uv run` could not be used (it tried to download a CPython 3.14 toolchain; sandbox network
blocked it), and the repo's `.venv` is a Windows venv. The suite was run on a copy of the
Python sources placed on the sandbox's native filesystem (the Windows mount fails SQLite
WAL with a `disk I/O error` — an environment limitation, **not** a code defect), under
Python 3.10 with dependencies installed from PyPI. The repo targets 3.11+, so this is a
strong-but-not-canonical green; a final confirmation run on 3.11+ in the canonical env is
advisable. The copy **excluded `.env`**, so no credentials were present.

Network isolation is structurally guaranteed by `tests/conftest.py`: an `autouse` fixture
blanks `APOLLO_API_KEY` / `ANTHROPIC_API_KEY` / `TAVILY_API_KEY` and all `GMAIL_*` creds
for every test, and forces `email_sender.EMAIL_SEND_ENABLED = False`. No test can make a
live/paid call or send mail by accident. This is good defensive design and is itself a
positive finding.

---

## 2. Module inventory (reconstructed, dependency order) and ★ marks

Foundation → leaves first. ★ = consequential-action module (real-world side effect:
sends email, spends paid API credits, places/mutates orders, or gates a consequential
action). Deepest scrutiny went to the ★ rows.

| Order | Module | ★ | Verdict |
|---|---|---|---|
| 1 | `utils/models.py`, `state/phases.py`, `price_db.py`, `audit_log.py`, `llm_tracker.py`, `contact_resolution.py`, `site_settings.py`, `sourcing_filter.py`, `quote_extractor.py`, `comparison_helpers.py` | | Sound foundation. `price_db` has one latent footgun (L2). |
| 2 | `state/persistence.py`, `state/approval_rules.py` | ★ | `approval_rules` logic sound; RBAC deferred (known, §4.1). |
| 3 | `utils/gmail_client.py` | ★ | **Sound.** Fail-soft, creds from env, lazy imports. |
| 4 | `utils/apollo_client.py` | ★ | **Sound.** Fail-soft, gated on key presence; no call when unconfigured. |
| 5 | `utils/email_sender.py` | ★★ | **Sound.** Real double gate. One stale docstring (D1). |
| 6 | `utils/rfq_send.py` | ★★ | **Sound.** HITL approval gate enforced before any send. Stale docstring (D1). |
| 7 | `utils/procurement_agent/outreach.py` | ★ | Drafts only; never sends. Hardcoded flag literal (L3). |
| 8 | `utils/orders.py` | ★★ | **Sound** state machine; no auto-placement. Conn-leak (L1). |
| 9 | `utils/reorder.py`, `utils/impact.py` | | Pure/forecast; sound. |
| 10 | `agents/procurement_agent.py` | ★★ | **No phase guard on `execute`** (H1). |
| 11 | `orchestrator/core.py` | ★★ | Sound coordination; sourcing-failure masking on Streamlit path (§4.5, known). Dual-approval distinct-approver gap (M1). |
| 12 | `agents/sourcing_agent.py` | ★ | Apollo name-match rescue gate present & verified (§4.6). Not deep-audited beyond the consequential clarifier path. |
| 13 | `api_server.py` (FastAPI surface) | ★★ | **Approval/execute gating gaps** (H1); admin gate sound. |
| — | `pages/sourcing_runs.py` (Streamlit) | ★ | Throwaway harness; auto-walks approve/execute stubs (acknowledged). |

---

## 3. Findings (prioritized by impact)

### HIGH

#### H1 — The durable FastAPI surface does not gate order placement on approval

This is the headline consequential-action finding. On the React/FastAPI path (the
*durable* surface per CLAUDE.md), the "commit to buy" action is reachable without a valid
approval:

1. `POST /api/runs/{id}/select-candidate` (api_server.py:1052) stores only
   `{candidate_id, tier, selected_at}` and advances to `PENDING_FIRST_APPROVAL`. It does
   **not** compute the approval path (`determine_approval_path` is never called here), so
   no `_approval_path` / required-approver count is ever persisted on this surface.
2. `POST /api/runs/{id}/approve` (api_server.py:1072) **always** sets phase to `APPROVED`
   after a **single** approval, for **any** dollar amount, with any `approver_name` /
   `approver_role` in the body. The dual-approver requirement encoded in
   `DEFAULT_RULES` ($5k → 2 approvers, $25k → 2 approvers) is never enforced here. The
   docstring concedes this: *"Phase 3 will implement the dual-approver routing."*
3. `POST /api/runs/{id}/execute` (api_server.py:1563 → `ProcurementAgent._execute`,
   procurement_agent.py:66) performs **no phase check at all**. It captures and `place_order`s
   a durable order whenever a `selected_candidate_json` resolves to a vendor + price.

Combined effect: `select-candidate` → `execute` (skipping `approve` entirely) **places a
durable order with no approval**, and even with `approve`, a $25k purchase needs only one
approver. The product's core control (human approval before commitment) is not enforced on
the durable surface.

**Severity rationale / mitigation.** Rated HIGH as a *design/control* gap on a
consequential action. Real-world blast radius in the current prototype is **bounded**:
`orders.py` is explicitly capture-and-track only — no PO transmission, no payment, no
email, no ERP. And this is the in-flight `feature/phase3-comparison-approval` branch, so
the routing is acknowledged WIP. It must not ship to a state where `execute` performs an
external action without the gate closed.

**Proposed fix (propose-only, not applied).** Add a phase guard to `_execute` so an order
can only be placed from an `APPROVED` run, and (separately) implement dual-approver
routing on the API `approve` endpoint. Minimal guard:

```diff
--- a/utils/procurement_agent/agents/procurement_agent.py
+++ b/utils/procurement_agent/agents/procurement_agent.py
@@ def _execute(self, run: SourcingRun) -> dict:
         from utils import orders
 
+        # Gate: an order may only be PLACED from an approved run. Without this,
+        # /execute can be called straight after /select-candidate and bypass approval.
+        phase = getattr(run, "current_phase", None)
+        if phase not in ("approved", "executing"):
+            return {"success": False, "action": "execute", "order": None, "placed": False,
+                    "message": f"Run is not approved (phase={phase}); cannot place order.",
+                    "next_phase": None}
+
         selection = self._selection_for_order(run)
         if not selection or not selection.get("vendor_name"):
             return {"success": False, "action": "execute", "order": None, "placed": False,
                     "message": "No selected candidate to order.", "next_phase": None}
```

And on the API approve path, route to a second approval when the rule requires it (sketch —
requires `select-candidate` to first persist the computed `_approval_path`, mirroring
`Orchestrator.select_candidate`):

```diff
--- a/api_server.py
+++ b/api_server.py
@@ def approve_run(run_id: str, body: ApproveRequest):
-        run.approval_history_json = json.dumps(history)
-        run.current_phase = Phase.APPROVED.value
+        run.approval_history_json = json.dumps(history)
+        selected = json.loads(run.selected_candidate_json) if run.selected_candidate_json else {}
+        required = int((selected.get("_approval_path") or {}).get("approvers_required", 1))
+        approvals = sum(1 for h in history if h.get("action") == "approved")
+        # require approvals from distinct approvers (see M1)
+        distinct = len({(h.get("approver_name") or h.get("approver_role"))
+                        for h in history if h.get("action") == "approved"})
+        if distinct >= required:
+            run.current_phase = Phase.APPROVED.value
+        else:
+            run.current_phase = Phase.PENDING_SECOND_APPROVAL.value
```

Treat the diffs as direction, not drop-in: `select-candidate` must persist
`_approval_path`, and tests around the single-approver happy path will need updating. Land
behind the Phase 3 work.

### MEDIUM

#### M1 — Dual approval can be satisfied by one identity approving twice

`Orchestrator.submit_approval` (core.py:195) advances `PENDING_FIRST_APPROVAL →
PENDING_SECOND_APPROVAL → APPROVED` based only on a **count** of approval rows. Nothing
checks that the two approvals come from **distinct** approvers (no `approver_id` / role
distinctness check). With or without RBAC, "2 approvers required" is enforceable as one
person clicking approve twice. This compounds H1. The proposed H1 API diff above includes a
`distinct` check; the same discipline should apply in `submit_approval`. Tie to the RBAC
work (§4.1) but note it is a *separate* gap from identity verification.

### LOW

#### L1 — `utils/orders.py` never closes SQLite connections
Every `_get_conn()` opens a connection that is never `.close()`d (no `try/finally`, no
context manager). Harmless for short scripts/tests; in the long-lived FastAPI process this
leaks file handles over time. Proposed: wrap reads/writes in `with closing(conn):` or
return connections to a small pool.

#### L2 — `price_db._make_key` does not None-guard `part_number`
```python
return f"{(manufacturer or '').lower().strip()}|{part_number.upper().strip()}"
```
`manufacturer` is None-guarded; `part_number` is not, so `part_number=None` raises
`AttributeError`. The live confirm path (`reply_processor.confirm_quote`) guards
`mfg and pn` first and `orders._resolve_price` only calls when both are truthy, so this is
**latent**, not live — but `scripts/process_replies_self_test.py` calls it unguarded.
One-line defensive fix:
```diff
--- a/utils/price_db.py
+++ b/utils/price_db.py
@@ def _make_key(manufacturer: str, part_number: str) -> str:
-    return f"{(manufacturer or '').lower().strip()}|{part_number.upper().strip()}"
+    return f"{(manufacturer or '').lower().strip()}|{(part_number or '').upper().strip()}"
```

#### L3 — `outreach.initiate_outreach_campaign` returns a hardcoded `email_send_enabled: False`
This is the third place the send flag is expressed (CLEANUP §2.2). It is a literal, not a
read of the canonical `email_sender.EMAIL_SEND_ENABLED`. If the canonical flag is ever
flipped on, this function still reports `False` to the UI — a reporting divergence (it does
not itself send; `rfq_send` does). Proposed: `from utils.email_sender import
EMAIL_SEND_ENABLED` and return it, removing the literal.

### DOC / HOUSEKEEPING

- **D1 — Stale "Gmail is STUBBED / not wired" docstrings.** `email_sender.GmailSender`
  class docstring (line ~142) and `rfq_send` module docstring (lines ~11–12) still say the
  Gmail call is stubbed/not wired. The code **does** call the real Gmail service when the
  gate is on and creds exist (email_sender.py:213–225); the module-level docstring and
  CLEANUP §2.2 correctly say it's wired. Update the two stale docstrings to match. (No
  behavior change — the double gate is intact and default-off.)
- **CLEANUP.md §3.3 is now stale (resolved in code).** See §4 below.

---

## 4. Known-inputs re-verification (vs `CLEANUP.md`)

| Item | Claim in CLEANUP | Re-verified state in current code |
|---|---|---|
| §2.2 Triple `EMAIL_SEND_ENABLED` | Flag defined in 3 places; consolidate | **Still 3 expressions.** Canonical (`email_sender`) correctly reads env and is default-off ✔. Archived copy is dead-but-imported. `outreach.py` still returns a literal (L3). Partially addressed. |
| §3.1 `audit_log` raw sqlite3 | ORM migration deferred | **Still raw sqlite3**, but SQL is parameterized (no injection). Accept as prototype debt. |
| §3.2 `persistence` `create_all()` only | No migrations; schema diff drops data | **Unchanged.** Prototype disclaimer still accurate. |
| **§3.3 PN-only price cache collision** | Listed as a **high-risk open** item; recommends composite key | **RESOLVED IN CODE.** `_make_key` now composites `manufacturer\|PART_NUMBER` and the docstring documents the legacy-miss behavior. **CLEANUP.md is stale on this item** — recommend marking §3.3 done (residual: the L2 None-guard). |
| §4.1 RBAC deferred | Any caller can supply any approver role | **Confirmed.** Roles are display labels. Admin endpoints **do** enforce a constant-time bearer check (`secrets.compare_digest`, 503 fail-closed when `ARKIM_ADMIN_TOKEN` unset) — verified at api_server.py:1408+. **New, beyond CLEANUP:** approver *count* and *distinctness* are also unenforced on the FastAPI surface (H1/M1). |
| §4.5 Streamlit vs API sourcing-failure handling | API → `Phase.ERROR`; Streamlit/core → COMPARISON (masks failure) | **Confirmed still divergent.** api_server `_run_sourcing_background` sets `Phase.ERROR` (line 441); `core._stub_sourcing` advances to COMPARISON with an error result (lines 296–315). Low urgency if Streamlit is retiring. |
| §4.6 Apollo wrong-org clarifier | Rescue gated on a name-consistency check | **Confirmed present & wired.** `_names_plausibly_match` and `rescue_withheld_name_mismatch` exist (sourcing_agent.py:228+); reject path stays flag-only; nothing excludes on the verdict (annotate-don't-remove). |

---

## 5. Security / secrets pass

| Check | Result |
|---|---|
| `.env` tracked by git | **No** (not in `git ls-files`) |
| `.gitignore` coverage | `.env`, `.env.*` (keeps `.env.example`), `.venv/`, `frontend/.env.local` — correct |
| Hardcoded secrets in `*.py` | **None found** (keys read from `os.environ`; admin token from env) |
| Dangerous sinks (`eval`/`exec`/`os.system`/`subprocess`/`shell=True`/`pickle.load`/`yaml.load`) | **None** in app code (excl. tests/.venv/archived/frontend) |
| SQL injection | Parameterized throughout (`orders`, `audit_log`, ORM). Dynamic SQL in `orders.get_orders` builds clauses from a **fixed** column tuple with bound params — safe. |
| Admin auth | Constant-time compare, fail-closed when token unset, read-only admin surface — sound |
| Credentials in test process | Neutralized by `conftest` autouse fixture (see §1.2) |

No security defects found. The secrets posture is good for a prototype.

---

## 6. Modules judged sound (no action needed)

`gmail_client.py`, `apollo_client.py`, `email_sender.py` (logic; see D1 for docs),
`rfq_send.py` (logic; see D1), `orders.py` state machine (see L1 for the conn leak),
`reorder.py`, `approval_rules.py` (logic; RBAC is a known external gap), `impact.py`,
`price_db.py` composite-key fix, the `conftest` safety net, and the admin auth gate. These
are well-structured, fail-soft, and (where consequential) appropriately gated by design —
the gaps are specifically at the FastAPI approval/execute *wiring*, not in the primitives.

---

## 7. Scope notes / what was not exhaustively reviewed

- `agents/sourcing_agent.py` (1190 lines), `supplier_registry.py` (881),
  `brand_intelligence.py`, `intake_agent.py`, `spec_comparison_agent.py`, and the frontend
  TypeScript were reviewed for their consequential touchpoints (sends, paid calls, price/
  order writes) but not line-by-line for correctness/quality — they are not consequential-
  action modules and the reconstructed inventory prioritized impact.
- `utils/sourcing_archieved/` (dead-but-imported, CLEANUP §1.1) was treated as known debt,
  not re-audited.
- The suite was confirmed green on Python 3.10; a canonical 3.11+ run is recommended as the
  final gate.

---

## 8. End-state attestation

- Shipping code changed: **none.**
- Files added by this audit: **one** — `PHASE_R_AUDIT_REPORT.md`.
- Pre-existing dirty tree: **left untouched** (see §1.1).
- Commits / pushes: **none.**
- Live/paid external calls (Apollo/Gmail/Anthropic/Tavily): **none.**
- `.env` / credentials: **never read, printed, or moved.**
- Test suite: **green at start (686 passed); unchanged by this audit.**

*Reminder: all diffs above are PROPOSED. None have been applied.*
