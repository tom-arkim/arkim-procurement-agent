"""
Tests for tavily_client geographic filtering (Fix 2).
"""

import pytest

from utils.sourcing_archieved.tavily_client import _vendor_authority_score, NON_US_TLDS, NON_US_DOMAIN_HINTS


class TestGeographicFiltering:
    def test_chinese_tld_rejected(self):
        """Vendors on .cn TLD must score 0."""
        score = _vendor_authority_score("https://antlets.cn/product/pump-seal", "buy now in stock", "Antlets")
        assert score == 0.0

    def test_antlets_domain_hint_rejected(self):
        """antlets.com is in NON_US_DOMAIN_HINTS and must score 0 even on .com."""
        score = _vendor_authority_score("https://antlets.com/pump-parts", "add to cart industrial", "Antlets")
        assert score == 0.0

    def test_made_in_china_domain_rejected(self):
        score = _vendor_authority_score("https://made-in-china.com/product/12345", "price buy", "Supplier")
        assert score == 0.0

    def test_us_industrial_vendor_not_rejected(self):
        """Legitimate US vendor on .com should pass geographic filter and score > 0."""
        score = _vendor_authority_score(
            "https://motionsolutions.com/product/bearing-abc",
            "add to cart in stock industrial distributor",
            "Motion Solutions",
        )
        assert score > 0.0

    def test_blacklisted_domain_still_rejected(self):
        """Existing blacklist (_BLACKLISTED_DOMAINS) must still work after geo filter added."""
        score = _vendor_authority_score("https://amazon.com/dp/B001234", "add to cart", "Amazon")
        assert score == 0.0

    def test_non_us_tlds_constant_has_expected_entries(self):
        assert ".cn" in NON_US_TLDS
        assert ".de" in NON_US_TLDS

    def test_non_us_domain_hints_constant_has_expected_entries(self):
        assert "antlets" in NON_US_DOMAIN_HINTS
        assert "indiamart" in NON_US_DOMAIN_HINTS


# ---------------------------------------------------------------------------
# Fix 2 — Nordic / Central European TLD extension
# ---------------------------------------------------------------------------

class TestNordicTldBlocking:
    """Nordic and Central European TLDs must score 0 via _vendor_authority_score."""

    def test_digikey_se_rejected(self):
        score = _vendor_authority_score(
            "https://www.digikey.se/products/en/sensors-transducers/pmc11",
            "add to cart in stock SEK EUR",
            "DigiKey Sweden",
        )
        assert score == 0.0

    def test_mouser_se_rejected(self):
        score = _vendor_authority_score(
            "https://www.mouser.se/ProductDetail/PMC11",
            "add to cart in stock industrial",
            "Mouser Electronics SE",
        )
        assert score == 0.0

    def test_digikey_no_rejected(self):
        score = _vendor_authority_score(
            "https://www.digikey.no/products/en/pmc11",
            "add to cart in stock NOK",
            "DigiKey Norway",
        )
        assert score == 0.0

    def test_fi_tld_rejected(self):
        score = _vendor_authority_score(
            "https://example.fi/industrial/sensor",
            "add to cart industrial distributor",
            "Finnish Vendor",
        )
        assert score == 0.0

    def test_dk_tld_rejected(self):
        score = _vendor_authority_score(
            "https://rs-online.dk/products/sensor",
            "in stock add to cart distributor",
            "RS Components DK",
        )
        assert score == 0.0

    def test_at_tld_rejected(self):
        score = _vendor_authority_score(
            "https://example.at/products/seal",
            "add to cart in stock",
            "Austrian Vendor",
        )
        assert score == 0.0

    def test_ch_tld_rejected(self):
        score = _vendor_authority_score(
            "https://mouser.ch/products/sensor",
            "in stock CHF EUR",
            "Mouser Switzerland",
        )
        assert score == 0.0

    def test_se_in_non_us_tlds_constant(self):
        assert ".se" in NON_US_TLDS

    def test_no_in_non_us_tlds_constant(self):
        assert ".no" in NON_US_TLDS

    def test_fi_in_non_us_tlds_constant(self):
        assert ".fi" in NON_US_TLDS

    def test_dk_in_non_us_tlds_constant(self):
        assert ".dk" in NON_US_TLDS

    def test_digikey_com_still_passes(self):
        """US .com DigiKey must not be blocked by the Nordic extension."""
        score = _vendor_authority_score(
            "https://www.digikey.com/products/en/sensors/pmc11",
            "add to cart in stock industrial",
            "DigiKey",
        )
        assert score > 0.0

    def test_digikey_ca_still_passes(self):
        """Canadian .ca must remain unblocked per spec."""
        score = _vendor_authority_score(
            "https://www.digikey.ca/products/en/sensors/pmc11",
            "add to cart in stock CAD industrial",
            "DigiKey Canada",
        )
        assert score > 0.0


# ---------------------------------------------------------------------------
# Fix 3 — Authorized distributor anchoring for Part category
# ---------------------------------------------------------------------------

