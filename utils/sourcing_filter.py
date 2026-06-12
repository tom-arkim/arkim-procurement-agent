"""
utils/sourcing_filter.py
Exact-only sourcing filter — the backend half of the "find exact replacements only"
choice (the Request honesty branch: when equivalents can't be vetted, show only the
exact OEM part, not aftermarket cross-references).

Pure transform over the raw sourcing-results dict (tier_1/tier_2/tier_3, each
{results, count, status}). Tier 2/3 are filtered to candidates that are an exact OEM
match; Tier 1 (the Arkim network) is left intact — those are trusted, not aftermarket
cross-references. No I/O.
"""

from __future__ import annotations


def _is_exact(opt: dict) -> bool:
    """A candidate is an exact replacement when it's an Exact-OEM match (by match_type
    or by an exact part-number match), not an aftermarket/equivalent cross-reference."""
    return opt.get("match_type") == "Exact OEM" or opt.get("pn_match_status") == "exact_match"


def apply_exact_only(results: dict) -> dict:
    """Drop equivalent/aftermarket candidates from Tier 2/3, keeping only exact OEM
    matches. Tier 1 is left intact. Mutates and returns the same dict (tiers filtered,
    counts updated). Safe on missing/malformed tiers."""
    if not isinstance(results, dict):
        return results
    for label in ("tier_2", "tier_3"):
        tier = results.get(label)
        if not isinstance(tier, dict):
            continue
        kept = [o for o in (tier.get("results") or []) if _is_exact(o)]
        tier["results"] = kept
        tier["count"] = len(kept)
    return results
