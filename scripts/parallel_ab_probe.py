"""
Opt-in A/B search probe — Tavily vs Parallel.ai on real queries.

Turns "Parallel seems better than Tavily" into DATA: runs both providers LIVE through
the swappable interface (utils/search_providers) and prints a side-by-side comparison —
per-provider result count, wall-clock latency, the result sets (url / title / first
excerpt), and Parallel's usage/cost signal.

This is the ONLY place live Parallel calls happen — it is NOT a pytest test (scripts/ is
outside the suite's test paths) and the unit suite stays fully mocked. Each run spends
real credits on whichever provider has a key.

Keys (read from the environment / .env you control — never committed):
  TAVILY_API_KEY     — Tavily; if missing, Tavily is skipped (clear message, no crash).
  PARALLEL_API_KEY   — Parallel.ai; if missing, Parallel is skipped.
  PARALLEL_BASE_URL  — optional (default https://api.parallel.ai).
  PARALLEL_SEARCH_MODE — optional; use "base"/"basic" to keep probe cost/latency low.

Run:
  uv run python scripts/parallel_ab_probe.py
  uv run python scripts/parallel_ab_probe.py "Goulds 3196 mechanical seal price USA"
  uv run python scripts/parallel_ab_probe.py --max-results 8
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils.search_providers import ParallelProvider, TavilyProvider  # noqa: E402

# A small fixed set of sourcing-style queries (used when none is given on the CLI).
_DEFAULT_QUERIES = [
    "Baldor EM3770T 7.5HP electric motor distributor price USA",
    "Goulds 3196 MTX mechanical seal cross-reference buy USA",
    '"HLS447SR0608" 150HP TEFC induction motor 460V distributor',
]


def _first_excerpt(result: dict) -> str:
    excerpts = result.get("excerpts")
    if excerpts:
        return str(excerpts[0])[:160]
    return str(result.get("content") or "")[:160]


def _run_one(label: str, provider, query: str, max_results: int) -> dict:
    """Run one provider for one query; return {results, latency_s, error}. Fail-soft —
    the providers never raise, but guard anyway so one provider can't abort the probe."""
    t0 = time.perf_counter()
    try:
        results = provider.search(query, max_results=max_results)
        err = None
    except Exception as exc:  # providers are fail-soft, but never let the probe crash
        results, err = [], f"{type(exc).__name__}: {exc}"
    return {"results": results, "latency_s": time.perf_counter() - t0, "error": err}


def _print_side(label: str, outcome: dict, meta: dict | None) -> None:
    print(f"\n  ── {label} " + "─" * (40 - len(label)))
    if outcome["error"]:
        print(f"     ERROR: {outcome['error']}")
    print(f"     results: {len(outcome['results'])}   latency: {outcome['latency_s']:.2f}s")
    if meta and meta.get("usage"):
        print(f"     usage:   {meta['usage']}")
    for i, r in enumerate(outcome["results"][:8], 1):
        title = (r.get("title") or "—")[:70]
        print(f"     {i:>2}. {r.get('url')}")
        print(f"         {title}")
        ex = _first_excerpt(r)
        if ex:
            print(f"         “{ex}”")


def main() -> int:
    ap = argparse.ArgumentParser(description="A/B probe: Tavily vs Parallel.ai (live).")
    ap.add_argument("query", nargs="?", help="One query to run (default: a built-in set).")
    ap.add_argument("--max-results", type=int, default=10)
    args = ap.parse_args()

    queries = [args.query] if args.query else _DEFAULT_QUERIES

    have_tavily = bool(os.environ.get("TAVILY_API_KEY"))
    have_parallel = bool(os.environ.get("PARALLEL_API_KEY"))
    if not have_tavily:
        print("[probe] TAVILY_API_KEY unset — skipping Tavily.")
    if not have_parallel:
        print("[probe] PARALLEL_API_KEY unset — skipping Parallel.")
    if not (have_tavily or have_parallel):
        print("[probe] No provider keys set — nothing to do.")
        return 1

    tavily = TavilyProvider() if have_tavily else None
    parallel = ParallelProvider() if have_parallel else None

    for q in queries:
        print("\n" + "=" * 78)
        print(f"QUERY: {q}")
        print("=" * 78)
        if tavily is not None:
            _print_side("TAVILY", _run_one("TAVILY", tavily, q, args.max_results), None)
        if parallel is not None:
            out = _run_one("PARALLEL", parallel, q, args.max_results)
            _print_side("PARALLEL", out, parallel.last_meta)

    print("\n[probe] Done. Compare result relevance, counts, and latency above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
