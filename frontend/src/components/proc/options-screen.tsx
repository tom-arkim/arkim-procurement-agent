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
import { ProcHead, SecHead, procMoney } from "./proc-ui";
import { composeFindings, registrableDomain } from "./options-compose";
import { GoferLoader } from "@/components/ui/gofer-loader";
import { useProcToast } from "./proc-shell";
import { QuotesSection } from "./quotes-section";
import { OrderSection } from "./order-section";
import type { Candidate, ComparisonArtifact, Phase, BasketRunRow, OutreachTarget } from "@/types";
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
    // Night 11: a structured quote carries its own confirmation date — the
    // card says WHEN the supplier confirmed. Email-parsed quotes keep their
    // exact pre-Night-11 line (no date field ⇒ no copy change).
    out.push(c.quoteUnverified
      ? `${supplier} sent a quote — we read the price with low confidence, so confirm the figure before ordering.`
      : c.quoteConfirmedAt
        ? `${supplier} confirmed this price and lead time in a quote on ${c.quoteConfirmedAt.slice(0, 10)}.`
        : `${supplier} confirmed this price, lead time, and terms in a quote.`);
    if (c.terms) out.push(`Terms: ${c.terms}.`);
    // The wrong-part gate's labelling half (criterion 4): a review-approved
    // alternative is presented as the QUOTED part number with equivalent
    // framing — never silently as the requested PN.
    if (c.pnDiffers && c.quotedPartNumber) {
      out.push(`Quoted as part ${c.quotedPartNumber} — an equivalent alternative to the requested part, confirmed by our team.`);
    } else {
      out.push(isExact(c)
        ? "Matches the exact part number on your equipment record."
        : "Functionally equivalent alternative per the manufacturer cross-reference — review the spec before purchase.");
    }
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
  // Priced — a real listing backs the claim. The availability claim must match the
  // price evidence: an indicative/unverified price can never claim "at this price".
  const vendor = c.vendorName || "the seller";
  const priceIndicative = Boolean(c.priceUnverified) || c.priceVerified === false;
  if (c.purchaseChannel === "marketplace") {
    out.push(priceIndicative
      ? `Available now at ${vendor} — the listed price is indicative; ${BRAND_NAME} confirms the final price before you're charged.`
      : `Available now at ${vendor} — ${BRAND_NAME} can order it for you at this price, no quote needed.`);
  }
  out.push(isExact(c)
    ? "Matches the exact part number on your equipment record."
    : "Functionally equivalent alternative per the manufacturer cross-reference — review the spec before purchase.");
  if (c.foundPartNumber) out.push(`Listed for part ${c.foundPartNumber}.`);
  // One price caveat, never stacked: the low-confidence extraction explains WHY the
  // price is indicative; limited-price-data is the fallback qualifier.
  if (c.priceUnverified) out.push(`We read this price from the listing at low confidence — treat it as indicative until ${BRAND_NAME} confirms it with the seller.`);
  else if (c.priceVerified === false) out.push("Limited price data — treat the listed price as indicative.");
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

  if (isLoading) return <Shell><Working label="Opening this request…" sub="Fetching the latest status." spin /></Shell>;
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
        <SourcingProgress phase={phase} />
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
  // RANKING_BANDS_V1: a banded payload carries findings[] (Band A/B evidence cards,
  // already in banded order) + outreachTargets (Band C — who we're ASKING, not what
  // we found). Detected from the payload itself: the backend emits these keys only
  // for results carrying the ranking_bands:v1 marker — never an env read here.
  // Flag-off / legacy results have no findings key → the tier-array path below is
  // byte-identical to before.
  const banded = Array.isArray(sr?.findings);
  const options: Candidate[] = banded
    ? (sr?.findings ?? [])
    : [...(sr?.tier1 ?? []), ...(sr?.tier2 ?? []), ...(sr?.tier3 ?? [])];
  // Band C = suppliers we intend to ASK (no candidate-specific evidence) — rendered
  // as the outreach status block below the findings, never as option cards.
  const outreachSuppliers = (banded ? sr?.outreachTargets?.suppliers : undefined) ?? [];
  const hasOutreach = outreachSuppliers.length > 0;
  const priced = options.filter((c) => c.price != null);
  const recId = run.selected_candidate?.id ?? priced[0]?.id ?? options[0]?.id;
  // Render-time composition (banded payloads only — brief §2.3): collapse
  // same-domain duplicates onto one card, then group buy-now apart from
  // quote-needed. Pure presentation; server band order preserved within groups.
  // Legacy payloads keep the flat tier-array list untouched.
  const composed = banded ? composeFindings(sr?.findings ?? []) : null;
  // Distinct cards actually painted (post-dedup) — drives counts and the
  // thin-results outreach placement.
  const displayCount = composed
    ? composed.buyNow.length + composed.quoteNeeded.length
    : options.length;

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

  // One card — the working decision surface. Shared by the banded (grouped,
  // deduped) and legacy (flat) lists so there is exactly one card renderer.
  // `alsoListed` = same-domain duplicates collapsed behind an affordance.
  const renderCard = (c: Candidate, alsoListed: Candidate[] = []) => {
              const rec = c.id === recId;
              const exact = isExact(c);
              const isMkt = c.purchaseChannel === "marketplace";  // State M: a buyable price now
              const quoted = isQuoted(c);                          // State C: a confirmed quote
              // Composition: the CLAIM ladder (C > M) vs the ACTION (channel) are orthogonal.
              // A marketplace+quote row shows "Supplier-confirmed" (claim) but keeps "Order
              // through Arkim" (action). The unverified qualifier is the QUOTE's confidence
              // when quoted, the listing's otherwise — composes, never masked.
              const unverified = quoted ? Boolean(c.quoteUnverified) : Boolean(c.priceUnverified);
              return (
                <div key={c.id} className="proc-opt" data-rec={rec && !quoted} data-conf={quoted}>
                  {/* State C band — a supplier-confirmed quote is the platform's strongest,
                      proudest claim: it outranks (and replaces) the Recommended band. Every
                      figure in it is the quote's own; an unverified-figure quote states the
                      confirmation without asserting the number. */}
                  {quoted ? (
                    <div className="conf-band">
                      <ProcIcon name="checkCircle" size={13} />Confirmed by supplier
                      <span style={{ opacity: 0.5, margin: "0 2px" }}>·</span>
                      <span className="reason">
                        {c.vendorName} confirmed{!c.quoteUnverified && c.price != null ? ` ${procMoney(c.price)}` : " this quote"}
                        {c.leadTime ? ` · ${c.leadTime.toLowerCase()}` : ""}
                        {c.quoteConfirmedAt ? ` · on ${c.quoteConfirmedAt.slice(0, 10)}` : ""}
                      </span>
                    </div>
                  ) : rec ? (
                    <div className="rec-band">
                      <ProcIcon name="spark" size={13} />Recommended
                      <span style={{ opacity: 0.5, margin: "0 2px" }}>·</span>
                      <span className="reason">{recReason(c)}</span>
                    </div>
                  ) : null}
                  <div className="o-body">
                    <div className="o-tt">
                      {/* The headline is ALWAYS the actual seller. Gofer is a buying agent
                          working FOR the user, not a reseller: merchant-of-record is
                          expressed on the action ("Order through Gofer") and the fulfilment
                          sub-line — it never replaces the seller's identity. */}
                      <div className="o-name">{c.vendorName}</div>
                      {/* Structural part evidence: the listing's actual PN is the buyer's
                          proof the seller has THIS part — first-class, not buried prose. */}
                      {(c.foundPartNumber || c.loc) && (
                        <div className="o-part">
                          {[c.foundPartNumber ? `PN ${c.foundPartNumber}` : null, c.loc]
                            .filter(Boolean).join(" · ")}
                        </div>
                      )}
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
                      {/* The evidence link — the buyer's ability to verify the part is
                          correct is the point of the evidence model. Every card with a
                          source listing links it, marketplace and quoted rows included
                          (transparency outranks disintermediation risk — Gofer earns the
                          order by doing the work, not by hiding the source). */}
                      {c.url && (
                        <a
                          className="o-listing"
                          href={c.url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          View listing ↗
                        </a>
                      )}
                      {/* Same-domain duplicates, collapsed at render time (brief §2.3) —
                          exposed, never dropped. */}
                      {alsoListed.length > 0 && <AlsoListed also={alsoListed} />}
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
                      {/* State M: in stock now, Arkim orders it — speed/certainty, not channel.
                          The sub-line matches the price evidence: an indicative price never
                          claims "no quote needed" (the figure isn't final). */}
                      {isMkt && !quoted && (
                        <div className="o-mkt">
                          {unverified || c.priceVerified === false
                            ? "Available now · final price confirmed at order"
                            : "Available now · no quote needed"}
                        </div>
                      )}
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
                      {/* The listing link lives structurally on the card (o-listing above)
                          for every row with a source_url — no duplicate here. */}
                      {!isUncontacted(c) && c.comparisonArtifact && <SpecMatch artifact={c.comparisonArtifact} />}
                    </div>
                  )}
                </div>
              );
  };

  // The candidate list — banded payloads paint the composed groups (buy-now apart
  // from quote-needed, headers only when BOTH exist so a single-kind list stays
  // plain); legacy payloads keep the flat tier-order list byte-for-byte.
  const optionsList = composed ? (
    <div className="proc-opts">
      {composed.buyNow.length > 0 && composed.quoteNeeded.length > 0 && (
        <SecHead t="Ready to order" c={composed.buyNow.length} />
      )}
      {composed.buyNow.map((e) => renderCard(e.primary, e.alsoListed))}
      {composed.buyNow.length > 0 && composed.quoteNeeded.length > 0 && (
        <SecHead t="Quote needed" c={composed.quoteNeeded.length} />
      )}
      {composed.quoteNeeded.map((e) => renderCard(e.primary, e.alsoListed))}
    </div>
  ) : (
    <div className="proc-opts">{options.map((c) => renderCard(c))}</div>
  );

  const partRail = (
    <div className="proc-rail">
      <RailPartContext specs={specs} />
    </div>
  );

  // The outreach status block (Band C). Null when there are no targets — no empty
  // shell — and never on legacy/flag-off payloads (hasOutreach is banded-only), so
  // the flag-off render tree is unchanged.
  // Thin results (≤2 cards): the buyer must see "your supplier is being asked"
  // without scrolling, so the block renders compact and ABOVE the findings;
  // rich results keep it below (brief §2.3).
  const findingsThin = displayCount > 0 && displayCount <= 2;
  const outreachBlock = hasOutreach
    ? <OutreachBlock suppliers={outreachSuppliers} manufacturer={specs?.manufacturer} compact={findingsThin} />
    : null;

  // Nothing found but suppliers to ask: a real state — the headline must not claim
  // options exist. (Findings + outreach and the plain legacy states keep the
  // standard headline.)
  const nothingFoundAsking = options.length === 0 && hasOutreach;

  return (
    <Shell
      title={nothingFoundAsking ? <>We&apos;re <b>requesting quotes</b> for this part</> : undefined}
      sub={`${partLabel}${specs?.part_number ? ` · ${specs.part_number}` : ""}`}
      onHome={() => router.push("/")}
      strip={basketStrip}
    >
      {/* Committed: foreground the order status — the decision is made, so the status is
          what the page is about; the shortlist collapses into the record below. Uncommitted:
          the candidate list leads (the working decision surface) and OrderSection stays at
          the bottom (self-hides until there's something to place/track). */}
      {committed && <OrderSection runId={runId} />}

      {options.length === 0 ? (
        nothingFoundAsking ? (
          <div className="proc-two-col">
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <Working
                label="We didn't find this part listed anywhere we searched."
                sub={`Instead, we're requesting quotes from ${outreachSuppliers.length} supplier${outreachSuppliers.length === 1 ? "" : "s"} that should carry it — details below.`}
              />
              {outreachBlock}
            </div>
            {partRail}
          </div>
        ) : (
          <Working label="No options found for this part." sub="We couldn't find suppliers for it — a direct call may be the fastest path." />
        )
      ) : committed ? (
        <div className="proc-two-col" style={{ marginTop: 22 }}>
          {/* The wrapper div exists only when the outreach block does — the flag-off
              committed layout keeps its exact previous DOM. */}
          {outreachBlock ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <CollapsedRecord
                count={displayCount}
                vendor={selectedVendor}
                open={optionsOpen}
                onToggle={() => setOptionsOpen((o) => !o)}
              >
                {optionsList}
              </CollapsedRecord>
              {outreachBlock}
            </div>
          ) : (
            <CollapsedRecord
              count={displayCount}
              vendor={selectedVendor}
              open={optionsOpen}
              onToggle={() => setOptionsOpen((o) => !o)}
            >
              {optionsList}
            </CollapsedRecord>
          )}
          {partRail}
        </div>
      ) : (
        <div className="proc-two-col">
          {outreachBlock ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {findingsThin ? (
                <>
                  {outreachBlock}
                  {optionsList}
                </>
              ) : (
                <>
                  {optionsList}
                  {outreachBlock}
                </>
              )}
            </div>
          ) : (
            optionsList
          )}
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

/** RANKING_BANDS_V1 — the Band-C outreach block: suppliers we intend to ASK, not
 *  findings. Deliberately NOT an option card — no scores, no prices, no order
 *  action, no "recommended" styling, and the copy says plainly that these are
 *  capability matches, not confirmed sources. Onboarded supplier(s) lead, named as
 *  the user's own. The copy claims intent only ("we're asking") — RFQ delivery is a
 *  separate milestone, so nothing here may read as "an email was sent". Renders
 *  nothing for an empty target list (callers also gate — belt and braces). */
function OutreachBlock({ suppliers, manufacturer, compact }: {
  suppliers: OutreachTarget[]; manufacturer?: string; compact?: boolean;
}) {
  const headId = useId();
  if (suppliers.length === 0) return null;
  const onboarded = suppliers.filter((s) => s.onboarded);
  const others = suppliers.filter((s) => !s.onboarded);
  // "Authorized distributors" only when the server's provenance actually says so
  // for every one of them; otherwise the neutral phrasing.
  const othersAuthorized = others.length > 0 &&
    others.every((s) => (s.provenance || "").toLowerCase().includes("authorized distributor"));
  const othersLabel = othersAuthorized
    ? `authorized ${manufacturer ? `${manufacturer} ` : ""}distributor${others.length === 1 ? "" : "s"}`
    : `supplier${others.length === 1 ? "" : "s"} matched to this part category`;
  return (
    <section className="proc-outreach" data-compact={compact || undefined} aria-labelledby={headId}>
      <div className="or-head" id={headId}>
        <ProcIcon name="mail" size={13} />
        Requesting quotes — in progress on your behalf
      </div>
      {onboarded.map((s) => (
        <div className="or-row" key={s.vendorName}>
          <span className="or-yours">Your supplier</span>
          <span className="or-text">
            <b>{s.vendorName}</b> — we&apos;re asking them to confirm availability and price.
          </span>
        </div>
      ))}
      {others.length > 0 && (
        <div className="or-row">
          <span className="or-text">
            {onboarded.length > 0 ? "Also asking" : "Asking"} {others.length} {othersLabel}:{" "}
            <b>{others.map((s) => s.vendorName).join(", ")}</b>.
          </span>
        </div>
      )}
      <div className="or-note">
        These suppliers are matched on what they carry — none has confirmed having this
        exact part yet. Their quotes are that confirmation, and they&apos;ll appear here.
      </div>
    </section>
  );
}

function Shell({ children, sub, onHome, strip, title }: {
  children: React.ReactNode; sub?: string; onHome?: () => void;
  strip?: React.ReactNode; title?: React.ReactNode;
}) {
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
      <ProcHead title={title ?? <>Here are your <b>best options</b></>} sub={sub} />
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

// Run progress is driven by the run's REAL phase — never a timer (brief §2.2:
// no fake progress; real stage labels only). The backend exposes exactly three
// observable stages before options exist: identifying (intake/inventory),
// searching (sourcing), options (comparison+ — the parent unmounts this view).
// No per-tier sourcing events exist server-side, so no per-tier sub-steps are shown.
const RUN_STEPS = ["Identify the part", "Search suppliers & marketplaces", "Options ready"];

/** Prominent sourcing-in-progress screen for the 30–60s live-search wait. Step
 *  states derive from run.phase; a step only reads "done" when the run has
 *  actually left that phase. */
function SourcingProgress({ phase }: { phase: Phase }) {
  const demo = useDemoMode();
  const searching = phase === "sourcing";
  const cur = searching ? 1 : 0;
  const sub = searching
    ? demo
      ? "Searching live across marketplaces and specialist suppliers — this can take up to a minute."
      : `Searching live across the ${BRAND_NAME} network, marketplaces, and specialist suppliers — this can take up to a minute.`
    : "Confirming what the part is from your request.";

  return (
    <div className="proc-loading proc-loading-split">
      <div className="pl-loader"><GoferLoader size={96} /></div>
      <div className="pl-body">
        <div className="pl-head">Finding your best options</div>
        <div className="pl-sub">{sub}</div>
        <ol className="sp-steps">
          {RUN_STEPS.map((label, i) => {
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

/** Human label for a collapsed duplicate listing: its domain + whatever real
 *  evidence it carries (PN, price). Nothing invented — absent fields just omit. */
function alsoLabel(c: Candidate): string {
  const bits = [registrableDomain(c.url) || "listing"];
  if (c.foundPartNumber) bits.push(`PN ${c.foundPartNumber}`);
  if (c.price != null) bits.push(procMoney(c.price));
  return bits.join(" · ");
}

/** "Also listed at N more pages" — the same vendor's other URL granularities,
 *  collapsed at render time (options-compose). Exposed on demand, each with its
 *  own working listing link — collapsed never means dropped. */
function AlsoListed({ also }: { also: Candidate[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="o-also">
      <button type="button" className="o-also-t" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        Also listed at {also.length} more page{also.length === 1 ? "" : "s"}
        <ProcIcon name={open ? "chevD" : "chevR"} size={12} />
      </button>
      {open && (
        <ul className="o-also-list">
          {also.map((c) => (
            <li key={c.id}>
              <a className="o-listing" href={c.url} target="_blank" rel="noopener noreferrer">
                {alsoLabel(c)} ↗
              </a>
            </li>
          ))}
        </ul>
      )}
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

