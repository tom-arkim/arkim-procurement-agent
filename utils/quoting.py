"""
Module D — Merchant Logic & TCA Scoring
Applies a flat Arkim processing fee, shipping, location-based tax, and ranks
all options by Total Cost of Acquisition.
Generates the final Arkim-branded quote with full line-item breakdown.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4
from utils.models import AssetSpecs, SourcingOption, ArkimQuote


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Single flat processing fee applied to (vendor_base + shipping).
# Replaces the previous tiered markup model (10% Enterprise / 25% Discovery).
ARKIM_PROCESSING_FEE_RATE = 0.035   # 3.5% of (vendor base + shipping)

# TCA weights — must sum to 1.0
SPEED_WEIGHT       = 0.35
RELIABILITY_WEIGHT = 0.25
FRICTION_WEIGHT    = 0.20
COST_WEIGHT        = 0.20

MAX_LEAD_TIME = 14  # days — normalisation ceiling

DEFAULT_DOWNTIME_COST_PER_DAY = 500.0  # USD/day — override via generate_arkim_quote param
LABOR_RATE_PER_HOUR           = 200.0  # USD/hr — labor cost for low-MCS spare parts
MCS_LABOR_THRESHOLD           = 5.0    # market_confidence_score < this triggers labor surcharge


# ---------------------------------------------------------------------------
# Location-Based Tax
# ---------------------------------------------------------------------------

_TAX_RATES: dict[str, float] = {
    "La Mirada": 0.0950,   # Los Angeles County, CA
    "Vista":     0.0775,   # San Diego County, CA
}
_DEFAULT_TAX = 0.0875      # CA average fallback


def get_tax_rate(site: str) -> float:
    return _TAX_RATES.get(site, _DEFAULT_TAX)


# ---------------------------------------------------------------------------
# Dynamic Shipping Estimate
# ---------------------------------------------------------------------------

def estimate_shipping(option: SourcingOption,
                      specs: "AssetSpecs" = None) -> tuple[float, bool, str]:
    """Return (shipping_cost_usd, is_ltl_freight, shipping_label).

    Freight guard takes absolute priority — if the item is classified LTL_freight,
    flagged as freight (is_freight=True), or weighs >100 lbs, the cost is always
    0.0 and the label is always S.F.Q. The flat-rate fallback is strictly unreachable
    for any of these conditions.

    Priority order for non-freight items:
      1. Extracted page fee (0 = Free Shipping, >0 = stated fee).
      2. LLM-extracted shipping_terms token.
      3. Flat-rate fallback ($40 Local/Quote Request; 5% min $15 Enterprise/Specialist).
    """
    # ── Magnitude guard: physical_magnitude classification takes first priority ─
    if specs is not None and getattr(specs, "physical_magnitude", None) == "LTL_freight":
        return (0.0, True, "S.F.Q.")

    # ── Weight guard: >100 lbs is always freight ──────────────────────────────
    _weight = getattr(option, "weight_lbs", None)
    if _weight is not None and _weight > 100:
        return (0.0, True, "S.F.Q.")

    # ── Freight guard: absolute priority — zero cost, always S.F.Q. ──────────
    if getattr(option, "is_freight", False):
        terms = getattr(option, "shipping_terms", None)
        label = terms if terms in ("LTL Freight Required", "S.F.Q.", "TBA - Freight") else "S.F.Q."
        return (0.0, True, label)

    # ── Extracted page fee (non-freight items only) ───────────────────────────
    extracted = getattr(option, "extracted_shipping_fee", None)
    if extracted is not None:
        if extracted == 0.0:
            return (0.0, False, "Free Shipping")
        return (float(extracted), False, f"${extracted:,.2f}")

    # ── LLM-extracted shipping term ───────────────────────────────────────────
    terms = getattr(option, "shipping_terms", None)
    if terms in ("LTL Freight Required", "S.F.Q.", "TBA - Freight"):
        return (0.0, True, terms)

    # ── Flat-rate fallback (unreachable for any freight-flagged item) ─────────
    if option.merchant_type in ("Local", "Direct Buy via Arkim", "Quote Request"):
        return (40.00, False, "$40.00 (est.)")
    fee = max(15.00, round(option.base_price * 0.05, 2))
    return (fee, False, f"${fee:,.2f} (est.)")


# ---------------------------------------------------------------------------
# TCA Score  (two-pass — cost component requires grand_total context)
# ---------------------------------------------------------------------------

def _compute_tca_score(option: SourcingOption,
                       grand_total: float = None,
                       min_grand: float = None,
                       max_grand: float = None,
                       urgency_factor: float = 0.3) -> float:
    """
    Score 0–100. Higher = better.
      Speed (35%)          : shorter lead time → higher score
      Reliability (25%)    : vendor reliability score
      Friction (20%)       : no RFQ = 100, RFQ = 50
      Cost Efficiency (20%): lower grand_total relative to peers → higher score
                             (neutral 100 on first pass when context is absent)

    Emergency re-weighting (urgency_factor >= 0.8):
      Speed weight rises from 35% to 50%; Cost weight drops from 20% to 5%.
      This ensures fastest-delivery vendors rank higher in emergency mode,
      even at a cost premium.  The 15 pt delta is taken entirely from Cost
      so that Reliability and Friction weights are not distorted.
    """
    # Dynamic weight re-weighting for emergency sourcing
    if urgency_factor >= 0.8:
        speed_w = 0.50
        cost_w  = 0.05   # 35→50 (+15) offset by 20→5 (-15)
    else:
        speed_w = SPEED_WEIGHT
        cost_w  = COST_WEIGHT

    speed_score       = max(0.0, (MAX_LEAD_TIME - option.lead_time_days) / MAX_LEAD_TIME) * 100
    reliability_score = option.reliability_score
    friction_score    = 50.0 if option.requires_rfq else 100.0

    if grand_total is not None and max_grand is not None and max_grand != min_grand:
        cost_score = 100.0 * (max_grand - grand_total) / (max_grand - min_grand)
    else:
        cost_score = 100.0  # neutral when no cross-quote context yet

    return round(
        speed_score       * speed_w
        + reliability_score * RELIABILITY_WEIGHT
        + friction_score    * FRICTION_WEIGHT
        + cost_score        * cost_w,
        2,
    )


# ---------------------------------------------------------------------------
# TLV & Labor Impact
# ---------------------------------------------------------------------------

def compute_tlv(quote: "ArkimQuote",
                downtime_cost_per_day: float = DEFAULT_DOWNTIME_COST_PER_DAY,
                urgency_factor: float = 0.3) -> float:
    """Total Life Cycle Value — lower is better.

    TLV = Purchase Price + (Downtime_Cost × Lead_Days × Urgency_Factor × Reliability_Risk)
          + Shipping + Tax

    urgency_factor  : 0.0 = stocking (ignore downtime), 0.3 = predictive (default),
                      1.0 = emergency (full downtime cost applied)
    Reliability_Risk: (1 - effective_reliability) blending vendor reliability_score
                      with market_confidence_score when available.

    Defaults urgency_factor to 0.3 (predictive) when not explicitly set.
    """
    opt = quote.chosen_option

    rel = opt.reliability_score / 100.0
    mcs = getattr(opt, "market_confidence_score", None)
    effective_rel = (rel + mcs / 10.0) / 2.0 if mcs is not None else rel

    risk_factor   = max(0.0, 1.0 - effective_rel)
    downtime_cost = (
        downtime_cost_per_day
        * opt.lead_time_days
        * risk_factor
        * max(0.0, min(1.0, urgency_factor))
    )
    return round(
        quote.vendor_base_price + downtime_cost + quote.shipping_cost + quote.tax_amount,
        2,
    )


def _compute_labor_impact(option: SourcingOption) -> float:
    """Projected extra labor cost when market_confidence_score < MCS_LABOR_THRESHOLD.

    Each point below the threshold represents ~2 additional hours of replacement labour.
    Returns 0.0 for high-confidence parts or when MCS is unknown.
    """
    mcs = getattr(option, "market_confidence_score", None)
    if mcs is None or mcs >= MCS_LABOR_THRESHOLD:
        return 0.0
    labor_hours = (MCS_LABOR_THRESHOLD - mcs) * 2.0
    return round(labor_hours * LABOR_RATE_PER_HOUR, 2)


# ---------------------------------------------------------------------------
# Arkim Quote Builder — flat 3.5% processing fee model
#
# Pricing formula:
#   vendor_base  = vendor_quoted_price + shipping
#   arkim_fee    = vendor_base × ARKIM_PROCESSING_FEE_RATE
#   subtotal     = vendor_base + arkim_fee
#   tax          = subtotal × tax_rate
#   grand_total  = subtotal + tax
# ---------------------------------------------------------------------------

def _build_arkim_quote(specs: AssetSpecs, option: SourcingOption,
                       site: str = "La Mirada") -> ArkimQuote:
    shipping_cost, is_ltl, s_label = estimate_shipping(option, specs)
    tax_rate                       = get_tax_rate(site)

    vendor_base      = option.base_price + shipping_cost
    arkim_fee        = round(vendor_base * ARKIM_PROCESSING_FEE_RATE, 2)
    arkim_sale_price = round(vendor_base + arkim_fee, 2)   # subtotal before tax
    tax_amount       = round(arkim_sale_price * tax_rate, 2)
    grand_total      = round(arkim_sale_price + tax_amount, 2)

    tca_score = _compute_tca_score(option)

    return ArkimQuote(
        quote_id=f"ARK-{uuid4().hex[:8].upper()}",
        generated_at=datetime.now(),
        asset_specs=specs,
        chosen_option=option,
        vendor_base_price=option.base_price,
        arkim_markup_pct=ARKIM_PROCESSING_FEE_RATE * 100,  # 3.5 — stored for audit
        admin_fee=0.0,                                       # removed in flat-fee model
        shipping_cost=shipping_cost,
        arkim_sale_price=arkim_sale_price,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        grand_total=grand_total,
        estimated_delivery_days=option.lead_time_days + 1,
        tca_score=tca_score,
        shipping_ltl=is_ltl,
        shipping_label=s_label,
        arkim_fee_rate_applied=ARKIM_PROCESSING_FEE_RATE,
    )


def _build_arkim_quote_for_workflow(specs: AssetSpecs, option: SourcingOption,
                                    site: str, workflow: str,
                                    downtime_cost_per_day: float,
                                    urgency_factor: float = 0.3,
                                    sourcing_run_id: Optional[str] = None) -> ArkimQuote:
    """Wraps _build_arkim_quote and stamps workflow, TLV, urgency, and run ID."""
    q = _build_arkim_quote(specs, option, site)
    q.workflow             = workflow
    q.urgency_factor_used  = urgency_factor
    q.sourcing_run_id      = sourcing_run_id
    q.tlv_score            = compute_tlv(q, downtime_cost_per_day, urgency_factor)
    return q


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_arkim_quote(specs: AssetSpecs,
                         options: list[SourcingOption],
                         site: str = "La Mirada",
                         workflow: str = "spare_parts",
                         downtime_cost_per_day: float = DEFAULT_DOWNTIME_COST_PER_DAY,
                         urgency_factor: float = 0.3,
                         sourcing_run_id: Optional[str] = None,
                         ) -> tuple[list[ArkimQuote], ArkimQuote]:
    """
    Two-pass scoring:
      Pass 1 — build all quotes (TCA cost component neutral).
      Pass 2 — recompute TCA with grand_total context; compute TLV for each quote.

    Ranking metric:
      spare_parts → TCA (speed + reliability + friction + cost)
                    Emergency re-weighting applies when urgency_factor >= 0.8.
      replacement → TLV (purchase price + downtime risk × urgency_factor + shipping + tax)
      capex       → TLV

    Returns (all_quotes sorted by primary metric desc/asc, recommended_quote).
    """
    if urgency_factor is None:
        print(f"[Quote Engine] WARNING: urgency_factor not provided — defaulting to 0.3 (predictive)")
        urgency_factor = 0.3

    print(f"\n[Quote Engine] Scoring {len(options)} options via "
          f"{'TCA' if workflow == 'spare_parts' else 'TLV'} "
          f"(site: {site}, workflow: {workflow}, urgency: {urgency_factor:.1f})...")

    # Pass 1
    all_quotes = [
        _build_arkim_quote_for_workflow(
            specs, opt, site, workflow, downtime_cost_per_day, urgency_factor, sourcing_run_id
        )
        for opt in options
    ]

    # Pass 2 — inject grand_total context into TCA; add labor impact for spare_parts
    for q in all_quotes:
        q.labor_impact_cost = _compute_labor_impact(q.chosen_option) if workflow == "spare_parts" else 0.0

    effective_costs = [q.grand_total + q.labor_impact_cost for q in all_quotes]
    min_gt = min(effective_costs)
    max_gt = max(effective_costs)
    for q, eff in zip(all_quotes, effective_costs):
        q.tca_score = _compute_tca_score(q.chosen_option, eff, min_gt, max_gt, urgency_factor)
        q.tlv_score = compute_tlv(q, downtime_cost_per_day, urgency_factor)

    if workflow == "spare_parts":
        all_quotes.sort(key=lambda q: q.tca_score, reverse=True)
    else:
        all_quotes.sort(key=lambda q: q.tlv_score)

    best = all_quotes[0]
    metric_str = (f"TCA: {best.tca_score:.1f}" if workflow == "spare_parts"
                  else f"TLV: ${best.tlv_score:,.2f}")
    print(
        f"[Quote Engine] Recommended: {best.chosen_option.vendor_name} "
        f"| {metric_str} "
        f"| Subtotal: ${best.arkim_sale_price:.2f} "
        f"| Grand Total: ${best.grand_total:.2f}"
        + (" +LTL freight" if best.shipping_ltl else "")
    )

    return all_quotes, best
