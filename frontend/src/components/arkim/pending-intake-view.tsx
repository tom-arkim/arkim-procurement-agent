"use client";

import { useRouter } from "next/navigation";
import { cn } from "@/lib/utils";
import { useOpenFromPending, useRejectSubmission } from "@/lib/queries";
import { useArkimStore } from "@/store";
import { Button } from "@/components/ui/button";
import { Pill } from "@/components/ui/pill";
import { PhaseBar } from "@/components/ui/phase";
import type { SourcingRunDetail } from "@/types";

export function PendingIntakeView({
  run,
  className,
}: {
  run: SourcingRunDetail;
  className?: string;
}) {
  const router = useRouter();
  const pushToast = useArkimStore((s) => s.pushToast);

  const handoff = run.maintenance_handoff as Record<string, unknown> | undefined;
  const context = handoff?.context as Record<string, unknown> | undefined;
  const specs = run.asset_specs;

  const openMut = useOpenFromPending(run.id);
  const rejectMut = useRejectSubmission(run.id);

  function handleOpen() {
    openMut.mutate(undefined, {
      onSuccess: () => pushToast({ head: "Run opened for review", tone: "green" }),
      onError: () => pushToast({ head: "Failed to open run", tone: "amber" }),
    });
  }

  function handleReject() {
    rejectMut.mutate(undefined, {
      onSuccess: () => {
        pushToast({ head: "Submission rejected", tone: "amber" });
        router.push("/runs");
      },
      onError: () => pushToast({ head: "Failed to reject submission", tone: "amber" }),
    });
  }

  const busy = openMut.isPending || rejectMut.isPending;

  return (
    <div
      className={cn(
        "flex flex-1 flex-col items-center justify-center gap-6 p-8 text-center",
        className,
      )}
    >
      <PhaseBar phase="pending_intake" />

      {/* Header */}
      <div className="flex flex-col items-center gap-2">
        <Pill tone="amber">Maintenance Handoff</Pill>
        {handoff?.submitted_by && (
          <p className="text-sm text-fg-2">
            Submitted by{" "}
            <span className="font-medium text-fg-1">
              {String(handoff.submitted_by)}
            </span>
          </p>
        )}
        <div className="flex flex-wrap gap-2 justify-center mt-1">
          {context?.work_order_id && (
            <span className="font-mono text-[11px] text-fg-3 bg-bg-2 border border-hr-2 rounded px-2 py-0.5">
              {String(context.work_order_id)}
            </span>
          )}
          {context?.asset_tag && (
            <span className="font-mono text-[11px] text-fg-3 bg-bg-2 border border-hr-2 rounded px-2 py-0.5">
              {String(context.asset_tag)}
            </span>
          )}
        </div>
      </div>

      {/* Asset specs */}
      {specs && (specs.manufacturer || specs.model) && (
        <div className="w-full max-w-sm rounded-card border border-hr-2 bg-bg-2 p-4 text-left">
          <p className="font-mono text-[10px] uppercase tracking-[0.08em] text-fg-4 mb-2">
            Asset
          </p>
          <p className="text-sm font-semibold text-fg-1">
            {[specs.manufacturer, specs.model].filter(Boolean).join(" · ")}
          </p>
          {specs.part_number && (
            <p className="font-mono text-[11px] text-fg-3 mt-0.5">{specs.part_number}</p>
          )}
        </div>
      )}

      {/* Maintenance context summary */}
      {context?.chat_thread_summary && (
        <div className="w-full max-w-sm rounded-card border border-amber-line bg-amber-tint p-4 text-left">
          <p className="font-mono text-[10px] uppercase tracking-[0.08em] text-fg-4 mb-2">
            Maintenance context
          </p>
          <p className="text-sm text-fg-2 leading-relaxed">
            {String(context.chat_thread_summary)}
          </p>
        </div>
      )}

      {/* Actions */}
      <div className="flex gap-3">
        <Button variant="primary" onClick={handleOpen} disabled={busy}>
          {openMut.isPending ? "Opening…" : "Open for Review"}
        </Button>
        <Button
          variant="ghost"
          onClick={handleReject}
          disabled={busy}
          className="text-red-fg hover:text-red-fg"
        >
          {rejectMut.isPending ? "Rejecting…" : "Reject Submission"}
        </Button>
      </div>
    </div>
  );
}
