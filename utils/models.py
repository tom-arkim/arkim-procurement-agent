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
    # Freight magnitude classification (set by vision extraction)
    # "parcel" | "heavy_parcel" | "LTL_freight"
    physical_magnitude: Optional[str] = None
    # Motor-specific specs (extracted by vision module)
    rpm: Optional[str] = None
    # True when critical specs (Frame + RPM) are absent for a motor — triggers chat pause
    missing_critical_specs: bool = False
    # Phase 1 additions — maintenance & procurement lifecycle context
    asset_id: Optional[str] = None                 # unique asset identifier from CMMS/EAM
    diagnostic_event_id: Optional[str] = None      # originating work order / diagnostic event
    warranty_status: Optional[str] = None          # "In Warranty" | "Out of Warranty" | "Unknown"
    urgency_factor: float = 0.3                    # 1.0=emergency, 0.3=predictive, 0.0=stocking
    failure_mode: Optional[str] = None             # e.g. "Bearing Failure", "Seal Leak", "Overheating"
    # Dimensional / fit-critical specs (parts and seals)
    shaft_size: Optional[str] = None       # e.g. "1-5/8\"", "42mm"
    bore_diameter: Optional[str] = None    # bore or ID spec
    seal_face_size: Optional[str] = None   # for mechanical seals
    connection_size: Optional[str] = None  # for fittings, valves, flanged connections
    material_spec: Optional[str] = None    # e.g. "Viton", "EPDM", "Carbon/Silicon"
    # Manufacturer identification confidence: 0-100.
    # 90+: name explicitly visible in image or stated by user.
    # 60-79: inferred from recognizable PN prefix/pattern.
    # 30-59: guessed from partial information.
    # <30: unknown / unrecognizable.
    # Default 100 keeps backward-compatible behaviour for programmatically-built specs.
    manufacturer_confidence: int = 100
    # Set only when detected_type/category were corrected post manufacturer-confirmation.
    # Shape: {"original_manufacturer": str, "original_detected_type": str,
    #         "original_category": str, "corrected_manufacturer": str,
    #         "corrected_detected_type": str, "corrected_category": str}
    # Captured by dataclasses.asdict() and written to audit log automatically.
    classification_correction: Optional[dict] = None


@dataclass
class SourcingOption:
    vendor_name: str
    base_price: float
    lead_time_days: int
    reliability_score: float
    merchant_type: str          # "Enterprise" | "National Specialist" | "Direct Buy via Arkim" | "Quote Request" | "Local"
    requires_rfq: bool = False
    contact_email: Optional[str] = None
    notes: Optional[str] = None
    admin_fee: float = 0.0
    source_url: Optional[str] = None
    price_tbd: bool = False             # True when no price was found
    extracted_shipping_fee: Optional[float] = None  # from vendor page; 0 = Free Shipping
    is_freight: bool = False            # True if LTL/truck freight required
    match_type: str = "Exact OEM"        # "Exact OEM" | "Aftermarket Compatible" | "Functional Alternative"
    found_part_number: Optional[str] = None   # actual PN shown on vendor page
    shipping_terms: Optional[str] = None      # human-readable: "Free Shipping", "LTL Freight Required", "S.F.Q.", etc.
    is_collection_page: bool = False          # True when URL is a search/collection list, not a direct product page
    suitability_score: float = 0.0            # 0-100 fit score for Tier 2/3 vendors (PN + type + auth status)
    suitability_tier: str = ""                # "Gold" | "Silver" | "" — Arkim network tier
    warranty_terms: Optional[str] = None      # e.g., "12-month standard", "5-year limited"
    market_confidence_score: Optional[float] = None  # 1-10 web-sourced reliability score
    weight_lbs: Optional[float] = None        # item weight in lbs (for freight guard)
    # Phase 1 additions — vendor qualification & risk
    confidence_score: float = 0.0            # 0-100 epistemic certainty of identification
    counterfeit_risk_flag: bool = False       # True when category or marketplace signals high counterfeit risk
    vendor_authorization_status: str = "Unknown"  # "Authorized" | "Unauthorized" | "Unknown"
    onboarding_status: str = "Not Onboarded"      # "Active" | "Pending" | "Not Onboarded"
    limited_price_data: bool = False              # True when fewer than 4 peer prices available for sanity check
    export_control_classification: Optional[str] = None  # EAR/ITAR classification if applicable
    ship_from_country: Optional[str] = None       # ISO country code for origin (counterfeit risk signal)
    is_oem_direct: Optional[bool] = None          # True when vendor domain matches manufacturer (home field bonus)
    # Quality filter annotation — set by post-processing guards; non-None = excluded from UI
    # Values: "confidence_below_floor" | "price_outlier_extreme" | "category_mismatch_suspected"
    #         | "pn_mismatch"
    rejection_reason: Optional[str] = None
    # PN match status from Tier 2 LLM extraction (mirrors Tier 1's found_part_number logic)
    # Values: "exact_match" | "partial_match" | "no_match" | "not_visible" | None (Tier 1)
    pn_match_status: Optional[str] = None


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
    arkim_fee_rate_applied: Optional[float] = None  # Arkim processing fee rate used (e.g. 0.035)
    # Phase 1 additions — audit, compliance, lifecycle context
    tax_jurisdiction: Optional[str] = None             # e.g. "LA County, CA"
    tax_exemption_certificate_ref: Optional[str] = None  # certificate number if tax-exempt
    merchant_of_record: str = "Arkim Industrial Procurement Services"
    urgency_factor_used: float = 0.3                   # mirrors AssetSpecs.urgency_factor at quote time
    agent_version: str = "1.0.0-phase1"                # semantic version of the sourcing agent
    sourcing_run_id: Optional[str] = None              # UUID for audit trail correlation
    user_id: Optional[str] = None                      # operator who initiated the request


@dataclass
class ProcurementReport:
    asset_specs: AssetSpecs
    internal_inventory_hit: bool
    all_options: list
    recommended_quote: ArkimQuote
    internal_location: Optional[str] = None
    rfq_email_draft: Optional[str] = None
