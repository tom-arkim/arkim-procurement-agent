"""
utils/sourcing/scoring.py
Pure suitability, confidence, and penalty scoring functions.

All functions are stateless (no API calls, no DB access).
The brand_intelligence module provides dynamic manufacturer context.
"""

import re
from typing import Optional

from utils.sourcing.constants import (
    _COLLECTION_URL_PATTERNS,
    _LOW_VALUE_SUBDOMAINS,
    _HIGH_COUNTERFEIT_RISK_CATEGORIES,
    _MARKETPLACE_DOMAINS,
    _VERIFIED_PARTNERS,
)
from utils.brand_intelligence import (
    get_wrong_category_terms,
    get_parent_brand,
)


# ---------------------------------------------------------------------------
# URL classification helpers
# ---------------------------------------------------------------------------

def _is_collection_url(url: str) -> bool:
    u = url.lower()
    return any(p in u for p in _COLLECTION_URL_PATTERNS)


def _is_low_value_landing_page(url: str, snippet: str, searched_pn: str) -> bool:
    """Return True for pages that cannot confirm product availability.

    Triggers when:
    - The URL subdomain is a known marketing/informational subdomain.
    - The URL path contains /category/ or /categories/ and the searched PN is
      absent from the snippet (pure browse page, not a product match).
    """
    from urllib.parse import urlparse
    u = url.lower()
    try:
        hostname = urlparse(u).hostname or ""
        parts    = hostname.split(".")
        if len(parts) >= 3 and any(f"{parts[0]}." == sub for sub in _LOW_VALUE_SUBDOMAINS):
            return True
    except Exception:
        pass
    if ("/category/" in u or "/categories/" in u) and searched_pn:
        if searched_pn.lower() not in (snippet or "").lower():
            return True
    return False


# ---------------------------------------------------------------------------
# Equipment type detection
# ---------------------------------------------------------------------------

def _detect_equip_type(specs) -> str:
    """Return the primary equipment-type keyword from detected_type or description."""
    ctx = (getattr(specs, 'detected_type', None) or specs.description or '').lower()
    for kw in ("motor", "pump", "compressor", "blower", "conveyor",
               "vfd", "starter", "bearing", "seal", "coupling", "belt", "contactor", "sensor"):
        if kw in ctx:
            return kw
    return ""


# ---------------------------------------------------------------------------
# Home field bonus (1.7)
# ---------------------------------------------------------------------------

def _home_field_bonus(specs, url: str, snippet: str) -> float:
    """Return +50 when the vendor domain is clearly the manufacturer/brand home page
    AND the page shows commerce signals (price, add to cart, buy).
    Return +25 when the domain matches the manufacturer but no commerce signals found.
    """
    mfg = (specs.manufacturer or "").lower().strip()
    if not mfg or mfg in ("unknown", "n/a", "null"):
        return 0.0

    try:
        from urllib.parse import urlparse
        hostname = (urlparse(url.lower()).hostname or "").replace("www.", "")
    except Exception:
        return 0.0

    mfg_slug  = re.sub(r"[^a-z0-9]", "", mfg)
    host_slug = re.sub(r"[^a-z0-9]", "", hostname)

    if mfg_slug not in host_slug:
        return 0.0

    commerce_signals = ("add to cart", "buy now", "add to order", "price", "purchase",
                        "in stock", "order now", "checkout")
    s_lower = (snippet or "").lower()
    has_commerce = any(sig in s_lower for sig in commerce_signals)

    return 50.0 if has_commerce else 25.0


# ---------------------------------------------------------------------------
# Counterfeit risk penalty (1.9)
# ---------------------------------------------------------------------------

def _counterfeit_suitability_penalty(url: str,
                                      vendor_authorization_status: str,
                                      is_risky_category: bool) -> float:
    """Return a suitability deduction for counterfeit-risk signals.

    -30 pts: marketplace domain AND high-risk category
    -15 pts: marketplace domain only (lower risk category)
    +15 pts: vendor is Authorized (bonus applies regardless of category)
    Net result is additive — authorized marketplace = -30 + 15 = -15.
    """
    penalty = 0.0
    try:
        from urllib.parse import urlparse
        hostname = (urlparse(url.lower()).hostname or "").replace("www.", "")
    except Exception:
        hostname = ""

    is_marketplace = any(dom in hostname for dom in _MARKETPLACE_DOMAINS)
    if is_marketplace:
        penalty -= 30.0 if is_risky_category else 15.0

    if vendor_authorization_status == "Authorized":
        penalty += 15.0

    return penalty


# ---------------------------------------------------------------------------
# Main suitability score
# ---------------------------------------------------------------------------

