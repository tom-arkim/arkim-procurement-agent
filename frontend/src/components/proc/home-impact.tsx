"use client";

/**
 * HomeProcImpact — the compact "Your Arkim impact" zone on Home.
 *
 * ALL numbers come from GET /api/impact (utils/impact.py). The UI never recomputes:
 * savings are MEASURED (customer transactions only, never a market baseline), counts
 * are COUNTED (shown as fact), and the time figure is an ESTIMATE — always labelled,
 * with the model version surfaced. The monthly trend shows real order-months only; a
 * month with no comparable purchase stays "—" (not zero, not interpolated).
 */

import { useImpact } from "@/lib/queries";
import { procMoney } from "./proc-ui";
import { ProcIcon } from "./proc-icon";
import { BRAND_NAME } from "@/lib/brand";
import type { ImpactMonth } from "@/types";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function formatMonth(m: string): string {
  const parts = m.split("-");
  const mo = Number(parts[1]);
  return mo >= 1 && mo <= 12 ? MONTHS[mo - 1] : m;
}

function formatTime(minutes: number): string {
  if (!minutes || minutes <= 0) return "—";
  if (minutes < 60) return `~${minutes} min`;
  const hours = minutes / 60;
  const rounded = Math.round(hours * 10) / 10;
  const display = Number.isInteger(rounded) ? rounded : Math.round(hours);
  return `~${display} hr${display === 1 ? "" : "s"}`;
}

export function HomeProcImpact({ onDrill }: { onDrill: () => void }) {
  const { data, isLoading } = useImpact();
  if (isLoading || !data) return null;

  const time = formatTime(data.time_estimate_minutes);
  const hasSavings = data.total_savings > 0;
  const months = data.savings_by_month;

  return (
    <div className="proc-impact-home">
      {/* Savings — MEASURED */}
      <div className="pih-cell">
        <div className="pih-l">
          <ProcIcon name="tag" size={12} />
          Savings
        </div>
        <div className="pih-num">{procMoney(data.total_savings)}</div>
        <div className="pih-sub">
          vs your own last-paid prices &amp; quote spreads
          <br />
          <span style={{ fontSize: 10.5, color: "var(--muted-2)" }}>
            {data.contributing_order_ids.length} order
            {data.contributing_order_ids.length === 1 ? "" : "s"} with a measured saving
          </span>
        </div>
      </div>

      {/* Work done — COUNTED */}
      <div className="pih-cell">
        <div className="pih-l">
          <ProcIcon name="checkCircle" size={12} />
          Work done
        </div>
        <div className="pih-num">{data.counts.suppliers_contacted}</div>
        <div className="pih-sub">
          suppliers contacted · {data.counts.quotes_read} quotes read
          <br />
          <span className="est">{time === "—" ? "no time logged yet" : `${time} of manual work, estimated`}</span>
        </div>
      </div>

      {/* Savings trend — real months only */}
      {months.length > 0 && (
        <div
          style={{
            gridColumn: "1 / -1",
            borderTop: "1px solid var(--border-soft)",
            paddingTop: 14,
            paddingBottom: 2,
          }}
        >
          <div
            style={{
              fontSize: 10,
              fontWeight: 700,
              letterSpacing: "1.1px",
              textTransform: "uppercase",
              color: "var(--muted-2)",
              marginBottom: 10,
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <ProcIcon name="tag" size={11} />
            Savings by month
          </div>
          <SavingsTrend months={months} />
          <div style={{ fontSize: 10.5, color: "var(--muted-2)", marginTop: 8, lineHeight: 1.5 }}>
            Savings accrued from your own orders — real months only.
            {months.some((m) => m.savings === 0) && ' "—" = no comparable prior purchase that month.'}
          </div>
        </div>
      )}

      {/* Methodology note */}
      <div className="pih-note">
        Savings anchored to real transactions only — no market estimates. Time figure (
        <span className="est">{time}</span>) is {BRAND_NAME}&apos;s estimate ({data.estimate_model_version}).&nbsp;
        <button className="pih-drill" onClick={onDrill}>
          See full breakdown →
        </button>
        {!hasSavings && (
          <>
            <br />
            Savings fill in here as you place orders we can compare to your own history.
          </>
        )}
      </div>
    </div>
  );
}

function SavingsTrend({ months }: { months: ImpactMonth[] }) {
  const maxSaving = Math.max(...months.map((m) => m.savings), 1);
  return (
    <div className="pi-bars">
      {months.map((m, i) => {
        const isLast = i === months.length - 1;
        const barH = m.savings > 0 ? Math.max(Math.round((m.savings / maxSaving) * 30), 5) : 3;
        return (
          <div key={m.month} className="b" data-on={m.savings > 0 && isLast} title={m.note}>
            <span className="v" data-zero={m.savings === 0}>
              {m.savings > 0 ? procMoney(m.savings) : "—"}
            </span>
            <i
              style={{
                height: barH + "px",
                background: m.savings > 0 ? undefined : "var(--border)",
                borderTop: m.savings > 0 ? undefined : "none",
                borderRadius: m.savings === 0 ? "2px" : undefined,
              }}
            />
            <span className="m">{formatMonth(m.month)}</span>
          </div>
        );
      })}
    </div>
  );
}
