"""
Tests for utils.price_db cache keying.

Regression test for CLEANUP.md §3.3: prices were keyed by part number ONLY, so
two manufacturers' parts that share a part number collided and the cache could
silently return the wrong manufacturer's price. The key is now
(manufacturer, part_number).
"""
import pytest

from utils import price_db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point price_db at a throwaway file so tests never touch the real cache."""
    monkeypatch.setattr(price_db, "_DB_PATH", str(tmp_path / "price_db.json"))


class TestManufacturerScopedCache:
    def test_same_pn_different_manufacturer_does_not_collide(self, isolated_db):
        """Same part number, two manufacturers, two prices — each must round-trip."""
        price_db.save_price(
            manufacturer="US Motors", part_number="PN-SHARED",
            vendor_name="MROSupply", price=100.0,
        )
        price_db.save_price(
            manufacturer="Hyundai", part_number="PN-SHARED",
            vendor_name="MROSupply", price=500.0,
        )

        us = price_db.get_cached_prices(manufacturer="US Motors", part_number="PN-SHARED")
        hy = price_db.get_cached_prices(manufacturer="Hyundai", part_number="PN-SHARED")

        assert us["MROSupply"]["price"] == 100.0, \
            "US Motors price was overwritten by Hyundai's — PN collision"
        assert hy["MROSupply"]["price"] == 500.0

    def test_lookup_does_not_leak_other_manufacturers_vendors(self, isolated_db):
        """A lookup for one manufacturer must not surface another manufacturer's vendors."""
        price_db.save_price(manufacturer="US Motors", part_number="PN-SHARED",
                            vendor_name="VendorA", price=100.0)
        price_db.save_price(manufacturer="Hyundai", part_number="PN-SHARED",
                            vendor_name="VendorB", price=500.0)

        us = price_db.get_cached_prices(manufacturer="US Motors", part_number="PN-SHARED")
        assert set(us) == {"VendorA"}
        assert "VendorB" not in us
