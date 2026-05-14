"use client";

import { useState, useEffect } from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Pill } from "@/components/ui/pill";
import { PnMatch, MatchBar } from "@/components/ui/match";
import { External, Clock, Dollar } from "@/components/ui/icons";
import { useSelectCandidate, useRequestConfirmation } from "@/lib/queries";
import { useArkimStore } from "@/store";
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
  className?: string;
}

export function VendorCard({ candidate, runId, className }: VendorCardProps) {
  const select = useSelectCandidate(runId);
  const requestConfirmation = useRequestConfirmation(runId);
  const markTier1ConfirmSent = useArkimStore((s) => s.markTier1ConfirmSent);
  const confirmSentAt = useArkimStore((s) => s.tier1ConfirmSentAt[runId]?.[candidate.id]);

  const [showBuyModal, setShowBuyModal] = useState(false);

  useEffect(() => {
    if (!showBuyModal) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setShowBuyModal(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [showBuyModal]);

  // Tier 1 three-state logic:
  //   isRequestMode → "Request Confirmation" (confirmationPending=true, not yet sent)
  //   isAwaiting    → "Awaiting response"     (confirmationPending=true, sent via Zustand sentAt)
  //   buy now       → modal → selectCandidate  (confirmationPending=false, or Tier 2)
  const isTier1 = candidate.tier === 1;
  const isRequestMode = isTier1 && candidate.confirmationPending === true && !confirmSentAt;
  const isAwaiting    = isTier1 && candidate.confirmationPending === true && Boolean(confirmSentAt);

  const sentTime = confirmSentAt
    ? new Date(confirmSentAt).toLocaleTimeString("en-US", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : null;

  const handleRequestConfirmation = () => {
    const sentAt = new Date().toISOString();
    requestConfirmation.mutate([candidate.id], {
      onSuccess: () => markTier1ConfirmSent(runId, candidate.id, sentAt),
    });
  };

  const modalSubtitle =
    candidate.tier === 1
      ? "This places a purchase order through your Arkim Network Partner."
      : "This purchases through an open marketplace vendor. Arkim handles the transaction on your behalf.";

  const hasPrice = candidate.price != null;

  return (
    <div
      className={cn(
        "rounded-card border border-hr-2 bg-bg-3 p-4 flex flex-col gap-3",
        candidate.isOemDirect && "border-blue-line",
        isAwaiting && "opacity-60 pointer-events-none",
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
          {isAwaiting && (
            <Pill tone="ghost">Awaiting</Pill>
          )}
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
          {isAwaiting ? (
            <div className="flex flex-col items-center justify-center gap-1 py-2.5 rounded border border-hr-2 bg-bg-2">
              <div className="flex items-center gap-1.5">
                <Clock size={13} className="text-fg-4 shrink-0" />
                <span className="font-mono text-[12px] font-medium text-fg-2">Awaiting response</span>
              </div>
              <span className="font-mono text-[10px] text-fg-4">Sent {sentTime}</span>
            </div>
          ) : (
            <>
              {isRequestMode && (
                <Button
                  variant="primary"
                  size="sm"
                  className="w-full"
                  loading={requestConfirmation.isPending}
                  onClick={handleRequestConfirmation}
                >
                  Request Confirmation
                </Button>
              )}

              {!isRequestMode && (
                <Button
                  variant={candidate.tier === 1 ? "primary" : "secondary"}
                  size="sm"
                  className="w-full"
                  loading={select.isPending}
                  onClick={() => setShowBuyModal(true)}
                >
                  {hasPrice ? "Buy Now" : "Request Quote"}
                </Button>
              )}

              <span className="font-mono text-[9px] uppercase tracking-[0.08em] text-fg-4 text-center">
                {candidate.tier === 1
                  ? "Procured through Arkim"
                  : "Available via marketplace · Arkim purchases"}
              </span>
            </>
          )}
        </div>

        {/* External link: Tier 2 only, not in awaiting state */}
        {!isAwaiting && candidate.tier === 2 && candidate.url && (
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
            <div className="flex flex-col gap-1">
              <p id="buy-modal-title" className="font-mono text-sm font-bold text-fg-1">
                Buy Now via Arkim
              </p>
              <p className="font-mono text-xs text-fg-3">
                {modalSubtitle}
              </p>
            </div>
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
                Confirm Purchase
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
