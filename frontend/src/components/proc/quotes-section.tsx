"use client";

/**
 * QuotesSection — proc-skinned supplier-quote review (frontend spec §4 / proc-quotes.jsx),
 * reusing the same wiring as the internal QuotesComparison (useReviewItems / process-
 * replies / confirm / reject). Mounted on the customer /parts/[id] run view; self-hides
 * until RFQs have gone out.
 *
 * Invariants preserved: the user's explicit "Looks right? Confirm" is the ONLY path that
 * writes price_db (no auto-confirm); honest partial state ("2 of 3 responded") and
 * confidence on each extracted quote; a subtle dismiss; nominated contacts as a small
 * "save this contact?" card; "Check for new replies" runs a live inbox read.
 */

import {
  useReviewItems,
  useProcessReplies,
  useConfirmReviewItem,
  useRejectReviewItem,
} from "@/lib/queries";
import { ProcIcon } from "./proc-icon";
import { procMoney } from "./proc-ui";
import type { ReviewItem } from "@/types";

function relTime(iso?: string): string {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 3600) return `${Math.max(1, Math.floor(diff / 60))}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function QuotesSection({ runId }: { runId: string }) {
  const { data, isLoading } = useReviewItems(runId);
  const process = useProcessReplies(runId);

  if (isLoading || !data) return null;
  const sent = data.sent_count;
  const items = data.review_items;
  if (sent === 0 && items.length === 0) return null; // no outreach yet

  const quotes = items.filter((i) => i.kind === "quote" && i.status !== "rejected");
  const contacts = items.filter(
    (i) => i.kind === "contact" && (i.status === "pending" || i.status === "needs_human_review"),
  );
  const priced = quotes.filter((q) => typeof q.payload.unit_price === "number");
  const recommendedId = priced.length
    ? priced.reduce((b, q) => ((q.payload.unit_price as number) < (b.payload.unit_price as number) ? q : b)).id
    : null;
  const confirmed = quotes.find((q) => q.status === "confirmed");
  const responded = data.quote_count;
  const waiting = Math.max(0, sent - responded);

  return (
    <section style={{ marginTop: 26 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
        <div className="proc-sec-h" style={{ margin: 0 }}>
          <span className="t">Supplier quotes</span>
        </div>
        <button
          className="proc-btn"
          style={{ marginLeft: "auto", padding: "7px 12px", fontSize: 12.5 }}
          disabled={process.isPending}
          onClick={() => process.mutate()}
        >
          <ProcIcon name="refresh" size={13} />
          {process.isPending ? "Checking…" : "Check for new replies"}
        </button>
      </div>

      {process.data && process.data.available === false && (
        <div className="rc-note" style={{ marginBottom: 10 }}>Inbox check unavailable right now.</div>
      )}

      {/* partial state */}
      {responded < sent && !confirmed && (
        <div className="proc-partial">
          <ProcIcon name="mail" size={16} color="var(--accent-text)" />
          <div>
            <span>
              <b>{responded} of {sent} suppliers</b> have responded
            </span>
            <div className="pp-sub">
              {waiting} {waiting === 1 ? "hasn't replied yet" : "haven't replied yet"} — some suppliers never do.
            </div>
          </div>
          <div className="pp-dots">
            {Array.from({ length: sent }).map((_, i) => (
              <i key={i} data-wait={i >= responded} />
            ))}
          </div>
        </div>
      )}

      {/* confirmed banner */}
      {confirmed && (
        <div className="proc-confirmed-banner">
          <ProcIcon name="checkCircle" size={18} color="var(--st-done)" />
          <span style={{ fontSize: 13, color: "var(--text)", fontWeight: 600 }}>
            Quote confirmed — {confirmed.vendor_name} · {procMoney(confirmed.payload.unit_price ?? 0)}. This is your price.
          </span>
        </div>
      )}

      {quotes.length === 0 ? (
        <div className="proc-pending">
          <span style={{ width: 30, height: 30, borderRadius: "var(--r)", border: "1px dashed var(--border)", display: "flex", alignItems: "center", justifyContent: "center", color: "var(--muted-2)" }}>
            <ProcIcon name="mail" size={15} />
          </span>
          <div className="p-tt">
            <div className="p-sup">Waiting on {sent} supplier{sent === 1 ? "" : "s"}</div>
            <div className="p-sub">Quotes will appear here as they reply. Some suppliers don&apos;t respond to every request.</div>
          </div>
        </div>
      ) : (
        <>
          {quotes.map((q) => (
            <QuoteCard key={q.id} runId={runId} item={q} recommended={q.id === recommendedId} otherLocked={Boolean(confirmed) && confirmed?.id !== q.id} />
          ))}
          {waiting > 0 && (
            <div className="proc-pending">
              <span style={{ width: 30, height: 30, borderRadius: "var(--r)", border: "1px dashed var(--border)", display: "flex", alignItems: "center", justifyContent: "center", color: "var(--muted-2)" }}>
                <ProcIcon name="mail" size={15} />
              </span>
              <div className="p-tt">
                <div className="p-sup">{waiting} supplier{waiting === 1 ? "" : "s"} still to reply</div>
                <div className="p-sub">No reply yet — some suppliers don&apos;t respond, especially on small orders.</div>
              </div>
            </div>
          )}
        </>
      )}

      {contacts.map((c) => (
        <ContactCard key={c.id} runId={runId} item={c} />
      ))}
    </section>
  );
}

function QuoteCard({ runId, item, recommended, otherLocked }: { runId: string; item: ReviewItem; recommended: boolean; otherLocked: boolean }) {
  const confirm = useConfirmReviewItem(runId);
  const reject = useRejectReviewItem(runId);
  const p = item.payload;
  const isConfirmed = item.status === "confirmed";
  const lowConfidence = item.status === "needs_human_review" || (item.confidence ?? 1) < 0.6;

  return (
    <div className="proc-quote" data-best={recommended && !isConfirmed} data-locked={isConfirmed} style={{ opacity: otherLocked ? 0.5 : 1 }}>
      <div className="q-head">
        <div style={{ flex: 1 }}>
          <div className="q-sup">{item.vendor_name ?? item.supplier_domain ?? "Supplier"}</div>
          <div className="q-via">Quote · arrived {relTime(item.created_at)}</div>
        </div>
        {recommended && !isConfirmed && <span className="q-tag">Best price</span>}
        {isConfirmed && (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11.5, fontWeight: 700, color: "var(--st-done)", background: "var(--st-done-fill)", borderRadius: 999, padding: "3px 11px" }}>
            <ProcIcon name="checkCircle" size={13} />Confirmed
          </span>
        )}
      </div>
      <div className="q-grid">
        <div className="q-cell"><div className="q-l">Price</div><div className="q-v">{p.unit_price != null ? procMoney(p.unit_price) : "—"}</div></div>
        <div className="q-cell"><div className="q-l">Lead time</div><div className="q-v">{p.lead_time ?? "—"}</div></div>
        <div className="q-cell"><div className="q-l">Min order</div><div className="q-v">{p.min_order ?? "None"}</div></div>
        <div className="q-cell"><div className="q-l">Terms</div><div className="q-v" style={{ fontSize: 13 }}>{p.terms ?? "—"}</div></div>
      </div>
      {!isConfirmed && (
        <div className="q-foot">
          <div className="q-conf" data-conf={lowConfidence ? "check" : "high"}>
            <span className="qc-ic"><ProcIcon name={lowConfidence ? "alert" : "checkCircle"} size={15} /></span>
            <span>
              {lowConfidence
                ? <><b>Worth a glance:</b> double-check the figures before you confirm.</>
                : "Numbers read cleanly from the quote."}
            </span>
            <div className="q-confirmbar">
              <span className="q-looks">Looks right?</span>
              <button className="proc-btn" data-kind="primary" style={{ padding: "6px 12px", fontSize: 12.5 }}
                disabled={confirm.isPending || otherLocked} onClick={() => confirm.mutate(item.id)}>
                <ProcIcon name="checkCircle" size={14} />Confirm
              </button>
              <button className="proc-btn" data-kind="quiet" style={{ padding: "6px 10px", fontSize: 12.5 }}
                disabled={confirm.isPending || reject.isPending} onClick={() => reject.mutate(item.id)}>
                Dismiss
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function ContactCard({ runId, item }: { runId: string; item: ReviewItem }) {
  const confirm = useConfirmReviewItem(runId);
  const reject = useRejectReviewItem(runId);
  const p = item.payload;
  return (
    <div className="q-contact" style={{ border: "1px solid var(--border)", borderRadius: "var(--r-lg)", marginTop: 12 }}>
      <ProcIcon name="user" size={14} />
      <span>
        <b>{p.name ?? "Contact"}</b>
        {p.position ? `, ${p.position}` : ""}
        {p.email ? ` (${p.email})` : ""} — save for future orders?
      </span>
      <button className="qc-save" disabled={confirm.isPending} onClick={() => confirm.mutate(item.id)}>
        <ProcIcon name="checkCircle" size={13} />Save contact
      </button>
      <button className="proc-btn" data-kind="quiet" style={{ padding: "4px 8px", fontSize: 12 }}
        disabled={confirm.isPending || reject.isPending} onClick={() => reject.mutate(item.id)}>
        Dismiss
      </button>
    </div>
  );
}
