"use client";

/**
 * ThresholdsEditor — edit the approval tiers that GOVERN routing (the same rules
 * determine_approval_path keys on). Per facility; proc edits its own facility
 * (PROC_FACILITY_ID — the one orders route against), so a saved change really does
 * change how many approvers the next order needs.
 *
 * Honesty: a no-auth WORKFLOW config tool, not a verified permission gate — anyone can
 * edit these today. When auth lands the procurement-role gate switches on (plumbing
 * exists). Saves PERSIST (the endpoints are wired to the approval_rules table), unlike
 * the delivery form's older client-only note.
 *
 * Validation is client-side (thresholds strictly ascending; approvers a non-negative
 * integer); backend validation errors are surfaced if a POST is rejected.
 */

import { useEffect, useState } from "react";
import { ProcIcon } from "./proc-icon";
import { SkelList, procMoney } from "./proc-ui";
import { ApiError } from "@/lib/query-client";
import { PROC_FACILITY_ID } from "@/lib/proc-config";
import { useApprovalRules, useUpsertApprovalRule } from "@/lib/queries";

interface TierDraft {
  id: string;            // "" => not yet persisted (a default tier); save inserts it
  threshold: number;
  approvers_required: number;
  approver_roles: string[];
  cap: number | null;
}

function validate(tiers: TierDraft[]): string | null {
  for (const t of tiers) {
    if (!Number.isFinite(t.threshold) || t.threshold < 0) return "Thresholds must be 0 or more.";
    if (!Number.isInteger(t.approvers_required) || t.approvers_required < 0)
      return "Approvers required must be a whole number, 0 or more.";
  }
  for (let i = 1; i < tiers.length; i++) {
    if (tiers[i].threshold <= tiers[i - 1].threshold)
      return "Each tier's threshold must be higher than the one above it.";
  }
  return null;
}

export function ThresholdsEditor() {
  const { data, isLoading, isError } = useApprovalRules(PROC_FACILITY_ID);
  const upsert = useUpsertApprovalRule();

  const [tiers, setTiers] = useState<TierDraft[] | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Seed once from the loaded rules (don't clobber in-progress edits on refetch).
  useEffect(() => {
    if (!data) return;
    setTiers((prev) => prev ?? data.map((r) => ({
      id: r.id,
      threshold: r.threshold,
      approvers_required: r.approvers_required,
      approver_roles: r.approver_roles,
      cap: r.cap ?? null,
    })));
  }, [data]);

  const setTier = (i: number, patch: Partial<TierDraft>) =>
    setTiers((t) => (t ? t.map((x, j) => (j === i ? { ...x, ...patch } : x)) : t));

  const onSave = async () => {
    if (!tiers) return;
    setError(null);
    setSaved(false);
    const invalid = validate(tiers);
    if (invalid) { setError(invalid); return; }

    try {
      // Persist each tier (the endpoint upserts by id; default tiers insert on first save).
      const results = await Promise.all(tiers.map((t) =>
        upsert.mutateAsync({
          id: t.id || undefined,
          facility_id: PROC_FACILITY_ID,
          threshold: t.threshold,
          cap: t.cap ?? undefined,
          approvers_required: t.approvers_required,
          approver_roles: t.approver_roles,
          applies_to: "buy",
        })));
      // Adopt the persisted ids so a second save updates in place (no duplicate rows).
      setTiers((t) => (t ? t.map((x, j) => ({ ...x, id: results[j]?.id ?? x.id })) : t));
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    } catch (e) {
      const detail = e instanceof ApiError ? (e.body as { detail?: string } | undefined)?.detail : null;
      setError(detail ?? "Couldn’t save the thresholds — please try again.");
    }
  };

  if (isError) {
    return <div className="rc-note" style={{ color: "var(--st-overdue)" }}>
      Couldn&apos;t load the approval thresholds. Is the backend running?
    </div>;
  }
  if (isLoading || !tiers) {
    return <SkelList rows={2} label="Loading approval thresholds" />;
  }

  return (
    <div className="proc-form">
      <div className="pf-h">Approval thresholds</div>
      <div className="pf-s">
        These tiers decide how many approvers an order needs, by total amount. Editing them
        changes routing for future orders — a higher tier means more sign-off. Anyone can
        edit these for now; approver permissions arrive with sign-in.
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 4 }}>
        {tiers.map((t, i) => (
          <div
            key={i}
            style={{
              display: "grid", gridTemplateColumns: "1fr 1fr auto", gap: 12, alignItems: "end",
              padding: "12px 14px", borderRadius: 10, border: "1px solid var(--border)", background: "var(--surface)",
            }}
          >
            <label style={{ fontSize: 12, color: "var(--muted)", display: "flex", flexDirection: "column", gap: 5 }}>
              Orders from (USD)
              <input
                type="number"
                min={0}
                step={100}
                value={t.threshold}
                onChange={(e) => setTier(i, { threshold: e.target.value === "" ? NaN : Number(e.target.value) })}
                style={{
                  padding: "9px 11px", fontSize: 13, borderRadius: 8,
                  border: "1px solid var(--border)", background: "var(--surface-2, var(--surface))", color: "var(--text)",
                }}
              />
            </label>
            <label style={{ fontSize: 12, color: "var(--muted)", display: "flex", flexDirection: "column", gap: 5 }}>
              Approvers required
              <input
                type="number"
                min={0}
                step={1}
                value={t.approvers_required}
                onChange={(e) => setTier(i, { approvers_required: e.target.value === "" ? NaN : Math.trunc(Number(e.target.value)) })}
                style={{
                  padding: "9px 11px", fontSize: 13, borderRadius: 8,
                  border: "1px solid var(--border)", background: "var(--surface-2, var(--surface))", color: "var(--text)",
                }}
              />
            </label>
            <div style={{ fontSize: 11.5, color: "var(--muted-2)", paddingBottom: 9, whiteSpace: "nowrap" }}>
              {t.cap != null
                ? `up to ${procMoney(t.cap)}`
                : i === tiers.length - 1
                ? "and above"
                : ""}
            </div>
          </div>
        ))}
      </div>

      {error && <div className="note" style={{ color: "var(--st-overdue)", fontSize: 12.5, marginTop: 10 }}>{error}</div>}

      <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 14 }}>
        <button className="proc-btn" data-kind="primary" onClick={onSave} disabled={upsert.isPending}>
          <ProcIcon name="checkCircle" size={14} />
          {upsert.isPending ? "Saving…" : saved ? "Saved" : "Save thresholds"}
        </button>
        {saved && <span style={{ fontSize: 12, color: "var(--muted)" }}>Future orders route by these tiers.</span>}
      </div>
    </div>
  );
}
