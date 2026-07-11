"""
Sourcing Agent — runs all three tiers and returns ranked candidates.

Brief reference: Section 3.3, Section 6, Section 8.3.

Rectification Sprint additions:
  - Fix 2: normalize_part_number() — strips delimiters before Tier 1 PN comparison
  - Fix 4: stem_part_number() — manufacturer-aware PN stemming for Tier 2 fallback search
  - Fix 5: Tier 3 capability pivot — when Tier 2 returns 0, searches for authorized distributors
"""

import dataclasses
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Optional

from utils.apollo_client import ApolloClient
from utils.models import SourcingRun, AssetSpecs, SourcingOption, lead_time_source_for, lead_time_speed_confidence
from utils.procurement_agent import tier1_matcher

_TIER1_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "data", "mock_tier1_suppliers.json",
)

_TIER_TIMEOUT = 30  # seconds

_URGENCY_WEIGHTS: dict[str, dict[str, float]] = {
    "emergency":  {"price": 0.15, "speed": 0.50, "reliability": 0.35},
    "predictive": {"price": 0.40, "speed": 0.35, "reliability": 0.25},
    "stocking":   {"price": 0.60, "speed": 0.20, "reliability": 0.20},
}

_WARRANTY_BANNER = (
    "This asset is under warranty. Aftermarket and third-party parts may void your warranty. "
    "Arkim Network (Tier 1) and OEM-authorized marketplace (Tier 2) options are recommended."
)

_ASSETSPECS_FIELDS = {f.name for f in dataclasses.fields(AssetSpecs)}
_REQUIRED_FIELDS   = {"manufacturer", "model", "part_number", "voltage"}

_UNKNOWN_MANUFACTURERS = {"Unknown", "unknown", "N/A", "n/a", "", None}


# ---------------------------------------------------------------------------
# DEMO_MODE — public no-login demo spine (mirrors api_server._env_truthy)
# ---------------------------------------------------------------------------
# Tier 1 is the Arkim ONBOARDED supplier catalog. As of this change the seed
# (data/mock_tier1_suppliers.json) is {"suppliers": []}: every entry it held
# was a FABRICATED distributor (invented vendors, fake prices, dead/parked/
# placeholder domains — e.g. industrialcontrolsolutions.com, nationalseal.com).
# They are gone at the source, so no fabricated Tier 1 vendor can be sourced or
# cached in ANY mode (demo or non-demo) — not merely gated under DEMO_MODE.
# The synthetic brand-intelligence Tier 1 fallback (_seeded_tier1_candidates)
# is likewise PERMANENTLY disabled (returns [] unconditionally): it minted a
# fabricated "Arkim Network — OEM authorized, confirm pricing in 30 min" vendor
# and, worse, wrote it back to known_parts.json, perpetuating a fabricated-
# vendor cache treadmill in non-demo. With both gone, Tier 1 is honestly EMPTY
# in all modes until real onboarded suppliers exist; Tier 2/3 (live Tavily +
# LLM) carry all real sourcing, and genuine brand-intelligence anchors still
# surface in Tier 3 via _seeded_tier3_candidates (untouched).
#
# The DEMO_MODE early-return below is retained as belt-and-suspenders: it
# short-circuits the (now-empty) seed read + _seeded_tier1_candidates call under
# demo, which changes nothing behaviorally (both already return []) but keeps
# the demo-live-only intent explicit and documented. Strict opt-in parse
# matches api_server._env_truthy / email_sender._env_truthy (only
# 1/true/yes/on -> True; everything else fails safe to False).

def _demo_mode_active() -> bool:
    """True iff env DEMO_MODE is a truthy token (1/true/yes/on). Read at call
    time so tests can set/unset the env before invoking agent.run()."""
    return (os.environ.get("DEMO_MODE") or "").strip().lower() in ("1", "true", "yes", "on")


