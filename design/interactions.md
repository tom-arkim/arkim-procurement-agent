# Arkim — Interaction Design Reference

Settled interaction rules derived from Phases 1–4. Use this as the source of
truth when building Phase 5+ features. Changes to these rules require updating
this file in the same commit.

---

## 1. Navigation and Layout

### Sidebar
- Fixed width 220px. Always visible on desktop; collapsible (state: `sidebarOpen`
  in Zustand store).
- Wordmark: "A" gradient-blue box (6×6) + "ARKIM" monospace text.
- Main section: Sourcing runs list, New run button.
- Bottom section (push via `mt-auto`): Admin nav (Facilities, Approval rules,
  Settings) + user info.
- Active link: `blue-tint` bg, `blue-fg` text, `blue-line` left border.
- Inactive link: transparent, `fg-3` text, hover `bg-bg-3`.

### Run page two-column layout (intake phase)
- Left column: `flex-1` — chat panel.
- Right sidebar: `w-72` (288px), `bg-bg-2`, `border-hr-2` divider.
- Spec panel lives in the right sidebar.

### Run summary bar
- Always shown at top of a run page.
- Run ID: monospace "RUN" label + first 8 chars of UUID, uppercase.
- Asset summary: sans-serif, truncated to max-width 240px.
- Pills (left to right): Phase, Urgency (omit if Stocking), Warranty (show
  only if Active), Amount (if present).
- Live phases pulse a dot inside the phase pill:
  `["intake", "inventory", "sourcing", "comparison"]`.

---

## 2. Phase Routing

### View assignment

| Phase(s) | View |
|---|---|
| `pending_intake` | `PendingIntakeView` (centered, single column) |
| `intake`, `inventory` | `IntakeView` (two-column) |
| `sourcing`, `comparison`, `approved` | `SourcingView` |
| `pending_first_approval`, `pending_second_approval`, `executing`, `fulfilling`, `completed`, `cancelled`, `error` | `TransitionalView` |

### TransitionalView states

| Condition | Indicator | Message |
|---|---|---|
| `executing`, `fulfilling` | Blue pulsing dot | "The sourcing pipeline is running. This page will update automatically." |
| `pending_first_approval`, `pending_second_approval` | Amber pill | "Waiting for approver sign-off. The run will advance once approved." |
| `completed` | Green pill | "Run complete. Results and selected vendor are locked." |
| `cancelled`, `error` | Red pill | "This run was cancelled or encountered an error." |

### Phase progress bar (five steps)
Steps: `Intake → Sourcing → Comparison → Approval → Completed`

Phase-to-step mapping:
- `pending_intake`, `inventory` → Intake
- `comparison` → Comparison
- `pending_first_approval`, `pending_second_approval`, `approved`, `executing` → Approval
- `fulfilling`, `completed`, `cancelled` → Completed
- `error` → Intake (treated as pre-intake failure)

Completed steps: `green-50` bg/border + Check icon.
Active step: `blue-50` bg/border + step letter.
Inactive steps: `bg-bg-3` border + step letter.
Step labels: monospace 9.5px uppercase.

### Approval history (TransitionalView)
- Rendered only when `run.approval_history` is non-empty.
- `approved` entry: `green-line` border, `green-tint` bg.
- Rejected entry: `red-line` border, `red-tint` bg.
- Shows approver role (bold, prominent) and optional notes (smaller, tertiary).

---

## 3. Intake

### Chat panel

**Empty state:** Package icon (28px), heading "What are you sourcing?", subtext
"Describe the part, paste a model number, or attach a nameplate photo."
Centered vertically.

**Textarea:** Min height 38px, max 120px, auto-grows. Enter sends, Shift+Enter
newlines. Placeholder: "Describe the part or paste specs…" Border goes
`blue-line` on focus.

**Input hint row:** "Enter to send · Shift+Enter for new line · 📎 for nameplate photo"

**File upload:** Hidden `<input accept="image/*">`. Button labelled with Plus
icon, title "Attach nameplate photo". Images only.

**Drag-and-drop upload:** The entire input area (`border-t` container) is a
drop target. On `dragover`: border switches to `border-blue-line`; an absolute
overlay with a dashed blue border and "Drop image here" label appears.
On `dragleave`/`drop`: overlay clears. Dropped file uses the same upload
path as the file picker. Non-image drops are silently ignored.

**Image preview in chat:** When a file is uploaded (picker or drag-drop), an
optimistic user-role message appears immediately in the chat thread
(right-aligned, same bubble style as text messages). The message contains:
- Thumbnail of the image (`max-h-200px`, `object-contain`).
- Filename below the thumbnail (monospace 10px, fg-4, truncated).

The optimistic preview uses a local `URL.createObjectURL` URL stored only on
the client-side message object (`attachment.previewUrl`). It is cleared when
the server confirms (message count grows past the pre-upload baseline). The
URL is revoked on component unmount to avoid memory leaks. Server-issued
system messages (`Nameplate uploaded: …`) continue to appear as centered
banners as before; the preview is an additive user-side confirmation.

**Input hint row:** "Enter to send · Shift+Enter for new line · 📎 or drag image here"

**Optimistic messages (text):** Appended locally with `opacity-60`. Cleared
when server message count grows past the baseline set at send time. Cleared
immediately on send failure (not persisted).

**Auto-scroll:** Scrolls to bottom on any new message, pending message, or
while send is in flight.

