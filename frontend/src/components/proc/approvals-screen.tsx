"use client";

/**
 * ApprovalsScreen — the approver worklist: every run awaiting approval, with the same
 * Approve/Reject affordance as the per-run panel (shared ApprovalActions, no fork).
 *
 * Honesty: no-auth WORKFLOW tool, not a verified permission gate. The queue shows all
 * pending runs and anyone can act; when auth lands the procurement-role gate + M1 switch
 * on (plumbing exists). No language implying authenticated/authorized access.
 *
 * The list endpoint filters by a SINGLE phase, so we fetch both approval phases and merge.
 * List rows carry no vendor/approval-history, so each row fetches its own run detail to
 * show the vendor + honest "N of M approved" progress and to drive the shared actions.
 * Approve/Reject invalidate the ["runs"] tree, so the queue refetches and rows leave it
 * as their phase advances (final approval → approved; reject → comparison).
 */

import { useRouter } from "next/navigation";
import { useRuns, useRun } from "@/lib/queries";
import { ProcIcon } from "./proc-icon";
import { ProcHead, ProcErrorNote, Skel, SkelList } from "./proc-ui";
import { ApprovalActions, deriveApproval, approvalStatusLine } from "./approval-actions";
import { ApprovalContext } from "./approval-context";
import type { SourcingRunListItem } from "@/types";

export function ApprovalsScreen() {
  const router = useRouter();
  const first = useRuns({ phase: "pending_first_approval" });
  const second = useRuns({ phase: "pending_second_approval" });

  const loading = first.isLoading || second.isLoading;
  const errored = first.isError || second.isError;
  const runs: SourcingRunListItem[] = [...(first.data ?? []), ...(second.data ?? [])]
    .sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));

  return (
    <div className="proc-max">
      <button className="proc-back" onClick={() => router.push("/")}>
        <span style={{ display: "inline-flex", transform: "rotate(180deg)" }}><ProcIcon name="chevR" size={14} /></span>
        Home
      </button>
      <ProcHead
        title={<>Approvals <b>queue</b></>}
        sub="Orders waiting on a decision before they're purchased."
      />

      {errored ? (
        <ProcErrorNote title="Couldn't load the approvals queue." sub="Is the backend running?" />
      ) : loading ? (
        <SkelList rows={2} label="Loading the approvals queue" />
      ) : runs.length === 0 ? (
        <div className="proc-empty" style={{ padding: "44px 20px 48px" }}>
          <div className="pe-mark"><ProcIcon name="checkCircle" size={26} /></div>
          <div className="pe-t">Nothing is waiting on approval.</div>
          <div className="pe-s">Orders that need a decision land here.</div>
          <div className="pe-sub">
            When a request crosses an approval threshold, it appears in this queue with
            the chosen supplier, the amount, and one-click approve or reject.
          </div>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {runs.map((r) => (
            <ApprovalQueueRow key={r.id} runId={r.id} />
          ))}
        </div>
      )}
    </div>
  );
}

function ApprovalQueueRow({ runId }: { runId: string }) {
  const router = useRouter();
  const { data: run, isLoading } = useRun(runId);

  if (isLoading || !run) {
    return (
      <div className="proc-track" role="status" aria-label="Loading approval">
        <Skel w="38%" h={14} />
        <div style={{ marginTop: 9 }}><Skel w="62%" h={11} /></div>
      </div>
    );
  }

  const d = deriveApproval(run);

  return (
    <div className="proc-track">
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 12, flexWrap: "wrap", gap: 10 }}>
        <div>
          <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text)" }}>{d.vendor}</div>
          <div style={{ fontSize: 12, color: "var(--muted-2)", marginTop: 4 }}>{approvalStatusLine(d)}</div>
        </div>
        <button className="proc-btn" data-kind="quiet" style={{ padding: "5px 10px", fontSize: 12 }} onClick={() => router.push(`/parts/${runId}`)}>
          Open run
        </button>
      </div>
      <ApprovalContext run={run} />
      <ApprovalActions run={run} />
    </div>
  );
}
