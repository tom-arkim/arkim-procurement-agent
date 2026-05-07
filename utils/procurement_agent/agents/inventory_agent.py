"""
Inventory Agent — query connected facility inventory before hitting the external market.

Brief reference: Section 3.2, Section 8.2.

Phase 1: run() is a stub that returns "no inventory data available."
Phase 5 implements:
  - Facility inventory connection patterns: none / CSV upload / API integration
  - Fuzzy part-number matching using LLM when exact PN not found
  - Future CMMS integrations: Maximo, eMaint, Fiix, UpKeep, NetSuite
"""

from utils.models import SourcingRun


class InventoryAgent:
    """Checks on-site inventory for the requested part before external sourcing."""

    def run(self, run: SourcingRun) -> dict:
        """Query connected inventory systems for the part in the run's AssetSpecs.

        Phase 1 stub — always returns "no data" so the workflow proceeds to sourcing.

        Returns:
            dict with keys:
                - "found": bool — True if part located on site
                - "results": list[dict] — [{location, quantity, condition}] or []
                - "connection_type": "none" | "csv" | "api"
                - "message": str — human-readable summary
                - "stub": bool — True in Phase 1
        """
        return {
            "found":           False,
            "results":         [],
            "connection_type": "none",
            "message":         "No inventory connection configured (Phase 5 feature).",
            "stub":            True,
        }