### Message bubbles

- User: right-aligned, `blue-tint` bg, `blue-line` border, max-width 78%.
- Agent: left-aligned, `bg-bg-3`, `hr-2` border, max-width 78%.
- Pending: `opacity-60`.
- Timestamp: monospace 10px. User: `blue-fg/60`. Agent: `fg-4`.
- Typing indicator: three 1.5×1.5 dots, each offset by 0.2s pulse delay.

### Confirm-card gate

**Visibility:** Card is shown when:
1. Phase is `intake`, AND
2. Specs contain `manufacturer` OR `part_number`, AND
3. Card has not been explicitly dismissed.

**Reset:** Dismissal state clears when the manufacturer, part_number, or
manufacturer_confidence fingerprint changes — surfaces the card again if a
follow-up message improves the extraction.

**Manufacturer field:** Always prominent. Confidence percentage shown
right-aligned, monospace, `tabular-nums`. Confidence < 70 triggers amber
background and warning text: "Confidence below threshold — verify before
confirming."

**Secondary fields:** Model, Part No. (monospace), Type (if present). Rendered
in dot-leader format (label … value).

- When `spec_based_sourcing` is `true` on the specs: Model and Part No. render
  as "By spec" in `text-fg-4` italic — indicates the field was intentionally not
  required for sourcing (spec-based path), not missing or unanswered.
- When `spec_based_sourcing` is false/absent: null fields render as "—"
  (not-yet-provided indicator).

**Sufficiency message variants (text input path — `send_message`):** The agent
message that fires when `sufficient=true` depends on whether a model or part
number was identified:

| Condition | Message |
|---|---|
| `proceed_with_manufacturer_caveat` AND NOT (both confidences ≥ 70 + model + PN present) | "Specs extracted but the manufacturer could not be confirmed. Verify the manufacturer in the panel before confirming." |
| Both model and part_number absent (spec-based path) | "Sourcing by category — we have enough specs (manufacturer, type, key dimensions) to find functionally equivalent options. No specific part number or model is required." |
| Model or part number present (fully-identified path) | "Specs look complete — review in the panel and confirm to start sourcing." |

Guard: if both `manufacturer_confidence` and `part_id_confidence` are ≥ 70 AND
model + part_number are both present, the `proceed_with_manufacturer_caveat`
branch is bypassed and the full-confidence message fires instead. Prevents
contradictory caveat on high-confidence extractions.

The spec-based path is triggered when both `model` and `part_number` are null
or null-equivalent ("N/A", "UNKNOWN-PN", etc.) at sufficiency. The backend sets
`spec_based_sourcing: true` on the AssetSpecs payload in this case.

**Image upload response variants (`POST /api/runs/{id}/upload`):** Four cases,
evaluated in order:

| Condition | Message |
|---|---|
| `sufficient=true` | "Extracted: {mfg} {pn\|model} — specs are in the panel. Review and confirm to start sourcing." |
| Both confidences ≥ 70, mfg present, `sufficient=false` (required field missing) | "Read the nameplate: {ident}. Some required fields may still be missing — review the panel and fill in any gaps before confirming." |
| At least one confidence < 70, mfg present | "Read the nameplate: {ident} (manufacturer confidence N%). {Dimension-specific low-confidence phrase} — please verify the specs in the panel[ or provide the part number directly]." Low-confidence phrase: "Part identification confidence is low" when mfg ≥ 70 and part < 70; "Manufacturer confidence is low" when mfg < 70 and part ≥ 70; "Confidence is low" when both < 70. The PN suggestion is omitted when `part_number` is already populated. |
| mfg absent or unreadable | Three-option recovery message (try clearer photo / type specs / continue with partial). |

**Actions:** Two equal-weight secondary buttons. Neither is primary visually.
- "Edit / Continue Chat" — dismisses card, returns focus to chat.
- "Confirm & Source" — fires `POST /api/runs/{id}/confirm-intake`, advances
  to sourcing.

**Extracting skeleton (spec panel, during sourcing phases):**
- Blue pulsing dot + "Extracting specs…" while `inventory` or `sourcing`.
- Ghost dot + "Awaiting description" when idle.
- Five animated skeleton bars at staggered widths (80%, 65%, 55%, 70%, 45%).

---

## 4. Sourcing Dashboard

### Loading state
Shown when `results === null`. Centered, blue pulsing dot. Displays three timed
process-step transitions to communicate what the system is doing during the
~7-second sourcing window.

**Step sequence:**

| Step | Trigger | Label | Subtext |
|---|---|---|---|
| 1 | 0s (immediate) | "Scanning Arkim Network..." | "Checking onboarded partners for confirmed pricing" |
| 2 | 2s | "Checking marketplaces..." | "Searching public catalogs for live availability" |
| 3 | 5s | "Reaching out to specialists..." | "Identifying regional distributors and authorized service brands" |

**Transition:** Step label and subtext fade out (200ms) then fade in with the
next step. The persistent "Sourcing in progress…" header and pulsing dot remain
visible throughout.

**Progress indicator:** Three pill-shaped dots below the step content. Active
step: wider pill (`w-4`), `blue-fg`. Completed steps: smaller dot, `blue-fg`
40% opacity. Upcoming: smaller dot, `bg-bg-3`.

