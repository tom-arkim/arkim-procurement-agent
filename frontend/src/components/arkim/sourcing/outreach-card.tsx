"use client";

import { cn } from "@/lib/utils";
import { MatchBar } from "@/components/ui/match";
import { Clock } from "@/components/ui/icons";
import { useArkimStore } from "@/store";
import type { Candidate } from "@/types";

interface OutreachCardProps {
  candidate: Candidate;
  runId: string;
  className?: string;
}

export function OutreachCard({ candidate, runId, className }: OutreachCardProps) {
  const toggle = useArkimStore((s) => s.toggleTier3Vendor);
  const selection = useArkimStore((s) => s.tier3Selection[runId] ?? new Set<string>());
  const selected = selection.has(candidate.id);

  return (
    <div
      className={cn(
        "rounded-card border bg-bg-3 p-4 flex gap-3 cursor-pointer",
        selected
          ? "border-green-line bg-green-tint"
          : "border-hr-2 hover:border-hr-1",
        className,
      )}
      onClick={() => toggle(runId, candidate.id)}
    >
      {/* Checkbox */}
      <div
        className={cn(
          "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border",
          selected ? "border-green-fg bg-green-fg" : "border-hr-1 bg-bg-2",
        )}
      >
        {selected && (
          <svg
            viewBox="0 0 10 10"
            width="10"
            height="10"
            fill="none"
            stroke="white"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M2 5l2.5 2.5L8 3" />
          </svg>
        )}
      </div>

      {/* Content */}
      <div className="flex flex-col gap-2 min-w-0 flex-1">
        <div className="flex items-start justify-between gap-2">
          <div className="flex flex-col gap-0.5 min-w-0">
            <span className="text-sm font-medium text-fg-1 truncate">
              {candidate.vendorName}
            </span>
            {candidate.loc && (
              <span className="font-mono text-[10px] text-fg-4 uppercase tracking-[0.06em]">
                {candidate.loc}
              </span>
            )}
          </div>
          {candidate.contact && (
            <span className="font-mono text-[10px] text-fg-3 shrink-0 truncate max-w-[140px]">
              {candidate.contact}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.06em] text-fg-4 shrink-0 w-16">
            Suitability
          </span>
          <MatchBar
            value={candidate.suitability}
            label={`${Math.round(candidate.suitability)}%`}
          />
        </div>

        <div className="flex items-center gap-1 text-fg-3">
          <Clock size={12} className="shrink-0" />
          <span className="font-mono text-[11px]">{candidate.leadTime}</span>
        </div>
      </div>
    </div>
  );
}