def _intake_type_aware() -> bool:
    """True iff env INTAKE_TYPE_AWARE is a truthy token (1/true/yes/on) — the
    intake-redesign feature flag (guardrail 3). Read at call time. When False,
    the component-aware sourcing query (T5b) is byte-identical to today: the
    `_component_of` internal key is not promoted and the query builders skip
    the component-aware branch. Mirrors api_server._env_truthy / the intake gate."""
    return (os.environ.get("INTAKE_TYPE_AWARE") or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Fix 2 — Part number normalization
# ---------------------------------------------------------------------------

def normalize_part_number(pn: str) -> str:
    """Strip all non-alphanumeric characters and uppercase for delimiter-agnostic comparison."""
    if not pn:
        return ""
    return re.sub(r"[^A-Z0-9]", "", pn.upper())


# ---------------------------------------------------------------------------
# Fix 4 — Manufacturer-aware PN stemming
# ---------------------------------------------------------------------------

def stem_part_number(pn: str, manufacturer: str) -> Optional[str]:
    """Return the family stem for a PN per manufacturer-specific rule, or None.

    None means exact PN match is required (no stemming applies).
    """
    from utils.brand_intelligence import get_pn_stemming_rule
    rule = get_pn_stemming_rule(manufacturer)
    if not rule:
        return None
    normalized = normalize_part_number(pn)
    m = re.match(rule["pattern"], normalized)
    return m.group(rule["group"]) if m else None


# ---------------------------------------------------------------------------
# Vendor name normalization
# ---------------------------------------------------------------------------

_VENDOR_SUFFIX_RE = re.compile(
    r"\b(?:co(?:mpany)?|inc(?:orporated)?|llc|ltd|corp(?:oration)?|limited)\b\.?$",
    re.IGNORECASE,
)


def _normalize_vendor_name(name: str) -> str:
    """Lowercase alphanumeric key, stripping legal suffixes (Co., Inc., LLC…).

    "Gainesville Industrial Electric" and "Gainesville Industrial Electric Co."
    both produce "gainesvilleindustrialelectric" so cross-tier dedup fires.
    """
    s = _VENDOR_SUFFIX_RE.sub("", (name or "").lower()).strip()
    return re.sub(r"[^a-z0-9]", "", s)


# ---------------------------------------------------------------------------
# Quality filtering functions (Items 4 and 6)
# ---------------------------------------------------------------------------

def _dedup_across_tiers(tier1: dict, tier2: dict, tier3: dict) -> int:
    """Mark lower-tier duplicates of active higher-tier vendors.

    Vendor identity: normalized vendor_name (lowercase alphanumeric only) OR the
    identical listing URL (normalized: trailing "/" stripped, lowercased). The URL
    check catches the same listing surfaced under different name spellings
    (e.g. "sealit123.com" / "sealit123" / "Seal It 123" at one URL) that the
    name-only key misses.

    Priority: Tier 1 > Tier 2 > Tier 3. An active higher-tier entry claims BOTH its
    name slot and its URL slot; the same vendor/listing in a lower tier gets
    rejection_reason="duplicate_in_higher_tier". First-set wins: options already
    rejected are left unchanged. Rejected higher-tier entries do NOT claim a slot,
    so the same vendor can resurface lower if the higher-tier result was poor.

    Scope note: this resolves only the URL-IDENTICAL subset of the §5a dedup
    backlog. The alias root cause — the same supplier under different names at
    DIFFERENT urls (e.g. OTC Industrial / OTC Industrial Technologies) — is
    unchanged and still needs the entity-resolution layer. §5a is NOT resolved.

    Returns the count of newly-marked duplicates.
    """
    seen_names: set[str] = set()
    seen_urls: set[str] = set()
    deduped = 0

    for tier_label, tier_data in (("tier_1", tier1), ("tier_2", tier2), ("tier_3", tier3)):
        for o in tier_data.get("results", []):
            name = _normalize_vendor_name(o.get("vendor_name") or "")
            url = (o.get("source_url") or "").rstrip("/").lower()
            if not name and not url:
                continue
            is_dup = (name and name in seen_names) or (url and url in seen_urls)
            if is_dup:
                if not o.get("rejection_reason"):
                    o["rejection_reason"] = "duplicate_in_higher_tier"
                    print(
                        f"[SourcingAgent] Rejected (duplicate_in_higher_tier): "
                        f"{o.get('vendor_name')!r} in {tier_label}"
                    )
                    deduped += 1
            elif not o.get("rejection_reason"):
                # First (highest-tier) occurrence claims BOTH slots.
                if name:
                    seen_names.add(name)
                if url:
                    seen_urls.add(url)

    return deduped


def _suitability_floor_for(specs) -> float:
    """The effective suitability floor for a request, PN-aware.

    Clean-PN parts (a real part_number per scoring._is_placeholder_pn) get the
    standard 30 floor — well-calibrated for their bimodal score distribution
    (junk ≤29, real matches ≥85). Spec-described parts (part_number null /
    placeholder — UNKNOWN-PN, N/A, etc.) get the lower 20 floor: they are
    structurally score-capped (no PN to match → fit_pts==0 → the 45-cap, then
    the type-gate ×0.7 compresses in-class specialists to ~24.5), so the 30
    floor culls legitimate specialists. See constants.TIER_SURFACE_MIN_SUITABILITY_SPEC
    for the full calibration evidence. ``specs`` may be None → standard floor.
    """
    from utils.sourcing_archieved.constants import (
        TIER_SURFACE_MIN_SUITABILITY,
        TIER_SURFACE_MIN_SUITABILITY_SPEC,
    )
    from utils.sourcing_archieved.scoring import _is_placeholder_pn

    if specs is not None and _is_placeholder_pn(getattr(specs, "part_number", None)):
        return TIER_SURFACE_MIN_SUITABILITY_SPEC
    return TIER_SURFACE_MIN_SUITABILITY


def _apply_suitability_floor(options: list[dict], threshold: float,
                             specs=None) -> None:
    """Annotate options below the suitability floor with rejection_reason.

    First-set wins: options already carrying a rejection_reason are skipped.
    Mutates in place; options remain in the list for audit log capture.

    PN-aware floor (calibration fix): when ``specs`` is supplied and the request
    part is spec-described (part_number null/placeholder per
    scoring._is_placeholder_pn), ``threshold`` is lowered to
    TIER_SURFACE_MIN_SUITABILITY_SPEC (20). Spec-described parts are structurally
    score-capped (no PN to match → fit_pts==0 → 45-cap, then the type-gate ×0.7
    compresses in-class specialists to ~24.5), so the 30 floor calibrated for
    clean-PN parts culls legitimate specialists (Goulds Pumps, Petro Valve,
    Valworx at 24.5 — 49 real vendors in the captured data). The 20 floor keeps
    them while still cutting the 0–9 junk lobe. This is an UNCONDITIONAL fix —
    the structural cap and the junk-vs-specialist separation exist independent of
    SCORING_V2; the floor is applied before the V2 TypeGate and on the flag-off
    path too. Callers that omit ``specs`` get the legacy behavior (their passed
    threshold) unchanged.
    """
    if specs is not None:
        threshold = _suitability_floor_for(specs)
    for o in options:
        if o.get("rejection_reason"):
            continue
        score = float(o.get("suitability_score") or 0.0)
        if score < threshold:
            o["rejection_reason"] = "suitability_below_floor"
            print(
                f"[SourcingAgent] Rejected (suitability_below_floor): {o.get('vendor_name')} "
                f"suitability={score:.1f}% < {threshold:.0f}% floor"
            )


# ---------------------------------------------------------------------------
# Apollo Tier 3 suitability clarifier helpers (CLAUDE.md §9)
# ---------------------------------------------------------------------------

_APOLLO_SUITABILITY_SYSTEM = (
    "You are a procurement sourcing analyst. Given a REQUIRED PART and a CANDIDATE "
    "SUPPLIER profile, decide whether the supplier could plausibly supply that part.\n"
    "Reply with exactly ONE word as the first token of your response:\n"
    "  CONFIRMED - the supplier's industry/keywords/description clearly indicate they "
    "sell or distribute this kind of part.\n"
    "  REJECTED  - the supplier is clearly in an unrelated business (e.g. a software "
    "company for a mechanical seal).\n"
    "  UNSURE    - not enough signal to decide.\n"
    "Output only that single word."
)

_US_COUNTRY_VALUES = {
    "united states", "usa", "us", "u.s.", "u.s.a.", "united states of america",
}


def _is_us(org: dict) -> bool:
    """US check from Apollo org country. Inconclusive (False) when country is absent."""
    return (org.get("country") or "").strip().lower() in _US_COUNTRY_VALUES


def _apollo_fields_from_org(org: dict) -> dict:
    """Project Apollo org-enrich output onto the supplier_registry apollo_* columns."""
    return {
        "apollo_org_name":   org.get("name"),
        "apollo_description": org.get("description"),
        "apollo_industry":    org.get("industry"),
        "apollo_keywords":    org.get("keywords") or [],
        "apollo_country":     org.get("country"),
        "apollo_state":       org.get("state"),
        "apollo_raw_address": org.get("raw_address"),
    }


def _build_suitability_user(specs: AssetSpecs, org: dict) -> str:
    """Compact requirement + supplier projection for the requirement-match prompt."""
    required = {
        "manufacturer":  specs.manufacturer,
        "part_number":   specs.part_number,
        "category":      specs.category,
        "detected_type": getattr(specs, "detected_type", None),
        "description":   getattr(specs, "description", None),
        "material_spec": getattr(specs, "material_spec", None),
    }
    supplier = {
        "name":        org.get("name"),
        "industry":    org.get("industry"),
        "keywords":    (org.get("keywords") or [])[:40],  # cap to bound tokens
        "description": org.get("description"),
    }
    return (
        "REQUIRED PART:\n" + json.dumps(required, default=str)
        + "\n\nCANDIDATE SUPPLIER:\n" + json.dumps(supplier, default=str)
    )


# Legal/entity suffixes stripped before comparing org names.
_NAME_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "co", "company", "corp",
    "corporation", "gmbh", "ag", "sa", "srl", "bv", "plc", "lp", "llp", "pllc",
    "pte", "pvt", "group", "holdings", "intl", "international",
}


def _normalize_org_name(name: Optional[str]) -> set:
    """Lowercase, drop punctuation + legal suffixes; return the set of core tokens."""
    if not name:
        return set()
    cleaned = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    return {t for t in cleaned.split() if t and t not in _NAME_LEGAL_SUFFIXES}


