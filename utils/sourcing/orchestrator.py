"""
utils/sourcing/orchestrator.py
Public entry point: find_vendors() orchestrates all tiers and post-processing.

Post-processing steps (in order):
  1. Registry enrichment       — populate onboarding_status / vendor_authorization_status
  2. Warranty filter            — gate interchange results when asset is in-warranty
  3. Aftermarket pass           — spec-based third-party discovery (Parts, non-in-warranty)
  4. Extreme price filter       — reject 10x+ price outliers at N>=2 (price_outlier_extreme)
  5. Category mismatch guard    — reject >5x peer median for Parts (category_mismatch_suspected)
  6. Confidence floor           — reject confidence_score < 50 (confidence_below_floor)

Filters 4-6 annotate rejected options with rejection_reason rather than removing them,
so vendors_considered in the audit log captures each option + rejection reason.
The UI skips options whose rejection_reason is set.
"""

from typing import Optional

from utils.models import AssetSpecs, SourcingOption
from utils.sourcing.enterprise_search import (
    _call_enterprise_api,
    _discover_national_specialists,
    _discover_aftermarket_specialists,
)
from utils.sourcing.filtering import (
    _apply_warranty_filter,
    _apply_registry_enrichment,
    _apply_confidence_floor,
    _apply_category_mismatch_guard,
)
from utils.sourcing.price_sanity import _apply_extreme_price_filter
from utils.sourcing.constants import TIER_SURFACE_MIN_CONFIDENCE


def find_vendors(specs: AssetSpecs,
                 site: str = "La Mirada",
                 force_refresh: bool = False,
                 search_mode: str = "exact",
                 workflow: str = "spare_parts") -> tuple[list[SourcingOption], Optional[str]]:
    """Run multi-tier sourcing and return (all_options, warranty_banner_or_None).

    all_options includes rejected options annotated with rejection_reason — callers
    must filter these out for display but should include them in audit log capture.
    """
    try:
        from utils.spec_lookup import enrich_equipment_specs
        specs = enrich_equipment_specs(specs)
    except Exception as _se:
        print(f"[Sourcing] Spec enrichment skipped: {_se}")

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

    all_options = enterprise + tier2

    _apply_registry_enrichment(all_options)

    filtered, warranty_banner = _apply_warranty_filter(specs, all_options)
    if warranty_banner:
        print(f"[Sourcing] Warranty banner: {warranty_banner}")

    # Aftermarket pass — spec-based third-party discovery (runs after warranty filter so
    # in-warranty assets are correctly gated; aftermarket options join filtered list)
    if workflow != "capex":
        print(f"\n[Sourcing] Aftermarket pass — spec-based third-party discovery...")
        aftermarket = _discover_aftermarket_specialists(specs, filtered)
        filtered = filtered + aftermarket

    # Quality filters — annotate rejected options with rejection_reason (do NOT remove).
    # Audit log captures all options; UI skips options with rejection_reason set.
    print(f"\n[Sourcing] Applying quality filters...")
    _apply_extreme_price_filter(filtered)
    _apply_category_mismatch_guard(filtered, specs)
    _apply_confidence_floor(filtered, TIER_SURFACE_MIN_CONFIDENCE)

    active   = sum(1 for o in filtered if not getattr(o, "rejection_reason", None))
    rejected = len(filtered) - active
    if rejected:
        print(f"[Sourcing] Quality filters: {rejected} option(s) rejected, {active} active")

    return filtered, warranty_banner
