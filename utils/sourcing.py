"""
Module C — Multi-Tier Sourcing Engine
Tier 1 / 1.5 : Real-time Tavily search across national distributors
                (Grainger, McMaster-Carr, MSC Industrial, Motion Industries,
                 Applied Industrial, Pumpman)
Tier 2        : Real-time local discovery via Tavily + Claude entity extraction
Tier 3        : Managed RFQ Outreach — per-vendor email drafts from Tier 2 contacts
"""

import os
import json
import re
import uuid
import requests
from typing import Optional

try:
    from tavily import TavilyClient
except ImportError:
    from tavily import Client as TavilyClient

from utils.models import AssetSpecs, SourcingOption

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

TAVILY_API_KEY      = os.environ.get("TAVILY_API_KEY")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
_EXTRACTION_MODEL   = os.environ.get("OS_EXTRACTION_MODEL", "claude-haiku-4-5-20251001")

_tavily = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None

TARGET_VENDORS = [
    # Tier 1: Generalist Enterprise
    "Grainger", "McMaster-Carr", "MSC Industrial",
    # Tier 1.5: Industrial & Equipment Specialists
    "Motion Industries", "Applied Industrial", "Pumpman",
    "Pump Products", "Pump Catalog", "Zoro", "Global Industrial", "Fastenal",
]

_VENDOR_DOMAINS = [
    # Tier 1
    "grainger.com", "mcmaster.com", "mscdirect.com",
    # Tier 1.5: industrial specialists
    "motionindustries.com", "applied.com", "pumpman.com",
    # Tier 1.5: pump & equipment specialists
    "pumpproducts.com", "pumpcatalog.com",
    # Tier 1.5: broad industrial
    "zoro.com", "globalindustrial.com", "fastenal.com",
]

# Vendors whose prices appear on sites without login walls → "Enterprise"
# Everything else (including Tier 1.5 specialists) → "National Specialist"
_TIER1_VENDORS = {"Grainger", "McMaster-Carr", "MSC Industrial"}


def _vendor_merchant_type(vendor_name: str) -> str:
    return "Enterprise" if vendor_name in _TIER1_VENDORS else "National Specialist"


def _anthropic_complete(system: str, user: str) -> str:
    """Call the Anthropic Messages API directly over HTTP."""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": _EXTRACTION_MODEL,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


# ---------------------------------------------------------------------------
# Tier 1 — Real-time Tavily Search + LLM Parsing
# ---------------------------------------------------------------------------

# URL patterns that indicate a list/category page rather than a direct product page.
# Results from these are flagged as low-reliability (collection_page = True).
_COLLECTION_URL_PATTERNS = ("/collections/", "/search", "/catalog/", "/category/",
                             "/browse/", "?q=", "&q=", "/results")


def _is_collection_url(url: str) -> bool:
    u = url.lower()
    return any(p in u for p in _COLLECTION_URL_PATTERNS)


# ---------------------------------------------------------------------------
# Suitability Scoring
# ---------------------------------------------------------------------------

_VERIFIED_PARTNERS: set[str] = set()   # populated from DB in production; empty = no Gold partners yet


def _compute_suitability_score(specs, snippet: str, url: str,
                                found_pn: Optional[str] = None) -> float:
    """0-100 score: how well this vendor/page matches the sourcing requirement.

    Primary key — PN mention (guardrail):
      If neither the searched PN nor a functional equivalent appears in the snippet,
      the total score is capped at 45 regardless of other signals.

    Components
      PN match        : 0 / 10 / 25 / 40 pts
      Equipment type  : 0-15 pts  (detected_type words in snippet)
      Manufacturer    : 0-10 pts
      Authorized dist : 0-20 pts  (bonus for authorized distributor / service center)
      Direct URL      : 0-10 pts  (product page vs list/search page)
    """
    s       = (snippet or "").lower()
    u_lower = url.lower()

    # ── Guardrail 0: niche mismatch — hard 0.0 when ≥2 wrong-category terms appear ──
    # A single cross-category term can appear in generic distributor snippets; requiring
    # at least 2 distinct hits avoids false positives while still eliminating pure
    # hydraulics/HVAC/plumbing shops returned for motor or pump searches.
    dtype_lower = (getattr(specs, "detected_type", "") or specs.description or "").lower()
    for equip_type, bad_terms in _NICHE_WRONG_TERMS.items():
        if equip_type in dtype_lower:
            hit_count = sum(1 for t in bad_terms if t in s or t in u_lower)
            if hit_count >= 2:
                return 0.0

    # ── Guardrail 0b: motor-without-electric verification ──────────────────────
    # Snippets that mention pump/hydraulic but NEVER mention motor/electric are
    # wrong-niche pages (e.g. Hydradyne returned for an electric motor search).
    if "motor" in dtype_lower:
        has_motor_signal  = "motor" in s or "electric" in s
        has_wrong_signal  = "pump" in s or "hydraulic" in s
        if has_wrong_signal and not has_motor_signal:
            return 0.0

    # ── PN match (primary key) ──────────────────────────────────────────────
    searched_pn = (specs.part_number or "").upper().strip()
    found_upper = (found_pn or "").upper().strip()
    pn_exact   = bool(found_upper and found_upper == searched_pn)
    pn_in_snip = bool(searched_pn and searched_pn.lower() in s)
    pn_alt     = bool(found_pn and not pn_exact)   # different PN found = functional equiv

    if pn_exact:
        pn_pts = 40
    elif pn_in_snip:
        pn_pts = 25
    elif pn_alt:
        pn_pts = 10
    else:
        pn_pts = 0   # guardrail will cap total at 45

    # ── Equipment type match ────────────────────────────────────────────────
    detected = (getattr(specs, "detected_type", "") or "").lower()
    type_pts  = 0
    if detected:
        words = [w for w in detected.split() if len(w) > 3]
        if words:
            matched  = sum(1 for w in words if w in s)
            type_pts = round(15 * matched / len(words))

    # ── Manufacturer match ──────────────────────────────────────────────────
    mfg     = (specs.manufacturer or "").lower()
    mfg_pts = 10 if (mfg and mfg not in ("unknown", "n/a", "null") and mfg in s) else 0

    # ── Authorized distributor / service center bonus ───────────────────────
    auth_phrases = ("authorized distributor", "authorized dealer", "factory authorized",
                    "authorized reseller", "authorized service center")
    svc_phrases  = ("service center", "repair center", "factory service")
    if any(p in s for p in auth_phrases):
        auth_pts = 20
    elif "authorized" in s:
        auth_pts = 8
    elif "distributor" in s or "distributor" in url.lower():
        auth_pts = 5
    else:
        auth_pts = 0
    if any(p in s for p in svc_phrases):
        auth_pts = min(20, auth_pts + 10)

    # ── URL quality ─────────────────────────────────────────────────────────
    is_coll = _is_collection_url(url)
    url_pts = 0 if is_coll else 10

    # ── PN mismatch penalty: a different PN found = functional equiv, not exact ──
    pn_mismatch_penalty = 30 if pn_alt else 0   # pn_alt = found_pn present but ≠ searched_pn

    total = pn_pts + type_pts + mfg_pts + auth_pts + url_pts - pn_mismatch_penalty

    # Guardrail 1: PN not mentioned at all → cap at 45
    if pn_pts == 0:
        total = min(total, 45)

    # Guardrail 2: collection/catalog URL → hard cap at 5 — landing pages without a
    # specific product match cannot confirm price or availability; rank near zero.
    if is_coll:
        total = min(total, 5)

    return min(100.0, max(0.0, round(float(total), 1)))


