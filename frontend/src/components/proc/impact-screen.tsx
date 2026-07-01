"use client";

/**
 * ImpactScreen — the full "Your Arkim impact" panel (frontend spec §6 / proc-history.jsx
 * impact tab), wired to GET /api/impact (utils/impact.py). The UI never recomputes.
 *
 * Three tiers kept visibly separate per the Calculator Methodology:
 *   • Savings — MEASURED (customer transactions only; never a market baseline).
 *   • Counts — COUNTED (the real proof: suppliers contacted, quotes read).
 *   • Time saved — ESTIMATED (labelled "Arkim's estimate", with the model version).
 * The two visuals the mockup data supports: the cumulative savings trend (real months
 * only — a no-saving month stays "—") and the labour-displaced "what Arkim handled"
 * step visual (off the real counts). The §7 methodology guardrail anchors trust.
 */

import { useRouter } from "next/navigation";
import { useImpact } from "@/lib/queries";
import { ProcIcon, type ProcIconName } from "./proc-icon";
import { ProcHead, procMoney } from "./proc-ui";
import type { CumulativeImpact, ImpactCounts, ImpactMonth } from "@/types";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const fmtMonth = (m: string) => { const mo = Number(m.split("-")[1]); return mo >= 1 && mo <= 12 ? MONTHS[mo - 1] : m; };
const basisLabel = (b: string | null) =>
  b === "vs_last_paid" ? "vs your last paid price"
  : b === "vs_highest_quote" ? "vs the highest quote received"
  : "measured saving";

function fmtTime(minutes: number): string {
  if (!minutes || minutes <= 0) return "—";
  if (minutes < 60) return `~${minutes} min`;
  const hours = minutes / 60;
  const rounded = Math.round(hours * 10) / 10;
  const display = Number.isInteger(rounded) ? rounded : Math.round(hours);
  return `~${display} hr${display === 1 ? "" : "s"}`;
}

function workSteps(c: ImpactCounts): { icon: ProcIconName; label: string }[] {
  const steps: { icon: ProcIconName; label: string }[] = [];
  if (c.suppliers_contacted > 0) {
    steps.push({ icon: "mail", label: `Contacted ${c.suppliers_contacted} supplier${c.suppliers_contacted === 1 ? "" : "s"}` });
    steps.push({ icon: "refresh", label: "Monitored for replies" });
  }
  if (c.quotes_read > 0) steps.push({ icon: "doc", label: `Read ${c.quotes_read} quote${c.quotes_read === 1 ? "" : "s"}` });
  if (c.comparisons_made > 0) steps.push({ icon: "sort", label: "Compared & ranked" });
  if (c.replies_chased > 0) steps.push({ icon: "alert", label: `Chased ${c.replies_chased} non-responder${c.replies_chased === 1 ? "" : "s"}` });
  return steps;
}

export function ImpactScreen() {
  const router = useRouter();
  const { data, isLoading, isError } = useImpact();

  return (
    <div className="proc-max">
      <button className="proc-back" onClick={() => router.push("/")}>
        <span style={{ display: "inline-flex", transform: "rotate(180deg)" }}><ProcIcon name="chevR" size={14} /></span>
        Home
      </button>
      <ProcHead title={<>Your Arkim <b>impact</b></>} sub="What you've saved and the legwork we handled — every figure traces to a real transaction." />

      {isLoading && <p style={{ fontSize: 13, color: "var(--muted)" }}>Loading…</p>}
      {isError && <p style={{ fontSize: 13, color: "var(--st-overdue)" }}>Couldn&apos;t load impact — is the backend running?</p>}
      {data && <ImpactPanelBody d={data} />}
    </div>
  );
}

