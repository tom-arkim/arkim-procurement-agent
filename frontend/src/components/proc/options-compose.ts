/**
 * options-compose — PURE render-time composition of the banded findings list
 * (brief §2.3). No API awareness, no React: takes the server's findings[] (already
 * in banded order) and returns what the options screen should paint. Two steps:
 *
 * 1. Dedup by registrable domain: the same vendor listed at multiple URL
 *    granularities collapses to ONE card. The richest duplicate becomes the card
 *    (supplier-confirmed quote > priced > exact-PN evidence > bare); the rest are
 *    kept as "also listed at N pages" — collapsed, never dropped. The collapsed
 *    card occupies the FIRST position its domain appeared at, so the server's
 *    band order is preserved (a duplicate never jumps the queue).
 *    Candidates without a URL carry no domain evidence and are never collapsed.
 *
 * 2. Priced-vs-quote-needed grouping: "buy now" (has a price) renders apart from
 *    "quote needed" (no price). Grouping is presentation-only — WITHIN each group
 *    the server's relative order is untouched (group-then-preserve-relative-order).
 *
 * This module deliberately does NOT re-sort, re-score, or re-rank anything —
 * server band order is the ranking (brief §1/§2.3). Unit-tested (§7.9):
 * __tests__/options-compose.test.ts.
 */

import type { Candidate } from "@/types";

/** One rendered card: the richest candidate for its domain + its collapsed duplicates. */
export interface ComposedEntry {
  primary: Candidate;
  /** Same-domain duplicates hidden behind the "also listed at N pages" affordance. */
  alsoListed: Candidate[];
}

export interface ComposedFindings {
  /** Entries whose primary carries a price — the "buy now" group, server order kept. */
  buyNow: ComposedEntry[];
  /** Entries with no price — the "quote needed" group, server order kept. */
  quoteNeeded: ComposedEntry[];
}

/** Registrable domain for render-time dedup: host minus "www.", last two labels.
 *  Naive on multi-part public suffixes (co.uk) — acceptable for grouping cards;
 *  a miss means we show two cards, never that we merge two different vendors'
 *  same-suffix domains beyond the registrable label. Empty string = no evidence. */
export function registrableDomain(url: string | undefined | null): string {
  if (!url) return "";
  try {
    const host = new URL(url).hostname.toLowerCase().replace(/^www\./, "");
    const labels = host.split(".").filter(Boolean);
    return labels.length <= 2 ? host : labels.slice(-2).join(".");
  } catch {
    return "";
  }
}

/** Evidence richness for choosing which duplicate fronts the collapsed card:
 *  supplier-confirmed quote > priced > exact-PN listing evidence > bare.
 *  Ties keep the EARLIER server position (stable). */
export function richness(c: Candidate): number {
  if (c.evidenceState === "quoted" && c.quoteConfirmed) return 3;
  if (c.price != null) return 2;
  if (c.foundPartNumber) return 1;
  return 0;
}

/** Collapse same-domain duplicates onto one entry at the domain's first position. */
export function dedupeByDomain(findings: Candidate[]): ComposedEntry[] {
  const slots: ComposedEntry[] = [];
  const byDomain = new Map<string, ComposedEntry>();
  for (const c of findings) {
    const domain = registrableDomain(c.url);
    if (!domain) {
      slots.push({ primary: c, alsoListed: [] });
      continue;
    }
    const existing = byDomain.get(domain);
    if (!existing) {
      const entry: ComposedEntry = { primary: c, alsoListed: [] };
      byDomain.set(domain, entry);
      slots.push(entry);
      continue;
    }
    // Same vendor domain again: the richer one fronts the card; the other is
    // kept behind the affordance. Strictly-greater keeps ties stable (earlier wins).
    if (richness(c) > richness(existing.primary)) {
      existing.alsoListed.push(existing.primary);
      existing.primary = c;
    } else {
      existing.alsoListed.push(c);
    }
  }
  return slots;
}

/** Full composition: dedup, then group priced apart from quote-needed,
 *  preserving each group's internal server order. */
export function composeFindings(findings: Candidate[]): ComposedFindings {
  const entries = dedupeByDomain(findings);
  return {
    buyNow: entries.filter((e) => e.primary.price != null),
    quoteNeeded: entries.filter((e) => e.primary.price == null),
  };
}