def _names_plausibly_match(vendor_name: Optional[str], apollo_org_name: Optional[str]) -> bool:
    """Lenient check: do the discovery vendor name and Apollo's resolved org name
    plausibly refer to the same entity?

    Catches GROSS mismatches (domain->wrong-org resolution, e.g. "J&D Manufacturing"
    vs "QC Supply"; "IBT Industrial Solutions" vs a Pakistani training company) while
    tolerating legal-suffix / extra-word variants ("All Seals Inc" vs "All Seals
    Incorporated"; "Warfield Electric" vs "Warfield Electric Products Inc"). Bias is
    toward matching legitimate variants — a false "mismatch" only withholds a rescue
    (mild); the goal is to deny rescue on a clearly different org.

    Missing/empty either name -> False (fail safe: can't corroborate -> withhold).
    """
    a = _normalize_org_name(vendor_name)
    b = _normalize_org_name(apollo_org_name)
    if not a or not b:
        return False
    # Despaced match (C1): the same core tokens differing only in internal spacing,
    # e.g. "MROSupply" vs "MRO Supply". Compares the character composition of the
    # suffix-stripped token sets, so a near-miss ("MRP Supply") stays a miss.
    if "".join(sorted(a)) == "".join(sorted(b)):
        return True
    if a <= b or b <= a:                      # one core-token set contains the other
        return True
    overlap = a & b
    return bool(overlap) and len(overlap) / min(len(a), len(b)) >= 0.5


# Tier 3 suitability ordering buckets (lower sorts first).
_TIER3_RANK_ORDER = {"top": 0, "middle": 1, "bottom": 2}

# Named-contact escalation: sales/account-exec title match + seniority preference.
_SENIORITY_RANK = {
    "owner": 0, "founder": 1, "c_suite": 2, "partner": 3, "vp": 4,
    "head": 5, "director": 6, "manager": 7, "senior": 8, "entry": 9, "intern": 10,
}


def _is_sales_title(title: Optional[str]) -> bool:
    t = (title or "").lower()
    return "sales" in t or "account executive" in t or "account exec" in t


def _pick_sales_contact(people: list) -> Optional[dict]:
    """Pick the best sales/account-exec person: highest seniority among sales-titled,
    tiebroken by search order (stable sort). None if no sales-titled person."""
    sales = [p for p in (people or []) if _is_sales_title(p.get("title"))]
    if not sales:
        return None
    sales.sort(key=lambda p: _SENIORITY_RANK.get((p.get("seniority") or "").lower(), 99))
    return sales[0]


# ---------------------------------------------------------------------------
# SourcingAgent
# ---------------------------------------------------------------------------