class TestPartCategoryAuthAnchor:
    """Tier 3 query for Parts must include authorized distributor names when available."""

    def test_eh_pmc11_query_includes_representative_names(self):
        """PMC11 (Part) Tier 3 query must anchor on E+H US Representatives."""
        from utils.sourcing_archieved.tavily_client import _build_tier3_query
        from utils.models import AssetSpecs

        specs = AssetSpecs(
            manufacturer="Endress+Hauser",
            model="PMC11",
            part_number="PMC11-AA1V1HFVXJA",
            voltage="N/A",
            category="Part",
            detected_type="pressure sensor",
        )
        query = _build_tier3_query(specs)
        # At least one E+H US Representative must appear in the query
        rep_names = ["Carotek", "TriNova", "Eastern Controls", "Vector Controls"]
        assert any(rep in query for rep in rep_names), (
            f"Expected at least one E+H US Representative in query. Got: {query!r}"
        )

    def test_gusher_seal_query_includes_distributors(self):
        """Gusher Type 21 (Part) Tier 3 query must anchor on Gusher distributors."""
        from utils.sourcing_archieved.tavily_client import _build_tier3_query
        from utils.models import AssetSpecs

        specs = AssetSpecs(
            manufacturer="Gusher Pumps",
            model="Type 21",
            part_number="TYPE21",
            voltage="N/A",
            category="Part",
            detected_type="mechanical seal",
        )
        query = _build_tier3_query(specs)
        gusher_dists = ["Phoenix Pumps", "Anderson Process", "OTC Industrial",
                        "Great Lakes Pump", "Wagner Process"]
        assert any(d in query for d in gusher_dists), (
            f"Expected at least one Gusher distributor in query. Got: {query!r}"
        )

    def test_unknown_manufacturer_part_uses_generic_anchor(self):
        """Part query for unknown manufacturer must still work without auth anchoring."""
        from utils.sourcing_archieved.tavily_client import _build_tier3_query
        from utils.models import AssetSpecs

        specs = AssetSpecs(
            manufacturer="Unknown Widget Co",
            model="X100",
            part_number="X100-A",
            voltage="N/A",
            category="Part",
            detected_type="valve",
        )
        query = _build_tier3_query(specs)
        assert "authorized distributor" in query
        assert "USA" in query


# ---------------------------------------------------------------------------
# Fix 4 — Brand intelligence seeded data
# ---------------------------------------------------------------------------

class TestBrandIntelligenceSeedData:
    """get_brand_relationships() must return seeded E+H and other manufacturer data."""

    def test_endress_hauser_plus_sign_returns_representatives(self):
        from utils.brand_intelligence import get_brand_relationships
        result = get_brand_relationships("Endress+Hauser", "pressure sensor")
        reps = result.get("authorized_service_brands") or []
        assert "Carotek" in reps
        assert "TriNova" in reps
        assert "Eastern Controls" in reps

    def test_endress_hauser_space_variant_returns_representatives(self):
        from utils.brand_intelligence import get_brand_relationships
        result = get_brand_relationships("Endress Hauser", "pressure sensor")
        reps = result.get("authorized_service_brands") or []
        assert len(reps) >= 5

    def test_eh_short_form_returns_representatives(self):
        from utils.brand_intelligence import get_brand_relationships
        result = get_brand_relationships("E+H", "pressure sensor")
        reps = result.get("authorized_service_brands") or []
        assert "Carotek" in reps

    def test_eh_competitors_populated(self):
        from utils.brand_intelligence import get_brand_relationships
        result = get_brand_relationships("Endress+Hauser", "pressure sensor")
        comps = result.get("common_competitors") or []
        assert any(c in comps for c in ["WIKA", "Honeywell", "Yokogawa"])

    def test_gusher_returns_authorized_distributors(self):
        from utils.brand_intelligence import get_brand_relationships
        result = get_brand_relationships("Gusher Pumps", "mechanical seal")
        reps = result.get("authorized_service_brands") or []
        assert "Phoenix Pumps" in reps
        assert "Anderson Process" in reps

    def test_john_crane_returns_authorized_distributors(self):
        from utils.brand_intelligence import get_brand_relationships
        result = get_brand_relationships("John Crane", "mechanical seal")
        reps = result.get("authorized_service_brands") or []
        assert "Crane Engineering" in reps
        assert "Tencarva Machinery" in reps

    def test_hyundai_returns_authorized_distributors(self):
        from utils.brand_intelligence import get_brand_relationships
        result = get_brand_relationships("Hyundai Heavy Industries", "motor")
        reps = result.get("authorized_service_brands") or []
        assert "Gainesville Industrial Electric" in reps
        assert "Houston Motor & Control" in reps

    def test_unknown_manufacturer_returns_empty_authorized_brands(self):
        from utils.brand_intelligence import get_brand_relationships
        result = get_brand_relationships("Acme Widget Corp", "valve")
        # Should not crash; authorized_service_brands is [] or populated by LLM
        assert "authorized_service_brands" in result

    def test_eh_us_representatives_constant_has_eleven_entries(self):
        from utils.brand_intelligence import EH_US_REPRESENTATIVES
        assert len(EH_US_REPRESENTATIVES) == 11
        assert "Vector Controls and Automation Group" in EH_US_REPRESENTATIVES
