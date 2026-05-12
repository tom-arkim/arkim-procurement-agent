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
# SourcingAgent
# ---------------------------------------------------------------------------

class SourcingAgent:
    """Runs three-tier vendor discovery and returns ranked per-tier result sets."""

    def __init__(
        self,
        tavily_api_key: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
    ):
        self._tavily_key    = tavily_api_key
        self._anthropic_key = anthropic_api_key

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

        filters: list[str] = []
        if warranty == "in_warranty":
            filters.append("in_warranty: aftermarket excluded from tier_3")

        # Item 4: suitability gate — first quality filter on the active pipeline path
        from utils.sourcing_archieved.constants import TIER_SURFACE_MIN_SUITABILITY
        for tier in (tier1, tier2, tier3):
            _apply_suitability_floor(tier.get("results", []), TIER_SURFACE_MIN_SUITABILITY)
        filters.append(f"suitability_floor:{TIER_SURFACE_MIN_SUITABILITY:.0f}%")

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
                    print(f"[SourcingAgent] Tier 2 stem fallback: {specs.part_number!r} → {stem!r}, {len(dicts)} result(s)")

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
            print("[SourcingAgent] Tier 3 skipped — asset in warranty")
            return []

        # Fix 5 — capability pivot when Tier 2 returned zero results
        if tier2_count == 0 and specs.manufacturer not in _UNKNOWN_MANUFACTURERS:
            print(f"[SourcingAgent] Tier 3 capability pivot — Tier 2 empty for {specs.manufacturer!r}")
            results = self._capability_search(specs)
            return self._rank(results, weights)

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