class SourcingAgent:
    """Runs three-tier vendor discovery and returns ranked per-tier result sets."""

    def __init__(
        self,
        tavily_api_key: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
        apollo_api_key: Optional[str] = None,
    ):
        self._tavily_key    = tavily_api_key
        self._anthropic_key = anthropic_api_key
        self._apollo_key    = apollo_api_key
        # Apollo Tier 3 suitability clarifier. ApolloClient reads APOLLO_API_KEY
        # from the env when apollo_api_key is None, and is disabled (no-op) when
        # no key is configured — so the pipeline runs with or without Apollo.
        self._apollo = ApolloClient(api_key=apollo_api_key)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, run: SourcingRun) -> dict:
        """Execute all three sourcing tiers for the run's AssetSpecs.

        Tier 1 and Tier 2 run in parallel. Tier 3 runs after Tier 2 so it can
        apply the capability pivot when Tier 2 returns zero results (Fix 5).

        Returns:
            dict with keys:
                - "tier_1", "tier_2", "tier_3": {results, count, status}
                - "warranty_banner": str | None
                - "urgency_applied": str
                - "filters_applied": list[str]
                - "tier3_capability_pivot": bool
        """
        specs_dict = run.asset_specs_json or {}
        specs      = self._dict_to_specs(specs_dict)
        urgency    = float(run.urgency_factor if run.urgency_factor is not None else 0.3)
        warranty   = (run.warranty_status or "unknown").lower()

        urgency_label = (
            "emergency" if urgency >= 0.9
            else "stocking" if urgency == 0.0
            else "predictive"
        )
        weights = _URGENCY_WEIGHTS[urgency_label]

        self._patch_sourcing_keys()

        # Tier 1 and Tier 2 in parallel
        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(self._run_tier1, specs, weights)
            f2 = executor.submit(self._run_tier2, specs, weights)
            tier1 = self._collect(f1, "tier_1")
            tier2 = self._collect(f2, "tier_2")

        # Tier 3 after Tier 2 — needs tier2 count for capability pivot (Fix 5)
        with ThreadPoolExecutor(max_workers=1) as executor:
            f3 = executor.submit(self._run_tier3, specs, weights, warranty, tier2["count"])
            tier3 = self._collect(f3, "tier_3")

        # Apollo suitability clarifier runs AFTER tier collection — outside the
        # per-tier timeout future — so its (network/LLM) latency can never trip the
        # timeout and discard Tier 3. Annotate-don't-remove; covers all Tier 3
        # results incl. the capability-pivot path; fail-soft / no-op without a key.
        self._apollo_clarify(tier3.get("results", []), specs)

        filters: list[str] = []
        if warranty == "in_warranty":
            filters.append("in_warranty: aftermarket excluded from tier_3")

        # Item 4: suitability gate — first quality filter on the active pipeline path.
        # PN-aware floor: spec-described parts (no real PN) get the lower 20 floor
        # (see _suitability_floor_for / TIER_SURFACE_MIN_SUITABILITY_SPEC); clean-PN
        # parts stay at 30. Same threshold applied to all three tiers for one run.
        floor = _suitability_floor_for(specs)
        for tier in (tier1, tier2, tier3):
            _apply_suitability_floor(tier.get("results", []), floor, specs)
        filters.append(f"suitability_floor:{floor:.0f}%")

        # Reconcile Apollo suitability with the floor verdict (Tier 3 only), AFTER
        # both clarifier annotation and the floor have run. Asymmetric, removes
        # nothing: confirmed rescues a "suitability_below_floor" reject; rejected
        # only flags (never drops — Apollo can resolve the wrong org, §9);
        # unconfirmed surfaces a review flag. Runs before dedup so a rescued
        # candidate participates correctly in cross-tier dedup.
        self._reconcile_suitability(tier3.get("results", []))
        rescued_n = sum(
            1 for c in tier3.get("results", [])
            if c.get("suitability_note") == "rescued_by_apollo_confirmed"
        )
        if rescued_n:
            filters.append(f"apollo_rescued:{rescued_n}")

        # Item 6: cross-tier dedup — vendor in highest active tier wins
        deduped = _dedup_across_tiers(tier1, tier2, tier3)
        if deduped:
            filters.append(f"cross_tier_dedup:{deduped}")

        # Suitability-driven down-ranking + outreach-selection metadata (Tier 3).
        # Annotate + stable reorder only — removes nothing. Corroborated rejects sink
        # to the bottom, are not default-selected, and require outreach confirmation.
        self._rank_and_select_tier3(tier3.get("results", []))

        # Contact resolution (free path) for the default-selected Tier 3 suppliers:
        # store -> generic inbox -> human-flag. No Apollo, no send; annotates only.
        self._resolve_contact(tier3.get("results", []))

        tier3_pivot = any(
            r.get("search_type") == "capability_pivot"
            for r in tier3.get("results", [])
        )

        result = {
            "tier_1":                 tier1,
            "tier_2":                 tier2,
            "tier_3":                 tier3,
            "warranty_banner":        _WARRANTY_BANNER if warranty == "in_warranty" else None,
            "urgency_applied":        urgency_label,
            "filters_applied":        filters,
            "tier3_capability_pivot": tier3_pivot,
        }

        # RANKING_BANDS_V1 (spec: RANKING_BANDS_SPEC.md) — evidence-banded ranking
        # post-pass. Flag OFF ⇒ this block never runs and the result is byte-
        # identical to pre-band behavior. Fail-soft: a banding error degrades to
        # the un-banded result (logged), never crashes the run.
        try:
            from utils.procurement_agent.ranking_bands import (
                apply_ranking_bands, ranking_bands_active,
            )
            if ranking_bands_active():
                apply_ranking_bands(result, specs.part_number)
                filters.append("ranking_bands:v1")
        except Exception as exc:
            print(f"[SourcingAgent] ranking_bands post-pass failed (un-banded result kept): {exc}")

        return result

    # ------------------------------------------------------------------
    # Tier runners
    # ------------------------------------------------------------------

    def _run_tier1(self, specs: AssetSpecs, weights: dict) -> list[dict]:
        """Match against the Arkim onboarded supplier catalog.

        Night 5 (TIER1_V2): when the supplier-scope redesign is live, Tier 1 is
        populated by the onboarded-supplier MATCHER (``tier1_matcher.match_tier1``)
        against the Night 3 supplier-scope registry — class hard-gate + brand
        amplifier + territory rank + local_service hard filter, fresh per run, NO
        fabricated prices (a card carries a price ONLY when a dated confirmed
        price_db quote exists; else quote-expected framing). See tier1_matcher.py
        for the design + honesty guarantees. The matcher result is built into
        honest candidate dicts and returned; the caller excludes Tier 1 from the
        known_parts write-back (I4 — registry-backed cards never enter staleness).

        Flag-off (TIER1_V2 not live): the seed catalog
        (data/mock_tier1_suppliers.json is {"suppliers": []} — fabricated vendors
        purged at the source) + the permanently-disabled _seeded_tier1_candidates
        fallback leave Tier 1 honestly EMPTY — byte-identical to pre-Night-5 (T5).
        The DEMO_MODE early-return is belt-and-suspenders: it short-circuits the
        empty read under demo (no behavioral change). Tier 2/3 carry all real
        sourcing when Tier 1 is empty.
        """
        if _demo_mode_active():
            print("[SourcingAgent] DEMO_MODE: Tier 1 live-only — seed catalog + synthetic fallback gated off")
            return []

        # Night 5 — registry-backed Tier 1 (TIER1_V2). Fresh per run, no cache write.
        # Gated by the flag (tier1_matcher.tier1_v2_active reads supplier_registry.TIER1_V2
        # live, so tests monkeypatch it). Fail-soft: a matcher/registry error degrades
        # to [] (the matcher never raises into the pipeline).
        if tier1_matcher.tier1_v2_active():
            try:
                matches = tier1_matcher.match_tier1(
                    detected_type=getattr(specs, "detected_type", None),
                    manufacturer=specs.manufacturer,
                    description=getattr(specs, "description", None),
                    model=getattr(specs, "model", None),
                )
            except Exception as exc:
                print(f"[SourcingAgent] Tier 1 matcher error (degraded to empty): {exc}")
                matches = []
            if not matches:
                return []
            results = tier1_matcher.candidates_from_matches(
                matches, manufacturer=specs.manufacturer,
                part_number=specs.part_number or "",
            )
            return self._rank(results, weights)

        try:
            with open(_TIER1_CATALOG_PATH, "r") as fh:
                catalog = json.load(fh)
        except Exception as exc:
            print(f"[SourcingAgent] Tier 1 catalog load failed: {exc}")
            return []

        pn_norm   = normalize_part_number(specs.part_number or "")
        mfg_lower = (specs.manufacturer or "").lower().strip()
        results: list[dict] = []

        for supplier in catalog.get("suppliers", []):
            for item in supplier.get("inventory", []):
                item_pn_norm = normalize_part_number(item.get("part_number") or "")
                item_mfg     = (item.get("manufacturer") or "").lower().strip()

                pn_match  = bool(pn_norm and item_pn_norm and pn_norm == item_pn_norm)
                mfg_match = bool(mfg_lower and item_mfg and mfg_lower in item_mfg)

                if not (pn_match or mfg_match):
                    continue

                match_type  = "Exact OEM" if pn_match else "Functional Alternative"
                suitability = 92.0 if pn_match else 58.0
                confidence  = 90.0 if pn_match else 62.0

                results.append({
                    "vendor_name":               supplier["name"],
                    "base_price":                float(item.get("price", 0.0)),
                    "lead_time_days":             int(item.get("lead_days", 2)),
                    "lead_time_source":          lead_time_source_for(item),  # extracted if the catalog row stated it
                    "reliability_score":          float(supplier.get("reliability_score", 95.0)),
                    "merchant_type":              "Arkim Network",
                    "match_type":                 match_type,
                    "source_url":                 supplier.get("website"),
                    "price_tbd":                  False,
                    "suitability_score":          suitability,
                    "confidence_score":           confidence,
                    "vendor_authorization_status": "Authorized",
                    "onboarding_status":          "Active",
                    "in_stock":                   bool(item.get("in_stock", True)),
                    "notes":                      f"Arkim Network — {supplier.get('location', 'US')}",
                    "found_part_number":          item.get("part_number"),
                })

        # Item 3: catalog wins — only seed when catalog finds nothing, so a real
        # Tier 1 match for any other manufacturer always takes precedence.
        if not results:
            results = self._seeded_tier1_candidates(specs)

        return self._rank(results, weights)

    def _run_tier2(self, specs: AssetSpecs, weights: dict) -> list[dict]:
        """Tavily search restricted to known marketplace domains.

        Fix 4: if the first search returns no results, retry with the stemmed PN.
        """
        try:
            from utils.sourcing_archieved.enterprise_search import _call_enterprise_api, _vendor_name_from_url
            options = _call_enterprise_api(specs, search_mode="exact")
            dicts   = [self._option_to_dict(o) for o in options]
            for d in dicts:
                if canonical := _vendor_name_from_url(d.get("source_url") or ""):
                    d["vendor_name"] = canonical

            # Fix 4 — stem-based fallback when exact search finds nothing
            if not dicts:
                stem = stem_part_number(specs.part_number or "", specs.manufacturer or "")
                orig_norm = normalize_part_number(specs.part_number or "")
                if stem and stem != orig_norm:
                    stemmed_specs = dataclasses.replace(specs, part_number=stem)
                    options2 = _call_enterprise_api(stemmed_specs, search_mode="broad")
                    dicts = [self._option_to_dict(o) for o in options2]
                    for d in dicts:
                        if canonical := _vendor_name_from_url(d.get("source_url") or ""):
                            d["vendor_name"] = canonical
                    print(f"[SourcingAgent] Tier 2 stem fallback: {specs.part_number!r} -> {stem!r}, {len(dicts)} result(s)")

            return self._rank(dicts, weights)
        except Exception as exc:
            print(f"[SourcingAgent] Tier 2 failed: {exc}")
            return []

    def _run_tier3(
        self, specs: AssetSpecs, weights: dict, warranty: str, tier2_count: int = -1
    ) -> list[dict]:
        """Broader market discovery with Fix 5 capability pivot.

        When tier2_count == 0 and manufacturer is known, pivots to an authorized
        distributor search instead of the standard part-specific queries.
        """
        if warranty == "in_warranty":
            print("[SourcingAgent] Tier 3 skipped -- asset in warranty")
            return []

        # Fix 5 — capability pivot when Tier 2 returned zero results
        if tier2_count == 0 and specs.manufacturer not in _UNKNOWN_MANUFACTURERS:
            print(f"[SourcingAgent] Tier 3 capability pivot -- Tier 2 empty for {specs.manufacturer!r}")
            seeded = self._seeded_tier3_candidates(specs)
            seeded_names = {
                _normalize_vendor_name(c.get("vendor_name") or "")
                for c in seeded
            }
            pivot = self._capability_search(specs)
            pivot_filtered = [
                d for d in pivot
                if _normalize_vendor_name(d.get("vendor_name") or "") not in seeded_names
            ]
            return self._rank(seeded + pivot_filtered, weights)

        try:
            from utils.sourcing_archieved.enterprise_search import (
                _discover_national_specialists,
                _discover_aftermarket_specialists,
                _vendor_name_from_url,
            )

            # Item 8: seeded OEM authorized distributors anchor the list;
            # Tavily results that duplicate a seeded name are dropped.
            seeded = self._seeded_tier3_candidates(specs)
            seeded_names = {
                _normalize_vendor_name(c.get("vendor_name") or "")
                for c in seeded
            }

            national    = _discover_national_specialists(specs, [])
            aftermarket = _discover_aftermarket_specialists(specs, national)
            raw_tavily  = [self._option_to_dict(o) for o in (national + aftermarket)]
            for d in raw_tavily:
                if canonical := _vendor_name_from_url(d.get("source_url") or ""):
                    d["vendor_name"] = canonical

            tavily_filtered = [
                d for d in raw_tavily
                if _normalize_vendor_name(d.get("vendor_name") or "")
                   not in seeded_names
            ]

            combined = seeded + tavily_filtered
            return self._rank(combined, weights)
        except Exception as exc:
            print(f"[SourcingAgent] Tier 3 failed: {exc}")
            return []

    # ------------------------------------------------------------------
    # Apollo Tier 3 suitability clarifier (CLAUDE.md §9)
    # ------------------------------------------------------------------

    def _apollo_clarify(self, candidates: list[dict], specs: AssetSpecs) -> list[dict]:
        """Annotate Tier 3 survivors with an Apollo suitability verdict, in place.

        Store-check-first (never pay for a fresh/onboarded supplier) -> org_enrich
        on miss/stale -> write back hit AND miss -> annotate-don't-remove. Fail-soft
        per candidate and no-ops cleanly when Apollo is disabled (no key). NEVER
        drops a candidate; downstream may use the annotation, the clarifier never
        removes (no rejection_reason is set here).
        """
        from utils import supplier_registry

        for c in candidates:
            try:
                url = c.get("source_url")
                if not url:
                    continue  # seeded OEM distributors etc. carry no domain to validate
                domain = ApolloClient._clean_domain(url)
                if not domain:
                    continue

                # STORE-CHECK-FIRST — fresh cache (or onboarded) => no Apollo call.
                record = supplier_registry.lookup_by_domain(domain)
                if record and not supplier_registry.needs_reenrichment(record):
                    self._annotate_from_cache(c, record)
                    continue

                # MISS / STALE-not-onboarded => enrich. org_enrich is fail-soft:
                # returns None when disabled, on a miss, or on any error.
                org = self._apollo.org_enrich(domain)
                if org:
                    is_us   = _is_us(org)
                    verdict = self._requirement_match(specs, org, is_us)
                    supplier_registry.upsert_apollo_data(domain, {
                        **_apollo_fields_from_org(org),
                        "is_us_confirmed":    is_us,
                        "suitability_status": verdict,
                    })
                    self._annotate(c, verdict, org=org, is_us=is_us)
                else:
                    # No coverage / disabled / error => flag for human, never drop.
                    self._annotate(c, "unconfirmed_flag_human")
                    if self._apollo.enabled:
                        # We actually consulted Apollo (real miss): cache the flag so
                        # we don't re-pay for this domain until it goes stale.
                        supplier_registry.upsert_apollo_data(
                            domain, {"suitability_status": "unconfirmed_flag_human"}
                        )
                    # If disabled, leave the store untouched so a later enrich fires.
            except Exception as exc:
                # Per-candidate fail-soft: a clarifier failure never blocks the run.
                print(f"[SourcingAgent] Apollo clarify failed for "
                      f"{c.get('vendor_name')!r}, flagging for human: {exc}")
                c["suitability_status"] = "unconfirmed_flag_human"

        return candidates

    def _requirement_match(self, specs: AssetSpecs, org: dict, is_us: bool) -> str:
        """Map (US check + LLM requirement match) to a suitability_status. Fail-soft.

        Non-US -> rejected_unsuitable (annotate-only, still not removed). US + LLM
        CONFIRMED -> confirmed; US + LLM REJECTED -> rejected_unsuitable; anything
        ambiguous or any LLM failure -> unconfirmed_flag_human.
        """
        if not is_us:
            return "rejected_unsuitable"
        try:
            from utils.sourcing_archieved.llm_parsing import _anthropic_complete
            raw = _anthropic_complete(
                _APOLLO_SUITABILITY_SYSTEM, _build_suitability_user(specs, org)
            )
            tokens = (raw or "").strip().upper().split()
            first  = tokens[0] if tokens else ""
            if first.startswith("CONFIRMED"):
                return "confirmed"
            if first.startswith("REJECTED"):
                return "rejected_unsuitable"
            return "unconfirmed_flag_human"
        except Exception as exc:
            print(f"[SourcingAgent] Apollo requirement-match LLM failed, "
                  f"flagging for human: {exc}")
            return "unconfirmed_flag_human"

    @staticmethod
    def _annotate(
        candidate: dict, status: str, org: Optional[dict] = None,
        is_us: Optional[bool] = None,
    ) -> None:
        """Attach the verdict (annotate-don't-remove; never sets rejection_reason)."""
        candidate["suitability_status"] = status
        if org:
            candidate["apollo_org_name"] = org.get("name")
            candidate["apollo_industry"] = org.get("industry")
            candidate["apollo_country"]  = org.get("country")
        if is_us is not None:
            candidate["is_us_confirmed"] = is_us

    @staticmethod
    def _annotate_from_cache(candidate: dict, record: dict) -> None:
        """Reuse a fresh cached verdict without an Apollo call."""
        candidate["suitability_status"] = (
            record.get("suitability_status") or "unconfirmed_flag_human"
        )
        candidate["apollo_org_name"] = record.get("apollo_org_name")
        candidate["apollo_industry"] = record.get("apollo_industry")
        candidate["apollo_country"]  = record.get("apollo_country")
        candidate["is_us_confirmed"] = record.get("is_us_confirmed")

    # ------------------------------------------------------------------
    # Apollo <-> suitability-floor reconciliation (CLAUDE.md §9)
    # ------------------------------------------------------------------

    def _reconcile_suitability(self, candidates: list[dict]) -> list[dict]:
        """Reconcile Apollo suitability_status with the suitability_floor verdict.

        ASYMMETRIC and REMOVES NOTHING (count out == count in):
          - "confirmed"              -> RESCUE (gated): clear ONLY a
            "suitability_below_floor" rejection, and ONLY when the candidate's vendor
            name plausibly matches Apollo's resolved org name. Otherwise withhold the
            rescue (leave the floor reject) and annotate "rescue_withheld_name_mismatch"
            — Apollo can "confirm" a DIFFERENT org from a mis-attributed domain.
            Never clears any other rejection type.
          - "rejected_unsuitable"    -> FLAG ONLY: attach a human-readable apollo_flag
            from apollo_country/apollo_industry. Never sets rejection_reason, never
            removes — Apollo can resolve the WRONG org from a domain (§9 / IBT).
          - "unconfirmed_flag_human" -> surface a review flag; rejection_reason
            untouched (unconfirmed does NOT rescue).
          - no suitability_status    -> untouched (e.g. seeded OEM, never clarified).

        Changes eligibility/flags, not list membership. Actual exclusion is a
        separate future step (requires explicit go-ahead).
        """
        for c in candidates:
            status = c.get("suitability_status")
            if not status:
                continue

            # Persist the name-consistency result once (reused by the rescue gate
            # below and by _rank_and_select_tier3) — don't recompute divergently.
            name_match = _names_plausibly_match(c.get("vendor_name"), c.get("apollo_org_name"))
            c["apollo_name_match"] = name_match

            if status == "confirmed":
                # RESCUE — only override the crude floor score, and only when the
                # candidate's identity corroborates Apollo's resolved org (Apollo can
                # "confirm" a DIFFERENT org from a mis-attributed domain — §9 / J&D).
                if c.get("rejection_reason") == "suitability_below_floor":
                    apollo_name = c.get("apollo_org_name")
                    if name_match:
                        c["rejection_reason"] = None
                        c["suitability_note"] = "rescued_by_apollo_confirmed"
                        print(f"[SourcingAgent] Rescued (apollo_confirmed): "
                              f"{c.get('vendor_name')} - cleared suitability_below_floor "
                              f"(score={float(c.get('suitability_score') or 0):.0f}%)")
                    else:
                        # Verdict belongs to a different (or unknown) org — withhold
                        # the rescue, leave the floor reject in place, annotate why.
                        c["suitability_note"] = "rescue_withheld_name_mismatch"
                        c["apollo_flag"] = (
                            f"apollo: rescue withheld - confirmed org "
                            f"'{apollo_name or 'unknown'}' != candidate "
                            f"'{c.get('vendor_name')}' (review)"
                        )
                        print(f"[SourcingAgent] Rescue withheld (name mismatch): "
                              f"{c.get('vendor_name')!r} vs apollo org {apollo_name!r}")

            elif status == "rejected_unsuitable":
                # FLAG ONLY — never drop, never set rejection_reason (a lone Apollo
                # reject can be a wrong-org match, e.g. ibtinc.com -> Pakistan).
                reason = "non-US" if c.get("is_us_confirmed") is False else "business mismatch"
                c["apollo_flag"] = (
                    f"apollo: {reason} - country={c.get('apollo_country') or 'unknown'}, "
                    f"industry={c.get('apollo_industry') or 'unknown'} (review)"
                )

            elif status == "unconfirmed_flag_human":
                c["apollo_flag"] = "apollo: unconfirmed - flag for human review"

        return candidates

    # ------------------------------------------------------------------
    # Suitability-driven down-ranking + outreach selection (Tier 3, CLAUDE.md §9)
    # ------------------------------------------------------------------

    def _rank_and_select_tier3(self, candidates: list[dict]) -> list[dict]:
        """Order Tier 3 by suitability confidence and set outreach-selection metadata.

        REMOVES NOTHING — annotate + STABLE reorder only (count out == count in).
        Corroboration gates action in BOTH directions (mirrors the rescue gate): a
        rejected_unsuitable candidate is down-ranked ONLY when the name-consistency
        check passed (Apollo resolved the right org and still rejects). A
        name-MISMATCHED reject stays neutral (MIDDLE, default-selectable) — its
        verdict may be about a different org — but keeps its apollo_flag.

        Per candidate sets:
          - suitability_rank_tier: "top" | "middle" | "bottom"
              TOP    = confirmed AND name-matched (trustworthy-suitable)
              BOTTOM = rejected_unsuitable AND name-matched (corroborated-unsuitable)
              MIDDLE = everything else (unconfirmed; no status / seeded OEM;
                       confirmed-but-name-mismatch; reject-but-name-mismatch)
          - default_outreach_selected: bucket is TOP/MIDDLE AND no active
              rejection_reason (a floor/dup-rejected candidate is never pre-selected).
          - requires_outreach_confirmation: True for BOTTOM only — the UI must demand
              review-and-accept before outreach. (MIDDLE name-mismatch rejects are not
              gated: the reject is untrusted; the flag remains for visibility.)
          - outreach_confirmation_reason: human-readable WHY (BOTTOM only).

        Then stable-sorts TOP -> MIDDLE -> BOTTOM, preserving existing intra-bucket
        order (the _rank TCA ordering + dedup state).
        """
        for c in candidates:
            status  = c.get("suitability_status")
            matched = c.get("apollo_name_match") is True

            if status == "confirmed" and matched:
                tier = "top"
            elif status == "rejected_unsuitable" and matched:
                tier = "bottom"
            else:
                tier = "middle"

            c["suitability_rank_tier"] = tier
            c["default_outreach_selected"] = (tier != "bottom") and not c.get("rejection_reason")
            c["requires_outreach_confirmation"] = (tier == "bottom")
            if tier == "bottom":
                if c.get("is_us_confirmed") is False:
                    why = f"Flagged non-US (Apollo: {c.get('apollo_country') or 'unknown'})"
                else:
                    why = f"Flagged business mismatch (Apollo: {c.get('apollo_industry') or 'unknown'})"
                c["outreach_confirmation_reason"] = why

        # Stable sort: intra-bucket order (TCA rank + dedup) is preserved.
        candidates.sort(key=lambda c: _TIER3_RANK_ORDER.get(c.get("suitability_rank_tier"), 1))
        return candidates

    # ------------------------------------------------------------------
    # Tier 3 contact resolution — free path (CLAUDE.md §9). Sub-step 1:
    # store -> generic inbox -> human-flag. NO Apollo, NO send.
    # ------------------------------------------------------------------

    def _resolve_contact(self, candidates: list[dict]) -> list[dict]:
        """Resolve HOW to contact each default-selected Tier 3 supplier (free path).

        REMOVES NOTHING — annotates contact metadata only. ZERO Apollo calls (no
        people-search / people-enrich — that's a later escalation). Does NOT send
        (EMAIL_SEND_ENABLED stays False); the generic inbox is CONSTRUCTED, not
        verified — a later bounce (mark_contact_bounced) clears + re-flags it.

        Only default_outreach_selected candidates are resolved (deselected / BOTTOM
        suppliers are left without a contact by design). Cascade per supplier:
          1. STORE      — a cached non-bounced contact for this domain is reused.
          2. GENERIC    — sales@{domain} (info@{domain} documented fallback),
                          written back. The default path.
          3. HUMAN-FLAG — no domain (e.g. seeded OEM, source_url=None).
        """
        from utils import supplier_registry

        for c in candidates:
            if not c.get("default_outreach_selected"):
                continue  # tie resolution to the selection contract

            url = c.get("source_url")
            domain = ApolloClient._clean_domain(url) if url else ""

            # 1. STORE — reuse a cached, non-bounced contact.
            record = supplier_registry.lookup_by_domain(domain) if domain else None
            if record and record.get("contact_email") and record.get("contact_status") != "bounced":
                self._annotate_contact(c, record.get("contact_email"), "store", "resolved")
                continue

            # 2. GENERIC INBOX (default) — construct + write back. Not verified.
            if domain:
                email = f"sales@{domain}"
                supplier_registry.upsert_contact(domain, {
                    "contact_email":  email,
                    "contact_method": "generic_inbox",
                    "contact_status": "resolved",
                })  # upsert_contact stamps contact_resolved_at
                self._annotate_contact(c, email, "generic_inbox", "resolved",
                                       fallback=f"info@{domain}")
                continue

            # 3. HUMAN-FLAG — no domain to construct an inbox from.
            self._annotate_contact(c, None, "human_flag", "needs_human")

        return candidates

    @staticmethod
    def _annotate_contact(
        candidate: dict, email: Optional[str], method: str, status: str,
        fallback: Optional[str] = None,
    ) -> None:
        """Attach resolved contact metadata to the candidate (distinct from the
        discovery `contact_email` field, which is left untouched)."""
        candidate["resolved_contact_email"] = email
        candidate["contact_method"] = method
        candidate["contact_status"] = status
        candidate["contact_email_fallback"] = fallback
        if method == "human_flag":
            candidate["contact_note"] = "no domain — needs human contact resolution"

    # ------------------------------------------------------------------
    # Tier 3 named-contact escalation — Apollo people-search -> enrich (credit-gated,
    # TRIGGERED ONLY: manual or on generic-inbox bounce). NOT in the default flow.
    # ------------------------------------------------------------------

    def _escalate_contact(self, candidate: dict) -> dict:
        """Escalate to a named PRIMARY contact via Apollo (credit-gated).

        people_search (free) -> pick best sales/account-exec -> people_match (exactly
        ONE, 1 credit, reveals email) -> store as PRIMARY; the generic inbox stays the
        FALLBACK. Cache-hard: a resolved primary in the store is reused (never
        re-enriched). Fail-soft: search empty / no sales person / enrich miss / Apollo
        disabled -> no primary (primary_contact_status='none'); the fallback stands.
        Never called by the default _resolve_contact flow — triggered only.
        """
        from utils import supplier_registry

        url = candidate.get("source_url")
        domain = ApolloClient._clean_domain(url) if url else ""
        if not domain:
            self._annotate_primary(candidate, None, status="none")
            return candidate

        # Cache-hard: reuse an already-resolved primary (zero Apollo calls).
        record = supplier_registry.lookup_by_domain(domain)
        if record and record.get("primary_contact_email") and \
                record.get("primary_contact_status") == "resolved":
            self._annotate_primary(
                candidate, record.get("primary_contact_email"),
                name=record.get("primary_contact_name"),
                title=record.get("primary_contact_title"),
                person_id=record.get("primary_contact_person_id"),
                source=record.get("primary_contact_source") or "store",
                status="resolved",
            )
            return candidate

        # people_search (free) -> pick one sales/account-exec.
        people = self._apollo.people_search(
            domain, titles=["sales", "account executive"], include_similar_titles=True
        )
        person = _pick_sales_contact(people)
        if not person:
            self._annotate_primary(candidate, None, status="none")
            return candidate

        # Enrich exactly ONE person — the only credit-spending call.
        enriched = self._apollo.people_match(
            person_id=person.get("person_id"),
            first_name=person.get("first_name"),
            last_name=person.get("last_name"),
            domain=domain,
        )
        if not enriched or not enriched.get("email"):
            # Search found a real sales person but no email is available. Persist WHO
            # they are (name/title/person_id, status="found_no_email") so we don't
            # pointlessly re-search and a human can see the contact. This does NOT make
            # them the effective contact — the generic-inbox fallback is unchanged
            # (found_no_email != resolved, see effective_contact / recipient_set).
            name  = person.get("name")
            title = person.get("title")
            pid   = person.get("person_id")
            supplier_registry.upsert_primary_contact(domain, {
                "primary_contact_name":      name,
                "primary_contact_title":     title,
                "primary_contact_person_id": pid,
                "primary_contact_source":    "apollo_search",
                "primary_contact_status":    "found_no_email",
            })
            print(f"[SourcingAgent] Apollo: searched, person found, no email - "
                  f"{name} ({title}) @ {domain} -> found_no_email")
            self._annotate_primary(candidate, None, name=name, title=title, person_id=pid,
                                   source="apollo_search", status="found_no_email")
            return candidate

        name  = enriched.get("name") or person.get("name")
        title = enriched.get("title") or person.get("title")
        pid   = enriched.get("person_id") or person.get("person_id")
        supplier_registry.upsert_primary_contact(domain, {
            "primary_contact_email":     enriched["email"],
            "primary_contact_name":      name,
            "primary_contact_title":     title,
            "primary_contact_person_id": pid,
            "primary_contact_source":    "apollo_enriched",
            "primary_contact_status":    "resolved",
        })
        self._annotate_primary(candidate, enriched["email"], name=name, title=title,
                               person_id=pid, source="apollo_enriched", status="resolved")
        return candidate

    @staticmethod
    def _annotate_primary(
        candidate: dict, email: Optional[str], name: Optional[str] = None,
        title: Optional[str] = None, person_id: Optional[str] = None,
        source: Optional[str] = None, status: str = "none",
    ) -> None:
        """Attach the named PRIMARY contact (the generic-inbox fallback is untouched)."""
        candidate["primary_contact_email"] = email
        candidate["primary_contact_name"] = name
        candidate["primary_contact_title"] = title
        candidate["primary_contact_person_id"] = person_id
        candidate["primary_contact_source"] = source
        candidate["primary_contact_status"] = status

    # ------------------------------------------------------------------
    # Fix 5 — Capability search (Tier 3 pivot)
    # ------------------------------------------------------------------

    def _capability_search(self, specs: AssetSpecs) -> list[dict]:
        """Search for authorized distributors when Tier 2 found nothing."""
        from utils.sourcing_archieved.enterprise_search import _vendor_name_from_url
        mfg = (specs.manufacturer or "").strip()
        if not mfg or mfg in _UNKNOWN_MANUFACTURERS:
            return []

        detected = (specs.detected_type or specs.category or "industrial equipment").strip()
        query    = f"Authorized {mfg} {detected} distributor"

        try:
            import utils.sourcing_archieved as _arch
            tavily = getattr(_arch, "_tavily", None)
            if not tavily:
                print("[SourcingAgent] Tier 3 capability pivot: no Tavily client available")
                return []

            response = tavily.search(query=query, max_results=5)
            from utils.sourcing_archieved.tavily_client import NON_US_TLDS, NON_US_DOMAIN_HINTS
            from urllib.parse import urlparse as _up
            options = []
            for r in response.get("results", []):
                h = (_up(r.get("url", "").lower()).hostname or "")
                if any(h.endswith(t) for t in NON_US_TLDS) or any(x in h for x in NON_US_DOMAIN_HINTS):
                    print(f"[SourcingAgent] Capability pivot: excluded non-US result {r.get('url')}")
                    continue
                options.append({
                    "vendor_name":               _vendor_name_from_url(r.get("url")) or r.get("title", "Unknown Distributor"),
                    "base_price":                0.0,
                    "lead_time_days":            7,
                    "lead_time_source":          "placeholder",  # capability-pivot: no contact, no real lead time
                    "reliability_score":         70.0,
                    "merchant_type":             "Capability Discovery",
                    "match_type":                "Capability Pivot",
                    "source_url":                r.get("url"),
                    "price_tbd":                 True,
                    "suitability_score":         65.0,
                    "confidence_score":          60.0,
                    "vendor_authorization_status": "Unverified",
                    "onboarding_status":         "Not Onboarded",
                    "in_stock":                  None,
                    "notes":                     f"Capability pivot: {query}",
                    "found_part_number":         None,
                    "search_type":               "capability_pivot",
                })
            return options
        except Exception as exc:
            print(f"[SourcingAgent] Capability search failed: {exc}")
            return []

    # ------------------------------------------------------------------
    # Item 8 — Seeded Tier 3 authorized distributor candidates
    # ------------------------------------------------------------------

    def _seeded_tier3_candidates(self, specs: AssetSpecs) -> list[dict]:
        """Synthesize Tier 3 candidates from seeded authorized distributor data.

        Seeded authorized distributors should rank above Tavily-discovered Tier 3
        results. Adjust if Tavily candidates regularly score 75+.
        """
        try:
            from utils.brand_intelligence import get_brand_relationships
            from utils.sourcing_archieved.scoring import _detect_equip_type

            if specs.manufacturer in _UNKNOWN_MANUFACTURERS:
                return []

            equip_kw = (
                _detect_equip_type(specs)
                or specs.detected_type
                or specs.category
                or "industrial"
            )
            br          = get_brand_relationships(specs.manufacturer, equip_kw)
            auth_brands = (br.get("authorized_service_brands") or [])[:5]
            if not auth_brands:
                return []
        except Exception:
            return []

        candidates = []
        for brand in auth_brands:
            candidates.append({
                "vendor_name":                brand,
                "base_price":                 0.0,
                "lead_time_days":             10,
                "lead_time_source":           "placeholder",  # seeded OEM distributor: quote-required, no real lead time
                "reliability_score":          85.0,
                "merchant_type":              "OEM Authorized Distributor",
                "match_type":                 "OEM Authorized Distributor",
                "source_url":                 None,
                "price_tbd":                  True,
                "suitability_score":          88.0,
                "confidence_score":           75.0,
                "vendor_authorization_status": "Authorized",
                "onboarding_status":          "Not Onboarded",
                "in_stock":                   None,
                "notes":                      f"OEM authorized distributor for {specs.manufacturer}",
                "found_part_number":          None,
                "is_authorized":              True,
                "is_mock":                    True,
            })
        print(f"[SourcingAgent] Seeded {len(candidates)} OEM authorized Tier 3 candidate(s) for {specs.manufacturer!r}")
        return candidates

    # ------------------------------------------------------------------
    # Item 3 — Seeded Tier 1 Arkim Network candidate (catalog fallback)
    # ------------------------------------------------------------------

    def _seeded_tier1_candidates(self, specs: AssetSpecs) -> list[dict]:
        """PERMANENTLY disabled — return [] in ALL modes.

        History: this was a synthetic Tier 1 fallback that minted a fabricated
        "Arkim Network — OEM authorized. Confirmed pricing within 30 min."
        vendor (is_mock=True, source_url=None, price_tbd=True) from
        brand-intelligence authorized_service_brands whenever the seed catalog
        had no match. It was fabricated even without a dead URL, and — worse —
        its Tier 1 results were written back to known_parts.json (edge keyed by
        name slug, since source_url is None), so it perpetuated a fabricated-
        vendor cache treadmill in non-demo: every run re-surfaced and re-cached
        the fake "Arkim Network" Tier 1 card.

        With the seed catalog purged (data/mock_tier1_suppliers.json is now
        {"suppliers": []} — all fabricated vendors gone at the source) AND this
        synthetic fallback gated off unconditionally, Tier 1 is honestly EMPTY
        in demo AND non-demo until real onboarded suppliers exist. Tier 2/3
        (live Tavily + LLM) carry all real sourcing; genuine brand-intelligence
        anchors still surface in Tier 3 via _seeded_tier3_candidates (untouched).

        The previous DEMO_MODE-only gate is retained as belt-and-suspenders in
        _run_tier1 (it short-circuits the now-empty file read under demo), but
        this method no longer keys on DEMO_MODE at all — it returns [] always.
        """
        return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _collect(self, future, tier_label: str) -> dict:
        try:
            results = future.result(timeout=_TIER_TIMEOUT)
            return {"results": results, "count": len(results), "status": "ok"}
        except TimeoutError:
            print(f"[SourcingAgent] {tier_label} timed out")
            return {"results": [], "count": 0, "status": "timeout"}
        except Exception as exc:
            print(f"[SourcingAgent] {tier_label} error: {exc}")
            return {"results": [], "count": 0, "status": f"error: {exc}"}

    def _rank(self, options: list[dict], weights: dict) -> list[dict]:
        """Sort by TCA score under the given urgency weights."""
        if not options:
            return options

        priced_prices = [
            o.get("base_price") or 0
            for o in options
            if not o.get("price_tbd") and (o.get("base_price") or 0) > 0
        ]
        max_price = max(priced_prices) if priced_prices else 1.0

        def tca(o: dict) -> float:
            price     = o.get("base_price") or 0
            price_tbd = o.get("price_tbd", False)
            price_norm = 0.5 if price_tbd or price <= 0 else 1.0 - min(1.0, price / max(max_price, 1.0))
            # Speed from the REAL value only — no `or 7` fabrication. A missing lead earns no
            # speed signal (0), and provenance gates the rest: a placeholder (fake 7/10) -> 0
            # credit, a defaulted/estimated lead -> halved, extracted/quoted -> full. So a
            # fabricated "fast" lead can't out-rank a genuinely-known one in the candidate list.
            lead_days  = o.get("lead_time_days")
            speed_norm = 0.0 if lead_days is None else (1.0 - min(1.0, lead_days / 30.0))
            speed_norm *= lead_time_speed_confidence(o.get("lead_time_source"))
            rel_norm   = (o.get("reliability_score") or 70.0) / 100.0
            return (
                weights["price"]       * price_norm
                + weights["speed"]       * speed_norm
                + weights["reliability"] * rel_norm
            )

        return sorted(options, key=tca, reverse=True)

    @staticmethod
    def _option_to_dict(option: SourcingOption) -> dict:
        return dataclasses.asdict(option)

    @staticmethod
    def _dict_to_specs(d: dict) -> AssetSpecs:
        filtered = {k: v for k, v in d.items() if k in _ASSETSPECS_FIELDS}
        kwargs   = {k: v for k, v in filtered.items() if k not in _REQUIRED_FIELDS}
        # Phase 1 — promote the gated intake classifier's `_component_of` internal
        # key to the AssetSpecs.component_of field for sourcing. Inert when the
        # key is absent (flag off / no parent). The query builders honor
        # component_of only under INTAKE_TYPE_AWARE, so flag-off sourcing stays
        # byte-identical even if the key rides in from a prior flag-on intake.
        if _intake_type_aware() and d.get("_component_of") and "component_of" not in filtered:
            kwargs["component_of"] = d.get("_component_of")
        return AssetSpecs(
            manufacturer=filtered.get("manufacturer") or "Unknown",
            model=filtered.get("model") or "Unknown",
            part_number=filtered.get("part_number") or "UNKNOWN-PN",
            voltage=filtered.get("voltage") or "N/A",
            **kwargs,
        )

    def _patch_sourcing_keys(self) -> None:
        """Inject API keys into the sourcing_archieved namespace."""
        try:
            import utils.sourcing_archieved as _arch

            if self._anthropic_key:
                _arch.ANTHROPIC_API_KEY = self._anthropic_key

            if self._tavily_key:
                from tavily import TavilyClient
                if not getattr(_arch, "_tavily", None):
                    _arch._tavily = TavilyClient(api_key=self._tavily_key)
                _arch.TAVILY_API_KEY = self._tavily_key
        except Exception as exc:
            print(f"[SourcingAgent] Key patch failed: {exc}")
