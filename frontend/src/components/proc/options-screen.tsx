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

import { useEffect, useId, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useRunLive, useOrderNow, useOrders } from "@/lib/queries";
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
  if (c.purchaseChannel === "marketplace") out.push("Available immediately at this price — Arkim can order it for you now, no quote needed.");
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
  const orderRef = useRef<HTMLDivElement>(null);
  const [whyOpen, setWhyOpen] = useState<Record<string, boolean>>({});
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
          if (res.pending_approval) {
            // Above the approval threshold: there's nothing to track on this options
            // page yet (the order materialises post-approval), so take the user to their
            // dashboard where the run now shows as "Awaiting approval".
            fire("Submitted for approval — track it on your dashboard.");
            router.push("/");
          } else {
            // Sub-threshold: the order exists now and renders below as "Being purchased".
            fire("Arkim is purchasing this for you — track it in Order.");
          }
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

  if (WORKING_PHASES.includes(phase)) {
    return (
      <Shell sub={partLabel} onHome={() => router.push("/")}>
        <Working label="Finding your best options…" sub="Checking the Arkim network, marketplaces, and specialist suppliers — this can take a minute or two." spin />
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
  const awaitingApproval =
    ["pending_first_approval", "pending_second_approval"].includes(phase) && !hasOrder;
  const selectedId = sel?.candidate_id ?? run.selected_candidate?.id;
  const selectedVendor = options.find((c) => c.id === selectedId)?.vendorName;

  function viewStatus() {
    // No per-run view for the pre-order awaiting-approval state → dashboard (reusing the
    // nav-fix destination); otherwise scroll to the OrderSection status/tracking view.
    if (awaitingApproval) router.push("/");
    else orderRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  return (
    <Shell sub={`${partLabel}${specs?.part_number ? ` · ${specs.part_number}` : ""}`} onHome={() => router.push("/")}>
      {committed && (
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
          flexWrap: "wrap", marginBottom: 16, padding: "12px 16px", borderRadius: "var(--r)",
          border: "1px solid var(--accent-line)", background: "var(--accent-fill)",
        }}>
          <div style={{ fontSize: 13.5, color: "var(--text)" }}>
            <ProcIcon name="checkCircle" size={14} />{" "}
            <b>You&apos;ve selected {selectedVendor ?? "this part"}</b>
            {" — "}{awaitingApproval ? "awaiting approval." : "Arkim is purchasing it for you."}
            <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 2 }}>
              A run sources one part — these options are locked. Start a new request to order another.
            </div>
          </div>
          <button className="proc-btn" data-kind="primary" onClick={viewStatus}>View status</button>
        </div>
      )}
      {options.length === 0 ? (
        <Working label="No options found for this part." sub="We couldn't find suppliers for it — a direct call may be the fastest path." />
      ) : (
        <div className="proc-two-col">
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
                      <div className="o-name">{namesSupplier ? c.vendorName : "Available through Arkim"}</div>
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
                      {/* Lead time shown only when a listing/quote backs it; on uncontacted
                          rows it's a hardcoded default, so it's omitted (not shown as fact). */}
                      {!isUncontacted(c) && c.leadTime && <div className="o-ships">{c.leadTime}</div>}
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
                              This selects this part for your run and starts the purchase through Arkim.
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
                            {isMkt ? "Order through Arkim" : "Order"}
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

          <div className="proc-rail">
            <RailPartContext specs={specs} />
          </div>
        </div>
      )}

      {/* Quotes review → confirm, and order place → track (self-hide until relevant) */}
      <QuotesSection runId={runId} />
      <div ref={orderRef}>
        <OrderSection runId={runId} />
      </div>
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