def _partner_status(vendor_name: str, suitability: float) -> str:
    """Return Arkim network tier: Gold (verified partner), Silver (high-suitability target), or ''."""
    if vendor_name in _VERIFIED_PARTNERS:
        return "Gold"
    if suitability >= 75:
        return "Silver"
    return ""


# Per-vendor UUID4 tokens — stable within a session; production: load/persist from DB.
_VENDOR_TOKENS: dict[str, str] = {}


def _get_vendor_token(vendor_name: str) -> str:
    """Return a stable UUID4 token for this vendor, creating one on first access."""
    key = vendor_name.lower()
    if key not in _VENDOR_TOKENS:
        _VENDOR_TOKENS[key] = str(uuid.uuid4())
    return _VENDOR_TOKENS[key]


def _onboarding_url(vendor_name: str, specs) -> str:
    """Generate a partner onboarding link using a random UUID4 token (not vendor-name-derived)."""
    token = _get_vendor_token(vendor_name)
    slug  = re.sub(r"[^a-z0-9]+", "-", vendor_name.lower()).strip("-")
    rfq   = re.sub(r"[^a-z0-9]+", "-", (specs.part_number or "rfq").lower())
    return f"https://partners.arkim.ai/claim?v={slug}&t={token}&rfq={rfq}"


# Wrong-niche terms per searched equipment type.
# If a vendor's snippet/URL contains these terms when we are searching for a DIFFERENT
# type of asset, the suitability score is penalised by 50 points to prevent irrelevant
# shops (e.g. a hydraulics-only distributor) from surfacing for motor or pump searches.
_NICHE_WRONG_TERMS: dict[str, tuple[str, ...]] = {
    "motor":      ("hydraulic pump", "hydraulic cylinder", "hydraulic distributor",
                   "pneumatic cylinder", "hvac unit", "hvac system", "hvac distributor"),
    "pump":       ("motor rewind", "motor winding", "hydraulic cylinder",
                   "electrical panel", "circuit breaker distributor"),
    "compressor": ("hydraulic press", "pump seal kit", "plumbing fixture",
                   "motor rewind", "hvac coil"),
    "blower":     ("hydraulic cylinder", "pneumatic cylinder", "pump seal kit",
                   "plumbing supply", "motor rewind"),
}

# Known competitor brands per equipment type — injected into equipment queries so Tavily
# surfaces alternatives that actually list "Add to Cart" prices even when the OEM doesn't.
_EQUIP_COMPETITORS = {
    "pump":       "Goulds Lowara Armstrong Xylem ITT",
    "motor":      "Baldor Leeson WEG Marathon Nidec",
    "compressor": "Ingersoll Rand Atlas Copco Gardner Denver",
    "blower":     "Spencer Hoffman Dresser",
}


