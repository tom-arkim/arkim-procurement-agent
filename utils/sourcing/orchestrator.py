"""
utils/sourcing/orchestrator.py
Public entry point: find_vendors() orchestrates all tiers and post-processing.
"""

from typing import Optional

from utils.models import AssetSpecs, SourcingOption
from utils.sourcing.enterprise_search import _call_enterprise_api, _discover_national_specialists
from utils.sourcing.filtering import _apply_warranty_filter, _apply_registry_enrichment


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
    Returns: (all_options, warranty_banner_message_or_None)

    Post-processing steps (in order):
      1. Registry enrichment  — populate onboarding_status / vendor_authorization_status
      2. Warranty filter       — gate interchange results when asset is in-warranty
    The warranty_banner is returned for the UI to display; it is None when no warning needed.
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

    return filtered, warranty_banner
