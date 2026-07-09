"use client";

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { TierHeader } from "@/components/arkim/tier-header";
import { VendorCard } from "@/components/arkim/sourcing/vendor-card";
import { OutreachCard } from "@/components/arkim/sourcing/outreach-card";
import { StickyActionBar } from "@/components/arkim/sourcing/sticky-action-bar";
import { QuotesComparison } from "@/components/arkim/comparison/quotes-comparison";
import { OrderPanel } from "@/components/arkim/orders/order-panel";
import { Dot } from "@/components/ui/pill";
import { Warn } from "@/components/ui/icons";
import { useGoferStore } from "@/store";
import { BRAND_NAME } from "@/lib/brand";
import type { SourcingRunDetail } from "@/types";

interface SourcingViewProps {
  run: SourcingRunDetail;
  className?: string;
}

export function SourcingView({ run, className }: SourcingViewProps) {
  const results = run.sourcing_results;
  const setTier3Selection = useGoferStore((s) => s.setTier3Selection);
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

      {run.no_exact_match && (
        <div className="px-4 py-2.5 bg-amber-tint border-b border-amber-line shrink-0 flex items-start gap-2">
          <Warn size={14} className="text-amber-fg mt-0.5 shrink-0" />
          <p className="text-[12px] text-amber-fg">
            No vendors had this exact part number. All candidates below are functionally equivalent alternatives — review specs carefully before purchase.
          </p>
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        {/* Tier 1 — Gofer Network */}
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
            <EmptyTier message={`No ${BRAND_NAME} network partners found for this part.`} />
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

        {/* Supplier quotes — inbound RFQ replies (self-hides until outreach happens) */}
        <QuotesComparison runId={run.id} />

        {/* Order — place + track (self-hides until approved / an order exists) */}
        <OrderPanel run={run} />
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

// Step timings are approximations of typical sourcing duration (~7s observed). Not synced to real
// backend events — this is intentional, see /design/interactions.md for rationale.
const STEP_TIMINGS_MS = [2000, 5000] as const;

const STEPS = [
  {
    label: `Scanning ${BRAND_NAME} Network...`,
    subtext: "Checking onboarded partners for confirmed pricing",
  },
  {
    label: "Checking marketplaces...",
    subtext: "Searching public catalogs for live availability",
  },
  {
    label: "Reaching out to specialists...",
    subtext: "Identifying regional distributors and authorized service brands",
  },
] as const;

function SourcingLoadingState({ className }: { className?: string }) {
  const [stepIndex, setStepIndex] = useState(0);
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    const timers: ReturnType<typeof setTimeout>[] = [];
    STEP_TIMINGS_MS.forEach((ms, i) => {
      timers.push(setTimeout(() => setVisible(false), ms));
      timers.push(setTimeout(() => { setStepIndex(i + 1); setVisible(true); }, ms + 200));
    });
    return () => timers.forEach(clearTimeout);
  }, []);

  const step = STEPS[stepIndex];

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

      <div
        className={cn(
          "flex flex-col items-center gap-1 transition-opacity duration-200",
          visible ? "opacity-100" : "opacity-0",
        )}
      >
        <p className="text-sm text-fg-2">{step.label}</p>
        <p className="text-[12px] text-fg-4 max-w-xs">{step.subtext}</p>
      </div>

      <div className="flex items-center gap-1.5 mt-1">
        {STEPS.map((_, i) => (
          <span
            key={i}
            className={cn(
              "h-1.5 rounded-full transition-all duration-300",
              i === stepIndex
                ? "w-4 bg-blue-fg"
                : i < stepIndex
                  ? "w-1.5 bg-blue-fg opacity-40"
                  : "w-1.5 bg-bg-3",
            )}
          />
        ))}
      </div>
    </div>
  );
}
