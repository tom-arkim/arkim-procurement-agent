"""
Tests for utils/search_providers.py — the swappable search-provider interface.

ALL MOCKED — no network, no spend. The Parallel adapter's HTTP layer (httpx.post) is
monkeypatched; the Tavily wrapper takes an injected fake client. Asserts: no-op without
a key, the /v1/search request shape (x-api-key + objective + search_queries), the
response mapping (excerpts PRESERVED, not collapsed; publish_date; joined content),
fail-soft on 422/timeout/network, the Tavily wrapper passes params through unchanged,
and provider selection (default Tavily).
"""

import pytest

from utils import search_providers
from utils.search_providers import ParallelProvider, TavilyProvider, get_search_provider


# ---------------------------------------------------------------------------
# Parallel adapter
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class TestParallelProvider:
    def test_no_key_is_noop(self, monkeypatch):
        monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
        called = {"n": 0}
        monkeypatch.setattr(search_providers.httpx, "post",
                            lambda *a, **k: called.__setitem__("n", called["n"] + 1))
        assert ParallelProvider().search("pump seal") == []   # no key -> [] ...
        assert called["n"] == 0                                # ... and no HTTP call

    def test_maps_200_response_and_request_shape(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(url=url, json=json, headers=headers, timeout=timeout)
            return _Resp(200, {"search_id": "s", "results": [
                {"url": "https://grainger.com/p", "title": "Pump Seal",
                 "publish_date": "2024-01-02",
                 "excerpts": ["Excerpt one.", "Excerpt two."]},
            ]})

        monkeypatch.setattr(search_providers.httpx, "post", fake_post)
        out = ParallelProvider(api_key="k", base_url="https://api.parallel.ai").search(
            "pump seal", max_results=5, include_domains=["grainger.com"])

        # Request shape (the confirmed /v1/search contract).
        assert captured["url"] == "https://api.parallel.ai/v1/search"
        assert captured["headers"]["x-api-key"] == "k"          # x-api-key, not Bearer
        assert captured["json"]["objective"] == "pump seal"     # semantic goal
        assert captured["json"]["search_queries"] == ["pump seal"]  # keyword form — both sent
        assert captured["json"]["advanced_settings"]["max_results"] == 5
        assert captured["json"]["advanced_settings"]["source_policy"]["include_domains"] == ["grainger.com"]

        # Response mapping.
        r = out[0]
        assert r["url"] == "https://grainger.com/p" and r["title"] == "Pump Seal"
        assert r["excerpts"] == ["Excerpt one.", "Excerpt two."]    # PRESERVED, not collapsed
        assert "Excerpt one." in r["content"] and "Excerpt two." in r["content"]  # joined parity field
        assert r["publish_date"] == "2024-01-02"

    def test_captures_usage_meta_for_probe(self, monkeypatch):
        monkeypatch.setattr(search_providers.httpx, "post", lambda *a, **k: _Resp(200, {
            "search_id": "sid", "results": [], "usage": [{"name": "search", "count": 1}],
            "warnings": None}))
        p = ParallelProvider(api_key="k")
        p.search("x")
        assert p.last_meta["search_id"] == "sid"
        assert p.last_meta["usage"] == [{"name": "search", "count": 1}]   # cost signal for the A/B probe

    def test_sends_mode_when_configured(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(search_providers.httpx, "post",
                            lambda url, json=None, **k: (captured.update(json=json), _Resp(200, {"results": []}))[1])
        ParallelProvider(api_key="k", mode="base").search("x")
        assert captured["json"]["mode"] == "base"

    def _post_results(self, monkeypatch, results):
        monkeypatch.setattr(search_providers.httpx, "post",
                            lambda *a, **k: _Resp(200, {"results": results}))

    def test_drops_anti_bot_excerpt_keeps_url(self, monkeypatch):
        # The live gouldspumps case: the ONLY excerpt is a bot wall. Keep the URL,
        # blank the content, no wall text leaks into `content`.
        self._post_results(monkeypatch, [
            {"url": "https://goulds.com/iom.pdf", "title": "IOM",
             "excerpts": ["# Pardon Our Interruption\nAs you were browsing..."]}])
        r = ParallelProvider(api_key="k").search("x")[0]
        assert r["url"] == "https://goulds.com/iom.pdf"   # URL preserved (valid candidate)
        assert r["content"] == "" and r["excerpts"] == []
        assert "pardon our interruption" not in r["content"].lower()
        assert r["excerpt_unusable"] is True

    def test_drops_empty_and_ellipsis_excerpts(self, monkeypatch):
        self._post_results(monkeypatch, [{"url": "u", "title": "t", "excerpts": ["...", "   "]}])
        r = ParallelProvider(api_key="k").search("x")[0]
        assert r["content"] == "" and r["excerpt_unusable"] is True

    def test_mixed_excerpts_keep_clean_drop_walls(self, monkeypatch):
        self._post_results(monkeypatch, [
            {"url": "u", "title": "t",
             "excerpts": ["Real price $42.", "Access Denied", "In stock."]}])
        r = ParallelProvider(api_key="k").search("x")[0]
        assert r["excerpts"] == ["Real price $42.", "In stock."]   # only the wall dropped
        assert "access denied" not in r["content"].lower()
        assert r["excerpt_unusable"] is False                      # had usable excerpts

    def test_clean_excerpts_unchanged(self, monkeypatch):
        self._post_results(monkeypatch, [{"url": "u", "title": "t",
                                          "excerpts": ["Clean one.", "Clean two."]}])
        r = ParallelProvider(api_key="k").search("x")[0]
        assert r["excerpts"] == ["Clean one.", "Clean two."] and r["excerpt_unusable"] is False

    def test_422_returns_empty(self, monkeypatch):
        monkeypatch.setattr(search_providers.httpx, "post",
                            lambda *a, **k: _Resp(422, {"type": "error"}, text='{"type":"error"}'))
        assert ParallelProvider(api_key="k").search("x") == []

    def test_timeout_returns_empty(self, monkeypatch):
        def boom(*a, **k):
            raise search_providers.httpx.TimeoutException("slow")
        monkeypatch.setattr(search_providers.httpx, "post", boom)
        assert ParallelProvider(api_key="k").search("x") == []   # fail-soft, no raise

    def test_network_error_returns_empty(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("connection reset")
        monkeypatch.setattr(search_providers.httpx, "post", boom)
        assert ParallelProvider(api_key="k").search("x") == []


# ---------------------------------------------------------------------------
# Tavily wrapper — passthrough, behaviour-identical
# ---------------------------------------------------------------------------

class _FakeTavily:
    def __init__(self, results=None):
        self.calls = []
        self._results = results if results is not None else [{"url": "u", "title": "t", "content": "c"}]

    def search(self, **kw):
        self.calls.append(kw)
        return {"results": self._results}


class TestTavilyProvider:
    def test_wraps_client_passthrough(self):
        fake = _FakeTavily()
        out = TavilyProvider(client=fake).search(
            "query", max_results=15, search_depth="advanced", include_domains=["a.com"])
        assert out == [{"url": "u", "title": "t", "content": "c"}]   # returns the results list
        assert fake.calls[0] == {"query": "query", "max_results": 15,
                                 "search_depth": "advanced", "include_domains": ["a.com"]}

    def test_omits_unset_params(self):
        # A call site that doesn't set search_depth/include_domains keeps Tavily's own
        # defaults — the wrapper must NOT inject them (behaviour-preservation).
        fake = _FakeTavily()
        TavilyProvider(client=fake).search("q", max_results=5)
        assert fake.calls[0] == {"query": "q", "max_results": 5}

    def test_no_client_no_key_is_noop(self, monkeypatch):
        monkeypatch.setattr(TavilyProvider, "_resolve_client", lambda self: None)
        assert TavilyProvider().search("q") == []

    def test_client_error_is_failsoft(self):
        class _Boom:
            def search(self, **kw):
                raise RuntimeError("tavily down")
        assert TavilyProvider(client=_Boom()).search("q") == []


# ---------------------------------------------------------------------------
# Provider selection — default Tavily
# ---------------------------------------------------------------------------

class TestProviderSelection:
    def test_default_is_tavily(self, monkeypatch):
        monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
        assert isinstance(get_search_provider(), TavilyProvider)

    def test_explicit_tavily(self, monkeypatch):
        monkeypatch.setenv("SEARCH_PROVIDER", "tavily")
        assert isinstance(get_search_provider(), TavilyProvider)

    def test_parallel_selected(self, monkeypatch):
        monkeypatch.setenv("SEARCH_PROVIDER", "parallel")
        assert isinstance(get_search_provider(), ParallelProvider)

    def test_unknown_falls_back_to_tavily(self, monkeypatch):
        monkeypatch.setenv("SEARCH_PROVIDER", "bogus")
        assert isinstance(get_search_provider(), TavilyProvider)

    def test_explicit_name_overrides_env(self, monkeypatch):
        monkeypatch.setenv("SEARCH_PROVIDER", "tavily")
        assert isinstance(get_search_provider("parallel"), ParallelProvider)
