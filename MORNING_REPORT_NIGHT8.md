# MORNING REPORT — NIGHT 8 (Multi-Channel Intake: Event Spine + Email Adapter, SMS/Voice Stubbed)

Unmanned overnight build. Foreground-only. **NO PUSH.**
Base: `test/flag-on-integration` @ `7be667b` (same BASE_HEAD as Night 7).
Work branch: `feature/email-intake-overnight` (a git **worktree** at `..\gofer-night8`,
parallel to the Night 7 read-only audit in the main tree).
Final HEAD: `8e664e7`. Suite: **1915 passed / 73 skipped** (1864 baseline + 51 new).

---

## 1. GUARDRAIL COMPLIANCE (numbered, itemised)

1. **Worktree verified, not the main tree.** `git rev-parse --git-dir` =
   `C:/dev/_Arkim/Arkim Procurement Agent Prototype/.git/worktrees/gofer-night8`;
   branch `feature/email-intake-overnight`; pwd `C:/dev/_Arkim/gofer-night8`. The
   session was initially launched in the main tree (branch
   `audit/matching-quality-night7`); per the brief's non-negotiable I STOPPED,
   made no changes, and reported. The operator uploaded the brief and confirmed
   the worktree; I entered the existing worktree via `EnterWorktree`. ✅

2. **BASE_HEAD recorded; branched from `test/flag-on-integration`.** BASE_HEAD =
   `7be667bc7a28e905415e7b79294f0fe58a46a0d7` (= `test/flag-on-integration` HEAD;
   `git rev-list --count HEAD..test/flag-on-integration` = 0). ✅

3. **All new behavior behind `INTAKE_CHANNELS_V1`; flag-off = endpoints absent.**
   Own kill switch (does NOT extend DEMO_MODE / TIER1_V2 / SUPPLIER_PORTAL_V1).
   Flag-off → every intake handler raises a 404 byte-identical to FastAPI's
   unknown-route body `{"detail":"Not Found"}`. Proven by
   `TestIntakeFlagOff.test_flag_off_404_byte_identical_to_unknown_route` (asserts
   the intake 404 == an unknown-route 404 in status + body) plus per-endpoint
   absence tests (email / confirm / sms / voice). The store/decision layer
   also no-ops on flag-off (defense-in-depth). ✅

4. **One normalized intake event; adapters are thin transports into the EXISTING
   pipeline seam. Never a parallel pipeline.** `utils/intake_channels.py` defines
   one `IntakeEvent`; every adapter (email / SMS / voice) produces it; ONE
   consumer (`consume_intake_event`) feeds it into the existing
   `confirm_intake` → `_run_sourcing_background` seam. The transition was
   factored into `_commit_intake_to_sourcing` (shared by `confirm_intake` and
   the intake firer) — `confirm_intake`'s behavior is byte-identical (the
   existing characterization suite stays green). ✅

5. **Parser honesty: propose, never invent. Ambiguity ⇒ NEEDS_CLARIFICATION.**
   The parser runs the EXISTING `IntakeAgent` over the message text + photo
   attachments (the same extractor the in-app intake chat / upload path uses).
   Insufficient specs / a family-variant block / an extractor failure all ⇒
   NEEDS_CLARIFICATION with a stubbed clarify reply — never a confidently-wrong
   request entering sourcing. The existing intake clarification logic is fed
   AS IT IS (the Goulds-3196 over-clarification defect is Night 7's territory;
   not touched). ✅

6. **AUTO-ORDER IS OUT. No path from intake to order placement/approval.**
   The consumer calls ONLY `fire_sourcing_run` (run creation + the
   confirm-intake→sourcing transition) + the reply sink. It never touches
   `orders.create_order` / `place_order` / `update_order_status` / the approve
   endpoints. Pinned by `test_intake_channels.TestNoOrderPath` (instruments all
   three `orders` functions and asserts they're never reached across every
   consumer outcome: RUN_CREATED, NEEDS_CLARIFICATION, UNKNOWN_SENDER, CONFIRMED)
   AND `test_intake_api.TestIntakeNoOrderPath` (API-layer: after an intake-fired
   run, the run is at SOURCING, no order row exists, `orders.create_order` /
   `place_order` never called). ✅

