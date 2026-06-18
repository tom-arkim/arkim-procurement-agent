"""
Tests for utils/url_normalize.normalize_url — full-URL canonicalization for cross-source
de-duplication (complements supplier_registry._normalize_domain, which is host-only).
"""

from utils.url_normalize import normalize_url


class TestNormalizeUrl:
    def test_strips_srsltid_tracking_param(self):
        # The real Parallel-vs-Tavily case.
        assert normalize_url("https://3ppumps.com/x/cartridge-seal/?srsltid=AfmBOoX") \
            == "https://3ppumps.com/x/cartridge-seal"

    def test_parallel_and_tavily_forms_collapse(self):
        parallel = "https://3ppumps.com/x/cartridge-seal/?srsltid=ABC"
        tavily = "https://3ppumps.com/x/cartridge-seal"
        assert normalize_url(parallel) == normalize_url(tavily)

    def test_strips_utm_and_other_trackers(self):
        u = "https://ex.com/p?utm_source=g&utm_medium=cpc&gclid=1&fbclid=2&msclkid=3"
        assert normalize_url(u) == "https://ex.com/p"

    def test_keeps_meaningful_query_params(self):
        # A product id in the query is NOT tracking — keep it (don't over-collapse).
        assert normalize_url("https://shop.com/item?id=42&srsltid=X") == "https://shop.com/item?id=42"

    def test_distinct_query_ids_do_not_collapse(self):
        assert normalize_url("https://shop.com/item?id=1") != normalize_url("https://shop.com/item?id=2")

    def test_trailing_slash_removed(self):
        assert normalize_url("https://ex.com/a/b/") == "https://ex.com/a/b"

    def test_scheme_and_host_lowercased_path_preserved(self):
        # Host case is irrelevant; path case is preserved (paths can be case-sensitive).
        assert normalize_url("HTTPS://Ex.COM/Goulds-3196") == "https://ex.com/Goulds-3196"

    def test_fragment_dropped(self):
        assert normalize_url("https://ex.com/p#section") == "https://ex.com/p"

    def test_bare_url_unchanged(self):
        assert normalize_url("https://ex.com/p") == "https://ex.com/p"

    def test_root_trailing_slash_collapses(self):
        assert normalize_url("https://ex.com/") == normalize_url("https://ex.com")

    def test_idempotent(self):
        for u in ("https://3ppumps.com/x/cartridge-seal/?srsltid=ABC",
                  "HTTPS://Ex.COM/A/B/?utm_source=g#frag",
                  "https://shop.com/item?id=42",
                  "https://ex.com/"):
            once = normalize_url(u)
            assert normalize_url(once) == once

    def test_empty_and_none_return_empty_string(self):
        assert normalize_url("") == ""
        assert normalize_url(None) == ""        # type: ignore[arg-type]
        assert normalize_url("   ") == ""

    def test_malformed_does_not_raise(self):
        # Best-effort: never raises; returns something (stripped input or a parsed form).
        assert isinstance(normalize_url("not a url"), str)
        assert isinstance(normalize_url("http://["), str)
