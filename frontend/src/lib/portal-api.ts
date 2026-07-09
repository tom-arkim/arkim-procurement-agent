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
