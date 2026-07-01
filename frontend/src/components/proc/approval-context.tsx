"use client";

/**
 * ApprovalContext — the enriched "what am I approving" block shared by BOTH approval
 * surfaces (the Approvals queue rows and the per-run AwaitingApproval panel), so they read
 * identically. It is a PURE READ of the already-fetched run DETAIL — no backend call, no
 * payload widening.
 *
 * Honesty is origin-branched and load-bearing:
 *  - maintenance-origin runs (maintenance_handoff present) carry REAL context — captured
 *    urgency, the request/diagnostic narrative (chat_thread_summary), and a work-order id.
 *  - direct-request runs carry NONE of that. Their urgency is a hardcoded backend default
 *    the form never asked for, so we NEVER render it as a chosen priority — we say
 *    "Not specified". The request text for these runs is only asset_specs.description, which
 *    RailPartContext already surfaces, so there is no separate (duplicate) "reason" block.
 * We surface what's real and mark the rest honestly; we fabricate nothing.
 */

import { RailPartContext } from "./part-context";
import { deriveApproval } from "./approval-actions";
import { ProcIcon } from "./proc-icon";
import { procMoney } from "./proc-ui";
import type { Candidate, SourcingRunDetail } from "@/types";

type HandoffCtx = {
  urgency?: string;
  chat_thread_summary?: string;
  work_order_id?: string;
  asset_tag?: string;
};

/** Read the maintenance handoff context, if this run came from maintenance. Returns null
 *  for direct-request runs (no handoff) — the signal the card branches on. */
function readHandoffContext(run: SourcingRunDetail): HandoffCtx | null {
  const h = run.maintenance_handoff as Record<string, unknown> | undefined;
  if (!h) return null;
  const ctx = (h.context as Record<string, unknown> | undefined) ?? {};
  const str = (v: unknown) => (typeof v === "string" && v.trim() ? v.trim() : undefined);
  return {
    urgency: str(ctx.urgency),
    chat_thread_summary: str(ctx.chat_thread_summary),
    work_order_id: str(ctx.work_order_id),
    asset_tag: str(ctx.asset_tag),
  };
}

/** Real captured urgency → human label. Only ever called with a maintenance-captured value;
 *  the request-origin default is never passed here. */
function urgencyLabel(raw?: string): string | null {
  if (!raw) return null;
  const k = raw.toLowerCase();
  if (k === "emergency") return "Emergency";
  if (k === "predictive") return "Predictive";
  if (k === "standard" || k === "routine" || k === "stocking") return "Routine";
  return raw.replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Chosen-candidate evidence → exact replacement vs equivalent, from the joined candidate. */
function matchLabel(c?: Candidate): string | null {
  if (!c) return null;
  if (c.isExactMatch || c.pnMatchLevel === "exact") return "Exact replacement";
  if (c.isAftermarket) return "Equivalent alternative";
  const fit = c.comparisonArtifact?.compatibilitySummary;
  if (fit === "fit_confirmed" || fit === "fit_likely") return "Compatible alternative";
  return "Alternative — verify fit";
}

export function ApprovalContext({ run }: { run: SourcingRunDetail }) {
  const d = deriveApproval(run);
  const ctx = readHandoffContext(run);
  const isMaintenance = ctx !== null;
  const cand = d.selectedCandidate;

  const narrative = ctx?.chat_thread_summary;
  const urgency = isMaintenance ? urgencyLabel(ctx?.urgency) : null;
  const match = matchLabel(cand);

  return (
    <div className="appctx">
      {/* origin marker — quiet, so present-vs-absent context reads as expected, not missing */}
      <div className="appctx-origin">
        <ProcIcon name={isMaintenance ? "toolbox" : "doc"} size={12} />
        {isMaintenance ? "From maintenance" : "Direct request"}
        {isMaintenance && ctx?.work_order_id && <span className="appctx-origin-sub">· {ctx.work_order_id}</span>}
      </div>

      {/* what it is — reused verbatim from the run page */}
      <RailPartContext specs={run.asset_specs} />

      {/* why it's needed — maintenance narrative only; omitted entirely when none (no invention).
          Direct-request reason lives in asset_specs.description, already shown above. */}
      {narrative && (
        <div className="appctx-block">
          <div className="appctx-label">Why it&apos;s needed</div>
          <div className="appctx-text">{narrative}</div>
        </div>
      )}

      {/* why this supplier — exact/equivalent + lead time + what it was chosen over */}
      {(match || cand?.leadTime || d.alternativesCount > 0) && (
        <div className="appctx-block">
          <div className="appctx-label">Why this supplier</div>
          <div className="appctx-facts">
            {match && <span className="appctx-fact">{match}</span>}
            {cand?.leadTime && (
              <span className="appctx-fact" title={cand.leadTimeSource === "defaulted" ? "Estimated lead time" : undefined}>
                Lead time {cand.leadTimeSource === "defaulted" ? `~${cand.leadTime}` : cand.leadTime}
              </span>
            )}
            {d.alternativesCount > 0 && (
              <span className="appctx-fact">Chosen over {d.alternativesCount} other option{d.alternativesCount === 1 ? "" : "s"}</span>
            )}
          </div>
        </div>
      )}

      {/* amount basis — what the money is, and why N approvers (authoritative count) */}
      <div className="appctx-block">
        <div className="appctx-label">Amount</div>
        <div className="appctx-amount">
          {d.total != null ? procMoney(d.total) : "—"}{d.qty ? ` · qty ${d.qty}` : ""}
        </div>
        {cand?.price != null && d.qty > 1 && <div className="appctx-sub">{procMoney(cand.price)} each</div>}
        <div className="appctx-sub">
          {d.required} approver{d.required === 1 ? "" : "s"} required — set by your approval thresholds for this amount.
        </div>
      </div>

      {/* priority — REAL only when maintenance captured it; never the request-origin default */}
      <div className="appctx-block appctx-inline">
        <div className="appctx-label">Priority</div>
        {urgency ? (
          <span className="proc-pill" data-tone={urgency === "Emergency" ? "overdue" : "open"}>
            <span className="d" />{urgency}
          </span>
        ) : (
          <span className="appctx-muted">
            Not specified{isMaintenance ? "" : " — direct request, no priority set"}
          </span>
        )}
      </div>
    </div>
  );
}