export function ImpactPanelBody({ d }: { d: CumulativeImpact }) {
  const time = fmtTime(d.time_estimate_minutes);
  const steps = workSteps(d.counts);
  const months = d.savings_by_month;
  const hasSavings = d.total_savings > 0;
  const hasAnything = hasSavings || months.length > 0 || d.counts.suppliers_contacted > 0;

  if (!hasAnything) {
    return (
      <div className="imp-trend-card">
        <div className="imp-low-data">
          <div className="ild-t">Not enough history for impact yet.</div>
          <div className="ild-s">This fills in as you use it.</div>
          <div className="ild-sub">
            Each time you compare a quote to your own prior purchase or to other quotes received, that saving
            gets recorded here — traceable to the real transaction. The legwork Arkim does is counted as you go.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div>
      {/* hero stats */}
      <div className="imp-hero-grid">
        <div className="imp-hero-card primary">
          <div className="ihc-eye"><ProcIcon name="clock" size={12} />Time saved</div>
          <div className="ihc-num">{time}</div>
          <div className="ihc-est">Arkim&apos;s estimate ({d.estimate_model_version}) — labelled because it&apos;s an estimate</div>
          <div className="ihc-proof">
            Based on: <b>{d.counts.suppliers_contacted} suppliers contacted</b> · <b>{d.counts.quotes_read} quotes read &amp; compared</b>.<br />
            The counts above are real. The hours figure is our rough estimate of equivalent manual effort.
          </div>
        </div>
        <div className="imp-hero-card">
          <div className="ihc-eye"><ProcIcon name="tag" size={12} />Saved</div>
          <div className="ihc-num">{procMoney(d.total_savings)}</div>
          <div className="ihc-est">&nbsp;</div>
          <div className="ihc-proof">
            <b>{d.contributing_order_ids.length} order{d.contributing_order_ids.length === 1 ? "" : "s"} with a measured saving.</b><br />
            Not vs a market rate — vs your own prior purchases and the quotes you actually received.
          </div>
        </div>
      </div>

      {/* what Arkim handled */}
      {steps.length > 0 && (
        <div className="imp-work-card">
          <div className="iwc-head">
            <div className="iwc-t">What Arkim handled</div>
            <div className="iwc-s">The steps you didn&apos;t have to take.</div>
          </div>
          <div className="imp-work-body">
            <div className="imp-steps">
              {steps.map((s, i) => (
                <div key={i} className="imp-step">
                  <span className="is-dot"><ProcIcon name={s.icon} size={13} /></span>
                  <span className="is-t">{s.label}</span>
                </div>
              ))}
            </div>
            <div className="imp-you">
              <div className="iy-l">You did</div>
              <div className="iy-num">1</div>
              <div className="iy-label">decision per request</div>
              <div className="iy-did">You reviewed the options and made the call — Arkim did the legwork.</div>
              {time !== "—" && <div className="iy-est">Arkim&apos;s estimate: {time} of manual work saved</div>}
            </div>
          </div>
        </div>
      )}

      {/* savings trend */}
      <div className="imp-trend-card">
        <div className="itc-head">
          <span className="itc-t">Savings over time</span>
          <span className="itc-s">— each bar = one month&apos;s measured savings · only real transactions</span>
        </div>
        {months.length > 0 ? <TrendBars months={months} /> : (
          <div className="imp-low-data" style={{ padding: "16px 20px" }}>
            <div className="ild-s">No comparable transactions yet.</div>
          </div>
        )}
        <div className="imp-trend-note">
          Savings accrued from your own orders — real months only.
          {months.some((m) => m.savings === 0) && ' "—" = no comparable prior purchase that month.'}
        </div>
      </div>

      {/* per-order proof — every figure traces to one real order */}
      {d.breakdown.length > 0 && (
        <div className="imp-proof-card">
          <div className="ipc-head"><ProcIcon name="doc" size={12} />Savings breakdown — every figure traces to a real order</div>
          {d.breakdown.map((row, i) => (
            <div key={row.order_id ?? i} className="imp-proof-row">
              <span className="ipr-ic"><ProcIcon name="checkCircle" size={15} /></span>
              <div className="ipr-tt">
                <div className="ipr-t">{row.part ?? "Part"}{row.vendor ? ` — ${row.vendor}` : ""}</div>
                <div className="ipr-s">{fmtMonth(row.month)} · {basisLabel(row.saving_basis)}</div>
              </div>
              <div className="ipr-num">− {procMoney(row.saving)}</div>
            </div>
          ))}
          <div className="imp-proof-foot">
            <span style={{ fontSize: 12.5, color: "var(--muted)", fontWeight: 600 }}>Total measured savings</span>
            <span style={{ fontSize: 17, fontWeight: 600, color: "var(--st-done)", fontVariantNumeric: "tabular-nums" }}>{procMoney(d.total_savings)}</span>
          </div>
        </div>
      )}

      {/* methodology guardrail */}
      <div className="imp-method">
        <b>A note on these numbers —</b> Savings compare your actual transaction prices against your own prior
        purchases or quotes you received. Never vs a market rate, list price, or modelled baseline. If we don&apos;t
        have two real comparable transactions, we don&apos;t claim a saving. Time-saved figures are Arkim&apos;s rough
        estimate of equivalent manual effort, always labelled as estimates. These are the numbers you can show your
        CFO and back up line by line.
      </div>
    </div>
  );
}

function TrendBars({ months }: { months: ImpactMonth[] }) {
  const max = Math.max(...months.map((m) => m.savings), 1);
  return (
    <div className="imp-trend-bars">
      {months.map((m) => {
        const zero = m.savings <= 0;
        const h = zero ? 3 : Math.max(Math.round((m.savings / max) * 64), 4);
        return (
          <div key={m.month} className="itb" title={m.note}>
            <span className={zero ? "itb-val zero" : "itb-val"}>{zero ? "—" : procMoney(m.savings)}</span>
            <div className={zero ? "itb-bar zero" : "itb-bar"} style={{ height: h + "px" }} />
            <span className="itb-lbl">{fmtMonth(m.month)}</span>
          </div>
        );
      })}
    </div>
  );
}