def _build_search_query(specs: AssetSpecs, search_mode: str = "exact") -> str:
    """Build the Tavily search query.

    search_mode:
      "exact"       — search only for the specific PN/model; no competitor injection.
      "equivalents" — also inject competitor brands for functional-equivalent discovery.
    """
    if specs.category == "Equipment":
        parts: list[str] = []

        desc = (specs.description or "").strip()
        if desc:
            parts.append(desc)
        else:
            model_lower = (specs.model or "").lower()
            for kw, label in [("pump", "pump"), ("motor", "motor"),
                               ("compressor", "compressor"), ("blower", "blower")]:
                if kw in model_lower:
                    parts.append(label)
                    break
            else:
                parts.append("industrial equipment")

        # Compact notation: "5GPM" / "250PSI"
        if specs.gpm:
            parts.append(re.sub(r"\s+", "", specs.gpm).upper())
        if specs.psi:
            parts.append(re.sub(r"\s+", "", specs.psi).upper())
        if specs.hp and specs.hp not in ("N/A", "None", "null"):
            parts.append(specs.hp)
        if specs.frame:
            parts.append(f"frame {specs.frame}")

        known = (specs.manufacturer not in ("Unknown", "N/A", "null") and
                 specs.model        not in ("Unknown", "N/A", "null"))

        if search_mode == "equivalents":
            # Always anchor to the exact PN/model first so the original is found,
            # then inject competitors so functional equivalents also surface.
            pn = specs.part_number
            if pn and pn not in ("N/A", "UNKNOWN-PN", "Unknown"):
                parts.append(f'"{pn}"')
            elif known:
                parts.append(f"{specs.manufacturer} {specs.model}")
            # Boolean OR competitor group — explicit operator for search precision
            desc_lower = (desc or specs.model or "").lower()
            for equip_type, competitors in _EQUIP_COMPETITORS.items():
                if equip_type in desc_lower:
                    brands = competitors.split()
                    parts.append(f"({' OR '.join(brands)})")
                    break
            if known:
                parts.append(f"OR equivalent to {specs.manufacturer} {specs.model}")
            # Cross-reference / interchange terms surface aftermarket parts and OEM alternates
            parts.append('(OR "cross-reference" OR "interchange" OR "drop-in replacement")')
        else:
            # Exact mode: include the specific PN in quotes
            pn = specs.part_number
            if pn and pn not in ("N/A", "UNKNOWN-PN", "Unknown"):
                parts.append(f'"{pn}"')
            elif known:
                parts.append(f"{specs.manufacturer} {specs.model}")

        parts.append("price buy")
        return " ".join(filter(None, parts))
    else:
        # Part: always exact PN search
        pn  = specs.part_number
        mfg = specs.manufacturer if specs.manufacturer not in ("N/A", "Unknown") else ""
        mdl = specs.model        if specs.model        not in ("N/A", "Unknown") else ""
        if pn and pn not in ("N/A", "UNKNOWN-PN", "Unknown"):
            pn_term = f'"{pn}"'
        else:
            pn_term = ""
        base = " ".join(p for p in [mfg, mdl, pn_term] if p)
        return f"{base} distributor price buy"


def _search_vendor_prices(specs: AssetSpecs, search_mode: str = "exact") -> list[dict]:
    """Tavily search for Tier 1 / 1.5 pricing.

    Parts         → domain-restricted to known distributor sites (precision).
    Equipment/exact → domain-restricted to find the specific PN on known sites.
    Equipment/equivalents → open web so competitor brands surface.
    """
    query = _build_search_query(specs, search_mode=search_mode)
    print(f"[Sourcing] Tavily query ({search_mode}): {query!r}")

    if not _tavily:
        print("[Sourcing] Tavily client not initialised — TAVILY_API_KEY missing.")
        return []
    try:
        kwargs: dict = dict(search_depth="advanced", max_results=15)
        # Domain-restrict for exact searches and all parts; open web for equivalents
        if specs.category != "Equipment" or search_mode == "exact":
            kwargs["include_domains"] = _VENDOR_DOMAINS
        response = _tavily.search(query=query, **kwargs)
        return response.get("results", [])
    except Exception as exc:
        print(f"[Sourcing] Tavily error: {exc}")
        return []


_PARSE_SYSTEM = """You are a procurement data extractor for industrial parts and equipment.
Given web search result snippets, extract pricing and shipping information.

Input snippets are wrapped in <snippet id="N"> tags.
Return ONLY valid JSON — a list of objects with these exact keys:
  original_snippet_id : integer — the id attribute of the <snippet> tag this item was extracted from
  vendor            : string — normalize to one of: "Grainger", "McMaster-Carr", "MSC Industrial",
                      "Motion Industries", "Applied Industrial", "Pumpman", "Pump Products",
                      "Pump Catalog", "Zoro", "Global Industrial", "Fastenal",
                      or the actual site/brand name if not in this list
  price             : number (USD, unit price) OR null if hidden / "Call for Quote"
  shipping_fee      : number (USD) — 0 if "Free Shipping", null if not shown or requires freight quote
  shipping_terms    : string — one of: "Free Shipping", "Flat Rate", "LTL Freight Required",
                      "S.F.Q." (Subject to Freight Quote), "TBA - Freight", or null if unknown
  is_freight        : boolean — true if page mentions "Freight", "LTL", "Truck Only", "Heavy Item",
                      or the item is clearly too large/heavy for standard parcel shipping
  found_part_number : string or null — the EXACT part number, model number, or stock ID shown on
                      the vendor's page for this item. If the page shows a list, include the specific
                      line item number or stock ID (e.g. "W2333581", "Item #: 123456"). null if not visible.
  exact_match       : boolean — true ONLY if found_part_number matches the part number being searched.
                      false if found_part_number is a different model/PN (functional equivalent).
  lead_days         : integer (business days to ship; "In Stock"→2, unknown→5)
  url               : string (exact source URL — REQUIRED)
  warranty_terms    : string or null — warranty period shown on the page (e.g. "12-month standard",
                      "5-year limited", "90-day parts only"). null if not mentioned.
  weight_lbs        : number or null — product weight in pounds if shown (used for freight guard). null if not visible.

Rules:
- Include entries where a real source URL is present EVEN IF no price is listed.
  Set price to null when hidden, "Call for Quote", or "Login to see price".
- Never invent or estimate prices — null only.
- If "Free Shipping" appears anywhere on the page: shipping_fee = 0, shipping_terms = "Free Shipping".
- If "Freight" / "LTL" appears: is_freight = true, shipping_fee = null, shipping_terms = "LTL Freight Required".
- If shipping cost is unclear / "subject to quote": shipping_terms = "S.F.Q.".
- found_part_number: extract even if it differs from the searched PN — this is the cross-reference key.
- exact_match: compare found_part_number to the searched PN; false if they differ.
- warranty_terms: extract verbatim warranty language; standardize to "N-month standard", "N-year limited", etc.
- weight_lbs: if weight is shown in kg, convert to lbs (×2.205). Any item >100 lbs likely needs freight.
- One entry per vendor — prefer entries that have a price over those that don't.
- Only SKIP entries with no URL at all.
"""


