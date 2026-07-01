"use client";

/**
 * HistoryScreen — "Past orders & prices" (frontend spec §6 / proc-history.jsx), ported
 * to the proc design and wired to the real order history (GET /api/orders).
 *
 * Plain language, decision-first, honest: price intelligence, supplier track record and
 * spend are all derived from the customer's OWN orders — never outside data. The
 * "Your Arkim impact" tab reuses the impact panel (GET /api/impact). Where the mockup
 * grouped spend by asset, orders carry no asset, so spend is grouped by supplier (real).
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useAllOrders, useImpact } from "@/lib/queries";
import { ProcIcon } from "./proc-icon";
import { ProcPill, ProcHead, procMoney, type ProcTone } from "./proc-ui";
import { ImpactPanelBody } from "./impact-screen";
import type { Order, OrderStatus } from "@/types";

const total = (o: Order) => (o.unit_price ?? 0) * (o.quantity ?? 1);
const partLabel = (o: Order) => [o.manufacturer, o.part_number].filter(Boolean).join(" ") || "Part";
const fmtDate = (iso?: string) => (iso ? new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "—");

function statusTone(s: OrderStatus): { tone: ProcTone; label: string } {
  switch (s) {
    case "received": return { tone: "done", label: "Delivered" };
    case "shipped": return { tone: "progress", label: "Shipped" };
    case "placed": return { tone: "open", label: "Placed" };
    case "confirmed": return { tone: "open", label: "Confirmed" };
    case "cancelled": return { tone: "muted", label: "Cancelled" };
    default: return { tone: "muted", label: "Draft" };
  }
}

type Tab = "orders" | "spend" | "impact";

export function HistoryScreen() {
  const router = useRouter();
  const [tab, setTab] = useState<Tab>("orders");
  const { data, isLoading } = useAllOrders();
  const orders = (data?.orders ?? []).filter((o) => o.status !== "draft");

  return (
    <div className="proc-max">
      <button className="proc-back" onClick={() => router.push("/")}>
        <span style={{ display: "inline-flex", transform: "rotate(180deg)" }}><ProcIcon name="chevR" size={14} /></span>
        Home
      </button>
      <ProcHead
        title={<>Past orders <b>&amp; prices</b></>}
        sub="What you've ordered and paid — so you know a good quote when you see one."
      />

      <div className="proc-sitetabs">
        {([["orders", "Orders"], ["spend", "Spend by supplier"], ["impact", "Your Arkim impact"]] as [Tab, string][]).map(([k, l]) => (
          <button key={k} className="proc-sitetab" data-on={tab === k} onClick={() => setTab(k)}>{l}</button>
        ))}
      </div>

      {isLoading && tab !== "impact" && <p style={{ fontSize: 13, color: "var(--muted)" }}>Loading…</p>}
      {tab === "orders" && !isLoading && <OrdersTab orders={orders} onNew={() => router.push("/request")} />}
      {tab === "spend" && !isLoading && <SpendTab orders={orders} />}
      {tab === "impact" && <ImpactTab />}
    </div>
  );
}

// ---------------------------------------------------------------------------

function OrdersTab({ orders, onNew }: { orders: Order[]; onNew: () => void }) {
  const [search, setSearch] = useState("");
  if (orders.length === 0) {
    return (
      <div className="proc-tblcard">
        <div className="proc-tblempty">
          <div className="te-s">No orders yet.</div>
          <div className="te-sub">As you place orders, your price history and supplier track record build up here.</div>
        </div>
      </div>
    );
  }

  // Price intelligence: parts you've bought more than once (own history only).
  const byPart = new Map<string, Order[]>();
  for (const o of orders) {
    const k = partLabel(o);
    byPart.set(k, [...(byPart.get(k) ?? []), o]);
  }
  const intel = [...byPart.entries()]
    .map(([k, os]) => ({ part: k, os: [...os].sort((a, b) => (a.created_at ?? "").localeCompare(b.created_at ?? "")) }))
    .sort((a, b) => b.os.length - a.os.length)
    .slice(0, 2);

  // Supplier reliability.
  const bySup = new Map<string, { n: number; received: number }>();
  for (const o of orders) {
    const k = o.vendor_name ?? "Supplier";
    const cur = bySup.get(k) ?? { n: 0, received: 0 };
    cur.n += 1;
    if (o.status === "received") cur.received += 1;
    bySup.set(k, cur);
  }

  const q = search.toLowerCase();
  const rows = orders.filter((o) =>
    !q || partLabel(o).toLowerCase().includes(q) || (o.vendor_name ?? "").toLowerCase().includes(q),
  );

  return (
    <>
      {intel.length > 0 && (
        <div className="proc-intel">
          {intel.map(({ part, os }) => <PriceCard key={part} part={part} os={os} />)}
        </div>
      )}

      <div className="proc-tblcard">
        <div className="tc-head">
          <div className="tc-title"><ProcIcon name="user" size={16} />Supplier track record</div>
          <span className="tc-note" style={{ marginLeft: "auto" }}>your orders only — no outside data</span>
        </div>
        <table className="proc-tbl">
          <thead><tr><th>Supplier</th><th>Orders from you</th><th>Delivered</th></tr></thead>
          <tbody>
            {[...bySup.entries()].sort((a, b) => b[1].n - a[1].n).map(([sup, s]) => (
              <tr key={sup}>
                <td className="t-strong">{sup}</td>
                <td>{s.n}</td>
                <td>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontWeight: 600, color: s.received === s.n ? "var(--st-done)" : "var(--st-open)" }}>
                    <ProcIcon name="checkCircle" size={13} />{s.received} / {s.n}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="proc-tblcard">
        <div className="tc-head">
          <div className="tc-title"><ProcIcon name="clock" size={16} />Past orders</div>
          <div className="tc-search" style={{ marginLeft: "auto" }}>
            <ProcIcon name="search" size={14} color="var(--muted-2)" />
            <input placeholder="Search part or supplier…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
        </div>
        <table className="proc-tbl">
          <thead><tr><th>Date</th><th>Part</th><th>Supplier</th><th className="t-r">Total</th><th>Status</th></tr></thead>
          <tbody>
            {rows.map((o) => {
              const st = statusTone(o.status);
              return (
                <tr key={o.id}>
                  <td className="t-muted" style={{ whiteSpace: "nowrap" }}>{fmtDate(o.created_at)}</td>
                  <td className="t-strong">{partLabel(o)}</td>
                  <td className="t-muted">{o.vendor_name ?? "—"}</td>
                  <td className="t-r t-strong">{procMoney(total(o))}</td>
                  <td><ProcPill tone={st.tone}>{st.label}</ProcPill></td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr><td colSpan={5}><div className="proc-tblempty"><div className="te-s">No orders match that search.</div></div></td></tr>
            )}
          </tbody>
        </table>
        <div className="proc-tblfoot" style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span>{orders.length} order{orders.length === 1 ? "" : "s"} on record.</span>
          <button className="proc-btn" data-kind="quiet" style={{ marginLeft: "auto", padding: "5px 10px", fontSize: 12 }} onClick={onNew}>
            <ProcIcon name="plus" size={13} />New request
          </button>
        </div>
      </div>
    </>
  );
}

function PriceCard({ part, os }: { part: string; os: Order[] }) {
  const prices = os.map((o) => o.unit_price ?? 0).filter((p) => p > 0);
  if (prices.length === 0) return null;
  const lastPaid = prices[prices.length - 1];
  const avg = prices.reduce((a, b) => a + b, 0) / prices.length;
  const below = lastPaid <= avg;
  const pct = avg > 0 ? Math.round((Math.abs(lastPaid - avg) / avg) * 100) : 0;
  const max = Math.max(...prices) * 1.12;

  return (
    <div className="pi-card">
      <div className="pi-l"><ProcIcon name="tag" size={13} />Your price history</div>
      <div className="pi-part">{part}</div>
      <div className="pi-line">{os.length} order{os.length === 1 ? "" : "s"} on record · your own purchases</div>
      <div className="pi-read">
        <div className="pi-num">{procMoney(lastPaid)}</div>
        <div className="pi-unit">last paid</div>
        {prices.length > 1 && (
          <span className="pi-delta" data-good={below}>{below ? "▼" : "▲"} {pct}% {below ? "below" : "above"} avg</span>
        )}
      </div>
      {prices.length > 1 && (
        <>
          <div className="pi-line" style={{ marginTop: 6 }}>Your average: <b>{procMoney(avg)}</b> — a quote below that is a good deal.</div>
          <div className="pi-bars" style={{ marginTop: 12 }}>
            {os.slice(-6).map((o, i, arr) => {
              const p = o.unit_price ?? 0;
              return (
                <div key={o.id} className="b" data-on={i === arr.length - 1}>
                  <i style={{ height: Math.max(Math.round((p / max) * 100), 6) + "%" }} />
                  <span className="m">{fmtDate(o.created_at)}</span>
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

function SpendTab({ orders }: { orders: Order[] }) {
  const bySup = new Map<string, { n: number; total: number }>();
  for (const o of orders) {
    const k = o.vendor_name ?? "Supplier";
    const cur = bySup.get(k) ?? { n: 0, total: 0 };
    cur.n += 1;
    cur.total += total(o);
    bySup.set(k, cur);
  }
  const rows = [...bySup.entries()].sort((a, b) => b[1].total - a[1].total);
  const max = Math.max(...rows.map((r) => r[1].total), 1);
  const grand = rows.reduce((a, r) => a + r[1].total, 0);

  if (rows.length === 0) {
    return <div className="proc-tblcard"><div className="proc-tblempty"><div className="te-s">No spend yet.</div></div></div>;
  }

  return (
    <div className="proc-tblcard">
      <div className="tc-head">
        <div className="tc-title"><ProcIcon name="tag" size={16} />Spend by supplier</div>
        <span className="tc-note" style={{ marginLeft: "auto" }}>from your order history only</span>
      </div>
      <table className="proc-tbl">
        <thead><tr><th>Supplier</th><th className="t-r">Orders</th><th className="t-r">Total spend</th><th style={{ width: 180 }} /></tr></thead>
        <tbody>
          {rows.map(([sup, s]) => (
            <tr key={sup}>
              <td className="t-strong">{sup}</td>
              <td className="t-r t-muted">{s.n}</td>
              <td className="t-r t-strong">{procMoney(s.total)}</td>
              <td><div className="spendbar"><i style={{ width: Math.round((s.total / max) * 100) + "%" }} /></div></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="proc-tblfoot">Total across all suppliers: <b style={{ color: "var(--text)" }}>{procMoney(grand)}</b></div>
    </div>
  );
}

function ImpactTab() {
  const { data, isLoading } = useImpact();
  if (isLoading || !data) return <p style={{ fontSize: 13, color: "var(--muted)" }}>Loading…</p>;
  return <ImpactPanelBody d={data} />;
}
