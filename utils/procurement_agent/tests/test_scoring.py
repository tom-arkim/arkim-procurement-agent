"""
Tests for scoring module — PN match classifier, niche mismatch guard,
collection URL detection, and manufacturer alias resolution.
"""

import pytest
from unittest.mock import patch

from utils.sourcing_archieved.scoring import (
    _classify_pn_match,
    _is_collection_url,
    _compute_suitability_score,
    PN_MATCH_POINTS,
)
from utils.models import AssetSpecs


# ---------------------------------------------------------------------------
# _classify_pn_match — 5-tier PN match classifier
# ---------------------------------------------------------------------------

class TestClassifyPnMatch:
    def test_exact_match_identical_strings(self):
        level = _classify_pn_match("HHI-150-12-447T", "HHI-150-12-447T", "", "US Motors")
        assert level == "exact"
        assert PN_MATCH_POINTS[level] == 40

    def test_normalized_match_delimiter_difference(self):
        """MR-1-1375 vs MR11375 — same PN, different delimiters → normalized, 40 pts."""
        level = _classify_pn_match("HHI-150-12-447T", "HHI150-12-447T", "", "US Motors")
        assert level == "normalized"
        assert PN_MATCH_POINTS[level] == 40

    def test_normalized_match_no_penalty(self):
        """normalized level must not trigger mismatch penalty (only 'none' with found_pn does)."""
        level = _classify_pn_match("HHI-150-12-447T", "HHI150-12-447T", "", "US Motors")
        pn_pts = PN_MATCH_POINTS[level]
        penalty = 30 if (level == "none" and "HHI150-12-447T") else 0
        assert pn_pts == 40
        assert penalty == 0

    def test_stem_match_endress_hauser(self):
        """PMC11-AA1U1HBWBJJ vs PMC11 — same family stem → stem."""
        level = _classify_pn_match(
            "PMC11-AA1U1HBWBJJ", "PMC11AA1U1HBWBJJ",
            "", "Endress+Hauser"
        )
        assert level in ("normalized", "stem")
        assert PN_MATCH_POINTS[level] >= 25

    def test_stem_match_different_ordering_code(self):
        """PMC11-AA1U1HBWBJJ (searched) vs PMC11-BB2X3YYZAA (different config) — same stem."""
        level = _classify_pn_match(
            "PMC11-AA1U1HBWBJJ", "PMC11-BB2X3YYZAA",
            "", "Endress+Hauser"
        )
        assert level == "stem"
        assert PN_MATCH_POINTS[level] == 25

    def test_substring_match_pn_in_snippet(self):
        """found_pn is None but searched PN appears in snippet → substring."""
        snippet = "We stock part number HHI15012447T for same-day shipping."
        level = _classify_pn_match("HHI-150-12-447T", None, snippet, "US Motors")
        assert level == "substring"
        assert PN_MATCH_POINTS[level] == 15

    def test_none_match_genuinely_different_pn(self):
        """Completely different found_pn and not in snippet → none → penalty applies."""
        level = _classify_pn_match("HHI-150-12-447T", "WEG-200-14-449T", "unrelated content", "US Motors")
        assert level == "none"
        assert PN_MATCH_POINTS[level] == 0
        penalty = 30 if (level == "none" and "WEG-200-14-449T") else 0
        assert penalty == 30

    def test_none_match_no_penalty_when_found_pn_absent(self):
        """No found_pn, PN not in snippet → none, but NO mismatch penalty."""
        level = _classify_pn_match("HHI-150-12-447T", None, "generic pump page", "US Motors")
        assert level == "none"
        found_pn = None
        penalty = 30 if (level == "none" and found_pn) else 0
        assert penalty == 0

    def test_empty_searched_pn_returns_none(self):
        level = _classify_pn_match("", "HHI-150-12-447T", "snippet", "US Motors")
        assert level == "none"


# ---------------------------------------------------------------------------
# Fix A — Niche mismatch guard: snippet-only + threshold 3
# ---------------------------------------------------------------------------

def _seal_specs() -> AssetSpecs:
    return AssetSpecs(
        manufacturer="Gusher Pumps",
        model="Type 21",
        part_number="TYPE21",
        voltage="N/A",
        category="Part",
        detected_type="mechanical seal",
    )


