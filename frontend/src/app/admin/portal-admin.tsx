"use client";

/**
 * Night 6 — Supplier claim-portal admin controls (T4 + T5).
 *
 * Two presentational components used by the admin inspector (page.tsx):
 *  - ClaimLinkPanel (T4): the show-once claim-link display (copy + expiry +
 *    "won't see this again" note + regenerate).
 *  - PortalRevisionsView (T5): the pending supplier-proposed revisions review
 *    (approve/reject, closing the propose→approve loop the public claim page
 *    starts).
 *
 * State/network live in page.tsx; these are pure renders over the data the
 * admin page fetches via its token-gated fetchAdmin/postAdmin helpers.
 */

export type ClaimLink = {
  supplier_domain: string;
  supplier_name: string | null;
  token: string;
  token_id: string;
  expires_at: string;
  link_path: string;
};

export type FetchResult = { ok: boolean; status: number; body: unknown };

// T4 — Show-once claim-link panel. The raw token is returned ONCE by the
// backend (hashed at rest); this panel renders it with a copy button, the
// expiry, and a clear "copy and send now — you won't see this again" note.
// Regenerate revokes the prior token and mints a new one (same show-once rule).
export function ClaimLinkPanel(props: {
  link: ClaimLink;
  copied: boolean;
  busy: boolean;
  onCopy: () => void;
  onRegenerate: () => void;
}) {
  const { link, copied, busy, onCopy, onRegenerate } = props;
  const full =
    (typeof window !== "undefined" ? window.location.origin : "") + link.link_path;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <p className="text-fg-1">
          Claim link for{" "}
          <span className="text-fg-2">{link.supplier_name ?? link.supplier_domain}</span>
        </p>
        <span className="text-fg-4 text-[10px]">expires {link.expires_at}</span>
      </div>
      <div className="flex items-center gap-2">
        <code className="flex-1 rounded border border-hr-2 bg-bg-2 px-2 py-1 text-fg-1 text-[11px] break-all">
          {full}
        </code>
        <button
          onClick={onCopy}
          className="rounded bg-fg-1 px-3 py-1 text-bg-1 text-[11px] whitespace-nowrap"
        >
          {copied ? "Copied" : "Copy link"}
        </button>
      </div>
      <p className="text-amber-fg text-[11px]">
        Copy and send this now — the raw token is shown once and won&apos;t be
        visible again.
      </p>
      <div className="flex items-center gap-2">
        <button
          onClick={onRegenerate}
          disabled={busy}
          className="rounded border border-hr-2 px-3 py-1 text-fg-2 text-[11px] hover:bg-bg-4"
        >
          Regenerate (revokes this link)
        </button>
      </div>
    </div>
  );
}

