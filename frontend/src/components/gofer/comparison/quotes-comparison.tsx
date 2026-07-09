"use client";

/**
 * QuotesComparison — the inbound supplier-quote comparison table (frontend spec §4.4).
 *
 * Decision-first, plain language: leads with the recommended (lowest-price) quote,
 * shows honest partial state ("2 of 3 suppliers have responded"), and a per-row
 * "Looks right? Confirm" that locks the quote in as the price (the ONLY UI path that
 * writes price_db, via the confirm endpoint). Nominated contacts surface as a small
 * "save this contact?" card. "Check for new replies" triggers a live inbox read.
 *
 * Structured for re-skin: data/logic here, presentation in small sub-components using
 * house primitives (Button, Pill, ConfidenceIndicator). The Figma design slots in by
 * restyling these pieces — the data wiring stays.
 */

import { type ReactNode } from "react";
import { cn } from "@/lib/utils";
import {
  useReviewItems,
  useProcessReplies,
  useConfirmReviewItem,
  useRejectReviewItem,
} from "@/lib/queries";
import { Button } from "@/components/ui/button";
import { Pill } from "@/components/ui/pill";
import { ConfidenceIndicator } from "@/components/ui/confidence-indicator";
import type { ReviewItem } from "@/types";

function money(v?: number | null, currency = "USD"): string {
  if (v == null) return "—";
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(v);
  } catch {
    return `$${v}`;
  }
}

export function QuotesComparison({
  runId,
  className,
}: {
  runId: string;
  className?: string;
}) {
  const { data, isLoading } = useReviewItems(runId);
  const process = useProcessReplies(runId);

  if (isLoading || !data) return null;

  const sent = data.sent_count;
  const items = data.review_items;
  // Self-hide on runs with no outreach activity (keeps Tier 1/2 buy-path runs clean).
  if (sent === 0 && items.length === 0) return null;

  const quotes = items.filter((i) => i.kind === "quote" && i.status !== "rejected");
  const contacts = items.filter(
    (i) => i.kind === "contact" && (i.status === "pending" || i.status === "needs_human_review"),
  );

  // Recommended = lowest unit price among the quotes received (decision-first).
  const priced = quotes.filter((q) => typeof q.payload.unit_price === "number");
  const recommendedId = priced.length
    ? priced.reduce((best, q) =>
        (q.payload.unit_price as number) < (best.payload.unit_price as number) ? q : best,
      ).id
    : null;

  const responded = data.quote_count;
  const waiting = Math.max(0, sent - responded);

  return (
    <section className={cn("border-t border-hr-2", className)}>
      <header className="flex items-start justify-between gap-3 px-4 py-3 border-b border-hr-2">
        <div>
          <h3 className="text-sm font-medium text-fg-1">Supplier quotes</h3>
          <p className="mt-0.5 text-[12px] text-fg-3">
            {sent > 0
              ? `${responded} of ${sent} supplier${sent === 1 ? "" : "s"} ${
                  responded === 1 ? "has" : "have"
                } responded`
              : "Quotes will appear here as suppliers respond."}
          </p>
        </div>
        <div className="flex flex-col items-end gap-1 shrink-0">
          <Button
            variant="secondary"
            size="sm"
            loading={process.isPending}
            onClick={() => process.mutate()}
          >
            Check for new replies
          </Button>
          {process.data && process.data.available === false && (
            <span className="text-[10.5px] text-fg-4">Inbox check unavailable right now</span>
          )}
        </div>
      </header>

      {quotes.length === 0 ? (
        <div className="px-4 py-6 text-center">
          <p className="text-[12px] text-fg-4">
            {waiting > 0
              ? `Waiting on ${waiting} supplier${waiting === 1 ? "" : "s"} to reply.`
              : "Quotes will appear here as suppliers respond."}
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-left text-fg-4 border-b border-hr-2">
                <Th>Supplier</Th>
                <Th>Price</Th>
                <Th>Lead time</Th>
                <Th>Minimum order</Th>
                <Th>Terms</Th>
                <Th>Confidence</Th>
                <Th className="text-right">{""}</Th>
              </tr>
            </thead>
            <tbody>
              {quotes.map((q) => (
                <QuoteRow
                  key={q.id}
                  runId={runId}
                  item={q}
                  recommended={q.id === recommendedId}
                />
              ))}
            </tbody>
          </table>
          {waiting > 0 && (
            <p className="px-4 py-2 text-[11.5px] text-fg-4 border-t border-hr-2">
              + {waiting} more supplier{waiting === 1 ? "" : "s"} haven&apos;t replied yet.
            </p>
          )}
        </div>
      )}

      {contacts.map((c) => (
        <ContactCard key={c.id} runId={runId} item={c} />
      ))}
    </section>
  );
}

