/**
 * Supplier claim-portal — PUBLIC route `/portal/[token]`.
 *
 * The app's FIRST public, unauthenticated, customer-facing page. The token is
 * the credential and lives in the URL. Security posture is the highest-risk
 * part of this build; see the security notes below and in lib/portal-api.ts.
 *
 * Demand is the hero: the teaser is the first, most prominent element. Then a
 * low-friction single-pass profile-confirm form (brands / classes / ship-area),
 * with the tri-state brand relationship as the centerpiece.
 *
 * SECURITY (holds for the whole page):
 *  - The token is read from the route param and held in a closure/local state
 *    for the page lifetime only. It is NEVER written to localStorage /
 *    sessionStorage / a cookie, NEVER sent to a third party (no analytics, no
 *    error reporting beacons), and NEVER logged (no console.* with the token).
 *  - Referrer-Policy: no-referrer is set (via <meta> in the page) so an
 *    outbound click can't leak the token via the Referer header. The API also
 *    sets it server-side; we set it here too, defense-in-depth.
 *  - The page calls ONLY /api/portal/{token}/* (lib/portal-api.ts). It can
 *    reach no admin endpoint and exposes no admin data.
 *  - Uniform rejection: on any non-200 (invalid / expired / reused / flag-off /
 *    network), the page renders ONE generic "link no longer valid" state. It
 *    never surfaces a status code or distinguishes the failure kind.
 */
import { Suspense } from "react";
import { ClaimPage } from "./claim-page";

export const dynamic = "force-dynamic";

export const metadata = {
  title: "Confirm your supplier profile",
  description: "Confirm your brands, classes, and ship area so buyers can match you.",
  // Defense-in-depth: the page-level meta is supplemented by an explicit tag in
  // the body too (see claim-page). The API also sets this header on responses.
  referrer: "no-referrer",
};

export default function PortalTokenPage({ params }: { params: { token: string } }) {
  return (
    <Suspense fallback={null}>
      <ClaimPage token={params.token} />
    </Suspense>
  );
}
