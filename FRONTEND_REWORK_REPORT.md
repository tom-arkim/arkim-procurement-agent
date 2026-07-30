# FRONTEND REWORK REPORT — Buyer & Supplier Surfaces

**Branch:** `feature/frontend-rework` (off `test/flag-on-integration`) · **NO PUSH** (per brief).
**Scope check:** every work commit touches only `frontend/` (`git diff --stat` verified per commit);
this report + `FRONTEND_REWORK_SHOTS/` are the brief's sanctioned repo-root final act.
**Gates:** `npm run type-check` and `npm run lint` clean at every commit; `npm test` (new) green — 8/8.

## 0. Honest scope note

The brief was written as a ground-up rework; investigation showed the app was already far
along — most §1 invariants were established product law in the code (seller-named cards,
linked evidence, three price states, the dashed outreach block, honest empty state, uniform
token rejections). The work was therefore **surgical**: build what was genuinely missing
(dedup, grouping, phase-driven progress, skeletons, the confirmed-quote treatment, the
composition unit tests, DESIGN_NOTES), tighten what fell short of the floor (44px targets,
standardized focus rings, triage ordering), and verify all of it against the live flag-on app.
Nothing was restyled for restyling's sake — the existing token system is good and stays.

## 1. Commits (one per screen/component group, per brief §5)

