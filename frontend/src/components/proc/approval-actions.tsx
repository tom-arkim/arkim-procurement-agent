"use client";

/**
 * Shared Approve/Reject affordance for a run awaiting approval — the SINGLE copy of the
 * approval-action logic, used by both the per-run AwaitingApproval panel (order-section)
 * and the Approvals queue rows (approvals-screen). No forked second copy.
 *
 * Honesty: the app is no-auth, so this is a WORKFLOW action, NOT a verified authorization
 * gate. The approver name is a claimed identity recorded against the run, never an
 * authenticated/verified approver. When auth lands, the backend's distinct-approver rule
 * (M1) enforces on the verified sub and this field is backed by it; the 409 distinct-
 * approver response is already mapped honestly below.
 *
 * Reject mirrors the real backend: it returns the run to comparison and clears the
 * selection — it is NOT a terminal "Rejected" state. On success the approve/reject hooks
 * invalidate the ["runs"] tree, so the run detail AND any run lists (the queue) refetch
 * and react.
 */

import { useState } from "react";
import { useApproveRun, useRejectRun } from "@/lib/queries";
import { ApiError } from "@/lib/query-client";
import { ProcIcon } from "./proc-icon";
import type { Candidate, SourcingRunDetail } from "@/types";

type SelWithPath = Candidate & {
  candidate_id?: string;
  quantity?: number;
  _approval_path?: { approvers_required?: number; grand_total_usd?: number; approver_roles?: string[] };
};

export interface ApprovalDerived {
  vendor: string;
  total?: number;
  qty: number;
  required: number;
  approvedCount: number;
  secondPending: boolean;
  stepRole: string;
}

/** Derive the honest approval state (vendor, amount, progress, the current step's expected
 *  role) from a run's selection + approval history. Shared by the panel and the queue. */
export function deriveApproval(run: SourcingRunDetail): ApprovalDerived {
  const sel = run.selected_candidate as SelWithPath | undefined;
  const ap = sel?._approval_path;
  const required = ap?.approvers_required ?? 1;
  const total = ap?.grand_total_usd;
  const qty = sel?.quantity ?? 1;

  // Vendor from the run's sourcing results, matched by the selection's candidate_id.
  const all: Candidate[] = [
    ...(run.sourcing_results?.tier1 ?? []),
    ...(run.sourcing_results?.tier2 ?? []),
    ...(run.sourcing_results?.tier3 ?? []),
  ];
  const selectedId = sel?.candidate_id ?? run.selected_candidate?.id;
  const vendor = all.find((c) => c.id === selectedId)?.vendorName ?? "the selected supplier";

  // Honest progress: how many distinct approvals are already recorded for this run.
  const approvedCount = (run.approval_history ?? []).filter((h) => h.action === "approved").length;
  const secondPending = run.phase === "pending_second_approval";
  // Expected role for THIS step, from the approval path (first → [0], second → [1]).
  const stepRole = ap?.approver_roles?.[approvedCount] ?? "Approver";

  return { vendor, total, qty, required, approvedCount, secondPending, stepRole };
}

/** Honest status line shared by the panel and the queue. */
export function approvalStatusLine(d: ApprovalDerived): string {
  return d.secondPending
    ? `Awaiting second approval — ${d.approvedCount} of ${d.required} approved.`
    : `Awaiting approval — ${d.required} approver${d.required === 1 ? "" : "s"} required.`;
}

export function ApprovalActions({ run }: { run: SourcingRunDetail }) {
  const { secondPending, stepRole } = deriveApproval(run);
  const approve = useApproveRun(run.id);
  const reject = useRejectRun(run.id);
  const [name, setName] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const busy = approve.isPending || reject.isPending;

  const onApprove = () => {
    if (!name.trim() || busy) return;
    approve.mutate({ approver_name: name.trim(), approver_role: stepRole });
  };
  const onReject = () => {
    if (!name.trim() || !reason.trim() || busy) return;
    reject.mutate({ approver_name: name.trim(), approver_role: stepRole, notes: reason.trim() });
  };

  // Map the backend's distinct-approver rejection (M1, 409) honestly; otherwise generic.
  const approveErr = approve.error;
  const errMsg = approveErr
    ? (approveErr instanceof ApiError && approveErr.status === 409
        ? (((approveErr.body as { detail?: string } | undefined)?.detail) ?? "This run needs a different approver.")
        : "Couldn’t record the approval — please try again.")
    : reject.error
    ? "Couldn’t record the rejection — please try again."
    : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <label style={{ fontSize: 12, color: "var(--muted)", display: "flex", flexDirection: "column", gap: 5 }}>
        Approving as <span style={{ color: "var(--muted-2)" }}>({stepRole})</span>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Your name"
          disabled={busy}
          style={{
            padding: "9px 11px", fontSize: 13, borderRadius: 8,
            border: "1px solid var(--border)", background: "var(--surface)", color: "var(--text)",
          }}
        />
        <span style={{ fontSize: 11, color: "var(--muted-2)" }}>
          Recorded as the approver for this run.
        </span>
      </label>

      {rejecting ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Reason for rejecting (required)"
            rows={3}
            disabled={busy}
            style={{
              padding: "9px 11px", fontSize: 13, borderRadius: 8, resize: "vertical",
              border: "1px solid var(--border)", background: "var(--surface)", color: "var(--text)",
            }}
          />
          <div style={{ fontSize: 11.5, color: "var(--muted-2)" }}>
            Rejecting returns this run to your options so you can choose again.
          </div>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button className="proc-btn" data-kind="quiet" disabled={busy} onClick={() => { setRejecting(false); setReason(""); }}>
              Cancel
            </button>
            <button
              className="proc-btn"
              disabled={busy || !name.trim() || !reason.trim()}
              onClick={onReject}
              style={{ background: "var(--st-overdue)", borderColor: "var(--st-overdue)", color: "#fff" }}
            >
              {reject.isPending ? "Rejecting…" : "Confirm rejection"}
            </button>
          </div>
        </div>
      ) : (
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button className="proc-btn" data-kind="quiet" disabled={busy} onClick={() => setRejecting(true)}>
            Reject
          </button>
          <button className="proc-btn" data-kind="primary" disabled={busy || !name.trim()} onClick={onApprove}>
            <ProcIcon name="checkCircle" size={15} />
            {approve.isPending ? "Approving…" : secondPending ? "Approve (2nd)" : "Approve"}
          </button>
        </div>
      )}

      {errMsg && (
        <span className="note" style={{ color: "var(--st-overdue)", fontSize: 12.5 }}>{errMsg}</span>
      )}
    </div>
  );
}
