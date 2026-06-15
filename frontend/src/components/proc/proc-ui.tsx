"use client";

import { type ReactNode } from "react";
import { ProcIcon } from "./proc-icon";

export const procMoney = (n: number) => "$" + Number(n).toFixed(2);

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

/** Brand loader — the Arkim mark (anvil bar + three descending arrow bands) with
 *  blue light cascading down it. The four bands light in sequence top→bottom. */
export function ArkimLoader({ size = 34 }: { size?: number }) {
  return (
    <span className="proc-arkloader" style={{ width: size, height: size }} aria-hidden="true">
      <svg viewBox="0 0 120 120" width={size} height={size}>
        <polygon className="ak" points="14,16 52,16 52,25 68,25 68,16 106,16 98,38 22,38" />
        <polygon className="ak" points="26,45 82,45 96,55.5 82,66 26,66 37,55.5" />
        <polygon className="ak" points="34,72 74,72 88,82 74,92 34,92 45,82" />
        <polygon className="ak" points="42,98 66,98 80,108 66,118 42,118 53,108" />
      </svg>
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
