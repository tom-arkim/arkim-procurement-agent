"""
scripts/refresh_brand_intel.py — Brand Intelligence cache manager.

Usage:
    python scripts/refresh_brand_intel.py list
    python scripts/refresh_brand_intel.py refresh <manufacturer> <equipment_type>
    python scripts/refresh_brand_intel.py warm
    python scripts/refresh_brand_intel.py invalidate <manufacturer> <equipment_type>

Commands:
    list        Show all cached brand intelligence records
    refresh     Force re-discover a single (manufacturer, equipment_type) pair
    warm        Warm the cache for common Arkim equipment types
    invalidate  Mark a record stale so it re-discovers on next access
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.brand_intelligence import (
    get_brand_relationships, warm_cache, all_cached_entries, invalidate
)

# Pairs to pre-warm for common Arkim equipment types
_WARM_PAIRS: list[tuple[str, str]] = [
    ("Gusher", "pump"),
    ("Nagle", "pump"),
    ("Baldor", "motor"),
    ("Leeson", "motor"),
    ("WEG", "motor"),
    ("Goulds", "pump"),
    ("Armstrong", "pump"),
    ("Ingersoll Rand", "compressor"),
    ("Atlas Copco", "compressor"),
    ("Spencer", "blower"),
]


def _print_entry(e: dict) -> None:
    sep = "-" * 70
    print(sep)
    print(f"  Manufacturer : {e['manufacturer']} / {e['equipment_type']}")
    print(f"  Parent       : {e.get('parent_company') or '--'}")
    print(f"  Subsidiaries : {', '.join(e.get('subsidiaries') or []) or '--'}")
    print(f"  Competitors  : {', '.join(e.get('common_competitors') or []) or '--'}")
    print(f"  Niche terms  : {', '.join(e.get('subcategory_niche_terms') or []) or '--'}")
    print(f"  Exclusions   : {', '.join(e.get('wrong_category_terms') or []) or '--'}")
    print(f"  Cached at    : {e.get('last_accessed_at', '--')} UTC "
          f"(TTL {e.get('ttl_days', 90)}d, model: {e.get('llm_model_used', '--')})")
    print()


def cmd_list(args) -> None:
    entries = all_cached_entries()
    if not entries:
        print("Brand intelligence cache is empty. Run: python scripts/refresh_brand_intel.py warm")
        return
    print(f"\n{'='*70}")
    print(f"  BRAND INTELLIGENCE CACHE -- {len(entries)} record(s)")
    print(f"{'='*70}")
    for e in entries:
        _print_entry(e)


def cmd_refresh(args) -> None:
    from utils.brand_intelligence import invalidate
    mfg   = args.manufacturer
    etype = args.equipment_type
    invalidate(mfg, etype)
    print(f"Re-discovering: {mfg!r} / {etype!r} ...")
    r = get_brand_relationships(mfg, etype)
    _print_entry(r)


def cmd_warm(args) -> None:
    print(f"Warming brand intelligence cache for {len(_WARM_PAIRS)} entries...")
    results = warm_cache(_WARM_PAIRS)
    cached  = sum(1 for r in results if r.get("from_cache"))
    fresh   = len(results) - cached
    print(f"Done. {fresh} newly discovered, {cached} already cached.")
    for r in results:
        tag = "(cached)" if r.get("from_cache") else "(new)"
        print(f"  {r['manufacturer']}/{r['equipment_type']:<12} "
              f"parent={r.get('parent_company') or 'none':<12} "
              f"competitors={len(r.get('common_competitors', []))} {tag}")


def cmd_invalidate(args) -> None:
    ok = invalidate(args.manufacturer, args.equipment_type)
    if ok:
        print(f"Invalidated: {args.manufacturer!r} / {args.equipment_type!r}")
        print("Record will be re-discovered on next access.")
    else:
        print(f"Not found: {args.manufacturer!r} / {args.equipment_type!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Arkim Brand Intelligence cache manager")
    sub    = parser.add_subparsers(dest="command")

    sub.add_parser("list",  help="Show all cached records")
    sub.add_parser("warm",  help="Pre-warm common equipment types")

    p_ref = sub.add_parser("refresh", help="Force re-discover a manufacturer/type pair")
    p_ref.add_argument("manufacturer")
    p_ref.add_argument("equipment_type")

    p_inv = sub.add_parser("invalidate", help="Mark a record stale")
    p_inv.add_argument("manufacturer")
    p_inv.add_argument("equipment_type")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "list":       cmd_list,
        "refresh":    cmd_refresh,
        "warm":       cmd_warm,
        "invalidate": cmd_invalidate,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
