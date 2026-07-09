# NIGHT 6 — Investigation Gate Findings (I1–I4)

Status: **PROCEED to build.** All four seams re-verified against the CURRENT
merged base `test/flag-on-integration` @ `d2b7de7` (Night 5 merged). The prior
halted run's notes were written against the pre-merge tree (`4cc06be`) and are
superseded here. One dev-DB-hygiene caveat from I4 is documented prominently
below — it is a morning-verification discrepancy, NOT a build blocker.

Base branch: `test/flag-on-integration` · BASE_HEAD: `d2b7de733be2b26bd18052343809f5a73c2b8b42`
Work branch (recreated from BASE_HEAD, zero commits): `feature/supplier-portal-overnight`
Suite baseline: **1795 passed / 73 skipped** (verified pre-flight, `.venv`).

---

## I1 — Night 4 review/pending machinery (the propose-revision seam)

There IS a reusable pending-revision seam. The generic pending store is the
`review_items` table (`utils/supplier_registry.py:135-153`; columns: id, kind,
status, run_id, supplier_domain, vendor_name, manufacturer, part_number,
payload_json, confidence, raw_source, created_at, resolved_at, thread_id,
sent_message_id, message_id). Accessors: `record_review_item`
(`utils/supplier_registry.py:1029`), `get_review_item` (`:1117`),
`get_review_items` (`:1092`), `set_review_item_status` (`:1128`).

Onboarding drafts are `review_items` rows with `kind="supplier_scope"`. The
concierge status vocabulary is in `utils/procurement_agent/onboarding/concierge.py:57-61`
(`DRAFT_KIND="supplier_scope"`, pending=`needs_human_review`, confirmed=`confirmed`,
rejected=`rejected`). The onboarding gate is TIER1_V2 itself, re-exported as
`ONBOARDING_ENABLED` (`utils/procurement_agent/onboarding/flags.py:38`,
`is_enabled()` reads `os.environ["TIER1_V2"]` live).

Admin endpoints (all `Depends(require_admin)` AND flag-gated via
`_require_onboarding_enabled()` → 503 when TIER1_V2 off,
`api_server.py:4423-4431`):
- `POST /api/admin/onboarding/harvest` (`api_server.py:4439`)
- `GET  /api/admin/onboarding/drafts` (`api_server.py:4473`)
- `GET  /api/admin/onboarding/drafts/{draft_id}` (`api_server.py:4482`)
- `POST /api/admin/onboarding/drafts/{draft_id}/approve` (`api_server.py:4505`)
- `POST /api/admin/onboarding/drafts/{draft_id}/reject` (`api_server.py:4527`)

Apply path: `concierge.approve_draft` (`utils/procurement_agent/onboarding/concierge.py:173-219`)
→ `_apply_scope_to_registry` (`:266-361`), which calls the four full-replace
scope setters: `set_supplier_classes` (`utils/supplier_registry.py:1221`),
`set_supplier_brands` (`:1287`), `set_supplier_territory` (`:1360`),
`set_supplier_verticals` (`:1441`), then drives the lifecycle forward
(discovered→…→onboarded, `:340-356`). The lifecycle drive is onboarding-specific;
the four setters are safe to re-run on an existing onboarded row (full-replace +
INSERT OR IGNORE; double-approve idempotent, asserted).

Payload shape: `OnboardingDraft.to_dict()`
(`utils/procurement_agent/onboarding/extractor.py:128-142`) — a full-scope
snapshot (brands/classes/locations/ship_area_guess/vertical), NOT a per-field
old/new diff. No `proposed_by`/proposer field exists on `review_items`;
`scope_set_by` records the *approver*, not the proposer.

Generic admin review queue (all kinds): `GET /api/admin/review-queue`
(`api_server.py:3045`) — excludes `kind="unmatched_reply"`.

