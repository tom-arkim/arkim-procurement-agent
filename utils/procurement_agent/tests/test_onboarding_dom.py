"""
Night 4 — onboarding agent tests: DOM pruner + harvester.

Offline against the live-harvested fixtures in tests/fixtures/supplier_sites/
(no live network). The harvester's fetcher is injected as a fixture-backed
callable; SSRF guards are unit-tested directly.
"""
from __future__ import annotations

import json
import os
import re

import pytest

from utils.procurement_agent.onboarding.dom import parse_html
from utils.procurement_agent.onboarding.harvester import (
    discover_pages, harvest_site, _host_is_public, _is_scheme_allowed,
    _registered_domain, MAX_PAGES,
)


_FIXTURE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))),
    "tests", "fixtures", "supplier_sites",
)


def _load_fixture_page(slug: str, rel: str) -> str:
    path = os.path.join(_FIXTURE_ROOT, slug, rel)
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _fixture_fetcher(slug: str):
    """Build a fixture-backed fetch_html for a site: reads pages from the
    manifest's url→file map (offline)."""
    manifest = json.load(
        open(os.path.join(_FIXTURE_ROOT, slug, "manifest.json"), encoding="utf-8"))

    def fetch(url: str):
        for u, fname in manifest["pages"].items():
            if url.rstrip("/").lower() == u.rstrip("/").lower():
                return open(os.path.join(_FIXTURE_ROOT, slug, fname),
                            encoding="utf-8", errors="replace").read()
        return None
    return fetch, manifest


# ---------------------------------------------------------------------------
# DOM pruner
# ---------------------------------------------------------------------------

class TestDomPruner:
    def test_drops_script_style_and_keeps_visible_text(self):
        html = ("<html><head><title>X</title><style>a{}</style></head>"
                "<body><script>var x=1</script><p>Hello <b>world</b></p>"
                "<noscript>nope</noscript></body></html>")
        p = parse_html(html, base_url="https://x.com/")
        assert "Hello world" in p.text
        assert "var x" not in p.text
        assert "nope" not in p.text
        assert p.title == "X"

    def test_drops_hidden_subtree(self):
        html = ("<body><p>visible</p>"
                '<div style="display:none"><p>hidden1</p></div>'
                '<div aria-hidden="true"><p>hidden2</p></div>'
                '<p hidden><span>hidden3</span></p></body>')
        p = parse_html(html, base_url="https://x.com/")
        assert "visible" in p.text
        assert "hidden1" not in p.text
        assert "hidden2" not in p.text
        assert "hidden3" not in p.text

    def test_collects_links_and_resolves_relative(self):
        html = ('<body><a href="/brands/">Brands</a>'
                '<a href="https://other.com/x">Other</a>'
                '<a href="/about">About Us</a></body>')
        p = parse_html(html, base_url="https://supplier.com/")
        hrefs = {h for _, h in p.links}
        assert "https://supplier.com/brands/" in hrefs
        assert "https://supplier.com/about" in hrefs
        assert "https://other.com/x" in hrefs

    def test_drops_non_http_links(self):
        html = ('<body><a href="mailto:a@b.com">mail</a>'
                '<a href="javascript:alert(1)">js</a>'
                '<a href="/ok">ok</a></body>')
        p = parse_html(html, base_url="https://supplier.com/")
        hrefs = {h for _, h in p.links}
        assert "https://supplier.com/ok" in hrefs
        assert all(not h.startswith("mailto") and not h.startswith("javascript")
                   for h in hrefs)

    def test_collects_img_alts_and_headings(self):
        html = ('<body><h1>Sup</h1><h2>Sub</h2>'
                '<img alt="Acme Corp logo"><img alt="">'
                '<img src="x.png" alt="Goulds"></body>')
        p = parse_html(html, base_url="https://x.com/")
        assert "Acme Corp logo" in p.alt_texts
        assert "Goulds" in p.alt_texts
        assert "" not in p.alt_texts
        assert "Sup" in p.headings and "Sub" in p.headings

    def test_meta_description_and_og_title(self):
        html = ('<head><meta name="description" content="we sell pumps">'
                '<meta property="og:title" content="PumpCo"></head><body>x</body>')
        p = parse_html(html, base_url="https://x.com/")
        assert p.meta_description == "we sell pumps"
        assert p.og_title == "PumpCo"

    def test_mismatched_unclosed_tags_do_not_lose_body_text(self):
        # The robustness regression: an early aria-hidden toggle must NOT bleed
        # _hidden onto the whole body. Unclosed <p>/<li> must not desync.
        html = ('<body><div aria-hidden="true"><i>menu</i></div>'
                '<p>first para unclosed'
                '<p>second para</p>'
                '<ul><li>one<li>two<li>three</ul>'
                '<p>real content here</p></body>')
        p = parse_html(html, base_url="https://x.com/")
        assert "real content here" in p.text
        assert "second para" in p.text
        assert "first para unclosed" in p.text
        # The aria-hidden menu text is dropped (it's inside the hidden div).
        assert "menu" not in p.text

    def test_never_raises_on_malformed(self):
        # Garbage / truncated HTML — must not raise.
        p = parse_html("<body><p>unclosed<<<<<", base_url="https://x.com/")
        assert "unclosed" in p.text


# ---------------------------------------------------------------------------
# SSRF guards
# ---------------------------------------------------------------------------

