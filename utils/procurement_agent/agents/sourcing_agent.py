"""
Sourcing Agent — runs all three tiers in parallel and returns ranked candidates.

Brief reference: Section 3.3, Section 6, Section 8.3.

Phase 2 implementation:
  - Tier 1: Arkim onboarded supplier catalog (data/mock_tier1_suppliers.json)
  - Tier 2: Tavily restricted to known marketplace domains (_call_enterprise_api)
  - Tier 3: broader discovery — _discover_national_specialists + _discover_aftermarket_specialists
  - concurrent.futures parallel execution with 30s timeout per tier
  - Urgency-based TCA ranking: Emergency shifts speed weight to 50%
  - in_warranty: Tier 3 skipped; warranty_banner added to output
"""

import dataclasses
import json
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Optional

from utils.models import ProcurementRun, AssetSpecs, SourcingOption

# Path to the mock Tier 1 catalog — injected via patch in tests.
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

    def run(self, run: ProcurementRun) -> dict:
        """Execute all three sourcing tiers for the run's AssetSpecs.

        Args:
            run: ProcurementRun with asset_specs_json, urgency_factor, warranty_status.

        Returns:
            dict with keys:
                - "tier_1": {results, count, status}
                - "tier_2": {results, count, status}
                - "tier_3": {results, count, status}
                - "warranty_banner": str | None
                - "urgency_applied": str
                - "filters_applied": list[str]
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

        with ThreadPoolExecutor(max_workers=3) as executor:
            f1 = executor.submit(self._run_tier1, specs, weights)
            f2 = executor.submit(self._run_tier2, specs, weights)
            f3 = executor.submit(self._run_tier3, specs, weights, warranty)

            tier1 = self._collect(f1, "tier_1")
            tier2 = self._collect(f2, "tier_2")
            tier3 = self._collect(f3, "tier_3")

        filters: list[str] = []
        if warranty == "in_warranty":
            filters.append("in_warranty: aftermarket excluded from tier_3")

        return {
            "tier_1":          tier1,
            "tier_2":          tier2,
            "tier_3":          tier3,
            "warranty_banner": _WARRANTY_BANNER if warranty == "in_warranty" else None,
            "urgency_applied": urgency_label,
            "filters_applied": filters,
        }

    # ------------------------------------------------------------------
    # Tier runners
    # ------------------------------------------------------------------

    def _run_tier1(self, specs: AssetSpecs, weights: dict) -> list[dict]:
        """Match against the Arkim onboarded supplier catalog."""
        try:
            with open(_TIER1_CATALOG_PATH, "r") as fh:
                catalog = json.load(fh)
        except Exception as exc:
            print(f"[SourcingAgent] Tier 1 catalog load failed: {exc}")
            return []

        pn_lower  = (specs.part_number or "").lower().strip()
        mfg_lower = (specs.manufacturer or "").lower().strip()
        results: list[dict] = []

        for supplier in catalog.get("suppliers", []):
            for item in supplier.get("inventory", []):
                item_pn  = (item.get("part_number") or "").lower().strip()
                item_mfg = (item.get("manufacturer") or "").lower().strip()

                pn_match  = bool(pn_lower and item_pn and pn_lower == item_pn)
                mfg_match = bool(mfg_lower and item_mfg and mfg_lower in item_mfg)

                if not (pn_match or mfg_match):
                    continue

                match_type = "Exact OEM" if pn_match else "Functional Alternative"
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

        return self._rank(results, weights)

    def _run_tier2(self, specs: AssetSpecs, weights: dict) -> list[dict]:
        """Tavily search restricted to known marketplace domains (Grainger, McMaster, etc.)."""
        try:
            from utils.sourcing_archieved.enterprise_search import _call_enterprise_api
            options = _call_enterprise_api(specs, search_mode="exact")
            dicts   = [self._option_to_dict(o) for o in options]
            return self._rank(dicts, weights)
        except Exception as exc:
            print(f"[SourcingAgent] Tier 2 failed: {exc}")
            return []

    def _run_tier3(self, specs: AssetSpecs, weights: dict, warranty: str) -> list[dict]:
        """Broader market: national specialists + aftermarket equivalents."""
        if warranty == "in_warranty":
            print("[SourcingAgent] Tier 3 skipped — asset in warranty")
            return []
        try:
            from utils.sourcing_archieved.enterprise_search import (
                _discover_national_specialists,
                _discover_aftermarket_specialists,
            )
            national    = _discover_national_specialists(specs, [])
            aftermarket = _discover_aftermarket_specialists(specs, national)
            combined    = [self._option_to_dict(o) for o in (national + aftermarket)]
            return self._rank(combined, weights)
        except Exception as exc:
            print(f"[SourcingAgent] Tier 3 failed: {exc}")
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
        """Inject API keys into both the shim and sourcing_archieved namespaces.

        llm_parsing._anthropic_complete() reads from utils.sourcing (the shim).
        enterprise_search and tavily_client read _tavily from utils.sourcing_archieved.
        Both must be patched so call-time lazy reads find the correct values.
        """
        try:
            import utils.sourcing as _shim
            import utils.sourcing_archieved as _arch

            if self._anthropic_key:
                _shim.ANTHROPIC_API_KEY = self._anthropic_key
                _arch.ANTHROPIC_API_KEY = self._anthropic_key

            if self._tavily_key:
                from tavily import TavilyClient
                if not getattr(_shim, "_tavily", None):
                    _shim._tavily = TavilyClient(api_key=self._tavily_key)
                if not getattr(_arch, "_tavily", None):
                    _arch._tavily = TavilyClient(api_key=self._tavily_key)
                _shim.TAVILY_API_KEY = self._tavily_key
                _arch.TAVILY_API_KEY = self._tavily_key
        except Exception as exc:
            print(f"[SourcingAgent] Key patch failed: {exc}")
