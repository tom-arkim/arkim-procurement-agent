/**
 * Arkim API client
 *
 * All requests go through the Next.js rewrite proxy at /api/* → FastAPI :8001.
 * Each function returns typed data or throws ApiError on non-2xx.
 */

import { ApiError } from "./query-client";
import type { ShipTo } from "./proc-config";

// When NEXT_PUBLIC_API_URL is set (e.g. http://localhost:8000) requests go
// directly to the FastAPI backend. When unset, the Next.js rewrite proxy at
// /api/* handles routing — useful for SSR contexts that can't reach localhost.
const API_BASE =
  typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_URL
    ? process.env.NEXT_PUBLIC_API_URL.replace(/\/$/, "")
    : "";

import type {
  ApproveRequest,
  ConfirmReviewItemResponse,
  CreateRunRequest,
  CreateRunResponse,
  CumulativeImpact,
  Facility,
  ReorderItem,
  ApprovalRule,
  Order,
  OrderActionResult,
  OrdersResponse,
  OutreachRequest,
  ProcessRepliesResponse,
  RejectRequest,
  RejectReviewItemResponse,
  ReviewItemsResponse,
  SelectCandidateRequest,
  SendMessageRequest,
  SendMessageResponse,
  SourcingRunDetail,
  SourcingRunListItem,
} from "@/types";

// ---------------------------------------------------------------------------
// Base fetch wrapper
// ---------------------------------------------------------------------------

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const res = await fetch(`${API_BASE}/api${path}`, {
    // Never serve API responses from the browser HTTP cache. Without this, a polled
    // GET to the same URL (e.g. useRunLive on /api/runs/{id}) can be answered from
    // cache and never observe the run advancing sourcing -> comparison — the run
    // stays stuck on "finding your best options" until a full browser reload.
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
    ...options,
  });

  if (!res.ok) {
    let body: unknown;
    try {
      body = await res.json();
    } catch {
      body = await res.text();
    }
    throw new ApiError(res.status, res.statusText, body);
  }

  // 204 No Content
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Sourcing runs
// ---------------------------------------------------------------------------

export async function createRun(body: CreateRunRequest): Promise<CreateRunResponse> {
  return request("/runs", { method: "POST", body: JSON.stringify(body) });
}

export async function listRuns(params?: {
  facilityId?: string;
  phase?: string;
  limit?: number;
  offset?: number;
}): Promise<SourcingRunListItem[]> {
  const qs = new URLSearchParams();
  if (params?.facilityId) qs.set("facility_id", params.facilityId);
  if (params?.phase) qs.set("phase", params.phase);
  if (params?.limit != null) qs.set("limit", String(params.limit));
  if (params?.offset != null) qs.set("offset", String(params.offset));
  const query = qs.toString() ? `?${qs}` : "";
  return request(`/runs${query}`);
}

export async function getRun(runId: string): Promise<SourcingRunDetail> {
  return request(`/runs/${runId}`);
}