**Timing rationale:** Step timings are hardcoded approximations of the observed
~7s typical sourcing duration. They are not driven by real backend events — this
is intentional for this prototype phase. Backend SSE/websocket integration would
require server-side changes and is deferred. The step labels describe real phases
that are actually happening (Tier 1 network check → Tier 2 marketplace scan →
Tier 3 specialist outreach), so the display is semantically honest per brief
Section 11.

**Result arrival:** When sourcing completes, `results` becomes non-null and
`SourcingView` immediately renders the comparison layout regardless of which step
is currently displayed. Step timers are cleaned up on component unmount via
`useEffect` cleanup. No forced wait for the third step to finish.

**Slow-sourcing behavior:** If sourcing exceeds 7 seconds, Step 3 remains
visible indefinitely. No looping, no fourth step. Step 3 is the catch-all for
extended sourcing runs (slow Tavily response, large result sets, etc.).

### Polling
`useRunLive` polls every **5 seconds** while phase is in
`["sourcing", "executing", "fulfilling", "inventory", "comparison"]`. Polling stops for all
other phases. TanStack Query handles refetch; no manual interval management.

`comparison` is included because Tier 1 mock confirmation responses are delivered
asynchronously while the run remains in `comparison` — polling picks them up without
a manual refresh.

Comparison artifact generation runs while phase is still `sourcing`. Phase
advances to `comparison` only after artifacts are written — the frontend
always receives a complete payload on its first post-transition poll.

### Tier layout

| Tier | Layout | Gap | Label | Blurb |
|---|---|---|---|---|
| 1 — Arkim Network | Grid 1→2→3 col | 12px | "Arkim Network" | "Preferred partners · price-locked · instant PO" |
| 2 — Open Marketplace | Grid 1→2→3 col | 12px | "Open Marketplace" | "National distributors · live pricing" |
| 3 — Outreach | Vertical flex | 12px | "Outreach" | "Regional specialists · negotiated quotes" |

Tier colors: Tier 1 = blue, Tier 2 = amber, Tier 3 = green.

Tier numeral badge: 7×7 box, monospace 13px bold, tier color.
Tier name: monospace 11.5px bold uppercase, `tracking-[0.08em]`.
Result count: monospace 10.5px secondary (e.g. "5 results").
Blurb: monospace 10px tertiary uppercase, `tracking-[0.06em]`.

Dividers between tiers: `border-hr-2` full-width.

### Empty tier state
Monospace centered text. Messages:
- Tier 1: "No Arkim network partners found for this part."
- Tier 2: "No open marketplace results found."
- Tier 3: "No outreach candidates identified."

### Warranty banner
If `results.warrantyBanner` is present, amber-tinted banner above all tier
content.

### No-exact-match banner
Rendered above all tier content (below warranty banner if both present) when
`run.no_exact_match === true`.

**Signal computation (backend, `_orm_to_detail`):** Examines transformed Tier 2
+ Tier 3 candidates. If the combined list has ≥ 1 candidate and none have
`pnMatchLevel == "exact"` (mapped from `pn_match_status == "exact_match"` in
raw Tavily data), `no_exact_match` is set True. Suppressed when:
- `asset_specs.spec_based_sourcing === true` (spec-based path, no PN to match)
- `asset_specs.part_number` is absent or null-equivalent (same — no PN to miss)
- T2 + T3 are both empty (no results at all is different from "results exist but no match")

Tier 1 candidates are excluded from the signal — they are seeded data with
assumed-exact match and do not reflect Tavily discovery results.

**Copy:** "No vendors had this exact part number. All candidates below are
functionally equivalent alternatives — review specs carefully before purchase."

**Visual treatment:** amber-tinted (`bg-amber-tint`, `border-amber-line`,
`text-amber-fg`), same scheme as warranty banner. Warn icon (14px) left of text.
Not dismissible — persists while reviewing candidates.

**Behavior:** Informational only. Does not block Buy Now, Request Confirmation,
or Tier 3 outreach selection. Per brief Section 11: "Trust is built by
transparency, not polish."

### Tier 3 capability pivot notice
If `results.tier3CapabilityPivot` is true, amber banner:
"Sourcing pivoted to specialist outreach — direct match not found in tiers 1 or 2."

### Tier 3 automatic pre-selection
On `SourcingView` mount, top 3 Tier 3 vendors by suitability score are
pre-selected. Runs once per mount (guarded by `initialized.current` ref).
User toggles after mount are never overwritten by this effect.

---

## 5. Vendor Cards (Tier 1 / Tier 2)

### Structure
`rounded-card`, `border-hr-2`, `bg-bg-3`, vertical flex, gap-3.
If `candidate.isOemDirect`, border upgrades to `border-blue-line`.

### Vendor type label mapping

| `vendorType` value | Display label |
|---|---|
| NetworkPartner | Network |
| NationalDistributor | Marketplace |
| AftermarketCompatible | Aftermarket |
| AuthorizedDistributor | OEM Auth |
| RegionalSpecialist | Specialist |
| IndustrialSurplus | Surplus |

### Header pills (left to right)
1. "OEM Direct" — `blue` pill (only if `isOemDirect`).
2. "Authorized Distributor" — `blue` pill (only if NOT OEM Direct).
3. "Aftermarket" — `amber` pill (if `isAftermarket`).
4. Vendor type label — `ghost` pill (always shown).

### Price display
- Price present: monospace 15px bold, `tabular-nums`, 2 decimal places with
  comma thousands separator.