def _compute_suitability_score(specs, snippet: str, url: str,
                                found_pn: Optional[str] = None) -> float:
    """0-100 score: how well this vendor/page matches the sourcing requirement.

    Primary key -- PN mention (guardrail):
      If neither the searched PN nor a functional equivalent appears in the snippet,
      the total score is capped at 45 regardless of other signals.

    Components
      PN match        : 0 / 10 / 25 / 40 pts
      Equipment type  : 0-15 pts  (detected_type words in snippet)
      Manufacturer    : 0-10 pts  (+40 parent-brand bonus)
      Authorized dist : 0-20 pts  (bonus for authorized distributor / service center)
      Direct URL      : 0-10 pts  (product page vs list/search page)
    """
    s       = (snippet or "").lower()
    u_lower = url.lower()

    _spn_early = (specs.part_number or "").upper().strip()
    if _is_low_value_landing_page(url, snippet, _spn_early):
        return 0.0

    # Guardrail 0: niche mismatch -- hard 0.0 when 2+ wrong-category terms appear.
    dtype_lower = (getattr(specs, "detected_type", "") or specs.description or "").lower()
    _equip_kw = _detect_equip_type(specs)
    if _equip_kw:
        _bad_terms = get_wrong_category_terms(specs.manufacturer, _equip_kw)
        if _bad_terms:
            hit_count = sum(1 for t in _bad_terms if t in s or t in u_lower)
            if hit_count >= 2:
                return 0.0

    # Guardrail 0b: motor-without-electric verification
    if "motor" in dtype_lower:
        has_motor_signal = "motor" in s or "electric" in s
        has_wrong_signal = "pump" in s or "hydraulic" in s
        if has_wrong_signal and not has_motor_signal:
            return 0.0

    # PN match (primary key)
    searched_pn = (specs.part_number or "").upper().strip()
    found_upper = (found_pn or "").upper().strip()
    pn_exact   = bool(found_upper and found_upper == searched_pn)
    pn_in_snip = bool(searched_pn and searched_pn.lower() in s)
    pn_alt     = bool(found_pn and not pn_exact)

    if pn_exact:
        pn_pts = 40
    elif pn_in_snip:
        pn_pts = 25
    elif pn_alt:
        pn_pts = 10
    else:
        pn_pts = 0

    # Equipment type match
    detected = (getattr(specs, "detected_type", "") or "").lower()
    type_pts  = 0
    if detected:
        words = [w for w in detected.split() if len(w) > 3]
        if words:
            matched  = sum(1 for w in words if w in s)
            type_pts = round(15 * matched / len(words))

    # Manufacturer match
    mfg     = (specs.manufacturer or "").lower()
    mfg_pts = 10 if (mfg and mfg not in ("unknown", "n/a", "null") and mfg in s) else 0

    # Parent brand bonus: brand intelligence resolves child brand -> parent company.
    _parent = get_parent_brand(mfg.strip(), _detect_equip_type(specs))
    if _parent and _parent.lower() in s:
        mfg_pts = max(mfg_pts, 40)

    # Distributor / stockist bonus (category-aware)
    if specs.category == "Part":
        stockist_phrases = ("in stock", "available", "ships today", "ready to ship",
                            "cross-reference", "interchange", "aftermarket")
        if any(p in s for p in stockist_phrases):
            auth_pts = 20
        elif "distributor" in s or "distributor" in url.lower() or "supply" in url.lower():
            auth_pts = 10
        else:
            auth_pts = 0
    else:
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

    is_coll = _is_collection_url(url)
    url_pts = 0 if is_coll else 10

    pn_mismatch_penalty = 30 if pn_alt else 0

    home_bonus = _home_field_bonus(specs, url, snippet)

    dtype_for_cf = (getattr(specs, "detected_type", "") or specs.description or "").lower()
    is_risky_cat = any(cat in dtype_for_cf for cat in _HIGH_COUNTERFEIT_RISK_CATEGORIES)
    cf_penalty   = _counterfeit_suitability_penalty(url, "Unknown", is_risky_cat)

    total = pn_pts + type_pts + mfg_pts + auth_pts + url_pts - pn_mismatch_penalty + home_bonus + cf_penalty

    if pn_pts == 0:
        total = min(total, 45)

    if is_coll:
        total = min(total, 5)

    return min(100.0, max(0.0, round(float(total), 1)))


# ---------------------------------------------------------------------------
# Suitability tier
# ---------------------------------------------------------------------------

def _suitability_tier(vendor_name: str, suitability: float) -> str:
    """Return Arkim network tier: Gold (verified partner), Silver (high-suitability target), or ''."""
    if vendor_name in _VERIFIED_PARTNERS:
        return "Gold"
    if suitability >= 75:
        return "Silver"
    return ""


# ---------------------------------------------------------------------------
# Confidence score (1.10)
# ---------------------------------------------------------------------------

def _compute_confidence_score(specs, suitability: float,
                               match_type: str,
                               vendor_authorization_status: str) -> float:
    """0-100 epistemic certainty that we have correctly identified and matched the part.

    Components:
      Suitability basis  (50 pts max) : scaled from suitability_score
      Match type         (30 pts max) : Exact OEM=30, Aftermarket=20, Functional=10
      Spec completeness  (10 pts max) : all critical specs present
      Authorization      (10 pts max) : Authorized vendor = +10
    """
    suit_pts = round(min(suitability, 100.0) * 0.50, 1)

    match_pts = {"Exact OEM": 30, "Aftermarket Compatible": 20, "Functional Alternative": 10}.get(
        match_type, 10
    )

    _null = {None, "", "null", "N/A", "Unknown", "UNKNOWN-PN"}
    spec_fields = [specs.manufacturer, specs.model, specs.part_number]
    if specs.category == "Equipment":
        spec_fields += [specs.voltage, specs.hp]
    filled   = sum(1 for f in spec_fields if f not in _null)
    spec_pts = round(10.0 * filled / max(len(spec_fields), 1), 1)

    auth_pts = 10.0 if vendor_authorization_status == "Authorized" else 0.0

    return min(100.0, round(suit_pts + match_pts + spec_pts + auth_pts, 1))