| Commit | Group | What changed |
|---|---|---|
| `cfd2374` | (a) design system | ≥44px touch-target floor (pseudo-element hit areas + padding bumps), skeleton primitives (`Skel`/`SkelList` + CSS), shared `ProcErrorNote`, `proc-rowic` tile, `frontend/DESIGN_NOTES.md` |
| `9b54ece` | (b) options screen | **`options-compose.ts`** (pure, unit-tested): render-time dedup by registrable domain (richest fronts: quoted > priced > exact-PN > bare; collapsed card keeps the domain's first server position) + buy-now/quote-needed grouping preserving in-group server band order; "Also listed at N more pages" affordance; supplier-confirmed **conf-band**; compact outreach block above thin (≤2) findings; legacy path untouched; vitest + 8 tests (§7.9) |
| `055db35` | (c) intake | Run stepper (Identify → Search → Options) now driven by `run.phase` — the timer-driven pseudo-progress checklist is gone; photo drag-and-drop with visible drag state; identification card: editable manufacturer/model/PN before Confirm (existing asset-specs PUT), real extraction confidence shown |
| `8cf3334` | (d) home + secondary | Home triage now surfaces mid-intake runs ("Waiting on your answer" → `/request?resume=`); reorder affordance prefills a new request (`?prefill=`, no endpoint invented); skeletons replace every raw "Loading…" (home, approvals, history, impact, thresholds); designed approvals empty state |
| `558a5b9` | (e) quote form | Post-submit confirmation echoes the submitted quote summary; revision renders "Update your quote" + explicit supersede copy |
| `4178775` | (f) portal | Quote-history status chips (dot shape + label + colour, palette-safe); portal revise flow gets the same supersede framing |
| `4d07fd3` | polish | Triage order decisions > handoffs > clarifications (capped at 5 with an honest "+N more" count); standardized `:focus-visible` accent ring across the buyer surface |

## 2. Invariant audit (§1 — all live, flag-on backend, screenshots in `FRONTEND_REWORK_SHOTS/`)

Canonical fixture: Gusher Pumps Type 21 seal `84004-28-C238CBC`. Fresh run created through the
UI (`e6f546d1…`); findings-rich stored banded run (`42e0f71f…`) exercised through the live API.

| # | Invariant | Evidence |
|---|---|---|
| 1 | Seller always named | `options_buy_now_group_dedup.jpg` — cards headline **Seal It**, **Seals Direct**; "Order through Gofer" is only the action |
| 2 | Evidence always linked | same — every card "View listing ↗" (`target=_blank`, `noopener noreferrer`); collapsed duplicates each keep their own link (`options_dedup_expanded.jpg`) |
| 3 | Found PN structural | "PN 84004-28-C238CBC" line on-card; quoted-PN labelling verified in whyBullets + quote path |
| 4 | No fabricated numbers | quote-needed cards show no price/score (`options_quote_needed_group.jpg`); no stars/ratings anywhere |
| 5 | Three price states | verified "no quote needed" (legacy shot), indicative "final price confirmed at order" (`options_buy_now_group_dedup.jpg`), unverified "≈$85.28 · price unverified ⓘ" (`BEFORE_options_unverified_interleaved.jpg` — same state renders post-rework behind the dedup affordance) |
| 6 | Findings ≠ outreach | `options_outreach_block_below.jpg` — dashed, no prices/scores/CTAs, "YOUR SUPPLIER · DXP Enterprises" first, intent-only copy |
| 7 | Honest empty state | `options_empty_state_outreach_first.jpg` — fresh live run: "We didn't find this part listed — requesting quotes from 6 suppliers", never an empty best-options list |
| 8 | No Order without price | quote-needed group renders "Get quote" only |
| 9 | Both payload shapes | banded (`42e0f71f`) composed; legacy (`03ce6bb2`, no banded keys) flat tier list — `options_legacy_flag_off.jpg` |
| 10 | A11y floor | 44px hit-area layer + padding bumps; standardized focus ring; status = shape+label+colour everywhere (chips/pills/glyphs); skeletons announced via `role="status"` |

**The proudest moment, live:** a structured quote submitted through the portal for the fresh run
promoted onto the buyer's options screen as **"CONFIRMED BY SUPPLIER — DXP Enterprises confirmed
$58.40 · 3 days · on 2026-07-30"** (green band, Supplier-confirmed tag, Order CTA), with the
compact outreach block above the thin findings — `options_promoted_quote_confirmed_band.jpg`.

## 3. Supplier surface walkthrough (§7.4–7.5, all live)

- **Quote form** `/quote/{token}`: form + prefills (`quote_form_360px.jpg` — 360px single-column,
  ≥44px targets, `inputMode` numeric keyboards, 16px inputs so iOS won't zoom); **pn-differs**
  calm inline notice (`quote_form_pn_differs.jpg`); **submit** → echoed quote summary + honest
  in-review copy + claim pitch (`quote_form_submitted_summary.jpg`); **revision** → "You quoted
  $51.75 earlier…" + "Update your quote" supersede framing (`quote_form_revision_note.jpg`,
  `quote_form_update_supersede.jpg`); **closed** state (`quote_form_closed.jpg`). No dead ends.
- **Portal** `/portal/{token}`: claim landing with real demand teaser ("matched 21 buyer requests
  in the last 30 days" — genuine count) (`portal_claim_teaser.jpg`); profile classes with CORE
  tags (`portal_profile_classes.jpg`); **open requests** → inline QuoteForm → "✓ Quoted $58.4"
  badge + **history with status chips** (Active) (`portal_open_requests.jpg`,
  `portal_quoted_history_chips.jpg`); brands edit → **"Submitted for review"** pending state,
  clock glyph + amber border, never silently absorbed (`portal_brands_pending_review.jpg`).
  Nothing cross-supplier renders (backend scopes by token; UI adds nothing).

Verification fixtures were minted with existing backend machinery only (`quote_tokens.mint_for_rfq`,
`claim_tokens.generate_for`, `supplier_registry.record_sent_message` — status `stubbed`, no email,
`is_test=1`). No backend code changed.

## 4. Before → after highlights

| Screen | Before | After |
|---|---|---|
| Options (money screen) | `BEFORE_options_undeduped.jpg` — Seal It, Seal It 123, Seals Direct ×2 as separate cards; priced/unpriced interleaved | `options_buy_now_group_dedup.jpg` / `options_quote_needed_group.jpg` — "Ready to order (2)" + "Quote needed (4)", same-domain listings collapsed with "Also listed at N more pages" |
| Sourcing wait | timer-driven 4-step pseudo-progress | `run_progress_stepper.jpg` — 3 real stages driven by `run.phase` |
| Home | `BEFORE_home_loading_text.jpg` — raw italic "Loading what needs you…", mid-intake runs invisible | `home_triage_after.jpg` — skeletons; decisions first, then clarifications with honest cap |
| Intake confirm card | identity read-only, no confidence | `intake_identified_gusher.jpg` — confidence 92% shown, Edit details, qty stepper |
| Approvals | `BEFORE_approvals_queue.jpg` (raw "Loading…" rows, plain-text empty) | `approvals_queue_after.jpg` + designed skeleton/empty states |
| Quote form post-submit | text-only confirmation | `quote_form_submitted_summary.jpg` — quote summary echoed back |
| Portal history | plain status text | `portal_quoted_history_chips.jpg` — shape+label+colour chips |

## 5. Component inventory

See `frontend/DESIGN_NOTES.md` (committed) — tokens for both palettes, type scale, spacing
rhythm, state rules, a11y floor, and the full component list. New this rework:
`options-compose.ts` (pure composition + tests), `AlsoListed`, conf-band, `Skel`/`SkelList`,
`ProcErrorNote`, `proc-rowic`, `portal-status-chip`, drag-state on `proc-ask`,
touch-target expansion layer, global buyer focus ring.

## 6. RBAC gaps found (§4 rule: render what the backend grants; fabricate nothing)

- **No requester identity**: runs carry no "requested by" user; the approvals queue can't show
  a requester column. Left absent (never invented).
- **Approver role is client-supplied** (`approver_role` on approve/reject) — no verified role
  exists, so the Approvals surface is visible to the (single, unauthenticated) tenant session
  exactly as before. RBAC enforcement is Arc 1 (CLAUDE.md §6).
- **Site admin vs requester**: no backend signal distinguishes them → Delivery settings stays
  visible as today.
- **Site/plant switcher**: `PROC_SITES` has two sites but nothing server-side scopes runs to a
  site, so switching would imply filtering that doesn't happen — the shell shows the primary
  site display-only, gap listed here instead of a fake switcher.
- **Notification read-state** is a per-device localStorage marker (no per-user store) — unread
  model stays honest-but-local, as before.

## 7. Wished-for endpoints (documented, not invented)

1. **Reorder → prefilled run**: a `POST /api/runs` accepting seed specs from a past order (today:
   `?prefill=` seeds the description text only).
2. **Per-part price history**: `GET /api/price-history?part=` from `price_db` (today the price
   cards derive only from the customer's own orders).
3. **Requester identity on runs** (blocks the approvals requester column).
4. **Site-scoped runs / facilities wire** (blocks a real site switcher).
5. **Sourcing progress events** (per-tier) — would let the wait screen show real sub-steps again.
6. **Multi-image intake** — `POST /runs/{id}/attachments` accepts one nameplate photo; the brief's
   multi-image dropzone needs a multi-file contract. Upload progress also needs a streaming
   endpoint (fetch gives no upload progress today).

## 8. Decisions taken (and why)

- **Dedup slots at the domain's first server position** — "preserve server order" read strictly;
  a richer duplicate fronts the card but never jumps the band queue. Unit-tested.
- **Grouping headers render only when both groups are non-empty** — a single-kind list stays
  plain rather than wearing a redundant header.
- **Conf-band replaces (not stacks on) the Recommended band** on supplier-confirmed cards — the
  stronger claim wins; stacked bands read as noise. An unverified-figure quote states the
  confirmation without asserting the number.
- **Clarification queue capped at 5 with an honest "+N more" count** — the test DB carries ~50
  stale mid-intake runs; a triage view that buries decisions under them fails its job, and the
  true count is still stated.
- **Kept legacy path byte-equivalent** (no dedup/grouping on tier arrays) — §1.9's detection
  stays payload-keyed and the flag-off render tree stays unchanged.
- **vitest added as devDependency** (only new dep) — §7.9 demands a unit test on the composition
  function; vitest is the lightest standard runner for a Next/TS repo. `npm test` wired.
- **`design/interactions.md` deliberately not updated** — the brief's hard rule is an empty diff
  outside `frontend/`; the behavior deltas needing documentation there at merge time are exactly
  the §1 commit list above.
- **Keyboard walkthrough caveat**: focus-visible rings are now standardized on both palettes and
  there are no traps (no positive tabindex, Escape closes the bell menu / tooltips, `details`
  fold is native). Full Tab-stepping could not be driven end-to-end through the browser
  automation harness (synthetic Tab doesn't move focus through CDP); rings + order were
  verified by CSS audit and element-level focus checks — a 2-minute manual Tab pass is
  recommended before merge.
- **"Before" screenshots** come from a temporary git worktree of `test/flag-on-integration`
  served on :3001 against the same live backend (config patched only inside the throwaway
  worktree for CORS; worktree removed afterwards).

## 9. Out of scope / untouched (per brief §6)

Admin/internal surfaces (`/admin`, labeling, release queue) — routes verified unbroken, not
restyled. No analytics, no auth redesign, no email templates, no dashboard-metrics widgets,
no payments UI, no i18n, no logo change. Backend: zero changes (`git diff` clean outside
`frontend/` for every work commit).

## 10. Backend suite

Frontend-only branch; `uv run pytest -q` run at the end per house rules:
**2305 passed, 73 skipped** (green — inherited unchanged from `test/flag-on-integration`).
Frontend: `npm run type-check` clean · `npm run lint` clean · `npm test` 8/8.