- Price hidden (`price_tbd` or `requires_rfq`): "Quote Required", monospace
  12px uppercase.

### Lead time
Clock icon (12px) + monospace text. Derived server-side from `lead_time_days`:
- ≤1 day → "Next day"
- ≤5 days → "{N} days"
- ≤14 days → "1–2 weeks"
- ≤30 days → "2–4 weeks"
- >30 days → "4+ weeks"

### P/N match + relationship
`PnMatch` component shows level. Relationship (e.g. "Authorized Distributor")
shown as green pill if present.

### Suitability bar
Label "Suitability" (shrink-0, w-16) + `MatchBar` + percentage label.

### Tier 1 action states

Each Tier 1 card resolves to one of three states based on `candidate.confirmationPending`
(server-side flag, flipped by backend after mock delay) and Zustand `tier1ConfirmSentAt`:

| State | Condition | Display |
|---|---|---|
| Request Confirmation | `confirmationPending === true` AND no Zustand sentAt | Primary "Request Confirmation" button. Fires `POST /runs/{id}/request-confirmation`, then records sentAt in Zustand on success. Sub-label: "Procured through Arkim". |
| Awaiting response | `confirmationPending === true` AND sentAt present in Zustand | Card dims to ~60% opacity, becomes non-interactive (`pointer-events-none`). "Awaiting" ghost pill added to header. Action area replaced by a centered status block: Clock icon + "Awaiting response" (medium-weight, fg-2) + "Sent HH:MM" (fg-4, smaller). |
| Buy Now | `confirmationPending === false` (backend flipped after mock delay, delivered via polling) | Primary "Buy Now" / "Request Quote" button → Buy Now modal → `selectCandidate`. Sub-label: "Procured through Arkim". |

### Tier 2 action
Always shows secondary "Buy Now" (or "Request Quote" if no price). Buy action opens
confirmation modal → `selectCandidate` → approval.

Sub-label: "Available via marketplace · Arkim purchases".

### Buy Now confirmation modal
Applies to both Tier 1 (Buy Now state) and Tier 2. All Arkim-mediated purchases go through
this modal — no `window.open` external redirects for primary actions.

- `role="dialog"`, `aria-modal="true"`, `aria-labelledby` wired to title.
- Escape key dismisses. Backdrop click dismisses.
- Title: "Buy Now via Arkim"
- Subtitle (tier-appropriate, monospace xs, tertiary):
  - Tier 1: "This places a purchase order through your Arkim Network Partner."
  - Tier 2: "This purchases through an open marketplace vendor. Arkim handles the transaction on your behalf."
- Body: explains MoR infrastructure is pending; selection advances run to approval only.
- "Cancel" — closes modal, no state change.
- "Confirm Purchase" (primary) — fires `selectCandidate` mutation, closes modal.

### External link button
Ghost variant, External icon (13px). Shown **only on Tier 2 cards** (if URL present).
Not shown on Tier 1 — Arkim Network Partner transactions are fully mediated; no direct
vendor page links in the UI.

---

## 6. Tier 3 Outreach Cards

### Card structure
Horizontal flex. Checkbox (or Clock icon) on left, content on right.

**Selected state:** `border-green-line`, `bg-green-tint`, `cursor-pointer`.
**Unselected state:** `border-hr-2`, hover `border-hr-1`, `cursor-pointer`.
**Sent state:** `border-hr-2 opacity-60 cursor-default`, non-interactive.

Click target is the entire card div (disabled in sent state).

### Checkbox / sent indicator
- Sent state: Clock icon (12px) in place of checkbox.
- Selected (not sent): `border-green-fg bg-green-fg` + white checkmark SVG
  (10×10, stroke-width 2, round caps/joins).
- Unselected: `border-hr-1 bg-bg-2`.

### Content layout
- Header row: vendor name (bold, truncate) + right-aligned status/contact:
  - Sent: "Awaiting" ghost pill (replaces the text label).
  - Not sent: contact info (monospace 10px, max-width 140px, truncated), if present.
- Suitability: label "Suitability" (shrink-0, w-16) + MatchBar + percentage.
- Lead time: Clock icon (12px) + monospace text.
- Sent state bottom block: centered status block below lead time — Clock icon (13px)
  + "Awaiting response" (monospace 12px, font-medium, fg-2) + "Sent HH:MM" (10px, fg-4).
  Matches the visual treatment of Tier 1 cards in awaiting state.

### Sent state trigger
`OutreachCard` receives a `sentAt?: string` prop from `SourcingView`, populated from
`run.tier3_outreach_sent?.[c.id]`. If `sentAt` is non-empty, the card renders in sent
state. This persists across page refreshes via the server-stored value.

---

## 7. Sticky Action Bar (Tier 3)

Rendered only when `tier3.length > 0`. Returns `null` when Zustand
`tier3Selection[runId]` count is 0 — hides automatically after successful outreach
or when all selections are toggled off. Sticky bottom-0, z-10.
Border-top `border-hr-2`, `bg-bg-1`.

### Selection count display
"{count} vendor{s} selected". Count in bold `fg-1`, remainder `fg-3` tertiary.

### Confirm outreach button
Single primary button with Send icon (13px). Loading while mutation is in flight.

- Success: clears `tier3Selection[runId]` in Zustand (bar hides) + green toast
  "Outreach sent · Contacted {count} vendor(s)".
