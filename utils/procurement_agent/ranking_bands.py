"""
utils/procurement_agent/ranking_bands.py — Evidence-banded ranking (RANKING_BANDS_V1).

Design source: RANKING_BANDS_SPEC.md ("Honest Ranking"). The principle (spec §2):

    Rank by evidence band first. Onboarding is the tiebreaker WITHIN a band,
    never a ladder ACROSS bands. No candidate carries a score it didn't earn.
    A frozen cache never outlives an improved matcher.

Bands (spec §3), computed from candidate evidence at ranking time:
  A — CONFIRMED PART: exact/canonical found-PN match with a real source_url, or an
      onboarded supplier's explicit confirmation record for this request.
  B — PROBABLE FIT: real candidate-specific evidence short of confirmation
      (compatible/aftermarket found PN, part-referencing listing URL, verified scope).
  C — ASK-AND-SEE: capability inferred only (class-match, brand-intelligence seeds,
      capability pivots). Presented as outreach targets, never as findings.

Hard rule: Band A > Band B > Band C absolutely — no score, boost, or tier moves a
candidate above a higher band. Within a band: onboarded first, then the
evidence-quality score (spec §4), then the existing TCA order (inherited by stable
sort — callers pass TCA-ordered lists).

Everything here is behind the RANKING_BANDS_V1 env flag (read at call time, strict
truthy parse mirroring api_server._env_truthy). Flag OFF ⇒ no caller invokes this
module on the pipeline path and behavior is byte-identical to pre-band code.

This module is pure w.r.t. external services: no network, no DB writes; it only
annotates and reorders candidate dicts (annotate-don't-remove — list membership is
never changed, mirroring the reconcile/rank discipline in sourcing_agent.py).
"""

from __future__ import annotations

import os
from typing import Optional

# Band labels (string values are part of the flag-on API/capture contract).
BAND_A = "A"
BAND_B = "B"
BAND_C = "C"

_BAND_ORDER: dict[str, int] = {BAND_A: 0, BAND_B: 1, BAND_C: 2}

# Matcher-version stamp for cache invalidation (spec §6): bump when band assignment /
# PN-evidence classification changes materially. Vendor edges written under an older
# (or absent) version are stale hints, never final answers.
MATCHER_VERSION: int = 1

# A canonical-variant PN match requires the shared base to be a substantial PN —
# guards against a 2-3 char prefix coincidence claiming Band A.
_CANONICAL_MIN_BASE_LEN = 6

# Band C is not scored (spec §5): instead of a floor, C candidates are capped in
# count — top-N by scope strength (evidence quality), onboarded ALWAYS included
# (never counts against nor is cut by the cap).
BAND_C_CAP = 5

# Evidence-quality points (spec §4 — verifiable inputs only). 0-100 scale.
_EQ_PN_POINTS: dict[str, float] = {
    "exact": 40.0,       # found PN equals the requested PN (normalized)
    "canonical": 34.0,   # canonical family variant (base-PN prefix relationship)
    "compatible": 22.0,  # aftermarket/compatible PN found (e.g. 84004-28SP)
    "none": 0.0,
}
_EQ_URL_PRODUCT = 12.0        # real listing/source URL present
_EQ_URL_COLLECTION = 4.0      # URL present but flagged a collection/browse page
_EQ_PRICE_LISTED = 18.0       # real listed price (price_tbd False, base_price > 0)
_EQ_PRICE_QUOTED = 25.0       # supplier-confirmed quote price (strongest provenance)
_EQ_SCOPE_DECLARED = 8.0      # registry-declared scope (claimed > inferred, spec §4)
_EQ_SUIT_ORDERING_CAP = 10.0  # crude suitability as Band-B ordering input ONLY (spec §5)

# Confidence display cap: evidence-derived confidence never claims certainty.
_CONFIDENCE_CAP = 95.0


def _env_truthy(value: Optional[str]) -> bool:
    """Strict truthy parse (mirrors api_server._env_truthy / scoring._env_truthy):
    only 1/true/yes/on enable; everything else fails safe to False."""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def ranking_bands_active() -> bool:
    """True iff env RANKING_BANDS_V1 is truthy. Read at call time so tests can
    set/unset the env per case (same convention as _intake_type_aware)."""
    return _env_truthy(os.environ.get("RANKING_BANDS_V1"))


