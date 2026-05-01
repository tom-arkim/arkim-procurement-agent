"""
utils/sourcing/filtering.py
Post-processing filters applied after vendor options are collected:
  - Counterfeit risk flag
  - Warranty gate
  - Supplier registry enrichment
"""

import statistics as _stats
from typing import Optional

from utils.sourcing.constants import (
    _HIGH_COUNTERFEIT_RISK_CATEGORIES,
    _MARKETPLACE_DOMAINS,
    TIER_SURFACE_MIN_CONFIDENCE,
)


# ---------------------------------------------------------------------------
# Counterfeit risk flag (1.9)
# ---------------------------------------------------------------------------

def _counterfeit_risk_flag(specs, url: str,
                            vendor_authorization_status: str) -> bool:
    """Return True when the combination of category + source domain signals
    elevated counterfeit risk.

    Triggers when ALL of:
      - detected_type or description matches a high-counterfeit-risk category
      - source URL is a marketplace domain OR vendor is not authorized
    """
    dtype = (getattr(specs, "detected_type", "") or specs.description or "").lower()
    is_risky_category = any(cat in dtype for cat in _HIGH_COUNTERFEIT_RISK_CATEGORIES)
    if not is_risky_category:
        return False

    try:
        from urllib.parse import urlparse
        hostname = (urlparse(url.lower()).hostname or "").replace("www.", "")
    except Exception:
        hostname = ""

    is_marketplace  = any(dom in hostname for dom in _MARKETPLACE_DOMAINS)
    is_unauthorized = vendor_authorization_status not in ("Authorized",)

    return is_marketplace or is_unauthorized


# ---------------------------------------------------------------------------
# Confidence floor (new)
# ---------------------------------------------------------------------------

def _apply_confidence_floor(options: list,
                             threshold: float = TIER_SURFACE_MIN_CONFIDENCE) -> list:
    """Reject options whose confidence_score is below the surface threshold.

    Sets rejection_reason="confidence_below_floor" so the option remains in the list
    and appears in vendors_considered in the audit log, but is excluded from the UI.
    Does not override an earlier rejection_reason.
    """
    for o in options:
        if getattr(o, "rejection_reason", None):
            continue
        cs = getattr(o, "confidence_score", 0.0)
        if cs < threshold:
            o.rejection_reason = "confidence_below_floor"
            print(
                f"[Sourcing] Rejected (confidence_below_floor): {o.vendor_name} "
                f"confidence={cs:.1f}% < {threshold:.0f}% floor"
            )
    return options


# ---------------------------------------------------------------------------
# Category mismatch guard (new)
# ---------------------------------------------------------------------------

def _apply_category_mismatch_guard(options: list, specs=None) -> list:
    """When sourcing a Part and a price is >5x the peer median, suspect wrong product category.

    A $2,621 result alongside a $53 median strongly indicates the candidate is a full
    equipment unit, not the replacement part being sourced. Sets
    rejection_reason="category_mismatch_suspected". Does not override earlier rejections.
    """
    if not specs or getattr(specs, "category", "Part") != "Part":
        return options

    priced = [
        (o, getattr(o, "base_price", 0.0))
        for o in options
        if not getattr(o, "price_tbd", False)
        and not getattr(o, "rejection_reason", None)
        and getattr(o, "base_price", 0.0) > 0
    ]

    if len(priced) < 2:
        return options

    all_prices   = [p for _, p in priced]
    median_price = _stats.median(all_prices)

    for opt, price in priced:
        if price > 5 * median_price:
            opt.rejection_reason = "category_mismatch_suspected"
            print(
                f"[Sourcing] Rejected (category_mismatch_suspected): {opt.vendor_name} "
                f"@ ${price:.2f} is {price / median_price:.1f}x peer median ${median_price:.2f}"
            )
    return options


# ---------------------------------------------------------------------------
# Warranty gate (2.2)
# ---------------------------------------------------------------------------

def _apply_warranty_filter(
    specs, options: list
) -> tuple[list, Optional[str]]:
    """Filter vendor results based on AssetSpecs.warranty_status.

    Returns (filtered_options, banner_message_or_None).

    in_warranty     -> Exact OEM only; if none found, returns empty list + error message.
    out_of_warranty |
    warranty_waived -> No filter applied.
    unknown / None  -> All results surfaced, but a warning banner is returned.
    """
    warranty = (getattr(specs, "warranty_status", None) or "").lower().strip()

    if warranty == "in_warranty":
        oem = [o for o in options if getattr(o, "match_type", "") == "Exact OEM"]
        if not oem:
            msg = (
                "Equipment is in warranty — only OEM parts are recommended. "
                "No OEM listings found. Consider Tier 3 RFQ to OEM direct."
            )
            return [], msg
        return oem, None

    if warranty in ("out_of_warranty", "warranty_waived"):
        return options, None

    if warranty and warranty not in ("unknown", "none", ""):
        return options, None

    banner = None
    has_interchange = any(
        getattr(o, "match_type", "") in ("Aftermarket Compatible", "Functional Alternative")
        for o in options
    )
    if has_interchange:
        banner = (
            "Warranty status unknown — interchange parts may void OEM warranty. "
            "Confirm warranty status with the asset owner before purchase."
        )
    return options, banner


# ---------------------------------------------------------------------------
# Registry enrichment (2.4)
# ---------------------------------------------------------------------------

def _apply_registry_enrichment(options: list) -> None:
    """Look up each vendor in the supplier registry and update onboarding state.

    Mutates options in place. Unknown vendors are auto-registered as discovery_only.
    Silent on errors so registry issues never break the sourcing pipeline.
    """
    try:
        from utils.supplier_registry import enrich_option
        for opt in options:
            enrich_option(opt)
    except Exception as exc:
        print(f"[Sourcing] Registry enrichment error (non-fatal): {exc}")
