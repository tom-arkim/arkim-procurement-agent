"use client";

import { use } from "react";
import { useRunLive } from "@/lib/queries";
import { RunSummaryBar } from "@/components/gofer/run-summary-bar";
import { ChatPanel } from "@/components/gofer/intake/chat-panel";
import { SpecPanel } from "@/components/gofer/intake/spec-panel";
import { SourcingView } from "@/components/gofer/sourcing/sourcing-view";
import { PendingIntakeView } from "@/components/gofer/pending-intake-view";
import { PhaseBar } from "@/components/ui/phase";
import { Pill } from "@/components/ui/pill";
import { Dot } from "@/components/ui/pill";
import { PHASE_LABELS } from "@/types";
import type { Phase, ChatMessage, SourcingRunDetail } from "@/types";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function RunDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const { data: run, isLoading, isError } = useRunLive(id);

  if (isLoading) return <LoadingShell />;
  if (isError || !run) return <ErrorShell id={id} />;

  const phase = run.phase as Phase;

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Summary bar */}
      <RunSummaryBar run={run} />

      {/* Phase body */}
      {isPendingIntakePhase(phase) ? (
        <PendingIntakeView run={run} className="flex-1" />
      ) : isIntakePhase(phase) ? (
        <IntakeView run={run} runId={id} />
      ) : isSourcingPhase(phase) ? (
        <SourcingView run={run} className="flex-1" />
      ) : (
        <TransitionalView run={run} phase={phase} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Intake view — two-column chat + spec
// ---------------------------------------------------------------------------

function IntakeView({
  run,
  runId,
}: {
  run: SourcingRunDetail;
  runId: string;
}) {
  const messages = (run.messages ?? []) as ChatMessage[];

  return (
    <div className="flex flex-1 overflow-hidden">
      {/* Chat (flex-1) */}
      <div className="flex-1 min-w-0 border-r border-hr-2">
        <ChatPanel runId={runId} messages={messages} className="h-full" />
      </div>

      {/* Spec panel (fixed width) */}
      <div className="w-72 shrink-0 bg-bg-2">
        <SpecPanel run={run} className="h-full" />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Transitional view — non-intake phases (sourcing, approval, etc.)
// ---------------------------------------------------------------------------

function TransitionalView({ run, phase }: { run: SourcingRunDetail; phase: Phase }) {
  const isProcessing = ["inventory", "sourcing", "executing", "fulfilling"].includes(phase);
  const isApproval = ["pending_first_approval", "pending_second_approval"].includes(phase);
  const isDone = phase === "completed";
  const isFailed = ["cancelled", "error"].includes(phase);

  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-6 p-8 text-center">
      {/* Phase progress */}
      <PhaseBar phase={phase} />

      {/* Status */}
      <div className="flex flex-col items-center gap-2">
        <div className="flex items-center gap-2">
          {isProcessing && <Dot tone="blue" pulse />}
          <Pill
            tone={
              isDone ? "green" :
              isApproval ? "amber" :
              isFailed ? "red" : "blue"
            }
            pulseDot={isProcessing}
          >
            {PHASE_LABELS[phase]}
          </Pill>
        </div>

        <p className="text-sm text-fg-2 max-w-sm">
          {isProcessing && "The sourcing pipeline is running. This page will update automatically."}
          {isApproval && "Waiting for approver sign-off. The run will advance once approved."}
          {isDone && "Run complete. Results and selected vendor are locked."}
          {isFailed && "This run was cancelled or encountered an error."}
        </p>

      </div>

      {/* Approval history */}
      {run.approval_history && run.approval_history.length > 0 && (
        <div className="flex flex-col gap-2 w-full max-w-sm mt-2">
          <div className="section-cap">
            Approval history <span className="rule" />
          </div>
          {run.approval_history.map((entry, i) => (
            <div
              key={i}
              className={cn(
                "flex items-start gap-2 rounded border p-3",
                entry.action === "approved"
                  ? "border-green-line bg-green-tint"
                  : "border-red-line bg-red-tint",
              )}
            >
              <Pill
                tone={entry.action === "approved" ? "green" : "red"}
                solid
                className="shrink-0"
              >
                {entry.action}
              </Pill>
              <div className="text-left min-w-0">
                <p className="text-sm text-fg-1 font-medium">{entry.approver_role ?? "—"}</p>
                {entry.notes && (
                  <p className="text-[12px] text-fg-3 mt-0.5">{entry.notes}</p>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Loading / error shells
// ---------------------------------------------------------------------------

function LoadingShell() {
  return (
    <div className="flex flex-1 items-center justify-center">
      <div className="flex items-center gap-2 text-fg-3">
        <Dot tone="blue" pulse />
        <span className="font-mono text-[11px] uppercase tracking-[0.08em]">Loading run…</span>
      </div>
    </div>
  );
}

function ErrorShell({ id }: { id: string }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center">
      <p className="text-sm text-red-fg">Run not found</p>
      <p className="font-mono text-[10.5px] text-fg-4">{id}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isPendingIntakePhase(phase: Phase) {
  return phase === "pending_intake";
}

function isIntakePhase(phase: Phase) {
  return ["intake", "inventory"].includes(phase);
}

function isSourcingPhase(phase: Phase) {
  return [
    "sourcing",
    "comparison",
    "approved",
  ].includes(phase);
}
