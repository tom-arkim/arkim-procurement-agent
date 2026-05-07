"""
Spec Comparison Agent — technical comparison artifacts for non-exact-match candidates.

Brief reference: Section 3.4, Section 8.4.

Three fidelity levels based on data availability:
  High   (Tier 1): full side-by-side comparison from structured catalog spec_data.
  Medium (Tier 2): partial comparison extracted from the vendor's listing snippet
                   via Claude; gaps flagged as "verify with vendor."
  Low    (Tier 3): placeholder artifact — spec sheet required before approval.

Honesty principle: the artifact always reflects what was and wasn't verified.
No fabricated matches.
"""

import json
import os
from typing import Optional

from utils.models import SourcingRun
from utils.procurement_agent.agents.comparison_helpers import (
    compare_dimensional,
    compare_material,
    compare_categorical,
    compare_frame,
)

_TIER1_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "data", "mock_tier1_suppliers.json",
)

# Fields drawn from AssetSpecs that are compared when present.
# (field_name, field_label, comparison_type)
_SPEC_FIELD_DEFS: list[tuple[str, str, str]] = [
    ("hp",               "Horsepower",        "categorical"),
    ("voltage",          "Voltage",           "categorical"),
    ("phase",            "Phase",             "categorical"),
    ("rpm",              "RPM",               "categorical"),
    ("frame",            "Frame",             "frame"),
    ("shaft_size",       "Shaft Size",        "dimensional"),
    ("bore_diameter",    "Bore Diameter",     "dimensional"),
    ("material_spec",    "Material",          "material"),
    ("gpm",              "Flow Rate (GPM)",   "categorical"),
    ("psi",              "Pressure (PSI)",    "categorical"),
    ("connection_size",  "Connection Size",   "dimensional"),
    ("detected_type",    "Equipment Type",    "categorical"),
]

_NULL_VALUES = {None, "", "null", "N/A", "Unknown", "UNKNOWN-PN", "none", "unknown"}


