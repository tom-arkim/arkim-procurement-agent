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
