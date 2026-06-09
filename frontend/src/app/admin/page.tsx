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

type Tab = "runs" | "suppliers" | "sent-messages" | "review-queue" | "orders" | "prices";

const TABS: { id: Tab; label: string; path: string; listKey: string }[] = [
  { id: "runs", label: "Runs", path: "/runs", listKey: "runs" },
  { id: "suppliers", label: "Suppliers", path: "/suppliers", listKey: "suppliers" },
  { id: "sent-messages", label: "Sent Messages", path: "/sent-messages", listKey: "sent_messages" },
  { id: "review-queue", label: "Review Queue", path: "/review-queue", listKey: "review_items" },
  { id: "orders", label: "Orders", path: "/orders", listKey: "orders" },
  { id: "prices", label: "Prices", path: "/prices", listKey: "prices" },
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
      setResult(await fetchAdmin(def.path, token));
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

        {result?.ok && (
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
                      </tr>
                      {expanded === i && (
                        <tr key={`${i}-raw`} className="bg-bg-3">
                          <td colSpan={cols.length + 1} className="p-3">
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
