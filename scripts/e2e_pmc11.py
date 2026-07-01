"""
E2E canonical scenario: Endress+Hauser PMC11-AA1V1HFVXJA
Reports tier results with vendor_name, URL host, suitability, and rejection_reason.
"""
import os, sys
from datetime import datetime, timezone
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from utils.models import SourcingRun
from utils.procurement_agent.agents.sourcing_agent import SourcingAgent

# ---------------------------------------------------------------------------
# Build canonical E+H PMC11 run
# ---------------------------------------------------------------------------

run = SourcingRun(
    id="e2e-pmc11-canonical",
    facility_id="fac-stockton",
    initiated_by_user_id="e2e-script",
    initiated_at=datetime.now(timezone.utc),
    current_phase="sourcing",
    urgency_factor=0.5,
    warranty_status="unknown",
    asset_specs_json={
        "manufacturer": "Endress+Hauser",
        "model": "PMC11",
        "part_number": "PMC11-AA1V1HFVXJA",
        "voltage": "24VDC",
        "category": "Part",
        "description": "Pressure transmitter, ceramic sensor, 0-1bar, 4-20mA",
        "detected_type": "pressure transmitter",
        "warranty_status": "unknown",
    },
)

# ---------------------------------------------------------------------------
# Run agent
# ---------------------------------------------------------------------------

agent = SourcingAgent(
    tavily_api_key=os.environ.get("TAVILY_API_KEY"),
    anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
)

SEP  = "=" * 70
SEP2 = "-" * 70

print(SEP)
print("E2E: Endress+Hauser PMC11-AA1V1HFVXJA")
print(SEP)
result = agent.run(run)

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def host(url):
    if not url:
        return "(no url)"
    try:
        return urlparse(url.lower()).hostname or "(no host)"
    except Exception:
        return "(bad url)"


def fmt_candidate(c):
    suit = c.get("suitability_score")
    suit_str = ("%3.0f%%" % suit) if suit is not None else "  -  "
    rejected = c.get("rejection_reason") or ""
    url = c.get("source_url") or ""
    label = "[REJECTED]" if rejected else "[active]  "
    return (
        "  %s %-30s suit=%s  host=%-35s  %s"
        % (label, (c.get("vendor_name") or "?")[:30], suit_str, host(url)[:35], rejected)
    )


for tier_key in ("tier_1", "tier_2", "tier_3"):
    tier = result[tier_key]
    candidates = tier.get("results", [])
    status = tier.get("status", "?")
    print("")
    print(SEP2)
    print("%s  status=%s  count=%d" % (tier_key.upper(), status, tier.get("count", 0)))
    print(SEP2)
    if not candidates:
        print("  (no candidates)")
    else:
        active   = [c for c in candidates if not c.get("rejection_reason")]
        rejected = [c for c in candidates if c.get("rejection_reason")]
        if active:
            print("  ACTIVE:")
            for c in active:
                print(fmt_candidate(c))
        if rejected:
            print("  REJECTED:")
            for c in rejected:
                print(fmt_candidate(c))

print("")
print(SEP2)
print("filters_applied : %s" % result.get("filters_applied"))
print("urgency_applied : %s" % result.get("urgency_applied"))
print("tier3_pivot     : %s" % result.get("tier3_capability_pivot"))
print("warranty_banner : %s" % ("yes" if result.get("warranty_banner") else "no"))
print(SEP)
