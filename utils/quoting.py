"""
Module D — Merchant Logic & TCA Scoring
Applies markup, shipping, location-based tax, and ranks all options by Total Cost of Acquisition.
Generates the final Arkim-branded quote with full line-item breakdown.
"""

from datetime import datetime
from uuid import uuid4
from utils.models import AssetSpecs, SourcingOption, ArkimQuote


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENTERPRISE_MARKUP = 0.10   # 10%  — Tier 1 (Grainger, McMaster-Carr, MSC)
DISCOVERY_MARKUP  = 0.25   # 25%  — Tier 2 (Direct Buy via Arkim) and Tier 3 (Local / RFQ)

# TCA weights — must sum to 1.0
SPEED_WEIGHT       = 0.35
RELIABILITY_WEIGHT = 0.25
FRICTION_WEIGHT    = 0.20
COST_WEIGHT        = 0.20

MAX_LEAD_TIME = 14  # days — normalisation ceiling

DEFAULT_DOWNTIME_COST_PER_DAY = 500.0  # USD/day — override via generate_arkim_quote param


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

def estimate_shipping(option: SourcingOption) -> tuple[float, bool, str]:
    """Return (shipping_cost_usd, is_ltl_freight, shipping_label).

    Priority order:
      1. Extracted page fee (0 = Free Shipping).
      2. LLM-extracted shipping_terms (S.F.Q., LTL Freight Required, etc.).
      3. If is_freight flag is set → LTL, cost unknown.
      4. Standard fallback: $40 flat for Local/Discovery, 5% min $15 for Enterprise.
         Heavy items (HP > 10, Motor/Pump category) never get flat-rate estimates.
    """
    extracted = getattr(option, "extracted_shipping_fee", None)
    if extracted is not None:
        if extracted == 0.0:
            return (0.0, False, "Free Shipping")
        return (float(extracted), False, f"${extracted:,.2f}")

    # LLM-extracted shipping terms take precedence over inferred freight
    terms = getattr(option, "shipping_terms", None)
    if terms in ("LTL Freight Required", "S.F.Q.", "TBA - Freight"):
        return (0.0, True, terms)

    if getattr(option, "is_freight", False):
        label = terms or "LTL Freight Required"
        return (0.0, True, label)

    # Flat-rate fallback — only for non-heavy items
    if option.merchant_type in ("Local", "Direct Buy via Arkim"):
        return (40.00, False, "$40.00 (est.)")
    fee = max(15.00, round(option.base_price * 0.05, 2))
    return (fee, False, f"${fee:,.2f} (est.)")


# ---------------------------------------------------------------------------
# TCA Score  (two-pass — cost component requires grand_total context)
# ---------------------------------------------------------------------------

def _compute_tca_score(option: SourcingOption,
                       grand_total: float = None,
                       min_grand: float = None,
                       max_grand: float = None) -> float:
    """
    Score 0–100. Higher = better.
      Speed (35%)         : shorter lead time → higher score
      Reliability (25%)   : vendor reliability score
      Friction (20%)      : no RFQ = 100, RFQ = 50
      Cost Efficiency (20%): lower grand_total relative to peers → higher score
                             (neutral 100 on first pass when context is absent)
    """
    speed_score       = max(0.0, (MAX_LEAD_TIME - option.lead_time_days) / MAX_LEAD_TIME) * 100
    reliability_score = option.reliability_score
    friction_score    = 50.0 if option.requires_rfq else 100.0

    if grand_total is not None and max_grand is not None and max_grand != min_grand:
        cost_score = 100.0 * (max_grand - grand_total) / (max_grand - min_grand)
    else:
        cost_score = 100.0  # neutral when no cross-quote context yet

    return round(
        speed_score       * SPEED_WEIGHT
        + reliability_score * RELIABILITY_WEIGHT
        + friction_score    * FRICTION_WEIGHT
        + cost_score        * COST_WEIGHT,
        2,
    )


# ---------------------------------------------------------------------------
# Markup selection
# ---------------------------------------------------------------------------

def compute_tlv(quote: "ArkimQuote", downtime_cost_per_day: float = DEFAULT_DOWNTIME_COST_PER_DAY) -> float:
    """Total Life Cycle Value — lower is better.

    TLV = Purchase Price + (Estimated Downtime Cost × Reliability Risk) + Shipping + Tax

    Reliability Risk is the expected downtime exposure during lead time:
      risk = lead_time_days × (1 - effective_reliability)
      effective_reliability blends vendor reliability_score (0-100 %) with
      market_confidence_score (1-10) when available.
    """
    opt = quote.chosen_option
    rel = opt.reliability_score / 100.0
    mcs = getattr(opt, "market_confidence_score", None)
    if mcs is not None:
        effective_rel = (rel + mcs / 10.0) / 2.0
    else:
        effective_rel = rel

    risk_factor    = max(0.0, 1.0 - effective_rel)
    downtime_cost  = downtime_cost_per_day * quote.chosen_option.lead_time_days * risk_factor
    shipping       = quote.shipping_cost
    tax            = quote.tax_amount
    return round(quote.vendor_base_price + downtime_cost + shipping + tax, 2)


