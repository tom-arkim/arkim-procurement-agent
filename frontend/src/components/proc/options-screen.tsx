"use client";

/**
 * OptionsScreen — "Here are your best options" (frontend spec §3 / proc-options.jsx),
 * ported to the proc design and wired to the real sourcing results.
 *
 * Source of truth: GET /api/runs/{id} (useRunLive) — sourcing_results candidates +
 * asset_specs. Decision-first: leads with the recommendation, plain language (no
 * Tier/suitability jargon), exact-vs-equivalent labelled, honest "get a quote" when a
 * price isn't on file. Act-on flows (Order / Get-quote) land in the next increments;
 * this screen is the read-and-decide surface.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useRunLive } from "@/lib/queries";
import { ProcIcon } from "./proc-icon";
import { ProcHead, ArkimLoader, procMoney } from "./proc-ui";
import { useProcToast } from "./proc-shell";
import { QuotesSection } from "./quotes-section";
import { OrderSection } from "./order-section";
import type { AssetSpecs, Candidate, ComparisonArtifact, Phase } from "@/types";

const SOURCED_PHASES: Phase[] = [
  "comparison", "pending_first_approval", "pending_second_approval", "approved",
  "executing", "fulfilling", "completed",
];
const WORKING_PHASES: Phase[] = ["intake", "inventory", "sourcing"];

function isExact(c: Candidate): boolean {
  return Boolean(c.isExactMatch) || c.pnMatchLevel === "exact" || c.pnMatchLevel === "normalized";
}

/** Evidence state drives the claim. Prefer the backend's explicit evidenceState;
 *  fall back to price presence for older payloads. ("quoted" is a later increment.) */
function isUncontacted(c: Candidate): boolean {
  return (c.evidenceState ?? (c.price != null ? "priced" : "uncontacted")) === "uncontacted";
}

function whyBullets(c: Candidate, manufacturer?: string): string[] {
  const out: string[] = [];
  if (isUncontacted(c)) {
    // No part-match / price / quote evidence for an uncontacted supplier — selection
    // rationale ONLY. Never assert an exact-PN or cross-reference match here.
    if (c.isAuthorizedDistributor) out.push(`Authorized distributor${manufacturer ? ` for ${manufacturer}` : ""}.`);
    else if (c.isOemDirect) out.push("Sells direct from the manufacturer.");
    else out.push("Specialist supplier selected for this part category.");
    const region = c.shipFrom || c.loc;
    if (region) out.push(`Based in ${region}.`);
    out.push("Pricing and exact-part confirmation come from the supplier when they quote.");
    return out;
  }
  // Priced — a real listing backs the claim.
  out.push(isExact(c)
    ? "Matches the exact part number on your equipment record."
    : "Functionally equivalent alternative per the manufacturer cross-reference — review the spec before purchase.");
  if (c.foundPartNumber) out.push(`Listed for part ${c.foundPartNumber}.`);
  if (c.priceUnverified) out.push("Price auto-extracted at low confidence — confirm it with the vendor before ordering.");
  if (c.priceVerified === false) out.push("Limited price data — treat the listed price as indicative.");
  if (c.comparisonArtifact?.engineerNotes) out.push(c.comparisonArtifact.engineerNotes);
  return out;
}

function recReason(c: Candidate): string {
  if (isUncontacted(c)) return "Best-matched supplier — request a quote.";
  const bits: string[] = [];
  if (c.stock?.toLowerCase().includes("stock")) bits.push("in stock");
  if (c.leadTime) bits.push(c.leadTime.toLowerCase());
  if (c.price != null && !c.priceUnverified) bits.push("best price");
  return bits.length ? bits.join(", ") + "." : "Best match for your part.";
}