- Failure: amber toast "Outreach failed · Check backend connection and retry."

Sub-label below button: monospace 8.5px, "On your behalf · Arkim sends, you receive".

### Selection lifecycle
1. SourcingView mounts → top 3 Tier 3 vendors by suitability pre-selected
   (guarded by `initialized.current` ref; never overwrites user changes).
2. User toggles cards freely — selection is fully reversible until Confirm outreach fires.
3. On success: all selected candidates receive `tier3_outreach_sent[id] = sentAt` in DB.
4. On next poll/mount: `OutreachCard` receives `sentAt` prop → dims, shows
   "Awaiting response · HH:MM", disables toggle.
5. Unselected (unsent) cards remain interactive — subsequent batches can be sent.

---

## 8. Match Scoring Display

### P/N match level

| Level | Tone | Label |
|---|---|---|
| `exact` | green | "Exact P/N" |
| `normalized` | green | "Normalized" |
| `stem` | amber | "Stem match" |
| `substring` | amber | "Partial" |
| `none` | ghost | "No match" |

### Compatibility summary

| Value | Tone |
|---|---|
| `fit_confirmed`, `fit_likely` | green |
| `verification_required` | amber |
| `incompatible` | red |

### MatchBar score thresholds

| Score | Tone |
|---|---|
| ≥ 90 | green |
| ≥ 75 | blue |
| ≥ 60 | amber |
| < 60 | red |

---

## 9. Toast System

- Tones: `blue`, `green`, `amber` only. No red toast.
- Fields: `tone`, `head` (required), `sub` (optional), `sticky` (optional).
- Sticky toasts do not auto-dismiss (reserved for persistent conditions like
  connection loss).
- IDs: auto-generated via `Math.random().toString(36).slice(2)` — not UUIDs.

### Rendering (ToastStack component)
- Mount: `providers.tsx`, inside `QueryClientProvider` — works across all routes.
- Position: `fixed bottom-4 right-4`, `w-80`. Mobile (`max-sm`): `inset-x-4 w-auto` (full width minus margins), still anchored to bottom.
- Stacking: `flex-col-reverse`, `gap-2`. Newest toast rendered last in array → appears at top. Maximum 5 visible; oldest evicted when limit exceeded.
- Auto-dismiss: 4 000 ms for non-sticky. Sticky toasts dismissed via X button only.
- Animation: slide-in from right + fade-in over 200ms on mount. Slide-out to right + fade-out over 200ms on dismiss (leaving state drives exit class before DOM removal).
- Tone styling: `border-l-2` with `{tone}-line` left border; `{tone}-fg` icon. Base: `bg-bg-3 border-hr-2 shadow-card rounded-card`.
- Structure: tone icon (left, 15px) · head + sub stacked (flex-1) · X dismiss button (right, 13px).
- `z-[200]` — sits above modals (`z-50`) and sticky action bars (`z-10`).

---

## 10. Asset Specs Panel

### Always-shown fields
Manufacturer, Model, Part No., Category.

### Expandable fields (15+)
HP, RPM, Voltage, Frame, Shaft, Enclosure, Phase, GPM, PSI, Impeller,
Mech seal, Material, Bore, Protocol, Warranty, Failure mode, Asset ID.

### Confidence indicators
`manufacturer_confidence` always shown. `part_id_confidence` shown if present.

### Expand/collapse
- Collapsed: shows count of hidden extra fields.
- Expanded: shows "Collapse" text.
- Chevron rotates based on state.
- Default in spec panel: expanded. Default elsewhere: collapsed.

---

## 11. Urgency and Warranty Pills

### Urgency tone

| Urgency | Tone |
|---|---|
| Emergency | red |
| Predictive | amber |
| Stocking | ghost |

Urgency pill omitted from run summary bar when value is "Stocking".

### Warranty pill
Green pill shown only when value is "Active". Other states: no pill.

---

## 12. Confirmed Thresholds and Constants

| Value | Constant | Where used |
|---|---|---|
| Manufacturer confidence gate | 70 | Confirm-card amber warning, `assess_proceed_state` |
| PN prefix lookup confidence | 92 | `_pn_prefix_hint` return value; overrides LLM when LLM conf < 92 |
| Polling interval | 5 000 ms | `useRunLive` |
| Spec panel width | 288px (w-72) | Intake two-column layout |
| Message bubble max-width | 78% | Chat panel |
| Chat textarea height | min 38px, max 120px | Chat panel |
| Asset summary truncation | 240px | Run summary bar |
| Contact truncation (Tier 3 card) | 140px | Outreach card |
| Run ID display length | 8 chars | Run summary bar |
| Tier 3 auto-pre-select count | 3 (top by suitability) | SourcingView mount |
| Approval rules cache stale time | 5 minutes | `useApprovalRules` |
| Facilities cache stale time | Infinity | `useFacilities` |

---

## 13. API Conventions

### Phase enum values (backend → frontend)
`intake`, `inventory`, `sourcing`, `comparison`, `pending_first_approval`,
`pending_second_approval`, `approved`, `executing`, `fulfilling`, `completed`,
`cancelled`, `error`

### `facility_state` field
Present on `RunDetail`. Defaults to `"unknown"`. Currently resolved from a
hardcoded mapping of 4 CA facilities. Used by vendor card to determine buy
button behavior (CA → Arkim MoR path; non-CA → external link).