// ---------------------------------------------------------------------------

function Th({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <th
      className={cn(
        "px-4 py-2 font-mono text-[10.5px] font-normal uppercase tracking-[0.06em] whitespace-nowrap",
        className,
      )}
    >
      {children}
    </th>
  );
}

function QuoteRow({
  runId,
  item,
  recommended,
}: {
  runId: string;
  item: ReviewItem;
  recommended: boolean;
}) {
  const confirm = useConfirmReviewItem(runId);
  const reject = useRejectReviewItem(runId);
  const p = item.payload;
  const isConfirmed = item.status === "confirmed";
  const highlight = recommended || isConfirmed;

  return (
    <tr className={cn("border-b border-hr-2/50 align-top", highlight && "bg-green-tint")}>
      <td className="px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="font-medium text-fg-1">
            {item.vendor_name ?? item.supplier_domain ?? "Supplier"}
          </span>
          {recommended && !isConfirmed && (
            <Pill tone="green" solid>
              Best price
            </Pill>
          )}
        </div>
        {recommended && !isConfirmed && (
          <p className="mt-0.5 text-[11px] text-green-fg">Lowest price of the quotes received</p>
        )}
      </td>
      <td className="px-4 py-3 tabular-nums text-fg-1">{money(p.unit_price, p.currency)}</td>
      <td className="px-4 py-3 text-fg-2">{p.lead_time ?? "—"}</td>
      <td className="px-4 py-3 text-fg-2">{p.min_order ?? "—"}</td>
      <td className="px-4 py-3 text-fg-3">{p.terms ?? "—"}</td>
      <td className="px-4 py-3 w-28">
        <ConfidenceIndicator score={Math.round((item.confidence ?? 0) * 100)} />
      </td>
      <td className="px-4 py-3 text-right whitespace-nowrap">
        {isConfirmed ? (
          <Pill tone="green" solid>
            Confirmed
          </Pill>
        ) : (
          <div className="flex items-center justify-end gap-1.5">
            <Button
              variant="success"
              size="sm"
              loading={confirm.isPending}
              onClick={() => confirm.mutate(item.id)}
            >
              Looks right? Confirm
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={confirm.isPending || reject.isPending}
              onClick={() => reject.mutate(item.id)}
            >
              Dismiss
            </Button>
          </div>
        )}
      </td>
    </tr>
  );
}

function ContactCard({ runId, item }: { runId: string; item: ReviewItem }) {
  const confirm = useConfirmReviewItem(runId);
  const reject = useRejectReviewItem(runId);
  const p = item.payload;

  return (
    <div className="m-4 flex items-start justify-between gap-3 rounded border border-blue-line bg-blue-tint px-3 py-2.5">
      <div className="min-w-0">
        <p className="text-[12px] font-medium text-fg-1">Save this contact for future requests?</p>
        <p className="mt-0.5 text-[12px] text-fg-2">
          {p.name ?? "Contact"}
          {p.position ? ` · ${p.position}` : ""}
          {p.email ? ` — ${p.email}` : ""}
          {item.vendor_name ? ` (${item.vendor_name})` : ""}
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        <Button
          variant="primary"
          size="sm"
          loading={confirm.isPending}
          onClick={() => confirm.mutate(item.id)}
        >
          Save contact
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={confirm.isPending || reject.isPending}
          onClick={() => reject.mutate(item.id)}
        >
          Dismiss
        </Button>
      </div>
    </div>
  );
}
