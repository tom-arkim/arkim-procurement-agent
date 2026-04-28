"""
Arkim Procure Agent — main.py
Orchestrates the full procurement pipeline and renders the terminal report.
"""

import sys
import os

# Ensure utils/ is importable
sys.path.insert(0, os.path.dirname(__file__))

from utils.vision    import extract_specs
from utils.inventory import check_internal
from utils.sourcing  import find_vendors
from utils.quoting   import generate_arkim_quote
from utils.models    import SourcingOption, ArkimQuote, ProcurementReport

# ── ANSI colour helpers ─────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BLUE   = "\033[94m"
GREY   = "\033[90m"
WHITE  = "\033[97m"

def c(text, *codes): return "".join(codes) + str(text) + RESET


# ── Report Renderer ─────────────────────────────────────────────────────────

def render_report(report: ProcurementReport, all_quotes: list[ArkimQuote]) -> None:

    sep  = "─" * 110
    dsep = "═" * 110

    print(f"\n{c(dsep, CYAN)}")
    print(c("  ▲  ARKIM PROCURE AGENT — PROCUREMENT RECOMMENDATION REPORT", BOLD, CYAN))
    print(c(dsep, CYAN))

    # ── Asset Specs ──
    s = report.asset_specs
    print(f"\n{c('ASSET IDENTIFIED', BOLD, WHITE)}")
    print(f"  Manufacturer : {c(s.manufacturer, YELLOW)}  │  Model   : {c(s.model, YELLOW)}")
    print(f"  Part Number  : {c(s.part_number, GREEN)}  │  Voltage : {s.voltage}  │  HP : {s.hp}")
    print(f"  Serial       : {s.serial_number}")
    print(f"  Description  : {GREY}{s.description}{RESET}")

    # ── Inventory Status ──
    print(f"\n{c('INTERNAL INVENTORY', BOLD, WHITE)}")
    if report.internal_inventory_hit:
        print(f"  {c('✅ IN STOCK', GREEN, BOLD)}  →  Location: {c(report.internal_location, YELLOW)}")
        print(f"  {c('Recommendation: Pull from shelf — zero sourcing cost.', GREEN)}")
    else:
        print(f"  {c('❌ NOT IN STOCK', RED, BOLD)} — Proceeding to external sourcing.")

    # ── Comparison Table ──
    print(f"\n{c('VENDOR COMPARISON TABLE', BOLD, WHITE)}")
    print(c(sep, GREY))
    header = (
        f"{'#':<3} {'Vendor':<28} {'Type':<12} "
        f"{'Vendor Price':>13} {'Shipping':>9} {'Arkim Price':>12} "
        f"{'Tax':>8} {'Grand Total':>12} "
        f"{'Lead (d)':>9} {'TCA Score':>10}  Mode"
    )
    print(c(header, BOLD))
    print(c(sep, GREY))

    for i, q in enumerate(all_quotes, 1):
        opt       = q.chosen_option
        is_best   = (i == 1)
        row_color = GREEN if is_best else (YELLOW if opt.merchant_type == "Enterprise" else RESET)
        rfq_tag   = c("[RFQ]", RED) if opt.requires_rfq else c("[API]", CYAN)
        star      = c("★ ", GREEN, BOLD) if is_best else "  "

        row = (
            f"{star}{i:<2} {opt.vendor_name:<28} {opt.merchant_type:<12} "
            f"${opt.base_price:>11.2f} ${q.shipping_cost:>7.2f} ${q.arkim_sale_price:>10.2f} "
            f"${q.tax_amount:>6.2f} ${q.grand_total:>10.2f} "
            f"{opt.lead_time_days:>9} {q.tca_score:>9.1f}  {rfq_tag}"
        )
        print(c(row, row_color))

    print(c(sep, GREY))
    print(f"  {c('★ = Recommended (highest TCA)', GREEN)}   {c('[RFQ] = Manual outreach required', RED)}")
    print(f"  {GREY}Grand Total includes Arkim markup + location sales tax{RESET}")

    # ── Final Sourcing Recommendation ──
    rq         = report.recommended_quote
    opt        = rq.chosen_option
    vendor_base = opt.base_price + rq.shipping_cost
    arkim_fee   = round(vendor_base * (rq.arkim_fee_rate_applied or 0.035), 2)

    print(f"\n{c(dsep, CYAN)}")
    print(c("  ▲  ARKIM SOURCING RECOMMENDATION (PENDING CLIENT APPROVAL)", BOLD, CYAN))
    print(c(dsep, CYAN))
    print(f"  Quote ID       : {c(rq.quote_id, BOLD, YELLOW)}")
    print(f"  Generated At   : {rq.generated_at.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Asset          : {s.manufacturer} {s.model} ({s.part_number})")
    print()
    print(f"  Preferred Vendor               : {c(opt.vendor_name, BOLD)}")
    print(f"  ── Cost Breakdown ──────────────────────────────────────")
    print(f"  Vendor Base Price              : ${opt.base_price:>10.2f}")
    print(f"  Shipping                       : ${rq.shipping_cost:>10.2f}")
    print(f"  Arkim Processing Fee (3.5%)    : ${arkim_fee:>10.2f}")
    print(f"  ────────────────────────────────────────────────────────")
    print(f"  Subtotal                       : ${rq.arkim_sale_price:>10.2f}")
    print(f"  Sales Tax ({rq.tax_rate*100:.2f}%)              : ${rq.tax_amount:>10.2f}")
    print(f"  ════════════════════════════════════════════════════════")
    print(f"  {c('GRAND TOTAL                    : ${:>10.2f}'.format(rq.grand_total), BOLD, GREEN)}")
    print(f"  ════════════════════════════════════════════════════════")
    print()
    print(f"  Est. Delivery      : {rq.estimated_delivery_days} business days")
    print(f"  AVL Status         : {c(rq.avl_bypass_label, BOLD, CYAN)}")
    print(f"  AVL Time Saved     : ~{rq.avl_time_saved_days} days (no vendor onboarding)")
    print(f"  TCA Score          : {rq.tca_score} / 100")
    print()
    print(c("  ✔  Client approves Arkim to proceed with quote request.", GREEN, BOLD))
    print(c(dsep, CYAN))

    # ── RFQ Email ──
    if report.rfq_email_draft:
        print(f"\n{c('OFFLINE RFQ EMAIL DRAFT (Tier-3 Outreach)', BOLD, WHITE)}")
        print(c(sep, GREY))
        print(GREY + report.rfq_email_draft + RESET)
        print(c(sep, GREY))

    print()


