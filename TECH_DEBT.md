# TECH_DEBT.md

Forward-looking debt items that are OUT OF SCOPE for the change that surfaced them.
(Distinct from CLEANUP.md, which tracks the standing cleanup backlog; entries here
graduate into CLEANUP.md or a ticket when picked up.)

---

## 1. Vendor identity across DIFFERENT registrable domains (dedup)

**Surfaced by:** MATCHING_CLEANUP F3 (2026-07-22).

F3 normalized all dedup keying (cross-tier dedup, seed merge, post-band pass) to the
**registrable domain** (`utils/url_normalize.registrable_domain`), so subdomain
variants of one vendor (`www.` / `static.` / `catalog.`) now collapse to one card.

What it deliberately does NOT solve: **one vendor operating multiple registrable
domains**. Observed live on the Gusher seal run: "Springer Parts / Springer Pumps,
LLC" at `springerparts.com` alongside the `catalog.springerpumps.com` seed — two
registrable domains (`springerparts.com` ≠ `springerpumps.com`), one real-world
supplier, two cards. Domain keying can never join these; name similarity alone is
too weak (see the §5a alias cases: OTC Industrial / OTC Industrial Technologies).

**Resolution path:** the entity-resolution / equivalence layer (BOM placement brief —
three-level sameness, link-don't-merge, under-merge principles; spans `onboarding` +
`core` services, Arc 2). Vendor identity records should link multiple domains to one
supplier id; dedup then keys on supplier id when a link exists, registrable domain
otherwise. Do NOT attempt with heuristics inside the sourcing pipeline.