def _markup_for(option: SourcingOption) -> float:
    """10 % for Tier 1 Enterprise; 25 % for Tier 2 (Direct Buy via Arkim) and Local."""
    if option.merchant_type == "Enterprise":
        return ENTERPRISE_MARKUP
    return DISCOVERY_MARKUP


# ---------------------------------------------------------------------------
# Arkim Quote Builder
# ---------------------------------------------------------------------------

def _build_arkim_quote(specs: AssetSpecs, option: SourcingOption,
                       site: str = "La Mirada") -> ArkimQuote:
    markup_pct                    = _markup_for(option)
    admin_fee                     = option.admin_fee
    shipping_cost, is_ltl, s_label = estimate_shipping(option)
    tax_rate                      = get_tax_rate(site)

    # Line-item sequence (shipping excluded from basis when LTL — unknown cost)
    cost_basis       = option.base_price + admin_fee + shipping_cost
    arkim_sale_price = round(cost_basis * (1 + markup_pct), 2)
    tax_amount       = round(arkim_sale_price * tax_rate, 2)
    grand_total      = round(arkim_sale_price + tax_amount, 2)

    # Pass-1 TCA: cost component is neutral (no peer context yet)
    tca_score = _compute_tca_score(option)

    return ArkimQuote(
        quote_id=f"ARK-{uuid4().hex[:8].upper()}",
        generated_at=datetime.now(),
        asset_specs=specs,
        chosen_option=option,
        vendor_base_price=option.base_price,
        arkim_markup_pct=markup_pct * 100,
        admin_fee=admin_fee,
        shipping_cost=shipping_cost,
        arkim_sale_price=arkim_sale_price,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        grand_total=grand_total,
        estimated_delivery_days=option.lead_time_days + 1,
        tca_score=tca_score,
        shipping_ltl=is_ltl,
        shipping_label=s_label,
    )


def _build_arkim_quote_for_workflow(specs: AssetSpecs, option: SourcingOption,
                                    site: str, workflow: str,
                                    downtime_cost_per_day: float) -> ArkimQuote:
    """Wraps _build_arkim_quote and stamps workflow + TLV onto the result."""
    q = _build_arkim_quote(specs, option, site)
    q.workflow  = workflow
    q.tlv_score = compute_tlv(q, downtime_cost_per_day)
    return q


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_arkim_quote(specs: AssetSpecs,
                         options: list[SourcingOption],
                         site: str = "La Mirada",
                         workflow: str = "spare_parts",
                         downtime_cost_per_day: float = DEFAULT_DOWNTIME_COST_PER_DAY,
                         ) -> tuple[list[ArkimQuote], ArkimQuote]:
    """
    Two-pass scoring:
      Pass 1 — build all quotes (TCA cost component neutral).
      Pass 2 — recompute TCA with grand_total context; compute TLV for each quote.

    Ranking metric:
      spare_parts → TCA (speed + reliability + friction + cost)
      replacement → TLV (purchase price + downtime risk + shipping + tax)
      capex       → TLV

    Returns (all_quotes sorted by primary metric desc/asc, recommended_quote).
    """
    print(f"\n[Quote Engine] Scoring {len(options)} options via "
          f"{'TCA' if workflow == 'spare_parts' else 'TLV'} (site: {site}, workflow: {workflow})...")

    # Pass 1
    all_quotes = [_build_arkim_quote_for_workflow(specs, opt, site, workflow, downtime_cost_per_day)
                  for opt in options]

    # Pass 2 — inject grand_total context into TCA
    grand_totals = [q.grand_total for q in all_quotes]
    min_gt = min(grand_totals)
    max_gt = max(grand_totals)
    for q in all_quotes:
        q.tca_score = _compute_tca_score(q.chosen_option, q.grand_total, min_gt, max_gt)
        q.tlv_score = compute_tlv(q, downtime_cost_per_day)

    if workflow == "spare_parts":
        all_quotes.sort(key=lambda q: q.tca_score, reverse=True)   # higher TCA = better
    else:
        all_quotes.sort(key=lambda q: q.tlv_score)                  # lower TLV  = better

    best = all_quotes[0]
    metric_str = (f"TCA: {best.tca_score:.1f}" if workflow == "spare_parts"
                  else f"TLV: ${best.tlv_score:,.2f}")
    print(
        f"[Quote Engine] Recommended: {best.chosen_option.vendor_name} "
        f"| {metric_str} "
        f"| Arkim Price: ${best.arkim_sale_price:.2f} "
        f"| Grand Total: ${best.grand_total:.2f}"
        + (" +LTL freight" if best.shipping_ltl else "")
    )

    return all_quotes, best