// T5 — Pending supplier-proposed revisions review. Each row is a review_items
// kind="supplier_revision" (status="needs_human_review") carrying the proposed
// scope (brands/classes/ship_area) in its payload. Approve applies the scope to
// the registry via the admin endpoint; reject discards.
export function PortalRevisionsView(props: {
  rows: Record<string, unknown>[];
  msg: string;
  result: FetchResult | null;
  onApprove: (id: string) => void;
  onReject: (id: string) => void;
  onRefresh: () => void;
}) {
  const { rows, msg, result, onApprove, onReject, onRefresh } = props;
  const pending = rows.filter((r) => r["status"] === "needs_human_review");
  const resolved = rows.filter((r) => r["status"] !== "needs_human_review");

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <p className="text-fg-4">
          Pending supplier-proposed revisions ({pending.length})
        </p>
        <button
          onClick={onRefresh}
          className="rounded border border-hr-2 px-2 py-0.5 text-fg-3"
        >
          Refresh
        </button>
      </div>

      {result && !result.ok && (
        <p className="text-red-fg">
          HTTP {result.status}
          {result.status === 503
            ? " — supplier portal disabled (SUPPLIER_PORTAL_V1 off)"
            : ""}
        </p>
      )}

      {msg && <p className="text-fg-3 text-[11px]">{msg}</p>}

      {pending.length === 0 && resolved.length === 0 && (
        <p className="text-fg-4">No supplier-proposed revisions.</p>
      )}

      {pending.map((r) => {
        const id = String(r["id"] ?? "");
        const payload = (r["payload"] as Record<string, unknown> | undefined) || {};
        const brands = (payload["brands"] as Record<string, unknown>[]) || [];
        const classes = (payload["classes"] as Record<string, unknown>[]) || [];
        const ship = payload["ship_area"] as Record<string, unknown> | null;
        return (
          <div key={id} className="rounded border border-hr-2 bg-bg-3 p-3 text-[11px]">
            <div className="flex items-center justify-between mb-2">
              <p className="text-fg-1">
                {String(payload["domain"] ?? r["supplier_domain"] ?? "—")}
                <span className="text-fg-4 ml-2">
                  proposed by {String(payload["proposed_by"] ?? "supplier")}
                </span>
              </p>
              <span className="text-fg-4 text-[10px]">{String(r["created_at"] ?? "")}</span>
            </div>
            <RevisionScope brands={brands} classes={classes} ship={ship} />
            <div className="flex gap-2 mt-3">
              <button
                onClick={() => onApprove(id)}
                className="rounded bg-fg-1 px-4 py-1.5 text-bg-1 text-[11px]"
              >
                Approve → apply to registry
              </button>
              <button
                onClick={() => onReject(id)}
                className="rounded border border-hr-2 px-4 py-1.5 text-fg-2 text-[11px]"
              >
                Reject (discard)
              </button>
            </div>
          </div>
        );
      })}

      {resolved.length > 0 && (
        <details className="mt-2">
          <summary className="text-fg-4 text-[11px] cursor-pointer">
            Resolved ({resolved.length})
          </summary>
          <div className="flex flex-col gap-2 mt-2">
            {resolved.map((r) => {
              const id = String(r["id"] ?? "");
              const payload = (r["payload"] as Record<string, unknown> | undefined) || {};
              const brands = (payload["brands"] as Record<string, unknown>[]) || [];
              const classes = (payload["classes"] as Record<string, unknown>[]) || [];
              const ship = payload["ship_area"] as Record<string, unknown> | null;
              return (
                <div key={id} className="rounded border border-hr-2/60 p-2 text-[11px] opacity-70">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-fg-2">
                      {String(payload["domain"] ?? r["supplier_domain"] ?? "—")}
                    </span>
                    <span
                      className={
                        "text-[10px] px-1.5 py-0.5 rounded " +
                        (r["status"] === "confirmed"
                          ? "bg-green-fg/15 text-green-fg"
                          : "bg-red-fg/15 text-red-fg")
                      }
                    >
                      {String(r["status"])}
                    </span>
                  </div>
                  <RevisionScope brands={brands} classes={classes} ship={ship} />
                </div>
              );
            })}
          </div>
        </details>
      )}
    </div>
  );
}

function RevisionScope(props: {
  brands: Record<string, unknown>[];
  classes: Record<string, unknown>[];
  ship: Record<string, unknown> | null;
}) {
  const { brands, classes, ship } = props;
  return (
    <div className="flex flex-col gap-2">
      <div>
        <p className="text-fg-4 mb-1">Brands ({brands.length})</p>
        <div className="flex flex-wrap gap-1">
          {brands.length === 0 && <span className="text-fg-4">—</span>}
          {brands.map((b, i) => (
            <span
              key={i}
              className="rounded border border-hr-2 px-2 py-0.5 text-[10px] text-fg-2"
            >
              {String(b["brand_id"] ?? "—")} · {String(b["relationship"] ?? "—")}
            </span>
          ))}
        </div>
      </div>
      <div>
        <p className="text-fg-4 mb-1">Classes ({classes.length})</p>
        <div className="flex flex-wrap gap-1">
          {classes.length === 0 && <span className="text-fg-4">—</span>}
          {classes.map((c, i) => (
            <span
              key={i}
              className={
                "rounded px-2 py-0.5 text-[10px] border " +
                (c["is_core"] ? "border-fg-1 text-fg-1" : "border-hr-2 text-fg-3")
              }
            >
              {String(c["class_id"] ?? "—")}
              {c["is_core"] ? " ★" : ""}
            </span>
          ))}
        </div>
      </div>
      <div>
        <p className="text-fg-4 mb-1">Ship area</p>
        <p className="text-fg-2">
          {ship?.kind === "STATES"
            ? `STATES: ${JSON.stringify(ship["states"] ?? [])}`
            : ship?.kind
              ? String(ship["kind"])
              : "—"}
        </p>
      </div>
    </div>
  );
}
