"use client";

/**
 * HomeScreen — the customer "What needs me" landing (frontend spec §1), ported from
 * the Figma mockup (proc-home.jsx) onto the real stack. Decision-first, plain language.
 *
 * Wired to real endpoints: handoffs + in-flight from GET /api/runs; impact from
 * GET /api/impact. Where the mockup faked data with no endpoint yet (reorder
 * intelligence; rich handoff reason/WO text beyond the runs list), the section
 * self-hides rather than show fabricated data — flagged in the build report.
 */

import { useRouter } from "next/navigation";
import { useRuns } from "@/lib/queries";
import { ProcIcon } from "./proc-icon";
import { ProcPill, SecHead, ProcHead, type ProcTone } from "./proc-ui";
import { HomeProcImpact } from "./home-impact";
import type { Phase, SourcingRunListItem } from "@/types";

const HANDOFF_PHASE: Phase = "pending_intake";
const INFLIGHT_PHASES: Phase[] = [
  "sourcing", "comparison", "pending_first_approval", "pending_second_approval",
  "approved", "executing", "fulfilling",
];
const DECISION_PHASES: Phase[] = [
  "comparison", "pending_first_approval", "pending_second_approval", "approved",
];

function phasePill(phase: Phase): { label: string; tone: ProcTone } {
  switch (phase) {
    case "sourcing": return { label: "Sourcing", tone: "open" };
    case "comparison": return { label: "Out for quotes", tone: "open" };
    case "pending_first_approval":
    case "pending_second_approval": return { label: "Awaiting approval", tone: "open" };
    case "approved": return { label: "Ready to order", tone: "done" };
    case "executing": return { label: "Placing order", tone: "progress" };
    case "fulfilling": return { label: "Shipped", tone: "progress" };
    case "completed": return { label: "Delivered", tone: "done" };
    default: return { label: "In progress", tone: "open" };
  }
}

function decisionCard(phase: Phase): { title: string; cta: string; tone: "ready" | "act"; icon: "mail" | "box" } {
  if (phase === "comparison") return { title: "Quotes ready to review", cta: "Review", tone: "ready", icon: "mail" };
  return { title: "Ready to place an order", cta: "Place order", tone: "act", icon: "box" };
}

