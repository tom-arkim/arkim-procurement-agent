"""
utils/sourcing/price_sanity.py
Price sanity checking: tiered thresholds, N>=4 guard, single-price market reference,
and extreme outlier filter (N>=2, 10x peer median).
"""

import re
import statistics as _stats


def _sanity_threshold(avg_price: float) -> float:
    """Return the minimum acceptable price as a fraction of the peer average.

    Tier       Avg price range    Max deviation allowed
    Low-cost   < $100             80 % below avg  -> threshold = avg x 0.20
    Mid-range  $100 - $5 000      70 % below avg  -> threshold = avg x 0.30
    High-value > $5 000           50 % below avg  -> threshold = avg x 0.50
    """
    if avg_price < 100:
        return avg_price * 0.20
    if avg_price <= 5000:
        return avg_price * 0.30
    return avg_price * 0.50


def _apply_price_sanity(items: list[dict], specs=None) -> list[dict]:
    """Strip prices that fall below a tier-based threshold vs. the peer average.

    Thresholds (via _sanity_threshold):
      < $100    -> reject if < 20% of avg
      $100-$5K  -> reject if < 30% of avg
      > $5K     -> reject if < 50% of avg

    N>=4 guard: when fewer than 4 peer prices exist, set limited_price_data=True on
    all items — sanity check still runs but callers should treat results with caution.

    Single-price case: validate against a market-reference Tavily search.
    Flagged items -> price stripped, price_sanity_flagged=True, suitability zeroed later.
    """
    import utils.sourcing as _pkg

    prices = [float(it["price"]) for it in items
              if it.get("price") is not None and float(it.get("price", 0)) > 0]

    if len(prices) == 0:
        return items

    limited = len(prices) < 4
    if limited:
        for it in items:
            it["limited_price_data"] = True
        print(f"[Sourcing] Price sanity: only {len(prices)} peer price(s) — limited_price_data flagged")

    if len(prices) == 1 and specs is not None and _pkg._tavily:
        ref_q = (f"{specs.manufacturer} {specs.model} {specs.part_number} "
                 f"price market distributor").strip()
        print(f"[Sourcing] Single-price market reference lookup: {ref_q!r}")
        try:
            resp = _pkg._tavily.search(query=ref_q, search_depth="basic", max_results=3)
            ref_prices: list[float] = []
            for r in resp.get("results", []):
                text = r.get("title", "") + " " + r.get("content", "")
                for m in re.finditer(r"\$\s*([\d,]+(?:\.\d{2})?)", text):
                    p = float(m.group(1).replace(",", ""))
                    if p > 10:
                        ref_prices.append(p)
            if ref_prices:
                ref_avg   = sum(ref_prices) / len(ref_prices)
                threshold = _sanity_threshold(ref_avg)
                if prices[0] < threshold:
                    for item in items:
                        p = item.get("price")
                        if p is not None and float(p) > 0:
                            print(f"[Sourcing] Single-price sanity FAIL: "
                                  f"{item.get('vendor','?')} @ ${float(p):.2f} "
                                  f"vs market ref ${ref_avg:.2f} — stripping price")
                            item["price"]                = None
                            item["price_sanity_flagged"] = True
        except Exception as exc:
            print(f"[Sourcing] Market reference lookup error: {exc}")
        return items

    avg       = sum(prices) / len(prices)
    threshold = _sanity_threshold(avg)

    for item in items:
        p = item.get("price")
        if p is not None and float(p) > 0 and float(p) < threshold:
            print(f"[Sourcing] Price sanity FAIL: {item.get('vendor','?')} "
                  f"@ ${float(p):.2f} vs peer avg ${avg:.2f} (threshold ${threshold:.2f}) — stripping price")
            item["price"]                = None
            item["price_sanity_flagged"] = True
    return items


def _apply_extreme_price_filter(options: list) -> list:
    """Hard-reject options whose price is >10x the peer median at N>=2 priced options.

    Catches category-mismatch results (e.g. a full pump at $2,621 when searching for a
    $53 seal). Sets rejection_reason="price_outlier_extreme" — the option remains in the
    list so vendors_considered in the audit log captures the rejection reason.

    Distinct from _apply_price_sanity which operates on raw parsed dicts and catches
    suspiciously LOW prices. This operates on SourcingOption objects and catches extreme
    HIGH prices.
    """
    priced = [
        (o, getattr(o, "base_price", 0.0))
        for o in options
        if not getattr(o, "price_tbd", False)
        and not getattr(o, "rejection_reason", None)
        and getattr(o, "base_price", 0.0) > 0
    ]

    if len(priced) < 2:
        return options

    for opt, price in priced:
        others = [p for o, p in priced if o is not opt]
        if not others:
            continue
        median_others = _stats.median(others)
        if median_others > 0 and price > 10 * median_others:
            opt.rejection_reason = "price_outlier_extreme"
            print(
                f"[Sourcing] Rejected (price_outlier_extreme): {opt.vendor_name} "
                f"@ ${price:.2f} is {price / median_others:.1f}x peer median ${median_others:.2f}"
            )
    return options
