"use client";

/**
 * OrderSection — proc-skinned order placement + tracking (frontend spec §5 / proc-order.jsx),
 * reusing the internal OrderPanel wiring (useOrders / useExecuteOrder / useMarkDelivered).
 * Mounted on the customer /parts/[id] run view; self-hides until there's something to
 * place or track.
 *
 * Invariants preserved: no accidental placement — "Place order" is a deliberate
 * review → confirm step; can't place without a resolvable price (the no-price case is
 * shown honestly and points back to confirming a quote).
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useOrders, useExecuteOrder, useMarkDelivered, useRunLive } from "@/lib/queries";
import { ProcIcon } from "./proc-icon";
import { procMoney } from "./proc-ui";
import { getShipTo, PRIMARY_SITE } from "@/lib/proc-config";
import type { Order, OrderStatus, Phase } from "@/types";

const FLOW = ["placed", "confirmed", "shipped", "received"] as const;
type FlowStatus = (typeof FLOW)[number];
const STEP_LABEL: Record<FlowStatus, string> = {
  placed: "Order placed",
  confirmed: "Confirmed by supplier",
  shipped: "Shipped",
  received: "Delivered",
};
const STEP_SUB: Record<FlowStatus, string> = {
  placed: "Sent to the supplier",
  confirmed: "Supplier accepted the order",
  shipped: "On the way to your dock",
  received: "Received at your facility",
};
const isFlow = (s: OrderStatus): s is FlowStatus => (FLOW as readonly OrderStatus[]).includes(s);

export function OrderSection({ runId }: { runId: string }) {
  const { data: ordersData } = useOrders(runId);
  const { data: run } = useRunLive(runId);
  const execute = useExecuteOrder(runId);

  const orders = ordersData?.orders ?? [];
  const placed = orders.find((o) => isFlow(o.status));
  const draft = orders.find((o) => o.status === "draft");
  const cancelled = orders.find((o) => o.status === "cancelled");
  const phase = (run?.phase ?? "") as Phase;
  const canPlace = ["approved", "executing"].includes(phase);

  if (!placed && !draft && !cancelled && !canPlace) return null;

  const noPrice = Boolean(draft) || execute.data?.placed === false;

  return (
    <section style={{ marginTop: 26 }}>
      <div className="proc-sec-h" style={{ marginBottom: 12 }}>
        <span className="t">Order</span>
      </div>
      {placed ? (
        <OrderTracking runId={runId} order={placed} />
      ) : cancelled && !canPlace ? (
        <div className="rc-note">This order was cancelled.</div>
      ) : (
        <PlaceOrderCard
          partName={[run?.asset_specs?.manufacturer, run?.asset_specs?.model || run?.asset_specs?.part_number].filter(Boolean).join(" ") || "the selected part"}
          supplier={run?.selected_candidate?.vendorName ?? "the selected supplier"}
          price={run?.selected_candidate?.price}
          noPrice={noPrice}
          pending={execute.isPending}
          errored={execute.isError}
          onPlace={() => execute.mutate()}
        />
      )}
    </section>
  );
}

function PlaceOrderCard({
  partName, supplier, price, noPrice, pending, errored, onPlace,
}: {
  partName: string; supplier: string; price?: number; noPrice: boolean;
  pending: boolean; errored: boolean; onPlace: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const router = useRouter();
  const shipTo = getShipTo(PRIMARY_SITE.id);

  if (noPrice) {
    return (
      <div className="proc-noprice">
        <p style={{ fontSize: 13, fontWeight: 600, color: "var(--st-open)" }}>
          We need a confirmed quote before this order can be placed.
        </p>
        <p style={{ fontSize: 12.5, color: "var(--muted)", marginTop: 4, lineHeight: 1.5 }}>
          Confirm a supplier&apos;s quote above, then place the order here.
        </p>
      </div>
    );
  }

  return (
    <div className="proc-review">
      <div className="rv-sec">
        <div className="rv-l">Part</div>
        <div className="rv-part">
          <div style={{ flex: 1 }}>
            <div className="rv-name">{partName}</div>
            <div className="rv-meta">Qty 1</div>
          </div>
          <div className="rv-total"><div className="rv-num">{price != null ? procMoney(price) : "price to be confirmed"}</div></div>
        </div>
      </div>
      <div className="rv-sec">
        <div className="rv-l">Supplier</div>
        <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text)" }}>{supplier}</div>
      </div>
      <div className="rv-sec">
        <div className="rv-l">
          Ship to
          <button className="edit" onClick={() => router.push("/settings")}>Edit</button>
        </div>
        <div className="rv-ship">
          <div><b style={{ fontWeight: 600, color: "var(--text)" }}>{shipTo.company}</b></div>
          <div>{shipTo.address}</div>
          <div>{shipTo.city}</div>
          <div><span className="dim">Attention:</span> {shipTo.attention}</div>
          <div><span className="dim">Receiving hours:</span> {shipTo.hours}</div>
          {shipTo.instructions && (
            <div style={{ marginTop: 6, fontSize: 12, color: "var(--muted-2)" }}>{shipTo.instructions}</div>
          )}
        </div>
      </div>
      <div className="proc-confirmbar" style={{ padding: "16px 18px", marginTop: 0 }}>
        {errored && <span className="note" style={{ color: "var(--st-overdue)" }}>Couldn&apos;t place the order — please try again.</span>}
        {confirming ? (
          <>
            <span className="note">Place this order for {partName} from {supplier}? This will actually order the part.</span>
            <button className="proc-btn" data-kind="quiet" disabled={pending} onClick={() => setConfirming(false)}>Cancel</button>
            <button className="proc-btn" data-kind="primary" disabled={pending} onClick={onPlace}>
              <ProcIcon name="checkCircle" size={15} />{pending ? "Placing…" : "Confirm placement"}
            </button>
          </>
        ) : (
          <>
            <span className="note">Review before confirming — this will actually order the part.</span>
            <button className="proc-btn" data-kind="primary" onClick={() => setConfirming(true)}>
              <ProcIcon name="box" size={15} />Place order
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function OrderTracking({ runId, order }: { runId: string; order: Order }) {
  const markDelivered = useMarkDelivered(runId);
  const currentIdx = (FLOW as readonly OrderStatus[]).indexOf(order.status);

  return (
    <div className="proc-track">
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 18, flexWrap: "wrap", gap: 10 }}>
        <div>
          <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text)" }}>{order.vendor_name ?? "Supplier"}</div>
          <div style={{ fontSize: 12.5, color: "var(--muted)", marginTop: 2 }}>
            {order.unit_price != null ? procMoney(order.unit_price) : "—"}
            {order.quantity ? ` · qty ${order.quantity}` : ""}
          </div>
        </div>
        <span className="proc-pill" data-tone={order.status === "received" ? "done" : "progress"}>
          <span className="d" />
          {STEP_LABEL[order.status as FlowStatus] ?? order.status}
        </span>
      </div>

      <div className="proc-tl">
        {FLOW.map((step, i) => {
          const done = i <= currentIdx;
          const current = i === currentIdx;
          return (
            <div key={step} className="tl-row" data-done={done} data-current={current}>
              <div className="tl-dot">{done && !current && <ProcIcon name="checkCircle" size={11} />}</div>
              <div style={{ paddingTop: 1 }}>
                <div className="tl-t">{STEP_LABEL[step]}</div>
                <div className="tl-s">{STEP_SUB[step]}</div>
              </div>
            </div>
          );
        })}
      </div>

      {order.status === "shipped" && (
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 16 }}>
          <button className="proc-btn" data-kind="primary" disabled={markDelivered.isPending} onClick={() => markDelivered.mutate()}>
            <ProcIcon name="checkCircle" size={14} />{markDelivered.isPending ? "Updating…" : "Mark delivered"}
          </button>
        </div>
      )}
    </div>
  );
}
