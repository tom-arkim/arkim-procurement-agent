"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useRouter, usePathname } from "next/navigation";
import { ProcIcon, type ProcIconName } from "./proc-icon";
import { ProcToast } from "./proc-ui";
import { GoferMark } from "./gofer-mark";
import { PROC_TENANT, PRIMARY_SITE } from "@/lib/proc-config";
import { BRAND_NAME } from "@/lib/brand";
import { useEvents } from "@/lib/queries";
import type { EventItem } from "@/types";

// Tenant / site framing comes from proc-config (fixture until customer auth + a real
// facilities/ship-to wire). Flagged in the build report.
const PROC_SITE = PRIMARY_SITE;

// ---------------------------------------------------------------------------
// Toast context — children call fire() for "coming soon" + confirmations.
// ---------------------------------------------------------------------------

const ProcToastContext = createContext<(msg: string) => void>(() => {});
export const useProcToast = () => useContext(ProcToastContext);

// ---------------------------------------------------------------------------
// Events context — the derived notification feed + a per-DEVICE "new since you last
// looked" marker. ONE marker (localStorage) drives BOTH the bell unread badge and the
// dashboard "new update" dots — no second source of truth. The marker is per-device
// (client-side), NOT a server-side per-user read-state: the feed is untargeted and
// real-state-only; opening the bell records what THIS device has seen.
// ---------------------------------------------------------------------------

const EVENTS_LAST_SEEN_KEY = "arkim:events:lastSeen";

type ProcEventsValue = {
  events: EventItem[];
  unreadCount: number;
  /** True when this run has an event newer than the last-seen marker (same marker as the badge). */
  isRunUpdated: (runId: string | null | undefined) => boolean;
};

const ProcEventsContext = createContext<ProcEventsValue>({
  events: [],
  unreadCount: 0,
  isRunUpdated: () => false,
});
export const useProcEvents = () => useContext(ProcEventsContext);

/** Relative time for feed rows. Timestamps are backend ISO-8601 UTC. */
function eventRelTime(iso?: string | null): string {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// ---------------------------------------------------------------------------
// Nav
// ---------------------------------------------------------------------------

type NavItem = { key: string; label: string; icon: ProcIconName; href?: string; soon?: boolean };

const NAV: NavItem[] = [
  { key: "home", label: "What needs me", icon: "box", href: "/" },
  { key: "request", label: "New request", icon: "plus", href: "/request" },
  { key: "approvals", label: "Approvals", icon: "checkCircle", href: "/approvals" },
  { key: "history", label: "History & prices", icon: "receipt", href: "/history" },
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

  // ---- Notification feed (derived, untargeted, real-state) + per-device "seen" marker ----
  const { data: events = [], refetch: refetchEvents } = useEvents();
  const [lastSeen, setLastSeen] = useState<string>("");
  useEffect(() => {
    // localStorage is client-only — read after mount so SSR markup matches (no spurious badge).
    const saved = typeof window !== "undefined" ? window.localStorage.getItem(EVENTS_LAST_SEEN_KEY) : null;
    if (saved) setLastSeen(saved);
  }, []);

  // Timestamps are ISO-8601 UTC across sources, so string comparison is chronological.
  const unreadCount = useMemo(
    () => events.filter((e) => e.timestamp && e.timestamp > lastSeen).length,
    [events, lastSeen],
  );
  const isRunUpdated = useCallback(
    (runId: string | null | undefined) =>
      Boolean(runId) && events.some((e) => e.run_id === runId && e.timestamp && e.timestamp > lastSeen),
    [events, lastSeen],
  );

  const [bellOpen, setBellOpen] = useState(false);
  const bellRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!bellOpen) return;
    const onDown = (e: MouseEvent) => {
      if (bellRef.current && !bellRef.current.contains(e.target as Node)) setBellOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setBellOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [bellOpen]);

  const toggleBell = () => {
    setBellOpen((open) => {
      const next = !open;
      if (next) {
        void refetchEvents();
        // Mark everything currently known as seen → clears badge AND dashboard dots (one
        // marker). Use the newest known event timestamp (not the client clock) so clock
        // skew can't leave a just-seen event unread; fall back to "now" if the feed is empty.
        const newest = events.reduce(
          (max, e) => (e.timestamp && e.timestamp > max ? e.timestamp : max),
          lastSeen,
        );
        const mark = newest || new Date().toISOString();
        setLastSeen(mark);
        if (typeof window !== "undefined") window.localStorage.setItem(EVENTS_LAST_SEEN_KEY, mark);
      }
      return next;
    });
  };

  const onNav = (item: NavItem) => {
    if (item.soon) fire("Coming in the next build");
    else if (item.href) router.push(item.href);
  };

  return (
    <div className="proc-theme" data-theme={theme} style={{ position: "absolute", inset: 0 }}>
      <ProcToastContext.Provider value={fire}>
       <ProcEventsContext.Provider value={{ events, unreadCount, isRunUpdated }}>
        <div className="proc-dash">
          {/* left nav */}
          <nav className="proc-dnav">
            <button type="button" className="proc-brand" onClick={() => router.push("/")} aria-label={`${BRAND_NAME} — go to home`}>
              <span className="bm"><GoferMark size={20} /></span>
              <span className="bn">{BRAND_NAME.toLowerCase()}</span>
            </button>

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
              <div className="proc-bellwrap" ref={bellRef}>
                <button
                  className="proc-iconbtn"
                  onClick={toggleBell}
                  title="Notifications"
                  aria-haspopup="menu"
                  aria-expanded={bellOpen}
                  aria-label={unreadCount > 0 ? `Notifications, ${unreadCount} new` : "Notifications"}
                >
                  <ProcIcon name="bell" size={18} />
                  {unreadCount > 0 && (
                    <span className="proc-bellbadge">{unreadCount > 9 ? "9+" : unreadCount}</span>
                  )}
                </button>
                {bellOpen && (
                  <div className="proc-bellmenu" role="menu">
                    <div className="proc-bellhead">Recent updates</div>
                    {events.length === 0 ? (
                      <div className="proc-bellempty">No recent updates.</div>
                    ) : (
                      <div className="proc-belllist">
                        {events.map((e) => (
                          <button
                            key={e.id}
                            className="proc-bellitem"
                            role="menuitem"
                            disabled={!e.run_id}
                            onClick={() => {
                              setBellOpen(false);
                              if (e.run_id) router.push(`/parts/${e.run_id}`);
                            }}
                          >
                            <span className="bi-title">{e.title}</span>
                            <span className="bi-time">{eventRelTime(e.timestamp)}</span>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
              <button className="proc-avatar" onClick={() => fire("Account")}>
                CS
              </button>
            </div>

            <div className="proc-dbody">{children}</div>
          </div>
        </div>

        <ProcToast msg={toast} />
       </ProcEventsContext.Provider>
      </ProcToastContext.Provider>
    </div>
  );
}
