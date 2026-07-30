# Gofer frontend — design system notes

What the system is, where it lives, and how to extend it without inventing one-offs.
Two palettes, **one component language**: the buyer (procurement) surface and the
supplier (portal + public quote) surface share structure, spacing, and state rules;
only the tokens differ.

## 1. Where things live

| Layer | File | Scope |
|---|---|---|
| Buyer tokens + components | `src/styles/procurement.css` | everything under `.proc-theme` (dark + light via `data-theme`) |
| Supplier tokens + components | `src/app/globals.css` (`.portal-surface` block) | public quote form + portal (warm off-white) |
| Shared React primitives | `src/components/proc/proc-ui.tsx` | `ProcPill`, `ProcHead`, `SecHead`, `Skel`/`SkelList`, `ProcErrorNote`, `ProcToast`, `procMoney` |
| Icons | `src/components/proc/proc-icon.tsx` | stroke icon set, `currentColor` |
| Brand | `src/lib/brand.ts` (`BRAND_NAME`), `gofer-mark.tsx` | wordmark stays lowercase `gofer` |

## 2. Buyer palette (`.proc-theme`)

Tokens are CSS custom properties; **never hard-code a hex in a component**. Both themes
define the same token set — components are theme-blind.

- Surfaces: `--bg`, `--surface`, `--surface-2`, `--surface-hi`, borders `--border`, `--border-soft`
- Text: `--text`, `--text-strong`, `--muted`, `--muted-2` (all WCAG-AA checked; see inline
  comments in `procurement.css` for the contrast math — keep those comments when retuning)
- Accent: `--accent`, `--accent-text`, `--accent-fill`, `--accent-fill-hi`, `--accent-line`, `--on-accent`
- Status: `--st-open` (amber), `--st-progress` (blue), `--st-done` (green), `--st-overdue`
  (orange-red), each with a `-fill`; `--st-cancel-fill`. **Status is never conveyed by colour
  alone** — pills carry a dot + label, tags carry an icon + label.
- Skeleton: `--sk` + `--sk-sweep`
- Radius: `--r` (3px), `--r-lg` (4px). Pills/chips use `999px`.
- Fonts: `--font-sans` (Mulish) for UI, `--font-serif` (Cormorant italic) for the
  editorial sub-lines only.

### Type scale (buyer)

28 light (page title `dh-title`) · 22–24 light (money `o-num`, `rv-num`) · 15–15.5/600
(card titles) · 13.5/600 (buttons, body-strong) · 12.5 (secondary) · 11.5/600 (pills, tags)
· 10.5–11/700 uppercase +0.8–1.2px tracking (kickers, table headers, section labels).
Numbers always `font-variant-numeric: tabular-nums`.

### Spacing rhythm

Card padding 15–18px; list gaps 10–14px; section header `proc-sec-h` 26px above / 10px
below; page gutter 24–26px. Stick to multiples of these — don't invent new paddings.

## 3. Supplier palette (`.portal-surface`)

Warm off-white, mobile-first (excellent at 360px). Tokens: `--portal-graphite` (text),
`--portal-slate` (secondary), `--portal-paper`, `--portal-card`, `--portal-line[-strong]`,
`--portal-orange` (**non-text accent only** — fails AA as text), `--portal-deep-orange`
(orange that may carry text), `--portal-amber` (fill/border only, always graphite text),
`--portal-success` / `--portal-error` (white-text-safe, CVD-separable). Focus ring:
`--portal-focus` box-shadow. Standing rule (documented in the CSS): every status shows
**shape + label + colour** — never colour alone.

## 4. Component inventory (one set, both palettes)

Buyer (`proc-*`): `proc-btn` (default / `data-kind="primary"|"quiet"`), `proc-btnprimary`,
`proc-iconbtn`, `proc-pill` (5 tones), `o-tag` (evidence badges: exact / equiv / stock /
quote), `proc-act` / `proc-handoff` / `proc-fl` (action & row cards), `proc-opt` (option
card + `rec-band`), `proc-outreach` (dashed status block — deliberately NOT card-styled),
`proc-tblcard` + `proc-tbl` (tables), `proc-form` + `proc-field` (forms), `proc-stepper`
(quantity), `proc-loading` / `sp-steps` (progress checklist), `proc-skel[row|list]`
(skeletons), `proc-empty` / `proc-tblempty` (empty states), `rail-card` (right rail),
`proc-rowic` (row leading icon tile).

Supplier (`portal-*`, `quote-*`): `quote-input` / `quote-field[-row]`, `portal-submit`,
`quote-state-card`, `portal-teaser`, `portal-rejection` / `portal-submitted` state shells,
`portal-brand-*` chips/editor, `portal-soft-error`.

## 5. State rules (every interactive element)

- **hover** — border shifts to `--accent-line` or background to `--surface-2`
- **focus-visible** — 2px `--accent` outline (buyer) / `--portal-focus` shadow (supplier);
  never removed without replacement
- **active** — subtle `scale(0.995)` on cards
- **disabled** — reduced opacity, `cursor: default`, never removed from the DOM
- **loading** — skeletons (`SkelList`) for lists/tables, the Gofer loader for full-screen
  waits; **no raw "Loading…" text**
- **empty** — a designed empty state that says what will appear and offers the next action
- **error** — `ProcErrorNote` with the real reason; never a dead end

## 6. Accessibility floor (brief §1.10 — non-negotiable)

- WCAG-AA contrast on both palettes (the token comments carry the math — keep them true)
- **≥44px touch targets**: the touch-target block at the bottom of `procurement.css`
  expands small controls' hit areas via centered pseudo-elements; padding-based bumps on
  stacked rows (nav, bell). New small controls must join one of those lists.
- Visible focus rings everywhere; semantic landmarks (`nav`, `main`, `section` +
  `aria-labelledby`); status never colour-only; keyboard-complete (no traps).

## 7. How to extend

1. Reuse a listed component; if none fits, build the new one **from tokens** in the
   appropriate stylesheet — no inline hexes, no per-screen font sizes.
2. Both payload shapes and both themes must render before a component ships.
3. Honesty invariants (FRONTEND_REWORK_BRIEF §1) are product law: sellers named, evidence
   linked, no fabricated numbers, three price states, findings ≠ outreach, honest empties.
4. New heavyweight dependencies need justification in the build report. Prefer CSS +
   the existing icon set.
