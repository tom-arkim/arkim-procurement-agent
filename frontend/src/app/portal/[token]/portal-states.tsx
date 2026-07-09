"use client";

/**
 * Portal state shells: the demand teaser (hero), the uniform rejection, and the
 * "submitted for review" confirmation. Kept separate from the form so the state
 * machine in claim-page.tsx reads clearly.
 *
 * Honesty carve-out (non-negotiable): zero-state renders the framing text as the
 * hero — NEVER a "0 matches" number, NEVER a fabricated/placeholder count, NEVER
 * demo data. has_matches renders the real count + window.
 */

import { BRAND_NAME } from "@/lib/brand";
import type { DemandTeaser } from "@/lib/portal-api";

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
// ---------------------------------------------------------------------------

export function PortalRejection() {
  return (
    <div className="portal-surface portal-rejection">
      <meta name="referrer" content="no-referrer" />
      <div className="portal-rejection-card">
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
// ---------------------------------------------------------------------------

export function PortalSubmitted() {
  return (
    <div className="portal-surface portal-submitted">
      <meta name="referrer" content="no-referrer" />
      <div className="portal-submitted-card">
        <p className="portal-submitted-eyebrow">Submitted for review</p>
        <h1 className="portal-submitted-title">Thanks — your rep will review and confirm these changes</h1>
        <p className="portal-submitted-body">
          Your edits are pending. Nothing changes on your profile until your {BRAND_NAME}{" "}
          representative approves them. We'll be in touch if anything needs clarifying.
        </p>
      </div>
    </div>
  );
}
