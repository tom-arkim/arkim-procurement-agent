"""
Comparison helpers for SpecComparisonAgent — dimensional, material, and categorical
comparators.

All functions are pure (no I/O, no API calls). The constants are intentionally
small — only the material families and unit conversions that appear in the
real test cases (mechanical seals, motors, sensors).
"""

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Material compatibility table
# Keys are normalized material names; values are the set of compatible materials.
# "exact" = same normalized key; "compatible" = in the same group; "different" = disjoint.
# ---------------------------------------------------------------------------

_MM_PER_INCH = 25.4

# Each group is a frozenset of equivalent/compatible names.
_MATERIAL_GROUPS: list[frozenset] = [
    frozenset({"epdm"}),
    frozenset({"viton", "fkm"}),
    frozenset({"buna-n", "nitrile", "buna", "buna n"}),
    frozenset({"ptfe", "teflon"}),
    frozenset({"carbon"}),
    frozenset({"silicon carbide", "sic"}),
    frozenset({"ceramic", "alumina"}),
    frozenset({"stainless steel", "ss304", "ss316", "316ss", "304ss"}),
    frozenset({"cast iron", "grey iron"}),
]

# Build lookup: normalized_name → group_index
_MATERIAL_INDEX: dict[str, int] = {}
for _idx, _group in enumerate(_MATERIAL_GROUPS):
    for _name in _group:
        _MATERIAL_INDEX[_name] = _idx


def _normalize_material(value: str) -> str:
    return value.lower().strip().replace("_", " ").replace("-", " ").replace("  ", " ")


# ---------------------------------------------------------------------------
# Motor frame equivalence table (NEMA T-frame ≈ NEMA U-frame compat rules)
# ---------------------------------------------------------------------------

_FRAME_ALIASES: dict[str, str] = {
    # T-frame: 143T and 145T both → same 1-2HP frame family
    "143t": "143-145t",
    "145t": "143-145t",
    "182t": "182-184t",
    "184t": "182-184t",
    "213t": "213-215t",
    "215t": "213-215t",
    "254t": "254-256t",
    "256t": "254-256t",
    "284t": "284-286t",
    "286t": "284-286t",
    "324t": "324-326t",
    "326t": "324-326t",
    "364t": "364-365t",
    "365t": "364-365t",
    "404t": "404-405t",
    "405t": "404-405t",
    "444t": "444-445t",
    "445t": "444-445t",
    "447t": "447t",
}


# ---------------------------------------------------------------------------
# Dimensional parsing — returns value in inches
# ---------------------------------------------------------------------------

_FRAC_RE      = re.compile(r'^(\d+)-(\d+)/(\d+)$')         # e.g. "1-5/8"
_FRAC_ONLY_RE = re.compile(r'^(\d+)/(\d+)$')               # e.g. "5/8"
_DECIMAL_RE   = re.compile(r'^(\d+\.?\d*)$')               # e.g. "1.625"
_UNIT_SUFFIX  = re.compile(
    r'^([\d./\-]+)\s*(inch|in|"|mm|cm|m)?', re.IGNORECASE
)


def _parse_to_inches(value: str) -> Optional[float]:
    """Convert a dimensional string to inches. Returns None if unparseable."""
    v = value.strip()
    m = _UNIT_SUFFIX.match(v)
    if not m:
        return None
    num_str = m.group(1).strip()
    unit    = (m.group(2) or "inch").lower().replace('"', 'inch')

    # Resolve num_str to float
    f = _FRAC_RE.match(num_str)
    if f:
        inches = int(f.group(1)) + int(f.group(2)) / int(f.group(3))
    else:
        fo = _FRAC_ONLY_RE.match(num_str)
        if fo:
            inches = int(fo.group(1)) / int(fo.group(2))
        else:
            try:
                inches = float(num_str)
            except ValueError:
                return None

    if unit in ("mm",):
        return inches / _MM_PER_INCH
    if unit in ("cm",):
        return inches * 10.0 / _MM_PER_INCH
    if unit in ("m",):
        return inches * 1000.0 / _MM_PER_INCH
    return inches  # inches, in, ", or bare number treated as inches


# ---------------------------------------------------------------------------
# Public comparison functions
# ---------------------------------------------------------------------------

def compare_dimensional(
    asset_value: str,
    candidate_value: str,
    tolerance_pct: float = 1.0,
) -> str:
    """Compare two dimensional strings with unit conversion and tolerance.

    Returns "exact" | "compatible" | "different".

    Examples:
      compare_dimensional("1-5/8 inch", "1.625 inch") -> "exact"
      compare_dimensional("1-5/8 inch", "41.275 mm")  -> "exact"
      compare_dimensional("1-5/8 inch", "1.620 inch") -> "compatible"
      compare_dimensional("1-5/8 inch", "1.500 inch") -> "different"
    """
    a = _parse_to_inches(asset_value)
    b = _parse_to_inches(candidate_value)
    if a is None or b is None:
        return "different"
    if abs(a - b) < 1e-6:
        return "exact"
    pct = abs(a - b) / max(a, 1e-9) * 100.0
    if pct <= tolerance_pct:
        return "compatible"
    return "different"


def compare_material(asset_value: str, candidate_value: str) -> str:
    """Compare material specs using the compatibility table.

    Returns "exact" | "compatible" | "different".

    Examples:
      compare_material("EPDM", "EPDM")    -> "exact"
      compare_material("Buna-N", "Nitrile") -> "compatible"
      compare_material("EPDM", "Viton")   -> "different"
    """
    a = _normalize_material(asset_value)
    b = _normalize_material(candidate_value)
    if a == b:
        return "exact"
    a_idx = _MATERIAL_INDEX.get(a)
    b_idx = _MATERIAL_INDEX.get(b)
    if a_idx is not None and b_idx is not None and a_idx == b_idx:
        return "compatible"
    return "different"


def compare_categorical(asset_value: str, candidate_value: str) -> str:
    """Strict equality after whitespace/case normalization.

    Returns "exact" | "different".
    """
    a = asset_value.strip().upper().replace(" ", "").replace("-", "")
    b = candidate_value.strip().upper().replace(" ", "").replace("-", "")
    return "exact" if a == b else "different"


def compare_frame(asset_value: str, candidate_value: str) -> str:
    """Compare NEMA motor frames — treats T-paired frames as compatible.

    Returns "exact" | "compatible" | "different".
    """
    a = asset_value.strip().lower()
    b = candidate_value.strip().lower()
    if a == b:
        return "exact"
    a_family = _FRAME_ALIASES.get(a, a)
    b_family = _FRAME_ALIASES.get(b, b)
    if a_family == b_family:
        return "compatible"
    return "different"
