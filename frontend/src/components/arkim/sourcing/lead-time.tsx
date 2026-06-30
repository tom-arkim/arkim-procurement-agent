import { Clock } from "@/components/ui/icons";
import { cn } from "@/lib/utils";
import type { Candidate } from "@/types";

/**
 * Honest lead-time chip, mirroring the price-provenance treatment (≈ / "Quote Required"):
 *  - no real lead time (null — a pre-quote/RFQ row or absent data) → "Lead time on quote",
 *    never a fabricated number.
 *  - "defaulted" (a Tier-2 heuristic) → shown but qualified with ~ + muted (estimated).
 *  - "extracted" (a stated catalog value) / "quoted" (a confirmed quote) → shown as-is.
 */
export function LeadTime({ candidate, className }: { candidate: Candidate; className?: string }) {
  const { leadTime, leadTimeSource } = candidate;

  if (!leadTime) {
    return (
      <div className={cn("flex items-center gap-1 text-fg-4", className)} title="Lead time provided on quote">
        <Clock size={12} className="shrink-0" />
        <span className="font-mono text-[11px] italic">Lead time on quote</span>
      </div>
    );
  }

  const estimated = leadTimeSource === "defaulted";
  return (
    <div
      className={cn("flex items-center gap-1", estimated ? "text-fg-4" : "text-fg-3", className)}
      title={estimated ? "Estimated lead time" : undefined}
    >
      <Clock size={12} className="shrink-0" />
      <span className={cn("font-mono text-[11px]", estimated && "italic")}>
        {estimated ? `~${leadTime}` : leadTime}
      </span>
    </div>
  );
}