def _llm_parse_results(specs: AssetSpecs, results: list[dict]) -> list[dict]:
    """Use Claude to extract price/lead_time from Tavily snippets."""
    if not results:
        return []

    if not ANTHROPIC_API_KEY:
        print("[Sourcing] No Anthropic API key — cannot parse results.")
        return []

    _BATCH_SIZE = 5
    all_items: list[dict] = []

    for batch_num, batch_start in enumerate(range(0, len(results), _BATCH_SIZE)):
        batch = results[batch_start:batch_start + _BATCH_SIZE]
        # Wrap each snippet in XML tags for source-traceability
        snippets_xml = ""
        for local_i, r in enumerate(batch):
            gid = batch_start + local_i
            snippets_xml += (
                f'<snippet id="{gid}">\n'
                f'URL: {r.get("url", "")}\n'
                f'Title: {r.get("title", "")}\n'
                f'Content: {r.get("content", "")}\n'
                f'</snippet>\n\n'
            )
        user_msg = (
            f"Part being searched: {specs.manufacturer} {specs.model} — {specs.part_number}\n\n"
            + snippets_xml.strip()
        )
        print(f"[Sourcing] Parsing batch {batch_num + 1} ({len(batch)} snippets)…")
        try:
            raw = _anthropic_complete(_PARSE_SYSTEM, user_msg)
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                print(f"[Sourcing] Batch {batch_num + 1}: no JSON array in response")
                continue
            items = json.loads(m.group(0))
            if isinstance(items, list):
                all_items.extend(items)
        except Exception as exc:
            print(f"[Sourcing] Batch {batch_num + 1} parse error: {exc}")

    print(f"[Sourcing] LLM extracted {len(all_items)} entries from {len(results)} snippets")
    return all_items


def _apply_price_sanity(items: list[dict],
                        specs: "AssetSpecs" = None) -> list[dict]:
    """Strip prices that are >70% below the peer average — likely hallucinations.

    Single-price case: fetch a Tavily market-reference query first; only accept
    the price if it is within 70% of the reference average.
    Multi-price case: compare within the peer set (existing logic).
    Flagged items → price stripped, price_sanity_flagged=True, suitability zeroed later.
    """
    prices = [float(it["price"]) for it in items
              if it.get("price") is not None and float(it.get("price", 0)) > 0]

    if len(prices) == 0:
        return items

    if len(prices) == 1 and specs is not None and _tavily:
        # Single price — validate against a quick market reference search
        ref_q = (f"{specs.manufacturer} {specs.model} {specs.part_number} "
                 f"price market distributor").strip()
        print(f"[Sourcing] Single-price market reference lookup: {ref_q!r}")
        try:
            resp = _tavily.search(query=ref_q, search_depth="basic", max_results=3)
            ref_prices: list[float] = []
            for r in resp.get("results", []):
                text = r.get("title", "") + " " + r.get("content", "")
                for m in re.finditer(r"\$\s*([\d,]+(?:\.\d{2})?)", text):
                    p = float(m.group(1).replace(",", ""))
                    if p > 10:
                        ref_prices.append(p)
            if ref_prices:
                ref_avg   = sum(ref_prices) / len(ref_prices)
                threshold = ref_avg * 0.30
                if prices[0] < threshold:
                    for item in items:
                        p = item.get("price")
                        if p is not None and float(p) > 0:
                            print(f"[Sourcing] Single-price sanity FAIL: "
                                  f"{item.get('vendor','?')} @ ${float(p):.2f} "
                                  f"vs market ref ${ref_avg:.2f} — stripping price")
                            item["price"]                = None
                            item["price_sanity_flagged"] = True
        except Exception as exc:
            print(f"[Sourcing] Market reference lookup error: {exc}")
        return items

    # Multi-price: peer comparison
    avg       = sum(prices) / len(prices)
    threshold = avg * 0.30

    for item in items:
        p = item.get("price")
        if p is not None and float(p) > 0 and float(p) < threshold:
            print(f"[Sourcing] Price sanity FAIL: {item.get('vendor','?')} "
                  f"@ ${float(p):.2f} vs peer avg ${avg:.2f} — stripping price")
            item["price"]                = None
            item["price_sanity_flagged"] = True
    return items



# Reliability by merchant tier — no per-vendor hardcoding; MCS refines at quote time.
_MERCHANT_RELIABILITY: dict[str, float] = {
    "Enterprise":           90.0,
    "National Specialist":  82.0,
    "Direct Buy via Arkim": 78.0,
}


def _base_reliability(merchant_type: str) -> float:
    return _MERCHANT_RELIABILITY.get(merchant_type, 78.0)


# ---------------------------------------------------------------------------
# Market Confidence Score — interim reliability metric via web search
# ---------------------------------------------------------------------------

_RELIABILITY_SYSTEM = """You are an industrial reliability analyst.
Given web search results about a specific component, assess its market reliability reputation.

Return ONLY valid JSON (no prose):
{
  "market_confidence_score": <integer 1-10>,
  "summary": "<one sentence>",
  "common_failures": "<brief comma-separated list or null>"
}

Scoring guide:
  10   : Outstanding MTBF, no documented failure patterns
  8-9  : Very reliable, minor isolated issues
  6-7  : Acceptable, some known failure patterns in the field
  4-5  : Below average reliability, notable concerns
  1-3  : Poor, widespread failures or product discontinuation

If no reliability information is found, return:
{"market_confidence_score": 6, "summary": "No reliability data found in web sources.", "common_failures": null}
"""


