import { cn } from "@/lib/utils";

interface TierHeaderProps {
  tier: 1 | 2 | 3;
  count?: number;
  className?: string;
}

const tierConfig = {
  1: {
    label: "Arkim Network",
    blurb: "Preferred partners · price-locked · instant PO",
    color: "var(--blue-fg)",
    bg: "var(--blue-tint)",
    border: "var(--blue-line)",
  },
  2: {
    label: "Open Marketplace",
    blurb: "National distributors · live pricing",
    color: "var(--amber-fg)",
    bg: "var(--amber-tint)",
    border: "var(--amber-line)",
  },
  3: {
    label: "Outreach",
    blurb: "Regional specialists · negotiated quotes",
    color: "var(--green-fg)",
    bg: "var(--green-tint)",
    border: "var(--green-line)",
  },
} as const;

export function TierHeader({ tier, count, className }: TierHeaderProps) {
  const config = tierConfig[tier];

  return (
    <div className={cn("flex items-center gap-3 py-3 px-4", className)}>
      {/* Tier numeral badge */}
      <div
        className="flex h-7 w-7 shrink-0 items-center justify-center rounded font-mono text-[13px] font-semibold"
        style={{
          background: config.bg,
          border: `1px solid ${config.border}`,
          color: config.color,
        }}
      >
        {tier}
      </div>

      {/* Name + blurb */}
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span
            className="font-mono text-[11.5px] font-semibold uppercase tracking-[0.08em]"
            style={{ color: config.color }}
          >
            {config.label}
          </span>
          {count != null && (
            <span className="font-mono text-[10.5px] text-fg-3">
              {count} result{count !== 1 ? "s" : ""}
            </span>
          )}
        </div>
        <p className="font-mono text-[10px] text-fg-4 uppercase tracking-[0.06em]">
          {config.blurb}
        </p>
      </div>
    </div>
  );
}