export function OptionsScreen({ runId }: { runId: string }) {
  const router = useRouter();
  const fire = useProcToast();
  const { data: run, isLoading, isError } = useRunLive(runId);
  const [whyOpen, setWhyOpen] = useState<Record<string, boolean>>({});

  if (isLoading) return <Shell><Working label="Loading…" sub="Fetching your request." /></Shell>;
  if (isError || !run) return <Shell><Working label="Couldn't load this request" sub="Is the backend running?" loud /></Shell>;

  const phase = run.phase as Phase;
  const specs = run.asset_specs;
  const partLabel = [specs?.manufacturer, specs?.model || specs?.part_number].filter(Boolean).join(" ") || "your part";

  if (WORKING_PHASES.includes(phase)) {
    return (
      <Shell sub={partLabel} onHome={() => router.push("/")}>
        <Working label="Finding your best options…" sub="Checking the Arkim network, marketplaces, and specialist suppliers." spin />
      </Shell>
    );
  }
  if (!SOURCED_PHASES.includes(phase)) {
    return (
      <Shell sub={partLabel} onHome={() => router.push("/")}>
        <Working label="This request isn't ready for options yet." sub="Describe the part and start sourcing first." />
      </Shell>
    );
  }

  const sr = run.sourcing_results;
  const options: Candidate[] = [...(sr?.tier1 ?? []), ...(sr?.tier2 ?? []), ...(sr?.tier3 ?? [])];
  const priced = options.filter((c) => c.price != null);
  const recId = run.selected_candidate?.id ?? priced[0]?.id ?? options[0]?.id;

  return (
    <Shell sub={`${partLabel}${specs?.part_number ? ` · ${specs.part_number}` : ""}`} onHome={() => router.push("/")}>
      {options.length === 0 ? (
        <Working label="No options found for this part." sub="We couldn't find suppliers for it — a direct call may be the fastest path." />
      ) : (
        <div className="proc-two-col">
          <div className="proc-opts">
            {options.map((c) => {
              const rec = c.id === recId;
              const exact = isExact(c);
              return (
                <div key={c.id} className="proc-opt" data-rec={rec}>
                  {rec && (
                    <div className="rec-band">
                      <ProcIcon name="spark" size={13} />Recommended
                      <span style={{ opacity: 0.5, margin: "0 2px" }}>·</span>
                      <span className="reason">{recReason(c)}</span>
                    </div>
                  )}
                  <div className="o-body">
                    <div className="o-tt">
                      <div className="o-name">{c.vendorName}</div>
                      {c.loc && <div className="o-part">{c.loc}</div>}
                      <div className="o-tags">
                        <span className="o-tag" data-kind={exact ? "exact" : "equiv"}>
                          {exact
                            ? <><ProcIcon name="checkCircle" size={12} />Exact replacement</>
                            : <><ProcIcon name="refresh" size={12} />Equivalent alternative</>}
                        </span>
                        {c.stock && <span className="o-tag" data-kind="stock">{c.stock}</span>}
                      </div>
                    </div>
                    <div className="o-price">
                      {c.price != null
                        ? (c.priceUnverified
                            ? <>
                                <div className="o-num" style={{ opacity: 0.8 }}>≈{procMoney(c.price)}</div>
                                <div className="o-ships" style={{ color: "var(--st-overdue)" }}>price unverified — confirm with vendor</div>
                              </>
                            : <div className="o-num">{procMoney(c.price)}</div>)
                        : <div className="o-num"><span className="q">Get a quote</span></div>}
                      {/* Lead time shown only when a listing backs it; on uncontacted rows
                          it's a hardcoded default, so it's omitted (not shown as fact). */}
                      {!isUncontacted(c) && c.leadTime && <div className="o-ships">{c.leadTime}</div>}
                    </div>
                    <div className="o-act">
                      <button
                        className="proc-btn"
                        data-kind="primary"
                        onClick={() => fire(c.price != null ? "Ordering — coming in the next build" : "Quote request — coming in the next build")}
                      >
                        {c.price != null ? "Order" : "Get quote"}
                      </button>
                      <button className="o-why" onClick={() => setWhyOpen((s) => ({ ...s, [c.id]: !s[c.id] }))}>
                        Why?<ProcIcon name={whyOpen[c.id] ? "chevD" : "chevR"} size={12} />
                      </button>
                    </div>
                  </div>
                  {whyOpen[c.id] && (
                    <div className="o-whybody">
                      {whyBullets(c, specs?.manufacturer).map((w, i) => (
                        <div key={i} className="wb-row"><span className="d" /><span>{w}</span></div>
                      ))}
                      {!isUncontacted(c) && c.url && (
                        <div className="wb-row"><span className="d" /><span>
                          <a href={c.url} target="_blank" rel="noopener noreferrer"
                             style={{ color: "var(--accent)", textDecoration: "underline" }}>View listing ↗</a>
                        </span></div>
                      )}
                      {!isUncontacted(c) && c.comparisonArtifact && <SpecMatch artifact={c.comparisonArtifact} />}
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          <div className="proc-rail">
            <RailPartContext specs={specs} />
          </div>
        </div>
      )}

      {/* Quotes review → confirm, and order place → track (self-hide until relevant) */}
      <QuotesSection runId={runId} />
      <OrderSection runId={runId} />
    </Shell>
  );
}

// ---------------------------------------------------------------------------

function Shell({ children, sub, onHome }: { children: React.ReactNode; sub?: string; onHome?: () => void }) {
  return (
    <div className="proc-max">
      {onHome && (
        <button className="proc-back" onClick={onHome}>
          <span style={{ display: "inline-flex", transform: "rotate(180deg)" }}>
            <ProcIcon name="chevR" size={14} />
          </span>
          Home
        </button>
      )}
      <ProcHead title={<>Here are your <b>best options</b></>} sub={sub} />
      {children}
    </div>
  );
}

function Working({ label, sub, spin, loud }: { label: string; sub?: string; spin?: boolean; loud?: boolean }) {
  return (
    <div className="proc-working">
      {spin ? <ArkimLoader size={36} /> : <ProcIcon name={loud ? "alert" : "box"} size={20} color={loud ? "var(--st-overdue)" : "var(--muted)"} />}
      <div>
        <div className="w-t">{label}</div>
        {sub && <div className="w-s">{sub}</div>}
      </div>
    </div>
  );
}

function SpecMatch({ artifact }: { artifact: ComparisonArtifact }) {
  // Only render fields we actually extracted a candidate value for — render nothing
  // (not a fabricated row) when the comparison has no real candidate data.
  const rows = (artifact.comparison ?? []).filter((f) => f.candidateValue);
  if (rows.length === 0) return null;
  return (
    <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px solid var(--border)" }}>
      <div style={{ fontSize: 11, color: "var(--muted-2)", marginBottom: 4 }}>
        Spec match · {artifact.fidelity} confidence — verify against your unit before ordering
      </div>
      {rows.map((f) => (
        <div key={f.field} style={{ display: "flex", justifyContent: "space-between", gap: 8, fontSize: 12, color: "var(--muted)" }}>
          <span>{f.fieldLabel}</span><span>{f.assetValue ?? "—"} → {f.candidateValue}</span>
        </div>
      ))}
    </div>
  );
}

function RailPartContext({ specs }: { specs?: AssetSpecs }) {
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