def _fetch_market_confidence(specs: AssetSpecs) -> Optional[float]:
    """Search the web for reliability/MTBF data on this specific brand+model.
    Returns a 1-10 Market Confidence Score, or None if unavailable.
    """
    if not _tavily or not ANTHROPIC_API_KEY:
        return None
    brand = specs.manufacturer if specs.manufacturer not in ("Unknown", "N/A", "null") else ""
    model = specs.model        if specs.model        not in ("Unknown", "N/A", "null") else ""
    if not (brand or model):
        return None

    query = f"{brand} {model} reliability MTBF common failures field reports".strip()
    print(f"[Sourcing] Market confidence query: {query!r}")
    try:
        resp    = _tavily.search(query=query, search_depth="basic", max_results=5)
        results = resp.get("results", [])
        if not results:
            return None
        snippets = "\n---\n".join(
            f"Title: {r.get('title','')}\nContent: {r.get('content','')}"
            for r in results
        )
        raw = _anthropic_complete(
            _RELIABILITY_SYSTEM,
            f"Component: {brand} {model}\n\n{snippets}"
        )
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            d = json.loads(m.group(0))
            score = d.get("market_confidence_score")
            if score is not None:
                s = float(score)
                print(f"[Sourcing] Market confidence {brand} {model}: {s:.1f}/10 — {d.get('summary','')}")
                return s
    except Exception as exc:
        print(f"[Sourcing] Market confidence error: {exc}")
    return None


def _is_heavy_item(specs: AssetSpecs, weight_lbs: Optional[float] = None) -> bool:
    """True if the item requires LTL freight: LTL_freight magnitude, >100 lbs, >10 HP, or large Equipment."""
    if getattr(specs, "physical_magnitude", None) == "LTL_freight":
        return True
    if weight_lbs is not None and weight_lbs > 100:
        return True
    if specs.category == "Equipment" and any(
        kw in (specs.description or "").lower()
        for kw in ("pump", "motor", "compressor", "blower")
    ):
        return True
    hp_str = specs.hp or ""
    m = re.search(r"(\d+(?:\.\d+)?)", hp_str)
    if m and float(m.group(1)) > 10:
        return True
    return False


