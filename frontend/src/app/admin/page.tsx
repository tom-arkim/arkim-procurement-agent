"use client";

/**
 * Internal INSPECTOR / ADMIN page (/admin) — read-only debug surface over the whole
 * pipeline (runs, suppliers, sent-messages, review-queue, orders, prices).
 *
 * Access control is REAL and server-side: every /api/admin/* endpoint requires the
 * admin bearer token (require_admin -> 401/403/503). This page just holds the token
 * (entered once, kept in localStorage) and sends it. It is intentionally NOT linked
 * from the main nav — reachable only by knowing the URL — but the API gate is the
 * actual control: a non-admin who hits the endpoints directly is rejected.
 *
 * Optimized for debug VISIBILITY over polish: dense tables of scalar fields + a raw
 * JSON dump per row for full-record inspection. CONFIRM actions (apply a queued quote/
 * contact) are deliberately NOT here yet — this build is read-only.
 */

import { Fragment, useCallback, useEffect, useState } from "react";

const API_BASE =
  typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_URL
    ? process.env.NEXT_PUBLIC_API_URL.replace(/\/$/, "")
    : "";

const TOKEN_KEY = "arkim_admin_token";

type Tab =
  | "runs" | "suppliers" | "sent-messages" | "review-queue" | "orders" | "prices"
  | "unmatched-replies" | "fulfilment" | "labeling";

// `labeling` is a Night 2 addition: it has no `path`/`listKey` (it renders its
// own dedicated view, not the generic table). TABS still lists it so the tab
// chip appears; the render branch special-cases it.
const TABS: { id: Tab; label: string; path: string; listKey: string }[] = [
  { id: "runs", label: "Runs", path: "/runs", listKey: "runs" },
  { id: "suppliers", label: "Suppliers", path: "/suppliers", listKey: "suppliers" },
  { id: "sent-messages", label: "Sent Messages", path: "/sent-messages", listKey: "sent_messages" },
  { id: "review-queue", label: "Review Queue", path: "/review-queue", listKey: "review_items" },
  { id: "unmatched-replies", label: "Unmatched Replies", path: "/unmatched-replies", listKey: "unmatched_replies" },
  { id: "fulfilment", label: "Fulfilment", path: "/fulfilment-queue", listKey: "orders" },
  { id: "orders", label: "Orders", path: "/orders", listKey: "orders" },
  { id: "prices", label: "Prices", path: "/prices", listKey: "prices" },
  { id: "labeling", label: "Labeling", path: "", listKey: "" },
];

type FetchResult = { ok: boolean; status: number; body: unknown };

async function fetchAdmin(path: string, token: string): Promise<FetchResult> {
  const res = await fetch(`${API_BASE}/api/admin${path}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const text = await res.text();
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    body = text;
  }
  return { ok: res.ok, status: res.status, body };
}

async function postAdmin(path: string, token: string, payload?: unknown): Promise<FetchResult> {
  const res = await fetch(`${API_BASE}/api/admin${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      ...(payload !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    ...(payload !== undefined ? { body: JSON.stringify(payload) } : {}),
  });
  const text = await res.text();
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    body = text;
  }
  return { ok: res.ok, status: res.status, body };
}

function scalarColumns(rows: Record<string, unknown>[]): string[] {
  const cols = new Set<string>();
  for (const r of rows.slice(0, 50)) {
    for (const [k, v] of Object.entries(r)) {
      if (v === null || ["string", "number", "boolean"].includes(typeof v)) cols.add(k);
    }
  }
  return Array.from(cols);
}

function cell(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
}

// ---------------------------------------------------------------------------

