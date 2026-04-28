"""
scripts/inspect_audit_log.py — CLI tool to display recent audit log entries.

Usage:
    python scripts/inspect_audit_log.py [--limit N] [--run-id UUID]

Options:
    --limit N       Show N most recent entries (default: 10)
    --run-id UUID   Show the specific run matching this sourcing_run_id
    --json          Dump raw JSON instead of formatted table
"""
import argparse
import json
import os
import sys

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.audit_log import recent_entries, get_entry


def _fmt_vendors(json_str: str, limit: int = 4) -> str:
    if not json_str:
        return "--"
    try:
        vendors = json.loads(json_str)
        names   = [v.get("vendor_name", "?") for v in vendors[:limit]]
        suffix  = f" +{len(vendors)-limit} more" if len(vendors) > limit else ""
        return ", ".join(names) + suffix
    except Exception:
        return json_str[:60]


def _print_entry(e: dict, verbose: bool = False) -> None:
    sep = "-" * 80
    print(sep)
    print(f"  Run ID     : {e.get('sourcing_run_id', '--')}")
    print(f"  Created    : {e.get('created_at', '--')} UTC")
    print(f"  Asset      : {e.get('input_summary', '--')}")
    print(f"  Workflow   : {e.get('workflow_mode', '--')} | "
          f"Urgency: {e.get('urgency_factor_used', '--')} | "
          f"Warranty: {e.get('warranty_status_used') or 'unknown'}")
    print(f"  Vendors    : {_fmt_vendors(e.get('vendors_surfaced', ''))}")
    print(f"  Recommended: {e.get('final_recommendation') or '--'} "
          f"| User chose: {e.get('user_selection') or '(none)'}")
    print(f"  LLM calls  : {e.get('llm_calls_made', 0)} "
          f"| Est. cost: ${e.get('estimated_llm_cost_usd', 0.0):.4f} "
          f"| Duration: {e.get('duration_ms', '?')} ms")
    print(f"  Agent ver  : {e.get('agent_version', '--')}")

    errors = []
    try:
        errors = json.loads(e.get("error_log") or "[]")
    except Exception:
        pass
    if errors:
        print(f"  [!] Errors : {'; '.join(str(x) for x in errors[:3])}")

    if verbose:
        print()
        specs = {}
        try:
            specs = json.loads(e.get("asset_specs_json") or "{}")
        except Exception:
            pass
        if specs:
            print("  Asset Specs:")
            for k in ("manufacturer", "model", "part_number", "voltage", "category"):
                if specs.get(k):
                    print(f"    {k:<16}: {specs[k]}")

        considered = []
        try:
            considered = json.loads(e.get("vendors_considered") or "[]")
        except Exception:
            pass
        if considered:
            print(f"\n  All {len(considered)} vendor(s) considered:")
            for v in considered:
                tbd  = v.get("price_tbd", False)
                pstr = "TBD" if tbd else f"${v.get('base_price', 0):.2f}"
                print(f"    * {v.get('vendor_name', '?'):<28} {pstr:<10} "
                      f"{v.get('merchant_type', '?')}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect Arkim audit log entries")
    parser.add_argument("--limit",  type=int, default=10, help="Number of recent entries to show")
    parser.add_argument("--run-id", type=str, default=None, help="Show a specific sourcing_run_id")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full vendor and spec details")
    parser.add_argument("--json",   action="store_true", help="Dump raw JSON")
    args = parser.parse_args()

    if args.run_id:
        entry = get_entry(args.run_id)
        if not entry:
            print(f"No entry found for run_id: {args.run_id}")
            sys.exit(1)
        entries = [entry]
    else:
        entries = recent_entries(args.limit)

    if not entries:
        print("No audit log entries found.")
        print("Run the pipeline first, or check that data/audit_log.sqlite exists.")
        sys.exit(0)

    if args.json:
        print(json.dumps(entries, indent=2, default=str))
        return

    print(f"\n{'=' * 80}")
    print(f"  ARKIM AUDIT LOG -- {len(entries)} entr{'y' if len(entries)==1 else 'ies'}")
    print(f"{'=' * 80}")
    for e in entries:
        _print_entry(e, verbose=args.verbose)


if __name__ == "__main__":
    main()
