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

import { useEffect, useId, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useRunLive, useOrderNow, useOrders, useGroup, useDemoMode } from "@/lib/queries";
import { ProcIcon } from "./proc-icon";
import { ProcHead, procMoney } from "./proc-ui";
import { GoferLoader } from "@/components/ui/gofer-loader";
import { useProcToast } from "./proc-shell";
import { QuotesSection } from "./quotes-section";
import { OrderSection } from "./order-section";
import type { Candidate, ComparisonArtifact, Phase, BasketRunRow } from "@/types";
import { RailPartContext } from "./part-context";
import { BRAND_NAME } from "@/lib/brand";

const SOURCED_PHASES: Phase[] = [
  "comparison", "pending_first_approval", "pending_second_approval", "approved",
  "executing", "fulfilling", "completed",
];
const WORKING_PHASES: Phase[] = ["intake", "inventory", "sourcing"];

function isExact(c: Candidate): boolean {
  return Boolean(c.isExactMatch) || c.pnMatchLevel === "exact" || c.pnMatchLevel === "normalized";
}

/** Evidence state drives the claim. Prefer the backend's explicit evidenceState;
 *  fall back to price presence for older payloads. */
function isUncontacted(c: Candidate): boolean {
  return (c.evidenceState ?? (c.price != null ? "priced" : "uncontacted")) === "uncontacted";
}

/** State C — a human-CONFIRMED RFQ quote, the strongest claim in the ladder. Only a
 *  confirmed quote shows State C (the backend sets both on overlay). */
function isQuoted(c: Candidate): boolean {
  return c.evidenceState === "quoted" && Boolean(c.quoteConfirmed);
}

