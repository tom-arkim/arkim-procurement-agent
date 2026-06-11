/**
 * Customer "Parts & Orders" tenant / site config + ship-to store.
 *
 * FIXTURE/CLIENT-ONLY for now: there is no backend ship-to endpoint (GET /api/facilities
 * returns only {id,name,state}). Ship-to is seeded from these defaults and persisted to
 * localStorage so the form is functional within the browser. Replace getShipTo/saveShipTo
 * with a real facilities/ship-to endpoint when it lands — flagged in the build report.
 */

export const PROC_TENANT = { name: "CAPTEK", sub: "Softgel" };

export interface ShipTo {
  company: string;
  address: string;
  city: string;
  attention: string;
  hours: string;
  instructions: string;
}

export interface ProcSite {
  id: string;
  name: string;
  sub: string;
  shipTo: ShipTo;
}

export const PROC_SITES: ProcSite[] = [
  {
    id: "lamirada",
    name: "La Mirada",
    sub: "Plant · West",
    shipTo: {
      company: "CAPTEK Softgel International",
      address: "14704 Industry Circle",
      city: "La Mirada, CA 90638",
      attention: "Sam Torres — Maintenance",
      hours: "Mon–Fri, 7:00 AM – 3:30 PM",
      instructions: "Deliver to Dock 2 (north side). Call Sam 30 min ahead for liftgate deliveries.",
    },
  },
  {
    id: "rancho",
    name: "Rancho Cucamonga",
    sub: "Plant · East",
    shipTo: {
      company: "CAPTEK Softgel International",
      address: "9774 Crescent Center Dr, Suite 402",
      city: "Rancho Cucamonga, CA 91730",
      attention: "Receiving — front office",
      hours: "Mon–Fri, 8:00 AM – 4:00 PM",
      instructions: "",
    },
  },
];

export const PRIMARY_SITE = PROC_SITES[0];

const key = (siteId: string) => `proc-shipto-${siteId}`;

/** Read a site's ship-to — the saved override if present, else the seeded default. */
export function getShipTo(siteId: string): ShipTo {
  const site = PROC_SITES.find((s) => s.id === siteId) ?? PRIMARY_SITE;
  if (typeof window === "undefined") return site.shipTo;
  try {
    const raw = window.localStorage.getItem(key(siteId));
    if (raw) return { ...site.shipTo, ...JSON.parse(raw) } as ShipTo;
  } catch {
    /* fall through to default */
  }
  return site.shipTo;
}

export function saveShipTo(siteId: string, data: ShipTo): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key(siteId), JSON.stringify(data));
  } catch {
    /* ignore quota/availability errors */
  }
}
