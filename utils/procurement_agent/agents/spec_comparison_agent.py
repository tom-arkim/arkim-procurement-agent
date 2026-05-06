"""
Spec Comparison Agent — technical comparison artifacts for non-exact-match candidates.

Brief reference: Section 3.4, Section 8.4.

Phase 1: run() is a stub that returns a low-fidelity placeholder.
Phase 3 implements:
  - High fidelity (Tier 1 onboarded): full side-by-side comparison table
  - Medium fidelity (Tier 2 marketplace): partial comparison + explicit gaps
  - Low fidelity (Tier 3 discovered): "spec sheet required" placeholder
  - Fidelity is always honestly communicated — no false certainty
  - Reasoning model: Claude Sonnet where structured data exists; placeholder otherwise
"""

from utils.models import ProcurementRun


class SpecComparisonAgent:
    """Generates technical comparison artifacts for a candidate vendor result."""

    def run(self, run: ProcurementRun, candidate: dict) -> dict:
        """Compare a vendor candidate against the run's AssetSpecs.

        Phase 1 stub — returns a low-fidelity placeholder regardless of tier.

        Args:
            run:       ProcurementRun providing target AssetSpecs
            candidate: vendor result dict from SourcingAgent (includes tier info)

        Returns:
            dict with keys:
                - "fidelity":    "high" | "medium" | "low"
                - "comparison":  dict | None — field-by-field comparison (None for low fidelity)
                - "gaps":        list[str] — fields that couldn't be verified
                - "summary":     str — human-readable comparison summary
                - "stub":        bool — True in Phase 1
        """
        return {
            "fidelity":   "low",
            "comparison": None,
            "gaps":       ["All fields — spec sheet required from vendor."],
            "summary":    "Spec Comparison Agent not yet implemented (Phase 3). "
                          "Spec sheet required from vendor before approval.",
            "stub":       True,
        }
