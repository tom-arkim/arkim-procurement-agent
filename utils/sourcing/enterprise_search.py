"""
utils/sourcing/enterprise_search.py
Tier 1/1.5 (_call_enterprise_api) and Tier 2 (_discover_national_specialists).

Both tiers share helpers (_base_reliability, _is_heavy_item, _vendor_merchant_type)
and differ mainly in merchant_type assignment and how prices are sourced.
"""

import json
import re
from typing import Optional

from utils.models import AssetSpecs, SourcingOption
from utils.sourcing.constants import (
    TARGET_VENDORS,
    _VERIFIED_PARTNERS,
    _MERCHANT_RELIABILITY,
    _TIER1_VENDORS,
)
from utils.sourcing.scoring import (
    _compute_suitability_score,
    _suitability_tier,
    _home_field_bonus,
    _compute_confidence_score,
    _is_collection_url,
)
from utils.sourcing.filtering import _counterfeit_risk_flag
from utils.sourcing.tavily_client import _search_vendor_prices, _build_tier2_query
from utils.sourcing.llm_parsing import _anthropic_complete, _llm_parse_results
from utils.sourcing.price_sanity import _apply_price_sanity
from utils.sourcing.market_confidence import _fetch_market_confidence
from utils.sourcing.vendor_tokens import _get_vendor_token


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _vendor_merchant_type(vendor_name: str) -> str:
    return "Enterprise" if vendor_name in _TIER1_VENDORS else "National Specialist"


def _base_reliability(merchant_type: str) -> float:
    return _MERCHANT_RELIABILITY.get(merchant_type, 78.0)


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


# ---------------------------------------------------------------------------
# Tier 1 / 1.5
# ---------------------------------------------------------------------------

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
        snippet_map = {
            r.get("url", ""): (r.get("title", "") + " " + r.get("content", "")).strip()
            for r in results if r.get("url")
        }
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
            limited_price   = bool(item.get("limited_price_data", False))
            exact_match     = bool(item.get("exact_match", True))
            heavy           = _is_heavy_item(specs, weight_lbs)
            is_freight      = bool(item.get("is_freight", False)) or (heavy and ship_fee is None and price is not None)

            searched_pn = (specs.part_number or "").upper().strip()
            if found_pn and searched_pn and found_pn.upper().strip() != searched_pn:
                exact_match = False
            if exact_match:
                match_type = "Exact OEM"
            elif found_pn:
                match_type = "Aftermarket Compatible"
            else:
                match_type = "Functional Alternative"

            if ship_fee is not None and ship_fee == 0:
                resolved_terms = "Free Shipping"
            elif is_freight:
                resolved_terms = ship_terms or "LTL Freight Required"
            elif ship_terms:
                resolved_terms = ship_terms
            else:
                resolved_terms = None

            if not url or vendor in seen:
                if not url:
                    print(f"[Sourcing] Skipping {vendor} — no URL")
                continue
            seen.add(vendor)

            is_coll = _is_collection_url(url)
            if is_coll:
                exact_match = False
                match_type  = "Functional Alternative"
                print(f"[Sourcing] Collection page detected — flagging as Functional Alternative: {url}")

            if heavy and resolved_terms is None and ship_fee is None:
                resolved_terms = "LTL Freight Required"
                is_freight     = True

            merchant_type = _vendor_merchant_type(vendor)
            fn_tag   = f" [Found: {found_pn}]" if found_pn else ""
            coll_tag = " [LIST PAGE]" if is_coll else ""
            tag      = ("" if match_type == "Exact OEM" else f" [{match_type}]") + (" [LTL]" if is_freight else "") + fn_tag + coll_tag

            if found_pn is None and price is not None:
                print(f"[Sourcing] PN Enforcement: {vendor} has no found_part_number "
                      f"— stripping price, demoting to Inquiry Required")
                price = None

            snippet = snippet_map.get(url, "")
            suit    = _compute_suitability_score(specs, snippet, url, found_pn)

            if sanity_flagged:
                suit = 0.0

            stier       = _suitability_tier(vendor, suit)
            auth_status = "Authorized" if stier == "Gold" else "Unknown"
            cf_risk     = _counterfeit_risk_flag(specs, url, auth_status)
            conf_score  = _compute_confidence_score(specs, suit, match_type, auth_status)
            is_oem_dir  = _home_field_bonus(specs, url, snippet) > 0

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
                suitability_tier=stier,
                warranty_terms=warranty,
                weight_lbs=weight_lbs,
                market_confidence_score=mkt_conf,
                counterfeit_risk_flag=cf_risk,
                vendor_authorization_status=auth_status,
                confidence_score=conf_score,
                limited_price_data=limited_price,
                is_oem_direct=is_oem_dir,
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
# Tier 2 — National Specialist Discovery
# ---------------------------------------------------------------------------