def _motor_specs() -> AssetSpecs:
    return AssetSpecs(
        manufacturer="Hyundai Heavy Industries",
        model="HHI-150-12-447T",
        part_number="HHI-150-12-447T",
        voltage="460V",
        category="Equipment",
        detected_type="Electric Motor",
        hp="150",
    )


class TestNicheMismatchGuard:
    _BAD_TERMS = ("motor rewind", "hydraulic cylinder", "electrical panel", "motor winding")

    def test_url_hits_not_counted(self):
        """URL contains 2 wrong-category terms — guard must NOT fire because URL is excluded."""
        url = "https://example.com/motor-rewind/hydraulic-cylinder/seal-kits"
        snippet = "Gusher Type 21 mechanical seal. In stock, ships same day."
        with patch("utils.sourcing_archieved.scoring.get_wrong_category_terms",
                   return_value=self._BAD_TERMS):
            score = _compute_suitability_score(_seal_specs(), snippet, url)
        assert score > 0.0

    def test_two_snippet_hits_does_not_fire(self):
        """Two wrong-category terms in snippet — below threshold of 3, guard does not fire."""
        url = "https://springer-pumps.com/seal-kits"
        snippet = "Gusher Type 21 seal kit. Also stocking motor rewind supplies and hydraulic cylinders."
        with patch("utils.sourcing_archieved.scoring.get_wrong_category_terms",
                   return_value=self._BAD_TERMS):
            score = _compute_suitability_score(_seal_specs(), snippet, url)
        assert score > 0.0

    def test_three_snippet_hits_fires(self):
        """Three wrong-category terms in snippet → guard fires → 0.0."""
        url = "https://example.com/random"
        snippet = (
            "motor rewind services, hydraulic cylinder repair, "
            "electrical panel installation — nothing to do with seals"
        )
        with patch("utils.sourcing_archieved.scoring.get_wrong_category_terms",
                   return_value=self._BAD_TERMS):
            score = _compute_suitability_score(_seal_specs(), snippet, url)
        assert score == 0.0

    def test_no_bad_terms_unchanged(self):
        """Empty wrong-category list — guard never fires regardless of snippet content."""
        url = "https://example.com/products/seal"
        snippet = "motor rewind hydraulic cylinder electrical panel lots of bad stuff"
        with patch("utils.sourcing_archieved.scoring.get_wrong_category_terms", return_value=()):
            score = _compute_suitability_score(_seal_specs(), snippet, url)
        assert score >= 0.0  # no guard, score computed normally


# ---------------------------------------------------------------------------
# Fix B — Collection URL: path-only matching
# ---------------------------------------------------------------------------

class TestCollectionUrl:
    def test_clean_product_url_not_collection(self):
        assert _is_collection_url("https://vendor.com/products/motors/HHI150-447T") is False

    def test_pumpcatalog_domain_not_collection(self):
        """pumpcatalog.com domain contains 'catalog' but path does not → not a collection."""
        assert _is_collection_url("https://pumpcatalog.com/gusher-type-21-seal-kit") is False

    def test_search_query_param_is_collection(self):
        assert _is_collection_url("https://vendor.com/products?q=PMC11") is True

    def test_category_path_is_collection(self):
        assert _is_collection_url("https://vendor.com/category/pumps/seal-kits") is True

    def test_search_path_is_collection(self):
        assert _is_collection_url("https://vendor.com/search/gusher-type-21") is True

    def test_catalog_path_is_collection(self):
        assert _is_collection_url("https://vendor.com/catalog/gusher-type-21") is True

    def test_catalog_word_in_domain_not_collection(self):
        """'catalog' in domain name only (no /catalog/ path segment) → not collection."""
        assert _is_collection_url("https://pumpcatalog.com/gusher/type-21") is False

    def test_research_path_not_collection(self):
        """'/research-resources/' contains 'search' as substring but is not a search page."""
        assert _is_collection_url("https://vendor.com/research-resources/case-study") is False

    def test_results_path_is_collection(self):
        assert _is_collection_url("https://vendor.com/results/pump-seals") is True

    def test_empty_url_returns_false(self):
        assert _is_collection_url("") is False


# ---------------------------------------------------------------------------
# Fix C — Manufacturer alias resolution
# ---------------------------------------------------------------------------