# ---------------------------------------------------------------------------
# PN evidence classification
# ---------------------------------------------------------------------------

def classify_pn_evidence(searched_pn: Optional[str], found_pn: Optional[str]) -> str:
    """Classify the found-PN evidence against the requested PN.

    Returns "exact" | "canonical" | "compatible" | "none".

      exact      — normalized equality (delimiter-agnostic).
      canonical  — canonical family variant: one normalized PN is a proper prefix of
                   the other and the shared base is a substantial PN. This is the
                   US-Seal case: found 84004-28 vs requested 84004-28-C238CBC (the
                   suffix is a configuration code on the same part identity).
      compatible — a real found PN that is neither exact nor canonical (the
                   aftermarket case: 84004-28SP for 84004-28-C238CBC).
      none       — no found PN (or no real searched PN to compare against).

    Pure string evidence; the caller composes this with the extractor's
    pn_match_status verdict (see pn_evidence_for).
    """
    from utils.procurement_agent.agents.sourcing_agent import normalize_part_number
    from utils.known_parts import _NULL_PN_TOKENS

    s = normalize_part_number(searched_pn or "")
    f = normalize_part_number(found_pn or "")
    if not s or s in _NULL_PN_TOKENS or not f or f in _NULL_PN_TOKENS:
        return "none"
    if f == s:
        return "exact"
    shorter, longer = (f, s) if len(f) < len(s) else (s, f)
    if len(shorter) >= _CANONICAL_MIN_BASE_LEN and longer.startswith(shorter):
        return "canonical"
    return "compatible"


def pn_evidence_for(candidate: dict, searched_pn: Optional[str]) -> str:
    """The candidate's effective PN-evidence level, composing string evidence with
    the extractor verdict:

      - pn_match_status == "no_match" (the extractor explicitly said the found PN is
        for a DIFFERENT part) overrides to "none" — a mismatched PN is not evidence.
      - Otherwise the string classification stands. An extractor "exact_match" that
        the string check cannot corroborate stays at its string level (conservative:
        an unverifiable claim never earns Band A on its own).
    """
    if candidate.get("pn_match_status") == "no_match":
        return "none"
    return classify_pn_evidence(searched_pn, candidate.get("found_part_number"))


# ---------------------------------------------------------------------------
# Candidate predicates
# ---------------------------------------------------------------------------

def is_onboarded(candidate: dict) -> bool:
    """True for an onboarded (Tier-1 relationship) supplier candidate.

    Registry-backed Tier 1 cards (tier1_matcher — only onboarded suppliers match
    under TIER1_V2) carry is_registry_backed=True; the legacy Tier 1 catalog path
    uses merchant_type "Arkim Network". Either marks the onboarding relationship.
    """
    return bool(candidate.get("is_registry_backed")) or \
        (candidate.get("merchant_type") == "Arkim Network")


def has_confirmation(candidate: dict) -> bool:
    """True when the candidate carries an explicit supplier confirmation record for
    this request (structured portal quote / parsed+confirmed email quote with
    part+price) — the spec §3 Band-A confirmation clause and the §3 band-mobility
    trigger. The quote-overlay path stamps quote_confirmed/supplier_confirmed."""
    return bool(candidate.get("quote_confirmed") or candidate.get("supplier_confirmed"))


def _has_real_url(candidate: dict) -> bool:
    return bool((candidate.get("source_url") or "").strip())


# ---------------------------------------------------------------------------
# Band assignment (spec §3)
# ---------------------------------------------------------------------------