_NATIONAL_SPECIALIST_SYSTEM = """You are a procurement data extractor for industrial equipment.
Given web search results, identify national specialist vendors that sell or distribute this equipment type.

Return ONLY valid JSON — a list of up to 5 objects:
[
  {
    "name":              "Vendor Business Name",
    "website":           "https://...",
    "email":             null,
    "phone":             null,
    "lead_days":         7,
    "price":             null,
    "found_part_number": null,
    "pn_match_status":   "not_visible"
  }
]

Rules:
- Include US-based national distributors, online retailers, and specialist suppliers.
- EXCLUDE: Grainger, McMaster-Carr, MSC Industrial, Amazon, eBay, Home Depot, Alibaba, Fastenal.
- price: a number if a specific price is explicitly visible in the snippet; null if hidden or missing.
- lead_days: use stated shipping time, otherwise default 7.
- Set email/phone to null if not in the snippets — do not invent contact details.
- Return [] if no qualifying national specialist vendors appear.

Part number matching (required for every result):
- found_part_number: the exact part number string visible in the snippet or page title. null if none shown.
- pn_match_status: classify the match against the searched part number:
    "exact_match"   — snippet shows a part number identical to the searched PN
    "partial_match" — snippet shows a related or variant PN (different suffix/prefix, but clearly related)
    "no_match"      — a clearly different part number is shown that does not match the searched PN
    "not_visible"   — no part number is visible in the snippet at all
"""


