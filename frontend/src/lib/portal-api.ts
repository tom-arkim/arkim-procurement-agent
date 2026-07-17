/**
 * Supplier claim-portal API client — the PUBLIC, unauthenticated surface.
 *
 * DELIBERATELY SEPARATE from the internal-app client (src/lib/api.ts). The
 * internal client attaches an X-Session-Id (DEMO_MODE isolation) and hits the
 * full /api/* surface. The portal must do NONE of that:
 *
 *  - It calls ONLY `/api/portal/{token}/*` (never an admin endpoint).
 *  - It sends NO session header, NO auth cookie, NO persistent session.
 *  - The token stays in the URL; it is passed into these functions by the page
 *    and NEVER stored (no localStorage/sessionStorage/cookie), never logged,
 *    never sent to any third party. See claim-page security notes.
 *  - On ANY non-200 the page renders a uniform rejection — this client surfaces
 *    only "ok" vs "rejected" so the UI can't branch on which failure occurred
 *    (invalid / expired / reused / flag-off all return 404 from the backend).
 *
 * Routed through the same Next.js rewrite proxy (/api/* -> FastAPI) as the
 * internal client, so no new network path is introduced.
 */

// ---------------------------------------------------------------------------
// Types — mirror the live-verified backend shapes (api_server.py / supplier_portal.py)
// ---------------------------------------------------------------------------

export type BrandRelationship =
  | "AUTHORIZED"
  | "CARRIES"
  | "AFTERMARKET_COMPATIBLE";

export interface PortalBrand {
  brand_id: string;
  relationship: BrandRelationship | null;
  evidence?: string | null;
  classes_for_brand?: Array<{ class_id?: string } | Record<string, unknown>>;
}

export interface PortalClass {
  class_id: string;
  is_core: boolean;
  subtype?: string | null;
}

export type ShipArea =
  | { kind: "NATIONWIDE_US" }
  | { kind: "STATES"; states: string[] }
  | null;

export interface DemandTeaser {
  has_matches: boolean;
  count: number;
  window_days: number;
  framing: string | null;
}

export interface PortalProfile {
  teaser: DemandTeaser;
  supplier_domain: string;
  name: string | null;
  brands: PortalBrand[];
  classes: PortalClass[];
  ship_area: ShipArea;
  aftermarket_disclosure: string | null;
}

export interface ProposeRevisionResponse {
  ok: boolean;
  revision_id: string;
  status: "pending";
}

// ---------------------------------------------------------------------------
// Fetch wrapper — the only network primitive the public page uses.
// ---------------------------------------------------------------------------

/**
 * Outcome model: the page never sees a status code or a distinguishable error.
 * `rejected` is the single safe state for invalid / expired / reused / flag-off
 * / network failure alike (uniform rejection — no oracle). `ok` carries data.
 */
export type PortalResult<T> =
  | { ok: true; data: T }
  | { ok: false; rejected: true };

/**
 * Fetch a portal resource. Returns a uniform `rejected` on ANY non-200 (the
 * backend gives away nothing about which failure occurred, and neither do we).
 * The token is used in the URL only and never stored/logged by this module.
 */
async function portalFetch<T>(path: string, init?: RequestInit): Promise<PortalResult<T>> {
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
    if (!res.ok) return { ok: false, rejected: true };
    const data = (await res.json()) as T;
    return { ok: true, data };
  } catch {
    // Network failure, parse failure, or a thrown response — all uniform.
    return { ok: false, rejected: true };
  }
}

// ---------------------------------------------------------------------------
// Public portal endpoints
// ---------------------------------------------------------------------------

/** GET /api/portal/{token}/profile — the editable supplier profile + demand teaser. */
export function getPortalProfile(token: string): Promise<PortalResult<PortalProfile>> {
  return portalFetch<PortalProfile>(`/portal/${encodeURIComponent(token)}/profile`);
}

export interface ProposeRevisionBody {
  brands?: Array<{ brand_id: string; relationship: BrandRelationship }>;
  classes?: Array<{ class_id: string; is_core: boolean }>;
  ship_area?: ShipArea;
}

/**
 * POST /api/portal/{token}/propose-revision — a supplier-proposed edit lands as
 * a PENDING revision (never writes the registry). Returns `rejected` on any
 * non-200 (incl. 422 malformed relationship), so the page preserves input and
 * shows the soft-error state rather than branching on the failure kind.
 */
export function proposeRevision(
  token: string,
  body: ProposeRevisionBody,
): Promise<PortalResult<ProposeRevisionResponse>> {
  return portalFetch<ProposeRevisionResponse>(
    `/portal/${encodeURIComponent(token)}/propose-revision`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

// ---------------------------------------------------------------------------
// Night 11 (QUOTE_SUBMIT_V1) — path B: open requests, quote history, submit.
// All three 404 (→ uniform `rejected`) when the quote feature is off, so the
// portal page simply hides the sections — no flag plumbing in the UI.
// ---------------------------------------------------------------------------

export interface OpenRequest {
  run_id: string;
  manufacturer: string | null;
  part_number: string | null;
  quantity: number | null;
  sent_at: string | null;
  quoted: {
    status: "active" | "review";
    unit_price: number;
    submitted_at: string;
  } | null;
}

export interface QuoteHistoryRow {
  quote_id: string;
  run_id: string | null;
  part_number: string | null;
  quoted_part_number: string | null;
  unit_price: number;
  quantity: number | null;
  lead_time: string | null;
  status: string; // effective status: active|review|superseded|expired|withdrawn
  submitted_at: string;
  submitted_via: string;
  valid_until: string | null;
}

export interface PortalQuoteSubmitResponse {
  ok: boolean;
  quote_id: string;
  status: "active" | "review";
  review_reasons: string[];
  pn_differs: boolean;
  valid_until: string;
}

/** GET /api/portal/{token}/open-requests — the supplier's own open RFQs. */
export function getOpenRequests(
  token: string,
): Promise<PortalResult<{ requests: OpenRequest[] }>> {
  return portalFetch<{ requests: OpenRequest[] }>(
    `/portal/${encodeURIComponent(token)}/open-requests`,
  );
}

/** GET /api/portal/{token}/quotes — the supplier's own quote history. */
export function getQuoteHistory(
  token: string,
): Promise<PortalResult<{ quotes: QuoteHistoryRow[] }>> {
  return portalFetch<{ quotes: QuoteHistoryRow[] }>(
    `/portal/${encodeURIComponent(token)}/quotes`,
  );
}

export interface PortalQuoteBody {
  run_id: string;
  quote_number: string;
  unit_price: number;
  quantity: number;
  lead_time: string;
  part_number?: string | null;
  freight?: string | null;
  valid_until?: string | null;
  notes?: string | null;
}

/** POST /api/portal/{token}/quotes — submit a quote on one open request. */
export function submitPortalQuote(
  token: string,
  body: PortalQuoteBody,
): Promise<PortalResult<PortalQuoteSubmitResponse>> {
  return portalFetch<PortalQuoteSubmitResponse>(
    `/portal/${encodeURIComponent(token)}/quotes`,
    { method: "POST", body: JSON.stringify(body) },
  );
}