function relTime(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso).getTime();
  const diff = (Date.now() - d) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function HomeScreen() {
  const router = useRouter();
  const { data: runs, isLoading, isError } = useRuns();

  const needPart = (
    <button className="proc-btnprimary" onClick={() => router.push("/request")}>
      <ProcIcon name="plus" size={15} />I need a part
    </button>
  );

  if (isLoading) {
    return (
      <div className="proc-max proc-center">
        <ProcHead title={<><b>Parts &amp; Orders</b></>} sub="Loading what needs you…" actions={needPart} />
      </div>
    );
  }

  if (isError) {
    return (
      <div className="proc-max proc-center">
        <ProcHead title={<><b>Parts &amp; Orders</b></>} sub="Couldn't reach the service." actions={needPart} />
        <p style={{ fontSize: 13, color: "var(--st-overdue)" }}>
          Couldn&apos;t load your requests — is the backend running?
        </p>
      </div>
    );
  }

  const all = runs ?? [];
  const handoffs = all.filter((r) => r.phase === HANDOFF_PHASE);
  const decisions = all.filter((r) => DECISION_PHASES.includes(r.phase as Phase));
  const inFlight = all.filter((r) => INFLIGHT_PHASES.includes(r.phase as Phase) && !DECISION_PHASES.includes(r.phase as Phase));
  const needsCount = handoffs.length + decisions.length;

  // Empty: nothing in the pipeline at all.
  if (all.length === 0) {
    return (
      <div className="proc-max proc-center">
        <ProcHead title={<><b>Parts &amp; Orders</b></>} sub="Get the right part without the runaround." actions={needPart} />
        <div className="proc-empty">
          <div className="pe-mark"><ProcIcon name="box" size={26} /></div>
          <div className="pe-t">Nothing needs you right now.</div>
          <div className="pe-s">When a machine needs a part, start here.</div>
          <div className="pe-sub">
            Describe the part in plain words — or snap a photo of the nameplate — and we&apos;ll
            find your best options, collect quotes, and track the order to your dock.
          </div>
          <button className="proc-btnprimary" onClick={() => router.push("/request")}>
            <ProcIcon name="plus" size={15} />I need a part
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="proc-max">
      <ProcHead
        title={<><b>Parts &amp; Orders</b></>}
        sub="A few things need a decision from you."
        actions={needPart}
      />

      {/* ---- Needs you ---- */}
      {needsCount > 0 && (
        <>
          <SecHead t="Needs you" c={needsCount} />
          <div className="proc-actions">
            {handoffs.map((r) => (
              <HandoffCard key={r.id} run={r} onOpen={() => router.push(`/parts/${r.id}`)} />
            ))}
            {decisions.map((r) => {
              const c = decisionCard(r.phase as Phase);
              return (
                <button key={r.id} className="proc-act" data-tone={c.tone} onClick={() => router.push(`/parts/${r.id}`)}>
                  <span className="pa-ic"><ProcIcon name={c.icon} size={18} /></span>
                  <span className="pa-tt">
                    <span className="pa-title" style={{ display: "block" }}>{c.title}</span>
                    <span className="pa-sub" style={{ display: "block" }}>
                      {r.asset_summary ?? "Maintenance part"}
                    </span>
                  </span>
                  <span className="pa-go">{c.cta}<ProcIcon name="arrowR" size={14} /></span>
                </button>
              );
            })}
          </div>
        </>
      )}

      {/* ---- In flight ---- */}
      {inFlight.length > 0 && (
        <>
          <SecHead t="In flight" c={inFlight.length} />
          <div className="proc-flight">
            {inFlight.map((r) => {
              const p = phasePill(r.phase as Phase);
              return (
                <button key={r.id} className="proc-fl" onClick={() => router.push(`/parts/${r.id}`)}>
                  <ProcPill tone={p.tone}>{p.label}</ProcPill>
                  <span className="fl-tt">
                    <span className="fl-title" style={{ display: "block" }}>
                      {r.asset_summary ?? "Maintenance part"}
                    </span>
                    <span className="fl-sub" style={{ display: "block" }}>{r.facility_id}</span>
                  </span>
                  <span className="fl-meta">{relTime(r.updated_at)}</span>
                  <ProcIcon name="chevR" size={15} color="var(--muted-2)" />
                </button>
              );
            })}
          </div>
        </>
      )}

      {/* ---- Your Arkim impact (real, from /api/impact) ---- */}
      <SecHead t="Your Arkim impact" />
      <HomeProcImpact onDrill={() => router.push("/impact")} />

      {/* ---- More ---- */}
      <SecHead t="More" />
      <div className="proc-flight">
        <button className="proc-fl" onClick={() => router.push("/impact")}>
          <span style={{ width: 32, height: 32, flexShrink: 0, borderRadius: "var(--r)", border: "1px solid var(--border)", background: "var(--bg)", color: "var(--muted)", display: "flex", alignItems: "center", justifyContent: "center" }}>
            <ProcIcon name="clock" size={16} />
          </span>
          <span className="fl-tt">
            <span className="fl-title" style={{ display: "block" }}>Past orders &amp; prices</span>
            <span className="fl-sub" style={{ display: "block" }}>
              What you&apos;ve paid before — so you know a good quote when you see one
            </span>
          </span>
          <ProcIcon name="chevR" size={15} color="var(--muted-2)" />
        </button>
        <button className="proc-fl" onClick={() => router.push("/settings")}>
          <span style={{ width: 32, height: 32, flexShrink: 0, borderRadius: "var(--r)", border: "1px solid var(--border)", background: "var(--bg)", color: "var(--muted)", display: "flex", alignItems: "center", justifyContent: "center" }}>
            <ProcIcon name="building" size={16} />
          </span>
          <span className="fl-tt">
            <span className="fl-title" style={{ display: "block" }}>Delivery settings</span>
            <span className="fl-sub" style={{ display: "block" }}>
              Where orders ship — address, receiving hours, dock instructions
            </span>
          </span>
          <ProcIcon name="chevR" size={15} color="var(--muted-2)" />
        </button>
      </div>
    </div>
  );
}

function HandoffCard({ run, onOpen }: { run: SourcingRunListItem; onOpen: () => void }) {
  const urgent = run.urgency === "Emergency";
  return (
    <button className="proc-handoff" data-urgent={urgent} onClick={onOpen}>
      <span className="hf-ic"><ProcIcon name={urgent ? "alert" : "refresh"} size={18} /></span>
      <span className="hf-tt">
        <span className="hf-kicker">
          <span className={urgent ? "urg" : "urg-ok"}>
            {urgent ? "Urgent" : "Routine"} · from maintenance
          </span>
          {run.maintenance_submission_id && (
            <>
              <span style={{ opacity: 0.4 }}>·</span>
              <span>{run.maintenance_submission_id}</span>
            </>
          )}
          <span style={{ opacity: 0.4 }}>·</span>
          <span>{relTime(run.created_at)}</span>
        </span>
        <span className="hf-title" style={{ display: "block" }}>
          {run.asset_summary ?? "A machine needs a part"}
        </span>
        <span className="hf-cta" style={{ display: "inline-flex" }}>
          Source this part<ProcIcon name="arrowR" size={14} />
        </span>
      </span>
    </button>
  );
}
