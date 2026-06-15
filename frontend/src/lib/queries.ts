/**
 * TanStack Query hooks for the Arkim API.
 *
 * Each hook owns its cache key (via queryKeys factory) so any component can
 * subscribe without coordinating with others. Mutations invalidate their
 * relevant list/detail keys automatically.
 */

"use client";

import {
  useQuery,
  useMutation,
  useQueryClient,
  type UseQueryOptions,
} from "@tanstack/react-query";
import {
  confirmIntake,
  createRun,
  listRuns,
  getRun,
  sendMessage,
  uploadNameplate,
  selectCandidate,
  approveRun,
  rejectRun,
  initiateOutreach,
  requestConfirmation,
  saveOutreachSelection,
  listFacilities,
  getApprovalRules,
  upsertApprovalRule,
  openFromPending,
  rejectSubmission,
  getReviewItems,
  processReplies,
  confirmReviewItem,
  rejectReviewItem,
  placeOrderFromQuote,
  executeOrder,
  markDelivered,
  getOrders,
  getAllOrders,
  getImpact,
  getReorder,
  getSiteShipTo,
  putSiteShipTo,
} from "./api";
import type { ShipTo } from "./proc-config";
import { queryKeys } from "./query-client";
import type {
  ApproveRequest,
  CreateRunRequest,
  OutreachRequest,
  RejectRequest,
  SelectCandidateRequest,
  SendMessageRequest,
  SourcingRunDetail,
} from "@/types";

// ---------------------------------------------------------------------------
// Runs — list
// ---------------------------------------------------------------------------

export function useRuns(params?: { facilityId?: string; phase?: string }) {
  return useQuery({
    queryKey: queryKeys.runs.list(params),
    queryFn: () => listRuns(params),
  });
}

// ---------------------------------------------------------------------------
// Run — detail (with optional polling while active)
// ---------------------------------------------------------------------------

export function useRun(
  runId: string,
  options?: Partial<UseQueryOptions<SourcingRunDetail>>,
) {
  return useQuery({
    queryKey: queryKeys.runs.detail(runId),
    queryFn: () => getRun(runId),
    enabled: Boolean(runId),
    ...options,
  });
}

/** Poll every 5 s while the run is in a transient phase (sourcing, executing…)
 *  or in comparison — needed so Tier 1 mock confirmation responses arrive
 *  without a manual refresh.
 *  TODO(post-seed): move to push/websocket to avoid prototype-scale polling overhead.
 */
export function useRunLive(runId: string) {
  return useRun(runId, {
    refetchInterval: (query) => {
      const phase = query.state.data?.phase;
      const activePhases = ["sourcing", "executing", "fulfilling", "inventory", "comparison"];
      // Poll while the phase is still in progress OR not yet known (undefined) — so a
      // transient/first-render gap never stops the interval before the run advances.
      return !phase || activePhases.includes(phase) ? 5_000 : false;
    },
    // Sourcing takes 1–2 min of real provider calls; users often tab away while
    // waiting. Keep polling in the background, and refetch on window focus, so the
    // run advances to its options view without a manual refresh.
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  });
}

// ---------------------------------------------------------------------------
// Create run
// ---------------------------------------------------------------------------

export function useCreateRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateRunRequest) => createRun(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.runs.all() });
    },
  });
}

// ---------------------------------------------------------------------------
// Intake: send message
// ---------------------------------------------------------------------------

export function useSendMessage(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SendMessageRequest) => sendMessage(runId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
    },
  });
}

// ---------------------------------------------------------------------------
// Intake: upload nameplate
// ---------------------------------------------------------------------------

export function useUploadNameplate(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => uploadNameplate(runId, file),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
    },
  });
}

// ---------------------------------------------------------------------------
// Intake: confirm specs and advance to sourcing
// ---------------------------------------------------------------------------

export function useConfirmIntake(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => confirmIntake(runId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.runs.all() });
    },
  });
}

// ---------------------------------------------------------------------------
// Comparison: select candidate
// ---------------------------------------------------------------------------

export function useSelectCandidate(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SelectCandidateRequest) => selectCandidate(runId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.runs.all() });
    },
  });
}

// ---------------------------------------------------------------------------
// Approval: approve / reject
// ---------------------------------------------------------------------------

export function useApproveRun(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ApproveRequest) => approveRun(runId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.runs.all() });
    },
  });
}

export function useRejectRun(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: RejectRequest) => rejectRun(runId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.runs.all() });
    },
  });
}

// ---------------------------------------------------------------------------
// Tier 1 confirmation request
// ---------------------------------------------------------------------------

