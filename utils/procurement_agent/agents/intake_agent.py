"""
Intake Agent — multimodal data extraction, sufficiency assessment, clarification.

Brief reference: Section 3.1, Section 8.1.

Phase 1: run() is a stub that returns a synthetic AssetSpecs dict and marks
the run as ready to proceed. Phase 2 implements:
  - Multimodal vision extraction (Claude Sonnet for image-heavy, Haiku for text-only)
  - Per-field confidence scoring
  - Sufficiency gate: manufacturer_confidence ≥ 70 AND part_id_confidence ≥ 70
  - Dynamic single-question clarification loop
  - "search anyway" force-proceed escape hatch
"""

from typing import Optional
from utils.models import ProcurementRun


class IntakeAgent:
    """Extracts structured AssetSpecs from user input and assesses sufficiency."""

    # Phase 2 will inject these at construction time from app config.
    def __init__(self, anthropic_api_key: Optional[str] = None):
        self._api_key = anthropic_api_key

    def run(self, run: ProcurementRun, user_input: dict) -> dict:
        """Extract AssetSpecs from user_input and assess sufficiency.

        Phase 1 stub — returns a synthetic populated spec dict so the orchestrator
        can advance to INVENTORY without real extraction logic.

        Args:
            run: the current ProcurementRun (provides prior context on follow-up turns)
            user_input: dict with keys:
                - "text": str — the user's chat message
                - "images": list[bytes] — uploaded image data (optional)
                - "force_proceed": bool — bypass sufficiency gate if True

        Returns:
            dict with keys:
                - "asset_specs": dict — populated AssetSpecs fields
                - "manufacturer_confidence": float 0-100
                - "part_id_confidence": float 0-100
                - "sufficient": bool — True = proceed, False = ask follow-up
                - "follow_up_question": str | None — single focused question when not sufficient
                - "stub": bool — True in Phase 1
        """
        return {
            "asset_specs": {
                "manufacturer":          user_input.get("text", "Unknown Manufacturer"),
                "model":                 "STUB-MODEL-001",
                "part_number":           "PN-STUB-001",
                "voltage":               "460V",
                "category":              "Part",
                "detected_type":         "Motor",
                "manufacturer_confidence": 95.0,
                "part_id_confidence":    90.0,
            },
            "manufacturer_confidence": 95.0,
            "part_id_confidence":      90.0,
            "sufficient":              True,
            "follow_up_question":      None,
            "stub":                    True,
        }
