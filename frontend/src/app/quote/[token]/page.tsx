/**
 * Supplier quote submission — PUBLIC route `/quote/[token]` (QUOTE_SUBMIT_V1).
 *
 * The path-A surface of the Night-11 quote loop: the RFQ email carries a
 * per-RFQ tokenized link to this page; the supplier — claimed or unclaimed —
 * submits a structured quote in five fields with NO account, NO login, NO
 * password (quoting is unconditional; signup is the upgrade, pitched only
 * AFTER submission).
 *
 * SECURITY (the claim-portal posture, re-applied — see portal/[token]/page.tsx):
 *  - The token is the credential, lives in the URL, is held in a closure for
 *    the page lifetime only — NEVER localStorage/sessionStorage/cookie, never
 *    logged, never sent to a third party.
 *  - Referrer-Policy: no-referrer via the metadata export; the API sets it
 *    server-side on every quote response too.
 *  - The page calls ONLY /api/quote/{token} (lib/quote-api.ts) — no admin
 *    endpoint, no internal session.
 *  - Uniform rejection on any indistinguishable failure; the CLOSED state
 *    (dead RFQ) renders honestly and separately — it is a state, not an error.
 */
import { Suspense, use } from "react";
import { QuotePage } from "./quote-page";

export const dynamic = "force-dynamic";

export const metadata = {
  title: "Submit your quote",
  description: "Submit your quote for the requested part in five fields.",
  referrer: "no-referrer",
};

export default function QuoteTokenPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = use(params);
  return (
    <Suspense fallback={null}>
      <QuotePage token={token} />
    </Suspense>
  );
}