def _discover_national_specialists(specs: AssetSpecs,
                                    enterprise_options: list[SourcingOption]) -> list[SourcingOption]:
    """Tier 2: open-web national specialist discovery.

    Searches the full US internet using detected_type so brand-agnostic specialists
    (e.g. pump distributors, conveyor suppliers) that list Add-to-Cart pricing appear.
    No price estimation — if price not found in snippet, the option is price_tbd=True (-> Tier 3).
    """
    import utils.sourcing as _pkg

    query = _build_tier2_query(specs)
    print(f"[Sourcing] Tier 2 national query: {query!r}")

    if not _pkg._tavily:
        print("[Sourcing] Tier 2 skipped — Tavily not initialised.")
        return []

    try:
        response = _pkg._tavily.search(query=query, search_depth="advanced", max_results=10)
        results  = response.get("results", [])
    except Exception as exc:
        print(f"[Sourcing] Tier 2 Tavily error: {exc}")
        return []

    if not results or not _pkg.ANTHROPIC_API_KEY:
        return []

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

    tier1_lower = {"grainger", "mcmaster", "mcmaster-carr", "msc industrial",
                   "motion industries", "applied industrial"}
    options: list[SourcingOption] = []

    for v in vendors[:5]:
        name = (v.get("name") or "").strip()
        if any(t in name.lower() for t in tier1_lower):
            continue
        url      = (v.get("website") or "").strip() or None
        price    = v.get("price")
        lead     = max(1, int(v.get("lead_days") or 7))
        email    = v.get("email") or None
        found_pn = (v.get("found_part_number") or "").strip() or None
        pn_status = (v.get("pn_match_status") or "not_visible").strip()
        if pn_status not in ("exact_match", "partial_match", "no_match", "not_visible"):
            pn_status = "not_visible"

        tbd        = price is None
        base_price = float(price) if price is not None else 0.0

        snippet = snippet_map.get(url or "", "")
        if not snippet:
            slug = re.sub(r"[^a-z0-9]", "", name.lower())
            for surl, stext in snippet_map.items():
                if slug[:6] in surl.lower():
                    snippet = stext
                    break

        # PN enforcement: no_match → annotate and skip scoring
        if pn_status == "no_match":
            print(f"[Sourcing] Tier 2 PN no_match (pn_mismatch): {name} — "
                  f"found '{found_pn}' vs searched '{specs.part_number}'")
            options.append(SourcingOption(
                vendor_name=name,
                base_price=0.0,
                lead_time_days=lead,
                reliability_score=_base_reliability("Quote Request"),
                merchant_type="Quote Request",
                requires_rfq=True,
                contact_email=email,
                source_url=url,
                price_tbd=True,
                found_part_number=found_pn,
                pn_match_status=pn_status,
                rejection_reason="pn_mismatch",
                match_type="Functional Alternative",
            ))
            continue

        # Determine match_type from pn_status
        if pn_status == "exact_match":
            match_type = "Exact OEM"
        elif pn_status == "partial_match":
            match_type = "Aftermarket Compatible"
        else:
            match_type = "Functional Alternative"

        # Pass found_pn to scoring only when we have a confirmed match
        scoring_pn = found_pn if pn_status in ("exact_match", "partial_match") else None
        suit = _compute_suitability_score(specs, snippet, url or "", found_pn=scoring_pn)

        # not_visible: cap suitability at 45
        if pn_status == "not_visible" and suit > 45:
            suit = 45.0

        stier     = _suitability_tier(name, suit)
        t2_is_oem = _home_field_bonus(specs, url or "", snippet) > 0

        # 1.2 — "Direct Buy via Arkim" reserved for verified partners only.
        # All other Tier 2 vendors are "Quote Request" until onboarded.
        # TODO: populate _VERIFIED_PARTNERS from DB when partner onboarding is live.
        assert isinstance(_VERIFIED_PARTNERS, set), "_VERIFIED_PARTNERS must be a set"
        t2_merchant = "Direct Buy via Arkim" if name in _VERIFIED_PARTNERS else "Quote Request"

        t2_auth = "Authorized" if name in _VERIFIED_PARTNERS else "Unknown"
        t2_cf   = _counterfeit_risk_flag(specs, url or "", t2_auth)
        t2_conf = _compute_confidence_score(specs, suit, match_type, t2_auth)
        # not_visible: the suitability cap at 45 already signals uncertainty.
        # An additional confidence penalty was redundant and caused a regression
        # where legitimate Tier 2 results with any non-visible PN scored below
        # the confidence floor (42.5 → floor 40, barely passing; 22.5 with penalty
        # → always failed). Removed per regression fix.

        options.append(SourcingOption(
            vendor_name=name,
            base_price=base_price,
            lead_time_days=lead,
            reliability_score=_base_reliability(t2_merchant),
            merchant_type=t2_merchant,
            requires_rfq=tbd,
            contact_email=email,
            admin_fee=0.0,
            notes=f"National specialist — {url}" if url else "National specialist",
            source_url=url,
            price_tbd=tbd,
            suitability_score=suit,
            suitability_tier=stier,
            counterfeit_risk_flag=t2_cf,
            vendor_authorization_status=t2_auth,
            onboarding_status="Active" if name in _VERIFIED_PARTNERS else "Not Onboarded",
            confidence_score=t2_conf,
            is_oem_direct=t2_is_oem,
            found_part_number=found_pn,
            pn_match_status=pn_status,
            match_type=match_type,
        ))
        tag = "TBD" if tbd else f"${base_price:.2f}"
        print(f"  Tier 2: {name} — {tag} | {lead}d | suit={suit:.0f}% | {t2_merchant} | "
              f"{stier or 'no tier'} | pn={pn_status}")

    if not options:
        print("[Sourcing] Tier 2: no qualifying national specialists found")
    return options


