"""
Component-aware sourcing query construction — Phase 2 plumbing (T5b).

When a part is a COMPONENT of a named parent machine (an ANCHORED part — e.g. a
mechanical seal for a "Goulds 3196" pump), the sourcing query must target the
COMPONENT for the parent — "mechanical seal for Goulds 3196" — and MUST NOT
degenerate to a bare parent query ("Goulds 3196") that would source the pump
itself instead of the seal.

This is the F1 fixture: specs carrying `part_type=mechanical_seal,
component_of="Goulds 3196"` -> a query string containing BOTH the component term
AND the parent identity, never the bare parent alone.

Pure + standalone + tested. The INTAKE_TYPE_AWARE gate is the caller's
responsibility (the sourcing query builders call `build_component_aware_query`
only under the flag, so flag-off sourcing is byte-identical to today).
"""

from __future__ import annotations

from typing import Optional

from utils.models import AssetSpecs


def build_component_aware_query(specs: AssetSpecs) -> Optional[str]:
    """Return a component-aware query phrase when `specs.component_of` is set,
    else None.

    Format: "<detected_type> for <component_of>" — e.g. "mechanical seal for
    Goulds 3196". Falls back to "<category> for <component_of>" when
    detected_type is absent, and to None when neither is present (the caller
    then uses the existing generic query). Never raises.
    """
    parent = (getattr(specs, "component_of", None) or "").strip()
    if not parent:
        return None
    component = (getattr(specs, "detected_type", None) or "").strip()
    if not component:
        component = (getattr(specs, "category", None) or "").strip().lower()
    if not component:
        return None
    return f"{component} for {parent}"


def is_bare_parent_query(query: str, parent: Optional[str]) -> bool:
    """True when `query` is a bare-parent query: it carries the parent identity
    but NOT the component term — i.e. it would source the parent machine itself
    rather than the component. The F1 anti-pattern: "Goulds 3196" alone.

    Used by tests to assert the bare-parent query is NOT produced. Conservative:
    only flags when the parent token is present AND no component term (detected
    from the registry's component vocabulary) appears.
    """
    if not parent or not query:
        return False
    q = query.lower()
    p = parent.lower().strip()
    if p not in q:
        return False
    # A component term present means the query is component-aware, not bare.
    _COMPONENT_TERMS = (
        "seal", "bearing", "valve", "gasket", "belt", "coupling", "filter",
        "impeller", "seal kit", "mechanical seal", "o-ring",
    )
    if any(t in q for t in _COMPONENT_TERMS):
        return False
    return True
