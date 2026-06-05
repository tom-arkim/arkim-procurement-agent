"""
Diagnostic: FS Curtis NX 4 oil filter — Tier 2 zero-result trace + Tier 3 characterization.
Run from project root: python scripts/diag_fscurtis_tier2.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import utils.sourcing_archieved as _pkg
from utils.sourcing_archieved.tavily_client import (
    _build_search_query,
    _build_tier3_query,
    _vendor_authority_score,
    _AUTHORITY_VIABLE_THRESHOLD,
    _DYNAMIC_FALLBACK_MIN_VIABLE,
)
from utils.sourcing_archieved.constants import _VENDOR_DOMAINS
from utils.sourcing_archieved.scoring import _detect_equip_type, _is_collection_url
from utils.price_db import get_cached_prices

# ---------------------------------------------------------------------------
# Reconstruct specs as intake would produce for FS Curtis oil filter
# ---------------------------------------------------------------------------
class Specs:
    def __init__(self, **kw):
        self.__dict__.update(kw)

# Two plausible intake outcomes — run both to see which query path is hit
SPEC_VARIANTS = [
    Specs(
        manufacturer="FS Curtis",
        model="NX 4",
        part_number="2605539890",
        category="Part",
        description="oil filter",
        detected_type="oil filter",
        hp=None, rpm=None, gpm=None, psi=None, frame=None,
    ),
    Specs(
        manufacturer="FS Curtis",
        model=None,
        part_number="2605539890",
        category="Part",
        description="oil filter",
        detected_type="oil filter",
        hp=None, rpm=None, gpm=None, psi=None, frame=None,
    ),
]

SEP = "=" * 70

def score_result(r):
    return _vendor_authority_score(r.get("url",""), r.get("content",""), r.get("title",""))

# ---------------------------------------------------------------------------
# STAGE 1 — Cache lookup
# ---------------------------------------------------------------------------
print(f"\n{SEP}")
print("STAGE 1 — Cache lookup (price_db)")
print(SEP)
cached = get_cached_prices("2605539890")
if cached:
    for vendor, data in cached.items():
        print(f"  HIT: {vendor} @ ${data['price']:.2f}  (fetched {data.get('date_fetched','?')[:10]})")
else:
    print("  MISS — no cached prices for 2605539890")

# ---------------------------------------------------------------------------
# STAGE 2 — Query construction, both variants
# ---------------------------------------------------------------------------
print(f"\n{SEP}")
print("STAGE 2 — Query construction (_build_search_query)")
print(SEP)
for i, specs in enumerate(SPEC_VARIANTS):
    q = _build_search_query(specs, search_mode="exact")
    print(f"  Variant {i+1} (model={'NX 4' if specs.model else 'None'}): {q!r}")
    eq = _detect_equip_type(specs)
    print(f"    _detect_equip_type -> {eq!r}  |  category={specs.category!r}")

# Use variant 1 (with model) for the live search since NX 4 is the most likely intake output
specs = SPEC_VARIANTS[0]
t2_query = _build_search_query(specs, search_mode="exact")
t3_query = _build_tier3_query(specs)
print(f"\n  USING FOR LIVE SEARCH: {t2_query!r}")
print(f"  Tier 3 query for comparison: {t3_query!r}")

# Check whether compressedairadvisors.com is in _VENDOR_DOMAINS
caa_in_domains = any("compressedairadvisors" in d for d in _VENDOR_DOMAINS)
print(f"\n  compressedairadvisors.com in _VENDOR_DOMAINS: {caa_in_domains}")
print(f"  _VENDOR_DOMAINS: {_VENDOR_DOMAINS}")

# ---------------------------------------------------------------------------
# STAGE 3 — Live Tavily search (Pass 1: unrestricted)
# ---------------------------------------------------------------------------
print(f"\n{SEP}")
print("STAGE 3 — Tavily live search (unrestricted, max_results=15)")
print(SEP)

if not _pkg._tavily:
    print("  ERROR: Tavily not initialised. Check TAVILY_API_KEY.")
    sys.exit(1)

resp = _pkg._tavily.search(query=t2_query, search_depth="advanced", max_results=15)
raw_results = resp.get("results", [])
print(f"  Raw result count: {len(raw_results)}")
print()

for idx, r in enumerate(raw_results):
    url = r.get("url", "")
    title = r.get("title", "")
    content_preview = (r.get("content","") or "")[:120].replace("\n"," ")
    auth_score = score_result(r)
    passes = auth_score >= _AUTHORITY_VIABLE_THRESHOLD
    caa_flag = " *** compressedairadvisors.com ***" if "compressedairadvisors" in url.lower() else ""
    print(f"  [{idx+1:2d}] score={auth_score:5.1f} {'PASS' if passes else 'FAIL'}  {url}{caa_flag}")
    print(f"        title: {title[:80]}")
    print(f"        snippet: {content_preview}")
    print()

caa_in_raw = any("compressedairadvisors" in (r.get("url","").lower()) for r in raw_results)
print(f"  compressedairadvisors.com in raw results: {caa_in_raw}")

# ---------------------------------------------------------------------------
# STAGE 4 — Authority score breakdown per result
# ---------------------------------------------------------------------------
print(f"\n{SEP}")
print("STAGE 4 — Authority score breakdown (_vendor_authority_score, threshold={})".format(_AUTHORITY_VIABLE_THRESHOLD))
print(SEP)

viable = [r for r in raw_results if score_result(r) >= _AUTHORITY_VIABLE_THRESHOLD]
print(f"  Viable after Pass 1: {len(viable)} / {len(raw_results)}")
for r in viable:
    print(f"    PASS  score={score_result(r):.1f}  {r.get('url','')}")
for r in raw_results:
    if score_result(r) < _AUTHORITY_VIABLE_THRESHOLD:
        print(f"    FAIL  score={score_result(r):.1f}  {r.get('url','')}")

# Authority score anatomy for compressedairadvisors.com specifically
print("\n  Authority score anatomy for compressedairadvisors.com (simulated):")
test_url = "https://www.compressedairadvisors.com/products/curtis-nx-4-15-oil-filter-pn-2605539890"
test_content = "FS Curtis NX 4-15 Oil Filter PN 2605539890. Add to cart. In stock. $24.95. Industrial air compressor supply distributor."
test_title = "Curtis NX 4-15 Oil Filter 2605539890 | Compressed Air Advisors"
print(f"    URL: {test_url}")
print(f"    Simulated content: {test_content}")
print(f"    Score: {_vendor_authority_score(test_url, test_content, test_title):.1f}")
print(f"    (Is collection URL: {_is_collection_url(test_url)})")

# ---------------------------------------------------------------------------
# STAGE 5 — Fallback domain-restricted search
# ---------------------------------------------------------------------------
print(f"\n{SEP}")
print(f"STAGE 5 — Fallback search (triggers if viable < {_DYNAMIC_FALLBACK_MIN_VIABLE})")
print(SEP)

if len(viable) < _DYNAMIC_FALLBACK_MIN_VIABLE:
    print(f"  Fallback TRIGGERED (viable={len(viable)} < {_DYNAMIC_FALLBACK_MIN_VIABLE})")
    fb_resp = _pkg._tavily.search(query=t2_query, search_depth="advanced",
                                   max_results=10, include_domains=_VENDOR_DOMAINS)
    fb_results = fb_resp.get("results", [])
    print(f"  Fallback returned {len(fb_results)} results:")
    existing_urls = {r.get("url") for r in viable}
    for r in fb_results:
        flag = " (new)" if r.get("url") not in existing_urls else " (dup)"
        print(f"    {r.get('url','')}{flag}")
    # Merge
    for r in fb_results:
        if r.get("url") not in existing_urls:
            viable.append(r)
    print(f"  Total viable after fallback: {len(viable)}")
else:
    print(f"  Fallback NOT triggered (viable={len(viable)} >= {_DYNAMIC_FALLBACK_MIN_VIABLE})")

# ---------------------------------------------------------------------------
# STAGE 6 — LLM parsing (report what survives into the parser)
# ---------------------------------------------------------------------------
print(f"\n{SEP}")
print("STAGE 6 — LLM parse input")
print(SEP)
print(f"  Results passed to _llm_parse_results: {len(viable)}")
for r in viable:
    print(f"    {r.get('url','')}")

# ---------------------------------------------------------------------------
# STAGE 7-9 — Run full _call_enterprise_api and report final
# ---------------------------------------------------------------------------
print(f"\n{SEP}")
print("STAGES 7-9 — Full pipeline via _call_enterprise_api")
print(SEP)
from utils.sourcing_archieved.enterprise_search import _call_enterprise_api
options = _call_enterprise_api(specs, force_refresh=True, search_mode="exact")
print(f"\n  Final Tier 2 options: {len(options)}")
for o in options:
    print(f"    vendor={o.vendor_name!r:30s}  price={'$'+str(round(o.base_price,2)) if not o.price_tbd else 'TBD':10s}  suit={o.suitability_score:.0f}%  url={o.source_url}")

# ---------------------------------------------------------------------------
# PART 2 — Tier 3 live search + characterization
# ---------------------------------------------------------------------------
print(f"\n{SEP}")
print("PART 2 — Tier 3 query + raw results characterization")
print(SEP)
print(f"  Tier 3 query: {t3_query!r}")
print()

t3_resp = _pkg._tavily.search(query=t3_query, search_depth="advanced", max_results=10)
t3_raw = t3_resp.get("results", [])
print(f"  Raw Tier 3 results: {len(t3_raw)}")
for idx, r in enumerate(t3_raw):
    url = r.get("url","")
    title = r.get("title","")
    content = (r.get("content","") or "")[:200].replace("\n"," ")
    print(f"\n  [{idx+1}] {url}")
    print(f"       title: {title}")
    print(f"       snippet: {content}")

print(f"\n{SEP}")
print("DIAGNOSTIC COMPLETE")
print(SEP)