**Reuse for a portal propose-revision:** the `review_items` table + status
vocabulary + the four scope setters are reusable. **Net-new required:**
- a new `kind` = `supplier_revision` (distinct from `supplier_scope` so the
  existing onboarding queue is not polluted),
- a new apply path that calls the four setters **WITHOUT the lifecycle drive**
  (the supplier is already onboarded — re-driving is idempotent but unnecessary
  and semantically wrong for a profile edit),
- proposer provenance (carried in `payload_json` — no schema migration needed;
  the `review_items` payload is free-form JSON),
- public-supplier token auth (see I2 — net-new).

The existing `OnboardingApproveRequest` model + `approve` endpoint already
demonstrate the `revisions` merge pattern (`concierge._merge_revisions`,
`concierge.py:222-240`) — the portal revision apply reuses the same per-field
merge + brand/class normalization (`_normalize_brand`/`_normalize_class`,
`concierge.py:243-263`).

## I2 — Existing auth/token primitives

**No reusable tokenized-link primitive.** The only thing that resembles one is
`utils/sourcing_archieved/vendor_tokens.py` — an in-memory UUID4 dict that
builds a partner URL but does no persistence, no expiry, no hashing at rest,
and no validation endpoint. It is a URL-construction stub, not a token system.
**Build net-new** as a standalone module `utils/claim_tokens.py` (house
standard: clean, typed, tested, fail-soft).

Evidence the required primitives are net-new:
- `secrets.token_urlsafe` — zero usages in the repo.
- `hashlib.sha256` for token-hashing-at-rest — no auth usage (only
  `utils/eval_export.py` for a filename). Net-new.
- `secrets.compare_digest` — two sites only: `api_server.py:2976` (admin bearer
  gate) and `utils/auth/dependencies.py` (s2s signature). Reusable as a pattern.
- `hmac` / `bcrypt` / `passlib` / `pbkdf2` / `scrypt` — zero usages.

