# NIGHT 8 KICKOFF — Multi-Channel Intake: Event Spine + Email Adapter (SMS/Voice Stubbed)

Overnight unmanned build. Foreground-only. NO PUSH. **Runs in the PARALLEL WORKTREE** (`..\gofer-night8`) while Night 7 (read-only audit) runs in the main tree — see pre-flight.

---

## PRE-FLIGHT (mandatory)

| Variable | Value |
|---|---|
| WORKING DIR | the worktree `..\gofer-night8` — verify: `git rev-parse --git-dir` shows a `.git/worktrees/...` path. STOP if you are in the main tree. |
| BASE_BRANCH | branched from `test/flag-on-integration` at the same BASE_HEAD as Night 7 — record it |
| WORK_BRANCH | `feature/email-intake-overnight` (the worktree was created on it) |
| FLAG | `INTAKE_CHANNELS_V1` — new, own kill switch. Flag off ⇒ no intake endpoints exist, no behavior change anywhere. Falsy-token parity test required. |
| SUITE BASELINE | ~1864 passed / 73 skipped — confirm in the worktree; STOP if different |
| EXPECTED ABSENCE | `data/supplier_registry.sqlite` is untracked and will be ABSENT in the worktree. This is expected and correct — do not create/copy it; tests run on fixtures/mocks as the suite already does. |
| ITERATION CAP | 5 per failing task |
| FINAL ACT | `MORNING_REPORT_NIGHT8.md` at worktree root. NO PUSH. |

Standing guardrails carry over wholesale from prior nights: mocks only, NO live network, NO live email/SMS/voice sends ("send" anywhere = stubbed under the existing `EMAIL_SEND_ENABLED` double-gate + conftest safety net), no do-not-touch paths (`.env`, `audit/`, purge guards, `known_parts.json`, `price_db.json`, seed/demo fixtures, §7.7 flaky pair), one commit per task, suite green at every commit. Any notification/DB rows created by tests use the `is_test=1` provenance marking.

---

## MISSION

Requests are born in email, texts, and phone calls — not in an app. Build the **channel-agnostic intake spine** once, with the **email adapter fully working (in tests)** and **SMS + voice adapters as contract-stubs**, so every future channel is a thin transport, not a new pipeline. Everything lands in the EXISTING intake pipeline — same identification, same gates, same sourcing run. **A transport, never a parallel pipeline, never an auto-purchase trigger.**

---

## SETTLED DESIGN DECISIONS

1. **One normalized intake event.** Define a single typed event: tenant identity, channel (EMAIL|SMS|VOICE), sender identity + verification level, text body, media attachments (nameplate photos), channel metadata (message-id / phone number / call transcript ref), received_at. Adapters produce this event; ONE consumer feeds it into the existing intake pipeline.
2. **Per-tenant addressing.** Email: per-tenant inbound addresses (pattern mirrors the claim-token tenant-keying — investigate the cleanest scheme: plus-addressing `intake+<tenant>@…` vs per-tenant mailbox mapping; propose in I-gate, decide, document). SMS/voice stubs: number→tenant mapping table, same shape.
3. **Unknown-sender defence.** A sender not recognized for the tenant gets a confirm step (stubbed reply: "confirm this request came from your plant") — never silently creates a sourcing run from an unverified stranger. Known senders flow straight through. This is the spam/abuse gate.
4. **Parser honesty (the intake wrong-part gate).** The parser PROPOSES a structured request from the message; it never invents specifics that aren't stated or in attachments. Ambiguous/underdetermined messages land as NEEDS_CLARIFICATION with a stubbed clarifying reply — never a confidently-wrong request entering sourcing. (Do not try to fix the known Goulds-3196 over-clarification defect — that's Night 7's audit territory; the adapter feeds the pipeline AS IT IS.)
5. **Fires the sourcing run.** A successfully parsed, tenant-attributed, sender-verified intake event triggers the existing sourcing run exactly as an in-app request does. Same flags, same gates.
6. **Acknowledgement reply.** Inbound gets a stubbed "Got it — we're on it" reply (email adapter), recorded not sent, under the send double-gate.
7. **AUTO-ORDER IS EXPLICITLY OUT.** Nothing in this build places, approves, or advances an order. DoA/one-tap approval is Night 9+ territory; autonomous ordering is a gated future decision. If a task seems to need it, STOP.

---

## INVESTIGATION GATE (read-only; file:line; self-gate — proceed if consistent, HALT if contradicted)

- **I1.** The existing intake pipeline's entry seam: exactly where an in-app request enters (function/endpoint), what shape it expects, what fires the sourcing run. The intake-event consumer must call THIS seam.
- **I2.** The existing gmail_client / reply-processing machinery from the RFQ loop: what's reusable for inbound parsing (MIME/attachment handling, mocking patterns), and how inbound-intake mail is kept cleanly separate from RFQ-reply mail (they must never cross streams).
- **I3.** Tenant model: how tenants are identified today, and the cleanest per-tenant address scheme (decision 2).
- **I4.** The image path: how an attached nameplate photo gets from an email attachment into the same image-handling the in-app photo intake uses.

---

## BUILD TASKS

- **T1. Intake event contract + consumer.** The typed event, its validation, and the single consumer that maps a valid event into the existing pipeline seam (I1) and fires the sourcing run. Property test: NO path from consumer to order placement/approval.
- **T2. Email adapter.** Inbound email (mocked transport per I2 patterns) → tenant resolution (per-tenant address) → sender check (decision 3) → parse to proposed structured request (decision 4: propose-don't-invent; NEEDS_CLARIFICATION path) → intake event. Attachments (photos) carried through per I4. Stubbed ack + stubbed clarify replies under the send gate.
- **T3. SMS + voice contract-stubs.** Webhook-shaped endpoints (flag-gated) that accept the channel payload (SMS text/MMS media; voice = transcript + metadata — NOT the conversational agent, which is explicitly out of scope), produce the same intake event, with tests proving contract conformance. Thin: the point is the contract, not the channel.
- **T4. Inertness + security tests.** Flag off ⇒ endpoints absent (byte-identical to unknown routes). Unknown sender ⇒ confirm step, no sourcing run. Malformed/garbage inbound ⇒ safe rejection, nothing enters the pipeline. Cross-tenant: a sender known to tenant A cannot create runs for tenant B.
- **T5. Live-faithful path.** The email→event→sourcing-run flow exercised through the real API/consumer with the suite's standard mocks (TestClient-level, not direct function calls).

## SUCCESS CRITERIA (falsifiable)

1. Valid email from a known sender to a tenant address ⇒ structured request created ⇒ sourcing run fired (same run as in-app), attachment available to identification.
2. Ambiguous email ⇒ NEEDS_CLARIFICATION + stubbed clarify reply; NO sourcing run with invented specifics.
3. Unknown sender ⇒ stubbed confirm step; no run until confirmed.
4. SMS + voice stub payloads produce conformant intake events (tests).
5. Flag off ⇒ no intake endpoints exist.
6. Zero live sends; zero order-path reachability (property test).
7. Suite ≥ baseline + new tests, green throughout.

## OUT OF SCOPE

The ElevenLabs conversational voice agent; live email/SMS provisioning (Twilio/Gmail credentials); DoA bands / one-tap approvals (Night 9); auto-ordering (gated future); fixing intake clarification logic (Night 7 audit's territory); any frontend UI for intake.

## MORNING REPORT

`MORNING_REPORT_NIGHT8.md` at worktree root, prior-night structure: guardrail compliance, per-task commits, suite counts, unspecified decisions enumerated, the I3 addressing-scheme decision documented, morning-verification commands. NO PUSH.