def assign_band(candidate: dict, searched_pn: Optional[str]) -> str:
    """Assign the evidence band for one candidate. Pure; no mutation.

    Order of checks is load-bearing:
      1. Mock/seeded cards → Band C unconditionally (a fabricated candidate can
         never be a finding — the contract findings must satisfy; a "confirmation"
         on a mock would itself be fabricated).
      2. An explicit confirmation record → Band A (even for an otherwise Band-C
         onboarded class-match — this is the §3 band-mobility promotion).
      3. Capability pivots → Band C (inferred capability only).
      4. Exact/canonical found-PN + real URL → Band A.
      5. Compatible found-PN, exact/canonical PN without a URL, an extractor
         partial-match, or a part-referencing discovered listing → Band B.
      6. Everything else (registry class-match, URL-less seeds) → Band C.
    """
    if candidate.get("is_mock"):
        return BAND_C
    if has_confirmation(candidate):
        return BAND_A
    if candidate.get("search_type") == "capability_pivot":
        return BAND_C

    pn_ev = pn_evidence_for(candidate, searched_pn)
    real_url = _has_real_url(candidate)

    if pn_ev in ("exact", "canonical"):
        # Real URL (or a registry-backed identity, whose domain is the registry
        # record) confirms the listing is candidate-specific → CONFIRMED PART.
        if real_url:
            return BAND_A
        return BAND_B  # PN evidence without a verifiable listing: probable fit
    if pn_ev == "compatible":
        return BAND_B
    # No usable found-PN. An extractor partial/exact claim on a real listing, or a
    # discovered part-specific listing URL, is candidate-specific evidence → B.
    if real_url and candidate.get("pn_match_status") in ("exact_match", "partial_match"):
        return BAND_B
    if candidate.get("is_registry_backed"):
        # Onboarded class/category match only (today's DXP case) → ask-and-see.
        return BAND_C
    if not real_url:
        return BAND_C
    # Discovered listing URL from a part-specific search, no clean PN extract:
    # §3 Band B third clause. The Band-B floor (spec §5) keeps junk out.
    return BAND_B


# ---------------------------------------------------------------------------
# Evidence-quality score (spec §4) — earned, not asserted
# ---------------------------------------------------------------------------

def evidence_quality(candidate: dict, searched_pn: Optional[str],
                     band: Optional[str] = None) -> float:
    """0-100 score from verifiable inputs only: PN-match quality, URL/listing
    verification, price presence + provenance, scope evidence, supplier data
    quality. The crude keyword suitability_score participates ONLY as a Band-B
    ordering input, capped (spec §5 — its role reduced to Band-B ordering)."""
    band = band or assign_band(candidate, searched_pn)
    eq = 0.0

    eq += _EQ_PN_POINTS[pn_evidence_for(candidate, searched_pn)]

    if _has_real_url(candidate):
        eq += _EQ_URL_COLLECTION if candidate.get("is_collection_page") else _EQ_URL_PRODUCT

    if has_confirmation(candidate):
        eq += _EQ_PRICE_QUOTED
    elif not candidate.get("price_tbd") and float(candidate.get("base_price") or 0) > 0:
        eq += _EQ_PRICE_LISTED

    if candidate.get("is_registry_backed"):
        eq += _EQ_SCOPE_DECLARED  # registry-declared (claimed) scope beats inferred

    if band == BAND_B:
        # Crude suitability as a WITHIN-BAND-B ordering input only, tightly capped.
        crude = float(candidate.get("suitability_score") or 0.0)
        eq += min(max(crude, 0.0), 100.0) * (_EQ_SUIT_ORDERING_CAP / 100.0)

    return round(min(eq, 100.0), 1)


def confidence_from_evidence(candidate: dict, searched_pn: Optional[str],
                             band: Optional[str] = None,
                             eq: Optional[float] = None) -> float:
    """Evidence-derived confidence (spec §4, the C3 fix): 0 means "nothing
    verified" — and ONLY Band C is nothing-verified. Band A/B confidence scales
    with evidence quality and is strictly positive (they required evidence to
    earn the band)."""
    band = band or assign_band(candidate, searched_pn)
    if band == BAND_C:
        return 0.0
    if eq is None:
        eq = evidence_quality(candidate, searched_pn, band=band)
    # Band A/B always carry at least SOME verified evidence; floor above zero so
    # 0-confidence remains a Band-C-only statement (spec §9 criterion 8).
    return round(min(max(eq, 5.0), _CONFIDENCE_CAP), 1)


