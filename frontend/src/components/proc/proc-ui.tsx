"use client";

import { type ReactNode } from "react";
import { ProcIcon } from "./proc-icon";
import { ArkimMark } from "./arkim-mark";

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

/** Brand loader — the real Arkim mark (ArkimMark) with blue light cascading down it:
 *  its shapes light in sequence top→bottom. The animation lives in the
 *  `.proc-arkloader` CSS (it wraps the static mark, not baked into the component). */
export function ArkimLoader({ size = 34 }: { size?: number }) {
  return (
    <span className="proc-arkloader" style={{ width: size, height: size }} aria-hidden="true">
      <ArkimMark size={size} />
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
