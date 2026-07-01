"use client";

/**
 * OrderPanel — place an order + track it (frontend spec §4.5).
 *
 * Decision-first, plain language, no accidental placement: a review card (part,
 * supplier, price, qty) gated behind a deliberate two-step confirm, then a plain-word
 * status timeline (placed -> confirmed -> shipped -> delivered). Honest about the
 * can't-place-without-price case, tying back to the quotes comparison above.
 *
 * Structured for re-skin: data/logic here, presentation in small sub-components using
 * house primitives. Customer actions are exactly the two the backend exposes ungated:
 * place (execute) and mark delivered; the intermediate supplier transitions are shown
 * as status only (they're advanced elsewhere).
 */

import { useState } from "react";
import { cn } from "@/lib/utils";
import { useOrders, useExecuteOrder, useMarkDelivered } from "@/lib/queries";
import { Button } from "@/components/ui/button";
import { Pill } from "@/components/ui/pill";
import type { Order, OrderStatus, SourcingRunDetail } from "@/types";

const FLOW = ["placed", "confirmed", "shipped", "received"] as const;
type FlowStatus = (typeof FLOW)[number];
const STEP_LABEL: Record<FlowStatus, string> = {
  placed: "Order placed",
  confirmed: "Supplier confirmed",
  shipped: "Shipped",
  received: "Delivered",
};
const isFlowStatus = (s: OrderStatus): s is FlowStatus =>
  (FLOW as readonly OrderStatus[]).includes(s);

function money(v?: number | null, currency = "USD"): string {
  if (v == null) return "price to be confirmed";
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(v);
  } catch {
    return `$${v}`;
  }
}

export function OrderPanel({ run }: { run: SourcingRunDetail }) {
  const runId = run.id;
  const { data, isLoading } = useOrders(runId);
  const execute = useExecuteOrder(runId);

  if (isLoading) return null;

  const orders = data?.orders ?? [];
  const placed = orders.find((o) => isFlowStatus(o.status));
  const draft = orders.find((o) => o.status === "draft");
  const cancelled = orders.find((o) => o.status === "cancelled");
  const canPlace = ["approved", "executing"].includes(run.phase);

  // Self-hide when there's nothing to place and no order on the run.
  if (!placed && !draft && !cancelled && !canPlace) return null;

  // "Placed but no price" — execute captured a draft it couldn't place.
  const noPrice = Boolean(draft) || execute.data?.placed === false;

  return (
    <section className="border-t border-hr-2 px-4 py-4">
      <h3 className="mb-3 text-sm font-medium text-fg-1">Order</h3>
      {placed ? (
        <OrderTracking runId={runId} order={placed} />
      ) : cancelled && !canPlace ? (
        <p className="text-[12px] text-fg-3">This order was cancelled.</p>
      ) : (
        <PlaceOrderCard
          run={run}
          noPrice={noPrice}
          pending={execute.isPending}
          errored={execute.isError}
          onPlace={() => execute.mutate()}
        />
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------

function PlaceOrderCard({
  run,
  noPrice,
  pending,
  errored,
  onPlace,
}: {
  run: SourcingRunDetail;
  noPrice: boolean;
  pending: boolean;
  errored: boolean;
  onPlace: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const sel = run.selected_candidate;
  const specs = run.asset_specs;
  const part =
    [specs?.manufacturer, specs?.model || specs?.part_number].filter(Boolean).join(" ") ||
    "the selected part";
  const supplier = sel?.vendorName ?? "the selected supplier";
  const price = sel?.price;

  if (noPrice) {
    return (
      <div className="rounded border border-amber-line bg-amber-tint px-3 py-2.5">
        <p className="text-[12px] font-medium text-amber-fg">
          We need a confirmed quote before this order can be placed.
        </p>
        <p className="mt-0.5 text-[12px] text-fg-2">
          Confirm a supplier&apos;s quote in the table above, then place the order here.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded border border-hr-3 bg-bg-2 p-3">
      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[12px]">
        <Field label="Part" value={part} />
        <Field label="Supplier" value={supplier} />
        <Field label="Price" value={money(price)} />
        <Field label="Quantity" value="1" />
      </div>

      {errored && (
        <p className="mt-2 text-[11.5px] text-red-fg">
          Couldn&apos;t place the order. Please try again.
        </p>
      )}

      <div className="mt-3 flex items-center justify-end gap-1.5">
        {confirming ? (
          <>
            <span className="mr-auto text-[11.5px] text-fg-3">
              Place this order for {part} from {supplier}?
            </span>
            <Button variant="ghost" size="sm" onClick={() => setConfirming(false)} disabled={pending}>
              Cancel
            </Button>
            <Button variant="primary" size="sm" loading={pending} onClick={onPlace}>
              Confirm placement
            </Button>
          </>
        ) : (
          <Button variant="primary" size="sm" onClick={() => setConfirming(true)}>
            Place order
          </Button>
        )}
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="font-mono text-[10px] uppercase tracking-[0.06em] text-fg-4">{label}</span>
      <span className="text-fg-1">{value}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------

function OrderTracking({ runId, order }: { runId: string; order: Order }) {
  const markDelivered = useMarkDelivered(runId);
  const currentIdx = (FLOW as readonly OrderStatus[]).indexOf(order.status);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 text-[12px]">
          <span className="font-medium text-fg-1">{order.vendor_name ?? "Supplier"}</span>
          <span className="text-fg-3">
            {" · "}
            {money(order.unit_price, order.currency ?? "USD")}
            {order.quantity ? ` · qty ${order.quantity}` : ""}
          </span>
        </div>
        <Pill tone={order.status === "received" ? "green" : "blue"} solid>
          {STEP_LABEL[order.status as FlowStatus] ?? order.status}
        </Pill>
      </div>

      {/* Plain-word status timeline */}
      <ol className="flex items-center gap-1">
        {FLOW.map((step, i) => {
          const done = i <= currentIdx;
          return (
            <li key={step} className="flex flex-1 flex-col items-center gap-1">
              <span
                className={cn(
                  "h-1.5 w-full rounded-full",
                  done ? "bg-blue-fg" : "bg-bg-4",
                  i === currentIdx && "bg-green-fg",
                )}
              />
              <span
                className={cn(
                  "text-[10.5px]",
                  done ? "text-fg-2" : "text-fg-4",
                )}
              >
                {STEP_LABEL[step]}
              </span>
            </li>
          );
        })}
      </ol>

      {order.status === "shipped" && (
        <div className="flex justify-end">
          <Button
            variant="success"
            size="sm"
            loading={markDelivered.isPending}
            onClick={() => markDelivered.mutate()}
          >
            Mark delivered
          </Button>
        </div>
      )}
    </div>
  );
}
