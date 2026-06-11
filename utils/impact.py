"""
utils/impact.py
The "Your Arkim impact" calculation module — built TO the Time-Saved Calculator
Methodology. ONE place owns the arithmetic; the API exposes its output and the frontend
renders it. UI must NOT re-implement any of this math.

THREE TIERS, kept strictly separate (never blended in one number):

  1. SAVINGS — MEASURED. From the customer's OWN transactions only:
       - vs_last_paid: last_paid_price - chosen_price (their most recent purchase of the
         same manufacturer+part_number).
       - vs_highest_quote: highest_quote - chosen_price across quotes returned for THIS
         RFQ (needs >= 2 quotes — a single quote is not a comparator).
     No real comparator -> None (NEVER a fabricated 0). There is NO market/list-price
     input to this module — savings can never anchor to an external baseline.

  2. ACTION COUNTS — COUNTED. Literal counts from Arkim's own records (sent_messages /
     review_items / runs). Real numbers, not estimates.

  3. TIME SAVED — ESTIMATED. counts x a VERSIONED per-action config. Conservative (low
     end) by design — under-claiming is correct. The estimate is always returned
     SEPARATELY from the measured savings and labelled with its model version.

Auditability: every figure is drillable — savings carry their inputs, cumulative carries
contributing order ids and a per-month breakdown. Change the config (a new version)
without touching savings or the UI.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Tier 3 — VERSIONED estimate config (the ONLY place per-action minutes live).
# v1: conservative defaults from methodology §3.2. To revise, add a new version
# key (e.g. "v2") and bump ESTIMATE_MODEL_VERSION — never edit history in place,
# so past estimates remain reproducible/auditable.
# ---------------------------------------------------------------------------

ESTIMATE_MODEL_VERSION: str = "v1"

ESTIMATE_MODELS: dict[str, dict[str, int]] = {
    "v1": {
        "identify_part": 15,
        "contact_supplier": 10,
        "read_quote": 5,
        "compare_quotes": 10,
        "chase_nonresponder": 5,
    },
}

# Maps a counted action -> the config key whose minutes it consumes.
_COUNT_TO_CONFIG: dict[str, str] = {
    "parts_identified": "identify_part",
    "suppliers_contacted": "contact_supplier",
    "quotes_read": "read_quote",
    "comparisons_made": "compare_quotes",
    "replies_chased": "chase_nonresponder",
}

# Canonical zero counts — the shape every counts dict uses.
_ZERO_COUNTS: dict[str, int] = {k: 0 for k in _COUNT_TO_CONFIG}


# ---------------------------------------------------------------------------
# Tier 1 — SAVINGS (measured). Pure; customer figures only.
# ---------------------------------------------------------------------------

def compute_savings(
    chosen_price: Optional[float],
    last_paid_price: Optional[float],
    quotes: Optional[list[float]],
) -> tuple[Optional[float], Optional[str], dict]:
    """Measured saving for one decision, or (None, None, ...) when no real comparator
    exists. Prefers the firmest comparator — the customer's own most recent purchase —
    then the spread of quotes actually returned for this RFQ.

    Returns (saving, basis, inputs) where basis is "vs_last_paid" | "vs_highest_quote"
    | None and inputs is the audit trail of the figures used. saving may be negative
    (paid more than last time) — that is reported honestly, not hidden.

    INVARIANT: the only inputs are the customer's own chosen_price / last_paid_price /
    this-RFQ quotes. There is no market/baseline parameter, so no saving can ever be
    derived from an external rate.
    """
    highest_quote = max(quotes) if quotes else None
    inputs = {
        "chosen_price": chosen_price,
        "last_paid_price": last_paid_price,
        "highest_quote": highest_quote,
    }
    if chosen_price is None:
        return None, None, inputs
    # 1) vs the customer's own last paid price (firmest ground).
    if last_paid_price is not None:
        return last_paid_price - chosen_price, "vs_last_paid", inputs
    # 2) vs the highest quote returned for THIS rfq (needs a real alternative).
    if quotes is not None and len(quotes) >= 2:
        return highest_quote - chosen_price, "vs_highest_quote", inputs
    # No real comparator -> no figure (never a fabricated 0).
    return None, None, inputs


# ---------------------------------------------------------------------------
# Tier 3 — TIME SAVED (estimated). counts x versioned config.
# ---------------------------------------------------------------------------

def time_saved_minutes(counts: dict, version: str = ESTIMATE_MODEL_VERSION) -> int:
    """Σ(action_count × minutes_per_action) under the named estimate model. Conservative
    by config. Unknown count keys are ignored; missing keys count as 0."""
    cfg = ESTIMATE_MODELS[version]
    return sum(
        int(counts.get(count_key, 0) or 0) * cfg[cfg_key]
        for count_key, cfg_key in _COUNT_TO_CONFIG.items()
    )


def normalize_counts(counts: Optional[dict]) -> dict[str, int]:
    """Return a full counts dict (every action key present), real ints, no estimation."""
    out = dict(_ZERO_COUNTS)
    for k in _COUNT_TO_CONFIG:
        if counts and counts.get(k) is not None:
            out[k] = int(counts[k])
    return out


# ---------------------------------------------------------------------------
# Per-decision impact — the three tiers in one payload, kept separate.
# ---------------------------------------------------------------------------

def per_decision_impact(
    *,
    chosen_price: Optional[float],
    last_paid_price: Optional[float] = None,
    quotes: Optional[list[float]] = None,
    counts: Optional[dict] = None,
    version: str = ESTIMATE_MODEL_VERSION,
) -> dict:
    """One decision's impact: MEASURED saving (or None), COUNTED actions, and the
    labelled time ESTIMATE — never blended. `counts` is a real pass-through."""
    saving, basis, inputs = compute_savings(chosen_price, last_paid_price, quotes)
    counts_out = counts if counts is not None else dict(_ZERO_COUNTS)
    return {
        "saving": saving,                       # measured | None (no comparator)
        "saving_basis": basis,                  # vs_last_paid | vs_highest_quote | None
        "saving_inputs": inputs,                # audit: the figures the saving used
        "counts": counts_out,                   # counted, real
        "time_estimate_minutes": time_saved_minutes(counts_out, version),  # estimated
        "estimate_model_version": version,
    }


# ---------------------------------------------------------------------------
# Cumulative impact — over real orders in a period. Real months only; drillable.
# ---------------------------------------------------------------------------

def cumulative_impact(decisions: list[dict], *, version: str = ESTIMATE_MODEL_VERSION) -> dict:
    """Aggregate per-decision results over the customer's real orders.

    `decisions` items: {order_id, month ("YYYY-MM"), saving (float|None), saving_basis,
    counts}. Savings sum only the MEASURED ones (None contributes nothing — not a 0
    invented to fill a gap). The monthly trend contains ONLY months that have real
    orders — never interpolated; a month whose orders carry no measured saving stays a
    real 0. Every order id is returned so the UI can drill in.
    """
    total = 0.0
    contributing: list[str] = []
    summed = dict(_ZERO_COUNTS)
    by_month: dict[str, dict] = {}
    order_seen: list[str] = []  # preserve first-seen month order for a stable trend

    for d in decisions:
        oid = d.get("order_id")
        month = d.get("month")
        saving = d.get("saving")
        basis = d.get("saving_basis")
        counts = normalize_counts(d.get("counts"))

        for k in summed:
            summed[k] += counts[k]

        if month not in by_month:
            by_month[month] = {"month": month, "savings": 0.0, "order_ids": [],
                               "measured_count": 0}
            order_seen.append(month)
        bucket = by_month[month]
        if oid is not None:
            bucket["order_ids"].append(oid)

        if saving is not None and basis is not None:
            total += saving
            bucket["savings"] += saving
            bucket["measured_count"] += 1
            if oid is not None:
                contributing.append(oid)

    savings_by_month = []
    for month in order_seen:
        b = by_month[month]
        n_orders = len(b["order_ids"])
        n_measured = b["measured_count"]
        note = f"{n_measured} of {n_orders} order(s) with a measured saving"
        savings_by_month.append({
            "month": month,
            "savings": b["savings"],          # real (0 stays 0; never interpolated)
            "order_ids": b["order_ids"],      # drillable
            "note": note,
        })

    return {
        "total_savings": total,
        "savings_by_month": savings_by_month,
        "counts": summed,                                  # counted, summed
        "time_estimate_minutes": time_saved_minutes(summed, version),  # estimated
        "estimate_model_version": version,
        "contributing_order_ids": contributing,            # measured-saving orders, drillable
    }


# ---------------------------------------------------------------------------
# Gather layer — build calc inputs from the local stores (no external/paid calls).
# Thin and store-only: read orders/quotes/sent-messages, hand plain data to the pure
# calc above. The API calls these; tests exercise the pure functions directly.
# ---------------------------------------------------------------------------

_PURCHASED_STATUSES = ("placed", "confirmed", "shipped", "received")


def _last_paid_price(manufacturer: Optional[str], part_number: Optional[str],
                     exclude_run_id: Optional[str]) -> Optional[float]:
    """The customer's most recent prior PURCHASE price of this exact part (their own
    order history), or None when there's no prior purchase. Never a market price."""
    if not (manufacturer and part_number):
        return None
    from utils import orders as orders_store
    rows = orders_store.get_orders()  # newest-first
    for o in rows:
        if o.get("run_id") == exclude_run_id:
            continue
        if (o.get("manufacturer") == manufacturer and o.get("part_number") == part_number
                and o.get("status") in _PURCHASED_STATUSES and o.get("unit_price") is not None):
            return float(o["unit_price"])
    return None


