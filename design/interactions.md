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
- `inventory` → Intake
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

**Optimistic messages:** Appended locally with `opacity-60`. Cleared when
server message count grows past the baseline set at send time. Cleared
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
Shown when `results === null`. Centered, blue pulsing dot.
- Header: "Sourcing in progress…"
- Subtext: "Scanning Arkim network, open marketplace, and specialist vendors. This typically takes 30–90 seconds."
- Four animated skeleton bars, staggered opacity.

### Polling
`useRunLive` polls every **5 seconds** while phase is in
`["sourcing", "executing", "fulfilling", "inventory"]`. Polling stops for all
other phases. TanStack Query handles refetch; no manual interval management.

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

### Buy button
- Tier 1: primary variant. Tier 2+: secondary variant.
- Label: "Buy Now" if price present, "Request Quote" otherwise.
- Sub-label (monospace 9px):
  - CA facility: "Procured through Arkim"
  - Non-CA: "Visit vendor · your procurement"
- CA facility buy action: opens confirmation modal (see below). Non-CA: `window.open(url, "_blank", "noopener,noreferrer")`.

### CA Buy Now confirmation modal
Shown before `selectCandidate` fires for CA facilities. Rationale: no actual
transaction occurs yet; modal prevents misrepresentation in demos.

- `role="dialog"`, `aria-modal="true"`, `aria-labelledby` wired to title.
- Escape key dismisses. Backdrop click dismisses.
- Title: "Buy Now via Arkim"
- Body: explains MoR infrastructure is pending; selection advances run to approval only.
- "Cancel" — closes modal, no state change.
- "Continue to approval" (primary) — fires `selectCandidate` mutation, closes modal.

### External link button
Ghost variant, External icon (13px). Shown only if URL present.

---

## 6. Tier 3 Outreach Cards

### Card structure
Horizontal flex. Checkbox on left, content on right.

**Selected state:** `border-green-line`, `bg-green-tint`.
**Unselected state:** `border-hr-2`, hover `border-hr-1`.
**Click target:** Entire card div toggles selection.

### Checkbox
Custom rendered 4×4 box. Selected: `border-green-fg bg-green-fg` + white
checkmark SVG (10×10, stroke-width 2, round caps/joins). Unselected:
`border-hr-1 bg-bg-2`.

### Content layout
- Header row: vendor name (bold, truncate) + location (monospace, uppercase,
  tertiary, right-aligned).
- Contact info: right-aligned, monospace 10px, max-width 140px, truncated.
- Suitability: label "Suitability" (shrink-0, w-16) + MatchBar + percentage.
- Lead time: Clock icon (12px) + monospace text.

---

## 7. Sticky Action Bar (Tier 3)

Rendered only when `tier3.length > 0`. Sticky bottom-0, z-10.
Border-top `border-hr-2`, `bg-bg-1`.

### Selection count display
"{count} vendor{s} selected". Count in bold white, remainder tertiary.

### Buttons (left to right)

| Button | Variant | State | Behavior |
|---|---|---|---|
| Preview drafts | ghost | Always disabled | Toast: "Draft preview coming in Phase 5." |
| Save selection | secondary | Loading during save | Toast (green): "Selection saved". Persists candidate IDs to `tier3_selection_json`; hydrated back into Zustand on next mount via `run.tier3_selection`. |
| Send outreach | primary + Send icon (13px) | Loading during send | Success toast: "Outreach sent · Contacted {count} vendor(s)". Failure toast (amber): "Outreach failed · Check backend connection and retry." |

All three buttons disabled when `count === 0`.

Sub-label below Send button: monospace 8.5px, "On your behalf · Arkim sends, you receive".

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
| `initiateOutreach` | run detail only |
| `saveOutreach` | none (local selection state only) |
