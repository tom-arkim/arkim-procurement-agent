/**
 * Customer "Parts & Orders" tenant / site config + ship-to seed defaults.
 *
 * Ship-to now PERSISTS to the backend (GET/PUT /api/sites/{id}/ship-to via
 * useSiteShipTo/useSaveSiteShipTo). These seeded defaults are the fallback the UI shows
 * before anything is saved. Tenant/site identity stays config-fixture until customer
 * auth + a real facilities wire.
 */

export const PROC_TENANT = { name: "CAPTEK", sub: "Softgel" };

/**
 * The facility id proc runs route against. The customer create-run flow posts no
 * facility_id, so the backend assigns its default (the all-zeros UUID), and order
 * placement keys approval routing on that. The approval-thresholds editor therefore
 * edits THIS facility's rules — so a saved change actually governs the next order.
 * Multi-facility selection arrives with the real facilities/auth wire (Arc 1).
 */
export const PROC_FACILITY_ID = "00000000-0000-0000-0000-000000000000";

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

/** The seeded default ship-to for a site — the fallback shown before anything is saved
 *  to the backend (GET/PUT /api/sites/{id}/ship-to). */
export function defaultShipTo(siteId: string): ShipTo {
  return (PROC_SITES.find((s) => s.id === siteId) ?? PRIMARY_SITE).shipTo;
}
