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
import {
  useOrders, useExecuteOrder, useMarkDelivered, useRunLive, useSiteShipTo,
  useApproveRun, useRejectRun,
} from "@/lib/queries";
import { ApiError } from "@/lib/query-client";
import { ProcIcon } from "./proc-icon";
import { procMoney } from "./proc-ui";
import { defaultShipTo, PRIMARY_SITE } from "@/lib/proc-config";
import type { Candidate, Order, OrderStatus, Phase, SourcingRunDetail } from "@/types";

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
  // Manual marketplace fulfilment: an approved order Arkim is buying on the customer's
  // behalf, before it's placed with the supplier. Shown as its own pending state — not
  // the place-order card (it's already ordered), not hidden.
  const pendingManual = orders.find((o) => o.status === "pending_manual_fulfilment");
  const draft = orders.find((o) => o.status === "draft");
  const cancelled = orders.find((o) => o.status === "cancelled");
  const phase = (run?.phase ?? "") as Phase;
  const canPlace = ["approved", "executing"].includes(phase);
  // Above-threshold order-now: the spend is in approval and NO order exists yet (the
  // order materialises post-approval). This is the lifecycle step before "being
  // purchased" — give it a status panel so /parts/[id] covers the whole lifecycle.
  const awaiting = ["pending_first_approval", "pending_second_approval"].includes(phase)
    && !placed && !pendingManual && !draft;

  if (!placed && !pendingManual && !draft && !cancelled && !canPlace && !awaiting) return null;

  const noPrice = Boolean(draft) || execute.data?.placed === false;

  return (
    <section style={{ marginTop: 26 }}>
      <div className="proc-sec-h" style={{ marginBottom: 12 }}>
        <span className="t">Order</span>
      </div>
      {placed ? (
        <OrderTracking runId={runId} order={placed} />
      ) : pendingManual ? (
        <BeingPurchased order={pendingManual} />
      ) : awaiting && run ? (
        <AwaitingApproval run={run} />
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
  const { data: shipToData } = useSiteShipTo(PRIMARY_SITE.id);
  const shipTo = shipToData?.ship_to ?? defaultShipTo(PRIMARY_SITE.id);

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

function AwaitingApproval({ run }: { run: SourcingRunDetail }) {
  // Above-threshold pre-order state: the selection is in approval; no order exists yet.
  // Same honest treatment as BeingPurchased — no placed/shipped steps (nothing's bought).
  //
  // The Approve/Reject actions here are a WORKFLOW step, not a verified authorization
  // gate: the app is currently no-auth, so the approver name is a claimed identity for
  // the record (NOT an authenticated/verified approver). When auth lands (Cognito JWT +
  // assigned_sites), the backend already enforces distinct-approver (M1) on the verified
  // sub and this same field is backed by it — but today it does not enforce who approves.
  const sel = run.selected_candidate as
    (Candidate & { candidate_id?: string; quantity?: number;
      _approval_path?: { approvers_required?: number; grand_total_usd?: number; approver_roles?: string[] } }) | undefined;
  const ap = sel?._approval_path;
  const required = ap?.approvers_required ?? 1;
  const total = ap?.grand_total_usd;
  const qty = sel?.quantity ?? 1;

  // Vendor from the run's sourcing results, matched by the selection's candidate_id.
  const all: Candidate[] = [
    ...(run.sourcing_results?.tier1 ?? []),
    ...(run.sourcing_results?.tier2 ?? []),
    ...(run.sourcing_results?.tier3 ?? []),
  ];
  const selectedId = sel?.candidate_id ?? run.selected_candidate?.id;
  const vendor = all.find((c) => c.id === selectedId)?.vendorName ?? "the selected supplier";

  // Honest progress: how many distinct approvals are already recorded for this run.
  const approvedCount = (run.approval_history ?? []).filter((h) => h.action === "approved").length;
  const secondPending = run.phase === "pending_second_approval";
  const statusLine = secondPending
    ? `Awaiting second approval — ${approvedCount} of ${required} approved.`
    : `Awaiting approval — ${required} approver${required === 1 ? "" : "s"} required.`;
  // Expected role for THIS step, from the approval path (first → [0], second → [1]).
  const stepRole = ap?.approver_roles?.[approvedCount] ?? "Approver";

  const approve = useApproveRun(run.id);
  const reject = useRejectRun(run.id);
  const [name, setName] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const busy = approve.isPending || reject.isPending;

  const onApprove = () => {
    if (!name.trim() || busy) return;
    approve.mutate({ approver_name: name.trim(), approver_role: stepRole });
  };
  const onReject = () => {
    if (!name.trim() || !reason.trim() || busy) return;
    reject.mutate({ approver_name: name.trim(), approver_role: stepRole, notes: reason.trim() });
  };

  // Map the backend's distinct-approver rejection (M1, 409) honestly; otherwise generic.
  const approveErr = approve.error;
  const errMsg = approveErr
    ? (approveErr instanceof ApiError && approveErr.status === 409
        ? (((approveErr.body as { detail?: string } | undefined)?.detail) ?? "This run needs a different approver.")
        : "Couldn’t record the approval — please try again.")
    : reject.error
    ? "Couldn’t record the rejection — please try again."
    : null;

  return (
    <div className="proc-track">
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14, flexWrap: "wrap", gap: 10 }}>
        <div>
          <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text)" }}>{vendor}</div>
          <div style={{ fontSize: 12.5, color: "var(--muted)", marginTop: 2 }}>
            {total != null ? procMoney(total) : "—"}
            {qty ? ` · qty ${qty}` : ""}
          </div>
        </div>
        <span className="proc-pill" data-tone="open"><span className="d" />Awaiting approval</span>
      </div>
      <div className="rc-note" style={{ marginBottom: 14 }}>
        {statusLine} Approving records this against the run; once {required >= 2 ? "both approvals are in" : "it’s approved"},
        you&apos;ll see purchasing and delivery tracking here.
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <label style={{ fontSize: 12, color: "var(--muted)", display: "flex", flexDirection: "column", gap: 5 }}>
          Approving as <span style={{ color: "var(--muted-2)" }}>({stepRole})</span>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Your name"
            disabled={busy}
            style={{
              padding: "9px 11px", fontSize: 13, borderRadius: 8,
              border: "1px solid var(--border)", background: "var(--surface)", color: "var(--text)",
            }}
          />
          <span style={{ fontSize: 11, color: "var(--muted-2)" }}>
            Recorded as the approver for this run.
          </span>
        </label>

        {rejecting ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Reason for rejecting (required)"
              rows={3}
              disabled={busy}
              style={{
                padding: "9px 11px", fontSize: 13, borderRadius: 8, resize: "vertical",
                border: "1px solid var(--border)", background: "var(--surface)", color: "var(--text)",
              }}
            />
            <div style={{ fontSize: 11.5, color: "var(--muted-2)" }}>
              Rejecting returns this run to your options so you can choose again.
            </div>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button className="proc-btn" data-kind="quiet" disabled={busy} onClick={() => { setRejecting(false); setReason(""); }}>
                Cancel
              </button>
              <button
                className="proc-btn"
                disabled={busy || !name.trim() || !reason.trim()}
                onClick={onReject}
                style={{ background: "var(--st-overdue)", borderColor: "var(--st-overdue)", color: "#fff" }}
              >
                {reject.isPending ? "Rejecting…" : "Confirm rejection"}
              </button>
            </div>
          </div>
        ) : (
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button className="proc-btn" data-kind="quiet" disabled={busy} onClick={() => setRejecting(true)}>
              Reject
            </button>
            <button className="proc-btn" data-kind="primary" disabled={busy || !name.trim()} onClick={onApprove}>
              <ProcIcon name="checkCircle" size={15} />
              {approve.isPending ? "Approving…" : secondPending ? "Approve (2nd)" : "Approve"}
            </button>
          </div>
        )}

        {errMsg && (
          <span className="note" style={{ color: "var(--st-overdue)", fontSize: 12.5 }}>{errMsg}</span>
        )}
      </div>
    </div>
  );
}

function BeingPurchased({ order }: { order: Order }) {
  // Manual marketplace fulfilment, customer view: honest pending state. No placed/
  // shipped steps yet — Arkim hasn't bought it; we don't imply progress that hasn't
  // happened. Once an operator marks it purchased it becomes "placed" and the full
  // OrderTracking timeline takes over.
  return (
    <div className="proc-track">
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14, flexWrap: "wrap", gap: 10 }}>
        <div>
          <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text)" }}>{order.vendor_name ?? "Supplier"}</div>
          <div style={{ fontSize: 12.5, color: "var(--muted)", marginTop: 2 }}>
            {order.unit_price != null ? procMoney(order.unit_price) : "—"}
            {order.quantity ? ` · qty ${order.quantity}` : ""}
          </div>
        </div>
        <span className="proc-pill" data-tone="progress"><span className="d" />Being purchased</span>
      </div>
      <div className="rc-note">
        Arkim is purchasing this on your behalf. You&apos;ll see delivery tracking here once
        it&apos;s placed with the supplier.
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