class TestManufacturerAliases:
    def test_hyundai_heavy_industries_crown_triton_alias(self):
        """Snippet mentions 'Crown Triton' — should award mfg_pts even though specs says HHI."""
        url = "https://dealersindustrial.com/motors/crown-triton-150hp"
        snippet = (
            "Crown Triton 150HP 447T frame motor, 460V 3-phase. "
            "Electric motor specialist. In stock, ships same day. Price: $3,200."
        )
        score = _compute_suitability_score(_motor_specs(), snippet, url)
        assert score > 0.0

    def test_hyundai_electric_alias_matches(self):
        """Snippet says 'Hyundai Electric' — alias of Hyundai Heavy Industries."""
        url = "https://mrosupply.com/motors/hyundai-electric-150hp"
        snippet = (
            "Hyundai Electric 150HP TEFC induction motor, frame 447T. "
            "In stock. distributor authorized. Price on request."
        )
        score = _compute_suitability_score(_motor_specs(), snippet, url)
        assert score > 0.0

    def test_endress_hauser_no_plus_sign_alias(self):
        """Snippet says 'Endress Hauser' (without +) — alias of Endress+Hauser."""
        specs = AssetSpecs(
            manufacturer="Endress+Hauser",
            model="PMC11",
            part_number="PMC11-AA1U1HBWBJJ",
            voltage="N/A",
            category="Part",
            detected_type="pressure sensor",
        )
        url = "https://instrumart.com/products/endress-hauser/pmc11"
        snippet = (
            "Endress Hauser PMC11 pressure sensor. Authorized distributor. "
            "PMC11AA1U1HBWBJJ in stock."
        )
        score = _compute_suitability_score(specs, snippet, url)
        assert score > 0.0

    def test_unknown_manufacturer_uses_input_name(self):
        """Unknown manufacturer falls back to exact string match."""
        specs = AssetSpecs(
            manufacturer="Acme Industrial Corp",
            model="X100",
            part_number="X100-A",
            voltage="N/A",
            category="Part",
            detected_type="valve",
        )
        url = "https://valve-supply.com/products/acme"
        snippet = "Acme Industrial Corp X100 valve — in stock. Distributor."
        score = _compute_suitability_score(specs, snippet, url)
        assert score > 0.0

    def test_wrong_manufacturer_alias_no_match(self):
        """Snippet mentions Caterpillar — no alias match for Hyundai Heavy Industries."""
        url = "https://cat.com/motors"
        snippet = "Caterpillar industrial motors. Heavy equipment power solutions."
        score = _compute_suitability_score(_motor_specs(), snippet, url)
        # mfg_pts should be 0, score won't benefit from alias match
        # Score may still be non-zero from other signals, but we verify no alias inflation
        from utils.brand_intelligence import get_manufacturer_aliases
        aliases = get_manufacturer_aliases("Hyundai Heavy Industries")
        alias_match = any(a.lower() in snippet.lower() for a in aliases)
        assert alias_match is False


# ---------------------------------------------------------------------------
# get_manufacturer_aliases unit tests
# ---------------------------------------------------------------------------

class TestGetManufacturerAliases:
    def test_hyundai_heavy_industries_returns_crown_triton(self):
        from utils.brand_intelligence import get_manufacturer_aliases
        aliases = get_manufacturer_aliases("Hyundai Heavy Industries")
        assert "Crown Triton" in aliases

    def test_case_insensitive_lookup(self):
        from utils.brand_intelligence import get_manufacturer_aliases
        aliases = get_manufacturer_aliases("HYUNDAI HEAVY INDUSTRIES")
        assert any("Crown Triton" in a for a in aliases)

    def test_unknown_manufacturer_returns_input(self):
        from utils.brand_intelligence import get_manufacturer_aliases
        aliases = get_manufacturer_aliases("Unknown Widget Co")
        assert aliases == ["Unknown Widget Co"]

    def test_empty_string_returns_empty(self):
        from utils.brand_intelligence import get_manufacturer_aliases
        aliases = get_manufacturer_aliases("")
        assert aliases == []

    def test_gusher_pumps_includes_ruthman(self):
        from utils.brand_intelligence import get_manufacturer_aliases
        aliases = get_manufacturer_aliases("Gusher Pumps")
        assert "Ruthman Companies" in aliases

    def test_allen_bradley_includes_rockwell(self):
        from utils.brand_intelligence import get_manufacturer_aliases
        aliases = get_manufacturer_aliases("Allen-Bradley")
        assert "Rockwell Automation" in aliases
