import { cn } from "@/lib/utils";
import { Pill } from "@/components/ui/pill";
import { PhaseBar } from "@/components/ui/phase";
import { urgencyTone, PHASE_LABELS } from "@/types";
import type { SourcingRunListItem, SourcingRunDetail } from "@/types";

type RunBarRun = Pick<
  SourcingRunListItem | SourcingRunDetail,
  "id" | "phase" | "urgency" | "warranty" | "facility_id"
> & {
  asset_summary?: string;
  amount?: number;
};

interface RunSummaryBarProps {
  run: RunBarRun;
  showPhaseBar?: boolean;
  className?: string;
}

export function RunSummaryBar({ run, showPhaseBar = false, className }: RunSummaryBarProps) {
  const urgTone = urgencyTone(run.urgency);

  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-3 px-5 py-3",
        "border-b border-hr-2 bg-bg-2",
        className,
      )}
    >
      {/* Run ID chip */}
      <div className="run-id shrink-0">
        <span className="text-fg-4 mr-1">RUN</span>
        {run.id.slice(0, 8).toUpperCase()}
      </div>

      {/* Asset summary */}
      {run.asset_summary && (
        <span className="font-sans text-sm text-fg-2 truncate max-w-[240px]">
          {run.asset_summary}
        </span>
      )}

      {/* Spacer */}
      <div className="flex-1" />

      {/* Pills */}
      <div className="flex items-center gap-2 shrink-0">
        {/* Phase */}
        <Pill tone="blue" pulseDot={isLivePhase(run.phase)}>
          {PHASE_LABELS[run.phase]}
        </Pill>

        {/* Urgency — only non-stocking */}
        {run.urgency !== "Stocking" && (
          <Pill tone={urgTone}>{run.urgency}</Pill>
        )}

        {/* Warranty */}
        {run.warranty === "Active" && (
          <Pill tone="green">Warranty</Pill>
        )}

        {/* Amount */}
        {run.amount != null && (
          <span className="font-mono text-[12px] text-fg-1 tabular-nums">
            ${run.amount.toLocaleString("en-US", { minimumFractionDigits: 2 })}
          </span>
        )}
      </div>

      {/* Phase progress bar */}
      {showPhaseBar && (
        <div className="w-full pt-1">
          <PhaseBar phase={run.phase} />
        </div>
      )}
    </div>
  );
}

function isLivePhase(phase: string): boolean {
  return ["intake", "inventory", "sourcing", "comparison"].includes(phase);
}
