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
  saveOutreachSelection,
  listFacilities,
  getApprovalRules,
  upsertApprovalRule,
} from "./api";
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

/** Poll every 5 s while the run is in a transient phase (sourcing, executing…) */
export function useRunLive(runId: string) {
  return useRun(runId, {
    refetchInterval: (query) => {
      const phase = query.state.data?.phase;
      const activePhases = ["sourcing", "executing", "fulfilling", "inventory"];
      return phase && activePhases.includes(phase) ? 5_000 : false;
    },
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
