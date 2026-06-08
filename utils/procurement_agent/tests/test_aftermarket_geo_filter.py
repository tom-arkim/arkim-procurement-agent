"""
Regression: _discover_aftermarket_specialists must drop non-US results (same geo
gate as _discover_national_specialists) so suppliers like Akman
(akman2006.en.made-in-china.com) cannot reach Tier 3 via the aftermarket path.

The Tavily client and the LLM are mocked; the fake LLM faithfully echoes one
vendor per URL it is shown, so a URL that survives the geo filter ends up in the
vendor list and one that's filtered does not. This both captures the bug (fails
pre-fix) and guards against over-filtering (US result must pass).
"""

import json
import re

import pytest

import utils.sourcing_archieved as _pkg
from utils.sourcing_archieved import enterprise_search
from utils.models import AssetSpecs

US_URL = "https://www.flexaseal.com/mechanical-seals"
CN_URL = "https://akman2006.en.made-in-china.com/product/seal"


class _FakeTavily:
    def __init__(self, results):
        self._results = results

    def search(self, query, **kwargs):
        return {"results": self._results}


def _specs() -> AssetSpecs:
    return AssetSpecs(
        manufacturer="Gusher Pumps", model="Type 21", part_number="TYPE21",
        voltage="N/A", category="Part", detected_type="mechanical seal",
    )


@pytest.fixture
def aftermarket_env(monkeypatch):
    """Mock Tavily (1 US + 1 made-in-china result) and a faithful echo-LLM."""
    monkeypatch.setattr(_pkg, "_tavily", _FakeTavily([
        {"url": US_URL, "title": "Flexaseal Mechanical Seals", "content": "US mechanical seal maker"},
        {"url": CN_URL, "title": "Akman Seal", "content": "mechanical seal manufacturer"},
    ]))
    monkeypatch.setattr(_pkg, "ANTHROPIC_API_KEY", "test-key")

    captured = {}

    def _echo_llm(system, user):
        captured["user"] = user
        urls = re.findall(r"URL:\s*(\S+)", user)
        return json.dumps([
            {"name": u.split("//")[-1].split("/")[0], "website": u, "price": None, "lead_days": 7}
            for u in urls
        ])

    monkeypatch.setattr(enterprise_search, "_anthropic_complete", _echo_llm)
    return captured


def test_made_in_china_filtered_from_aftermarket(aftermarket_env):
    options = enterprise_search._discover_aftermarket_specialists(_specs(), [])
    urls = [(o.source_url or "") for o in options]

    # The non-US supplier must NOT reach the aftermarket vendor list.
    assert not any("made-in-china" in u for u in urls), f"non-US supplier leaked: {urls}"
    # It must also be filtered BEFORE the LLM (not after).
    assert "made-in-china" not in aftermarket_env.get("user", "")


def test_us_aftermarket_result_still_passes(aftermarket_env):
    """Don't over-filter: a legitimate US supplier must still come through."""
    options = enterprise_search._discover_aftermarket_specialists(_specs(), [])
    urls = [(o.source_url or "") for o in options]

    assert any("flexaseal" in u for u in urls), f"US supplier dropped: {urls}"
    assert "flexaseal" in aftermarket_env.get("user", "")
