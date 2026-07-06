"""
utils/sourcing/tavily_client.py
Tavily search client, query builders, and vendor authority scoring.

_search_vendor_prices and helpers read _tavily from the package at call
time so that _patch_sourcing_keys() in chat_app.py takes effect.
"""

import re

NON_US_TLDS = (
    ".cn", ".uk", ".de", ".fr", ".it", ".es", ".nl",
    ".pl", ".ru", ".jp", ".kr", ".tw", ".hk", ".sg",
    ".au", ".nz", ".br", ".mx", ".in", ".tr", ".za",
    # Nordic and Central European: major distributors (DigiKey, Mouser, Farnell)
    # maintain localized storefronts on these TLDs with non-USD pricing.
    ".se", ".no", ".fi", ".dk", ".be", ".at", ".ch",
    ".ie", ".pt", ".gr",
)

NON_US_DOMAIN_HINTS = (
    "antlets", "made-in-china", "indiamart", "tradeindia",
    "europages", "manufacturer.com.cn",
    # UK/European industrial distributors that operate on .com TLDs with non-USD pricing
    "farnell", "rs-online", "rsonline", "element14", "rs-components",
    "rscomponents", "distrelec", "buerklin", "mouser.co",
    # EU specialist distributors confirmed to appear in E+H sourcing runs
    "tme.eu", "tme.com", "automation24",
    # Note: "conrad" excluded from hints — substring match would hit unrelated .com domains
    # (e.g. conradson.com). Add as explicit host entry in _KNOWN_VENDOR_HOSTS when needed.
)

from utils.sourcing_archieved.constants import (
    _VENDOR_DOMAINS,
    _BLACKLISTED_DOMAINS,
    _AUTHORITY_VIABLE_THRESHOLD,
    _DYNAMIC_FALLBACK_MIN_VIABLE,
)
from utils.sourcing_archieved.scoring import _is_collection_url
from utils.brand_intelligence import get_competitors, get_subcategory_refinement, get_brand_relationships
from utils.sourcing_archieved.scoring import _detect_equip_type


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

        # Phase 2 — component-aware (T5b / defense-in-depth): when component_of is
        # set, lead with the COMPONENT-for-parent phrase so the query targets the
        # component for the named machine, never the bare parent. This mirrors the
        # Part branch below + _build_tier3_query + _build_aftermarket_query. The
        # intended classifier path only sets component_of for mechanical_seal
        # (categorized Part), so this branch is reached for a component-of part
        # only via an LLM extraction misclassification of the seal as Equipment —
        # defensive, but closes the last parent-led hole and keeps all four
        # builders consistent. Inert when component_of is None.
        from utils.procurement_agent.component_query import build_component_aware_query
        component_phrase = build_component_aware_query(specs)  # "X for Y" or None
        if component_phrase:
            parts.append(component_phrase)

        desc = (specs.description or "").strip()
        if desc:
            parts.append(desc)
        elif not component_phrase:
            # No component phrase and no description — fall back to the legacy
            # model-keyword heuristics. Skipped when component_phrase already
            # leads (the phrase carries the component + parent identity).
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

        parts.append("price buy USA")
        return " ".join(filter(None, parts))
    else:
        # A Part: lead with the COMPONENT term (detected_type) so the query
        # targets the part itself, never a bare parent machine. The F1 bug:
        # a mechanical-seal request where extraction set manufacturer/model to
        # the PARENT ("Goulds" "3196") produced "Goulds 3196 US distributor
        # price buy" — sourcing the pump, not the seal. Leading with
        # detected_type, and when component_of is set using the
        # "mechanical seal for Goulds 3196" phrase
        # (component_query.build_component_aware_query, the T5 helper), makes
        # the query component-led. detected_type is unconditional (intake
        # populates it regardless of INTAKE_TYPE_AWARE); component_of is
        # populated only under the flag. When detected_type is absent the
        # query falls back to the legacy mfg+mdl+pn anchors (byte-identical to
        # pre-fix for those rows). Matches the T5 pattern used in
        # _build_tier3_query / _build_aftermarket_query.
        pn  = specs.part_number
        mfg = specs.manufacturer if specs.manufacturer not in ("N/A", "Unknown") else ""
        mdl = specs.model        if specs.model        not in ("N/A", "Unknown") else ""
        if pn and pn not in ("N/A", "UNKNOWN-PN", "Unknown"):
            pn_term = f'"{pn}"'
        else:
            pn_term = ""

        from utils.procurement_agent.component_query import build_component_aware_query
        component_phrase = build_component_aware_query(specs)  # "X for Y" or None
        detected = (getattr(specs, "detected_type", None) or "").strip()

        base_parts: list[str] = []
        if component_phrase:
            base_parts.append(component_phrase)
        elif detected:
            base_parts.append(detected)
        # Identity anchors narrow to the specific unit. Skip a term already
        # carried in the leading phrase (e.g. mfg="Goulds" model="3196" are the
        # parent, already in "mechanical seal for Goulds 3196") so the parent
        # isn't doubled.
        _lower = " ".join(base_parts).lower()
        for term in (mfg, mdl, pn_term):
            if term and term.lower() not in _lower:
                base_parts.append(term)
                _lower = _lower + " " + term.lower()
        base = " ".join(base_parts)
        return f"{base} US distributor price buy"


