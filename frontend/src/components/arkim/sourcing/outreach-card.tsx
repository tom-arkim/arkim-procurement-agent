"use client";

import { cn } from "@/lib/utils";
import { MatchBar } from "@/components/ui/match";
import { Clock } from "@/components/ui/icons";
import { LeadTime } from "./lead-time";
import { Pill } from "@/components/ui/pill";
import { useArkimStore } from "@/store";
import type { Candidate } from "@/types";

interface OutreachCardProps {
  candidate: Candidate;
  runId: string;
  /** ISO sentAt from run.tier3_outreach_sent — if set, card is in sent/awaiting state. */
  sentAt?: string;
  className?: string;
}

export function OutreachCard({ candidate, runId, sentAt, className }: OutreachCardProps) {
  const toggle = useArkimStore((s) => s.toggleTier3Vendor);
  const selection = useArkimStore((s) => s.tier3Selection[runId] ?? new Set<string>());
  const selected = selection.has(candidate.id);
  const hasSent = Boolean(sentAt);

  const sentTime = sentAt
    ? new Date(sentAt).toLocaleTimeString("en-US", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : null;

  return (
    <div
      className={cn(
        "rounded-card border bg-bg-3 p-4 flex gap-3",
        hasSent
          ? "border-hr-2 opacity-60 cursor-default"
          : selected
          ? "border-green-line bg-green-tint cursor-pointer"
          : "border-hr-2 hover:border-hr-1 cursor-pointer",
        className,
      )}
      onClick={hasSent ? undefined : () => toggle(runId, candidate.id)}
    >
      {/* Checkbox / sent indicator */}
      {hasSent ? (
        <div className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center">
          <Clock size={12} className="text-fg-4" />
        </div>
      ) : (
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
      )}

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
          {hasSent ? (
            <Pill tone="ghost">Awaiting</Pill>
          ) : candidate.contact ? (
            <span className="font-mono text-[10px] text-fg-3 shrink-0 truncate max-w-[140px]">
              {candidate.contact}
            </span>
          ) : null}
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

        <LeadTime candidate={candidate} />

        {hasSent && (
          <div className="flex flex-col items-center justify-center gap-1 py-2.5 rounded border border-hr-2 bg-bg-2 mt-1">
            <div className="flex items-center gap-1.5">
              <Clock size={13} className="text-fg-4 shrink-0" />
              <span className="font-mono text-[12px] font-medium text-fg-2">Awaiting response</span>
            </div>
            <span className="font-mono text-[10px] text-fg-4">Sent {sentTime}</span>
          </div>
        )}
      </div>
    </div>
  );
}
