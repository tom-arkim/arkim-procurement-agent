"use client";

import { useState, useEffect } from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Pill } from "@/components/ui/pill";
import { PnMatch, MatchBar } from "@/components/ui/match";
import { External, Clock, Dollar } from "@/components/ui/icons";
import { useSelectCandidate } from "@/lib/queries";
import type { Candidate } from "@/types";

// ---------------------------------------------------------------------------
// Vendor type label
// ---------------------------------------------------------------------------

const VENDOR_TYPE_LABEL: Record<string, string> = {
  NetworkPartner:        "Network",
  NationalDistributor:   "Marketplace",
  AftermarketCompatible: "Aftermarket",
  AuthorizedDistributor: "OEM Auth",
  RegionalSpecialist:    "Specialist",
  IndustrialSurplus:     "Surplus",
};

// ---------------------------------------------------------------------------
// VendorCard — Tier 1 and Tier 2
// ---------------------------------------------------------------------------

interface VendorCardProps {
  candidate: Candidate;
  runId: string;
  facilityState: string;
  className?: string;
}

export function VendorCard({ candidate, runId, facilityState, className }: VendorCardProps) {
  const select = useSelectCandidate(runId);
  const isCA = facilityState === "CA";
  const hasPrice = candidate.price != null;
  const [showBuyModal, setShowBuyModal] = useState(false);

  useEffect(() => {
    if (!showBuyModal) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setShowBuyModal(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [showBuyModal]);

  const handleBuy = () => {
    if (isCA) {
      setShowBuyModal(true);
    } else {
      window.open(candidate.url, "_blank", "noopener,noreferrer");
    }
  };

  return (
    <div
      className={cn(
        "rounded-card border border-hr-2 bg-bg-3 p-4 flex flex-col gap-3",
        candidate.isOemDirect && "border-blue-line",
        className,
      )}
    >
      {/* Header row */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-1 min-w-0">
          <span className="text-sm font-medium text-fg-1 truncate">
            {candidate.vendorName}
          </span>
          {candidate.loc && (
            <span className="font-mono text-[10px] text-fg-4 uppercase tracking-[0.06em]">
              {candidate.loc}
            </span>
          )}
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          {candidate.isOemDirect && (
            <Pill tone="blue">OEM Direct</Pill>
          )}
          {candidate.isAuthorizedDistributor && !candidate.isOemDirect && (
            <Pill tone="blue">OEM Auth</Pill>
          )}
          {candidate.isAftermarket && (
            <Pill tone="amber">Aftermarket</Pill>
          )}
          <Pill tone="ghost">
            {VENDOR_TYPE_LABEL[candidate.vendorType] ?? candidate.vendorType}
          </Pill>
        </div>
      </div>

      {/* Price + lead time */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-1.5">
          <Dollar size={13} className="text-fg-4 shrink-0" />
          {hasPrice ? (
            <span className="font-mono text-[15px] font-medium text-fg-1 tabular-nums">
              ${candidate.price!.toLocaleString("en-US", { minimumFractionDigits: 2 })}
            </span>
          ) : (
            <span className="font-mono text-[12px] text-fg-3 uppercase tracking-[0.06em]">
              Quote Required
            </span>
          )}
        </div>

        <div className="flex items-center gap-1 text-fg-3">
          <Clock size={12} className="shrink-0" />
          <span className="font-mono text-[11px]">{candidate.leadTime}</span>
        </div>
      </div>

      {/* PN match + suitability */}
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <PnMatch level={candidate.pnMatchLevel} />
          {candidate.relationship && (
            <Pill tone="green">{candidate.relationship}</Pill>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.06em] text-fg-4 shrink-0 w-16">
            Suitability
          </span>
          <MatchBar value={candidate.suitability} label={`${Math.round(candidate.suitability)}%`} />
        </div>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2 pt-1">
        <div className="flex flex-col gap-1 flex-1">
          <Button
            variant={candidate.tier === 1 ? "primary" : "secondary"}
            size="sm"
            className="w-full"
            loading={select.isPending}
            onClick={handleBuy}
          >
            {hasPrice ? "Buy Now" : "Request Quote"}
          </Button>
          <span className="font-mono text-[9px] uppercase tracking-[0.08em] text-fg-4 text-center">
            {isCA ? "Procured through Arkim" : "Visit vendor · your procurement"}
          </span>
        </div>

        {candidate.url && (
          <Button
            variant="ghost"
            size="sm"
            className="shrink-0"
            onClick={() => window.open(candidate.url, "_blank", "noopener,noreferrer")}
            title="Open vendor page"
          >
            <External size={13} />
          </Button>
        )}
      </div>

      {showBuyModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
          onClick={() => setShowBuyModal(false)}
          role="dialog"
          aria-modal="true"
          aria-labelledby="buy-modal-title"
        >
          <div
            className="bg-bg-1 border border-hr-2 rounded-card p-6 max-w-md w-full mx-4 flex flex-col gap-4"
            onClick={(e) => e.stopPropagation()}
          >
            <p id="buy-modal-title" className="font-mono text-sm font-bold text-fg-1">
              Buy Now via Arkim
            </p>
            <p className="font-mono text-xs text-fg-3 leading-relaxed">
              Procurement transactions through Arkim will be available once our
              merchant-of-record infrastructure goes live. For now, this records your
              candidate selection and advances the run to approval. Your facility can
              complete the purchase using your existing procurement process; Arkim will
              handle this end-to-end at launch.
            </p>
            <div className="flex gap-2 justify-end">
              <Button variant="secondary" size="sm" onClick={() => setShowBuyModal(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                size="sm"
                loading={select.isPending}
                onClick={() => {
                  select.mutate({ candidate_id: candidate.id, tier: candidate.tier });
                  setShowBuyModal(false);
                }}
              >
                Continue to approval
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
