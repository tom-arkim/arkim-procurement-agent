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
