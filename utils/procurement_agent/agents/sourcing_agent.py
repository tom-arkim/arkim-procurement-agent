"""
Sourcing Agent — runs all three tiers in parallel and returns ranked candidates.

Brief reference: Section 3.3, Section 6, Section 8.3.

Phase 1: run() is a stub that returns empty tier result sets.
Phase 2 implements:
  - Tier 1: direct API calls to onboarded vendors (mocked in prototype)
  - Tier 2: Tavily search restricted to known marketplace domains
  - Tier 3: Tavily unrestricted search with manufacturer-anchored queries
  - Parallel tier execution
  - Urgency and warranty guardrails applied to ranking (not filtering, except
    in_warranty which filters Tier 3 aftermarket)
  - OEM authorized distributor detection (brand_intelligence + URL pattern)
  - Re-uses scoring.py, filtering.py, price_sanity.py, market_confidence.py
    from utils/sourcing_archieved/ without modification
"""

from typing import Optional
from utils.models import ProcurementRun


class SourcingAgent:
    """Runs three-tier vendor discovery and returns ranked per-tier result sets."""

    def __init__(
        self,
        tavily_api_key: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
    ):
        self._tavily_key = tavily_api_key
        self._anthropic_key = anthropic_api_key

    def run(self, run: ProcurementRun) -> dict:
        """Execute all three sourcing tiers for the run's AssetSpecs.

        Phase 1 stub — returns empty result sets.

        Args:
            run: ProcurementRun with asset_specs_json, urgency_factor, warranty_status.

        Returns:
            dict with keys:
                - "tier1": list[dict] — Arkim onboarded supplier results
                - "tier2": list[dict] — digital marketplace results (Grainger, McMaster, etc.)
                - "tier3": list[dict] — broader market discovery results
                - "stub": bool — True in Phase 1
                - "message": str
        """
        return {
            "tier1":   [],
            "tier2":   [],
            "tier3":   [],
            "stub":    True,
            "message": "Sourcing agent not yet implemented (Phase 2).",
        }
