"use client";

import * as React from "react";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Tone helpers
// ---------------------------------------------------------------------------

type Tone = "green" | "blue" | "amber" | "red";

function confidenceTone(score: number): Tone {
  if (score >= 90) return "green";
  if (score >= 75) return "blue";
  if (score >= 60) return "amber";
  return "red";
}

const fillColor: Record<Tone, string> = {
  green: "bg-green-50",
  blue: "bg-blue-50",
  amber: "bg-amber-50",
  red: "bg-red-50",
};

const textColor: Record<Tone, string> = {
  green: "text-green-fg",
  blue: "text-blue-fg",
  amber: "text-amber-fg",
  red: "text-red-fg",
};

const toneLabel: Record<Tone, string> = {
  green: "High confidence",
  blue: "Good confidence",
  amber: "Moderate confidence",
  red: "Low confidence",
};

// ---------------------------------------------------------------------------
// ConfidenceIndicator
// ---------------------------------------------------------------------------

interface ConfidenceIndicatorProps {
  score: number; // 0–100
  label?: string;
  showBar?: boolean;
  showScore?: boolean;
  className?: string;
}

export function ConfidenceIndicator({
  score,
  label,
  showBar = true,
  showScore = true,
  className,
}: ConfidenceIndicatorProps) {
  const tone = confidenceTone(score);
  const pct = Math.min(100, Math.max(0, score));

  return (
    <TooltipPrimitive.Provider delayDuration={300}>
      <TooltipPrimitive.Root>
        <TooltipPrimitive.Trigger asChild>
          <div className={cn("flex items-center gap-2 cursor-default", className)}>
            {label && (
              <span className="font-mono text-[11px] uppercase tracking-[0.06em] text-fg-3 shrink-0">
                {label}
              </span>
            )}
            {showBar && (
              <div className="relative flex-1 h-1 min-w-[40px] rounded-full bg-bg-4 overflow-hidden">
                <div
                  className={cn("absolute inset-y-0 left-0 rounded-full transition-all", fillColor[tone])}
                  style={{ width: `${pct}%` }}
                />
              </div>
            )}
            {showScore && (
              <span className={cn("font-mono text-[11px] tabular-nums shrink-0", textColor[tone])}>
                {score}%
              </span>
            )}
          </div>
        </TooltipPrimitive.Trigger>
        <TooltipPrimitive.Portal>
          <TooltipPrimitive.Content
            sideOffset={6}
            className={cn(
              "z-50 rounded border bg-bg-4 border-hr-3 px-2.5 py-1.5 shadow-elevated",
              "font-mono text-[11px] text-fg-1",
              "animate-fade-in",
            )}
          >
            <div className="flex flex-col gap-0.5">
              <span className={cn("font-medium", textColor[tone])}>{toneLabel[tone]}</span>
              <span className="text-fg-3">{pct}% confidence score</span>
            </div>
            <TooltipPrimitive.Arrow className="fill-bg-4" />
          </TooltipPrimitive.Content>
        </TooltipPrimitive.Portal>
      </TooltipPrimitive.Root>
    </TooltipPrimitive.Provider>
  );
}
