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
