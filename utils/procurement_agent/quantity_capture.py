"""
Quantity capture — Phase 1 of the intake redesign (gated behind INTAKE_TYPE_AWARE).

A lightweight, DETERMINISTIC quantity extractor (no LLM call) that reads a
stated quantity from the user's intake text ("I need 6 SKF 6205 bearings" -> 6)
and defaults to 1 with an internal `_quantity_assumed=true` marker when unstated.

Why deterministic regex and not the LLM: the extraction prompt must stay
byte-identical when INTAKE_TYPE_AWARE is off (guardrail 3 — flag off = current
behavior). A separate gated post-extraction step keeps the LLM path untouched
and is fully testable without mocking. Live-LLM quantity accuracy is out of
scope tonight (T9 measures classifier + extraction component-preservation, not
quantity).

The marker `_quantity_assumed` is `_`-prefixed so the existing filters in
`intake_agent._build_context_summary` and the api_server RunDetail serializer
strip it automatically — it never leaks to the extractor context or the
frontend specs display. The `quantity` field itself is NOT `_`-prefixed and
surfaces on RunDetail.asset_specs.

This module is pure (no I/O, no env read). The flag gate lives at the call site
in IntakeAgent.run() so this module can be unit-tested in isolation.
"""

from __future__ import annotations

import re
from typing import Tuple

# ---------------------------------------------------------------------------
# Stated-quantity patterns. Conservative by design: a false negative just falls
# to the safe default of 1; a false positive would mis-source a quantity. So we
# require an explicit quantity SIGNAL (a quantity verb, "of", "qty", an "Nx"
# multiplier, or "N pieces/pcs/units") rather than matching any leading digit —
# part numbers like "6205" must never be read as a quantity.
# ---------------------------------------------------------------------------

# A quantity verb followed by a number: "need 6", "I need 6", "order 6", "buy 6" ...
_VERB_QTY = re.compile(
    r"\b(?:i\s+)?(?:need|want|looking\s+for|require|requiring|order|ordering|"
    r"get|buy|purchase|source|sourcing)\s+(\d{1,4})\b",
    re.IGNORECASE,
)

# An explicit multiplier: "6x", "6 x", "6×" (but NOT a bare "6" — too ambiguous).
# Note: × (U+00D7) is a non-word char, so a trailing \b won't fire before a space;
# use a negative word-char lookahead instead so "6× bearings" and "6x bearings"
# both match, while "6xyz" does not.
_MULT_QTY = re.compile(r"\b(\d{1,4})\s*[x×](?!\w)", re.IGNORECASE)

# "qty 6", "qty: 6", "qty=6"
_QTY_LABEL = re.compile(r"\bqty(?:\s*[:=])?\s*(\d{1,4})\b", re.IGNORECASE)

# "quantity 6", "quantity: 6"
_QUANTITY_LABEL = re.compile(r"\bquantity(?:\s*[:=])?\s*(\d{1,4})\b", re.IGNORECASE)

# "6 of" — "I need 6 of those", "6 of these bearings"
_N_OF = re.compile(r"\b(\d{1,4})\s+of\b", re.IGNORECASE)

# "6 pieces", "6 pcs", "6 units"
_N_UNITS = re.compile(
    r"\b(\d{1,4})\s+(?:pieces|pcs|units|ea|each)\b",
    re.IGNORECASE,
)

_PATTERNS = (_VERB_QTY, _MULT_QTY, _QTY_LABEL, _QUANTITY_LABEL, _N_OF, _N_UNITS)

# Sanity ceiling — an absurd stated quantity is treated as unstated (default 1)
# rather than persisted. Avoids a typo like "need 999999" poisoning a run.
_MAX_PLAUSIBLE_QTY = 99_999


def extract_quantity(text: str) -> Tuple[int, bool]:
    """Return (quantity, assumed) for an intake message.

    - (N, False) when a quantity is explicitly stated in `text`.
    - (1, True)  when no quantity is stated (the safe default + assumed marker).

    Never raises. A None/empty/non-string input -> (1, True).
    """
    if not isinstance(text, str) or not text.strip():
        return 1, True
    for pat in _PATTERNS:
        m = pat.search(text)
        if m:
            try:
                qty = int(m.group(1))
            except (ValueError, IndexError):
                continue
            if 1 <= qty <= _MAX_PLAUSIBLE_QTY:
                return qty, False
    return 1, True


# ---------------------------------------------------------------------------
# Merge helper — applies quantity to a specs dict, preserving a prior REAL
# (non-assumed) quantity when the current turn states none.
# ---------------------------------------------------------------------------

_NULL_VALUES = {None, "", "null", "N/A", "Unknown", "none", "unknown"}


def apply_quantity(specs: dict, text: str) -> dict:
    """Mutate-and-return `specs` with quantity + _quantity_assumed under the rules:

    - Current turn states a quantity -> it wins (quantity=N, _quantity_assumed=False).
    - Current turn states none -> preserve a prior REAL quantity (assumed=False)
      if present; otherwise default to quantity=1, _quantity_assumed=True.

    `specs` is the merged specs dict (prior + this turn's extraction). Pure
    w.r.t. I/O — no network, no env. The INTAKE_TYPE_AWARE gate is the caller's
    responsibility; this function runs the logic unconditionally so it is
    unit-testable in isolation.
    """
    qty, assumed = extract_quantity(text)
    if not assumed:
        specs["quantity"] = qty
        specs["_quantity_assumed"] = False
        return specs
    # No quantity stated this turn.
    prior_qty = specs.get("quantity")
    prior_assumed = specs.get("_quantity_assumed")
    if prior_qty not in _NULL_VALUES and prior_assumed is False:
        # Keep the prior real quantity — don't clobber it with an assumed 1.
        return specs
    specs["quantity"] = 1
    specs["_quantity_assumed"] = True
    return specs