# ---------------------------------------------------------------------------
# Ordering (spec §3) — band absolute, onboarded-then-evidence within band
# ---------------------------------------------------------------------------

def banded_sort_key(candidate: dict) -> tuple:
    """Stable-sort key: (band, not-onboarded, -evidence_quality). Requires the
    candidate to already carry `band` and `evidence_quality` annotations (see
    annotate_candidate / apply_ranking_bands). TCA order is the final tiebreak by
    STABILITY — callers sort lists that are already TCA-ordered."""
    band = candidate.get("band") or BAND_C
    return (
        _BAND_ORDER.get(band, _BAND_ORDER[BAND_C]),
        0 if is_onboarded(candidate) else 1,
        -float(candidate.get("evidence_quality") or 0.0),
    )


def provenance_for(candidate: dict) -> str:
    """Human-readable evidence provenance (spec §4/§7): what a candidate's presence
    is based on — the ONLY thing a no-evidence candidate carries (no numbers)."""
    if candidate.get("is_mock"):
        return "Authorized distributor per brand intelligence"
    if candidate.get("search_type") == "capability_pivot":
        return "Capability discovery"
    if is_onboarded(candidate):
        return "Onboarded supplier — class match"
    if _has_real_url(candidate):
        return "Discovered listing"
    return "Discovered supplier"


def annotate_candidate(candidate: dict, searched_pn: Optional[str]) -> dict:
    """Attach band + evidence annotations in place (annotate-don't-remove):
    band, evidence_quality, provenance, banded=True — and make the scores honest
    (spec §4):

      - is_mock (seeded) candidates carry NO suitability and NO confidence number
        (the fabricated 88.0/75.0 die here) — only provenance + Band C.
      - every other candidate's confidence_score is REPLACED by the evidence-
        derived confidence (the C3 fix): 0 means nothing verified, and only
        Band C is nothing-verified.

    Returns the candidate."""
    band = assign_band(candidate, searched_pn)
    eq = evidence_quality(candidate, searched_pn, band=band)
    candidate["band"] = band
    candidate["evidence_quality"] = eq
    candidate["provenance"] = provenance_for(candidate)
    if candidate.get("is_mock"):
        candidate["suitability_score"] = None
        candidate["confidence_score"] = None
    else:
        candidate["confidence_score"] = confidence_from_evidence(
            candidate, searched_pn, band=band, eq=eq)
    candidate["banded"] = True
    return candidate


def order_banded(candidates: list[dict]) -> list[dict]:
    """Return candidates in banded order: Band A > B > C absolutely; within a band
    onboarded first, then evidence quality; prior (TCA) order breaks remaining
    ties via sort stability. Input candidates must be annotated."""
    return sorted(candidates, key=banded_sort_key)


# ---------------------------------------------------------------------------
# Findings vs outreach targets (spec §7) — answers are cards, leads are outreach
# ---------------------------------------------------------------------------

def banded_findings(result: dict) -> list[tuple[dict, int, int]]:
    """The FINDINGS of a banded result: Band A/B candidates without an active
    rejection, in banded order across ALL tiers. Returns (candidate, tier_number,
    index-within-raw-tier-list) so the API layer can build cards whose ids match
    the tier arrays. is_mock candidates are structurally Band C (assign_band) and
    therefore never findings — the §4 contract."""
    entries: list[tuple[dict, int, int]] = []
    for tier_key, n in (("tier_1", 1), ("tier_2", 2), ("tier_3", 3)):
        for i, c in enumerate((result.get(tier_key) or {}).get("results") or []):
            if c.get("rejection_reason"):
                continue
            if c.get("band") in (BAND_A, BAND_B):
                entries.append((c, n, i))
    entries.sort(key=lambda e: banded_sort_key(e[0]))
    return entries


