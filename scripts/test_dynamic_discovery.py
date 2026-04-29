"""
scripts/test_dynamic_discovery.py — Phase 3.3 validation

Tests dynamic Tier 1 vendor discovery against 20 sample parts/equipment.
Compares: (a) unrestricted results vs (b) domain-restricted fallback.

Usage:
    python scripts/test_dynamic_discovery.py [--limit N]

Output: summary table of viable vendor count per query, plus aggregated stats.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.models import AssetSpecs
from utils.sourcing import _build_search_query, _vendor_authority_score, _VENDOR_DOMAINS

try:
    from tavily import TavilyClient
except ImportError:
    from tavily import Client as TavilyClient

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

# 20 representative samples: parts + equipment, obscure + common
_SAMPLES: list[tuple[str, AssetSpecs]] = [
    ("Gusher pump seal",    AssetSpecs(manufacturer="Gusher", model="GUS-50",
                             part_number="SL-7743", voltage="N/A", category="Part",
                             description="mechanical seal")),
    ("Baldor motor 5HP",    AssetSpecs(manufacturer="Baldor", model="EM3710T",
                             part_number="EM3710T", voltage="230/460V", category="Equipment",
                             description="TEFC motor 5HP")),
    ("Grainger bearing",    AssetSpecs(manufacturer="SKF", model="6206-2RS",
                             part_number="6206-2RS", voltage="N/A", category="Part",
                             description="deep groove ball bearing")),
    ("VFD AB PowerFlex",    AssetSpecs(manufacturer="Allen-Bradley", model="22B-D2P3N104",
                             part_number="22B-D2P3N104", voltage="460V", category="Part",
                             description="VFD PowerFlex 40")),
    ("Leeson motor 1HP",    AssetSpecs(manufacturer="Leeson", model="110086.00",
                             part_number="110086.00", voltage="115/208-230V", category="Equipment",
                             description="single-phase motor 1HP")),
    ("Goulds pump 3656",    AssetSpecs(manufacturer="Goulds", model="3656 M",
                             part_number="3656M", voltage="208-230/460V", category="Equipment",
                             description="centrifugal pump", gpm="50 GPM")),
    ("Falk coupling",       AssetSpecs(manufacturer="Falk", model="1060T10",
                             part_number="1060T10", voltage="N/A", category="Part",
                             description="shaft coupling")),
    ("Dodge bearing",       AssetSpecs(manufacturer="Dodge", model="P2B-SCEZ-100",
                             part_number="P2B-SCEZ-100", voltage="N/A", category="Part",
                             description="pillow block bearing")),
    ("Teco Westinghouse",   AssetSpecs(manufacturer="Teco-Westinghouse", model="TEFC-TEM-7510",
                             part_number="TEFC-TEM-7510", voltage="230/460V", category="Equipment",
                             description="electric motor 10HP")),
    ("Xylem pump",          AssetSpecs(manufacturer="Xylem", model="LF 3/4",
                             part_number="LF34", voltage="115V", category="Equipment",
                             description="circulator pump")),
    ("Siemens contactor",   AssetSpecs(manufacturer="Siemens", model="3RT2016-1AB01",
                             part_number="3RT2016-1AB01", voltage="24VAC", category="Part",
                             description="IEC contactor 9A")),
    ("Ingersoll Rand comp", AssetSpecs(manufacturer="Ingersoll Rand", model="2545",
                             part_number="2545E10", voltage="208-230/460V", category="Equipment",
                             description="rotary screw compressor")),
    ("Gates V-belt",        AssetSpecs(manufacturer="Gates", model="B78",
                             part_number="B78", voltage="N/A", category="Part",
                             description="classical V-belt")),
    ("Rexnord seal",        AssetSpecs(manufacturer="Rexnord", model="MS-7550",
                             part_number="MS-7550", voltage="N/A", category="Part",
                             description="mechanical face seal")),
    ("WEG motor 3HP",       AssetSpecs(manufacturer="WEG", model="00318ET3EAL",
                             part_number="00318ET3EAL", voltage="208-230/460V", category="Equipment",
                             description="TEFC motor 3HP")),
    ("ABB VFD ACS550",      AssetSpecs(manufacturer="ABB", model="ACS550-U1-05A4-4",
                             part_number="ACS550-U1-05A4-4", voltage="380-480V", category="Part",
                             description="variable frequency drive 3HP")),
    ("Grundfos pump CM5",   AssetSpecs(manufacturer="Grundfos", model="CM5-6",
                             part_number="96806877", voltage="208-230/460V", category="Equipment",
                             description="multistage centrifugal pump")),
    ("NSK bearing",         AssetSpecs(manufacturer="NSK", model="6210ZZCM",
                             part_number="6210ZZCM", voltage="N/A", category="Part",
                             description="deep groove ball bearing")),
    ("Dayton motor",        AssetSpecs(manufacturer="Dayton", model="6K346",
                             part_number="6K346", voltage="115/208-230V", category="Equipment",
                             description="TEFC motor 1/2HP")),
    ("Parker filter",       AssetSpecs(manufacturer="Parker", model="933876Q",
                             part_number="933876Q", voltage="N/A", category="Part",
                             description="hydraulic filter element")),
]

_AUTHORITY_THRESHOLD = 30.0


def run_sample(label: str, specs: AssetSpecs, client: "TavilyClient",
               search_mode: str = "exact") -> dict:
    query = _build_search_query(specs, search_mode=search_mode)
    result = {
        "label":           label,
        "query":           query[:80],
        "unrestricted":    0,
        "viable":          0,
        "fallback_needed": False,
        "domains_found":   [],
        "error":           None,
    }
    try:
        resp    = client.search(query=query, search_depth="advanced", max_results=12)
        results = resp.get("results", [])
        result["unrestricted"] = len(results)

        viable_rs = [
            r for r in results
            if _vendor_authority_score(r.get("url", ""), r.get("content", ""), r.get("title", ""))
               >= _AUTHORITY_THRESHOLD
        ]
        result["viable"] = len(viable_rs)
        result["fallback_needed"] = len(viable_rs) < 3

        from urllib.parse import urlparse
        result["domains_found"] = list({
            urlparse(r.get("url", "")).hostname or "?"
            for r in viable_rs
        })[:5]
    except Exception as exc:
        result["error"] = str(exc)[:60]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 3.3 dynamic discovery test")
    parser.add_argument("--limit", type=int, default=20,
                        help="Number of samples to test (default: all 20)")
    args = parser.parse_args()

    if not TAVILY_API_KEY:
        print("ERROR: TAVILY_API_KEY not set.")
        sys.exit(1)

    client  = TavilyClient(api_key=TAVILY_API_KEY)
    samples = _SAMPLES[:args.limit]

    print(f"\n{'='*90}")
    print(f"  PHASE 3.3 — Dynamic Tier 1 Discovery Test  ({len(samples)} samples)")
    print(f"{'='*90}")
    print(f"  {'Label':<24} {'Mode':<7} {'Total':>6} {'Viable':>7} {'Fallback?':<11} {'Domains'}")
    print(f"  {'-'*85}")

    results = []
    for label, specs in samples:
        mode = "equiv" if specs.category == "Equipment" else "exact"
        r    = run_sample(label, specs, client, search_mode="equivalents" if mode == "equiv" else "exact")
        results.append(r)
        fb   = "YES" if r["fallback_needed"] else "no"
        doms = ", ".join(r["domains_found"][:3]) or "--"
        err  = f" [ERR: {r['error']}]" if r["error"] else ""
        print(f"  {label:<24} {mode:<7} {r['unrestricted']:>6} {r['viable']:>7} {fb:<11} {doms}{err}")
        time.sleep(0.5)  # polite rate-limit

    # Summary stats
    total     = len(results)
    errors    = sum(1 for r in results if r["error"])
    fallbacks = sum(1 for r in results if r["fallback_needed"] and not r["error"])
    avg_viable = (sum(r["viable"] for r in results if not r["error"]) / max(1, total - errors))

    print(f"\n{'='*90}")
    print(f"  SUMMARY")
    print(f"  Samples run   : {total}  |  Errors: {errors}")
    print(f"  Fallback needed: {fallbacks}/{total - errors} ({100*fallbacks/max(1,total-errors):.0f}%)")
    print(f"  Avg viable/run : {avg_viable:.1f}")
    print(f"{'='*90}\n")


if __name__ == "__main__":
    main()
