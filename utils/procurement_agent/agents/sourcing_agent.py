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
from utils.models import SourcingRun, AssetSpecs, SourcingOption

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

    Vendor identity: normalized vendor_name (lowercase alphanumeric only).
    Priority: Tier 1 > Tier 2 > Tier 3. An active higher-tier entry claims the
    vendor slot; the same vendor in a lower tier gets rejection_reason=
    "duplicate_in_higher_tier". First-set wins: options already rejected are
    left unchanged. Rejected higher-tier entries do NOT claim the slot, so the
    same vendor can surface in a lower tier if the higher-tier result was poor.

    Returns the count of newly-marked duplicates.
    """
    seen: set[str] = set()
    deduped = 0

    for tier_label, tier_data in (("tier_1", tier1), ("tier_2", tier2), ("tier_3", tier3)):
        for o in tier_data.get("results", []):
            name = _normalize_vendor_name(o.get("vendor_name") or "")
            if not name:
                continue
            if name in seen:
                if not o.get("rejection_reason"):
                    o["rejection_reason"] = "duplicate_in_higher_tier"
                    print(
                        f"[SourcingAgent] Rejected (duplicate_in_higher_tier): "
                        f"{o.get('vendor_name')!r} in {tier_label}"
                    )
                    deduped += 1
            elif not o.get("rejection_reason"):
                seen.add(name)

    return deduped


def _apply_suitability_floor(options: list[dict], threshold: float) -> None:
    """Annotate options below the suitability floor with rejection_reason.

    First-set wins: options already carrying a rejection_reason are skipped.
    Mutates in place; options remain in the list for audit log capture.
    """
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

        # Item 4: suitability gate — first quality filter on the active pipeline path
        from utils.sourcing_archieved.constants import TIER_SURFACE_MIN_SUITABILITY
        for tier in (tier1, tier2, tier3):
            _apply_suitability_floor(tier.get("results", []), TIER_SURFACE_MIN_SUITABILITY)
        filters.append(f"suitability_floor:{TIER_SURFACE_MIN_SUITABILITY:.0f}%")

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

        tier3_pivot = any(
            r.get("search_type") == "capability_pivot"
            for r in tier3.get("results", [])
        )

        return {
            "tier_1":                 tier1,
            "tier_2":                 tier2,
            "tier_3":                 tier3,
            "warranty_banner":        _WARRANTY_BANNER if warranty == "in_warranty" else None,
            "urgency_applied":        urgency_label,
            "filters_applied":        filters,
            "tier3_capability_pivot": tier3_pivot,
        }

    # ------------------------------------------------------------------
    # Tier runners
    # ------------------------------------------------------------------

    def _run_tier1(self, specs: AssetSpecs, weights: dict) -> list[dict]:
        """Match against the Arkim onboarded supplier catalog (Fix 2: normalized PN)."""
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
        candidate["apollo_industry"] = record.get("apollo_industry")
        candidate["apollo_country"]  = record.get("apollo_country")
        candidate["is_us_confirmed"] = record.get("is_us_confirmed")

    # ------------------------------------------------------------------
    # Apollo <-> suitability-floor reconciliation (CLAUDE.md §9)
    # ------------------------------------------------------------------

    def _reconcile_suitability(self, candidates: list[dict]) -> list[dict]:
        """Reconcile Apollo suitability_status with the suitability_floor verdict.

        ASYMMETRIC and REMOVES NOTHING (count out == count in):
          - "confirmed"              -> RESCUE: clear ONLY a "suitability_below_floor"
            rejection (Apollo resolved the org as US + right-business; it overrides
            the crude floor score). Never clears any other rejection type.
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

            if status == "confirmed":
                # RESCUE — only override the crude floor score, nothing else.
                if c.get("rejection_reason") == "suitability_below_floor":
                    c["rejection_reason"] = None
                    c["suitability_note"] = "rescued_by_apollo_confirmed"
                    print(f"[SourcingAgent] Rescued (apollo_confirmed): "
                          f"{c.get('vendor_name')} — cleared suitability_below_floor "
                          f"(score={float(c.get('suitability_score') or 0):.0f}%)")

            elif status == "rejected_unsuitable":
                # FLAG ONLY — never drop, never set rejection_reason (a lone Apollo
                # reject can be a wrong-org match, e.g. ibtinc.com -> Pakistan).
                reason = "non-US" if c.get("is_us_confirmed") is False else "business mismatch"
                c["apollo_flag"] = (
                    f"apollo: {reason} — country={c.get('apollo_country') or 'unknown'}, "
                    f"industry={c.get('apollo_industry') or 'unknown'} (review)"
                )

            elif status == "unconfirmed_flag_human":
                c["apollo_flag"] = "apollo: unconfirmed — flag for human review"

        return candidates

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
        """Return one Tier 1 Arkim Network candidate from seeded authorized brands.

        Called only when the catalog match returns nothing, so real catalog
        entries always take precedence over this synthetic fallback.

        is_mock: reserved for future filtering (production exclusion,
        programmatic distinction when real Tier 1 vendors exist). For demos,
        mock vendors render normally.
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
            auth_brands = br.get("authorized_service_brands") or []
            if not auth_brands:
                return []
        except Exception:
            return []

        brand = auth_brands[0]
        print(f"[SourcingAgent] Seeded Tier 1 mock candidate: {brand!r} for {specs.manufacturer!r}")
        return [{
            "vendor_name":                brand,
            "base_price":                 0.0,
            "lead_time_days":             4,
            "reliability_score":          95.0,
            "merchant_type":              "Arkim Network",
            "match_type":                 "Exact OEM",
            "source_url":                 None,
            "price_tbd":                  True,
            "suitability_score":          92.0,
            "confidence_score":           88.0,
            "vendor_authorization_status": "Authorized",
            "onboarding_status":          "Active",
            "in_stock":                   True,
            "notes":                      "Arkim Network — OEM authorized. Confirmed pricing within 30 min.",
            "found_part_number":          None,
            "is_mock":                    True,
        }]

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
            speed_norm = 1.0 - min(1.0, (o.get("lead_time_days") or 7) / 30.0)
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
