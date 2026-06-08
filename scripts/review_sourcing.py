"""
Local live harness — run SourcingAgent over representative parts and dump a
per-candidate review table (Tier 3 focus: suitability_status / is_us_confirmed /
rejection_reason), then the supplier_registry Apollo cache after the runs.

Untracked dev tooling. Makes LIVE Anthropic + Tavily + Apollo calls and populates
data/supplier_registry.sqlite (additive Apollo-cache migration on first run).
Does NOT persist sourcing runs to the DB — it only prints.

Usage:
    uv run python scripts/review_sourcing.py          # all parts below
    uv run python scripts/review_sourcing.py 0        # only PARTS[0]
    uv run python scripts/review_sourcing.py 0 2      # PARTS[0] and PARTS[2]
"""
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from utils.models import SourcingRun
from utils.procurement_agent.agents.sourcing_agent import SourcingAgent
from utils import supplier_registry

PARTS = [
    {
        "label": "Gusher mechanical seal (Akman scenario)",
        "specs": {
            "manufacturer": "Gusher Pumps", "model": "Type 21",
            "part_number": "TYPE21", "voltage": "N/A", "category": "Part",
            "detected_type": "mechanical seal",
            "description": "Mechanical seal for Gusher centrifugal pump",
            "material_spec": "Carbon/Silicon Carbide",
            "warranty_status": "unknown",
        },
    },
    {
        "label": "Endress+Hauser PMC11 pressure transmitter",
        "specs": {
            "manufacturer": "Endress+Hauser", "model": "PMC11",
            "part_number": "PMC11-AA1V1HFVXJA", "voltage": "24VDC",
            "category": "Part", "detected_type": "pressure transmitter",
            "description": "Pressure transmitter, ceramic sensor, 0-1bar, 4-20mA",
            "warranty_status": "unknown",
        },
    },
    {
        "label": "Baldor motor (aftermarket-viable)",
        "specs": {
            "manufacturer": "Baldor", "model": "EM3770T",
            "part_number": "EM3770T", "voltage": "230/460V",
            "category": "Part", "detected_type": "motor",
            "description": "7.5 HP 3-phase TEFC industrial motor",
            "warranty_status": "unknown",
        },
    },
]


def _domain(url: str) -> str:
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "").replace("www.", "")
    except Exception:
        return ""


def _run_one(agent: SourcingAgent, label: str, specs: dict) -> None:
    print("\n" + "=" * 110)
    print(f"PART: {label}")
    print("=" * 110)
    run = SourcingRun(
        id=f"review-{abs(hash(label)) % 10**8}",
        facility_id="fac-stockton",
        initiated_by_user_id="review-script",
        initiated_at=datetime.now(timezone.utc),
        current_phase="sourcing",
        urgency_factor=0.5,
        warranty_status=specs.get("warranty_status", "unknown"),
        asset_specs_json=specs,
    )
    res = agent.run(run)

    for tier in ("tier_1", "tier_2", "tier_3"):
        block = res.get(tier) or {}
        rows = block.get("results") or []
        print(f"\n-- {tier}  ({block.get('count')} candidates, status={block.get('status')}) --")
        hdr = (f"{'vendor':34} {'domain':26} {'suitability_status':22} "
               f"{'is_us':5} {'rejection_reason':22} {'suit%':>5}  apollo note/flag")
        print(hdr)
        print("-" * len(hdr))
        for c in rows:
            iv = c.get("is_us_confirmed")
            note = c.get("suitability_note") or c.get("apollo_flag") or "-"
            print(f"{(c.get('vendor_name') or '')[:33]:34} "
                  f"{_domain(c.get('source_url'))[:25]:26} "
                  f"{str(c.get('suitability_status') or '-')[:22]:22} "
                  f"{('-' if iv is None else str(iv)):5} "
                  f"{str(c.get('rejection_reason') or '-')[:21]:22} "
                  f"{float(c.get('suitability_score') or 0):5.0f}  "
                  f"{note}")
    print("\nfilters_applied:", res.get("filters_applied"))
    print("tier3_capability_pivot:", res.get("tier3_capability_pivot"))


def _dump_registry() -> None:
    print("\n" + "=" * 110)
    print("SUPPLIER_REGISTRY APOLLO CACHE (after runs)")
    print("=" * 110)
    rows = supplier_registry.all_entries()
    enriched = [r for r in rows if r.get("apollo_enriched_at")]
    print(f"{len(rows)} total rows; {len(enriched)} with Apollo data\n")
    hdr = (f"{'name':32} {'domain':30} {'country':16} {'is_us':6} "
           f"{'suitability_status':22} {'enriched_at':20}")
    print(hdr)
    print("-" * len(hdr))
    for r in sorted(rows, key=lambda x: (x.get("apollo_enriched_at") or "")):
        print(f"{(r.get('name') or '')[:31]:32} "
              f"{(r.get('domain') or '')[:29]:30} "
              f"{str(r.get('apollo_country', '-') or '-')[:15]:16} "
              f"{str(r.get('is_us_confirmed', '-')):6} "
              f"{str(r.get('suitability_status', '-') or '-')[:21]:22} "
              f"{str(r.get('apollo_enriched_at', '-') or '-')[:19]:20}")


def main() -> None:
    for k in ("ANTHROPIC_API_KEY", "TAVILY_API_KEY", "APOLLO_API_KEY"):
        print(f"{k}: {'set' if os.environ.get(k) else 'MISSING'}")

    args = [int(a) for a in sys.argv[1:] if a.isdigit()]
    selected = [PARTS[i] for i in args] if args else PARTS
    print(f"Running {len(selected)} part(s): {[p['label'] for p in selected]}")

    agent = SourcingAgent(
        tavily_api_key=os.environ.get("TAVILY_API_KEY"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        apollo_api_key=os.environ.get("APOLLO_API_KEY"),
    )
    for p in selected:
        try:
            _run_one(agent, p["label"], p["specs"])
        except Exception as exc:
            print(f"[review] run FAILED for {p['label']}: {type(exc).__name__}: {exc}")

    _dump_registry()


if __name__ == "__main__":
    main()
