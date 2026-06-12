"use client";

/**
 * SettingsScreen — site delivery (ship-to) settings (frontend spec §7 / proc-settings.jsx).
 *
 * Graduated shipping disclosure: this is where the full ship-to lives; it's pulled into
 * the order review only at placement time. Plain-language form, per-site.
 *
 * Persistence is CLIENT-ONLY (localStorage) until a backend ship-to endpoint exists —
 * the form is functional within the browser; flagged in the build report.
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ProcIcon } from "./proc-icon";
import { ProcHead } from "./proc-ui";
import { PROC_SITES, defaultShipTo, type ShipTo } from "@/lib/proc-config";
import { useSiteShipTo, useSaveSiteShipTo } from "@/lib/queries";

type Field = { k: keyof ShipTo; label: string; placeholder: string; hint?: string; multiline?: boolean };

/** Pick the 6 ship-to fields off the backend payload (drops site_id/updated_at). */
function pickShipTo(s: (ShipTo & { updated_at?: string }) | null | undefined): ShipTo | null {
  if (!s) return null;
  return {
    company: s.company ?? "", address: s.address ?? "", city: s.city ?? "",
    attention: s.attention ?? "", hours: s.hours ?? "", instructions: s.instructions ?? "",
  };
}

const FIELDS: Field[] = [
  { k: "company", label: "Receiving company name", placeholder: "Company name as it should appear on packages" },
  { k: "address", label: "Street address", placeholder: "123 Industrial Blvd" },
  { k: "city", label: "City, state, zip", placeholder: "City, CA 90000" },
  { k: "attention", label: "Attention / point of contact", placeholder: "Name and role — e.g. Sam Torres — Maintenance", hint: "(shown on the shipping label)" },
  { k: "hours", label: "Receiving hours", placeholder: "Mon–Fri, 7:00 AM – 3:30 PM", hint: "(shown to suppliers)" },
  { k: "instructions", label: "Delivery instructions", placeholder: "Dock number, liftgate requirements, call-ahead…", hint: "(optional)", multiline: true },
];

export function SettingsScreen() {
  const router = useRouter();
  const [siteIdx, setSiteIdx] = useState(0);
  const site = PROC_SITES[siteIdx];
  const { data, isLoading } = useSiteShipTo(site.id);
  const saveMutation = useSaveSiteShipTo(site.id);

  const [forms, setForms] = useState<Record<string, ShipTo>>({});
  const [savedId, setSavedId] = useState<string | null>(null);

  // Seed the active site's form when its stored ship-to resolves (saved value, else the
  // seeded default). Only seeds once per site so in-progress edits aren't clobbered.
  useEffect(() => {
    if (isLoading) return;
    setForms((f) => (f[site.id] ? f : { ...f, [site.id]: pickShipTo(data?.ship_to) ?? defaultShipTo(site.id) }));
  }, [site.id, isLoading, data]);

  const form = forms[site.id] ?? defaultShipTo(site.id);
  const update = (k: keyof ShipTo, v: string) =>
    setForms((f) => ({ ...f, [site.id]: { ...(f[site.id] ?? defaultShipTo(site.id)), [k]: v } }));

  const save = () =>
    saveMutation.mutate(form, {
      onSuccess: () => {
        setSavedId(site.id);
        setTimeout(() => setSavedId((s) => (s === site.id ? null : s)), 2500);
      },
    });

  return (
    <div className="proc-max proc-center">
      <button className="proc-back" onClick={() => router.push("/")}>
        <span style={{ display: "inline-flex", transform: "rotate(180deg)" }}><ProcIcon name="chevR" size={14} /></span>
        Home
      </button>
      <ProcHead
        title={<>Delivery <b>settings</b></>}
        sub="Where orders ship to — pulled in automatically when you place an order."
      />

      {PROC_SITES.length > 1 && (
        <div className="proc-sitetabs">
          {PROC_SITES.map((s, i) => (
            <button key={s.id} className="proc-sitetab" data-on={siteIdx === i} onClick={() => setSiteIdx(i)}>
              <span className="d" />
              {s.name}
              <span style={{ fontSize: 11, fontWeight: 500, color: "var(--muted)", marginLeft: 2 }}>{s.sub}</span>
            </button>
          ))}
        </div>
      )}

      <div className="proc-form">
        <div className="pf-h">Ship-to profile — {site.name}</div>
        <div className="pf-s">
          This address appears on every order from this site. Keep the receiving hours accurate so
          suppliers know when to schedule deliveries.
        </div>

        {FIELDS.map((f) => (
          <div className="proc-field" key={f.k}>
            <label>
              {f.label}
              {f.hint && <span className="hint">{f.hint}</span>}
            </label>
            {f.multiline ? (
              <textarea value={form[f.k]} placeholder={f.placeholder} onChange={(e) => update(f.k, e.target.value)} />
            ) : (
              <input value={form[f.k]} placeholder={f.placeholder} onChange={(e) => update(f.k, e.target.value)} />
            )}
          </div>
        ))}

        <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 4 }}>
          <button className="proc-btn" data-kind="quiet" onClick={() => router.push("/")}>Cancel</button>
          <button className="proc-btn" data-kind="primary" onClick={save} disabled={saveMutation.isPending}>
            <ProcIcon name="checkCircle" size={14} />
            {saveMutation.isPending ? "Saving…" : savedId === site.id ? "Saved" : "Save settings"}
          </button>
        </div>
      </div>
    </div>
  );
}
