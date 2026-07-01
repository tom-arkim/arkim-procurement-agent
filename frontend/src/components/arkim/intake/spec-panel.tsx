"use client";

import { useState, useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import { AssetPanel } from "@/components/arkim/asset-panel";
import { Button } from "@/components/ui/button";
import { Dot } from "@/components/ui/pill";
import { useConfirmIntake } from "@/lib/queries";
import type { AssetSpecs, SourcingRunDetail } from "@/types";

interface SpecPanelProps {
  run: SourcingRunDetail;
  className?: string;
}

export function SpecPanel({ run, className }: SpecPanelProps) {
  const specs = run.asset_specs as AssetSpecs | null | undefined;
  const hasSpecs = Boolean(specs && (specs.manufacturer || specs.part_number));
  const showConfirmCard = run.phase === "intake" && hasSpecs;

  const [dismissed, setDismissed] = useState(false);

  // Reset dismiss when identifying fields change so a follow-up message
  // that updates the manufacturer or PN re-surfaces the card.
  const fingerprint = specs
    ? `${specs.manufacturer}|${specs.part_number}|${specs.manufacturer_confidence}`
    : null;
  const prevFingerprintRef = useRef(fingerprint);
  useEffect(() => {
    if (fingerprint && fingerprint !== prevFingerprintRef.current) {
      prevFingerprintRef.current = fingerprint;
      setDismissed(false);
    }
  }, [fingerprint]);

  return (
    <div className={cn("flex flex-col gap-4 overflow-y-auto p-4", className)}>
      <div className="section-cap">
        Asset Specs
        <span className="rule" />
      </div>

      {showConfirmCard && !dismissed ? (
        <ConfirmCard
          runId={run.id}
          specs={specs!}
          onDismiss={() => setDismissed(true)}
        />
      ) : specs ? (
        <AssetPanel specs={specs} defaultExpanded />
      ) : (
        <ExtractingSkeleton phase={run.phase} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ConfirmCard
// ---------------------------------------------------------------------------

interface ConfirmCardProps {
  runId: string;
  specs: AssetSpecs;
  onDismiss: () => void;
}

function ConfirmCard({ runId, specs, onDismiss }: ConfirmCardProps) {
  const confirm = useConfirmIntake(runId);
  const lowConf = specs.manufacturer_confidence < 70;

  return (
    <div className="flex flex-col gap-3">
      {/* Manufacturer — primary verification target, always prominent.
          Amber styling fires when confidence < 70 as an additional signal;
          it is not the only visual cue — the field is front-and-center every time. */}
      <div
        className={cn(
          "rounded-card border px-4 py-3 flex flex-col gap-1",
          lowConf
            ? "border-amber-line bg-amber-tint"
            : "border-hr-2 bg-bg-2",
        )}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.10em] text-fg-3">
            Manufacturer
          </span>
          <span
            className={cn(
              "font-mono text-[11px] tabular-nums",
              lowConf ? "text-amber-fg" : "text-fg-3",
            )}
          >
            {specs.manufacturer_confidence}%
          </span>
        </div>

        <span
          className={cn(
            "font-mono text-[15px] font-medium leading-tight tracking-[-0.01em]",
            lowConf ? "text-amber-fg" : "text-fg-1",
          )}
        >
          {specs.manufacturer || "—"}
        </span>

        {lowConf && (
          <p className="mt-0.5 text-[11.5px] text-amber-fg leading-snug">
            Confidence below threshold — verify before confirming.
          </p>
        )}
      </div>

      {/* Secondary fields */}
      {(() => {
        const specBased = specs.spec_based_sourcing === true;
        return (
          <div className="rounded-card border border-hr-2 bg-bg-2 divide-y divide-hr-2">
            <SpecRow label="Model" value={specs.model || "—"} specBased={specBased} />
            <SpecRow label="Part No." value={specs.part_number || "—"} mono specBased={specBased} />
            {(specs.detected_type || specs.category) && (
              <SpecRow
                label="Type"
                value={specs.detected_type || specs.category || "—"}
              />
            )}
          </div>
        );
      })()}

      {/* Actions — equal weight: both choices are equally valid */}
      <div className="flex gap-2">
        <Button
          variant="secondary"
          size="md"
          className="flex-1"
          disabled={confirm.isPending}
          onClick={onDismiss}
        >
          Edit / Continue Chat
        </Button>
        <Button
          variant="secondary"
          size="md"
          className="flex-1"
          loading={confirm.isPending}
          onClick={() => confirm.mutate()}
        >
          Confirm &amp; Source
        </Button>
      </div>

      {confirm.isError && (
        <p className="text-[11px] text-red-fg text-center">
          Failed to confirm — is the backend running?
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// SpecRow — dot-leader label / value pair
// ---------------------------------------------------------------------------

interface SpecRowProps {
  label: string;
  value: string;
  mono?: boolean;
  specBased?: boolean;
}

function SpecRow({ label, value, mono, specBased }: SpecRowProps) {
  return (
    <div className="px-4 py-3">
      <div className="leader">
        <span className="lbl">{label}</span>
        <span className="dots" />
        {specBased ? (
          <span className="val text-fg-4 italic">By spec</span>
        ) : (
          <span className={cn("val", mono && "font-mono")}>{value}</span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ExtractingSkeleton
// ---------------------------------------------------------------------------

function ExtractingSkeleton({ phase }: { phase: string }) {
  const isProcessing = ["inventory", "sourcing"].includes(phase);

  return (
    <div className="rounded-card border border-hr-2 bg-bg-2 p-4 flex flex-col gap-4">
      <div className="flex items-center gap-2">
        <Dot tone={isProcessing ? "blue" : "ghost"} pulse={isProcessing} />
        <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-fg-3">
          {isProcessing ? "Extracting specs…" : "Awaiting description"}
        </span>
      </div>

      <div className="flex flex-col gap-2">
        {[80, 65, 55, 70, 45].map((w, i) => (
          <div
            key={i}
            className="h-2.5 rounded bg-bg-3"
            style={{ width: `${w}%`, opacity: 1 - i * 0.12 }}
          />
        ))}
      </div>

      {!isProcessing && (
        <p className="text-fg-4 text-[12px] leading-relaxed">
          Describe the part in the chat or attach a nameplate photo — specs will
          appear here as they&apos;re extracted.
        </p>
      )}
    </div>
  );
}
