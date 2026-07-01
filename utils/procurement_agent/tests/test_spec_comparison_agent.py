"""
Tests for SpecComparisonAgent — all three fidelity tiers plus comparison helpers.
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from utils.procurement_agent.agents.comparison_helpers import (
    compare_dimensional,
    compare_material,
    compare_categorical,
    compare_frame,
)
from utils.procurement_agent.agents.spec_comparison_agent import SpecComparisonAgent
from utils.models import SourcingRun


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_run(specs: dict) -> SourcingRun:
    return SourcingRun(
        id="test-run-001",
        facility_id="00000000-0000-0000-0000-000000000000",
        current_phase="comparison",
        asset_specs_json=specs,
    )


def _seal_specs() -> dict:
    return {
        "manufacturer":  "John Crane",
        "model":         "MR-1-1375",
        "part_number":   "MR-1-1375",
        "detected_type": "mechanical seal",
        "shaft_size":    "1-3/8 inch",
        "material_spec": "Carbon",
        "voltage":       "N/A",
        "category":      "Part",
    }


def _motor_specs() -> dict:
    return {
        "manufacturer": "US Motors",
        "model":        "EM7054T",
        "part_number":  "EM7054T",
        "detected_type":"Electric Motor",
        "hp":           "30",
        "voltage":      "460V",
        "phase":        "3-phase",
        "rpm":          "1800",
        "frame":        "326T",
        "category":     "Equipment",
    }


@pytest.fixture
def agent():
    return SpecComparisonAgent(anthropic_api_key=None)


# ---------------------------------------------------------------------------
# comparison_helpers — dimensional
# ---------------------------------------------------------------------------

class TestCompareDimensional:
    def test_exact_inch_vs_decimal(self):
        assert compare_dimensional("1-5/8 inch", "1.625 inch") == "exact"

    def test_exact_inch_vs_mm(self):
        assert compare_dimensional("1-5/8 inch", "41.275 mm") == "exact"

    def test_compatible_within_tolerance(self):
        assert compare_dimensional("1-5/8 inch", "1.620 inch") == "compatible"

    def test_different_outside_tolerance(self):
        assert compare_dimensional("1-5/8 inch", "1.500 inch") == "different"

    def test_exact_same_value(self):
        assert compare_dimensional("1 inch", "1 inch") == "exact"

    def test_fraction_only(self):
        assert compare_dimensional("5/8 inch", "0.625 inch") == "exact"

    def test_mm_to_mm(self):
        assert compare_dimensional("25 mm", "25 mm") == "exact"

    def test_unparseable_returns_different(self):
        assert compare_dimensional("N/A", "1 inch") == "different"


# ---------------------------------------------------------------------------
# comparison_helpers — material
# ---------------------------------------------------------------------------

class TestCompareMaterial:
    def test_exact_same(self):
        assert compare_material("EPDM", "EPDM") == "exact"

    def test_buna_n_nitrile_compatible(self):
        assert compare_material("Buna-N", "Nitrile") == "compatible"

    def test_nitrile_buna_compatible(self):
        assert compare_material("Nitrile", "Buna-N") == "compatible"

    def test_viton_fkm_compatible(self):
        assert compare_material("Viton", "FKM") == "compatible"

    def test_epdm_viton_different(self):
        assert compare_material("EPDM", "Viton") == "different"

    def test_ptfe_teflon_compatible(self):
        assert compare_material("PTFE", "Teflon") == "compatible"

    def test_carbon_carbon_exact(self):
        assert compare_material("Carbon", "Carbon") == "exact"

    def test_unknown_material_different(self):
        assert compare_material("EPDM", "Unobtainium") == "different"


# ---------------------------------------------------------------------------
# comparison_helpers — categorical and frame
# ---------------------------------------------------------------------------

class TestCompareCategorical:
    def test_exact_match(self):
        assert compare_categorical("460V", "460V") == "exact"

    def test_case_insensitive(self):
        assert compare_categorical("3-phase", "3PHASE") == "exact"

    def test_different(self):
        assert compare_categorical("460V", "230V") == "different"


class TestCompareFrame:
    def test_exact_frame(self):
        assert compare_frame("326T", "326T") == "exact"

    def test_t_paired_frames_compatible(self):
        assert compare_frame("182T", "184T") == "compatible"

    def test_incompatible_frames(self):
        assert compare_frame("182T", "326T") == "different"


# ---------------------------------------------------------------------------
# High-fidelity — Tier 1 catalog lookup
# ---------------------------------------------------------------------------

class TestHighFidelity:
    TIER1_CANDIDATE_SEAL = {
        "vendor_name":       "National Seal & Bearing Co.",
        "found_part_number": "MR-1-1375",
        "source_url":        "https://nationalseal.com",
        "base_price":        340.0,
    }

    TIER1_CANDIDATE_MOTOR = {
        "vendor_name":       "Gulf Coast Electric Motor Service",
        "found_part_number": "EM7054T",
        "source_url":        "https://gulfcoastmotor.com",
        "base_price":        2100.0,
    }

    def test_high_fidelity_seal_exact_match(self, agent):
        run = _make_run(_seal_specs())
        artifact = agent.run(run, self.TIER1_CANDIDATE_SEAL, tier=1)

        assert artifact["fidelity"] == "high"
        assert artifact["vendor_name"] == "National Seal & Bearing Co."
        # shaft_size: "1-3/8 inch" vs "1-3/8 inch" → exact
        shaft_row = next(c for c in artifact["comparison"] if c["field"] == "shaft_size")
        assert shaft_row["match"] == "exact"

    def test_high_fidelity_seal_confirms_carbon_material(self, agent):
        run = _make_run(_seal_specs())
        artifact = agent.run(run, self.TIER1_CANDIDATE_SEAL, tier=1)

        mat_row = next((c for c in artifact["comparison"] if c["field"] == "material_spec"), None)
        assert mat_row is not None
        assert mat_row["match"] in ("exact", "compatible")

    def test_high_fidelity_fit_confirmed_when_all_match(self, agent):
        run = _make_run(_seal_specs())
        artifact = agent.run(run, self.TIER1_CANDIDATE_SEAL, tier=1)
        assert artifact["compatibility_summary"] in ("fit_confirmed", "fit_likely")

    def test_high_fidelity_motor_incompatible_hp(self, agent):
        specs = dict(_motor_specs())
        specs["hp"] = "150"  # asset needs 150HP, catalog has 30HP → incompatible
        run = _make_run(specs)
        artifact = agent.run(run, self.TIER1_CANDIDATE_MOTOR, tier=1)

        hp_row = next((c for c in artifact["comparison"] if c["field"] == "hp"), None)
        assert hp_row is not None
        assert hp_row["match"] == "different"
        assert artifact["compatibility_summary"] == "incompatible"

    def test_high_fidelity_motor_all_match(self, agent):
        run = _make_run(_motor_specs())
        artifact = agent.run(run, self.TIER1_CANDIDATE_MOTOR, tier=1)
        assert artifact["compatibility_summary"] in ("fit_confirmed", "fit_likely")
        assert artifact["verification_required_fields"] == []

    def test_high_fidelity_missing_catalog_falls_back(self, agent):
        candidate = {
            "vendor_name":       "National Seal & Bearing Co.",
            "found_part_number": "DOES-NOT-EXIST-999",
            "source_url":        "https://nationalseal.com",
        }
        run = _make_run(_seal_specs())
        artifact = agent.run(run, candidate, tier=1)
        # Falls back — still returns "high" fidelity label with honest engineer_notes
        assert artifact["fidelity"] == "high"
        assert artifact["engineer_notes"] is not None


# ---------------------------------------------------------------------------
# Medium-fidelity — Tier 2 snippet extraction (mocked)
# ---------------------------------------------------------------------------

class TestMediumFidelity:
    TIER2_CANDIDATE = {
        "vendor_name": "Instrumart",
        "source_url":  "https://instrumart.com/products/seal-123",
        "snippet":     "John Crane MR-1 mechanical seal. Shaft size 1-3/8 inch. Carbon/ceramic faces.",
        "base_price":  310.0,
    }

    def _mock_extract(self, snippet, fields):
        """Simulates Claude returning extracted specs."""
        return {
            "shaft_size":    "1-3/8 inch",
            "material_spec": "Carbon",
            "detected_type": "mechanical seal",
        }

    def test_medium_fidelity_returns_medium(self, agent):
        run = _make_run(_seal_specs())
        with patch.object(agent, "_extract_specs_from_snippet", side_effect=self._mock_extract):
            artifact = agent.run(run, self.TIER2_CANDIDATE, tier=2)
        assert artifact["fidelity"] == "medium"

    def test_medium_fidelity_extracted_fields_compared(self, agent):
        run = _make_run(_seal_specs())
        with patch.object(agent, "_extract_specs_from_snippet", side_effect=self._mock_extract):
            artifact = agent.run(run, self.TIER2_CANDIDATE, tier=2)

        shaft_row = next(c for c in artifact["comparison"] if c["field"] == "shaft_size")
        assert shaft_row["candidate_value"] == "1-3/8 inch"
        assert shaft_row["match"] == "exact"

    def test_medium_fidelity_verification_required_when_fields_missing(self, agent):
        run = _make_run(_motor_specs())  # motor has hp, voltage, phase, rpm, frame

        def _partial_extract(snippet, fields):
            # Only returns voltage; others missing
            return {"voltage": "460V", "hp": None, "phase": None, "rpm": None, "frame": None}

        with patch.object(agent, "_extract_specs_from_snippet", side_effect=_partial_extract):
            artifact = agent.run(run, self.TIER2_CANDIDATE, tier=2)

        assert artifact["compatibility_summary"] == "verification_required"
        assert "hp" in artifact["verification_required_fields"]
        assert artifact["engineer_notes"] is not None

    def test_medium_fidelity_all_visible_fit_confirmed(self, agent):
        run = _make_run(_seal_specs())
        with patch.object(agent, "_extract_specs_from_snippet", side_effect=self._mock_extract):
            artifact = agent.run(run, self.TIER2_CANDIDATE, tier=2)
        # shaft_size and material_spec both match and are visible → fit_confirmed
        assert artifact["compatibility_summary"] in ("fit_confirmed", "fit_likely")

    def test_medium_fidelity_extraction_failure_marks_unknown(self, agent):
        run = _make_run(_seal_specs())

        def _fail_extract(snippet, fields):
            return {f: None for f in fields}

        with patch.object(agent, "_extract_specs_from_snippet", side_effect=_fail_extract):
            artifact = agent.run(run, self.TIER2_CANDIDATE, tier=2)

        assert artifact["compatibility_summary"] == "verification_required"
        for row in artifact["comparison"]:
            assert row["match"] == "unknown"


# ---------------------------------------------------------------------------
# Low-fidelity — Tier 3 placeholder
# ---------------------------------------------------------------------------

class TestLowFidelity:
    TIER3_CANDIDATE = {
        "vendor_name": "Generic Seal Supply",
        "source_url":  "https://genericseals.com/products/crane-mr1",
        "base_price":  0.0,
        "price_tbd":   True,
    }

    def test_low_fidelity_returns_low(self, agent):
        run = _make_run(_seal_specs())
        artifact = agent.run(run, self.TIER3_CANDIDATE, tier=3)
        assert artifact["fidelity"] == "low"

    def test_low_fidelity_all_fields_unknown(self, agent):
        run = _make_run(_seal_specs())
        artifact = agent.run(run, self.TIER3_CANDIDATE, tier=3)
        assert all(c["match"] == "unknown" for c in artifact["comparison"])

    def test_low_fidelity_verification_required_summary(self, agent):
        run = _make_run(_seal_specs())
        artifact = agent.run(run, self.TIER3_CANDIDATE, tier=3)
        assert artifact["compatibility_summary"] == "verification_required"

    def test_low_fidelity_engineer_notes_present(self, agent):
        run = _make_run(_seal_specs())
        artifact = agent.run(run, self.TIER3_CANDIDATE, tier=3)
        assert artifact["engineer_notes"] is not None
        assert "spec sheet required" in artifact["engineer_notes"].lower()

    def test_low_fidelity_includes_vendor_url(self, agent):
        run = _make_run(_seal_specs())
        artifact = agent.run(run, self.TIER3_CANDIDATE, tier=3)
        assert "https://genericseals.com" in (artifact["engineer_notes"] or "")

    def test_low_fidelity_candidate_value_is_none(self, agent):
        run = _make_run(_seal_specs())
        artifact = agent.run(run, self.TIER3_CANDIDATE, tier=3)
        assert all(c["candidate_value"] is None for c in artifact["comparison"])