def _call_enterprise_api(specs: AssetSpecs,
                          force_refresh: bool = False,
                          search_mode: str = "exact") -> list[SourcingOption]:
    """Tier 1 / 1.5: check JSON price DB first, then real-time Tavily search.

    Produces two kinds of SourcingOptions:
      - price_tbd=False : real price found — goes into TCA comparison table
      - price_tbd=True  : URL found but price is hidden / CfQ — shown as Inquiry Required card
    No synthetic prices, no fallbacks.
    """
    from utils.price_db import get_cached_prices, save_price

    options: list[SourcingOption] = []
    cached_vendors: set[str] = set()

    if not force_refresh:
        cached = get_cached_prices(specs.part_number)
        for vendor_name, data in cached.items():
            fetched    = data["date_fetched"][:10]
            source     = data.get("source", "live")
            label      = "Pre-Negotiated" if source == "rfq" else "Cached"
            cached_url = data.get("url")
            print(f"[Sourcing] Price DB HIT ({label}): {vendor_name} @ ${data['price']:.2f} (fetched {fetched})")
            options.append(SourcingOption(
                vendor_name=vendor_name,
                base_price=float(data["price"]),
                lead_time_days=int(data.get("lead_days") or 5),
                reliability_score=_base_reliability(_vendor_merchant_type(vendor_name)),
                merchant_type=_vendor_merchant_type(vendor_name),
                requires_rfq=False,
                notes=f"{label} Price — fetched {fetched}",
                source_url=cached_url,
                price_tbd=False,
            ))
            cached_vendors.add(vendor_name)
    else:
        print("[Sourcing] Force refresh — bypassing price DB.")

    missing = [v for v in TARGET_VENDORS if v not in cached_vendors]
    if missing:
        print(f"[Sourcing] Tavily search for: {missing}")
        results = _search_vendor_prices(specs, search_mode=search_mode)
        parsed  = _apply_price_sanity(_llm_parse_results(specs, results), specs)
        # URL → raw snippet map for suitability scoring
        snippet_map = {
            r.get("url", ""): (r.get("title", "") + " " + r.get("content", "")).strip()
            for r in results if r.get("url")
        }
        # Market confidence — one search per pipeline run, applied to all options
        mkt_conf = _fetch_market_confidence(specs)

        seen: set[str] = set()
        for item in parsed:
            vendor          = item.get("vendor", "Unknown")
            price           = item.get("price")
            url             = item.get("url", "").strip()
            lead            = item.get("lead_days", 5)
            ship_fee        = item.get("shipping_fee")
            ship_terms      = item.get("shipping_terms")
            warranty        = item.get("warranty_terms") or None
            weight_lbs      = item.get("weight_lbs")
            weight_lbs      = float(weight_lbs) if weight_lbs is not None else None
            found_pn        = (item.get("found_part_number") or "").strip() or None
            sanity_flagged  = bool(item.get("price_sanity_flagged", False))
            exact_match     = bool(item.get("exact_match", True))
            # Freight guard: >100 lbs OR >10 HP OR category heavy → S.F.Q.
            heavy         = _is_heavy_item(specs, weight_lbs)
            is_freight    = bool(item.get("is_freight", False)) or (heavy and ship_fee is None and price is not None)

            # Assign granular match tier:
            #   Exact OEM           — found PN matches searched PN exactly
            #   Aftermarket Compatible — different PN found (cross-reference / alternate brand)
            #   Functional Alternative — no PN confirmed (spec-based match only)
            searched_pn = (specs.part_number or "").upper().strip()
            if found_pn and searched_pn and found_pn.upper().strip() != searched_pn:
                exact_match = False
            if exact_match:
                match_type = "Exact OEM"
            elif found_pn:
                match_type = "Aftermarket Compatible"   # different PN confirmed in snippet
            else:
                match_type = "Functional Alternative"   # no PN to compare

            # Freight label: use extracted shipping_terms if present, else infer from flags
            if ship_fee is not None and ship_fee == 0:
                resolved_terms = "Free Shipping"
            elif is_freight:
                resolved_terms = ship_terms or "LTL Freight Required"
            elif ship_terms:
                resolved_terms = ship_terms
            else:
                resolved_terms = None   # will be filled in by quoting engine

            if not url or vendor in seen:
                if not url:
                    print(f"[Sourcing] Skipping {vendor} — no URL")
                continue
            seen.add(vendor)

            # Collection/search page — cannot confirm product; demote to Functional Alternative
            is_coll = _is_collection_url(url)
            if is_coll:
                exact_match = False
                match_type  = "Functional Alternative"
                print(f"[Sourcing] Collection page detected — flagging as Functional Alternative: {url}")

            # Heavy items with no explicit shipping → always S.F.Q., never flat-rate
            if heavy and resolved_terms is None and ship_fee is None:
                resolved_terms = "LTL Freight Required"
                is_freight     = True

            merchant_type = _vendor_merchant_type(vendor)
            fn_tag   = f" [Found: {found_pn}]" if found_pn else ""
            coll_tag = " [LIST PAGE]" if is_coll else ""
            tag      = ("" if match_type == "Exact OEM" else f" [{match_type}]") + (" [LTL]" if is_freight else "") + fn_tag + coll_tag

            # PN Enforcement: no verified part number → cannot confirm product identity
            # → demote to Inquiry Required regardless of stated price.
            if found_pn is None and price is not None:
                print(f"[Sourcing] PN Enforcement: {vendor} has no found_part_number "
                      f"— stripping price, demoting to Inquiry Required")
                price = None

            # Suitability scoring using raw Tavily snippet
            snippet = snippet_map.get(url, "")
            suit    = _compute_suitability_score(specs, snippet, url, found_pn)

            # Price sanity flag → zero suitability (unreliable result, keep as Inquiry Required)
            if sanity_flagged:
                suit = 0.0

            pstat   = _partner_status(vendor, suit)

            _common = dict(
                lead_time_days=int(lead),
                reliability_score=_base_reliability(merchant_type),
                merchant_type=merchant_type,
                source_url=url,
                extracted_shipping_fee=float(ship_fee) if ship_fee is not None else None,
                is_freight=is_freight,
                match_type=match_type,
                found_part_number=found_pn,
                shipping_terms=resolved_terms,
                is_collection_page=is_coll,
                suitability_score=suit,
                partner_status=pstat,
                warranty_terms=warranty,
                weight_lbs=weight_lbs,
                market_confidence_score=mkt_conf,
            )
            if price is not None:
                save_price(specs.part_number, vendor, float(price), int(lead), source="live", url=url)
                print(f"[Sourcing] Priced{tag} suit={suit:.0f}%: {vendor} @ ${price:.2f} | {url}")
                options.append(SourcingOption(
                    vendor_name=vendor,
                    base_price=float(price),
                    requires_rfq=False,
                    notes=f"Live{tag}",
                    price_tbd=False,
                    **_common,
                ))
            else:
                print(f"[Sourcing] Inquiry Required{tag} suit={suit:.0f}%: {vendor} | {url}")
                options.append(SourcingOption(
                    vendor_name=vendor,
                    base_price=0.0,
                    requires_rfq=False,
                    notes=f"Price inquiry required{tag}",
                    price_tbd=True,
                    **_common,
                ))

    priced = sum(1 for o in options if not o.price_tbd)
    tbd    = sum(1 for o in options if o.price_tbd)
    print(f"[Sourcing] Enterprise/Specialist: {priced} priced, {tbd} inquiry-required")
    return options


# ---------------------------------------------------------------------------
# Tier 2 — National Specialist Discovery via open-web Tavily + LLM extraction
# ---------------------------------------------------------------------------

_NATIONAL_SPECIALIST_SYSTEM = """You are a procurement data extractor for industrial equipment.
Given web search results, identify national specialist vendors that sell or distribute this equipment type.

Return ONLY valid JSON — a list of up to 5 objects:
[
  {
    "name":      "Vendor Business Name",
    "website":   "https://...",
    "email":     null,
    "phone":     null,
    "lead_days": 7,
    "price":     null
  }
]

Rules:
- Include US-based national distributors, online retailers, and specialist suppliers.
- EXCLUDE: Grainger, McMaster-Carr, MSC Industrial, Amazon, eBay, Home Depot, Alibaba, Fastenal.
- price: a number if a specific price is explicitly visible in the snippet; null if hidden or missing.
- lead_days: use stated shipping time, otherwise default 7.
- Set email/phone to null if not in the snippets — do not invent contact details.
- Return [] if no qualifying national specialist vendors appear.
"""


