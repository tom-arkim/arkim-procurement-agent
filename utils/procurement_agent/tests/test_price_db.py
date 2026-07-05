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


class TestNullPNGuard:
    """CLEANUP §7.1 — null/placeholder identities must never read or write the
    cache. The historical bug: every manufacturer-less / PN-less (spec-based)
    request collapsed to one ``unknown|UNKNOWN-PN`` bucket, so a motor page
    cached under vague-motor was served (at a stale static 50% score, no
    per-request re-scoring) on every subsequent vague query — valve, solenoid,
    hose, gearbox oil. The guard returns "" for those identities → miss + no-op.
    """

    @pytest.mark.parametrize("mfg, pn", [
        ("Unknown", "UNKNOWN-PN"),     # the literal collapse bucket (sourcing_agent._dict_to_specs defaults)
        ("unknown", "unknown-pn"),     # case-insensitive
        ("", ""),                      # both blank
        (None, None),
        ("N/A", "TBD"),
        ("Unknown", "6205-2RS"),       # null manufacturer alone → not cacheable
        ("SKF", "UNKNOWN-PN"),         # placeholder PN alone → not cacheable
        ("SKF", ""),                   # blank PN
        ("SKF", "N/A"),
    ])
    def test_placeholder_identity_neither_reads_nor_writes(self, isolated_db, mfg, pn):
        # Seed a bucket that a loose key WOULD have collided into, then prove the
        # guard prevents both the write (below) and the read (after).
        price_db.save_price(manufacturer=mfg, part_number=pn,
                            vendor_name="ShouldNeverBeStored", price=999.0)
        # No bucket created for the placeholder identity.
        assert price_db.get_cached_prices(manufacturer=mfg, part_number=pn) == {}
        # And it does NOT pollute a real key's namespace.
        assert price_db.all_entries() == {}

    @pytest.mark.parametrize("mfg, pn", [
        ("SKF", "6205-2RS"),
        ("Baldor", "EM3546T"),
        ("US Motors", "PN-SHARED"),
        ("Allen-Bradley", "22B-D010N104"),
    ])
    def test_real_identity_round_trips_unchanged(self, isolated_db, mfg, pn):
        """Real (mfg, PN) pairs still cache and read back — the guard is surgical."""
        price_db.save_price(manufacturer=mfg, part_number=pn,
                            vendor_name="MROSupply", price=123.45)
        cached = price_db.get_cached_prices(manufacturer=mfg, part_number=pn)
        assert cached["MROSupply"]["price"] == 123.45

    def test_placeholder_does_not_collide_with_real_key(self, isolated_db):
        """A real (SKF, 6205-2RS) entry is not surfaced by an (Unknown, UNKNOWN-PN) lookup."""
        price_db.save_price(manufacturer="SKF", part_number="6205-2RS",
                            vendor_name="MROSupply", price=50.0)
        # A vague query that happens to default to Unknown/UNKNOWN-PN must miss.
        assert price_db.get_cached_prices(manufacturer="Unknown", part_number="UNKNOWN-PN") == {}