# ---------------------------------------------------------------------------
# Aftermarket / Third-Party Specialist Discovery
# ---------------------------------------------------------------------------

_AFTERMARKET_SPECIALIST_SYSTEM = """You are a procurement data extractor for industrial parts.
Given web search results, identify third-party manufacturers and aftermarket specialists
who supply functionally equivalent replacement parts — NOT the OEM manufacturer.

Return ONLY valid JSON — a list of up to 5 objects:
[
  {
    "name":      "Vendor Business Name",
    "website":   "https://...",
    "email":     null,
    "price":     null,
    "lead_days": 7
  }
]

Rules:
- Include aftermarket manufacturers, independent seal/bearing/belt suppliers, third-party specialists.
- EXCLUDE the OEM manufacturer that makes the original part.
- EXCLUDE major national distributors: Grainger, McMaster-Carr, MSC Industrial, Amazon, eBay.
- price: a number if a specific unit price is explicitly visible in the snippet; null if not shown.
- lead_days: use stated shipping time; default 7 if unknown.
- Return [] if no qualifying aftermarket vendors are found.
"""


def _build_aftermarket_query(specs: AssetSpecs) -> str:
    """Spec-based query — omits OEM brand and part number to find aftermarket equivalents."""
    parts: list[str] = []

    detected = (getattr(specs, "detected_type", None) or "").lower()
    if detected:
        parts.append(detected)

    # Motor/Equipment fit specs — hp and frame are primary for cross-referencing
    # equivalent motors (e.g., 150HP 447T narrows to the right frame class).
    # voltage and rpm further constrain to compatible units.
    if getattr(specs, "hp", None):
        parts.append(f"{specs.hp}")
    if getattr(specs, "frame", None):
        parts.append(f"{specs.frame} frame")
    if getattr(specs, "voltage", None) and specs.voltage not in ("N/A", "Unknown"):
        parts.append(specs.voltage)
    if getattr(specs, "rpm", None):
        parts.append(specs.rpm)

    # Part/seal dimensional specs
    if getattr(specs, "shaft_size", None):
        size = re.sub(r'"', " inch", specs.shaft_size)
        parts.append(f"{size} shaft")
    if getattr(specs, "bore_diameter", None):
        parts.append(specs.bore_diameter)
    if getattr(specs, "material_spec", None):
        parts.append(specs.material_spec)
    if getattr(specs, "connection_size", None):
        parts.append(specs.connection_size)

    # Type number from description (e.g. "Type 21")
    desc = specs.description or ""
    type_m = re.search(r"\bType\s+\d+\b", desc, re.IGNORECASE)
    if type_m:
        parts.append(type_m.group(0))

    parts.append("aftermarket equivalent supplier price buy")
    return " ".join(filter(None, parts))


