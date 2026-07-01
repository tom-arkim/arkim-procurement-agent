"""
Diagnostic: live Tier 2 and Tier 3 trace for three scenarios.
Runs SourcingAgent and reports every candidate with per-stage disposition.
No production code changes — read-only investigation script.
"""
import os, sys, io, contextlib
from datetime import datetime, timezone
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from utils.models import SourcingRun
from utils.procurement_agent.agents.sourcing_agent import SourcingAgent

SEP  = "=" * 80
SEP2 = "-" * 80

SCENARIOS = [
    {
        "label": "SCENARIO 1: Baldor 5HP motor 1750rpm 460V 184T frame 3-phase",
        "specs": {
            "manufacturer": "Baldor",
            "model": "N/A",
            "part_number": "UNKNOWN-PN",
            "voltage": "460V",
            "category": "Equipment",
            "description": "5 HP induction motor 1750 RPM 460V 184T frame 3 phase",
            "detected_type": "induction motor",
            "warranty_status": "unknown",
            "hp": "5HP",
            "rpm": "1750",
            "frame": "184T",
            "phase": "3-phase",
        },
    },
    {
        "label": "SCENARIO 2: Promag 10W (maintenance handoff demo-001)",
        "specs": {
            "manufacturer": "Endress+Hauser",
            "model": "Promag 10W",
            "part_number": "10W40-AA2B1AA0AAAA",
            "voltage": "N/A",
            "category": "Equipment",
            "description": "Complete electromagnetic flow meter (full unit replacement), Promag 10W",
            "detected_type": "electromagnetic flow meter",
            "warranty_status": "unknown",
        },
    },
    {
        "label": "SCENARIO 3: Hyundai Crown Triton 15HP induction motor",
        "specs": {
            "manufacturer": "Hyundai",
            "model": "Crown Triton",
            "part_number": "UNKNOWN-PN",
            "voltage": "460V",
            "category": "Equipment",
            "description": "Hyundai Crown Triton 15HP 3-phase induction motor 460V",
            "detected_type": "induction motor",
            "warranty_status": "unknown",
            "hp": "15HP",
            "frame": "254T",
            "phase": "3-phase",
            "rpm": "1800",
        },
    },
]


def host(url):
    if not url:
        return "(no url)"
    try:
        return urlparse(url.lower()).hostname or "(no host)"
    except Exception:
        return "(bad url)"


def run_scenario(scenario):
    print(SEP)
    print(scenario["label"])
    print(SEP)

    run = SourcingRun(
        id=f"diag-{datetime.now(timezone.utc).strftime('%H%M%S')}",
        facility_id="fac-diag",
        initiated_by_user_id="diag-script",
        initiated_at=datetime.now(timezone.utc),
        current_phase="sourcing",
        urgency_factor=0.5,
        warranty_status="unknown",
        asset_specs_json=scenario["specs"],
    )

    agent = SourcingAgent(
        tavily_api_key=os.environ.get("TAVILY_API_KEY"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
    )

    # Capture stdout from the agent run (stage logs go to stdout)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = agent.run(run)
    logs = buf.getvalue()

    # Print captured logs so we can see every stage message
    print("--- STAGE LOGS (stdout from agent.run) ---")
    for line in logs.splitlines():
        print("  LOG:", line)
    print()

    # Report Tier 2 candidates
    tier2 = result.get("tier_2", {})
    candidates = tier2.get("results", [])
    print(f"--- TIER 2 CANDIDATES (count={tier2.get('count',0)}, status={tier2.get('status','?')}) ---")
    if not candidates:
        print("  (none)")
    else:
        for c in candidates:
            rej = c.get("rejection_reason") or "ACTIVE"
            suit = c.get("suitability_score")
            suit_s = f"{suit:.0f}%" if suit is not None else "-"
            pn_status = c.get("pn_match_status") or "-"
            found_pn = c.get("found_part_number") or "-"
            match_type = c.get("match_type") or "-"
            url = c.get("source_url") or ""
            print(f"  [{rej:30s}] {(c.get('vendor_name') or '?')[:28]:28s} suit={suit_s:5s} "
                  f"pn_status={pn_status:15s} found_pn={found_pn[:25]:25s} "
                  f"match={match_type[:22]:22s} host={host(url)}")

    # Report Tier 3 candidates (where _discover_national_specialists lives)
    tier3 = result.get("tier_3", {})
    candidates3 = tier3.get("results", [])
    print()
    print(f"--- TIER 3 CANDIDATES (count={tier3.get('count',0)}, status={tier3.get('status','?')}) ---")
    if not candidates3:
        print("  (none)")
    else:
        for c in candidates3:
            rej = c.get("rejection_reason") or "ACTIVE"
            suit = c.get("suitability_score")
            suit_s = f"{suit:.0f}%" if suit is not None else "-"
            pn_status = c.get("pn_match_status") or "-"
            found_pn = c.get("found_part_number") or "-"
            match_type = c.get("match_type") or "-"
            url = c.get("source_url") or ""
            print(f"  [{rej:30s}] {(c.get('vendor_name') or '?')[:28]:28s} suit={suit_s:5s} "
                  f"pn_status={pn_status:15s} found_pn={found_pn[:25]:25s} "
                  f"match={match_type[:22]:22s} host={host(url)}")

    print()
    print("  filters_applied:", result.get("filters_applied"))
    print("  tier3_pivot    :", result.get("tier3_capability_pivot"))
    print()
    return logs, result


if __name__ == "__main__":
    for s in SCENARIOS:
        run_scenario(s)
        print()