def outreach_targets(result: dict) -> list[dict]:
    """The OUTREACH BLOCK of a banded result (spec §7): Band C candidates without
    an active rejection and within the count cap, onboarded FIRST (named as
    yours), then by scope strength (evidence quality). These are RFQ targets, not
    results — the caller must render provenance strings only, never numbers."""
    targets = [
        c
        for tier_key in ("tier_1", "tier_2", "tier_3")
        for c in ((result.get(tier_key) or {}).get("results") or [])
        if (c.get("band") == BAND_C
            and not c.get("rejection_reason")
            and not c.get("band_c_capped"))
    ]
    targets.sort(key=lambda c: (0 if is_onboarded(c) else 1,
                                -float(c.get("evidence_quality") or 0.0)))
    return targets


# ---------------------------------------------------------------------------
# Floor re-scoping (spec §5) — the floor is a Band-B quality bar, nothing more
# ---------------------------------------------------------------------------

def rescope_floor(candidate: dict, searched_pn: Optional[str]) -> None:
    """Re-scope a `suitability_below_floor` rejection to the band rules (spec §5).
    Mutates in place; touches ONLY the floor rejection (pn_mismatch /
    duplicate_in_higher_tier / any other rejection type is never cleared).

      Band A — NEVER floor-rejected: an exact/canonical part-match being floored
               is the defect this kills (US Seal at 12.6). Cleared.
      Band C — not scored, so the floor does not apply (the count cap replaces
               it). Cleared.
      Band B — the floor stands as a quality bar on partial evidence, EXCEPT when
               the candidate has real found-PN evidence (a compatible/aftermarket
               PN — Zoro at 10.5): a vendor who found a part variant is evidence-
               qualified; the crude keyword score is ordering input only (§5).
    """
    if candidate.get("rejection_reason") != "suitability_below_floor":
        return
    band = candidate.get("band") or assign_band(candidate, searched_pn)
    if band == BAND_A:
        note = "floor_cleared_band_a"
    elif band == BAND_C:
        note = "floor_not_applicable_band_c"
    elif pn_evidence_for(candidate, searched_pn) != "none":
        note = "floor_cleared_pn_evidence"
    else:
        return  # Band B without PN evidence: the quality bar stands.
    candidate["rejection_reason"] = None
    candidate["band_note"] = note


def cap_band_c(candidates: list[dict], cap: int = BAND_C_CAP) -> None:
    """Apply the Band-C count cap (spec §5) across a candidate set: keep the
    top-`cap` non-onboarded C candidates by evidence quality (scope strength);
    onboarded C candidates are ALWAYS included and never count against the cap.
    Annotate-don't-remove: over-cap candidates get band_c_capped=True (excluded
    from the outreach block, still present for audit). Candidates already
    carrying a rejection_reason don't compete for cap slots."""
    eligible = [
        c for c in candidates
        if (c.get("band") == BAND_C
            and not c.get("rejection_reason")
            and not is_onboarded(c))
    ]
    eligible.sort(key=lambda c: -float(c.get("evidence_quality") or 0.0))
    for c in eligible[:cap]:
        c["band_c_capped"] = False
    for c in eligible[cap:]:
        c["band_c_capped"] = True


def apply_ranking_bands(result: dict, searched_pn: Optional[str]) -> dict:
    """Flag-on post-pass over a SourcingAgent result dict:
      1. annotate every tier candidate with band + evidence_quality,
      2. re-scope the suitability floor to the band rules (§5),
      3. apply the Band-C count cap (cross-tier, onboarded always included),
      4. stable-reorder each tier list into banded order.
    Mutates `result` in place and returns it. Never changes list membership.
    Callers gate on ranking_bands_active() — this function itself is
    unconditional so tests can drive it directly."""
    all_candidates: list[dict] = []
    for tier_key in ("tier_1", "tier_2", "tier_3"):
        tier = result.get(tier_key) or {}
        results = tier.get("results")
        if not results:
            continue
        for c in results:
            annotate_candidate(c, searched_pn)
            rescope_floor(c, searched_pn)
        all_candidates.extend(results)
    cap_band_c(all_candidates)
    for tier_key in ("tier_1", "tier_2", "tier_3"):
        results = (result.get(tier_key) or {}).get("results")
        if results:
            results.sort(key=banded_sort_key)
    return result