# Asset-type niche terms for Tier 2 discovery.
# Using specific industry vocabulary surfaces specialist shops instead of generalists.
_TIER2_NICHE_TERMS: dict[str, str] = {
    "motor":      "electric motor distributor service center repair authorized",
    "pump":       "industrial pump distributor authorized service center",
    "compressor": "air compressor distributor service repair authorized",
    "blower":     "industrial blower fan distributor service",
    "vfd":        "variable frequency drive VFD distributor industrial",
    "starter":    "motor starter contactor distributor industrial",
    "bearing":    "industrial bearing distributor authorized",
    "seal":       "mechanical seal industrial distributor",
    "conveyor":   "conveyor equipment distributor industrial",
}


def _build_tier2_query(specs: AssetSpecs) -> str:
    """Build an asset-specific national specialist discovery query.

    Uses explicit boolean AND/OR operators so Tavily surfaces authorized
    distributors and service centers rather than generic industrial shops.
    Format: (authorized OR distributor OR "service center") AND "<type>" AND "<mfg>" AND "<spec>"
    """
    detected = (getattr(specs, "detected_type", None) or "").lower()
    desc     = (specs.description or "").lower()
    ctx      = detected or desc or ""

    # Determine the primary niche term (quoted for precision)
    niche_term = None
    for equip_type in _TIER2_NICHE_TERMS:
        if equip_type in ctx:
            niche_term = equip_type
            break
    if not niche_term:
        niche_term = (getattr(specs, "detected_type", None) or specs.description or "industrial equipment")

    # Build boolean AND chain
    known_mfg = specs.manufacturer not in ("Unknown", "N/A", "null", None)
    parts = [
        '(authorized OR distributor OR "service center")',
        f'"{niche_term}"',
    ]
    if known_mfg:
        parts.append(f'"{specs.manufacturer}"')

    # Inject primary performance spec (HP for motors, GPM for pumps, PSI for compressors)
    if specs.hp and specs.hp not in ("N/A", "None", "null"):
        hp_val = re.sub(r"\s+", "", specs.hp).upper()
        parts.append(f'"{hp_val}"')
    elif getattr(specs, "gpm", None):
        gpm_val = re.sub(r"\s+", "", specs.gpm).upper()
        parts.append(f'"{gpm_val}"')

    return " AND ".join(parts)


def _discover_national_specialists(specs: AssetSpecs,
                                    enterprise_options: list[SourcingOption]) -> list[SourcingOption]:
    """Tier 2: open-web national specialist discovery.

    Searches the full US internet using detected_type so brand-agnostic specialists
    (e.g. pump distributors, conveyor suppliers) that list Add-to-Cart pricing appear.
    No price estimation — if price not found in snippet, the option is price_tbd=True (→ Tier 3).
    """
    query = _build_tier2_query(specs)
    print(f"[Sourcing] Tier 2 national query: {query!r}")

    if not _tavily:
        print("[Sourcing] Tier 2 skipped — Tavily not initialised.")
        return []

    try:
        response = _tavily.search(query=query, search_depth="advanced", max_results=10)
        results  = response.get("results", [])
    except Exception as exc:
        print(f"[Sourcing] Tier 2 Tavily error: {exc}")
        return []

    if not results or not ANTHROPIC_API_KEY:
        return []

    # URL → raw snippet for suitability scoring
    snippet_map = {
        r.get("url", ""): (r.get("title", "") + " " + r.get("content", "")).strip()
        for r in results if r.get("url")
    }

    snippets = [
        f"URL: {r.get('url', '')}\nTitle: {r.get('title', '')}\nSnippet: {r.get('content', '')}\n"
        for r in results
    ]
    equip_term = getattr(specs, "detected_type", None) or specs.description or "industrial equipment"
    user_msg   = f"Finding national specialists for: {equip_term}\n\n" + "\n---\n".join(snippets)

    try:
        raw   = _anthropic_complete(_NATIONAL_SPECIALIST_SYSTEM, user_msg)
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            return []
        vendors = [v for v in json.loads(match.group(0))
                   if isinstance(v, dict) and v.get("name")]
        print(f"[Sourcing] Tier 2 found {len(vendors)} national specialist(s)")
    except Exception as exc:
        print(f"[Sourcing] Tier 2 LLM error: {exc}")
        return []

    # Exclude anything already in Tier 1 by name
    tier1_lower = {"grainger", "mcmaster", "mcmaster-carr", "msc industrial",
                   "motion industries", "applied industrial"}
    options: list[SourcingOption] = []

    for v in vendors[:5]:
        name = (v.get("name") or "").strip()
        if any(t in name.lower() for t in tier1_lower):
            continue
        url   = (v.get("website") or "").strip() or None
        price = v.get("price")
        lead  = max(1, int(v.get("lead_days") or 7))
        email = v.get("email") or None

        tbd        = price is None
        base_price = float(price) if price is not None else 0.0

        # Suitability: match url to best snippet (vendor may not match exactly, use first found)
        snippet = snippet_map.get(url or "", "")
        if not snippet:
            # fallback: search snippet_map for any URL containing the vendor name fragment
            slug = re.sub(r"[^a-z0-9]", "", name.lower())
            for surl, stext in snippet_map.items():
                if slug[:6] in surl.lower():
                    snippet = stext
                    break
        suit  = _compute_suitability_score(specs, snippet, url or "", found_pn=None)
        pstat = _partner_status(name, suit)

        options.append(SourcingOption(
            vendor_name=name,
            base_price=base_price,
            lead_time_days=lead,
            reliability_score=78.0,
            merchant_type="Direct Buy via Arkim",
            requires_rfq=tbd,
            contact_email=email,
            admin_fee=50.0 if tbd else 0.0,
            notes=f"National specialist — {url}" if url else "National specialist",
            source_url=url,
            price_tbd=tbd,
            suitability_score=suit,
            partner_status=pstat,
        ))
        tag = "TBD" if tbd else f"${base_price:.2f}"
        print(f"  Tier 2: {name} — {tag} | {lead}d | suit={suit:.0f}% | {pstat or 'no partner'}")

    if not options:
        print("[Sourcing] Tier 2: no qualifying national specialists found")
    return options