### Price visibility rule
Backend sets `price` to `null` when `price_tbd` OR `requires_rfq` is true.
Frontend never needs to check these flags directly.

### Endpoints with cache invalidation

| Mutation | Invalidates |
|---|---|
| `confirmIntake` | run detail + run list |
| `selectCandidate` | run detail + run list |
| `requestConfirmation` | run detail (polling delivers the flip to `confirmationPending=false`) |
| `initiateOutreach` | run detail only |
| `openFromPending` | run detail + run list |
| `rejectSubmission` | run detail + run list |

---

## 14. Maintenance Handoff (pending_intake phase)

### Handoff payload schema

| Field | Type | Notes |
|---|---|---|
| `submission_id` | `string` | Opaque ID from the maintenance app |
| `facility_id` | `string` | Must match a known facility |
| `submitted_by` | `string` | Display name of the maintenance tech |
| `asset_specs` | `object \| null` | Pre-populated specs; seeded into `asset_specs_json` |
| `context.chat_thread_summary` | `string` | Full maintenance conversation summary |
| `context.urgency` | `"emergency" \| "predictive" \| "standard"` | Maps to urgency_factor (0.9 / 0.5 / 0.3) |
| `context.work_order_id` | `string \| null` | Optional; shown as monospace badge |
| `context.asset_tag` | `string \| null` | Optional; shown as monospace badge |

### Runs list: Pending from Maintenance queue

- A second section, labelled "Pending from maintenance", appears above the Active section when any `pending_intake` run exists.
- `phaseTone("pending_intake")` → `"amber"`. Pill shows `PHASE_LABELS["pending_intake"]` = "Maintenance".
- No pulse dot — `isLive` returns `false` for `pending_intake` (waiting on human action, not a running pipeline).
- "Active" section header only renders when both pending and active sections are non-empty.

### PendingIntakeView layout

Centered single-column. Top to bottom:
1. `PhaseBar` (maps `pending_intake` → "Intake" step)
2. Amber "Maintenance Handoff" pill + submitted_by text
3. `work_order_id` and `asset_tag` as monospace badges (omitted when null)
4. Asset card: manufacturer · model, part_number (omitted when specs absent)
5. Context card (`amber-tint` bg, `amber-line` border): `chat_thread_summary` text
6. Two action buttons: "Open for Review" (primary) + "Reject Submission" (ghost, `red-fg`)

### Open for Review transition

- `POST /api/runs/{run_id}/open-from-pending`
- Backend: `pending_intake → intake`; injects `chat_thread_summary` as a synthetic `role: "agent"` message into `_messages[run_id]`
- On success: invalidate run detail + run list → `useRunLive` refetch → phase changes to `intake` → routing predicate `isPendingIntakePhase` returns false → `isIntakePhase` returns true → `IntakeView` renders
- Chat panel displays the synthetic agent message as the first message; ConfirmCard appears because `asset_specs` is already populated from the handoff

### Reject Submission transition

- `POST /api/runs/{run_id}/reject-submission`
- Backend: `pending_intake → cancelled`
- On success: push amber toast "Submission rejected" → `router.push("/runs")` → run disappears from Pending queue (phase is now `cancelled`, filtered out)

### Button states

- Both buttons disabled while either mutation is `isPending`
- "Open for Review" label → "Opening…" while `openMut.isPending`
- "Reject Submission" label → "Rejecting…" while `rejectMut.isPending`

### Seeded demo run

`_seed_demo_maintenance_run()` runs at API server startup (idempotent: skips if any `pending_intake` run exists). Creates one E+H Promag 10W run at `fac-stockton` with `urgency_factor = 0.9` (emergency). Submission ID: `maint-sub-demo-001`.

---

## 15. Notification Feed (bell + dashboard "new" dots)

A real notification feed over `GET /api/events`, replacing the placeholder bell toast. **This is V1 — read honestly.**

### What it is (and what it is NOT)
- **Derived + real-state-only.** The feed is shaped server-side (`api_server.py` `_derive_events()`) from existing rows — order statuses, run approval phase/history, confirmed quotes. There is **no notifications table** and **no migration**. An event shows "Order shipped" **only because** the backend order status is `shipped`; there is no optimistic notice.
- **Untargeted.** No verified per-user identity exists yet (`initiated_by_user_id` is unpopulated, `company_id` is NULL in the no-auth demo), so the feed lists events across **all** runs and **never claims a specific person was notified or emailed.** Do not add "notified [person]"-style language.
- **Read-only.** The frontend never writes through this surface.

### Bell dropdown (`proc-shell.tsx`)
- Bell in the top bar opens a dropdown listing events newest-first: each row is the event `title` + a relative timestamp, and (when the event has a `run_id`) links to `/parts/{run_id}`. Rows with no `run_id` are non-navigable (disabled).
- **Honest empty state:** "No recent updates."
- Closes on outside-click or `Escape`. Opening triggers a `refetch()`.
- Feed fetch (`useEvents`) is **fail-soft**: any error resolves to an empty list, so a feed hiccup never crashes the shell. Refetches on mount + window focus + bell-open — no heavier interval than the dashboard already runs. Follows the `cache: "no-store"` convention for freshness.