export function useRequestConfirmation(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (candidateIds: string[]) =>
      requestConfirmation(runId, { candidate_ids: candidateIds }),
    onSuccess: () => {
      // Poll will pick up confirmation_needed=false after mock delay; no immediate invalidation needed.
      // Invalidate anyway so the "Awaiting" state (from Zustand sentAt) is consistent with server state.
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
    },
  });
}

// ---------------------------------------------------------------------------
// Tier 3 outreach
// ---------------------------------------------------------------------------

export function useInitiateOutreach(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: OutreachRequest) => initiateOutreach(runId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
    },
  });
}

export function useSaveOutreach(runId: string) {
  return useMutation({
    mutationFn: (candidateIds: string[]) => saveOutreachSelection(runId, candidateIds),
  });
}

// ---------------------------------------------------------------------------
// Buyer loop — inbound quote review (comparison table)
// ---------------------------------------------------------------------------

export function useReviewItems(runId: string) {
  return useQuery({
    queryKey: queryKeys.reviewItems.byRun(runId),
    queryFn: () => getReviewItems(runId),
    enabled: Boolean(runId),
  });
}

/** "Check for new replies" — triggers a live inbox read + ingest, then refreshes. */
export function useProcessReplies(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => processReplies(runId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.reviewItems.byRun(runId) });
    },
  });
}

export function useConfirmReviewItem(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (itemId: string) => confirmReviewItem(itemId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.reviewItems.byRun(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
    },
  });
}

export function useRejectReviewItem(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (itemId: string) => rejectReviewItem(itemId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.reviewItems.byRun(runId) });
    },
  });
}

/** Place an order from a confirmed quote (RFQ path) — surfaces in the order section. */
export function usePlaceOrderFromQuote(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (itemId: string) => placeOrderFromQuote(itemId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.orders.byRun(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.reviewItems.byRun(runId) });
    },
  });
}

// ---------------------------------------------------------------------------
// Buyer loop — order placement + tracking
// ---------------------------------------------------------------------------

export function useOrders(runId: string) {
  return useQuery({
    queryKey: queryKeys.orders.byRun(runId),
    queryFn: () => getOrders(runId),
    enabled: Boolean(runId),
  });
}

export function useAllOrders() {
  return useQuery({
    queryKey: queryKeys.orders.all(),
    queryFn: getAllOrders,
  });
}

export function useExecuteOrder(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => executeOrder(runId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.orders.byRun(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
    },
  });
}

export function useMarkDelivered(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => markDelivered(runId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.orders.byRun(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
    },
  });
}

// ---------------------------------------------------------------------------
// Your Arkim impact (cumulative)
// ---------------------------------------------------------------------------

export function useImpact() {
  return useQuery({
    queryKey: queryKeys.impact.cumulative(),
    queryFn: getImpact,
  });
}

export function useReorder() {
  return useQuery({
    queryKey: queryKeys.reorder.all(),
    queryFn: getReorder,
  });
}

// ---------------------------------------------------------------------------
// Site delivery (ship-to) settings
// ---------------------------------------------------------------------------

export function useSiteShipTo(siteId: string) {
  return useQuery({
    queryKey: queryKeys.siteShipTo.bySite(siteId),
    queryFn: () => getSiteShipTo(siteId),
    enabled: Boolean(siteId),
  });
}

export function useSaveSiteShipTo(siteId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ShipTo) => putSiteShipTo(siteId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.siteShipTo.bySite(siteId) });
    },
  });
}

// ---------------------------------------------------------------------------
// Facilities
// ---------------------------------------------------------------------------

export function useFacilities() {
  return useQuery({
    queryKey: queryKeys.facilities.all(),
    queryFn: listFacilities,
    staleTime: Infinity, // rarely changes
  });
}

// ---------------------------------------------------------------------------
// Approval rules
// ---------------------------------------------------------------------------

export function useApprovalRules(facilityId: string) {
  return useQuery({
    queryKey: queryKeys.approvalRules.byFacility(facilityId),
    queryFn: () => getApprovalRules(facilityId),
    enabled: Boolean(facilityId),
    staleTime: 5 * 60_000,
  });
}

export function useUpsertApprovalRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: upsertApprovalRule,
    onSuccess: (_data, variables) => {
      qc.invalidateQueries({
        queryKey: queryKeys.approvalRules.byFacility(variables.facility_id),
      });
    },
  });
}

// ---------------------------------------------------------------------------
// Maintenance handoff: open / reject
// ---------------------------------------------------------------------------

export function useOpenFromPending(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => openFromPending(runId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.runs.all() });
    },
  });
}

export function useRejectSubmission(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => rejectSubmission(runId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.runs.all() });
    },
  });
}
