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
        estimated_delivery_days=option.lead_time_days + 1,  # +1 for Arkim processing
        tca_score=tca_score,
        shipping_ltl=is_ltl,
        shipping_label=s_label,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_arkim_quote(specs: AssetSpecs,
                         options: list[SourcingOption],
                         site: str = "La Mirada") -> tuple[list[ArkimQuote], ArkimQuote]:
    """
    Two-pass scoring:
      Pass 1 — build all quotes with line-item totals (TCA cost component neutral).
      Pass 2 — recompute TCA using each quote's grand_total relative to peers.

    Returns (all_quotes sorted by tca_score desc, recommended_quote).
    """
    print(f"\n[Quote Engine] Scoring {len(options)} sourcing options via TCA (site: {site})...")

    # Pass 1
    all_quotes = [_build_arkim_quote(specs, opt, site) for opt in options]

    # Pass 2 — inject grand_total context into TCA
    grand_totals = [q.grand_total for q in all_quotes]
    min_gt = min(grand_totals)
    max_gt = max(grand_totals)
    for q in all_quotes:
        q.tca_score = _compute_tca_score(q.chosen_option, q.grand_total, min_gt, max_gt)

    all_quotes.sort(key=lambda q: q.tca_score, reverse=True)
    best = all_quotes[0]

    print(
        f"[Quote Engine] Recommended: {best.chosen_option.vendor_name} "
        f"| TCA: {best.tca_score} "
        f"| Arkim Price: ${best.arkim_sale_price:.2f} "
        f"| Tax: ${best.tax_amount:.2f} "
        f"| Grand Total: ${best.grand_total:.2f}"
        + (" +LTL freight" if best.shipping_ltl else "")
    )

    return all_quotes, best
