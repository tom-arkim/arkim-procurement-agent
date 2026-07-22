"""
utils/url_normalize.py — canonicalize a full URL for cross-source de-duplication.

Complements (does NOT duplicate) supplier_registry._normalize_domain, which is
HOST-ONLY (lowercase host, strip www). This is the FULL-URL canonicalizer used to tell
whether two result URLs from different search engines point at the same page — e.g.
Parallel returns `…/cartridge-seal/?srsltid=ABC` while Tavily returns `…/cartridge-seal`;
both must collapse to one key.

What it does:
  - lowercase scheme + host (paths are case-sensitive in general, so the path is left
    as-is apart from the trailing-slash trim);
  - strip TRACKING query params only (srsltid, utm_*, gclid/gbraid/wbraid, fbclid,
    msclkid, mc_cid/mc_eid, _ga) — NOT the whole query string. Rationale: for a dedupe
    key, dropping the whole query risks OVER-collapsing genuinely distinct products that
    differ only by a meaningful param (e.g. ?id=1 vs ?id=2). The same product from two
    engines carries the same meaningful params, so tracking-only strip still dedupes
    correctly and never loses a real candidate. (Full-strip is a one-line change if ever
    wanted — see _strip_query.)
  - drop the fragment;
  - trim a trailing slash from the path.

Pure / standalone (stdlib only, no project imports → no cycles). Idempotent. Never
raises: empty/None → "", a malformed URL → returned stripped (best-effort).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Tracking/click-attribution params with no bearing on page identity. Tunable. utm_* is
# matched by prefix (below); the rest are exact (case-insensitive).
_TRACKING_PARAMS: frozenset[str] = frozenset({
    "srsltid",                    # Google Shopping (seen on Parallel's results)
    "gclid", "gbraid", "wbraid",  # Google Ads
    "fbclid",                     # Facebook
    "msclkid",                    # Microsoft Ads
    "mc_cid", "mc_eid",           # Mailchimp
    "_ga",                        # Google Analytics linker
})


def _is_tracking(key: str) -> bool:
    k = key.lower()
    return k in _TRACKING_PARAMS or k.startswith("utm_")


def _strip_query(query: str) -> str:
    """Keep only non-tracking params. (Swap to `return ""` for full-strip if ever wanted.)"""
    kept = [(k, v) for (k, v) in parse_qsl(query, keep_blank_values=True) if not _is_tracking(k)]
    return urlencode(kept)


# Common two-part public suffixes for the registrable-domain heuristic. NOT the
# full PSL (no new dependency — MATCHING_CLEANUP F3); covers the ccTLD patterns
# realistically seen in sourcing results. Extend as needed.
_TWO_PART_SUFFIXES: frozenset[str] = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk",
    "com.au", "net.au", "org.au",
    "co.jp", "co.in", "co.nz", "co.za", "co.kr",
    "com.br", "com.mx", "com.cn", "com.tw", "com.sg", "com.hk",
})

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def registrable_domain(url_or_host: str) -> str:
    """The registrable domain (eTLD+1 heuristic) of a URL or bare host, for
    vendor-level de-duplication: `www.globalindustrial.com`,
    `static.globalindustrial.com` and `catalog.globalindustrial.com` all →
    `globalindustrial.com` (MATCHING_CLEANUP F3 — subdomain variants of one
    vendor must key identically).

    Heuristic: last two labels, or last three when the trailing two form a known
    two-part public suffix (shop.example.co.uk → example.co.uk). IPv4 literals
    and single-label hosts are returned as-is. Returns "" for empty/unparseable
    input. Never raises. Pure stdlib (no project imports, no cycles).
    """
    if not url_or_host or not isinstance(url_or_host, str):
        return ""
    raw = url_or_host.strip().lower()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw if "://" in raw else f"https://{raw}")
        host = parts.hostname or ""
    except ValueError:
        host = ""
    if not host:
        # Bare-host fallback for inputs urlsplit rejects: strip any path piece.
        host = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        host = host.split("@")[-1].split(":", 1)[0]
    host = host.strip(".")
    if not host:
        return ""
    if _IPV4_RE.match(host):
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _TWO_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def normalize_url(url: str) -> str:
    """Canonical form of a URL for de-duplication. Returns "" for empty/None; returns the
    stripped input unchanged if it can't be parsed. Idempotent."""
    if not url or not isinstance(url, str):
        return ""
    raw = url.strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        path = parts.path.rstrip("/")            # trailing-slash trim ("/" -> "")
        return urlunsplit((
            parts.scheme.lower(),
            parts.netloc.lower(),
            path,
            _strip_query(parts.query),
            "",                                  # drop fragment
        ))
    except Exception:
        return raw
