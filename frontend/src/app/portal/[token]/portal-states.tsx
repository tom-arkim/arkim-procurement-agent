"use client";

/**
 * Portal state shells: the demand teaser (hero), the uniform rejection, and the
 * "submitted for review" confirmation. Kept separate from the form so the state
 * machine in claim-page.tsx reads clearly.
 *
 * Honesty carve-out (non-negotiable): zero-state renders the framing text as the
 * hero — NEVER a "0 matches" number, NEVER a fabricated/placeholder count, NEVER
 * demo data. has_matches renders the real count + window.
 *
 * Status redundancy (WCAG 1.4.1 — color is never the sole signal): every status
 * indicator below carries a SHAPE (glyph) + a TEXT LABEL + color. The glyphs are
 * inline SVG (no new dependency) drawn in graphite — contrast-safe on the card
 * — while the status color is carried by the card border/accent. So the
 * deuteranopia/protanopia color collisions can never cause a misread: even with
 * color removed, the glyph + the words still identify the state.
 */

import { BRAND_NAME } from "@/lib/brand";
import type { DemandTeaser } from "@/lib/portal-api";

// ---------------------------------------------------------------------------
// Status glyphs — inline SVG, currentColor set to graphite (contrast-safe).
// These are the SHAPE half of the shape+label+color status rule. The status
// COLOR is carried separately by the card border, not by the glyph, so an amber
// glyph (which would fail the 3:1 non-text threshold on white) is never needed.
// ---------------------------------------------------------------------------

function ClockGlyph({ label }: { label: string }) {
  // A clock reads as "pending / waiting" — the right semantics for "submitted
  // for review" (NOT a checkmark, which would imply done/saved).
  return (
    <svg
      className="portal-status-glyph"
      viewBox="0 0 24 24"
      width="28"
      height="28"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      role="img"
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </svg>
  );
}

function WarningGlyph({ label }: { label: string }) {
  return (
    <svg
      className="portal-status-glyph"
      viewBox="0 0 24 24"
      width="28"
      height="28"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      role="img"
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      <path d="M10.3 3.9 2.4 17.6a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9v4" />
      <path d="M12 17h.01" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Demand teaser — the hero (first, most prominent element)
// ---------------------------------------------------------------------------

export function PortalTeaser({ teaser }: { teaser: DemandTeaser }) {
  if (teaser.has_matches) {
    // Real demand — the count is genuine (backend counts real buyer-match
    // events only; never seeded/demo/synthetic). The number is the hero.
    return (
      <section className="portal-teaser portal-teaser--has-matches" aria-labelledby="teaser-h">
        <p className="portal-teaser-eyebrow" id="teaser-h">
          Buyer demand
        </p>
        <p className="portal-teaser-hero">
          <span className="portal-teaser-count">{teaser.count}</span>{" "}
          buyer{teaser.count === 1 ? "" : "s"} matched your categories in the last{" "}
          {teaser.window_days} days
        </p>
        <p className="portal-teaser-sub">
          Confirm your profile so {BRAND_NAME} keeps matching you to the right requests.
        </p>
      </section>
    );
  }

  // Zero-state: framing text is the hero. NO number, NO "0 matches".
  // (has-matches vs zero-state is already distinguished by text content — a
  // non-color cue would add noise, so none is added per the brief.)
  return (
    <section className="portal-teaser portal-teaser--zero" aria-labelledby="teaser-h">
      <p className="portal-teaser-eyebrow" id="teaser-h">
        Buyer demand
      </p>
      <p className="portal-teaser-hero portal-teaser-framing">{teaser.framing}</p>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Uniform rejection — identical for invalid / expired / reused / flag-off.
// Never reveals which failure occurred (no oracle).
//
// Status redundancy: warning-triangle glyph (shape) + "This link is no longer
// valid" (text) — color is intentionally neutral here (no status hue), so the
// state is identifiable by shape + words alone, not by any color a CVD user
// could misread.
// ---------------------------------------------------------------------------

export function PortalRejection() {
  return (
    <div className="portal-surface portal-rejection">
      <div className="portal-rejection-card">
        <StatusGlyphWrap>
          <WarningGlyph label="Warning" />
        </StatusGlyphWrap>
        <h1 className="portal-rejection-title">This link is no longer valid</h1>
        <p className="portal-rejection-body">
          The claim link may have expired or already been used. Contact your {BRAND_NAME}{" "}
          representative for a new one.
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Submitted — amber "pending" tone, NOT green "saved". Nothing is live until
// the concierge approves the revision.
//
// Status redundancy: clock glyph (shape — "pending", not "done") + "Submitted
// for review" (text) + amber card border (color). Color alone never identifies
// the state; a CVD user who can't separate the amber border still reads the
// clock + the words.
// ---------------------------------------------------------------------------

export function PortalSubmitted() {
  return (
    <div className="portal-surface portal-submitted">
      <div className="portal-submitted-card">
        <StatusGlyphWrap>
          <ClockGlyph label="Pending" />
        </StatusGlyphWrap>
        <p className="portal-submitted-eyebrow">Submitted for review</p>
        <h1 className="portal-submitted-title">Thanks — your rep will review and confirm these changes</h1>
        <p className="portal-submitted-body">
          Your edits are pending. Nothing changes on your profile until your {BRAND_NAME}{" "}
          representative approves them. We&apos;ll be in touch if anything needs clarifying.
        </p>
      </div>
    </div>
  );
}

// A small wrapper that centers the glyph above the status text. Shared by both
// status cards so the shape+label+color layout stays consistent.
function StatusGlyphWrap({ children }: { children: React.ReactNode }) {
  return <div className="portal-status-glyph-wrap">{children}</div>;
}