# ---------------------------------------------------------------------------
# Tier 3 — RFQ Email Draft Generator
# ---------------------------------------------------------------------------

def draft_rfq_email(specs: AssetSpecs, vendor: SourcingOption) -> str:
    """Generate a Partner Invitation email for Tier 3 / TBD vendors.

    Combines an immediate RFQ (specific part, 48-hour intent) with an Arkim
    Partner Network onboarding offer (Merchant of Record value prop + unique claim link).
    """
    to_address   = vendor.contact_email or "[ vendor email — see contact details below ]"
    onboard_url  = _onboarding_url(vendor.vendor_name, specs)
    suit_pct     = f"{vendor.suitability_score:.0f}%" if vendor.suitability_score else "—"

    # Asset-type label for the "why selected" paragraph
    asset_type = (getattr(specs, "detected_type", None) or specs.description
                  or f"{specs.manufacturer} {specs.model}")

    part_line = f"{specs.manufacturer} {specs.model}"
    if specs.part_number and specs.part_number not in ("N/A", "UNKNOWN-PN", "Unknown"):
        part_line += f" (PN: {specs.part_number})"
    if specs.hp and specs.hp not in ("N/A", "None", "null"):
        part_line += f" — {specs.hp} HP"
    if specs.description:
        part_line += f" — {specs.description}"

    # CapEx extras
    use_case   = getattr(specs, "use_case",   None)
    duty_cycle = getattr(specs, "duty_cycle",  None)
    budget_max = getattr(specs, "budget_max",  None)
    capex_block = ""
    if use_case or duty_cycle or budget_max:
        capex_block = (
            "\n──────────────────────────────────────────\n"
            "APPLICATION CONTEXT\n"
            "──────────────────────────────────────────\n"
            + (f"  Use Case     : {use_case}\n"   if use_case   else "")
            + (f"  Duty Cycle   : {duty_cycle}\n"  if duty_cycle else "")
            + (f"  Max Budget   : {budget_max}\n"  if budget_max else "")
        )

    return f"""To: {to_address}
Subject: Sourcing Inquiry + Arkim Partner Invitation — {specs.manufacturer} {specs.model}

Dear {vendor.vendor_name} Team,

I represent Arkim Industrial Procurement Services. Our sourcing system identified
{vendor.vendor_name} as a specialist in {asset_type} equipment (match score: {suit_pct}).
We have an active, time-sensitive requirement for the item below and believe your
expertise makes you the right supplier:

  {part_line}
{capex_block}
──────────────────────────────────────────
IMMEDIATE SOURCING REQUEST
──────────────────────────────────────────
  Manufacturer  : {specs.manufacturer}
  Model         : {specs.model}
  Part Number   : {specs.part_number}
  Voltage       : {specs.voltage}
  Horsepower    : {specs.hp or "—"}
  Description   : {specs.description or "—"}
  Quantity      : 1 unit (first order)

Please reply with:
  • Unit price and lead time
  • Shipping terms (FOB origin or destination)
  • Availability / stock status

We aim to place an order within 48 hours of a complete quote.

──────────────────────────────────────────
ARKIM MERCHANT OF RECORD — HOW IT WORKS
──────────────────────────────────────────
Arkim acts as the Merchant of Record on every transaction:

  ✓ Net-0 instant payment — ACH wired upon order confirmation, zero
    collections risk, no invoice chasing
  ✓ No corporate onboarding — bypass the 4-6 week AP/vendor setup
    that blocks most industrial buyers; sell immediately
  ✓ Guaranteed volume — Arkim aggregates demand across multiple facilities;
    partners in our network see 3-8× more repeat orders per year
  ✓ Regional RFQ priority — once verified, you appear first in our
    sourcing queue for {specs.detected_type or specs.description or "this equipment category"}

Our matching algorithm rated {vendor.vendor_name} at {suit_pct} compatibility
for this category. Claim your Partner profile (under 5 minutes):

  → {onboard_url}

──────────────────────────────────────────

Thank you — we look forward to working together.

Arkim Procurement Team
procurement@arkim.ai  |  partners.arkim.ai
──────────────────────────────────────────""".strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_vendors(specs: AssetSpecs,
                 site: str = "La Mirada",
                 force_refresh: bool = False,
                 search_mode: str = "exact",
                 workflow: str = "spare_parts") -> tuple[list[SourcingOption], Optional[str]]:
    """
    Run multi-tier sourcing.

    workflow:     "spare_parts" | "replacement" | "capex"
                  CapEx skips Tier 1/2 price search and goes straight to specialist discovery.
    search_mode:  "exact" | "equivalents" — controls competitor injection in Equipment queries.
    Returns: (all_options, None)
    """
    enterprise: list[SourcingOption] = []

    if workflow != "capex":
        print(f"\n[Sourcing] Tier 1/1.5 — Querying national vendors (mode: {search_mode})...")
        enterprise = _call_enterprise_api(specs, force_refresh=force_refresh, search_mode=search_mode)
        for o in enterprise:
            tag = " [INQUIRY REQUIRED]" if o.price_tbd else f" @ ${o.base_price:.2f}"
            print(f"  {o.vendor_name} ({o.merchant_type}){tag} | {o.lead_time_days}d")
    else:
        print(f"\n[Sourcing] CapEx workflow — skipping Tier 1/2, going direct to specialist outreach...")

    print(f"\n[Sourcing] Tier 2 — National specialist discovery...")
    tier2 = _discover_national_specialists(specs, enterprise)

    return enterprise + tier2, None
