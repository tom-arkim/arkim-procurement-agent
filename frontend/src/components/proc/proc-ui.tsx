"use client";

import { type ReactNode } from "react";
import { ProcIcon } from "./proc-icon";

// Thousands-separated, 2-decimal currency: $13,528.89 (not $13528.89). Keeps cents —
// a price is a price — and groups so large figures read cleanly.
export const procMoney = (n: number) =>
  "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export type ProcTone = "open" | "progress" | "done" | "overdue" | "muted";

export function ProcPill({ tone, children }: { tone: ProcTone; children: ReactNode }) {
  return (
    <span className="proc-pill" data-tone={tone}>
      <span className="d" />
      {children}
    </span>
  );
}

export function SecHead({ t, c }: { t: string; c?: number }) {
  return (
    <div className="proc-sec-h">
      <span className="t">{t}</span>
      {c != null && <span className="c">{c}</span>}
    </div>
  );
}

/** Page header — `title` is a JSX node so callers use the <>plain <b>bold</b></> pattern. */
export function ProcHead({
  title,
  sub,
  actions,
}: {
  title: ReactNode;
  sub?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="proc-dhead">
      <div className="dh-tt">
        <div className="dh-title">{title}</div>
        {sub && <div className="dh-sub">{sub}</div>}
      </div>
      {actions && <div>{actions}</div>}
    </div>
  );
}

export function ChevLoader({ size = 20 }: { size?: number }) {
  return (
    <span className="proc-chevstack" style={{ width: size }} aria-hidden="true">
      <i />
      <i />
      <i />
      <i />
    </span>
  );
}

/** Bottom-center toast (mockup ProcToast), positioned within the proc surface. */
export function ProcToast({ msg }: { msg: string | null }) {
  if (!msg) return null;
  return (
    <div
      style={{
        position: "absolute",
        left: 0,
        right: 0,
        bottom: 26,
        display: "flex",
        justifyContent: "center",
        zIndex: 80,
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          background: "var(--surface-hi)",
          border: "1px solid var(--border)",
          color: "var(--text)",
          fontSize: 13.5,
          fontWeight: 600,
          padding: "11px 15px",
          borderRadius: "var(--r)",
          boxShadow: "0 12px 30px -8px rgba(0,0,0,0.5)",
        }}
      >
        <span style={{ color: "var(--st-done)", display: "flex" }}>
          <ProcIcon name="checkCircle" size={16} />
        </span>
        {msg}
      </div>
    </div>
  );
}
