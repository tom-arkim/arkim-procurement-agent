"""
Tests for utils/marketplace_registry — the curated transactable-marketplace allowlist.
Detection must match the registered domain robustly (www., subdomain, path, scheme)
but never over-match a different domain that merely contains a registered string.
"""

from utils.marketplace_registry import is_marketplace


class TestIsMarketplace:
    def test_registered_domain_matches(self):
        assert is_marketplace("https://www.grainger.com/product/123")
        assert is_marketplace("grainger.com")                       # bare domain, no scheme
        assert is_marketplace("https://sealit123.com/oem-pump-seals/brands-f-p/gusher")
        assert is_marketplace("HTTPS://Zoro.com/X")                 # case-insensitive

    def test_subdomain_matches(self):
        assert is_marketplace("https://shop.grainger.com/x")

    def test_unregistered_does_not_match(self):
        assert not is_marketplace("https://industrialpumpparts.com/products/type-21")
        assert not is_marketplace("https://gaddiscompany.com/x")

    def test_does_not_over_match(self):
        # A different domain that merely contains a registered string must NOT match.
        assert not is_marketplace("https://notgrainger.com/x")
        assert not is_marketplace("https://grainger.com.evil.com/x")

    def test_empty_or_malformed(self):
        assert not is_marketplace("")
        assert not is_marketplace(None)
        assert not is_marketplace("not a url")