Admin auth model: a single static bearer token, `ARKIM_ADMIN_TOKEN` env var,
enforced by `require_admin` (`api_server.py:2962-2978`) — no login/session/JWT.
Fail-closed: unset secret → 503 (admin disabled). A full Cognito RS256 JWT stack
exists at `utils/auth/` but is explicitly unenforced (docstrings: "no endpoint
enforces it yet"); it targets customer users, not unauthenticated suppliers,
and is not the right fit for a magic-link claim portal.

**Token store location:** a new standalone `claim_tokens` table. Decision: keep
the token store in its own sqlite file `data/claim_tokens.sqlite`, managed by
`utils/claim_tokens.py` with its own connection (mirrors supplier_registry's
`_DATA_DIR`/`_DB_PATH` pattern, isolatable in tests via monkeypatch). This keeps
the new module standalone (house standard) and avoids shared-connection concerns
with supplier_registry's non-WAL sqlite. The token row carries `supplier_domain`
(a string reference into supplier_registry — no FK, mirrors the loose coupling
the rest of the registry uses). Token validation: lookup-by-hash → resolve
`supplier_domain` → read the profile from supplier_registry.

Admin supplier view: `GET /api/admin/suppliers` (`api_server.py:3026-3034`,
`supplier_registry.all_entries()`). Frontend `frontend/src/app/admin/page.tsx`
(monolithic `AdminInspectorPage`, "Suppliers" tab at line 36). No per-row action
button on the Suppliers tab today — the "Generate claim link" button is a new
per-row action cell. `fetchAdmin`/`postAdmin` helpers
(`frontend/src/app/admin/page.tsx:49,63`) already handle authed POST to
`/api/admin/*`. **The admin "Generate claim link" endpoint is admin-gated +
flag-gated (SUPPLIER_PORTAL_V1), NOT on the DEMO_MODE allowlist** (mirrors the
onboarding SSRF-caution pattern at `api_server.py:4410-4414`).

## I3 — Public vs admin route separation

Admin gating is a FastAPI dependency, not a path-prefix middleware: every
`/api/admin/*` route declares `Depends(require_admin)`; there is no blanket
admin guard on the `/api/admin/` prefix (it is per-route). A `DEMO_MODE`
allowlist middleware exists (`api_server.py:144-229`, `_DEMO_ALLOWLIST` at
`:166`) but is unrelated to admin auth — it is inert unless `DEMO_MODE` is
truthy, and it DENIES-by-default (a route not on the list 403s). The new public
portal route is added to no allowlist; under DEMO_MODE it 403s fail-closed (the
claim portal must not be reachable from the public no-login demo).

App structure: flat — all routes registered inline on `app` (`api_server.py:105`);
no `APIRouter`/`include_router` anywhere. The new public supplier route attaches
inline as `@app.get`/`@app.post` under a fresh prefix `/api/portal/...` that
does NOT declare `require_admin`, with its own token-validation dependency. The
admin-session/auth boundary is exactly `require_admin` (`api_server.py:2962`):
anything not declaring it is public. The brief's "no admin surface reachable
from the supplier route" is satisfied by (a) keeping portal endpoints in their
own `/api/portal/` prefix with only the token dependency, and (b) proving via a
property/inertness test that no `/api/admin/*` path is reachable through portal
tokens (a portal token never satisfies `require_admin`).

Flag gating: a new `SUPPLIER_PORTAL_V1` flag (strict `_env_truthy` parse,
mirrors TIER1_V2/SCORING_V2). Flag-off → the portal routes are NOT registered
(the route does not exist; response byte-identical to any unknown route —
FastAPI 404 `{"detail":"Not Found"}`). The route MAY read TIER1_V2 data (the
profile scope + the demand ledger) but its existence gates on
SUPPLIER_PORTAL_V1 alone. Implementation: a module-level `_portal_enabled()`
guard inside each portal handler that raises 404 (matching FastAPI's unknown-
route shape) when the flag is off — proved by an inertness test mirroring
Night 5's `test_falsy_token_is_flag_off`
(`utils/procurement_agent/tests/test_tier1_matcher.py:88`) and the onboarding
demo-allowlist-absence test (`test_onboarding_api.py:332`).

Frontend routing (Next.js): the backend gate is the load-bearing boundary. A
public `/portal/[token]` page is a new Next route under `frontend/src/app/`,
but the brief's T5 + morning-verification test through the real API
(TestClient/incognito), not via the Next router. The frontend public page is a
thin client of the `/api/portal/*` endpoints.

## I4 — supplier_notifications read path + genuine-vs-test separation

The `supplier_notifications` table EXISTS on the current base
(`utils/supplier_registry.py:358-383`; DDL executed in `_get_conn` at `:468`).
Accessors: `record_supplier_notification` (`:1715`), `get_supplier_notifications`
(`:1763`, filters by run_id and/or domain). Both are TIER1_V2-gated: flag-off →
writes return None, reads return [] (`_tier1_dormant()` at `:1769`).

**The sole writer is the live notify layer.** `record_supplier_notification` is
called from exactly one place: `utils/procurement_agent/tier1_notify.py:175`
inside `notify_tier1`, which fires post-sourcing for onboarded Tier 1 suppliers
that cleared the notify gate (brand-match-or-core-class). The notify layer is
itself only invoked from the live sourcing pipeline. Confirmed by grep:
`grep -rn "record_supplier_notification" --include="*.py"` → only
`tier1_notify.py:175` (call) + `supplier_registry.py:1715` (def) + tests.

**There are NO seed/demo/synthetic notification rows by construction.**
`_maybe_seed` (`utils/supplier_registry.py:475-489`) inserts ONLY into the
`suppliers` table (the 11 `_SEED_VENDORS` at `:386`, all `discovery_only`). No
code path inserts seed/demo/synthetic rows into `supplier_notifications`. The
`send_status` field ("stubbed"|"sent"|"error") refers to the EMAIL send being
stubbed (EMAIL_SEND_ENABLED defaults OFF + the conftest safety net at
`utils/procurement_agent/tests/conftest.py:63` + TIER1_V2 = the double-gate) —
it does NOT mean the match is synthetic. A "stubbed" row is a genuine match
event whose FYI email was queued, not sent.

**The read path is therefore naturally honest:** the teaser counts
`supplier_notifications` rows for the supplier's domain within a stated time
window. There is nothing synthetic to exclude — the table contains only real
notify events by construction. The count is satisfiable entirely within token
scope (filter `supplier_domain = ?` to the token's supplier) — no per-request,
per-buyer, or per-time-window detail beyond the bare count is returned
(success criterion: the route returns the COUNT, not the rows).

**DEV-DB-HYGIENE CAVEAT (prominent — a morning-verification discrepancy, NOT a
build blocker):** the live `data/supplier_registry.sqlite` currently holds
**3** `supplier_notifications` rows, ALL for `dxpe.com` (DXP Enterprises),
all `notify_reason=core_class`, all `send_status=stubbed`:

| notified_at          | run_id                                  | origin |
|----------------------|-----------------------------------------|--------|
| 2026-07-08T22:05:14  | 9b558fbf-…-d9a052dd0e45 (UUID)          | real API sourcing run (POST /api/runs mints UUID run_ids) |
| 2026-07-08T19:32:14  | run-test                                | manual/direct call to record_supplier_notification (non-UUID) |
| 2026-07-08T19:31:22  | run-1                                   | manual/direct call (non-UUID) |

The brief's morning-verification step 2 expects DXP to show **≈1** ("the single
stubbed Goulds-seal match recorded in Night 5"). The dev DB will actually show
**3** within a 30-day window. The two extra rows are NOT synthetic/seed (no
seeding code wrote them) — they are genuine match events written by direct
manual calls to `record_supplier_notification` during Night 5 development
(non-UUID `run_id`s; the API only ever mints UUID run_ids, so these did not
come through POST /api/runs). The read-only scratch inspector `check_notif.py`
(untracked, pre-existing) only SELECTs — it did not create them.

This is a dev-DB pollution artifact, not a read-path defect: in production no
one runs scratch scripts against the notify layer, so the count is honest. The
build's teaser is honest (counts real notify events; zero fabrication; zero-
state falls back to category/network framing). Success criterion 8 is asserted
with **isolated fixture DBs** (a has-matches fixture supplier with N genuine
notify events + a no-matches fixture supplier with 0), where the count is exact.

**Decision: do NOT add a `run_id`-format heuristic filter** (e.g. "only count
UUID-shaped run_ids"). That would paper over the dev-hygiene issue per the
brief's "not to paper over" instruction, and it is fragile (a future test
created via the API would have a UUID and be counted anyway). Instead:
- Build the honest teaser (count rows for the domain in the window).
- Document this caveat in the morning report so the human decides: (a) clean
  the two manual rows from the dev DB, (b) accept that the dev count includes
  manual-test events, or (c) add a deliberate source/flag field in a
  supervised session.
- Do NOT touch `data/supplier_registry.sqlite` (do-not-touch data file) or
  `check_notif.py` (pre-existing untracked scratch file).

This is consistent with the brief's I4: the read path CAN exclude
seed/demo/synthetic rows because there ARE none — the concern the brief was
guarding against (fabricated inflation) does not materialize. The dev-DB
discrepancy is flagged for the human, not papered over.

---

## Self-gate verdict

All four seams are consistent with the brief's expectations. The one nuance
(I4 dev-DB pollution) is documented above and is NOT a material contradiction:
the build is honest, success criteria are satisfiable with fixtures, and the
dev-DB discrepancy is a morning-verification finding for the human. **PROCEED
to Step 4 (build T1→T5).**
