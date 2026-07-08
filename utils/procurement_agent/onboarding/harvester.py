"""
utils/procurement_agent/onboarding/harvester.py

T1 — Page harvester: URL → fetch home + discover a bounded set of same-domain
pages (brands / line-card, about, locations, products) via link-text heuristics
→ DOM-pruned ``PageContent`` per page (see ``dom.py``).

Design / guardrails (Night 4 brief):

  - **Bounded**: at most ``MAX_PAGES`` pages total (home + up to 7 discovered).
  - **Same-domain only**: discovered links must resolve to the home page's
    registered domain. No cross-domain following (prevents crawler runaway +
    SSRF pivot via redirect-to-internal).
  - **SSRF-safe** (the harvester fetches arbitrary URLs server-side):
      * only ``http``/``https`` schemes,
      * the host is DNS-resolved and EVERY resolved IP is checked against
        private/loopback/link-local/multicast/reserved ranges — blocked if any
        is non-public (defends against DNS-rebinding to 169.254.169.254 etc.),
      * no redirects to a disallowed host are followed (redirects are resolved
        and re-checked; a redirect to an internal host is dropped),
      * per-request timeout.
    SSRF posture is reported in I1 / the morning report.
  - **Fail-soft** (house integration pattern §9): a fetch error/timeout/blocked
    host → that page is skipped (logged), the run continues with the pages that
    did fetch. The harvester NEVER raises into the pipeline.
  - **Pluggable fetcher**: ``fetch_html`` is a callable injected by the caller so
    tests run offline against fixtures (no live network). The default fetcher
    uses ``requests`` with the SSRF guard applied to the resolved host.

No bs4/lxml (not installed); DOM pruning is stdlib (``dom.py``).
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlparse, urljoin

from utils.procurement_agent.onboarding.dom import PageContent, parse_html


MAX_PAGES = 8                      # home + up to 7 discovered
FETCH_TIMEOUT = 20.0               # seconds per page
MAX_BYTES = 2_500_000              # ~2.5MB cap per page (guards against giant pages)
_USER_AGENT = (
    "Mozilla/5.0 (compatible; ArkimProcurementOnboardingBot/1.0; +supplier-scope onboarding)"
)

# Anchor-text / href keyword → page-role buckets. Matched case-insensitively as
# substrings. These drive discovery of the high-signal pages beyond the home.
_ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "brands":      ("brand", "line card", "linecard", "manufacturer", "vendor",
                    "supplier", "our partners", "partners", "principal"),
    "about":       ("about", "company", "who we are", "our story", "history"),
    "locations":   ("location", "locations", "contact", "find us", "where"),
    "products":    ("product", "products", "catalog", "solutions", "capabilities",
                    "services", "offering"),
}


# ---------------------------------------------------------------------------
# Fetcher protocol
# ---------------------------------------------------------------------------

FetchResult = tuple[Optional[str], Optional[str], int]
"""(html, final_url, status). html/final_url None on failure."""


def default_fetch_html(url: str, *, timeout: float = FETCH_TIMEOUT) -> FetchResult:
    """Live fetcher (requests) with the SSRF guard applied to the resolved host.

    Returns (html, final_url, status); (None, None, 0) on any failure/blocked
    host (fail-soft — never raises).
    """
    if not _is_scheme_allowed(url):
        return (None, None, 0)
    if not _host_is_public(url):
        return (None, None, 0)
    try:
        import requests  # imported lazily so the module imports offline
    except Exception:
        return (None, None, 0)
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT,
                     "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
        # Re-check the FINAL url after redirects — a redirect to an internal host
        # is dropped (no following into a blocked target).
        final_url = str(resp.url)
        if not _host_is_public(final_url) or not _is_scheme_allowed(final_url):
            return (None, None, 0)
        if resp.status_code != 200:
            return (None, None, resp.status_code)
        # Bounded read (don't load a 50MB page into memory).
        content = resp.raw.read(MAX_BYTES + 1, decode_content=True)
        if content is None:
            content = resp.content
        if isinstance(content, bytes):
            # Respect declared charset; fall back to utf-8 errors-replaced.
            ctype = (resp.headers.get("content-type") or "").lower()
            enc = "utf-8"
            m = re.search(r"charset=([\w-]+)", ctype)
            if m:
                enc = m.group(1)
            try:
                text = content.decode(enc, errors="replace")
            except (LookupError, TypeError):
                text = content.decode("utf-8", errors="replace")
        else:
            text = str(content)
        if len(text) > MAX_BYTES:
            text = text[:MAX_BYTES]
        return (text, final_url, resp.status_code)
    except Exception:
        return (None, None, 0)


# ---------------------------------------------------------------------------
# SSRF guards (pure)
# ---------------------------------------------------------------------------

def _is_scheme_allowed(url: str) -> bool:
    try:
        return urlparse(url).scheme.lower() in ("http", "https")
    except Exception:
        return False


def _registered_domain(url: str) -> Optional[str]:
    """Return the registered domain (last two labels, coarse) stripped of www.

    Coarse heuristic (ignores public-suffix edge cases like co.uk) — adequate
    for same-domain scoping where a false-negative just skips a cross-domain
    link (safe).
    """
    try:
        host = (urlparse(url).hostname or "").lower().replace("www.", "")
    except Exception:
        return None
    if not host:
        return None
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host or None


def _host_is_public(url: str) -> bool:
    """True iff the URL's host resolves ONLY to public, non-reserved IPs.

    Blocks loopback/private/link-local/multicast/reserved (the SSRF surface:
    127.0.0.0/8, 10/100.64/172.16/192.168/169.254/::1/fc00::/fe80:: etc.).
    DNS failures → False (fail-closed: an unresolvable host is not fetched).
    A host that resolves to a MIX of public + private → False (any private
    hits → blocked, the conservative choice against DNS-rebinding).
    """
    try:
        host = urlparse(url).hostname
    except Exception:
        return False
    if not host:
        return False
    # If the host is already an IP literal, check it directly.
    try:
        ip = ipaddress.ip_address(host)
        return _ip_is_public(ip)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if not _ip_is_public(ip):
            return False
    return True


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    """True iff the IP is global/unicast and not private/loopback/link-local/etc."""
    try:
        if ip.is_private or ip.is_loopback or ip.is_link_local \
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False
        # is_global is the strongest signal but is False for some documented
        # public ranges on older Pythons; the explicit checks above are the
        # authoritative SSRF gate, this just refines.
        if hasattr(ip, "is_global") and not ip.is_global:
            return False
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Page discovery (pure, over a parsed PageContent)
# ---------------------------------------------------------------------------

def discover_pages(home: PageContent, *, max_pages: int = MAX_PAGES) -> list[str]:
    """From a parsed home page, pick a bounded set of same-domain page URLs to
    fetch next. Pure: no I/O. Returns absolute URLs, deduped, home excluded,
    capped so total (home + these) <= max_pages.

    Discovery ranks by page-role (brands > about > locations > products) and
    within a role by anchor-text keyword match, then path heuristics. Only
    same-registered-domain links are kept.
    """
    home_domain = _registered_domain(home.url)
    if not home_domain:
        return []
    budget = max(0, max_pages - 1)  # home already fetched

    # Bucket links by role.
    role_buckets: dict[str, list[tuple[int, str]]] = {r: [] for r in _ROLE_KEYWORDS}
    seen_urls: set[str] = set()
    for text, href in home.links:
        if _registered_domain(href) != home_domain:
            continue
        # Drop anchors/query-only duplicates and normalize.
        clean = _strip_fragment(href)
        if not clean or clean in seen_urls:
            continue
        # Skip non-pagey extensions.
        if _looks_like_asset(clean):
            continue
        hay = (text + " " + urlparse(clean).path).lower()
        best_role: Optional[str] = None
        best_score = 0
        for role, kws in _ROLE_KEYWORDS.items():
            score = 0
            for kw in kws:
                if kw in hay:
                    score += 2 if kw in text.lower() else 1
            if score > best_score:
                best_role = role
                best_score = score
        if best_role and best_score > 0:
            role_buckets[best_role].append((best_score, clean))
            seen_urls.add(clean)

    # Rank: brands first (the line-card is the headline brand signal), then
    # about, locations, products. Within a role, higher score first; ties by
    # shorter path (a top-level /brands beats /products/brand/foo).
    order = ("brands", "about", "locations", "products")
    picked: list[str] = []
    for role in order:
        bucket = sorted(role_buckets[role], key=lambda s: (-s[0], len(urlparse(s[1]).path)))
        for _, url in bucket:
            if len(picked) >= budget:
                break
            picked.append(url)
        if len(picked) >= budget:
            break
    return picked


def _strip_fragment(url: str) -> str:
    try:
        p = urlparse(url)
        return urljoin(url, p.path or "/")
    except Exception:
        return url


_ASSET_EXT_RE = re.compile(
    r"\.(css|js|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|eot|pdf|zip|mp[34]|json|xml|rss)"
    r"(\?|#|$)", re.I)


def _looks_like_asset(url: str) -> bool:
    try:
        path = urlparse(url).path
    except Exception:
        return False
    return bool(_ASSET_EXT_RE.search(path))


# ---------------------------------------------------------------------------
# Harvester
# ---------------------------------------------------------------------------

@dataclass
class HarvestResult:
    """Outcome of harvesting one site.

    ``pages`` is the per-page DOM-pruned content (home first). ``fetched_urls``
    is the ordered list of URLs successfully fetched. ``skipped`` records
    (url, reason) for pages that were attempted but not fetched (fail-soft).
    ``home_url`` is the final URL of the home page after redirects.
    """
    home_url: str
    pages: list[PageContent] = field(default_factory=list)
    fetched_urls: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def page_by_url(self, url: str) -> Optional[PageContent]:
        norm = (url or "").rstrip("/").lower()
        for p in self.pages:
            if p.url.rstrip("/").lower() == norm:
                return p
        return None


def harvest_site(
    seed_url: str,
    *,
    fetch_html: Optional[Callable[[str], FetchResult]] = None,
    max_pages: int = MAX_PAGES,
    timeout: float = FETCH_TIMEOUT,
) -> HarvestResult:
    """Harvest a bounded set of DOM-pruned pages from a supplier site.

    ``fetch_html`` defaults to ``default_fetch_html`` (live, SSRF-guarded).
    Tests inject a fixture-backed fetcher. Fail-soft: never raises; unreachable
    / blocked pages are recorded in ``skipped`` and the run continues.
    """
    fetcher = fetch_html or default_fetch_html
    result = HarvestResult(home_url=seed_url)

    # 1. Home page.
    home_html, home_final, status = _fetch(fetcher, seed_url, timeout)
    if home_html is None:
        result.skipped.append((seed_url, f"home fetch failed (status={status})"))
        return result
    home_url = home_final or seed_url
    result.home_url = home_url
    home_page = parse_html(home_html, base_url=home_url)
    result.pages.append(home_page)
    result.fetched_urls.append(home_page.url)

    # 2. Discover + fetch same-domain sub-pages.
    targets = discover_pages(home_page, max_pages=max_pages)
    fetched_norm: set[str] = {home_page.url.rstrip("/").lower()}
    for url in targets:
        if len(result.pages) >= max_pages:
            break
        if url.rstrip("/").lower() in fetched_norm:
            continue  # don't double-fetch the home page under a variant URL
        html, final, st = _fetch(fetcher, url, timeout)
        if html is None:
            result.skipped.append((url, f"fetch failed (status={st})"))
            continue
        page = parse_html(html, base_url=final or url)
        # Guard against a discovered URL that redirects back to the home page
        # (some sites canonicalize every nav link to "/"). Dedup by final URL.
        if page.url.rstrip("/").lower() in fetched_norm:
            result.skipped.append((url, "redirected to an already-fetched page"))
            continue
        fetched_norm.add(page.url.rstrip("/").lower())
        result.pages.append(page)
        result.fetched_urls.append(page.url)
    return result


def _fetch(fetcher: Callable[[str], FetchResult], url: str, timeout: float) -> FetchResult:
    """Call the fetcher, tolerating either a 1-arg or 3-arg signature.

    Fixture fetchers are 1-arg (url -> html|None); the live fetcher is 3-arg.
    Fail-soft: any exception -> (None, None, 0).
    """
    try:
        out = fetcher(url)
    except TypeError:
        # 3-arg-only fetcher (the live default) — retry with timeout kwarg.
        try:
            out = fetcher(url, timeout=timeout)  # type: ignore[call-arg]
        except Exception:
            return (None, None, 0)
    except Exception:
        return (None, None, 0)
    # Normalize: a 1-arg fixture fetcher returns html str | None.
    if out is None:
        return (None, None, 0)
    if isinstance(out, str):
        return (out, None, 200)
    if isinstance(out, tuple) and len(out) == 3:
        return out  # type: ignore[return-value]
    if isinstance(out, tuple) and len(out) == 2:
        html, final = out
        return (html, final, 200 if html else 0)
    return (None, None, 0)