def _discover_aftermarket_specialists(
    specs: AssetSpecs,
    all_existing_options: list[SourcingOption],
) -> list[SourcingOption]:
    """Spec-based aftermarket discovery — finds third-party equivalents by description.

    Only runs when:
    - specs.category == "Part"
    - detected_type contains a term from AFTERMARKET_VIABLE_CATEGORIES
    - warranty_status is not "in_warranty"
    """
    import utils.sourcing as _pkg
    from utils.sourcing.constants import AFTERMARKET_VIABLE_CATEGORIES

    warranty = (getattr(specs, "warranty_status", None) or "").lower()
    if warranty == "in_warranty":
        print("[Sourcing] Aftermarket pass skipped — asset is in warranty")
        return []

    # Gate by AFTERMARKET_VIABLE_CATEGORIES rather than category field.
    # The old `category != "Part"` guard incorrectly blocked Equipment items like
    # motors, which are in AFTERMARKET_VIABLE_CATEGORIES and have a healthy
    # aftermarket (WEG, Baldor, Marathon equivalents). The category check was
    # redundant — if detected_type doesn't match a viable category, the next
    # guard below catches it anyway.
    dtype_lower = (getattr(specs, "detected_type", None) or "").lower()
    if not any(cat in dtype_lower for cat in AFTERMARKET_VIABLE_CATEGORIES):
        print(f"[Sourcing] Aftermarket pass skipped — '{dtype_lower}' not in viable categories")
        return []

    if not _pkg._tavily:
        print("[Sourcing] Aftermarket pass skipped — Tavily not initialised")
        return []

    query = _build_aftermarket_query(specs)
    print(f"[Sourcing] Aftermarket spec-based query: {query!r}")

    try:
        response = _pkg._tavily.search(query=query, search_depth="advanced", max_results=8)
        results  = response.get("results", [])
    except Exception as exc:
        print(f"[Sourcing] Aftermarket Tavily error: {exc}")
        return []

    if not results or not _pkg.ANTHROPIC_API_KEY:
        return []

    snippet_map = {
        r.get("url", ""): (r.get("title", "") + " " + r.get("content", "")).strip()
        for r in results if r.get("url")
    }
    snippets = [
        f"URL: {r.get('url', '')}\nTitle: {r.get('title', '')}\nSnippet: {r.get('content', '')}\n"
        for r in results
    ]
    equip_term = getattr(specs, "detected_type", None) or specs.description or "industrial part"
    user_msg   = f"Finding aftermarket suppliers for: {equip_term}\n\n" + "\n---\n".join(snippets)

    existing_lower = {o.vendor_name.lower() for o in all_existing_options}
    oem_lower      = (specs.manufacturer or "").lower()

    try:
        raw   = _anthropic_complete(_AFTERMARKET_SPECIALIST_SYSTEM, user_msg)
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            return []
        vendors = [v for v in json.loads(match.group(0))
                   if isinstance(v, dict) and v.get("name")]
        print(f"[Sourcing] Aftermarket found {len(vendors)} potential vendor(s)")
    except Exception as exc:
        print(f"[Sourcing] Aftermarket LLM error: {exc}")
        return []

    options: list[SourcingOption] = []

    for v in vendors[:5]:
        name = (v.get("name") or "").strip()
        if not name:
            continue
        if oem_lower and name.lower() == oem_lower:
            continue
        if name.lower() in existing_lower:
            continue

        url   = (v.get("website") or "").strip() or None
        price = v.get("price")
        lead  = max(1, int(v.get("lead_days") or 7))

        tbd        = price is None
        base_price = float(price) if price is not None else 0.0

        snippet = snippet_map.get(url or "", "")
        if not snippet:
            slug = re.sub(r"[^a-z0-9]", "", name.lower())
            for surl, stext in snippet_map.items():
                if slug[:6] in surl.lower():
                    snippet = stext
                    break

        suit = _compute_suitability_score(specs, snippet, url or "", found_pn=None)
        # Aftermarket: -15 confidence penalty (match by inference, not by PN)
        conf = max(0.0, _compute_confidence_score(specs, suit, "Aftermarket Equivalent", "Unknown") - 15.0)

        options.append(SourcingOption(
            vendor_name=name,
            base_price=base_price,
            lead_time_days=lead,
            reliability_score=_base_reliability("Quote Request"),
            merchant_type="Aftermarket Specialist",
            requires_rfq=tbd,
            source_url=url,
            price_tbd=tbd,
            match_type="Aftermarket Equivalent",
            suitability_score=suit,
            confidence_score=conf,
            vendor_authorization_status="Unknown",
            onboarding_status="Not Onboarded",
            notes=f"Aftermarket equivalent — {url}" if url else "Aftermarket equivalent",
        ))
        tag = "TBD" if tbd else f"${base_price:.2f}"
        print(f"  Aftermarket: {name} — {tag} | {lead}d | suit={suit:.0f}% | conf={conf:.0f}%")

    if not options:
        print("[Sourcing] Aftermarket: no qualifying vendors found")
    return options