class SpecComparisonAgent:
    """Generates technical comparison artifacts for a candidate vendor result."""

    def __init__(self, anthropic_api_key: Optional[str] = None):
        self._anthropic_key = anthropic_api_key

    def run(self, run: SourcingRun, candidate: dict, tier: int = 3) -> dict:
        """Compare a vendor candidate against the run's AssetSpecs.

        Args:
            run:       SourcingRun providing target AssetSpecs
            candidate: vendor result dict from SourcingAgent
            tier:      1 = Arkim Network, 2 = Marketplace, 3 = Broader Market

        Returns:
            Comparison artifact dict matching the output contract in the brief.
        """
        specs_dict = run.asset_specs_json or {}

        if tier == 1:
            return self._high_fidelity(specs_dict, candidate)
        elif tier == 2:
            return self._medium_fidelity(specs_dict, candidate)
        else:
            return self._low_fidelity(specs_dict, candidate)

    # ------------------------------------------------------------------
    # High fidelity — Tier 1 catalog spec_data
    # ------------------------------------------------------------------

    def _high_fidelity(self, specs: dict, candidate: dict) -> dict:
        vendor_name  = candidate.get("vendor_name") or "Unknown"
        found_pn     = candidate.get("found_part_number") or ""
        source_url   = candidate.get("source_url")
        candidate_id = candidate.get("_candidate_id") or found_pn or vendor_name

        spec_data = self._lookup_tier1_spec_data(vendor_name, found_pn)
        if spec_data is None:
            # Catalog match not found — fall back to low fidelity with explanation
            artifact = self._low_fidelity(specs, candidate)
            artifact["fidelity"] = "high"
            artifact["engineer_notes"] = (
                "High-fidelity comparison requested (Tier 1 vendor) but spec data "
                "was not found in the catalog for this part number. "
                "Verify specs directly with the vendor."
            )
            return artifact

        comparison, incompatible_fields, unknown_fields = self._compare_specs(specs, spec_data)
        summary = self._derive_summary_high(comparison, incompatible_fields)

        return {
            "fidelity":                    "high",
            "candidate_id":                candidate_id,
            "vendor_name":                 vendor_name,
            "candidate_url":               source_url,
            "comparison":                  comparison,
            "compatibility_summary":       summary,
            "verification_required_fields": [],
            "engineer_notes":              None,
        }

    def _lookup_tier1_spec_data(self, vendor_name: str, found_pn: str) -> Optional[dict]:
        """Find spec_data for the given vendor + part_number in the catalog."""
        try:
            with open(_TIER1_CATALOG_PATH, "r") as fh:
                catalog = json.load(fh)
        except Exception:
            return None

        vendor_lower = vendor_name.lower().strip()
        found_pn_norm = found_pn.upper().strip()

        for supplier in catalog.get("suppliers", []):
            if vendor_lower not in supplier.get("name", "").lower():
                continue
            for item in supplier.get("inventory", []):
                if item.get("part_number", "").upper().strip() == found_pn_norm:
                    return item.get("spec_data")
        return None

    def _compare_specs(self, asset_specs: dict, catalog_spec: dict) -> tuple[list, list, list]:
        """Build a comparison list from asset specs vs catalog spec_data.

        Returns (comparison_rows, incompatible_field_names, unknown_field_names).
        """
        comparison: list[dict] = []
        incompatible: list[str] = []
        unknown: list[str] = []

        for field, label, ctype in _SPEC_FIELD_DEFS:
            asset_val = asset_specs.get(field)
            cand_val  = catalog_spec.get(field)

            if asset_val in _NULL_VALUES:
                continue  # skip fields the asset doesn't specify

            if cand_val in _NULL_VALUES:
                match = "unknown"
                notes = "not specified in catalog"
                unknown.append(field)
            else:
                match, notes = self._compare_field(ctype, str(asset_val), str(cand_val))
                if match == "different":
                    incompatible.append(field)

            comparison.append({
                "field":           field,
                "field_label":     label,
                "asset_value":     str(asset_val) if asset_val not in _NULL_VALUES else None,
                "candidate_value": str(cand_val) if cand_val not in _NULL_VALUES else None,
                "match":           match,
                "notes":           notes,
            })

        return comparison, incompatible, unknown

    def _compare_field(self, ctype: str, a: str, b: str) -> tuple[str, Optional[str]]:
        """Dispatch to the appropriate comparator. Returns (match_result, notes)."""
        if ctype == "dimensional":
            result = compare_dimensional(a, b)
            notes  = f"{a} vs {b}" if result != "exact" else None
            return result, notes
        elif ctype == "material":
            result = compare_material(a, b)
            notes  = f"{a} vs {b}" if result != "exact" else None
            return result, notes
        elif ctype == "frame":
            result = compare_frame(a, b)
            notes  = f"{a} vs {b}" if result != "exact" else None
            return result, notes
        else:
            result = compare_categorical(a, b)
            notes  = f"{a} vs {b}" if result != "exact" else None
            return result, notes

    @staticmethod
    def _derive_summary_high(comparison: list, incompatible: list) -> str:
        if incompatible:
            return "incompatible"
        compatible_count = sum(1 for c in comparison if c["match"] == "compatible")
        if compatible_count > 0:
            return "fit_likely"
        if all(c["match"] in ("exact", "unknown") for c in comparison):
            unknown_count = sum(1 for c in comparison if c["match"] == "unknown")
            if unknown_count > 0:
                return "fit_likely"
            return "fit_confirmed"
        return "fit_confirmed"

    # ------------------------------------------------------------------
    # Medium fidelity — Tier 2 snippet extraction via Claude
    # ------------------------------------------------------------------

    def _medium_fidelity(self, specs: dict, candidate: dict) -> dict:
        vendor_name  = candidate.get("vendor_name") or "Unknown"
        source_url   = candidate.get("source_url")
        snippet      = candidate.get("snippet") or candidate.get("notes") or ""
        candidate_id = candidate.get("_candidate_id") or vendor_name

        # Determine which spec fields are relevant (non-null in asset specs)
        relevant_fields = [
            (field, label, ctype)
            for field, label, ctype in _SPEC_FIELD_DEFS
            if specs.get(field) not in _NULL_VALUES
        ]

        if not relevant_fields:
            return self._low_fidelity(specs, candidate)

        # Extract spec values from snippet via Claude
        extracted = self._extract_specs_from_snippet(
            snippet, [field for field, _, _ in relevant_fields]
        )

        comparison: list[dict] = []
        verification_required: list[str] = []

        for field, label, ctype in relevant_fields:
            asset_val = specs.get(field)
            cand_val  = extracted.get(field)

            if cand_val in _NULL_VALUES or cand_val is None:
                comparison.append({
                    "field":           field,
                    "field_label":     label,
                    "asset_value":     str(asset_val),
                    "candidate_value": None,
                    "match":           "unknown",
                    "notes":           "not visible in listing — verify with vendor",
                })
                verification_required.append(field)
            else:
                match, notes = self._compare_field(ctype, str(asset_val), str(cand_val))
                if match in ("different", "unknown"):
                    verification_required.append(field)
                comparison.append({
                    "field":           field,
                    "field_label":     label,
                    "asset_value":     str(asset_val),
                    "candidate_value": str(cand_val),
                    "match":           match,
                    "notes":           notes,
                })

        all_visible    = not verification_required
        any_different  = any(c["match"] == "different" for c in comparison)

        if any_different:
            summary = "incompatible"
        elif all_visible:
            has_compatible = any(c["match"] == "compatible" for c in comparison)
            summary = "fit_likely" if has_compatible else "fit_confirmed"
        else:
            summary = "verification_required"

        engineer_notes = (
            None if all_visible
            else (
                "Some fields could not be verified from the vendor's listing. "
                "Confirm the following with the vendor before approval: "
                + ", ".join(verification_required) + "."
            )
        )

        return {
            "fidelity":                    "medium",
            "candidate_id":                candidate_id,
            "vendor_name":                 vendor_name,
            "candidate_url":               source_url,
            "comparison":                  comparison,
            "compatibility_summary":       summary,
            "verification_required_fields": verification_required,
            "engineer_notes":              engineer_notes,
        }

    def _extract_specs_from_snippet(self, snippet: str, fields: list[str]) -> dict:
        """Use Claude to extract spec values from a Tavily listing snippet.

        Returns a dict of {field: value | None}. Missing fields are None.
        Isolated for mocking in tests.
        """
        if not snippet or not fields:
            return {f: None for f in fields}

        field_list = ", ".join(fields)
        prompt = (
            f"Extract the following technical specifications from this product listing. "
            f"Return ONLY valid JSON with these exact keys: {field_list}.\n"
            f"Use null for any field not mentioned. Do not add commentary.\n\n"
            f"Listing:\n{snippet[:2000]}"
        )

        try:
            import anthropic
            client = anthropic.Anthropic(
                api_key=self._anthropic_key or os.environ.get("ANTHROPIC_API_KEY", "")
            )
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except Exception as exc:
            print(f"[SpecComparisonAgent] Snippet extraction failed: {exc}")
            return {f: None for f in fields}

    # ------------------------------------------------------------------
    # Low fidelity — Tier 3 placeholder
    # ------------------------------------------------------------------

    def _low_fidelity(self, specs: dict, candidate: dict) -> dict:
        vendor_name  = candidate.get("vendor_name") or "Unknown"
        source_url   = candidate.get("source_url")
        candidate_id = candidate.get("_candidate_id") or vendor_name

        relevant_fields = [
            (field, label)
            for field, label, _ in _SPEC_FIELD_DEFS
            if specs.get(field) not in _NULL_VALUES
        ]

        comparison = [
            {
                "field":           field,
                "field_label":     label,
                "asset_value":     str(specs[field]),
                "candidate_value": None,
                "match":           "unknown",
                "notes":           "not available — request from vendor",
            }
            for field, label in relevant_fields
        ]

        return {
            "fidelity":                    "low",
            "candidate_id":                candidate_id,
            "vendor_name":                 vendor_name,
            "candidate_url":               source_url,
            "comparison":                  comparison,
            "compatibility_summary":       "verification_required",
            "verification_required_fields": [f for f, _ in relevant_fields],
            "engineer_notes": (
                "This is a low-fidelity comparison — spec sheet required from vendor "
                "before approval. The vendor's catalog page is at "
                f"{source_url or '[URL not available]'} — contact them via "
                "Arkim's quote request workflow to obtain detailed specs."
            ),
        }
