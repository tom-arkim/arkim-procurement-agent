import { QueryClient } from "@tanstack/react-query";

// Singleton across SSR / client renders. Keeps the cache stable during hot
// reloads in development without leaking across requests in production.
let queryClient: QueryClient | undefined;

export function getQueryClient(): QueryClient {
  if (typeof window === "undefined") {
    // Server: always create a new client to avoid cross-request state pollution.
    return makeQueryClient();
  }
  // Browser: create once and reuse.
  if (!queryClient) queryClient = makeQueryClient();
  return queryClient;
}

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // 30s stale time — sourcing run state doesn't change faster than this.
        staleTime: 30_000,
        // 5m cache time after all consumers unmount.
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => {
          // Don't retry 4xx errors (bad request, not found, forbidden).
          if (error instanceof ApiError && error.status < 500) return false;
          return failureCount < 2;
        },
      },
      mutations: {
        // Show mutations as pending for at least 300ms so fast responses
        // don't flash the UI.
        networkMode: "always",
      },
    },
  });
}

// ---------------------------------------------------------------------------
// Query key factory — typed, co-located with the client
// ---------------------------------------------------------------------------
export const queryKeys = {
  runs: {
    all: () => ["runs"] as const,
    list: (params?: { facilityId?: string; phase?: string }) =>
      ["runs", "list", params ?? {}] as const,
    detail: (runId: string) => ["runs", "detail", runId] as const,
  },
  facilities: {
    all: () => ["facilities"] as const,
  },
  groups: {
    detail: (groupId: string) => ["groups", "detail", groupId] as const,
  },
  approvalRules: {
    byFacility: (facilityId: string) => ["approval-rules", facilityId] as const,
  },
  reviewItems: {
    byRun: (runId: string) => ["review-items", runId] as const,
  },
  orders: {
    byRun: (runId: string) => ["orders", runId] as const,
    all: () => ["orders", "all"] as const,
  },
  impact: {
    cumulative: () => ["impact", "cumulative"] as const,
  },
  reorder: {
    all: () => ["reorder"] as const,
  },
  events: {
    all: () => ["events"] as const,
  },
  siteShipTo: {
    bySite: (siteId: string) => ["site-shipto", siteId] as const,
  },
} as const;

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly statusText: string,
    public readonly body?: unknown,
  ) {
    super(`${status} ${statusText}`);
    this.name = "ApiError";
  }
}

/**
 * Human-readable reason for a failed API call, or `null` for a pure network/connectivity
 * failure (no response). Lets the UI distinguish a real server error (B/C — show the
 * backend's `detail`) from "can't reach the backend" (A — the only case where "is the
 * backend running?" is the right message), instead of masking everything as a catch-all.
 */
export function apiErrorMessage(err: unknown): string | null {
  if (err instanceof ApiError) {
    const detail = (err.body as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === "string" && detail.trim()) return detail;          // FastAPI {"detail": "..."}
    if (Array.isArray(detail) && detail.length) {                            // 422 validation array
      const msg = (detail[0] as { msg?: string })?.msg;
      return msg ? `Invalid request: ${msg}` : `Request rejected (${err.status}).`;
    }
    return `Request failed (${err.status}).`;                                // a status, no usable detail
  }
  return null;                                                               // no response -> connectivity
}
