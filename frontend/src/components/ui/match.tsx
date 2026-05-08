import { cn } from "@/lib/utils";
import { Pill } from "./pill";
import type { PnMatchLevel, CompatibilitySummary } from "@/types";

// ---------------------------------------------------------------------------
// PnMatch — part-number match level badge
// ---------------------------------------------------------------------------

interface PnMatchProps {
  level: PnMatchLevel;
  className?: string;
}

const pnMatchConfig: Record<
  PnMatchLevel,
  { tone: "green" | "amber" | "red" | "ghost"; label: string }
> = {
  exact: { tone: "green", label: "Exact P/N" },
  normalized: { tone: "green", label: "Normalized" },
  stem: { tone: "amber", label: "Stem match" },
  substring: { tone: "amber", label: "Partial" },
  none: { tone: "ghost", label: "No match" },
};

export function PnMatch({ level, className }: PnMatchProps) {
  const { tone, label } = pnMatchConfig[level];
  return (
    <Pill tone={tone} className={className}>
      {label}
    </Pill>
  );
}

// ---------------------------------------------------------------------------
// CompatBadge — fit/compatibility summary
// ---------------------------------------------------------------------------

interface CompatBadgeProps {
  summary: CompatibilitySummary;
  className?: string;
}

const compatConfig: Record<
  CompatibilitySummary,
  { tone: "green" | "amber" | "red" | "ghost"; label: string }
> = {
  fit_confirmed: { tone: "green", label: "Fit confirmed" },
  fit_likely: { tone: "green", label: "Fit likely" },
  verification_required: { tone: "amber", label: "Verify fit" },
  incompatible: { tone: "red", label: "Incompatible" },
};

export function CompatBadge({ summary, className }: CompatBadgeProps) {
  const { tone, label } = compatConfig[summary];
  return (
    <Pill tone={tone} className={className}>
      {label}
    </Pill>
  );
}

// ---------------------------------------------------------------------------
// MatchScore — numeric suitability score with color band
// ---------------------------------------------------------------------------

interface MatchScoreProps {
  score: number; // 0–100
  className?: string;
}

function scoreTone(s: number): "green" | "blue" | "amber" | "red" {
  if (s >= 90) return "green";
  if (s >= 75) return "blue";
  if (s >= 60) return "amber";
  return "red";
}

export function MatchScore({ score, className }: MatchScoreProps) {
  const tone = scoreTone(score);
  return (
    <Pill tone={tone} className={className}>
      {score}%
    </Pill>
  );
}

// ---------------------------------------------------------------------------
// MatchBar — horizontal fill bar (suitability / confidence)
// ---------------------------------------------------------------------------

interface MatchBarProps {
  value: number; // 0–100
  label?: string;
  className?: string;
}

const barColor: Record<ReturnType<typeof scoreTone>, string> = {
  green: "bg-green-50",
  blue: "bg-blue-50",
  amber: "bg-amber-50",
  red: "bg-red-50",
};

export function MatchBar({ value, label, className }: MatchBarProps) {
  const tone = scoreTone(value);
  return (
    <div className={cn("flex items-center gap-2", className)}>
      <div className="relative h-1 flex-1 rounded-full bg-bg-4 overflow-hidden">
        <div
          className={cn("absolute inset-y-0 left-0 rounded-full", barColor[tone])}
          style={{ width: `${Math.min(100, Math.max(0, value))}%` }}
        />
      </div>
      {label !== undefined && (
        <span className="font-mono text-[11px] text-fg-2 tabular-nums w-7 text-right shrink-0">
          {label}
        </span>
      )}
    </div>
  );
}