7. **Unknown sender ⇒ confirm step, no run. Cross-tenant isolation tested.**
   A sender not recognized for the tenant is held (token hashed at rest,
   `is_test=1`) + sent a stubbed confirm reply; no sourcing run. Known senders
   flow straight through. A sender known to tenant A is NOT known to tenant B
   (`TestCrossTenantIsolation.test_known_sender_for_a_not_b` — a bayfoods
   sender addressed to acme ⇒ confirm step, no acme run; the reverse ⇒ an acme
   run stamped with acme's facility). ✅

8. **Zero live sends.** Ack / clarify / confirm replies go through
   `email_sender.GmailSender` under the `EMAIL_SEND_ENABLED` double-gate
   (default OFF + the conftest autouse safety net forces it off) — always
   `status="stubbed"`, zero network. SMS/voice record replies, no transport
   exists (live SMS/voice provisioning is out of scope). ✅

9. **Any DB rows tests create: `is_test=1`.** The standalone
   `data/intake_channels.sqlite` store (`intake_known_senders`,
   `intake_held_events`) carries an `is_test` column; every test write + every
   build-time hold/add passes `is_test=True`. The held-event token is hashed at
   rest (SHA-256), raw token returned once — mirrors `claim_tokens`. ✅

10. **No do-not-touch paths touched.** `.env`, `audit/`, `known_parts.json`,
    `price_db.json`, seed/demo fixtures, the §7.7 flaky pair, purge guards —
    none touched. `data/supplier_registry.sqlite` is correctly ABSENT in the
    worktree (untracked); not created or copied (the suite runs on
    fixtures/mocks). The security/allowlist surface was NOT extended — the
    intake routes are deliberately NOT on `_DEMO_ALLOWLIST` (so DEMO_MODE 403s
    them fail-closed; intake is a tenant feature, not a demo feature). ✅

11. **`design/interactions.md` updated** in the same change as the user-facing
    behavior change (the intake surface), per CLAUDE.md §5. ✅

12. **Iteration cap 5 per failing task.** One test-isolation incident during T2
    (two root causes: an `IntakeAgent.__new__` patch that leaked across modules,
    and a `DEMO_MODE` module-attribute leak from `test_demo_mode`'s reload) —
    both diagnosed and fixed within the cap (3 iterations total across the
    incident). No task exceeded 5 iterations. ✅

13. **Final act: this report. NO PUSH.** Not pushed. ✅

---

## 2. PRE-FLIGHT (recorded values)

| Variable | Value |
|---|---|
| WORKING DIR | the worktree `..\gofer-night8` (verified `.git/worktrees/...`) |
| BASE_BRANCH | `test/flag-on-integration` |
| BASE_HEAD | `7be667bc7a28e905415e7b79294f0fe58a46a0d7` |
| WORK_BRANCH | `feature/email-intake-overnight` |
| FLAG | `INTAKE_CHANNELS_V1` (own kill switch; default OFF) |
| SUITE BASELINE | 1864 passed / 73 skipped (verified pre-flight) |
| FINAL SUITE | 1915 passed / 73 skipped (+51: 23 channels unit + 28 API) |
| ITERATION CAP | 5 per failing task (max used: ~3, on the T2 isolation incident) |
| COMMIT STYLE | Conventional Commits, one commit per task (6 commits + brief + interactions) |
| FINAL HEAD | `8e664e7` |

**Baseline note (environmental, not a regression):** the worktree has NO `.env`
(gitignored, untracked, not carried into the worktree). A bare `uv run pytest`
shows 1863 passed / 1 failed — the single failure is `test_health`, which
expects `capture_failures`/`label_failures` in the health body, fields that
only appear when `RUN_CAPTURE=1` (set in the main tree's `.env`). Replicating
the main tree's env (`RUN_CAPTURE=1 INTAKE_TYPE_AWARE=1 SCORING_V2=1 uv run
pytest`) gives the documented **1864 passed / 73 skipped** baseline exactly.
This is an environmental artifact of the worktree not carrying the gitignored
`.env`, not a code regression — confirmed by the matching main-tree `.env`
contents (`RUN_CAPTURE=1`, `INTAKE_TYPE_AWARE=1`, `SCORING_V2=1`). All
build-verification runs use these env flags. **The morning-verification
commands below include them.**

Pre-flight, in order:
1. Worktree check — initially FAILED (main tree); STOPPED, reported, re-entered
   the worktree after operator confirmation. ✅
2. BASE_HEAD captured = `7be667b` (= `test/flag-on-integration`). ✅
3. Baseline `RUN_CAPTURE=1 INTAKE_TYPE_AWARE=1 SCORING_V2=1 uv run pytest -q` =
   **1864 passed / 73 skipped**. ✅
4. `data/supplier_registry.sqlite` absent in the worktree (expected) — not
   created/copied. ✅
5. Brief present (copied into the worktree from the main tree — it was not
   committed to the branch; committed as a docs commit). ✅

---

## 3. INVESTIGATION GATE (I1–I4) — self-gate verdict: PROCEED

Read-only, file:line. Nothing materially contradicted the brief; the I1 pipeline
seam is confirmed and clean. Summary:

- **I1 (the intake pipeline entry seam — the one that matters most):** confirmed.
  Run birth: `POST /api/runs` (`create_run`, `api_server.py:1555`) →
  `_new_run_orm` (`api_server.py:1528`) at `Phase.INTAKE` with optional seeded
  `asset_specs`. Intake chat extraction: `POST /api/runs/{run_id}/messages`
  (`send_message`, `:1816`) runs `IntakeAgent.run(run_obj, {"text": ...,
  "images": [...]})` — does NOT advance phase. Image upload: `POST /api/runs/
  {run_id}/upload` (`upload_nameplate`, `:1970`) → `agent.run(..., {"images":
  [contents]})`. **The intake→sourcing transition seam: `POST /api/runs/{run_id}/
  confirm-intake`** (`confirm_intake`, `:2607`): 404/409/422 guards →
  family-variant binding guard → writes the inventory stub →
  `current_phase = Phase.SOURCING` → commits → schedules
  `_run_sourcing_background` (`:1123`) via `background_tasks.add_task`. The
  intake consumer fires THIS seam: `_fire_sourcing_run_for_intake` creates the
  run (tenant-stamped, specs seeded) + calls the factored
  `_commit_intake_to_sourcing` (the load-bearing transition, shared with
  `confirm_intake`). A transport, never a parallel pipeline.

- **I2 (gmail_client / reply-processing reuse + inbound/RFQ separation):**
  `gmail_client.build_gmail_service()` (`utils/gmail_client.py:67`) is a generic
  reusable service builder; `GmailInboxReader._parse_reply` (`utils/inbox_reader.py:170`)
  and `_get_raw` (`:135`) are pure/testable MIME parsers reusable for inbound
  intake. The RFQ reply path reads `procurement@arkim.ai` with query
  `in:inbox -from:mailer-daemon -from:postmaster` (`inbox_reader.py:224`) — NO
  recipient/subject/label filter; matching is post-hoc against `sent_messages`
  (`reply_matcher.py:33`); unmatched → `unmatched_reply` queue (never enters the
  RFQ pipeline, but is noise). **Separation:** the intake adapter keys on the
  `intake+<tenant>@arkim.ai` `To` address, which the RFQ path never targets as
  an outbound; the live path would use a distinct intake mailbox/label. The
  adapter only processes intake-addressed mail, so cross-stream is structurally
  impossible at the adapter boundary. (Live email provisioning is out of scope;
  the build's email adapter accepts a parsed envelope — a live path feeds raw
  RFC822 through the reused `_parse_reply` first.)

- **I3 (tenant model + the addressing-scheme DECISION):** tenant = `company_id`
  (a UUID PIN from Cognito, validated via `get_caller` against the JWT;
  `X-Arkim-CompanyId` header; `utils/auth/dependencies.py:50,97`). Facilities
  belong to a company (`_MOCK_FACILITIES` are all "Bay Foods" — single demo
  tenant). No existing per-tenant inbound address mapping, no known-senders
  table; `claim_tokens` is supplier-scoped, not tenant-scoped. **DECISION:
  plus-addressing `intake+<tenant-key>@arkim.ai`** (documented in §8.1 +
  `design/interactions.md`). Rationale: mirrors the claim-token tenant-keying
  pattern (identity encoded in the address, no separate mapping table to keep
  in sync), one credential set, scales without per-tenant provisioning, Gmail
  (the wired provider) supports RFC 5233 subaddressing natively. A fixture-
  overridable `_TENANT_MAP` resolves the plus-local-part to
  `(company_id, facility_id)`. SMS/voice use a `_NUMBER_TENANT_MAP` (same shape).

- **I4 (the image path):** confirmed clean. In-app upload reads
  `file: UploadFile` → `contents = await file.read()` →
  `agent.run(run_obj, {"text": text, "images": [contents]})` (`api_server.py:2038`);
  `IntakeAgent.run` handles the `images` kwarg (multimodal;
  `_detect_media_type` infers jpeg/png/webp from magic bytes). For email/MMS,
  the attachment bytes (from the parsed attachment `data`) are raw bytes passed
  directly as `images: [att.data]` — same handling, no disk I/O. Proven by
  `test_attachment_carried_through_to_intake_agent` (email) and
  `test_sms_mms_media_carried_to_intake_agent` (SMS).

---

## 4. PER-TASK STATUS + COMMIT HASHES

| Task | Status | Commit | Suite after |
|---|---|---|---|
| docs — Night 8 brief | ✅ DONE | `20e7023` | 1864 (baseline, no new tests) |
| T1 — Intake event contract + consumer + no-order property test | ✅ DONE | `54ba44a` | 1887 (+23) |
| T2 — Email adapter endpoint + unknown-sender confirm step | ✅ DONE | `a5a8034` | 1901 (+14) |
| T3 — SMS + voice contract-stub endpoints | ✅ DONE | `1baaec3` | 1908 (+7) |
| T4 — Inertness + security tests (consolidate) | ✅ DONE | `d8b6d93` | 1913 (+5) |
| T5 — Live-faithful path through real API + mocks | ✅ DONE | `2a1b395` | 1915 (+2) |
| docs — interactions.md | ✅ DONE | `8e664e7` | 1915 (no new tests) |

**Commit log (`git log --oneline 7be667b..HEAD`):**
```
8e664e7 docs(interactions): document multi-channel intake behavior (Night 8)
2a1b395 test(intake): live-faithful path through real API + consumer + firer (T5)
d8b6d93 test(intake): inertness + security — byte-identical 404, zero leakage, garbage rejection (T4)
1baaec3 feat(intake): SMS + voice contract-stub endpoints (T3)
a5a8034 feat(intake): email adapter endpoint + unknown-sender confirm step (T2)
54ba44a feat(intake): channel-agnostic intake event contract + consumer (T1)
20e7023 docs: add Night 8 multi-channel intake brief
```

**New files:** `utils/intake_channels.py` (the spine — 766 lines),
`utils/procurement_agent/tests/test_intake_channels.py` (unit + property, 23
tests), `utils/procurement_agent/tests/test_intake_api.py` (API-layer T2/T3/
T4/T5, 28 tests).
**Modified:** `api_server.py` (the `INTAKE_CHANNELS_V1` flag + the factored
`_commit_intake_to_sourcing` + `_fire_sourcing_run_for_intake` + the four
`/api/intake/*` endpoints), `design/interactions.md` (the intake behavior
section).

---

## 5. SUCCESS CRITERIA (falsifiable) — all met

1. ✅ Valid email from a known sender to a tenant address ⇒ structured request
   created ⇒ sourcing run fired (same run as in-app), attachment available to
   identification — `TestIntakeEmailFlow.test_valid_known_sender_fires_sourcing_run`
   + `test_attachment_carried_through_to_intake_agent` + the T5
   `TestLiveFaithfulPath` (run advances SOURCING→COMPARISON via the real
   background task; intake run indistinguishable from an in-app run).
2. ✅ Ambiguous email ⇒ NEEDS_CLARIFICATION + stubbed clarify reply; NO sourcing
   run with invented specifics — `test_ambiguous_email_needs_clarification_no_run`
   + `test_intake_channels.TestParserHonesty` (insufficient ⇒ no run; extractor
   failure ⇒ NEEDS_CLARIFICATION, never invent).
3. ✅ Unknown sender ⇒ stubbed confirm step; no run until confirmed —
   `test_unknown_sender_confirm_step_no_run` + `test_confirm_advances_held_event_to_run`.
4. ✅ SMS + voice stub payloads produce conformant intake events —
   `TestSmsVoiceContractStubs` (SMS/voice known sender ⇒ run; MMS media ⇒
   IntakeAgent images; unknown sender ⇒ confirm; no-tenant ⇒ safe reject;
   `test_sms_and_voice_produce_same_event_contract` proves both channels produce
   the same event shape + feed the same consumer, channel the only difference).
5. ✅ Flag off ⇒ no intake endpoints exist —
   `TestIntakeFlagOff` (email/confirm/sms/voice all 404; byte-identical to an
   unknown route; zero run rows; zero held events).
6. ✅ Zero live sends; zero order-path reachability (property test) —
   `TestNoOrderPath` (consumer) + `TestIntakeNoOrderPath` (API): the order
   surface is never reached across every consumer outcome; replies stubbed.
7. ✅ Suite ≥ baseline + new tests, green throughout — 1915 passed / 73 skipped
   (1864 + 51), green at every commit.

---

## 6. THE I3 ADDRESSING-SCHEME DECISION (documented)

**Decision: plus-addressing `intake+<tenant-key>@arkim.ai`.** Considered
alternatives:

| Scheme | Pros | Cons | Verdict |
|---|---|---|---|
| **Plus-addressing** `intake+<tenant>@arkim.ai` | one credential set; no per-tenant provisioning; scales infinitely; mirrors the claim-token tenant-keying pattern (identity in the address); Gmail supports RFC 5233 natively | provider-specific (Gmail supports it; a port to a non-subaddressing provider would need a fallback); relies on the provider preserving the `To` header | **CHOSEN** |
| Per-tenant mailbox mapping `intake_<tenant>@arkim.ai` | provider-portable; explicit | per-tenant provisioning (new tenant = new mailbox + creds); operational overhead; doesn't scale cleanly | rejected |

The tenant-key is resolved from the plus-local-part by
`resolve_tenant_from_address` (`utils/intake_channels.py`); a fixture-overridable
`_TENANT_MAP` maps it to `(company_id, facility_id)` for run stamping. A bare
`intake@` (no plus) or an unknown tenant-key ⇒ `TENANT_UNKNOWN` (no run, safe).
SMS/voice mirror this with a `_NUMBER_TENANT_MAP` (inbound number → tenant-key),
same shape. Production would back both maps from a tenants table; the maps are
the address→tenant resolution, kept separate from the run's `company_id` stamp
(which the firer sets from the map).

**This is the decision the brief asked to be documented.**

---

## 7. SECURITY POSTURE

- **Flag-gated, byte-identical 404.** Flag-off → intake handlers raise 404
  matching FastAPI's unknown-route body `{"detail":"Not Found"}` (no oracle, no
  leakage). Proven byte-identical to an unknown route.
- **NOT on the DEMO allowlist.** Under `DEMO_MODE` the allowlist middleware 403s
  the intake routes fail-closed (a public no-login demo must not accept inbound
  intake — it's a tenant feature). The flag-off 404 is the gate in normal
  dev/prod.
- **Unknown-sender defence (the spam/abuse gate).** A sender not recognized for
  the tenant is held (token hashed at rest, `is_test=1`) + sent a stubbed confirm
  reply; no sourcing run from an unverified stranger. The held event advances via
  `POST /api/intake/confirm/{token}` (one-time; a reused token ⇒ uniform 404, no
  oracle — mirrors the supplier portal). Known senders flow straight through.
- **Cross-tenant isolation.** The address resolves the tenant; the sender check
  is per-tenant. A sender known to tenant A cannot create runs for tenant B.
- **Malformed/garbage inbound ⇒ safe rejection.** `validate()` rejects
  (REJECTED_MALFORMED) before the parser is ever invoked — nothing enters the
  pipeline. TENANT_UNKNOWN for an unattributable address.
- **Token hygiene (held events).** `secrets.token_urlsafe(32)` entropy, SHA-256
  hashed at rest, lookup by hash (never string compare over raw tokens), one-time
  confirm (reuse rejected uniformly). Mirrors `claim_tokens`.
- **Zero live sends.** Replies via `email_sender.GmailSender` under the
  `EMAIL_SEND_ENABLED` double-gate (default OFF + conftest safety net) — always
  `stubbed`. SMS/voice record replies (no transport).
- **Intake vs RFQ-reply separation.** The adapter keys on the
  `intake+<tenant>@` `To` address, which the RFQ reply path never targets; the
  live path would use a distinct intake mailbox/label. Cross-stream is
  structurally impossible at the adapter boundary.
- **No auto-order.** The consumer's only run-affecting call is
  `fire_sourcing_run` (run creation + sourcing transition). It never orders /
  approves. Property-tested at both the unit and API layers.

---

## 8. UNSPECIFIED DECISIONS (enumerated)

1. **I3 addressing scheme: plus-addressing** (see §6). Documented. The
   `_TENANT_MAP` / `_NUMBER_TENANT_MAP` are fixture-overridable module dicts
   (tests monkeypatch); production would back them from a tenants table.
2. **Standalone store location:** `data/intake_channels.sqlite` (its own
   connection), NOT the supplier_registry or main DB — mirrors `claim_tokens`
   so `intake_channels.py` is a clean standalone module (house standard) with no
   shared-connection concerns. Override via `_DATA_DIR`/`_DB_PATH` (test-
   isolated).
3. **Held-event attachments are NOT replayed.** `hold_event` serializes
   attachment *metadata* only (bytes aren't JSON-safe); a confirmed event re-
   parses text-only. A real confirm path would re-fetch the original message's
   attachments. Documented in `consume_confirmed_event`'s docstring. For the
   build (mocked transport), the confirm path exercises text-only re-parse.
4. **Known-senders registry:** a `intake_known_senders` table (tenant_key,
   sender_email, `is_test`) — NOT auto-populated by the confirm step (the brief
   says "confirm step," not "auto-add as known"). Confirm just advances the held
   event as a CONFIRMED sender. Whether to persist a confirmed sender as known
   is a future decision (an admin/seed path uses `add_known_sender`). Tests seed
   known senders directly.
5. **Reply transport for SMS/voice:** none exists. The build records the reply
   in the outcome + the stubbed email-sender print path; live SMS/voice reply
   provisioning (Twilio) is out of scope. The email reply uses the existing
   `email_sender` (stubbed).
6. **`is_test` on the sourcing RUN row:** the run ORM has no `is_test` column
   (the intake surface is exercised under the TestClient, so runs it births are
   test runs by definition). Provenance for the intake *store* rows
   (known-senders / held-events) carries `is_test=1`. A real intake path would
   set `is_test=False` and stamp provenance; noted as morning/prod hardening,
   not a build defect (the run row already carries the tenant `company_id` from
   the address, which is the real provenance signal).
7. **The orchestrator (`utils/procurement_agent/orchestrator/core.py`) is NOT on
   the shipping path** (per CLAUDE.md §8) — the intake consumer fires the
   FastAPI/`_run_sourcing_background` seam, NOT the orchestrator. Untouched.
8. **The brief was NOT committed to the branch** when the worktree was created
   (it lived in the main tree only). I copied it into the worktree and committed
   it as a docs commit so the worktree state is clean and the morning report can
   reference it.

---

## 9. THE T2 TEST-ISOLATION INCIDENT (root causes + fix, within iteration cap)

During T2 the full suite showed 41 failures (my new tests + the
variant/quantity/classification/labeling intake tests) while every test passed
in isolation. Diagnosed as **test-ordering state pollution** with two
independent root causes, both in my new test code:

1. **`IntakeAgent.__new__` patch leak.** `_mock_intake_sufficient` patched
   `IntakeAgent.__new__` to return a mock. `monkeypatch` reverts the attribute,
   BUT `__new__` is a dunder resolved on the *type*, and the patch interfered
   with later tests' `IntakeAgent(anthropic_api_key="test-key")` construction
   (`TypeError: object.__new__() takes exactly one argument`). **Fix:** patch
   the *class object* on both bindings (`ia_mod.IntakeAgent` +
   `api_server.IntakeAgent`) with a lambda returning the mock — no dunder
   mutation, safe across the suite.
2. **`DEMO_MODE` module-attribute leak.** `test_demo_mode` (alphabetically
   before `test_intake_api`) reloads `api_server` with `DEMO_MODE=true` and
   leaves `api_server.DEMO_MODE = True` on the cached module. My `api` fixture
   imported the cached module without resetting it → the allowlist middleware
   403'd my non-allowlisted intake routes. **Fix:** the `api` fixture now
   `monkeypatch.delenv("DEMO_MODE")` + `monkeypatch.setattr(api_server,
   "DEMO_MODE", False)` (mirrors the `demo_off` fixture's discipline).

Both fixed in ≤3 iterations total (well under the 5-per-task cap). The incident
is a test-hygiene issue in MY new tests, not a production-code defect — the
production intake code is unaffected (it reads `INTAKE_CHANNELS_V1` / `DEMO_MODE`
live from env at call time). Documented for transparency.

---

## 10. BLOCKERS

None. The build is complete and green. The one item requiring a human decision
is the **baseline-env note (§2)**: the worktree has no `.env`, so a bare `uv run
pytest` reads 1863/1-failed (the `test_health` env-gated field). This is
environmental, not a regression — use the env flags in the morning-verification
commands. Whether to commit a minimal `.env` or a `.env.example` to the branch
is a morning decision (`.env` is gitignored and a do-not-touch path; I did not
create one).

---

## 11. MORNING VERIFICATION (~20 min, supervised) — exact commands

The intake surface is backend-only (the brief defers frontend UI for intake to
out-of-scope). Verification is via the API directly. The worktree has no `.env`,
so set the flags explicitly (replicating the main tree's `.env` so the suite
matches the documented baseline).

```bash
# 0. Confirm you're in the worktree, on the right branch.
cd C:\dev\_Arkim\gofer-night8
git rev-parse --git-dir    # -> .../.git/worktrees/gofer-night8
git branch --show-current  # -> feature/email-intake-overnight
git log --oneline -1       # -> 8e664e7 docs(interactions)...

# 1. Full automated re-run (the green net). Use the env flags (no .env in worktree).
#    PowerShell:
$env:RUN_CAPTURE="1"; $env:INTAKE_TYPE_AWARE="1"; $env:SCORING_V2="1"
uv run pytest -q
# -> 1915 passed, 73 skipped
uv run pytest utils/procurement_agent/tests/test_intake_channels.py utils/procurement_agent/tests/test_intake_api.py -q
# -> 51 passed (the new net)
```

```bash
# 2. Start the API with the intake flag ON.
$env:INTAKE_CHANNELS_V1="1"
uv run uvicorn api_server:app --port 8001
```

```bash
# 3. Valid email from a known sender -> sourcing run fired.
#    First seed a known sender for the demo tenant (the build's tenant map has
#    "bayfoods" -> company-bayfoods / fac-stockton). Use the module directly:
uv run python -c "from utils import intake_channels; intake_channels.add_known_sender('bayfoods','plant@bayfoods.com')"
#    Then POST an intake email (mocked transport):
curl -s -X POST http://localhost:8001/api/intake/email -H "Content-Type: application/json" -d '{
  "to":"intake+bayfoods@arkim.ai","from":"plant@bayfoods.com",
  "body":"Goulds 3196 5HP pump, part 3196MTX"}'
# -> {"status":"RUN_CREATED","run_id":"<uuid>","reason":null,"clarify_attrs":null}
#    GET the run -> phase "sourcing", specs seeded, company_id stamped.
curl -s http://localhost:8001/api/runs/<uuid> | python -m json.tool
```

```bash
# 4. Ambiguous email -> NEEDS_CLARIFICATION, no run.
curl -s -X POST http://localhost:8001/api/intake/email -H "Content-Type: application/json" -d '{
  "to":"intake+bayfoods@arkim.ai","from":"plant@bayfoods.com","body":"need a thing"}'
# -> {"status":"NEEDS_CLARIFICATION","run_id":null,...}
# (the stubbed reply is logged [EmailSender] STUBBED ... + [Intake] reply (clarify) -> ...)
```

```bash
# 5. Unknown sender -> confirm step, no run.
curl -s -X POST http://localhost:8001/api/intake/email -H "Content-Type: application/json" -d '{
  "to":"intake+bayfoods@arkim.ai","from":"stranger@bayfoods.com","body":"Goulds 3196"}'
# -> {"status":"UNKNOWN_SENDER_CONFIRM_SENT","run_id":null,"reason":null,"clarify_attrs":null}
#    A held event row (is_test=1, token hashed) is in data/intake_channels.sqlite.
```

```bash
# 6. Flag off -> intake endpoints absent (byte-identical 404).
#    Restart WITHOUT the flag:
$env:INTAKE_CHANNELS_V1="0"; uv run uvicorn api_server:app --port 8001
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8001/api/intake/email -H "Content-Type: application/json" -d '{"to":"intake+bayfoods@arkim.ai","from":"x@y.com","body":"hi"}'
# -> 404
curl -s http://localhost:8001/api/this-route-does-not-exist
# -> {"detail":"Not Found"}  (identical)
```

```bash
# 7. No-order property (re-run the property test explicitly):
uv run pytest utils/procurement_agent/tests/test_intake_channels.py::TestNoOrderPath utils/procurement_agent/tests/test_intake_api.py::TestIntakeNoOrderPath -q
# -> 2 passed (orders.create_order / place_order never reached from intake)
```

---

## 12. NOTES FOR THE MORNING

- **Frontend (out of scope):** the brief explicitly defers any frontend UI for
  intake. The backend API + the full test net are built and green.
- **Live email/SMS/voice provisioning (out of scope):** the email adapter
  accepts a parsed envelope (a live path feeds raw RFC822 through the reused
  `inbox_reader._parse_reply` first); SMS/voice accept webhook-shaped payloads.
  Twilio/Gmail credentials, webhook auth, and a distinct intake mailbox/label
  are morning/prod work.
- **Known-senders registry:** currently seeded via `add_known_sender` (tests) /
  the module directly. A real onboarding/admin surface for managing recognized
  senders per tenant is a future decision (§8.4).
- **`data/intake_channels.sqlite`** is created in the worktree's `data/` by the
  suite (test-isolated to `tmp_path` in tests; the module default path is used
  only by a manual `add_known_sender` call). It is untracked. Do not commit it.
- **The orchestrator** (`utils/procurement_agent/orchestrator/core.py`) is
  retained/tested but NOT on the shipping path (CLAUDE.md §8) — the intake
  consumer fires the FastAPI seam, not it. Untouched.
- **DO NOT PUSH.** This branch is local only (in the worktree); merge/review is
  a supervised decision.

---

*End of Night 8 report. No push performed. Worktree `..\gofer-night8`, branch
`feature/email-intake-overnight` at `8e664e7`, 7 commits, suite green at
1915/73.*
