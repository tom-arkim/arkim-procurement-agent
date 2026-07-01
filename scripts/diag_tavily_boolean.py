"""
Task 1 diagnostic: Empirically test whether Tavily honors Boolean scaffolding.

Runs two paired queries for Promag 10W (the clearest failing Tier 3 scenario):
  Query A: current (authorized OR ...) AND "type" AND "mfg" Boolean format
  Query B: proposed natural-language quoted-anchors format

Reports result counts, top-5 URLs side-by-side, and marketplace hits.
No modifications to _build_tier3_query.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

import utils.sourcing_archieved as _pkg

MARKETPLACE_DOMAINS = {
    "mouser", "digikey", "grainger", "automationdirect", "mcmaster",
    "motion", "zoro", "mscdirect", "instrumart", "transcat",
}

QUERY_A = '(authorized OR distributor OR "service center") AND "electromagnetic flow meter" AND "Endress+Hauser" USA'
QUERY_B = '"electromagnetic flow meter" "Endress+Hauser" authorized distributor buy USA'

SEP = "-" * 80

def run_query(label: str, query: str) -> list[dict]:
    print(f"\n{SEP}")
    print(f"QUERY {label}: {query!r}")
    print(SEP)

    if not _pkg._tavily:
        print("ERROR: Tavily client not initialised -- TAVILY_API_KEY missing.")
        sys.exit(1)

    try:
        response = _pkg._tavily.search(query=query, search_depth="advanced", max_results=10)
        results  = response.get("results", [])
    except Exception as exc:
        print(f"ERROR: {exc}")
        return []

    print(f"Result count: {len(results)}")
    return results


def report(label: str, results: list[dict]) -> None:
    print(f"\n--- Top URLs (Query {label}) ---")
    marketplace_hits = []
    for i, r in enumerate(results[:10]):
        url   = r.get("url", "")
        title = r.get("title", "")[:60]
        is_mp = any(m in url.lower() for m in MARKETPLACE_DOMAINS)
        marker = " [MARKETPLACE]" if is_mp else ""
        if is_mp:
            marketplace_hits.append(url)
        print(f"  {i+1:2}. {url}{marker}")
        print(f"       {title}")
    if marketplace_hits:
        print(f"\n  Marketplace hits ({len(marketplace_hits)}): {marketplace_hits}")
    else:
        print("\n  No marketplace listings in top 10.")


def main() -> None:
    if not _pkg._tavily:
        print("ERROR: TAVILY_API_KEY not set in environment / .env")
        sys.exit(1)

    results_a = run_query("A (Boolean)", QUERY_A)
    results_b = run_query("B (Natural-language)", QUERY_B)

    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    print(f"  Query A result count : {len(results_a)}")
    print(f"  Query B result count : {len(results_b)}")

    report("A", results_a)
    report("B", results_b)

    # Side-by-side URL comparison
    print(f"\n--- Side-by-side top 5 URLs ---")
    print(f"{'#':<4} {'Query A':<55} {'Query B':<55}")
    print("-" * 114)
    for i in range(5):
        url_a = results_a[i].get("url", "") if i < len(results_a) else "(none)"
        url_b = results_b[i].get("url", "") if i < len(results_b) else "(none)"
        print(f"{i+1:<4} {url_a[:54]:<55} {url_b[:54]:<55}")

    # Overlap
    urls_a = {r.get("url") for r in results_a}
    urls_b = {r.get("url") for r in results_b}
    overlap = urls_a & urls_b
    print(f"\n  Overlapping URLs between A and B: {len(overlap)}")
    if overlap:
        for u in sorted(overlap):
            print(f"    {u}")


if __name__ == "__main__":
    main()