def gather_run_decision(run_id: str) -> dict:
    """Assemble one decision's calc inputs for a run from the stores, then compute.

    chosen_price = the confirmed/selected quote (the rfq price the buyer took); quotes =
    unit prices of all quote review-items for this run; counts from sent_messages /
    review_items / run specs. Returns the per_decision_impact payload (+ run_id)."""
    from utils import supplier_registry

    review_items = supplier_registry.get_review_items(run_id=run_id)
    quote_items = [i for i in review_items if i.get("kind") == "quote"]
    quote_prices = [
        float((i.get("payload") or {}).get("unit_price"))
        for i in quote_items
        if (i.get("payload") or {}).get("unit_price") is not None
    ]
    # Chosen = a confirmed quote if one exists, else the lowest quote on the table.
    confirmed = [i for i in quote_items if i.get("status") == "confirmed"]
    chosen_item = confirmed[0] if confirmed else None
    chosen_price = None
    if chosen_item is not None:
        chosen_price = (chosen_item.get("payload") or {}).get("unit_price")
    elif quote_prices:
        chosen_price = min(quote_prices)
    chosen_price = float(chosen_price) if chosen_price is not None else None

    # Specs come from the persistence run record (where the API's runs live), not the
    # separate audit log. get_run returns asset_specs_json already decoded to a dict.
    from utils.procurement_agent.state import persistence
    run = persistence.get_run(run_id) or {}
    specs = run.get("asset_specs_json") or {}
    if isinstance(specs, str):
        import json
        try:
            specs = json.loads(specs or "{}")
        except (ValueError, TypeError):
            specs = {}
    manufacturer, part_number = specs.get("manufacturer"), specs.get("part_number")

    last_paid = _last_paid_price(manufacturer, part_number, exclude_run_id=run_id)

    sent = supplier_registry.get_sent_messages(run_id=run_id)
    counts = {
        "parts_identified": 1 if (manufacturer or part_number) else 0,
        "suppliers_contacted": len(sent),
        "quotes_read": len(quote_items),
        "comparisons_made": 1 if quote_items else 0,
        # No chase-action log exists yet -> a real 0 (counted, not estimated). When a
        # follow-up record lands, source it here.
        "replies_chased": 0,
    }

    out = per_decision_impact(chosen_price=chosen_price, last_paid_price=last_paid,
                              quotes=quote_prices or None, counts=counts)
    out["run_id"] = run_id
    return out


def gather_cumulative(version: str = ESTIMATE_MODEL_VERSION) -> dict:
    """Cumulative impact over the customer's REAL orders: one decision per run that
    produced a purchased order, aggregated by cumulative_impact. Month comes from the
    order's created_at; the per-run measured saving comes from gather_run_decision.

    NOTE (auditable simplification): last_paid is the customer's most recent purchase of
    the same part across runs — not strictly the purchase preceding this order's date.
    Refine here when historical temporal ordering matters; the savings stay measured
    from the customer's own orders only (never an external baseline)."""
    from utils import orders as orders_store

    purchased = [o for o in orders_store.get_orders() if o.get("status") in _PURCHASED_STATUSES]
    decisions: list[dict] = []
    seen_runs: set[str] = set()
    for o in purchased:
        rid = o.get("run_id")
        if not rid or rid in seen_runs:
            continue
        seen_runs.add(rid)
        dec = gather_run_decision(rid)
        decisions.append({
            "order_id": o.get("id"),
            "month": (o.get("created_at") or "")[:7],   # YYYY-MM (real order month)
            "saving": dec["saving"],
            "saving_basis": dec["saving_basis"],
            "counts": dec["counts"],
        })
    return cumulative_impact(decisions, version=version)