### Unread badge + dashboard dots — ONE marker
- Unread is a **per-device** "new since you last looked" marker, **NOT** a server-side per-user read-state. It is a single ISO timestamp in `localStorage` under `arkim:events:lastSeen` (SSR-guarded: read in `useEffect`, never during render).
- **Badge:** count of events with `timestamp > lastSeen`, shown on the bell; capped display "9+"; hidden at 0.
- **Opening the bell** sets `lastSeen` to the newest known event timestamp (falls back to `now` only if the feed is empty) — using the event timestamp, not the client clock, so clock skew can't leave a just-seen event unread. This clears the badge.
- **Dashboard "new" dots:** on the "Needs you" decision cards and "In flight" rows (`home-screen.tsx`), a run shows a small dot when it has an event newer than the **same** `lastSeen` marker (via `useProcEvents()`). One source of truth — opening the bell clears the dots too. The dot reinforces the status the row already renders; it is not a second signal.
- `lastSeen` persists across reloads, so already-seen events don't re-surface as unread.

### Deferred (recorded so V1's honesty is on the page)
- **Per-user targeting + server-side read-state** → gated on auth (a later increment). Until then the feed is untargeted and "seen" is per-device.
- **Email / push delivery** → a later increment. V1 surfaces in-app only.
- **A notifications store** → not built; the feed stays derived from existing rows.

## 16. Supplier Onboarding (concierge v1 — Night 4)

URL → harvest → extract → prepopulate a supplier profile → concierge
review/approve → an onboarded supplier in the Night 3 TIER1_V2 registry.
**Deterministic core is live (admin-gated); UI polish is morning.**

### Where it lives
- **Admin surface only.** `/admin` → "Onboarding" tab. Every endpoint is under
  `/api/admin/onboarding/*` and gated by `require_admin` (401/403/503 — same
  bearer-token gate as the rest of the admin surface) AND by `TIER1_V2` (503
  dormant when the flag is off). The routes are NOT on the `DEMO_MODE`
  allowlist — a public demo 403s them fail-closed (the harvester fetches
  arbitrary URLs server-side; it must not be reachable unauthenticated).
- **v1 is concierge-only.** An admin (operator) drives review/approve. There
  is NO supplier-facing magic-link/token review flow yet — that is a flagged
  follow-on. The approve action is the admin's explicit confirm.

### The must-confirm trio (always flagged, regardless of confidence)
- **Brand relationship** (AUTHORIZED / CARRIES / AFTERMARKET_COMPATIBLE), the
  **class core-competency** (is_core), and the **ship-area** are marked
  `must_confirm=True` on EVERY draft, no matter how confident the extractor
  is. These three drive sourcing routing and carry channel/territory risk;
  v1 never auto-applies them.
- The UI shows the `must_confirm` flags per section so the concierge knows
  exactly which fields need their eyes.

### Approve-gated write (the single writer)
- **Nothing writes to the supplier registry without approve.** Harvest/extract
  only creates a PENDING review item (the existing `review_items` "extraction
  lands as pending, a human confirms" pattern) — the scope tables are
  untouched until approve.
- **Approve** writes classes/brands/territory/verticals via the Night 3
  `set_supplier_*` API and drives the lifecycle
  `discovered→contacted→quoted→onboarding→onboarded`. **Double-approve is
  idempotent** (the registry setters are full-replace; a second approve is a
  no-op write that returns the same record). **Reject** discards (nothing
  applied).
- The concierge can edit name / vertical / ship-area / brands / classes on the
  draft before approving (revisions override the stored draft; approve is still
  the only registry-write point — the editor never writes directly).

### What the concierge sees (inspector)
- The draft's brands (name + relationship + confidence + evidence), classes
  (canonical noun-class + is_core star), locations, and ship-area, each with
  its `must_confirm` flag, plus overall confidence and the extraction method
  (LLM vs heuristic fallback). A raw per-field provenance (evidence quote +
  source_url) rides on each field for review.

---

## 10. Supplier claim-portal (public)

The app's first public, unauthenticated, customer-facing surface. Route
`/portal/[token]` — the token is the credential and lives in the URL. Behavior
gated by the backend `SUPPLIER_PORTAL_V1` flag (flag-off → the public route
returns 404, rendered as the uniform rejection; the admin portal surfaces
return 503 = dormant).

### Security posture (highest priority — first public route)
- The token is NEVER stored (no localStorage / sessionStorage / cookie), NEVER
  sent to a third party (no analytics / error reporting beacons), and NEVER
  logged. It is held in a ref for the page lifetime only.
- `Referrer-Policy: no-referrer` is set on the page (metadata + meta) so an
  outbound click can't leak the token via Referer. The API also sets it
  server-side.
- The public page calls ONLY `/api/portal/[token]/*` (a dedicated client,
  `lib/portal-api.ts`, separate from the internal-app client — no session
  header, no auth). It can reach no admin endpoint and exposes no admin data.
- Uniform rejection: invalid / expired / reused / flag-off / network failure all
  render the SAME generic "this link is no longer valid — contact your rep"
  page. The UI never surfaces a status code or distinguishes the failure kind
  (no oracle). The portal client returns only `ok` vs `rejected`.

### Demand-as-hero (settled decision 1)
- The teaser is the FIRST and most prominent element — above the profile form.
- `has_matches` → real count + window as the hero ("12 buyers matched your
  categories in the last 30 days"). The count is genuine (backend counts real
  buyer-match events only; never seeded/demo/synthetic).
- Zero-state (`has_matches:false`) → the `framing` text is the hero. NEVER a
  "0 matches" number, NEVER a fabricated/placeholder count, NEVER demo data
  (honesty carve-out).

### Profile-confirm form (settled decision 3 — anti-Ariba)
- Single-pass edit on ONE page (no multi-screen wizard): brands / classes /
  ship-area.
- Tri-state brand relationship (AUTHORIZED / CARRIES /
  AFTERMARKET_COMPATIBLE) is the centerpiece — the most prominent, clearest
  control (only the supplier authoritatively knows it).
- Aftermarket disclosure shown when the supplier carries any
  AFTERMARKET_COMPATIBLE brand (so they see what buyers see).
- One "Submit for review" → `POST /api/portal/[token]/propose-revision`. This
  NEVER writes the registry — it lands as a pending revision the concierge
  approves. Success message says "submitted for review," NOT "saved."
- Submit error is a soft-error that PRESERVES the supplier's input (never
  re-enter). Submitted state is amber "pending," not green "saved."