function whyBullets(c: Candidate, manufacturer?: string): string[] {
  const out: string[] = [];
  if (isQuoted(c)) {
    // State C: the supplier's actual commitment — the strongest claim. The signed-off
    // claim line composes with (never masks) the unverified caveat: even when we read
    // the figure with low confidence, "supplier-confirmed" holds AND the caveat coexists.
    const supplier = c.vendorName || "The supplier";
    out.push(c.quoteUnverified
      ? `${supplier} sent a quote — we read the price with low confidence, so confirm the figure before ordering.`
      : `${supplier} confirmed this price, lead time, and terms in a quote.`);
    if (c.terms) out.push(`Terms: ${c.terms}.`);
    out.push(isExact(c)
      ? "Matches the exact part number on your equipment record."
      : "Functionally equivalent alternative per the manufacturer cross-reference — review the spec before purchase.");
    if (c.comparisonArtifact?.engineerNotes) out.push(c.comparisonArtifact.engineerNotes);
    return out;
  }
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
  // State M: framed as speed/certainty + Arkim fulfils — NOT "go buy at {marketplace}".
  // The marketplace is Arkim's supply source (operational), not the customer's destination.
  if (c.purchaseChannel === "marketplace") out.push(`Available immediately at this price — ${BRAND_NAME} can order it for you now, no quote needed.`);
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
  if (isQuoted(c)) {
    const bits = ["supplier-confirmed"];
    if (c.leadTime) bits.push(c.leadTime.toLowerCase());
    return bits.join(", ") + ".";
  }
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
  const { data: run, isLoading, isError, refetch } = useRunLive(runId);
  const { data: ordersData } = useOrders(runId);
  const [whyOpen, setWhyOpen] = useState<Record<string, boolean>>({});
  // Default-closed accordion for the candidate list once the run is committed (the
  // decision is made — foreground the status, keep the shortlist as a collapsible record).
  const [optionsOpen, setOptionsOpen] = useState(false);
  // "Order" / "Order through Arkim" on any priced candidate: an UNCONDITIONAL confirm
  // step (so a click can't accidentally select/buy), then the order-now call. Buying ⇒
  // selecting; the spend still routes through approval server-side (not exempt).
  const orderNow = useOrderNow(runId);
  const [confirmMktId, setConfirmMktId] = useState<string | null>(null);

  function placeOrderNow(c: Candidate) {
    orderNow.mutate(
      { candidate_id: c.id, tier: c.tier, quantity: 1 },
      {
        onSuccess: (res) => {
          setConfirmMktId(null);
          // Both paths now stay on the run page — OrderSection shows the status below
          // ("Awaiting approval" pre-approval, "Being purchased" once the order exists).
          fire(res.pending_approval
            ? "Submitted for approval — see status below."
            : `${BRAND_NAME} is purchasing this for you — see status below.`);
        },
        onError: () => {
          fire("Couldn't submit the order — please try again.");
          setConfirmMktId(null);
        },
      },
    );
  }

  // Reliable auto-advance: while the run is still being prepared (or phase unknown),
  // poll via the query's own refetch() on a plain interval. This drives the view
  // imperatively (the same call the "Check for results now" button uses), so it
  // populates even when React Query's declarative refetchInterval doesn't propagate
  // in a long-lived dev tab. Stops once the run is sourced (comparison+).
  const runPhase = run?.phase as string | undefined;
  useEffect(() => {
    const preparing = !runPhase || ["intake", "inventory", "sourcing"].includes(runPhase);
    if (!preparing) return;
    const t = setInterval(() => { void refetch(); }, 4000);
    return () => clearInterval(t);
  }, [runPhase, refetch]);

  if (isLoading) return <Shell><Working label="Loading…" sub="Fetching your request." /></Shell>;
  if (isError || !run) return <Shell><Working label="Couldn't load this request" sub="Is the backend running?" loud /></Shell>;

  const phase = run.phase as Phase;
  const specs = run.asset_specs;
  const partLabel = [specs?.manufacturer, specs?.model || specs?.part_number].filter(Boolean).join(" ") || "your part";

  // Basket status strip — only when this run belongs to a multi-part basket (group_id present).
  // Rendered on every loaded state (working + results) so the user can hop between parts and is
  // never stranded. A single-part run (group_id null) mounts no strip and never fetches the group.
  const basketStrip = run.group_id ? <BasketStrip groupId={run.group_id} activeRunId={runId} /> : null;

  if (WORKING_PHASES.includes(phase)) {
    return (
      <Shell sub={partLabel} onHome={() => router.push("/")} strip={basketStrip}>
        <SourcingProgress />
      </Shell>
    );
  }
  if (!SOURCED_PHASES.includes(phase)) {
    return (
      <Shell sub={partLabel} onHome={() => router.push("/")} strip={basketStrip}>
        <Working label="This request isn't ready for options yet." sub="Describe the part and start sourcing first." />
      </Shell>
    );
  }

  const sr = run.sourcing_results;
  const options: Candidate[] = [...(sr?.tier1 ?? []), ...(sr?.tier2 ?? []), ...(sr?.tier3 ?? [])];
  const priced = options.filter((c) => c.price != null);
  const recId = run.selected_candidate?.id ?? priced[0]?.id ?? options[0]?.id;

  // Committed = this run's single selection/order is locked in (buying ⇒ selecting), so
  // the per-candidate order/quote actions must lock. Three signals (mirror the backend
  // guard): a committed phase, an existing order, or a manual-fulfilment selection.
  // The run's selected_candidate is the thin order-now wrapper ({candidate_id, source,
  // fulfilment, ...}), so read those fields loosely.
  const sel = run.selected_candidate as
    (Candidate & { fulfilment?: string; candidate_id?: string }) | undefined;
  const hasOrder = (ordersData?.orders?.length ?? 0) > 0;
  const COMMITTED_PHASES: Phase[] = [
    "pending_first_approval", "pending_second_approval", "approved", "executing",
    "fulfilling", "completed",
  ];
  const committed = COMMITTED_PHASES.includes(phase) || hasOrder || sel?.fulfilment === "manual";
  const selectedId = sel?.candidate_id ?? run.selected_candidate?.id;
  const selectedVendor = options.find((c) => c.id === selectedId)?.vendorName;

  // The candidate list — the working decision surface when uncommitted, the locked,
  // read-only record (collapsed into the accordion) once committed. Defined once so both
  // the committed and uncommitted layouts render the SAME list (no fork).
  const optionsList = (
    <div className="proc-opts">
            {options.map((c) => {
              const rec = c.id === recId;
              const exact = isExact(c);
              const isMkt = c.purchaseChannel === "marketplace";  // State M: a buyable price now
              const quoted = isQuoted(c);                          // State C: a confirmed quote
              // Composition: the CLAIM ladder (C > M) vs the ACTION (channel) are orthogonal.
              // A marketplace+quote row shows "Supplier-confirmed" (claim) but keeps "Order
              // through Arkim" (action). The unverified qualifier is the QUOTE's confidence
              // when quoted, the listing's otherwise — composes, never masked.
              const unverified = quoted ? Boolean(c.quoteUnverified) : Boolean(c.priceUnverified);
              // Name the supplier on a quoted row (the claim names them, and there's no
              // outbound link to bypass Arkim); keep State-M's "Available through Arkim"
              // only for non-quoted marketplace rows.
              const namesSupplier = quoted || !isMkt;
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
                      {/* State M: don't headline the marketplace name — it's Arkim's supply
                          source, not a customer destination. Frame as an Arkim-fulfilled
                          in-stock option; price + match tag differentiate the rows. */}
                      <div className="o-name">{namesSupplier ? c.vendorName : `Available through ${BRAND_NAME}`}</div>
                      {namesSupplier && c.loc && <div className="o-part">{c.loc}</div>}
                      <div className="o-tags">
                        {/* State C is the strongest claim — lead the tags with it (C > M). */}
                        {quoted && (
                          <span className="o-tag" data-kind="quote">
                            <ProcIcon name="checkCircle" size={12} />Supplier-confirmed
                          </span>
                        )}
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
                        ? (unverified
                            ? <>
                                <div className="o-num" style={{ opacity: 0.85 }}>≈{procMoney(c.price)}</div>
                                <UnverifiedNote reason={quoted ? "quote" : "listing"} />
                              </>
                            : <div className="o-num">{procMoney(c.price)}</div>)
                        : <div className="o-num"><span className="q">Get a quote</span></div>}
                      {/* Lead time shown only when a value backs it (the backend nulls
                          placeholder/absent leads). A "defaulted" heuristic is shown but
                          qualified (~ + estimated); extracted/quoted render as fact. */}
                      {!isUncontacted(c) && c.leadTime && (
                        <div className="o-ships" title={c.leadTimeSource === "defaulted" ? "Estimated lead time" : undefined}>
                          {c.leadTimeSource === "defaulted" ? `~${c.leadTime}` : c.leadTime}
                        </div>
                      )}
                      {/* State C: surface the quote's terms alongside price + lead time. */}
                      {quoted && c.terms && <div className="o-terms">{c.terms}</div>}
                      {/* State M: in stock now, Arkim orders it — speed/certainty, not channel. */}
                      {isMkt && !quoted && <div className="o-mkt">Available now · no quote needed</div>}
                    </div>
                    <div className="o-act">
                      {committed ? (
                        // Locked: the run's selection is committed. No order/quote actions;
                        // just mark which candidate was chosen (others show nothing).
                        c.id === selectedId ? (
                          <span style={{ fontSize: 12, fontWeight: 600, color: "var(--accent-text)",
                                         display: "inline-flex", alignItems: "center", gap: 4 }}>
                            <ProcIcon name="checkCircle" size={12} />Selected
                          </span>
                        ) : null
                      ) : c.price != null ? (
                        // Any PRICED candidate orders through Arkim (marketplace OR reference)
                        // via an unconditional confirm step. Price-less rows below → Get quote.
                        confirmMktId === c.id ? (
                          <div style={{ display: "flex", flexDirection: "column", gap: 6, alignItems: "flex-end" }}>
                            <span style={{ fontSize: 11.5, color: "var(--muted)", maxWidth: 210, textAlign: "right", lineHeight: 1.4 }}>
                              This selects this part for your run and starts the purchase through {BRAND_NAME}.
                            </span>
                            <div style={{ display: "flex", gap: 6 }}>
                              <button className="proc-btn" data-kind="quiet" disabled={orderNow.isPending}
                                      onClick={() => setConfirmMktId(null)}>Cancel</button>
                              <button className="proc-btn" data-kind="primary" disabled={orderNow.isPending}
                                      onClick={() => placeOrderNow(c)}>
                                {orderNow.isPending ? "Submitting…" : "Confirm order"}
                              </button>
                            </div>
                          </div>
                        ) : (
                          <button className="proc-btn" data-kind="primary" onClick={() => setConfirmMktId(c.id)}>
                            {isMkt ? `Order through ${BRAND_NAME}` : "Order"}
                          </button>
                        )
                      ) : (
                        <button
                          className="proc-btn"
                          data-kind="primary"
                          onClick={() => fire("Quote request — coming in the next build")}
                        >
                          Get quote
                        </button>
                      )}
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
                      {/* Reference rows link out to verify the price; marketplace (State M)
                          rows do NOT — sending the customer to the marketplace is the
                          disintermediation we're avoiding (Arkim is buyer-of-record).
                          Quoted (State C) rows also don't — the shown figure is the
                          supplier's quote, not that listing's price. */}
                      {!isUncontacted(c) && !isMkt && !quoted && c.url && (
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
  );

  const partRail = (
    <div className="proc-rail">
      <RailPartContext specs={specs} />
    </div>
  );

  return (
    <Shell sub={`${partLabel}${specs?.part_number ? ` · ${specs.part_number}` : ""}`} onHome={() => router.push("/")} strip={basketStrip}>
      {/* Committed: foreground the order status — the decision is made, so the status is
          what the page is about; the shortlist collapses into the record below. Uncommitted:
          the candidate list leads (the working decision surface) and OrderSection stays at
          the bottom (self-hides until there's something to place/track). */}
      {committed && <OrderSection runId={runId} />}

      {options.length === 0 ? (
        <Working label="No options found for this part." sub="We couldn't find suppliers for it — a direct call may be the fastest path." />
      ) : committed ? (
        <div className="proc-two-col" style={{ marginTop: 22 }}>
          <CollapsedRecord
            count={options.length}
            vendor={selectedVendor}
            open={optionsOpen}
            onToggle={() => setOptionsOpen((o) => !o)}
          >
            {optionsList}
          </CollapsedRecord>
          {partRail}
        </div>
      ) : (
        <div className="proc-two-col">
          {optionsList}
          {partRail}
        </div>
      )}

      {/* Quotes review → confirm (self-hides until relevant). */}
      <QuotesSection runId={runId} />
      {!committed && <OrderSection runId={runId} />}
    </Shell>
  );
}

// ---------------------------------------------------------------------------

/** Collapsed record of the candidate shortlist, shown once the run is committed. The
 *  header (a card matching the option cards) subsumes the old lock banner: "{N} suppliers
 *  considered · you chose {vendor}". Default-closed; expanding reveals the full locked,
 *  read-only list (collapsed ≠ hidden). */
function CollapsedRecord({
  count, vendor, open, onToggle, children,
}: {
  count: number; vendor?: string; open: boolean; onToggle: () => void; children: React.ReactNode;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="proc-opt"
        style={{
          width: "100%", textAlign: "left", cursor: "pointer", fontFamily: "var(--font-sans)",
          color: "var(--text)", padding: "16px 17px", display: "flex", alignItems: "center",
          justifyContent: "space-between", gap: 14,
        }}
      >
        <div>
          <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text)" }}>
            {count} supplier{count === 1 ? "" : "s"} considered
            {vendor ? <> · you chose <span style={{ color: "var(--accent-text)" }}>{vendor}</span></> : ""}
          </div>
          <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 3 }}>
            {vendor
              ? "These options are locked to this run — start a new request to order another part."
              : "Expand to review what was considered — locked to this run."}
          </div>
        </div>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12.5, fontWeight: 600, color: "var(--muted-2)", flex: "none" }}>
          {open ? "Hide" : "Review"}
          <ProcIcon name={open ? "chevD" : "chevR"} size={14} />
        </span>
      </button>
      {open && children}
    </div>
  );
}

