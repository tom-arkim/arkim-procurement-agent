"""
Arkim Procure Agent — Data Models (stdlib dataclasses, no third-party deps)
"""

from dataclasses import dataclass
from typing import Optional
from datetime import datetime


@dataclass
class AssetSpecs:
    manufacturer: str
    model: str
    part_number: str
    voltage: str
    # "Part"  = replacement component (starter, VFD, bearing, seal, relay …)
    # "Equipment" = full assembled unit (pump, motor, compressor, blower …)
    category: str = "Part"
    hp: Optional[str] = None
    serial_number: Optional[str] = None
    description: Optional[str] = None
    raw_text: Optional[str] = None
    # Equipment-specific technical requirements
    gpm: Optional[str] = None           # flow rate  (pumps)
    psi: Optional[str] = None           # pressure   (pumps / compressors)
    frame: Optional[str] = None         # NEMA frame size (motors / pumps)
    phase: Optional[str] = None         # electrical phase (e.g. "3-phase", "single-phase")
    detected_type: Optional[str] = None # specific equipment type (e.g. "Vertical Multi-Stage Pump")
    # CapEx workflow extras
    use_case: Optional[str] = None      # application context (e.g. "cooling water recirculation")
    duty_cycle: Optional[str] = None    # "Continuous" | "Intermittent" | "Standby"
    budget_max: Optional[str] = None    # maximum budget for CapEx requests


@dataclass
class SourcingOption:
    vendor_name: str
    base_price: float
    lead_time_days: int
    reliability_score: float
    merchant_type: str          # "Enterprise" | "National Specialist" | "Direct Buy via Arkim" | "Local"
    requires_rfq: bool = False
    contact_email: Optional[str] = None
    notes: Optional[str] = None
    admin_fee: float = 0.0
    source_url: Optional[str] = None
    price_tbd: bool = False             # True when no price was found
    extracted_shipping_fee: Optional[float] = None  # from vendor page; 0 = Free Shipping
    is_freight: bool = False            # True if LTL/truck freight required
    match_type: str = "Exact"           # "Exact" | "Alternative" (PN match quality)
    found_part_number: Optional[str] = None   # actual PN shown on vendor page
    shipping_terms: Optional[str] = None      # human-readable: "Free Shipping", "LTL Freight Required", "S.F.Q.", etc.
    is_collection_page: bool = False          # True when URL is a search/collection list, not a direct product page
    suitability_score: float = 0.0            # 0-100 fit score for Tier 2/3 vendors (PN + type + auth status)
    partner_status: str = ""                  # "Gold" | "Silver" | "" — Arkim network tier
    warranty_terms: Optional[str] = None      # e.g., "12-month standard", "5-year limited"
    market_confidence_score: Optional[float] = None  # 1-10 web-sourced reliability score
    weight_lbs: Optional[float] = None        # item weight in lbs (for freight guard)


@dataclass
class ArkimQuote:
    quote_id: str
    generated_at: datetime
    asset_specs: AssetSpecs
    chosen_option: SourcingOption
    vendor_base_price: float
    arkim_markup_pct: float
    admin_fee: float
    shipping_cost: float
    arkim_sale_price: float      # (base + admin + shipping) * (1 + markup)
    tax_rate: float              # e.g. 0.095
    tax_amount: float            # arkim_sale_price * tax_rate
    grand_total: float           # arkim_sale_price + tax_amount
    estimated_delivery_days: int
    tca_score: float
    shipping_ltl: bool = False          # True when LTL freight cost is unknown
    shipping_label: str = ""            # display label: "Free Shipping", "LTL Freight Required", "S.F.Q.", "$40.00", etc.
    avl_bypass_label: str = "Direct Buy via Arkim"
    avl_time_saved_days: int = 14
    avl_friction_note: str = (
        "Client purchases via Arkim — no new vendor onboarding required."
    )
    tlv_score: float = 0.0             # Total Life Cycle Value (Purchase + Downtime Risk + Shipping + Tax)
    workflow: str = "spare_parts"      # "spare_parts" | "replacement" | "capex"
    labor_impact_cost: float = 0.0    # Projected labor cost for low-MCS spare parts ($200/hr × hours)


@dataclass
class ProcurementReport:
    asset_specs: AssetSpecs
    internal_inventory_hit: bool
    all_options: list
    recommended_quote: ArkimQuote
    internal_location: Optional[str] = None
    rfq_email_draft: Optional[str] = None
