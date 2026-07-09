"use client";

import Link from "next/link";
import { useRuns } from "@/lib/queries";
import { Button } from "@/components/ui/button";
import { Pill } from "@/components/ui/pill";
import { Plus, Package } from "@/components/ui/icons";
import { PHASE_LABELS, urgencyTone } from "@/types";
import type { Phase, Urgency } from "@/types";
import { cn } from "@/lib/utils";
import { BRAND_NAME } from "@/lib/brand";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function phaseTone(phase: Phase): "blue" | "green" | "amber" | "red" | "ghost" {
  if (["completed", "approved"].includes(phase)) return "green";
  if (["pending_first_approval", "pending_second_approval", "pending_intake"].includes(phase)) return "amber";
  if (["cancelled", "error"].includes(phase)) return "red";
  if (["intake", "inventory", "sourcing", "comparison", "executing", "fulfilling"].includes(phase))
    return "blue";
  return "ghost";
}

function isLive(phase: Phase) {
  return ["intake", "inventory", "sourcing", "comparison"].includes(phase);
}

function formatDate(iso: string) {
  if (!iso) return "—";
  const d = new Date(iso);
  const now = new Date();
  const diff = (now.getTime() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function RunsPage() {
  const { data: runs, isLoading, isError } = useRuns();

  const pendingRuns = runs?.filter((r) => r.phase === "pending_intake") ?? [];
  const activeRuns = runs?.filter((r) => r.phase !== "pending_intake") ?? [];

  return (
    <div className="flex flex-col h-full">
      {/* Top bar */}
      <div className="shrink-0 flex items-center justify-between border-b border-hr-2 px-5 py-3">
        <div>
          <h1 className="text-h2 text-fg-1">Sourcing Runs</h1>
          {runs && (
            <p className="font-mono text-[10.5px] text-fg-4 mt-0.5">
              {runs.length} run{runs.length !== 1 ? "s" : ""}
            </p>
          )}
        </div>
        <Link href="/runs/new">
          <Button variant="primary" size="sm" leadingIcon={<Plus size={12} />}>
            New run
          </Button>
        </Link>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto px-5 py-4">
        {isLoading && <LoadingSkeleton />}
        {isError && (
          <p className="text-sm text-red-fg py-8 text-center">
            Failed to load runs — is the backend running?
          </p>
        )}
        {runs && runs.length === 0 && <EmptyState />}
        {runs && runs.length > 0 && (
          <div className="flex flex-col gap-4">
            {/* Pending from maintenance */}
            {pendingRuns.length > 0 && (
              <div className="flex flex-col gap-2">
                <div className="section-cap">
                  Pending from maintenance <span className="rule" />
                </div>
                {pendingRuns.map((run) => (
                  <RunCard key={run.id} run={run} />
                ))}
              </div>
            )}

            {/* Active sourcing runs */}
            {activeRuns.length > 0 && (
              <div className="flex flex-col gap-2">
                {pendingRuns.length > 0 && (
                  <div className="section-cap">
                    Active <span className="rule" />
                  </div>
                )}
                {activeRuns.map((run) => (
                  <RunCard key={run.id} run={run} />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Run card
// ---------------------------------------------------------------------------

function RunCard({ run }: { run: ReturnType<typeof useRuns>["data"] extends (infer T)[] | undefined ? T : never }) {
  if (!run) return null;
  const tone = phaseTone(run.phase as Phase);
  const live = isLive(run.phase as Phase);

  return (
    <Link
      href={`/runs/${run.id}`}
      className={cn(
        "block rounded-card border border-hr-2 bg-bg-3 p-4",
        "hover:bg-bg-4 hover:border-hr-3 transition-colors",
      )}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex flex-col gap-1 min-w-0">
          <div className="run-id shrink-0">
            <span className="text-fg-4 mr-1">RUN</span>
            {run.id.slice(0, 8).toUpperCase()}
          </div>
          <p className="text-sm text-fg-1 truncate font-medium">
            {run.asset_summary ?? "No description yet"}
          </p>
          <p className="font-mono text-[10.5px] text-fg-4 truncate">{run.facility_id}</p>
        </div>

        <div className="flex flex-col items-end gap-1.5 shrink-0">
          <Pill tone={tone} pulseDot={live}>
            {PHASE_LABELS[run.phase as Phase]}
          </Pill>
          {run.urgency !== "Stocking" && (
            <Pill tone={urgencyTone(run.urgency as Urgency)}>
              {run.urgency}
            </Pill>
          )}
          {run.amount != null && (
            <span className="font-mono text-[11.5px] text-fg-2 tabular-nums">
              ${run.amount.toLocaleString("en-US", { minimumFractionDigits: 2 })}
            </span>
          )}
          <span className="font-mono text-[10px] text-fg-4">
            {formatDate(run.created_at)}
          </span>
        </div>
      </div>
    </Link>
  );
}

// ---------------------------------------------------------------------------
// States
// ---------------------------------------------------------------------------

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center gap-4 py-20 text-center">
      <Package size={32} className="text-fg-4" />
      <div>
        <p className="text-sm text-fg-2 font-medium">No sourcing runs yet</p>
        <p className="mt-1 text-[12.5px] text-fg-4">
          Start a new run to search the {BRAND_NAME} network and open marketplace.
        </p>
      </div>
      <Link href="/runs/new">
        <Button variant="primary" size="sm" leadingIcon={<Plus size={12} />}>
          New run
        </Button>
      </Link>
    </div>
  );
}

function LoadingSkeleton() {
  return (
    <div className="flex flex-col gap-2">
      {Array.from({ length: 4 }).map((_, i) => (
        <div
          key={i}
          className="h-[76px] rounded-card border border-hr-2 bg-bg-3 animate-pulse"
        />
      ))}
    </div>
  );
}