class TestSSRFGuards:
    def test_loopback_blocked(self):
        assert _host_is_public("http://127.0.0.1/") is False
        assert _host_is_public("http://localhost/") is False

    def test_link_local_blocked(self):
        # 169.254.169.254 is the cloud-metadata SSRF target.
        assert _host_is_public("http://169.254.169.254/latest/meta-data/") is False

    def test_private_ranges_blocked(self):
        assert _host_is_public("http://10.0.0.1/") is False
        assert _host_is_public("http://192.168.1.1/") is False
        assert _host_is_public("http://172.16.0.1/") is False

    def test_non_http_schemes_blocked(self):
        assert _is_scheme_allowed("file:///etc/passwd") is False
        assert _is_scheme_allowed("gopher://x/") is False
        assert _is_scheme_allowed("http://x/") is True
        assert _is_scheme_allowed("https://x/") is True

    def test_registered_domain_strips_www_and_subdomain(self):
        assert _registered_domain("https://www.ibtinc.com/brands/") == "ibtinc.com"
        assert _registered_domain("https://shop.ibtinc.com/x") == "ibtinc.com"


# ---------------------------------------------------------------------------
# Harvester — page discovery + bounded fetch (offline, fixtures)
# ---------------------------------------------------------------------------

class TestHarvesterFixtures:
    def test_ibt_discovers_brands_page_from_home(self):
        fetch, manifest = _fixture_fetcher("ibt")
        home = parse_html(_load_fixture_page("ibt", "home.html"),
                          base_url=manifest["home_url"])
        targets = discover_pages(home)
        # The brands/line-card page must be among the discovered targets.
        assert any("brands" in t for t in targets), targets
        # Bounded: home + discovered <= MAX_PAGES.
        assert len(targets) <= MAX_PAGES - 1

    def test_ibt_harvest_finds_home_and_brands(self):
        fetch, manifest = _fixture_fetcher("ibt")
        r = harvest_site(manifest["home_url"], fetch_html=fetch)
        urls = [p.url for p in r.pages]
        assert any("ibtinc.com" in u and u.rstrip("/").endswith(".com") is False
                   or u.rstrip("/") == "https://ibtinc.com" for u in urls)
        assert any("brands" in u for u in urls), urls
        # Bounded total.
        assert len(r.pages) <= MAX_PAGES
        # Each page has pruned text + alts (the brand signal).
        brands_page = next(p for p in r.pages if "brands" in p.url)
        assert len(brands_page.text) > 100
        assert len(brands_page.alt_texts) > 0

    def test_all_five_fixtures_harvest_home_page(self):
        """Every fixture must at least fetch its home page (fail-soft on the
        rest — discovered sub-pages may be absent if not in the fixture set)."""
        for slug in ("ibt", "lesman", "seal", "bearing", "smallshop"):
            fetch, manifest = _fixture_fetcher(slug)
            r = harvest_site(manifest["home_url"], fetch_html=fetch)
            assert len(r.pages) >= 1, f"{slug}: no pages harvested"
            assert r.pages[0].url == r.home_url
            assert len(r.pages[0].text) > 50, f"{slug}: home text too short"

    def test_same_domain_only_no_cross_domain(self):
        fetch, manifest = _fixture_fetcher("ibt")
        r = harvest_site(manifest["home_url"], fetch_html=fetch)
        home_dom = _registered_domain(manifest["home_url"])
        for p in r.pages:
            assert _registered_domain(p.url) == home_dom

    def test_discovery_drops_asset_extensions(self):
        fetch, manifest = _fixture_fetcher("ibt")
        home = parse_html(_load_fixture_page("ibt", "home.html"),
                          base_url=manifest["home_url"])
        targets = discover_pages(home)
        # No css/js/pdf/image assets among discovered page targets.
        assert all(not re.search(r"\.(css|js|png|jpg|svg|pdf)(\?|#|$)", t, re.I)
                   for t in targets), targets

    def test_no_duplicate_home_fetch_on_canonicalization(self):
        # A site whose discovered links canonicalize back to "/" must not
        # produce a duplicate home entry in the result. RBC's home page links
        # to "/Products", "/About-Us" etc.; with a fetcher that returns the
        # home HTML for every path (simulating a canonicalizing site), the
        # harvester must keep exactly ONE home row.
        home_html = _load_fixture_page("bearing", "home.html")

        def fetch(url):
            # Every URL returns the home HTML (a canonicalizing site).
            return home_html
        r = harvest_site("https://rbcbearings.com/", fetch_html=fetch)
        home_rows = [p for p in r.pages
                     if p.url.rstrip("/").lower() == "https://rbcbearings.com"]
        assert len(home_rows) == 1, [p.url for p in r.pages]

    def test_harvest_failsoft_on_unreachable_home(self):
        def fetch(url):
            return None
        r = harvest_site("https://nonexistent.example.com/", fetch_html=fetch)
        assert r.pages == []
        assert r.skipped and "home fetch failed" in r.skipped[0][1]

    def test_bounded_to_max_pages(self):
        # A home page with many same-domain nav links — confirm the harvester
        # never exceeds MAX_PAGES even when discovery finds more candidates.
        fetch, manifest = _fixture_fetcher("ibt")
        r = harvest_site(manifest["home_url"], fetch_html=fetch, max_pages=3)
        assert len(r.pages) <= 3