function Shell({ children, sub, onHome, strip }: { children: React.ReactNode; sub?: string; onHome?: () => void; strip?: React.ReactNode }) {
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
      {strip}
      {children}
    </div>
  );
}

/** Honest per-part status for a basket chip: "picked $X" when a candidate is selected, else a
 *  phase-derived label. No invented richness (the rollup gives phase, not a count). */
function basketRowStatus(row: BasketRunRow): string {
  if (row.error) return "Error";
  if (row.selected_amount > 0) return `Picked ${procMoney(row.selected_amount)}`;
  const byPhase: Record<string, string> = {
    pending_intake: "Identifying", intake: "Identifying", inventory: "Checking stock",
    sourcing: "Sourcing", comparison: "Options ready",
    pending_first_approval: "Needs approval", pending_second_approval: "Needs approval",
    approved: "Approved", executing: "Ordering", fulfilling: "Ordering",
    completed: "Done", cancelled: "Cancelled", error: "Error",
  };
  return byPhase[row.phase ?? ""] ?? "—";
}

/** Basket status strip — shows every part in the group with its real per-part status, the active
 *  part highlighted. Owns useGroup (only mounts when group_id is present). Renders nothing for a
 *  1-run group (not a multi-part basket). Labels/status are the rollup's real values — never guessed. */