# ── Pipeline ────────────────────────────────────────────────────────────────

class SourcingEngine:
    """Main orchestrator — runs the full procurement pipeline."""

    def __init__(self, site: str = "La Mirada"):
        self.site = site

    def run(self, image_description: str) -> ProcurementReport:
        print(c("\n  ▲  ARKIM PROCURE AGENT — Starting Pipeline\n", BOLD, CYAN))

        # Module A: Vision
        specs = extract_specs(image_description)

        # Module B: Inventory
        found, location, _ = check_internal(specs)
        if found:
            # Still run sourcing for comparison, but we'll note the hit
            pass

        # Module C: Sourcing
        all_options, rfq_draft = find_vendors(specs, site=self.site)

        # Module D: Quote Generation
        all_quotes, best_quote = generate_arkim_quote(specs, all_options, site=self.site)

        report = ProcurementReport(
            asset_specs=specs,
            internal_inventory_hit=found,
            internal_location=location,
            all_options=all_options,
            recommended_quote=best_quote,
            rfq_email_draft=rfq_draft,
        )

        render_report(report, all_quotes)
        return report


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Test Case: AmeriGlide Hercules II 750 — 1/3 HP, 90 VDC Motor
    NAMEPLATE_IMAGE = (
        "AmeriGlide Hercules II 750 stair lift drive unit nameplate. "
        "Rated 1/3 HP, 90 VDC Motor. Serial Number: SN-AGL-2024-08812. "
        "Manufactured 2019. Class B Insulation. Duty: Continuous."
    )

    engine = SourcingEngine(site="La Mirada")
    report = engine.run(NAMEPLATE_IMAGE)
