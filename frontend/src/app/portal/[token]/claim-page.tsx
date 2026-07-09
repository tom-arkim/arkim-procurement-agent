"use client";

/**
 * ClaimPage — the interactive body of the supplier claim-portal.
 *
 * State machine (all six required states):
 *  loading       — branded spinner (GoferLoader) while the profile fetches.
 *  has-matches   — teaser with count/window as the hero, then the profile form.
 *  zero-state    — framing text as the hero (NO "0", NO fabricated count), then form.
 *  rejection     — uniform "link no longer valid" page (invalid/expired/reused/flag-off).
 *  submitted     — amber "submitted for review" confirmation (NOT green "saved").
 *  soft-error    — submit failed; input PRESERVED, an honest retry message shown.
 *
 * The token is the credential and stays in the URL. It is passed in as a prop,
 * kept in a ref/local state for the page lifetime, and never persisted/logged/
 * sent to a third party. See page.tsx security notes.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { BRAND_NAME } from "@/lib/brand";
import {
  getPortalProfile,
  proposeRevision,
  type BrandRelationship,
  type PortalProfile,
  type PortalResult,
  type ProposeRevisionBody,
  type ShipArea,
} from "@/lib/portal-api";
import { ProfileForm, type FormState } from "./profile-form";
import { PortalTeaser, PortalRejection, PortalSubmitted } from "./portal-states";
import { GoferLoader } from "@/components/ui/gofer-loader";

type Phase =
  | "loading"
  | "ready" // has profile data (has-matches OR zero-state — both render the form)
  | "rejected"
  | "submitted"
  | "soft-error";

export function ClaimPage({ token }: { token: string }) {
  // The token is held in a ref so its identity is stable for the page lifetime
  // and it is never echoed into state that could be logged/persisted. We do NOT
  // store it anywhere outside this closure.
  const tokenRef = useRef(token);
  const [phase, setPhase] = useState<Phase>("loading");
  const [profile, setProfile] = useState<PortalProfile | null>(null);
  // Form state lives here so it survives the soft-error transition (input
  // preserved across a failed submit — the supplier never re-enters).
  const [form, setForm] = useState<FormState | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState<boolean>(false);
  // Detect first paint so we don't flash the rejection page during SSR/hydration.
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  // --- Initial profile fetch -------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    setPhase("loading");
    getPortalProfile(tokenRef.current).then((result: PortalResult<PortalProfile>) => {
      if (cancelled) return;
      if (result.ok) {
        setProfile(result.data);
        setForm(profileToForm(result.data));
        setPhase("ready");
      } else {
        // Uniform rejection — no branching on the failure kind.
        setProfile(null);
        setForm(null);
        setPhase("rejected");
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // --- Submit for review -----------------------------------------------------
  const handleSubmit = useCallback(
    async (nextForm: FormState) => {
      // Keep the latest input regardless of outcome so a failed submit never
      // loses the supplier's edits.
      setForm(nextForm);
      setSubmitError(null);
      setSubmitting(true);
      const body: ProposeRevisionBody = formToRevision(nextForm);
      const result = await proposeRevision(tokenRef.current, body);
      setSubmitting(false);
      if (result.ok) {
        setPhase("submitted");
      } else {
        // Uniform soft-error: never reveal whether it was 422/404/network.
        setSubmitError(
          "We couldn't submit your changes right now. Your edits are kept — please try again in a moment.",
        );
        setPhase("soft-error");
      }
    },
    [],
  );

  // --- Render ----------------------------------------------------------------
  // Before mount (SSR): render a quiet placeholder. The real state is decided
  // client-side after the fetch — never flash the rejection page on first paint.
  if (!mounted || phase === "loading") {
    return <PortalLoading />;
  }

  if (phase === "rejected") {
    return <PortalRejection />;
  }

  if (phase === "submitted") {
    return <PortalSubmitted />;
  }

  // ready OR soft-error: render the hero + form. soft-error re-shows the form
  // with preserved input and the honest retry message above it.
  if (profile && form) {
    return (
      <PortalLayout supplierName={profile.name} domain={profile.supplier_domain}>
        <PortalTeaser teaser={profile.teaser} />
        {phase === "soft-error" && submitError && (
          <div
            className="portal-soft-error"
            role="alert"
            aria-live="polite"
          >
            {submitError}
          </div>
        )}
        <ProfileForm
          initial={form}
          aftermarketDisclosure={profile.aftermarket_disclosure}
          onChange={setForm}
          onSubmit={handleSubmit}
          submitting={submitting}
        />
      </PortalLayout>
    );
  }

  // Defensive: if we reach here without data, fall back to loading rather than
  // rendering a blank or fabricated state.
  return <PortalLoading />;
}

// ---------------------------------------------------------------------------
// Layout + simple state shells
// ---------------------------------------------------------------------------

function PortalLayout({
  supplierName,
  domain,
  children,
}: {
  supplierName: string | null;
  domain: string;
  children: React.ReactNode;
}) {
  return (
    <div className="portal-surface">
      {/* Referrer-Policy: no-referrer is set once, canonically, via the page
          metadata export in page.tsx (metadata.referrer). No inline <meta> here
          — it was technically-invalid redundancy in the body. The no-referrer
          behavior is preserved; the API also sets it server-side. */}
      <header className="portal-header">
        <span className="portal-brand">{BRAND_NAME}</span>
        <span className="portal-supplier" title={domain}>
          {supplierName || domain}
        </span>
      </header>
      <main className="portal-main">{children}</main>
      <footer className="portal-footer">
        {BRAND_NAME} never asks for payment or credentials over chat.
      </footer>
    </div>
  );
}

function PortalLoading() {
  return (
    <div className="portal-surface portal-loading">
      <GoferLoader size={120} aria-label="Loading your supplier profile" />
      <p className="portal-loading-text">Loading your profile…</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// mappers: backend profile <-> form state
// ---------------------------------------------------------------------------

export function profileToForm(p: PortalProfile): FormState {
  return {
    brands: p.brands.map((b) => ({
      brand_id: b.brand_id,
      relationship: (b.relationship ?? "CARRIES") as BrandRelationship,
    })),
    classes: p.classes.map((c) => ({ class_id: c.class_id, is_core: c.is_core })),
    shipArea: p.ship_area ?? { kind: "NATIONWIDE_US" },
  };
}

export function formToRevision(f: FormState): ProposeRevisionBody {
  return {
    brands: f.brands.map((b) => ({ brand_id: b.brand_id, relationship: b.relationship })),
    classes: f.classes.map((c) => ({ class_id: c.class_id, is_core: c.is_core })),
    ship_area: f.shipArea,
  };
}

// Re-export the ShipArea type for the form module (keeps imports tidy).
export type { ShipArea };