export default function AdminInspectorPage() {
  const [token, setToken] = useState<string>("");
  const [tokenInput, setTokenInput] = useState<string>("");
  const [tab, setTab] = useState<Tab>("runs");
  const [result, setResult] = useState<FetchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState<FetchResult | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  // Per-row ref/tracking input for the Fulfilment "mark purchased" action.
  const [refInputs, setRefInputs] = useState<Record<string, string>>({});

  // Labeling tab state (Night 2): the selected run's labeling view + label drafts.
  const [labelingRun, setLabelingRun] = useState<FetchResult | null>(null);
  const [labelingRunId, setLabelingRunId] = useState<string | null>(null);
  // Drafts for the run-scope (intake) label.
  const [intakeDraft, setIntakeDraft] = useState<{
    expected_part_type: string;
    expected_component_of: string;
    expected_regime: string;
    intake_correct: boolean;
    note: string;
  }>({ expected_part_type: "", expected_component_of: "", expected_regime: "DIRECT",
       intake_correct: true, note: "" });
  // Drafts for per-candidate labels, keyed by candidate_id.
  const [candDrafts, setCandDrafts] = useState<Record<string, {
    right_part_type: string; should_pass_floor: boolean; note: string;
  }>>({});
  const [labelMsg, setLabelMsg] = useState<string>("");

  useEffect(() => {
    const saved = typeof window !== "undefined" ? window.localStorage.getItem(TOKEN_KEY) : null;
    if (saved) setToken(saved);
  }, []);

  const load = useCallback(
    async (t: Tab) => {
      if (!token) return;
      setLoading(true);
      setDetail(null);
      setExpanded(null);
      const def = TABS.find((x) => x.id === t)!;
      if (def.id === "labeling") {
        // Labeling tab has its own view; fetch the failures-first queue.
        setResult(await fetchAdmin("/labeling/queue", token));
      } else {
        setResult(await fetchAdmin(def.path, token));
      }
      setLoading(false);
    },
    [token],
  );

  useEffect(() => {
    if (token) load(tab);
  }, [token, tab, load]);

  function saveToken() {
    window.localStorage.setItem(TOKEN_KEY, tokenInput.trim());
    setToken(tokenInput.trim());
  }

  function clearToken() {
    window.localStorage.removeItem(TOKEN_KEY);
    setToken("");
    setResult(null);
  }

  async function openRun(id: string) {
    setDetail(await fetchAdmin(`/runs/${id}`, token));
  }

  // --- Labeling (Night 2) -------------------------------------------------
  async function openLabelingRun(id: string) {
    setLabelingRunId(id);
    setLabelMsg("");
    const r = await fetchAdmin(`/labeling/runs/${id}`, token);
    setLabelingRun(r);
    // Seed drafts from the current labels (if any).
    const body = r.body as Record<string, unknown> | undefined;
    const runLabel = body?.run_label as Record<string, unknown> | undefined;
    setIntakeDraft({
      expected_part_type: (runLabel?.expected_part_type as string) || "",
      expected_component_of: (runLabel?.expected_component_of as string) || "",
      expected_regime: (runLabel?.expected_regime as string) || "DIRECT",
      intake_correct: runLabel?.intake_correct !== false,
      note: (runLabel?.note as string) || "",
    });
    const cands = (body?.candidates as Record<string, unknown>[]) || [];
    const drafts: Record<string, { right_part_type: string; should_pass_floor: boolean; note: string }> = {};
    for (const c of cands) {
      const lbl = (c.label as Record<string, unknown>) || undefined;
      drafts[String(c.candidate_id)] = {
        right_part_type: (lbl?.right_part_type as string) || "",
        should_pass_floor: lbl?.should_pass_floor === true,
        note: (lbl?.note as string) || "",
      };
    }
    setCandDrafts(drafts);
  }

  async function submitRunLabel() {
    if (!labelingRunId) return;
    const r = await postAdmin("/labeling/label", token, {
      run_id: labelingRunId, scope: "run",
      label: {
        intake_correct: intakeDraft.intake_correct,
        expected_part_type: intakeDraft.expected_part_type,
        expected_component_of: intakeDraft.expected_component_of || null,
        expected_regime: intakeDraft.expected_regime,
      },
      note: intakeDraft.note || undefined,
      labeled_by: "admin",
    });
    setLabelMsg(r.ok ? "Run label saved." : `Save failed (HTTP ${r.status})`);
    if (r.ok) { refreshQueue(); openLabelingRun(labelingRunId); }
  }

  async function submitCandidateLabel(candidateId: string) {
    if (!labelingRunId) return;
    const d = candDrafts[candidateId];
    const r = await postAdmin("/labeling/label", token, {
      run_id: labelingRunId, scope: "candidate", candidate_ref: candidateId,
      label: { right_part_type: d.right_part_type, should_pass_floor: d.should_pass_floor },
      note: d.note || undefined,
      labeled_by: "admin",
    });
    setLabelMsg(r.ok ? `Candidate ${candidateId} label saved.` : `Save failed (HTTP ${r.status})`);
    if (r.ok) openLabelingRun(labelingRunId);
  }

  async function exportLabels() {
    const r = await postAdmin("/labeling/export", token);
    if (r.ok) {
      const b = r.body as Record<string, Record<string, number>>;
      setLabelMsg(
        `Exported: intake ${b.intake.emitted} emitted/${b.intake.withheld} withheld, ` +
        `scoring ${b.scoring.emitted} emitted/${b.scoring.withheld} withheld.`
      );
    } else {
      setLabelMsg(`Export failed (HTTP ${r.status})`);
    }
  }

  async function refreshQueue() {
    setResult(await fetchAdmin("/labeling/queue", token));
  }

  async function dismissUnmatched(id: string) {
    const r = await postAdmin(`/unmatched-replies/${id}/dismiss`, token);
    if (r.ok) load(tab);        // refresh — a dismissed row drops out of the open list
    else setResult(r);          // surface a gate/guard error in the existing error panel
  }

  async function markPurchased(id: string) {
    const reference = (refInputs[id] ?? "").trim();
    const r = await postAdmin(`/orders/${id}/mark-purchased`, token, { reference });
    if (r.ok) {
      setRefInputs((s) => { const n = { ...s }; delete n[id]; return n; });
      load(tab);                // pending -> placed: the row drops out of the queue
    } else {
      setResult(r);             // surface a gate/guard error in the existing error panel
    }
  }

  // Token gate (UI-side convenience; the API is the real gate).
  if (!token) {
    return (
      <div className="mx-auto max-w-md px-5 py-16 font-mono text-sm">
        <h1 className="text-h2 text-fg-1 mb-2">Inspector — admin token</h1>
        <p className="text-fg-4 text-[12px] mb-4">
          Internal debug surface. Enter the admin token (ARKIM_ADMIN_TOKEN). The server
          enforces it — endpoints return 401/403 without a valid token.
        </p>
        <input
          type="password"
          value={tokenInput}
          onChange={(e) => setTokenInput(e.target.value)}
          placeholder="admin token"
          className="w-full rounded border border-hr-2 bg-bg-3 px-3 py-2 text-fg-1"
        />
        <button
          onClick={saveToken}
          className="mt-3 rounded bg-fg-1 px-4 py-2 text-bg-1 disabled:opacity-40"
          disabled={!tokenInput.trim()}
        >
          Save token
        </button>
      </div>
    );
  }

  const body = result?.body as Record<string, unknown> | undefined;
  const def = TABS.find((x) => x.id === tab)!;
  const rows: Record<string, unknown>[] = Array.isArray(body?.[def.listKey])
    ? (body![def.listKey] as Record<string, unknown>[])
    : [];
  const cols = scalarColumns(rows);

  return (
    <div className="flex flex-col h-full font-mono text-[12px]">
      <div className="shrink-0 flex items-center justify-between border-b border-hr-2 px-5 py-3">
        <div>
          <h1 className="text-h2 text-fg-1">Pipeline Inspector</h1>
          <p className="text-fg-4 text-[10.5px] mt-0.5">read-only · admin-gated</p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => load(tab)} className="rounded border border-hr-2 px-2 py-1 text-fg-2">
            Refresh
          </button>
          <button onClick={clearToken} className="rounded border border-hr-2 px-2 py-1 text-fg-4">
            Clear token
          </button>
        </div>
      </div>

      <div className="shrink-0 flex gap-1 border-b border-hr-2 px-5 py-2">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={
              "rounded px-3 py-1 " +
              (t.id === tab ? "bg-fg-1 text-bg-1" : "text-fg-3 hover:bg-bg-4")
            }
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-auto px-5 py-4">
        {loading && <p className="text-fg-4">loading…</p>}

        {tab === "labeling" && result?.ok && (
          <LabelingView
            token={token}
            queue={result.body as Record<string, unknown>}
            labelingRun={labelingRun}
            labelingRunId={labelingRunId}
            intakeDraft={intakeDraft}
            candDrafts={candDrafts}
            labelMsg={labelMsg}
            onOpenRun={openLabelingRun}
            onIntakeDraft={setIntakeDraft}
            onCandDrafts={setCandDrafts}
            onSubmitRunLabel={submitRunLabel}
            onSubmitCandidateLabel={submitCandidateLabel}
            onExport={exportLabels}
            onRefreshQueue={refreshQueue}
          />
        )}

        {result && !result.ok && (
          <div className="rounded border border-red-fg/40 bg-bg-3 p-4 text-red-fg">
            <p className="font-medium">HTTP {result.status}</p>
            <pre className="mt-1 whitespace-pre-wrap text-[11px]">
              {JSON.stringify(result.body, null, 2)}
            </pre>
            {(result.status === 401 || result.status === 403) && (
              <p className="mt-2 text-fg-4">
                Token rejected by the server gate. Use “Clear token” and re-enter.
              </p>
            )}
          </div>
        )}

        {result?.ok && tab !== "labeling" && (
          <>
            <p className="text-fg-4 mb-2">
              {typeof body?.count === "number" ? body.count : rows.length} record(s)
            </p>

            {rows.length === 0 && <p className="text-fg-4">no rows</p>}

            {rows.length > 0 && (
              <table className="w-full border-collapse text-[11px]">
                <thead>
                  <tr className="text-left text-fg-4 border-b border-hr-2">
                    <th className="py-1 pr-2"></th>
                    {cols.map((c) => (
                      <th key={c} className="py-1 pr-3 font-normal whitespace-nowrap">
                        {c}
                      </th>
                    ))}
                    {(tab === "unmatched-replies" || tab === "fulfilment") && (
                      <th className="py-1 pr-3 font-normal">action</th>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, i) => (
                    <Fragment key={i}>
                      <tr
                        className="border-b border-hr-2/50 hover:bg-bg-4 align-top cursor-pointer"
                        onClick={() => setExpanded(expanded === i ? null : i)}
                      >
                        <td className="py-1 pr-2 text-fg-4">{expanded === i ? "▾" : "▸"}</td>
                        {cols.map((c) => (
                          <td key={c} className="py-1 pr-3 text-fg-2 max-w-[280px] truncate">
                            {tab === "runs" && c === "id" ? (
                              <button
                                className="text-blue-fg underline"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  openRun(String(row.id));
                                }}
                              >
                                {cell(row[c])}
                              </button>
                            ) : (
                              cell(row[c])
                            )}
                          </td>
                        ))}
                        {tab === "unmatched-replies" && (
                          <td className="py-1 pr-3">
                            <button
                              className="rounded border border-hr-2 px-2 py-0.5 text-fg-2 hover:bg-bg-4"
                              onClick={(e) => {
                                e.stopPropagation();
                                dismissUnmatched(String(row.id));
                              }}
                            >
                              Dismiss
                            </button>
                          </td>
                        )}
                        {tab === "fulfilment" && (
                          <td className="py-1 pr-3" onClick={(e) => e.stopPropagation()}>
                            <div className="flex items-center gap-2">
                              <input
                                value={refInputs[String(row.id)] ?? ""}
                                onChange={(e) =>
                                  setRefInputs((s) => ({ ...s, [String(row.id)]: e.target.value }))
                                }
                                placeholder="marketplace ref / tracking"
                                className="rounded border border-hr-2 bg-bg-3 px-2 py-0.5 text-fg-1 w-[180px]"
                              />
                              <button
                                className="rounded border border-hr-2 px-2 py-0.5 text-fg-2 hover:bg-bg-4 whitespace-nowrap"
                                onClick={() => markPurchased(String(row.id))}
                              >
                                Mark purchased
                              </button>
                            </div>
                          </td>
                        )}
                      </tr>
                      {expanded === i && (
                        <tr key={`${i}-raw`} className="bg-bg-3">
                          <td colSpan={cols.length + 1 + (tab === "unmatched-replies" || tab === "fulfilment" ? 1 : 0)} className="p-3">
                            <pre className="whitespace-pre-wrap text-[10.5px] text-fg-2">
                              {JSON.stringify(row, null, 2)}
                            </pre>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}

        {detail && (
          <div className="mt-6 rounded border border-hr-2 bg-bg-3 p-4">
            <p className="text-fg-1 mb-2">
              Run detail {detail.ok ? "" : `(HTTP ${detail.status})`}
            </p>
            <pre className="whitespace-pre-wrap text-[10.5px] text-fg-2">
              {JSON.stringify(detail.body, null, 2)}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Night 2 — Labeling view (function over polish; keyboard-friendly).
// Renders the failures-first queue, a selected run's input/intake/candidates
// with score+verdict, and one-click + keyboard label controls.
// ---------------------------------------------------------------------------

const PART_TYPES = [
  "mechanical_seal", "pump", "valve", "sensor_instrument", "motor_drive", "unknown",
];
const NOUN_CLASSES = ["SEAL", "PUMP", "VALVE", "BEARING", "MOTOR", "SENSOR", "OTHER"];

type IntakeDraft = {
  expected_part_type: string;
  expected_component_of: string;
  expected_regime: string;
  intake_correct: boolean;
  note: string;
};
type CandDraft = { right_part_type: string; should_pass_floor: boolean; note: string };

function LabelingView(props: {
  token: string;
  queue: Record<string, unknown>;
  labelingRun: FetchResult | null;
  labelingRunId: string | null;
  intakeDraft: IntakeDraft;
  candDrafts: Record<string, CandDraft>;
  labelMsg: string;
  onOpenRun: (id: string) => void;
  onIntakeDraft: (d: IntakeDraft) => void;
  onCandDrafts: (d: Record<string, CandDraft>) => void;
  onSubmitRunLabel: () => void;
  onSubmitCandidateLabel: (cid: string) => void;
  onExport: () => void;
  onRefreshQueue: () => void;
}) {
  const {
    queue, labelingRun, labelingRunId, intakeDraft, candDrafts, labelMsg,
    onOpenRun, onIntakeDraft, onCandDrafts, onSubmitRunLabel,
    onSubmitCandidateLabel, onExport, onRefreshQueue,
  } = props;
  const queueRows = (queue.queue as Record<string, unknown>[]) || [];

  const runBody = (labelingRun?.ok ? labelingRun.body : null) as Record<string, unknown> | null;
  const candidates = (runBody?.candidates as Record<string, unknown>[]) || [];
  const specs = (runBody?.asset_specs as Record<string, unknown>) || {};
  const intakeResult = (runBody?.intake_result as Record<string, unknown>) || {};

  // Keyboard-friendly: number keys 1..N jump to the Nth queue row.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (!labelingRunId && e.key >= "1" && e.key <= "9") {
        const idx = parseInt(e.key, 10) - 1;
        if (queueRows[idx]) onOpenRun(String(queueRows[idx].id));
      }
      if (e.key === "Escape") onOpenRun("");
      // s = save run label, x = export.
      if (e.key === "s" && labelingRunId) onSubmitRunLabel();
      if (e.key === "x") onExport();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  return (
    <div className="flex gap-4">
      {/* Queue column */}
      <div className="w-[340px] shrink-0">
        <div className="flex items-center justify-between mb-2">
          <p className="text-fg-4">Labeling queue — failures-first ({queueRows.length})</p>
          <button onClick={onRefreshQueue}
            className="rounded border border-hr-2 px-2 py-0.5 text-fg-3">Refresh</button>
        </div>
        <div className="flex flex-col gap-1">
          {queueRows.length === 0 && <p className="text-fg-4">no runs</p>}
          {queueRows.map((r, i) => (
            <button
              key={String(r.id)}
              onClick={() => onOpenRun(String(r.id))}
              className={
                "text-left rounded border px-2 py-1 " +
                (String(r.id) === labelingRunId
                  ? "border-fg-1 bg-bg-4"
                  : "border-hr-2 hover:bg-bg-4")
              }
            >
              <span className="text-fg-4 mr-2">{i + 1}</span>
              <span className="text-fg-2">{String(r.outcome)}</span>
              {r.labeled ? (
                <span className="ml-2 text-fg-4">✓</span>
              ) : null}
              <span className="block text-fg-4 text-[10px] truncate">
                {String(r.part || r.id)}
              </span>
            </button>
          ))}
        </div>
        <button onClick={onExport}
          className="mt-3 rounded bg-fg-1 px-3 py-1 text-bg-1 text-[11px]">
          Export labeled → eval (x)
        </button>
        {labelMsg && <p className="mt-2 text-fg-3 text-[11px]">{labelMsg}</p>}
      </div>

      {/* Run labeling column */}
      <div className="flex-1">
        {!labelingRunId && (
          <p className="text-fg-4">Pick a run from the queue (or press 1–9).</p>
        )}
        {labelingRun && !labelingRun.ok && (
          <p className="text-red-fg">HTTP {labelingRun.status}</p>
        )}
        {runBody && (
          <div className="flex flex-col gap-4">
            <div className="rounded border border-hr-2 bg-bg-3 p-3">
              <p className="text-fg-1 mb-1">Run {labelingRunId} — provenance {String(runBody.provenance)}</p>
              <p className="text-fg-4 text-[11px]">first user turn (intake input):</p>
              <pre className="whitespace-pre-wrap text-[11px] text-fg-1 mt-1">
                {String(runBody.first_user_turn ?? "—")}
              </pre>
              <details className="mt-2">
                <summary className="text-fg-4 text-[11px] cursor-pointer">asset_specs / intake_result</summary>
                <pre className="whitespace-pre-wrap text-[10.5px] text-fg-2 mt-1">
                  {JSON.stringify({ specs, intake_result: intakeResult }, null, 2)}
                </pre>
              </details>
            </div>

            {/* Intake (run-scope) label controls */}
            <div className="rounded border border-hr-2 p-3">
              <p className="text-fg-1 mb-2">Intake label (run scope)</p>
              <div className="grid grid-cols-2 gap-2 text-[11px]">
                <label className="text-fg-4">expected_part_type
                  <select value={intakeDraft.expected_part_type}
                    onChange={(e) => onIntakeDraft({ ...intakeDraft, expected_part_type: e.target.value })}
                    className="block w-full rounded border border-hr-2 bg-bg-3 px-2 py-1 text-fg-1">
                    <option value="">—</option>
                    {PART_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                </label>
                <label className="text-fg-4">expected_regime
                  <select value={intakeDraft.expected_regime}
                    onChange={(e) => onIntakeDraft({ ...intakeDraft, expected_regime: e.target.value })}
                    className="block w-full rounded border border-hr-2 bg-bg-3 px-2 py-1 text-fg-1">
                    <option value="DIRECT">DIRECT</option>
                    <option value="ANCHORED">ANCHORED</option>
                  </select>
                </label>
                <label className="text-fg-4 col-span-2">expected_component_of (nullable; set only for ANCHORED mechanical_seal)
                  <input value={intakeDraft.expected_component_of}
                    onChange={(e) => onIntakeDraft({ ...intakeDraft, expected_component_of: e.target.value })}
                    className="block w-full rounded border border-hr-2 bg-bg-3 px-2 py-1 text-fg-1" />
                </label>
                <label className="text-fg-4 flex items-center gap-2 col-span-2">
                  <input type="checkbox" checked={intakeDraft.intake_correct}
                    onChange={(e) => onIntakeDraft({ ...intakeDraft, intake_correct: e.target.checked })} />
                  intake_correct
                </label>
                <label className="text-fg-4 col-span-2">note
                  <input value={intakeDraft.note}
                    onChange={(e) => onIntakeDraft({ ...intakeDraft, note: e.target.value })}
                    className="block w-full rounded border border-hr-2 bg-bg-3 px-2 py-1 text-fg-1" />
                </label>
              </div>
              <button onClick={onSubmitRunLabel}
                className="mt-2 rounded bg-fg-1 px-3 py-1 text-bg-1 text-[11px]">
                Save run label (s)
              </button>
            </div>

            {/* Per-candidate label controls */}
            <div className="rounded border border-hr-2 p-3">
              <p className="text-fg-1 mb-2">Candidates ({candidates.length})</p>
              <div className="flex flex-col gap-3">
                {candidates.map((c) => {
                  const cid = String(c.candidate_id);
                  const d = candDrafts[cid] || { right_part_type: "", should_pass_floor: false, note: "" };
                  return (
                    <div key={cid} className="border border-hr-2/60 p-2">
                      <div className="flex items-center justify-between">
                        <span className="text-fg-2 text-[11px]">
                          {String(c.vendor_name ?? cid)} · tier {String(c.tier ?? "—")}
                        </span>
                        <span className={
                          "text-[10px] px-1.5 py-0.5 rounded " +
                          (c.verdict === "rejected" ? "bg-red-fg/15 text-red-fg" : "bg-bg-4 text-fg-3")
                        }>
                          {String(c.verdict)} · score {String(c.suitability_score ?? "—")}
                        </span>
                      </div>
                      {c.rejection_reason ? (
                        <p className="text-red-fg/80 text-[10px] mt-1">reject: {String(c.rejection_reason)}</p>
                      ) : null}
                      <div className="grid grid-cols-3 gap-2 mt-2 text-[11px]">
                        <label className="text-fg-4">right_part_type
                          <select value={d.right_part_type}
                            onChange={(e) => onCandDrafts({ ...candDrafts, [cid]: { ...d, right_part_type: e.target.value } })}
                            className="block w-full rounded border border-hr-2 bg-bg-3 px-1 py-1 text-fg-1">
                            <option value="">—</option>
                            {NOUN_CLASSES.map((t) => <option key={t} value={t}>{t}</option>)}
                          </select>
                        </label>
                        <label className="text-fg-4 flex items-center gap-2">
                          <input type="checkbox" checked={d.should_pass_floor}
                            onChange={(e) => onCandDrafts({ ...candDrafts, [cid]: { ...d, should_pass_floor: e.target.checked } })} />
                          should_pass_floor
                        </label>
                        <label className="text-fg-4">note
                          <input value={d.note}
                            onChange={(e) => onCandDrafts({ ...candDrafts, [cid]: { ...d, note: e.target.value } })}
                            className="block w-full rounded border border-hr-2 bg-bg-3 px-1 py-1 text-fg-1" />
                        </label>
                      </div>
                      <button onClick={() => onSubmitCandidateLabel(cid)}
                        className="mt-2 rounded border border-hr-2 px-2 py-0.5 text-fg-2 text-[11px]">
                        Save candidate label
                      </button>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