def _build_tier3_query(specs) -> str:
    """Build an asset-specific national specialist discovery query (brief Section 8.3 Tier 3).

    Formerly named _build_tier2_query — see commit history for rename context.

    Tavily treats its query parameter as natural language — Boolean operators
    (AND, OR, parenthetical grouping) are literal text, not logical operators.
    This function produces quoted-anchor queries: high-signal terms are quoted
    phrases, unquoted tail words (authorized distributor buy USA) shape semantic
    ranking without forcing exact matches.

    Equipment: type + manufacturer + PN (when present) + model + auth brands + spec anchors
    Parts:     type + PN + manufacturer + auth brands
    """
    detected = (getattr(specs, "detected_type", None) or "").lower()
    desc     = (specs.description or "").lower()
    ctx      = detected or desc or ""

    # Determine primary niche term via brand intelligence (falls back to detected_type)
    _equip_kw  = _detect_equip_type(specs)
    niche_term = get_subcategory_refinement(specs.manufacturer, _equip_kw) if _equip_kw else None
    if not niche_term:
        niche_term = getattr(specs, "detected_type", None) or specs.description or "industrial equipment"

    # Phase 2 — component-aware anchor (T5b, gated INTAKE_TYPE_AWARE at the call
    # site / promotion). When component_of is set, anchor the query on the
    # COMPONENT-for-parent phrase so discovery targets the seal/bearing/etc. for
    # the named machine, never the bare parent. Inert when component_of is None
    # (flag off / no parent -> byte-identical to today's query).
    _component_of = getattr(specs, "component_of", None)
    if _component_of:
        niche_term = f"{niche_term} for {_component_of}"

    # Fetch authorized_service_brands for both Equipment and Part queries.
    _auth_brands: list[str] = []
    known_mfg = specs.manufacturer not in ("Unknown", "N/A", "null", None)
    if known_mfg and _equip_kw:
        try:
            _br = get_brand_relationships(specs.manufacturer, _equip_kw)
            _auth_brands = _br.get("authorized_service_brands") or []
        except Exception:
            pass

    pn       = specs.part_number
    known_pn = pn and pn not in ("N/A", "UNKNOWN-PN", "Unknown", None)

    if specs.category == "Part":
        q_parts: list[str] = [f'"{niche_term}"']
        if known_pn:
            q_parts.append(f'"{pn}"')
        if known_mfg:
            q_parts.append(f'"{specs.manufacturer}"')
        for ab in _auth_brands[:4]:
            if ab:
                q_parts.append(f'"{ab}"')
        if "seal" in ctx:
            q_parts.append("cross-reference aftermarket interchange")
        q_parts.append("authorized distributor buy USA")
        if _auth_brands:
            print(f"[Sourcing] Tier 3 Part query anchored on {len(_auth_brands)} authorized brand(s): {_auth_brands[:4]}")
        return " ".join(q_parts)
    else:
        q_parts = [f'"{niche_term}"']
        if known_mfg:
            q_parts.append(f'"{specs.manufacturer}"')
        # Include PN for Equipment when present -- previously this was always dropped
        if known_pn:
            q_parts.append(f'"{pn}"')
        model = (specs.model or "").strip()
        if model and model not in ("N/A", "Unknown", "null", ""):
            q_parts.append(f'"{model}"')
        for ab in _auth_brands[:4]:
            if ab:
                q_parts.append(f'"{ab}"')
        if specs.hp and specs.hp not in ("N/A", "None", "null"):
            q_parts.append(re.sub(r"\s+", "", specs.hp).upper())
        elif getattr(specs, "gpm", None):
            q_parts.append(re.sub(r"\s+", "", specs.gpm).upper())
        q_parts.append("authorized distributor buy USA")
        if _auth_brands:
            print(f"[Sourcing] Tier 3 query anchored on {len(_auth_brands)} authorized brand(s): {_auth_brands[:4]}")
        return " ".join(q_parts)


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
        if any(hostname.endswith(tld) for tld in NON_US_TLDS):
            return 0.0
        if any(hint in hostname for hint in NON_US_DOMAIN_HINTS):
            return 0.0
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
    import utils.sourcing_archieved as _pkg

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