export async function sendMessage(
  runId: string,
  body: SendMessageRequest,
): Promise<SendMessageResponse> {
  return request(`/runs/${runId}/messages`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function uploadNameplate(
  runId: string,
  file: File,
): Promise<{
  run_id: string;
  filename: string;
  size_bytes: number;
  message?: { id: string; role: string; content: string; created_at: string };
  extraction: unknown;
}> {
  const form = new FormData();
  form.append("file", file);
  // Don't set Content-Type header — browser sets it with boundary for multipart.
  return request(`/runs/${runId}/upload`, {
    method: "POST",
    headers: {},
    body: form,
  });
}

export async function selectCandidate(
  runId: string,
  body: SelectCandidateRequest,
): Promise<{ run_id: string; phase: string }> {
  return request(`/runs/${runId}/select-candidate`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function approveRun(
  runId: string,
  body: ApproveRequest,
): Promise<{ run_id: string; phase: string }> {
  return request(`/runs/${runId}/approve`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function rejectRun(
  runId: string,
  body: RejectRequest,
): Promise<{ run_id: string; phase: string }> {
  return request(`/runs/${runId}/reject`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function confirmIntake(
  runId: string,
  exactOnly = false,
): Promise<{ run_id: string; phase: string }> {
  const qs = exactOnly ? "?exact_only=true" : "";
  return request(`/runs/${runId}/confirm-intake${qs}`, { method: "POST" });
}

export async function openFromPending(
  runId: string,
): Promise<{ run_id: string; phase: string }> {
  return request(`/runs/${runId}/open-from-pending`, { method: "POST" });
}

export async function rejectSubmission(
  runId: string,
): Promise<{ run_id: string; phase: string }> {
  return request(`/runs/${runId}/reject-submission`, { method: "POST" });
}

/** "Order" / "Order through Arkim" on any PRICED candidate: buying ⇒ selecting + routes
 *  through approval. Server reconstructs the candidate + price authoritatively and
 *  re-derives the channel (no client price/channel). pending_approval=true → at/above
 *  threshold (no order yet); false → sub-threshold (order in pending_manual_fulfilment). */
export async function createOrderNow(
  runId: string,
  body: { candidate_id: string; tier: number; quantity?: number },
): Promise<{ pending_approval: boolean; order: Order | null; phase: string }> {
  return request(`/runs/${runId}/order-now`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function initiateOutreach(
  runId: string,
  body: OutreachRequest,
): Promise<{ run_id: string; candidates_contacted: number; sent_at: string; tier3_outreach_sent: Record<string, string> }> {
  return request(`/runs/${runId}/outreach`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function requestConfirmation(
  runId: string,
  body: { candidate_ids: string[] },
): Promise<{ run_id: string; candidates: string[]; mock_response_in: string }> {
  return request(`/runs/${runId}/request-confirmation`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function saveOutreachSelection(
  runId: string,
  candidateIds: string[],
): Promise<{ run_id: string; saved_count: number; phase: string }> {
  return request(`/runs/${runId}/save-outreach`, {
    method: "POST",
    body: JSON.stringify({ candidate_ids: candidateIds }),
  });
}

// ---------------------------------------------------------------------------
// Buyer loop — inbound quote review (comparison table)
// ---------------------------------------------------------------------------

export async function getReviewItems(runId: string): Promise<ReviewItemsResponse> {
  return request(`/runs/${runId}/review-items`);
}

export async function processReplies(runId: string): Promise<ProcessRepliesResponse> {
  return request(`/runs/${runId}/process-replies`, { method: "POST" });
}

export async function confirmReviewItem(itemId: string): Promise<ConfirmReviewItemResponse> {
  return request(`/review-items/${itemId}/confirm`, { method: "POST" });
}

export async function rejectReviewItem(itemId: string): Promise<RejectReviewItemResponse> {
  return request(`/review-items/${itemId}/reject`, { method: "POST" });
}

/** Place a durable order directly from a CONFIRMED quote (RFQ path). */
export async function placeOrderFromQuote(itemId: string): Promise<OrderActionResult> {
  return request(`/review-items/${itemId}/place-order`, { method: "POST" });
}

// ---------------------------------------------------------------------------
// Buyer loop — order placement + tracking
// ---------------------------------------------------------------------------

export async function executeOrder(runId: string): Promise<OrderActionResult> {
  return request(`/runs/${runId}/execute`, { method: "POST" });
}

export async function markDelivered(runId: string): Promise<OrderActionResult> {
  return request(`/runs/${runId}/mark-delivered`, { method: "POST" });
}

export async function getOrders(runId: string): Promise<OrdersResponse> {
  return request(`/runs/${runId}/orders`);
}

/** All captured orders — the customer History feed. */
export async function getAllOrders(): Promise<{ count: number; orders: OrdersResponse["orders"] }> {
  return request("/orders");
}

// ---------------------------------------------------------------------------
// Your Arkim impact (cumulative)
// ---------------------------------------------------------------------------

export async function getImpact(): Promise<CumulativeImpact> {
  return request("/impact");
}

/** Reorder intelligence — parts due to be reordered (forecast from order cadence). */
export async function getReorder(): Promise<{ count: number; reorder: ReorderItem[] }> {
  return request("/reorder");
}

// ---------------------------------------------------------------------------
// Site delivery (ship-to) settings
// ---------------------------------------------------------------------------

export interface SiteShipToResponse {
  site_id: string;
  ship_to: (ShipTo & { updated_at?: string }) | null;
}

export async function getSiteShipTo(siteId: string): Promise<SiteShipToResponse> {
  return request(`/sites/${siteId}/ship-to`);
}

export async function putSiteShipTo(siteId: string, body: ShipTo): Promise<SiteShipToResponse> {
  return request(`/sites/${siteId}/ship-to`, { method: "PUT", body: JSON.stringify(body) });
}

// ---------------------------------------------------------------------------
// Facilities
// ---------------------------------------------------------------------------

export async function listFacilities(): Promise<Facility[]> {
  return request("/facilities");
}

// ---------------------------------------------------------------------------
// Approval rules
// ---------------------------------------------------------------------------

export async function getApprovalRules(facilityId: string): Promise<ApprovalRule[]> {
  return request(`/approval-rules/${facilityId}`);
}

export async function upsertApprovalRule(
  rule: Omit<ApprovalRule, "id"> & { id?: string },   // id => update in place; omit => insert
): Promise<ApprovalRule> {
  return request("/approval-rules", {
    method: "POST",
    body: JSON.stringify(rule),
  });
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export async function checkHealth(): Promise<{ status: string; version: string }> {
  return request("/health");
}
