"""
utils/search_providers.py — ONE swappable search-provider interface (CLAUDE.md §9).

Two providers satisfy the same `search(...) -> list[dict]` contract so the sourcing
pipeline can be pointed at either by config (SEARCH_PROVIDER), defaulting to Tavily:

  - TavilyProvider   : WRAPS the existing Tavily client (utils/sourcing_archieved._tavily,
                       late-bound so key-patching still applies) — behaviour-identical,
                       not a rewrite.
  - ParallelProvider : NEW adapter for the Parallel.ai Search API (POST /v1/search,
                       x-api-key auth, objective + search_queries, multi-excerpt results).

Both are FAIL-SOFT / NO-OP without a key: a missing key / error / timeout returns [] and
logs — it NEVER raises into the sourcing pipeline. Parallel is a PAID API; this adapter
only calls out when a key is present, and live calls otherwise live only in the opt-in
A/B probe (scripts/parallel_ab_probe.py). Secrets are read from os.environ, never .env.

Common result shape (a dict): {url, title, content, ...}. Tavily already yields
url/title/content; Parallel ADDS its richer fields (excerpts[] preserved, publish_date)
and a joined `content` for parity with the Tavily field downstream code reads.
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Protocol

import httpx

log = logging.getLogger(__name__)

# Env names (read at call time; never read .env).
ENV_PARALLEL_API_KEY = "PARALLEL_API_KEY"
ENV_PARALLEL_BASE_URL = "PARALLEL_BASE_URL"
ENV_PARALLEL_MODE = "PARALLEL_SEARCH_MODE"
ENV_SEARCH_PROVIDER = "SEARCH_PROVIDER"

_PARALLEL_DEFAULT_BASE_URL = "https://api.parallel.ai"
_PARALLEL_TIMEOUT_S = 30.0   # Parallel can be slower than Tavily; sane upper bound.

# Heuristic anti-bot / junk excerpt signatures (case-insensitive substring). An excerpt
# matching any is DROPPED before it becomes `content` — these are page-block walls, not
# document text, and would poison downstream authority scoring + price extraction (seen
# live: Parallel returned a "Pardon Our Interruption" wall as the only excerpt for a
# gouldspumps PDF). Tunable — extend as new phrasings appear. Drops the EXCERPT, NEVER
# the URL (the candidate may still be valid; only the excerpt is noise).
_BLOCKED_EXCERPT_SIGNATURES: tuple[str, ...] = (
    "pardon our interruption",
    "are you a robot",
    "enable javascript and cookies",
    "verify you are human",
    "access denied",
    "request blocked",
)


def _usable_excerpt(text: Optional[str]) -> bool:
    """False for empty/whitespace/'...' or a known anti-bot wall; True for real text."""
    if not text:
        return False
    t = str(text).strip()
    if not t or t == "...":
        return False
    tl = t.lower()
    return not any(sig in tl for sig in _BLOCKED_EXCERPT_SIGNATURES)


class SearchProvider(Protocol):
    """The swappable contract. `search_depth` (Tavily) and `objective` (Parallel) are
    accepted by both for interface uniformity; each provider uses what applies and
    ignores the rest. Returns a list of common-shape result dicts (never raises)."""
    def search(self, query: str, *, max_results: int = 10,
               search_depth: Optional[str] = None,
               include_domains: Optional[list[str]] = None,
               objective: Optional[str] = None) -> list[dict]: ...


# ---------------------------------------------------------------------------
# Tavily — wrap the existing client (don't rewrite). Behaviour-identical.
# ---------------------------------------------------------------------------

class TavilyProvider:
    def __init__(self, client=None) -> None:
        # An explicit client (tests / the probe) wins; otherwise resolved lazily.
        self._client = client

    def _resolve_client(self):
        """The live, key-patched Tavily client if present (utils/sourcing_archieved._tavily,
        read at call time); else construct one from TAVILY_API_KEY; else None (no-op)."""
        if self._client is not None:
            return self._client
        try:
            import utils.sourcing_archieved as _arch
            if getattr(_arch, "_tavily", None):
                return _arch._tavily
        except Exception:
            pass
        key = os.environ.get("TAVILY_API_KEY")
        if not key:
            return None
        try:
            from tavily import TavilyClient
            return TavilyClient(api_key=key)
        except Exception:
            return None

    def search(self, query: str, *, max_results: int = 10,
               search_depth: Optional[str] = None,
               include_domains: Optional[list[str]] = None,
               objective: Optional[str] = None) -> list[dict]:
        client = self._resolve_client()
        if client is None:
            log.info("[search.tavily] no client/key — no-op (returning [])")
            return []
        # Pass through EXACTLY what the caller specifies; omit unset params so the SDK's
        # own defaults are preserved (e.g. a call site that doesn't set search_depth keeps
        # Tavily's default rather than being forced to 'advanced').
        kw: dict = {"query": query, "max_results": max_results}
        if search_depth is not None:
            kw["search_depth"] = search_depth
        if include_domains is not None:
            kw["include_domains"] = include_domains
        try:
            resp = client.search(**kw)
        except Exception as exc:
            log.warning("[search.tavily] error: %s", exc)
            return []
        return resp.get("results", []) if isinstance(resp, dict) else (resp or [])


# ---------------------------------------------------------------------------
# Parallel.ai — new adapter (POST /v1/search, x-api-key).
# ---------------------------------------------------------------------------

class ParallelProvider:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 mode: Optional[str] = None, timeout: float = _PARALLEL_TIMEOUT_S) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get(ENV_PARALLEL_API_KEY)
        self._base_url = (base_url or os.environ.get(ENV_PARALLEL_BASE_URL)
                          or _PARALLEL_DEFAULT_BASE_URL).rstrip("/")
        self._mode = mode if mode is not None else os.environ.get(ENV_PARALLEL_MODE)
        self._timeout = timeout
        # Diagnostics side-channel (usage/warnings/search_id from the last 200 response) —
        # the normalized search() return is just results; the A/B probe reads this for the
        # cost/usage signal. Per-instance, not used by the sourcing path.
        self.last_meta: dict = {}

    def search(self, query: str, *, max_results: int = 10,
               search_depth: Optional[str] = None,   # accepted for interface parity; unused
               include_domains: Optional[list[str]] = None,
               objective: Optional[str] = None) -> list[dict]:
        if not self._api_key:
            log.info("[search.parallel] PARALLEL_API_KEY unset — no-op (returning [])")
            return []

        # Parallel wants BOTH a semantic goal and keyword queries — send both.
        body: dict = {
            "objective": objective or query,
            "search_queries": [query],
        }
        if self._mode:
            body["mode"] = self._mode
        advanced: dict = {"max_results": max_results}
        if include_domains:
            advanced["source_policy"] = {"include_domains": list(include_domains)}
        body["advanced_settings"] = advanced

        try:
            resp = httpx.post(
                f"{self._base_url}/v1/search",
                json=body,
                headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                log.warning("[search.parallel] HTTP %s: %s",
                            resp.status_code, getattr(resp, "text", "")[:300])
                return []
            data = resp.json()
        except Exception as exc:  # timeout / network / decode — fail-soft, never raise
            log.warning("[search.parallel] request failed: %s", exc)
            return []

        self.last_meta = {
            "search_id": data.get("search_id"),
            "usage":     data.get("usage"),
            "warnings":  data.get("warnings"),
        }
        out: list[dict] = []
        for r in (data.get("results") or []):
            raw_excerpts = r.get("excerpts") or []
            # Drop anti-bot walls / empty excerpts so they never reach `content` (the
            # field downstream authority-scoring + price-extraction read).
            usable = [e for e in raw_excerpts if _usable_excerpt(e)]
            # "Result present, excerpt unusable": keep the URL (a valid candidate) with
            # EMPTY content — never fabricate text, never drop the URL over a bad excerpt.
            unusable = bool(raw_excerpts) and not usable
            if unusable:
                log.info("[search.parallel] excerpt for %s was anti-bot/empty — content blanked",
                         r.get("url"))
            out.append({
                "url":            r.get("url"),
                "title":          r.get("title"),
                # Common field for parity with Tavily's single `content` (downstream
                # authority scoring reads it) — joined from the USABLE markdown excerpts.
                "content":        "\n\n".join(usable),
                # Parallel's richer fields, PRESERVED (not flattened to one snippet) —
                # only the noise excerpts are removed.
                "excerpts":       usable,
                "publish_date":   r.get("publish_date"),
                "excerpt_unusable": unusable,
            })
        return out


# ---------------------------------------------------------------------------
# Provider selection — config/runtime, default Tavily (no behaviour change).
# ---------------------------------------------------------------------------

def get_search_provider(name: Optional[str] = None) -> SearchProvider:
    """Return the configured provider. `name` (or SEARCH_PROVIDER env) selects it;
    default is Tavily so existing sourcing behaviour is unchanged unless explicitly
    switched. Unknown names fall back to Tavily."""
    choice = (name or os.environ.get(ENV_SEARCH_PROVIDER) or "tavily").strip().lower()
    if choice == "parallel":
        return ParallelProvider()
    return TavilyProvider()
