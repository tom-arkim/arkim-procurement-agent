"use client";

import { useState } from "react";
import { cn } from "@/lib/utils";
import { ChevronDown, ChevronRight } from "@/components/ui/icons";
import { ConfidenceIndicator } from "@/components/ui/confidence-indicator";
import type { AssetSpecs } from "@/types";

interface AssetPanelProps {
  specs: AssetSpecs;
  defaultExpanded?: boolean;
  className?: string;
}

type FieldDef = { label: string; key: keyof AssetSpecs; mono?: boolean };

const ALWAYS_SHOWN: FieldDef[] = [
  { label: "Manufacturer", key: "manufacturer" },
  { label: "Model", key: "model" },
  { label: "Part No.", key: "part_number", mono: true },
  { label: "Category", key: "detected_type" },
];

const EXPANDABLE: FieldDef[] = [
  { label: "HP", key: "hp", mono: true },
  { label: "RPM", key: "rpm", mono: true },
  { label: "Voltage", key: "voltage", mono: true },
  { label: "Frame", key: "frame", mono: true },
  { label: "Shaft", key: "shaft_size", mono: true },
  { label: "Enclosure", key: "enclosure" },
  { label: "Phase", key: "phase" },
  { label: "GPM", key: "gpm", mono: true },
  { label: "PSI", key: "psi", mono: true },
  { label: "Impeller", key: "impeller_size", mono: true },
  { label: "Mech seal", key: "mech_seal" },
  { label: "Material", key: "material_spec" },
  { label: "Bore", key: "bore_diameter", mono: true },
  { label: "Protocol", key: "protocol" },
  { label: "Warranty", key: "warranty_status" },
  { label: "Failure mode", key: "failure_mode" },
  { label: "Asset ID", key: "asset_id", mono: true },
];

export function AssetPanel({ specs, defaultExpanded = false, className }: AssetPanelProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);

  const visibleExtra = EXPANDABLE.filter(({ key }) => {
    const v = specs[key];
    return v != null && v !== "";
  });

  return (
    <div className={cn("rounded-card border border-hr-2 bg-bg-2", className)}>
      {/* Header */}
      <button
        onClick={() => setExpanded((p) => !p)}
        className="flex w-full items-center justify-between px-4 py-3 text-left hover:bg-bg-3 transition-colors rounded-t-card"
      >
        <span className="font-mono text-[10.5px] font-semibold uppercase tracking-[0.10em] text-fg-2">
          Asset Specs
        </span>
        <span className="text-fg-4">
          {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
      </button>

      {/* Always-visible fields */}
      <div className="border-t border-hr-2 px-4 py-3 flex flex-col gap-2">
        {ALWAYS_SHOWN.map(({ label, key, mono }) => {
          const val = specs[key];
          if (val == null || val === "") return null;
          return (
            <LeaderRow key={key} label={label} value={String(val)} mono={mono} />
          );
        })}

        {/* Confidence indicators */}
        <div className="mt-1 flex flex-col gap-1.5">
          <ConfidenceIndicator
            score={specs.manufacturer_confidence}
            label="Mfr confidence"
            showBar
          />
          {specs.part_id_confidence != null && (
            <ConfidenceIndicator
              score={specs.part_id_confidence}
              label="P/N confidence"
              showBar
            />
          )}
        </div>
      </div>

      {/* Expandable fields */}
      {expanded && visibleExtra.length > 0 && (
        <div className="border-t border-hr-2 px-4 py-3 flex flex-col gap-2">
          <p className="font-mono text-[10px] uppercase tracking-[0.10em] text-fg-4 mb-1">
            Spec detail
          </p>
          {visibleExtra.map(({ label, key, mono }) => {
            const val = specs[key];
            if (val == null || val === "") return null;
            return (
              <LeaderRow key={key} label={label} value={String(val)} mono={mono} />
            );
          })}
        </div>
      )}

      {/* Toggle footer */}
      {visibleExtra.length > 0 && (
        <button
          onClick={() => setExpanded((p) => !p)}
          className={cn(
            "flex w-full items-center justify-center gap-1.5 py-2",
            "border-t border-hr-2 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-4",
            "hover:text-fg-2 hover:bg-bg-3 transition-colors",
            expanded ? "rounded-b-card" : "rounded-b-card",
          )}
        >
          {expanded ? (
            <>
              <ChevronDown size={11} />
              Collapse
            </>
          ) : (
            <>
              <ChevronRight size={11} />
              {visibleExtra.length} more fields
            </>
          )}
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// LeaderRow — dot-leader label / value pair
// ---------------------------------------------------------------------------

function LeaderRow({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="leader">
      <span className="lbl">{label}</span>
      <span className="dots" />
      <span className={cn("val", mono && "font-mono")}>{value}</span>
    </div>
  );
}