function BasketStrip({ groupId, activeRunId }: { groupId: string; activeRunId: string }) {
  const { data: rollup } = useGroup(groupId);
  if (!rollup || rollup.run_count <= 1) return null;
  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", margin: "2px 0 18px" }}>
      {rollup.runs.map((row, i) => {
        const active = row.run_id === activeRunId;
        return (
          <Link
            key={row.run_id ?? i}
            href={`/parts/${row.run_id}`}
            style={{
              display: "inline-flex", flexDirection: "column", gap: 1,
              padding: "6px 12px", borderRadius: "var(--r)", textDecoration: "none",
              border: `1px solid ${active ? "var(--accent-line)" : "var(--border)"}`,
              background: active ? "var(--accent-fill)" : "var(--bg-1)",
              color: active ? "var(--accent-text)" : "var(--fg-2)",
            }}
          >
            <span style={{ fontWeight: 600, fontSize: 13, maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {row.part || "Unidentified part"}
            </span>
            <span style={{ fontSize: 11, color: active ? "var(--accent-text)" : "var(--muted)" }}>
              {basketRowStatus(row)}
            </span>
          </Link>
        );
      })}
    </div>
  );
}

function Working({ label, sub, spin, loud }: { label: string; sub?: string; spin?: boolean; loud?: boolean }) {
  return (
    <div className="proc-working">
      {spin ? <GoferLoader size={36} /> : <ProcIcon name={loud ? "alert" : "box"} size={20} color={loud ? "var(--st-overdue)" : "var(--muted)"} />}
      <div>
        <div className="w-t">{label}</div>
        {sub && <div className="w-s">{sub}</div>}
      </div>
    </div>
  );
}

// The steps mirror the real sourcing tiers. Under DEMO_MODE, Tier 1 (the "Arkim network" catalog)
// is gated off / returns nothing, so we DROP that step + mention — the loader must tell the same
// story the demo actually tells (live search across marketplaces + specialist suppliers). Non-demo
// keeps the full four (Tier 1 runs there). The labels are honest about what runs; the TIMING is
// approximate — the backend holds phase="sourcing" for the whole run and emits no per-tier events,
// so this is a frontend-only progression, NOT backend-synced.
const SOURCING_STEPS_FULL = [
  "Searching the Gofer network",
  "Scanning marketplaces",
  "Checking specialist suppliers",
  "Comparing candidates",
];
const SOURCING_STEPS_DEMO = [
  "Scanning marketplaces",
  "Checking specialist suppliers",
  "Comparing candidates",
];

/** Prominent, alive sourcing-in-progress screen for the 30–60s live-search wait. Advances the
 *  steps on a timer and HOLDS on the last one until results arrive (the parent unmounts this
 *  when the run flips to comparison), so a step never reads "done" before the data is actually in. */
function SourcingProgress() {
  const demo = useDemoMode();
  const steps = demo ? SOURCING_STEPS_DEMO : SOURCING_STEPS_FULL;
  const sub = demo
    ? "Searching live across marketplaces and specialist suppliers — this can take up to a minute."
    : `Searching live across the ${BRAND_NAME} network, marketplaces, and specialist suppliers — this can take up to a minute.`;

  const [step, setStep] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setStep((s) => s + 1), 9000);
    return () => clearInterval(t);
  }, []);
  const cur = Math.min(step, steps.length - 1);   // clamp so a shorter (demo) list holds on its last step

  return (
    <div className="proc-loading proc-loading-split">
      <div className="pl-loader"><GoferLoader size={96} /></div>
      <div className="pl-body">
        <div className="pl-head">Finding your best options</div>
        <div className="pl-sub">{sub}</div>
        <ol className="sp-steps">
          {steps.map((label, i) => {
            const state = i < cur ? "done" : i === cur ? "active" : "pending";
            return (
              <li key={label} className="sp-step" data-state={state}>
                <span className="sp-dot" aria-hidden="true">
                  {state === "done" && <ProcIcon name="checkCircle" size={18} color="var(--accent-text)" />}
                </span>
                <span className="sp-label">{label}</span>
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}

/** Low-confidence price affordance: short "price unverified" label + an info icon
 *  whose tooltip explains it. Shows on hover, keyboard focus, and tap (toggles).
 *  `reason` distinguishes a low-confidence LISTING read from a low-confidence QUOTE
 *  extraction (State C) — the figure source differs, so the explanation does too. */
function UnverifiedNote({ reason = "listing" }: { reason?: "listing" | "quote" }) {
  const [open, setOpen] = useState(false);
  const tipId = useId();
  return (
    <div className="o-unv">
      <span className="o-unv-l">price unverified</span>
      <button
        type="button"
        className="o-unv-i"
        aria-label="Why is this price unverified?"
        aria-describedby={tipId}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        onBlur={() => setOpen(false)}
        onKeyDown={(e) => { if (e.key === "Escape") setOpen(false); }}
      >
        <svg viewBox="0 0 24 24" width={13} height={13} fill="none" stroke="currentColor"
             strokeWidth={1.8} strokeLinecap="round" aria-hidden="true">
          <circle cx="12" cy="12" r="9" /><path d="M12 11v5" /><path d="M12 8h.01" />
        </svg>
      </button>
      <div id={tipId} role="tooltip" className="o-unv-tip" data-open={open}>
        {reason === "quote"
          ? <>The supplier confirmed this quote, but we read the price figure with low
              confidence. Confirm the figure with the supplier — you&apos;ll be charged
              their actual price when the order is placed, not this estimate.</>
          : <>We read this price from the listing but couldn&apos;t verify it with confidence.
              You&apos;ll be charged the supplier&apos;s actual price when the order is confirmed —
              not this estimate.</>}
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

