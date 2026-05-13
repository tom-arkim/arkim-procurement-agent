"use client";

import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import { TierHeader } from "@/components/arkim/tier-header";
import { VendorCard } from "@/components/arkim/sourcing/vendor-card";
import { OutreachCard } from "@/components/arkim/sourcing/outreach-card";
import { StickyActionBar } from "@/components/arkim/sourcing/sticky-action-bar";
import { Dot } from "@/components/ui/pill";
import { useArkimStore } from "@/store";
import type { SourcingRunDetail } from "@/types";

interface SourcingViewProps {
  run: SourcingRunDetail;
  className?: string;
}

export function SourcingView({ run, className }: SourcingViewProps) {
  const results = run.sourcing_results;
  const setTier3Selection = useArkimStore((s) => s.setTier3Selection);
  const initialized = useRef(false);

  // Pre-select top 3 Tier 3 vendors by suitability on first results load.
  // Only runs once per mount so user toggle changes are not overwritten.
  useEffect(() => {
    if (!results || initialized.current) return;
    initialized.current = true;
    if (run.tier3_selection && run.tier3_selection.length > 0) {
      setTier3Selection(run.id, run.tier3_selection);
    } else {
      const top3 = [...(results.tier3 ?? [])]
        .sort((a, b) => b.suitability - a.suitability)
        .slice(0, 3)
        .map((c) => c.id);
      setTier3Selection(run.id, top3);
    }
  }, [results, run.id, run.tier3_selection, setTier3Selection]);

  if (!results) {
    return <SourcingLoadingState className={className} />;
  }

  const tier1 = results.tier1 ?? [];
  const tier2 = results.tier2 ?? [];
  const tier3 = results.tier3 ?? [];

  return (
    <div className={cn("flex flex-col h-full overflow-hidden", className)}>
      {results.warrantyBanner && (
        <div className="px-4 py-2 bg-amber-tint border-b border-amber-line shrink-0">
          <p className="text-[12px] text-amber-fg">{results.warrantyBanner}</p>
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        {/* Tier 1 — Arkim Network */}
        <section>
          <TierHeader tier={1} count={tier1.length} className="border-b border-hr-2" />
          {tier1.length > 0 ? (
            <div className="p-4 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {tier1.map((c) => (
                <VendorCard
                  key={c.id}
                  candidate={c}
                  runId={run.id}
                />
              ))}
            </div>
          ) : (
            <EmptyTier message="No Arkim network partners found for this part." />
          )}
        </section>

        <div className="border-t border-hr-2" />

        {/* Tier 2 — Open Marketplace */}
        <section>
          <TierHeader tier={2} count={tier2.length} className="border-b border-hr-2" />
          {tier2.length > 0 ? (
            <div className="p-4 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {tier2.map((c) => (
                <VendorCard
                  key={c.id}
                  candidate={c}
                  runId={run.id}
                />
              ))}
            </div>
          ) : (
            <EmptyTier message="No open marketplace results found." />
          )}
        </section>

        <div className="border-t border-hr-2" />

        {/* Tier 3 — Outreach */}
        <section>
          <TierHeader tier={3} count={tier3.length} className="border-b border-hr-2" />
          {results.tier3CapabilityPivot && (
            <div className="mx-4 mt-3 rounded border border-amber-line bg-amber-tint px-3 py-2">
              <p className="text-[11.5px] text-amber-fg">
                Sourcing pivoted to specialist outreach — direct match not found in tiers 1 or 2.
              </p>
            </div>
          )}
          {tier3.length > 0 ? (
            <div className="p-4 flex flex-col gap-3">
              {tier3.map((c) => (
                <OutreachCard
                  key={c.id}
                  candidate={c}
                  runId={run.id}
                  sentAt={run.tier3_outreach_sent?.[c.id]}
                />
              ))}
            </div>
          ) : (
            <EmptyTier message="No outreach candidates identified." />
          )}
        </section>
      </div>

      {tier3.length > 0 && <StickyActionBar runId={run.id} />}
    </div>
  );
}

function EmptyTier({ message }: { message: string }) {
  return (
    <div className="px-4 py-6 text-center">
      <p className="font-mono text-[11px] text-fg-4">{message}</p>
    </div>
  );
}

function SourcingLoadingState({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "flex flex-1 flex-col items-center justify-center gap-4 p-8 text-center",
        className,
      )}
    >
      <div className="flex items-center gap-2">
        <Dot tone="blue" pulse />
        <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-fg-3">
          Sourcing in progress…
        </span>
      </div>
      <p className="text-sm text-fg-3 max-w-xs">
        Scanning Arkim network, open marketplace, and specialist vendors. This
        typically takes 30–90 seconds.
      </p>
      <div className="flex flex-col gap-2 w-full max-w-xs mt-2">
        {[85, 65, 75, 55].map((w, i) => (
          <div
            key={i}
            className="h-2 rounded bg-bg-3"
            style={{ width: `${w}%`, opacity: 1 - i * 0.15 }}
          />
        ))}
      </div>
    </div>
  );
}
