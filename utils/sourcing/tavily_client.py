"""
utils/sourcing/tavily_client.py
Tavily search client, query builders, and vendor authority scoring.

_search_vendor_prices and helpers read _tavily from the package at call
time so that _patch_sourcing_keys() in chat_app.py takes effect.
"""

import re

from utils.sourcing.constants import (
    _VENDOR_DOMAINS,
    _BLACKLISTED_DOMAINS,
    _AUTHORITY_VIABLE_THRESHOLD,
    _DYNAMIC_FALLBACK_MIN_VIABLE,
)
from utils.sourcing.scoring import _is_collection_url
from utils.brand_intelligence import get_competitors, get_subcategory_refinement, get_brand_relationships
from utils.sourcing.scoring import _detect_equip_type


# ---------------------------------------------------------------------------
# Query builders
# ---------------------------------------------------------------------------

def _build_search_query(specs, search_mode: str = "exact") -> str:
    """Build the Tavily search query.

    search_mode:
      "exact"       -- search only for the specific PN/model; no competitor injection.
      "equivalents" -- also inject competitor brands for functional-equivalent discovery.
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
            pn = specs.part_number
            if pn and pn not in ("N/A", "UNKNOWN-PN", "Unknown"):
                parts.append(f'"{pn}"')
            elif known:
                parts.append(f"{specs.manufacturer} {specs.model}")
            # Boolean OR competitor group -- brand intelligence provides manufacturer-specific alternatives
            _bi_equip_kw   = _detect_equip_type(specs)
            _bi_competitors = get_competitors(specs.manufacturer, _bi_equip_kw) if _bi_equip_kw else []
            if _bi_competitors:
                parts.append(f"({' OR '.join(_bi_competitors)})")
            if known:
                parts.append(f"OR equivalent to {specs.manufacturer} {specs.model}")
            parts.append('(OR "cross-reference" OR "interchange" OR "drop-in replacement")')
        else:
            pn = specs.part_number
            if pn and pn not in ("N/A", "UNKNOWN-PN", "Unknown"):
                parts.append(f'"{pn}"')
            elif known:
                parts.append(f"{specs.manufacturer} {specs.model}")

        parts.append("price buy")
        return " ".join(filter(None, parts))
    else:
        pn  = specs.part_number
        mfg = specs.manufacturer if specs.manufacturer not in ("N/A", "Unknown") else ""
        mdl = specs.model        if specs.model        not in ("N/A", "Unknown") else ""
        if pn and pn not in ("N/A", "UNKNOWN-PN", "Unknown"):
            pn_term = f'"{pn}"'
        else:
            pn_term = ""
        base = " ".join(p for p in [mfg, mdl, pn_term] if p)
        return f"{base} distributor price buy"


def _build_tier2_query(specs) -> str:
    """Build an asset-specific national specialist discovery query.

    Equipment: boolean AND/OR targeting authorized distributors and service centers.
             When brand intelligence returns authorized_service_brands, those brand names
             are added as an OR anchor so Tavily surfaces the OEM channel directly.
    Parts: cross-reference and stockist focused -- no "service center" requirement.
    """
    detected = (getattr(specs, "detected_type", None) or "").lower()
    desc     = (specs.description or "").lower()
    ctx      = detected or desc or ""

    # Determine primary niche term via brand intelligence (falls back to detected_type)
    _equip_kw  = _detect_equip_type(specs)
    niche_term = get_subcategory_refinement(specs.manufacturer, _equip_kw) if _equip_kw else None
    if not niche_term:
        niche_term = getattr(specs, "detected_type", None) or specs.description or "industrial equipment"

    # Fetch authorized_service_brands for Equipment queries so we can anchor on OEM channel
    _auth_brands: list[str] = []
    known_mfg = specs.manufacturer not in ("Unknown", "N/A", "null", None)
    if known_mfg and _equip_kw and specs.category == "Equipment":
        try:
            _br = get_brand_relationships(specs.manufacturer, _equip_kw)
            _auth_brands = _br.get("authorized_service_brands") or []
        except Exception:
            pass

    pn        = specs.part_number
    known_pn  = pn and pn not in ("N/A", "UNKNOWN-PN", "Unknown", None)

    if specs.category == "Part":
        q_parts = [
            '("authorized distributor" OR "stocking distributor" OR stockist OR "in stock" OR "cross-reference" OR interchange)',
            f'"{niche_term}"',
        ]
        if known_pn:
            q_parts.append(f'"{pn}"')
        if known_mfg:
            q_parts.append(f'"{specs.manufacturer}"')
        if "seal" in ctx:
            q_parts.append('("seal cross reference" OR "aftermarket" OR "equivalent" OR "interchange")')
        return " AND ".join(q_parts)
    else:
        # Build manufacturer anchor: prefer explicit authorized_service_brands if available
        if _auth_brands:
            # Quote each brand name and OR them together with the manufacturer itself
            _brand_terms = " OR ".join(
                f'"{ab}"' for ab in _auth_brands[:4] if ab
            )
            auth_anchor = f'("authorized distributor" OR "authorized dealer" OR {_brand_terms})'
        else:
            auth_anchor = '(authorized OR distributor OR "service center")'

        q_parts = [auth_anchor, f'"{niche_term}"']
        if known_mfg:
            q_parts.append(f'"{specs.manufacturer}"')
        if specs.hp and specs.hp not in ("N/A", "None", "null"):
            q_parts.append(f'"{re.sub(r"\\s+", "", specs.hp).upper()}"')
        elif getattr(specs, "gpm", None):
            q_parts.append(f'"{re.sub(r"\\s+", "", specs.gpm).upper()}"')

        query = " AND ".join(q_parts)
        if _auth_brands:
            print(f"[Sourcing] Tier 2 query anchored on {len(_auth_brands)} authorized brand(s): {_auth_brands[:4]}")
        return query


# ---------------------------------------------------------------------------
# Vendor authority scoring (Phase 3.3)
# ---------------------------------------------------------------------------

def _vendor_authority_score(url: str, content: str, title: str = "") -> float:
    """0-100 score for whether a URL is from a viable B2B industrial vendor.

    Used by dynamic Tier 1 discovery to rank unrestricted Tavily results
    before falling back to the hardcoded _VENDOR_DOMAINS list.
    """
    from urllib.parse import urlparse
    u_lower  = url.lower()
    combined = (content + " " + title).lower()

    if any(b in u_lower for b in _BLACKLISTED_DOMAINS):
        return 0.0

    score = 0.0
    try:
        hostname = urlparse(u_lower).hostname or ""
        if any(d in hostname for d in _VENDOR_DOMAINS):
            score += 60.0
    except Exception:
        pass

    if any(p in combined for p in ("add to cart", "in stock", "per unit", "unit price")):
        score += 20.0
    elif any(p in combined for p in ("price", "buy", "usd")):
        score += 10.0

    if any(t in combined for t in ("industrial", "distributor", "supply", "automation", "mro")):
        score += 10.0

    if not _is_collection_url(url):
        score += 10.0

    return min(100.0, score)


# ---------------------------------------------------------------------------
# Tavily search with dynamic discovery + fallback
# ---------------------------------------------------------------------------

def _search_vendor_prices(specs, search_mode: str = "exact") -> list[dict]:
    """Tavily search for Tier 1 / 1.5 pricing.

    Discovery-first: unrestricted Tavily search scored by vendor authority.
    Falls back to _VENDOR_DOMAINS-restricted search when fewer than
    _DYNAMIC_FALLBACK_MIN_VIABLE authoritative results are found, ensuring
    existing vetted vendors always surface even for obscure part queries.
    """
    import utils.sourcing as _pkg

    query = _build_search_query(specs, search_mode=search_mode)
    print(f"[Sourcing] Tavily query ({search_mode}): {query!r}")

    if not _pkg._tavily:
        print("[Sourcing] Tavily client not initialised -- TAVILY_API_KEY missing.")
        return []

    # Pass 1: unrestricted search
    try:
        response = _pkg._tavily.search(query=query, search_depth="advanced", max_results=15)
        results  = response.get("results", [])
    except Exception as exc:
        print(f"[Sourcing] Tavily error: {exc}")
        return []

    viable = [
        r for r in results
        if _vendor_authority_score(r.get("url", ""), r.get("content", ""), r.get("title", ""))
           >= _AUTHORITY_VIABLE_THRESHOLD
    ]
    print(f"[Sourcing] Dynamic discovery: {len(results)} results, {len(viable)} viable vendor pages")

    # Pass 2: supplement from known-good domains when dynamic discovery yields too few results
    if len(viable) < _DYNAMIC_FALLBACK_MIN_VIABLE:
        print(f"[Sourcing] Viable < {_DYNAMIC_FALLBACK_MIN_VIABLE} -- supplementing with domain-restricted fallback")
        try:
            fb_resp    = _pkg._tavily.search(query=query, search_depth="advanced",
                                              max_results=10, include_domains=_VENDOR_DOMAINS)
            fb_results = fb_resp.get("results", [])
            existing_urls = {r.get("url") for r in viable}
            for r in fb_results:
                if r.get("url") not in existing_urls:
                    viable.append(r)
            print(f"[Sourcing] After fallback: {len(viable)} total results")
        except Exception as exc:
            print(f"[Sourcing] Fallback search error: {exc}")
            return results

    return viable if viable else results
