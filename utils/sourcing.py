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

TAVILY_API_KEY    = os.environ.get("TAVILY_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

_tavily = TavilyClient(api_key=TAVILY_API_KEY)

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
            "model": "claude-haiku-4-5-20251001",
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

# Known competitor brands per equipment type — injected into equipment queries so Tavily
# surfaces alternatives that actually list "Add to Cart" prices even when the OEM doesn't.
_EQUIP_COMPETITORS = {
    "pump":       "Goulds Lowara Armstrong Xylem ITT",
    "motor":      "Baldor Leeson WEG Marathon Nidec",
    "compressor": "Ingersoll Rand Atlas Copco Gardner Denver",
    "blower":     "Spencer Hoffman Dresser",
}


def _build_search_query(specs: AssetSpecs) -> str:
    if specs.category == "Equipment":
        # Equipment: description + compact tech specs + competitor brands.
        # Part number intentionally excluded — OEM PN never finds competitor alternatives.
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

        # Compact notation: "5GPM" / "250PSI" — no internal space for better search matching
        if specs.gpm:
            parts.append(re.sub(r"\s+", "", specs.gpm).upper())
        if specs.psi:
            parts.append(re.sub(r"\s+", "", specs.psi).upper())
        if specs.hp and specs.hp not in ("N/A", "None", "null"):
            parts.append(specs.hp)
        if specs.frame:
            parts.append(f"frame {specs.frame}")

        # Competitor brands — forces results from vendors that DO list prices
        desc_lower = (desc or specs.model or "").lower()
        for equip_type, competitors in _EQUIP_COMPETITORS.items():
            if equip_type in desc_lower:
                parts.append(competitors)
                break

        known = (specs.manufacturer not in ("Unknown", "N/A", "null") and
                 specs.model        not in ("Unknown", "N/A", "null"))
        if known:
            parts.append(f"equivalent to {specs.manufacturer} {specs.model}")

        parts.append("price")
        return " ".join(filter(None, parts))
    else:
        # Part: prioritise exact PN match — wrap PN in quotes for search-engine precision
        pn  = specs.part_number
        mfg = specs.manufacturer if specs.manufacturer not in ("N/A", "Unknown") else ""
        mdl = specs.model        if specs.model        not in ("N/A", "Unknown") else ""
        if pn and pn not in ("N/A", "UNKNOWN-PN", "Unknown"):
            pn_term = f'"{pn}"'
        else:
            pn_term = ""
        base = " ".join(p for p in [mfg, mdl, pn_term] if p)
        return f"{base} distributor price buy"


def _search_vendor_prices(specs: AssetSpecs) -> list[dict]:
    """Tavily search for Tier 1 / 1.5 pricing.

    Parts   → domain-restricted to known distributor sites (precision).
    Equipment → open web search so competitor brands (Goulds, Lowara, …) appear;
                those brands list 'Add to Cart' prices even when the OEM hides them.
    """
    query = _build_search_query(specs)
    print(f"[Sourcing] Tavily query: {query!r}")

    try:
        kwargs: dict = dict(search_depth="advanced", max_results=15)
        if specs.category != "Equipment":
            kwargs["include_domains"] = _VENDOR_DOMAINS
        response = _tavily.search(query=query, **kwargs)
        return response.get("results", [])
    except Exception as exc:
        print(f"[Sourcing] Tavily error: {exc}")
        return []


_PARSE_SYSTEM = """You are a procurement data extractor for industrial parts and equipment.
Given web search result snippets, extract pricing and shipping information.

Return ONLY valid JSON — a list of objects with these exact keys:
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

Rules:
- Include entries where a real source URL is present EVEN IF no price is listed.
  Set price to null when hidden, "Call for Quote", or "Login to see price".
- Never invent or estimate prices — null only.
- If "Free Shipping" appears anywhere on the page: shipping_fee = 0, shipping_terms = "Free Shipping".
- If "Freight" / "LTL" appears: is_freight = true, shipping_fee = null, shipping_terms = "LTL Freight Required".
- If shipping cost is unclear / "subject to quote": shipping_terms = "S.F.Q.".
- found_part_number: extract even if it differs from the searched PN — this is the cross-reference key.
- exact_match: compare found_part_number to the searched PN; false if they differ.
- One entry per vendor — prefer entries that have a price over those that don't.
- Only SKIP entries with no URL at all.
"""


def _llm_parse_results(specs: AssetSpecs, results: list[dict]) -> list[dict]:
    """Use Claude to extract price/lead_time from Tavily snippets."""
    if not results:
        return []

    if not ANTHROPIC_API_KEY:
        print("[Sourcing] No Anthropic API key — using regex fallback.")
        return _regex_fallback_parse(results)

    snippets = []
    for r in results:
        snippets.append(
            f"URL: {r.get('url', '')}\n"
            f"Title: {r.get('title', '')}\n"
            f"Snippet: {r.get('content', '')}\n"
        )

    user_msg = (
        f"Part being searched: {specs.manufacturer} {specs.model} — {specs.part_number}\n\n"
        + "\n---\n".join(snippets)
    )

    try:
        raw = _anthropic_complete(_PARSE_SYSTEM, user_msg)
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            raise ValueError("No JSON array in LLM response")
        parsed = json.loads(match.group(0))
        items = parsed if isinstance(parsed, list) else []
        print(f"[Sourcing] LLM extracted {len(items)} price(s) from {len(results)} snippets")
        return items
    except Exception as exc:
        print(f"[Sourcing] LLM parse error: {exc} — falling back to regex")
        return _regex_fallback_parse(results)


def _regex_fallback_parse(results: list[dict]) -> list[dict]:
    """Simple regex price extraction when LLM is unavailable."""
    extracted = []
    vendor_map = {
        "grainger.com":          "Grainger",
        "mcmaster.com":          "McMaster-Carr",
        "mscdirect.com":         "MSC Industrial",
        "motionindustries.com":  "Motion Industries",
        "applied.com":           "Applied Industrial",
        "pumpman.com":           "Pumpman",
        "pumpproducts.com":      "Pump Products",
        "pumpcatalog.com":       "Pump Catalog",
        "zoro.com":              "Zoro",
        "globalindustrial.com":  "Global Industrial",
        "fastenal.com":          "Fastenal",
    }
    for r in results:
        url    = r.get("url", "")
        vendor = next((v for k, v in vendor_map.items() if k in url), None)
        if not vendor or not url:
            continue
        text = r.get("title", "") + " " + r.get("content", "")
        m = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", text)
        price = float(m.group(1).replace(",", "")) if m else None
        extracted.append({"vendor": vendor, "price": price, "lead_days": 5, "url": url})
    return extracted


def _vendor_reliability(vendor_name: str) -> float:
    return {
        "Grainger":            95.0,
        "McMaster-Carr":       92.0,
        "MSC Industrial":      88.0,
        "Motion Industries":   87.0,
        "Applied Industrial":  86.0,
        "Pumpman":             83.0,
        "Pump Products":       82.0,
        "Pump Catalog":        80.0,
        "Zoro":                80.0,
        "Global Industrial":   79.0,
        "Fastenal":            85.0,
    }.get(vendor_name, 78.0)


def _is_heavy_item(specs: AssetSpecs) -> bool:
    """True if the item is likely to require LTL freight (motors >10 HP, pumps, compressors)."""
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
                          force_refresh: bool = False) -> list[SourcingOption]:
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
                reliability_score=_vendor_reliability(vendor_name),
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
        results = _search_vendor_prices(specs)
        parsed  = _llm_parse_results(specs, results)
        heavy = _is_heavy_item(specs)
        seen: set[str] = set()
        for item in parsed:
            vendor       = item.get("vendor", "Unknown")
            price        = item.get("price")            # None = hidden / CfQ
            url          = item.get("url", "").strip()
            lead         = item.get("lead_days", 5)
            ship_fee     = item.get("shipping_fee")     # 0 = free, None = unknown
            ship_terms   = item.get("shipping_terms")   # "Free Shipping", "LTL Freight Required", "S.F.Q.", etc.
            is_freight   = bool(item.get("is_freight", False)) or (heavy and ship_fee is None and price is not None)
            found_pn     = (item.get("found_part_number") or "").strip() or None
            exact_match  = bool(item.get("exact_match", True))

            # Cross-reference: if found PN is present and differs from searched PN → Alternative
            searched_pn = (specs.part_number or "").upper().strip()
            if found_pn and searched_pn and found_pn.upper().strip() != searched_pn:
                exact_match = False
            match_type = "Exact" if exact_match else "Alternative"

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
            merchant_type = _vendor_merchant_type(vendor)
            fn_tag  = f" [Found: {found_pn}]" if found_pn else ""
            tag     = ("" if match_type == "Exact" else " [Alt]") + (" [LTL]" if is_freight else "") + fn_tag

            if price is not None:
                save_price(specs.part_number, vendor, float(price), int(lead), source="live", url=url)
                print(f"[Sourcing] Priced{tag}: {vendor} @ ${price:.2f} | {url}")
                options.append(SourcingOption(
                    vendor_name=vendor,
                    base_price=float(price),
                    lead_time_days=int(lead),
                    reliability_score=_vendor_reliability(vendor),
                    merchant_type=merchant_type,
                    requires_rfq=False,
                    notes=f"Live{tag}",
                    source_url=url,
                    price_tbd=False,
                    extracted_shipping_fee=float(ship_fee) if ship_fee is not None else None,
                    is_freight=is_freight,
                    match_type=match_type,
                    found_part_number=found_pn,
                    shipping_terms=resolved_terms,
                ))
            else:
                print(f"[Sourcing] Inquiry Required{tag}: {vendor} | {url}")
                options.append(SourcingOption(
                    vendor_name=vendor,
                    base_price=0.0,
                    lead_time_days=int(lead),
                    reliability_score=_vendor_reliability(vendor),
                    merchant_type=merchant_type,
                    requires_rfq=False,
                    notes=f"Price inquiry required{tag}",
                    source_url=url,
                    price_tbd=True,
                    extracted_shipping_fee=None,
                    is_freight=is_freight,
                    match_type=match_type,
                    found_part_number=found_pn,
                    shipping_terms=resolved_terms,
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


def _build_tier2_query(specs: AssetSpecs) -> str:
    """Build a national specialist discovery query using detected_type."""
    detected = getattr(specs, "detected_type", None) or ""
    desc     = (specs.description or "").strip()
    equip_term = detected or desc or "industrial equipment"

    known = (specs.manufacturer not in ("Unknown", "N/A", "null") and
             specs.model        not in ("Unknown", "N/A", "null"))
    suffix = f"{specs.manufacturer} {specs.model} equivalent" if known else ""

    return f"{equip_term} distributor USA buy online {suffix}".strip()


def _discover_national_specialists(specs: AssetSpecs,
                                    enterprise_options: list[SourcingOption]) -> list[SourcingOption]:
    """Tier 2: open-web national specialist discovery.

    Searches the full US internet using detected_type so brand-agnostic specialists
    (e.g. pump distributors, conveyor suppliers) that list Add-to-Cart pricing appear.
    No price estimation — if price not found in snippet, the option is price_tbd=True (→ Tier 3).
    """
    query = _build_tier2_query(specs)
    print(f"[Sourcing] Tier 2 national query: {query!r}")

    try:
        response = _tavily.search(query=query, search_depth="advanced", max_results=10)
        results  = response.get("results", [])
    except Exception as exc:
        print(f"[Sourcing] Tier 2 Tavily error: {exc}")
        return []

    if not results or not ANTHROPIC_API_KEY:
        return []

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

        options.append(SourcingOption(
            vendor_name=name,
            base_price=base_price,
            lead_time_days=lead,
            reliability_score=78.0,
            merchant_type="Direct Buy via Arkim",
            requires_rfq=tbd,     # TBD options → Tier 3 email RFQ workflow
            contact_email=email,
            admin_fee=50.0 if tbd else 0.0,
            notes=f"National specialist — {url}" if url else "National specialist",
            source_url=url,
            price_tbd=tbd,
        ))
        tag = "TBD" if tbd else f"${base_price:.2f}"
        print(f"  Tier 2: {name} — {tag} | {lead}d")

    if not options:
        print("[Sourcing] Tier 2: no qualifying national specialists found")
    return options


# ---------------------------------------------------------------------------
# Tier 3 — RFQ Email Draft Generator
# ---------------------------------------------------------------------------

def draft_rfq_email(specs: AssetSpecs, vendor: SourcingOption) -> str:
    """Generate a professional RFQ email using real contact details from Tier 2 search."""
    to_address = vendor.contact_email or "[ vendor email — see contact details below ]"

    # Parse contact block out of notes (format: "Address: ... | Phone: ... | Web: ...")
    contact_lines = []
    if vendor.notes:
        for part in vendor.notes.split(" | "):
            part = part.strip()
            if part and not part.startswith("Local supplier") and not part.startswith("Generic"):
                contact_lines.append(f"  {part}")
    contact_block = "\n".join(contact_lines) if contact_lines else "  (Verify contact details before sending)"

    return f"""To: {to_address}
Subject: Request for Quote — {specs.manufacturer} {specs.model} | Arkim Procurement

Dear {vendor.vendor_name} Sales Team,

My name is [Arkim Agent], and I represent Arkim Industrial Procurement Services.
We are sourcing the following component on behalf of a client and would like to
request your best available pricing and lead time.

──────────────────────────────────────────
PART DETAILS
──────────────────────────────────────────
  Manufacturer  : {specs.manufacturer}
  Model         : {specs.model}
  Part Number   : {specs.part_number}
  Voltage       : {specs.voltage}
  Horsepower    : {specs.hp}
  Description   : {specs.description}
  Serial Ref.   : {specs.serial_number}

──────────────────────────────────────────
REQUESTED INFORMATION
──────────────────────────────────────────
  1. Unit price (quantity: 1 and 5)
  2. Lead time (standard and expedited)
  3. Warranty / return policy
  4. Shipping terms (FOB origin or destination)

Please reply to this email with your quote at your earliest convenience.
We aim to make a purchasing decision within 48 hours.

Thank you for your time,

[Arkim Procurement Agent]
Arkim Industrial Services
procurement@arkim.ai | (800) 555-ARKIM

──────────────────────────────────────────
VENDOR CONTACT ON FILE (from Arkim search)
──────────────────────────────────────────
{contact_block}
──────────────────────────────────────────""".strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_vendors(specs: AssetSpecs,
                 site: str = "La Mirada",
                 force_refresh: bool = False) -> tuple[list[SourcingOption], Optional[str]]:
    """
    Run full multi-tier sourcing.

    Returns:
        (all_options, None)  — RFQ emails are generated per-vendor in the UI.
    """
    print(f"\n[Sourcing] Tier 1/1.5 — Querying national vendors via Tavily...")
    enterprise = _call_enterprise_api(specs, force_refresh=force_refresh)
    for o in enterprise:
        tag = " [INQUIRY REQUIRED]" if o.price_tbd else f" @ ${o.base_price:.2f}"
        print(f"  {o.vendor_name} ({o.merchant_type}){tag} | {o.lead_time_days}d")

    print(f"\n[Sourcing] Tier 2 — National specialist discovery...")
    tier2 = _discover_national_specialists(specs, enterprise)

    return enterprise + tier2, None
