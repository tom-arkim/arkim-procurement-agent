/**
 * §7.9 — render-time dedup and priced-vs-quote grouping demonstrably preserve
 * server band order within groups. Pure-function tests; no DOM.
 */
import { describe, it, expect } from "vitest";
import { composeFindings, dedupeByDomain, registrableDomain, richness } from "../options-compose";
import type { Candidate } from "@/types";

/** Minimal candidate factory — only the fields composition reads. */
function cand(over: Partial<Candidate> & { id: string }): Candidate {
  return {
    vendorName: over.id,
    vendorType: "RegionalSpecialist",
    tier: 3,
    leadTime: null,
    url: "",
    suitability: 0,
    confidence: 0,
    pnMatchLevel: "none",
    loc: "",
    ...over,
  } as Candidate;
}

describe("registrableDomain", () => {
  it("normalizes to the registrable domain", () => {
    expect(registrableDomain("https://www.sealit.com/p/123")).toBe("sealit.com");
    expect(registrableDomain("https://shop.sealit.com/x")).toBe("sealit.com");
    expect(registrableDomain("")).toBe("");
    expect(registrableDomain("not a url")).toBe("");
  });
});

describe("richness ladder", () => {
  it("ranks quoted > priced > exact-PN > bare", () => {
    const quoted = cand({ id: "q", evidenceState: "quoted", quoteConfirmed: true, price: 10 });
    const priced = cand({ id: "p", price: 10 });
    const pn = cand({ id: "n", foundPartNumber: "A-1" });
    const bare = cand({ id: "b" });
    expect(richness(quoted)).toBeGreaterThan(richness(priced));
    expect(richness(priced)).toBeGreaterThan(richness(pn));
    expect(richness(pn)).toBeGreaterThan(richness(bare));
  });
});

describe("dedupeByDomain", () => {
  it("collapses same-domain listings to one card fronted by the richest", () => {
    const bare = cand({ id: "sealit-bare", url: "https://sealit.com/catalog" });
    const priced = cand({ id: "sealit-priced", url: "https://www.sealit.com/p/gusher", price: 129 });
    const other = cand({ id: "dxp", url: "https://dxpe.com/x", price: 200 });
    const out = dedupeByDomain([bare, other, priced]);
    expect(out).toHaveLength(2);
    // Collapsed card sits at the domain's FIRST server position (slot 0)…
    expect(out[0].primary.id).toBe("sealit-priced");
    // …with the weaker duplicate kept behind the affordance, never dropped.
    expect(out[0].alsoListed.map((c) => c.id)).toEqual(["sealit-bare"]);
    expect(out[1].primary.id).toBe("dxp");
  });

  it("keeps the earlier candidate on a richness tie (stable)", () => {
    const a = cand({ id: "first", url: "https://sealit.com/a", price: 100 });
    const b = cand({ id: "second", url: "https://sealit.com/b", price: 90 });
    const out = dedupeByDomain([a, b]);
    expect(out[0].primary.id).toBe("first");
    expect(out[0].alsoListed.map((c) => c.id)).toEqual(["second"]);
  });

  it("never collapses candidates without a URL (no domain evidence)", () => {
    const a = cand({ id: "a" });
    const b = cand({ id: "b" });
    expect(dedupeByDomain([a, b])).toHaveLength(2);
  });
});

describe("composeFindings — grouping preserves server order within groups", () => {
  it("splits buy-now from quote-needed without reordering either group", () => {
    const f = [
      cand({ id: "p1", url: "https://one.com", price: 10 }),
      cand({ id: "u1", url: "https://two.com" }),
      cand({ id: "p2", url: "https://three.com", price: 20 }),
      cand({ id: "u2", url: "https://four.com" }),
      cand({ id: "p3", url: "https://five.com", price: 30 }),
    ];
    const { buyNow, quoteNeeded } = composeFindings(f);
    // Interleaved server order groups cleanly…
    expect(buyNow.map((e) => e.primary.id)).toEqual(["p1", "p2", "p3"]);
    // …and each group's internal order is exactly the server's relative order.
    expect(quoteNeeded.map((e) => e.primary.id)).toEqual(["u1", "u2"]);
  });

  it("groups by the collapsed card's evidence (a priced duplicate lifts its domain into buy-now)", () => {
    const f = [
      cand({ id: "sealit-bare", url: "https://sealit.com/catalog" }),
      cand({ id: "sealit-priced", url: "https://sealit.com/p/1", price: 129 }),
    ];
    const { buyNow, quoteNeeded } = composeFindings(f);
    expect(buyNow.map((e) => e.primary.id)).toEqual(["sealit-priced"]);
    expect(quoteNeeded).toHaveLength(0);
  });

  it("returns empty groups for an empty findings list (honest empty state upstream)", () => {
    const { buyNow, quoteNeeded } = composeFindings([]);
    expect(buyNow).toEqual([]);
    expect(quoteNeeded).toEqual([]);
  });
});
