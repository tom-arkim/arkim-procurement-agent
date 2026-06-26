"use client";

/**
 * RailPartContext — the part + asset identity card ("what it is"), composed from the run's
 * asset_specs (manufacturer/model/part_number/description). Shared verbatim between the
 * run page (options-screen) and the approval card (approval-context) so both read the same
 * part identity the same way — one composition, not two.
 */

import { ProcIcon } from "./proc-icon";
import type { AssetSpecs } from "@/types";

export function RailPartContext({ specs }: { specs?: AssetSpecs }) {
  if (!specs) return null;
  const name = [specs.manufacturer, specs.model || specs.part_number].filter(Boolean).join(" ") || "Part";
  return (
    <div className="rail-card">
      <div className="rc-head"><ProcIcon name="toolbox" size={13} />Part &amp; asset</div>
      <div className="rc-body">
        <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-strong)", marginBottom: 3 }}>{name}</div>
        {specs.part_number && <div style={{ fontSize: 12, color: "var(--muted)" }}>{specs.part_number}{specs.manufacturer ? ` · ${specs.manufacturer}` : ""}</div>}
        {specs.description && (
          <>
            <div className="rc-divider" />
            <div style={{ fontSize: 12.5, color: "var(--muted)", lineHeight: 1.5 }}>{specs.description}</div>
          </>
        )}
        <div className="rc-note" style={{ marginTop: 8 }}>Identified from your request and equipment records.</div>
      </div>
    </div>
  );
}