### Public page states
| State | Render |
|---|---|
| Loading | GoferLoader (branded spinner) |
| Valid + has_matches | Teaser count/window hero + profile form |
| Valid + zero-state | Framing text hero (no number) + profile form |
| Invalid / expired / reused / flag-off | Uniform rejection page |
| Submitted | Amber "submitted for review" confirmation |
| Submit error | Soft-error banner + form with preserved input |

### Brand-string discipline
- The customer-facing brand name is one constant: `lib/brand.ts` `BRAND_NAME`
  (defaults to "Arkim"; "Gofer" pending USPTO clearance). No hard-coded brand
  strings elsewhere in the portal. Backend follow-up:
  `utils/supplier_portal.py` `_ZERO_STATE_FRAMING` carries the same name inline
  — swap it in the same motion when the name settles.

## 11. Supplier claim-portal (admin controls)

In the admin inspector (`/admin`), token-gated as the rest of that surface.

### Generate claim link (T4) — Suppliers tab
- Per-supplier "Claim link" action on the suppliers tab → mints a claim link
  (`POST /api/admin/suppliers/claim-link`).
- The raw token is returned ONCE (hashed at rest) and shown in a show-once
  panel: the full link, a Copy button, the expiry, and a "copy and send now —
  you won't see this again" note.
- Regenerate (`POST /api/admin/suppliers/claim-link/regenerate`) revokes the
  prior token and mints a new one (same show-once rule).
- The show-once panel is cleared on tab switch so a raw token is never left
  visible on a stale screen.
- 503 = `SUPPLIER_PORTAL_V1` off (dormant); surfaced as such, not hidden.

### Revision review (T5) — Portal Revisions tab
- Surfaces pending supplier-proposed revisions (`review_items`
  `kind=supplier_revision`, `status=needs_human_review`). No new backend
  endpoint — `/api/admin/review-queue` already returns them; filtered
  client-side.
- Each row shows the proposed scope (brands / classes / ship-area).
- Approve → `POST /api/admin/portal/revisions/{id}/approve` (applies the scope
  to the registry — the ONLY writer). Reject → `/reject` (discards, nothing
  applied). Resolved revisions collapse under a details element.
- Closes the propose→approve loop the public claim page starts.

### Accessibility (WCAG 2.1 + CVD) — portal palette & status system
- **Neutrals unchanged:** graphite `#26282B` (13.67:1 on paper) and slate
  `#4E5A63` (6.55:1) are the text workhorses and stay as-is.
- **Orange demoted to non-text accent only.** Bright `#F4581C` cannot carry text
  (best 3.36:1 → fails AA). It stays for border-lefts, checked-state borders,
  and input `accent-color` (3:1 UI). Any orange that carries text or a button
  label uses **deep-orange `#B23A12`** (white-on-it 5.99:1; on-paper 5.54:1).
- **Primary action button** (`.portal-submit`): graphite `#26282B` fill + white
  text (14.78:1). Deep-orange `#B23A12` (5.99:1) is the documented alternate if
  an orange button is ever wanted.
- **Amber `#E8A33D` is fill/border only, always with graphite text (6.85:1).**
  It is never a text color. The submitted/disclosure eyebrows use graphite text;
  amber remains only as the card border/accent.
- **Status redundancy (WCAG 1.4.1 — color is never the sole signal):** every
  status carries a SHAPE (glyph) + a TEXT LABEL + color.
  - Submitted → clock glyph + "Submitted for review" + amber card border.
  - Rejection → warning-triangle glyph + "This link is no longer valid" (neutral
    card; no status hue — the state reads by shape + words alone).
  - Glyphs are inline SVG in graphite (13.67:1; passes AA + 3:1 non-text); the
    status color lives on the card border, never the glyph.
  - The teaser has-matches/zero-state pair is already distinguished by text
    content, so no non-color cue is added there.
- **Future success/error status** (not yet wired): success `#1E7A50` (white text
  5.31:1), error `#B3261E` (white text 6.54:1) — white-text-safe and more
  CVD-separable than the originals. **Standing rule:** never introduce a
  green/red (or any color-only) status pair without shape + text-label
  redundancy, so the deuteranopia green↔red / protan green↔amber collisions can
  never cause a misread. (Encoded as a comment at the portal token block.)
- **Referrer policy:** set once via the page `metadata.referrer = "no-referrer"`
  export (the single source); no inline body `<meta>`. The API also sets it
  server-side.
