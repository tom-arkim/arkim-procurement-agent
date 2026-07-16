/**
 * Quote-submission API client — the PUBLIC, unauthenticated surface
 * (QUOTE_SUBMIT_V1, Night 11 path A).
 *
 * Mirrors lib/portal-api.ts deliberately (the settled public-surface posture):
 *  - Calls ONLY `/api/quote/{token}` (never an admin endpoint, never the
 *    internal client's session header).
 *  - The token stays in the URL; passed in by the page, NEVER stored
 *    (no localStorage/sessionStorage/cookie), never logged, never sent to a
 *    third party.
 *  - Uniform rejection: any non-200 that isn't the explicit closed-state 409
 *    renders ONE generic "link no longer valid" / soft-error state. The page
 *    can't branch on which failure occurred (unknown token / flag-off /
 *    network are all indistinguishable — the backend's uniform 404).
 *  - The CLOSED state is NOT a rejection: a known token whose RFQ has closed
 *    renders an honest "this request has closed" (GET returns 200
 *    {state:"closed"}; POST returns 409). Knowing a real token already proves
 *    receipt of the RFQ email, so this leaks nothing to an enumerator.
 */

// ---------------------------------------------------------------------------
// Types — mirror the live backend shapes (api_server.py Night 11 section)
// ---------------------------------------------------------------------------

export interface QuoteRequestContext {
  manufacturer: string | null;
  part_number: string | null;
  quantity: number | null;
  need_by: string | null;
}

export interface QuoteContext {
  state: "live" | "closed";
  request?: QuoteRequestContext;
  supplier?: { name: string | null; domain: string };
  expires_at?: string;
  existing_quote?: {
    status: "active" | "review";
    unit_price: number;
    submitted_at: string;
  } | null;
}

export interface QuoteSubmissionBody {
  quote_number: string;
  unit_price: number;
  quantity: number;
  lead_time: string;
  part_number?: string | null;
  freight?: string | null;
  valid_until?: string | null;
  notes?: string | null;
}

export interface QuoteSubmitResponse {
  ok: boolean;
  quote_id: string;
  status: "active" | "review";
  review_reasons: string[];
  pn_differs: boolean;
  valid_until: string;
  claim_pitch: boolean;
}

/**
 * Outcome model: `ok` carries data; `closed` is the honest dead-RFQ state
 * (not an error); `rejected` is the single uniform failure state for
 * everything else (invalid token / flag-off / 422 / network alike).
 */
export type QuoteResult<T> =
  | { ok: true; data: T }
  | { ok: false; closed: true }
  | { ok: false; rejected: true };

// ---------------------------------------------------------------------------
// Fetch wrapper — the only network primitive the public quote page uses.
// ---------------------------------------------------------------------------

async function quoteFetch<T>(
  path: string,
  init?: RequestInit,
): Promise<QuoteResult<T>> {
  try {
    const res = await fetch(`/api${path}`, {
      cache: "no-store",
      // No session header, no auth. The token IS the credential, in the URL.
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
      ...init,
    });
    if (res.status === 409) return { ok: false, closed: true };
    if (!res.ok) return { ok: false, rejected: true };
    const data = (await res.json()) as T;
    return { ok: true, data };
  } catch {
    // Network failure, parse failure — uniform rejection.
    return { ok: false, rejected: true };
  }
}

// ---------------------------------------------------------------------------
// Public quote endpoints
// ---------------------------------------------------------------------------

/** GET /api/quote/{token} — the form context (request identity + prefills). */
export function getQuoteContext(token: string): Promise<QuoteResult<QuoteContext>> {
  return quoteFetch<QuoteContext>(`/quote/${encodeURIComponent(token)}`);
}

/** POST /api/quote/{token} — submit (or revise) the structured quote. */
export function submitQuote(
  token: string,
  body: QuoteSubmissionBody,
): Promise<QuoteResult<QuoteSubmitResponse>> {
  return quoteFetch<QuoteSubmitResponse>(`/quote/${encodeURIComponent(token)}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
