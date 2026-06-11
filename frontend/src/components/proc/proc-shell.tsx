"use client";

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useRouter, usePathname } from "next/navigation";
import { ProcIcon, type ProcIconName } from "./proc-icon";
import { ProcToast } from "./proc-ui";
import { PROC_TENANT, PRIMARY_SITE } from "@/lib/proc-config";

// Tenant / site framing comes from proc-config (fixture until customer auth + a real
// facilities/ship-to wire). Flagged in the build report.
const PROC_SITE = PRIMARY_SITE;

// ---------------------------------------------------------------------------
// Toast context — children call fire() for "coming soon" + confirmations.
// ---------------------------------------------------------------------------

const ProcToastContext = createContext<(msg: string) => void>(() => {});
export const useProcToast = () => useContext(ProcToastContext);

// ---------------------------------------------------------------------------
// Nav
// ---------------------------------------------------------------------------

type NavItem = { key: string; label: string; icon: ProcIconName; href?: string; soon?: boolean };

const NAV: NavItem[] = [
  { key: "home", label: "What needs me", icon: "box", href: "/" },
  { key: "request", label: "New request", icon: "plus", href: "/request" },
  { key: "history", label: "History & prices", icon: "receipt", soon: true },
  { key: "settings", label: "Delivery settings", icon: "building", href: "/settings" },
];

export function ProcShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();

  const [theme, setTheme] = useState<"dark" | "light">("dark");
  useEffect(() => {
    const saved = typeof window !== "undefined" ? window.localStorage.getItem("ark-theme") : null;
    if (saved === "light" || saved === "dark") setTheme(saved);
  }, []);
  const toggleTheme = () =>
    setTheme((t) => {
      const n = t === "dark" ? "light" : "dark";
      window.localStorage.setItem("ark-theme", n);
      return n;
    });

  const [toast, setToast] = useState<string | null>(null);
  const tRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fire = (msg: string) => {
    setToast(msg);
    if (tRef.current) clearTimeout(tRef.current);
    tRef.current = setTimeout(() => setToast(null), 1900);
  };

  const onNav = (item: NavItem) => {
    if (item.soon) fire("Coming in the next build");
    else if (item.href) router.push(item.href);
  };

  return (
    <div className="proc-theme" data-theme={theme} style={{ position: "absolute", inset: 0 }}>
      <ProcToastContext.Provider value={fire}>
        <div className="proc-dash">
          {/* left nav */}
          <nav className="proc-dnav">
            <div className="proc-brand">
              <span className="bm">A</span>
              <span className="bn">arkim</span>
            </div>

            <div className="proc-navsite">
              <span className="sd" />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="sn">{PROC_SITE.name}</div>
                <div className="ss">{PROC_SITE.sub}</div>
              </div>
              <ProcIcon name="chevD" size={15} color="var(--muted)" />
            </div>

            <div className="proc-navlabel">Parts &amp; Orders</div>

            <div className="proc-navlist">
              {NAV.map((item) => {
                const active = item.href === "/" ? pathname === "/" : Boolean(item.href) && pathname.startsWith(item.href!);
                return (
                  <button
                    key={item.key}
                    className="proc-navitem"
                    data-on={active}
                    onClick={() => onNav(item)}
                    title={item.label}
                  >
                    <ProcIcon name={item.icon} size={18} className="ni-ic" />
                    <span>{item.label}</span>
                  </button>
                );
              })}
            </div>
          </nav>

          {/* main */}
          <div className="proc-dmain">
            <div className="proc-dtop">
              <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
                <span className="dt-tenant">{PROC_TENANT.name}</span>
                <span className="dt-sub">{PROC_TENANT.sub}</span>
              </div>
              <div className="dt-spacer" />
              <button className="proc-iconbtn" onClick={toggleTheme} title="Toggle theme">
                <ProcIcon name={theme === "dark" ? "sun" : "moon"} size={18} />
              </button>
              <button className="proc-iconbtn" onClick={() => fire("Notifications")} title="Notifications">
                <ProcIcon name="bell" size={18} />
              </button>
              <button className="proc-avatar" onClick={() => fire("Account")}>
                CS
              </button>
            </div>

            <div className="proc-dbody">{children}</div>
          </div>
        </div>

        <ProcToast msg={toast} />
      </ProcToastContext.Provider>
    </div>
  );
}
