"""
utils/sourcing/market_confidence.py
Market Confidence Score: web-sourced reliability rating via Tavily + Claude.
"""

import json
import re
from typing import Optional

from utils.sourcing.llm_parsing import _anthropic_complete


_RELIABILITY_SYSTEM = """You are an industrial reliability analyst.
Given web search results about a specific component, assess its market reliability reputation.

Return ONLY valid JSON (no prose):
{
  "market_confidence_score": <integer 1-10>,
  "summary": "<one sentence>",
  "common_failures": "<brief comma-separated list or null>"
}

Scoring guide:
  10   : Outstanding MTBF, no documented failure patterns
  8-9  : Very reliable, minor isolated issues
  6-7  : Acceptable, some known failure patterns in the field
  4-5  : Below average reliability, notable concerns
  1-3  : Poor, widespread failures or product discontinuation

If no reliability information is found, return:
{"market_confidence_score": 6, "summary": "No reliability data found in web sources.", "common_failures": null}
"""


def _fetch_market_confidence(specs) -> Optional[float]:
    """Search the web for reliability/MTBF data on this specific brand+model.
    Returns a 1-10 Market Confidence Score, or None if unavailable.
    """
    import utils.sourcing as _pkg

    if not _pkg._tavily or not _pkg.ANTHROPIC_API_KEY:
        return None
    brand = specs.manufacturer if specs.manufacturer not in ("Unknown", "N/A", "null") else ""
    model = specs.model        if specs.model        not in ("Unknown", "N/A", "null") else ""
    if not (brand or model):
        return None

    query = f"{brand} {model} reliability MTBF common failures field reports".strip()
    print(f"[Sourcing] Market confidence query: {query!r}")
    try:
        resp    = _pkg._tavily.search(query=query, search_depth="basic", max_results=5)
        results = resp.get("results", [])
        if not results:
            return None
        snippets = "\n---\n".join(
            f"Title: {r.get('title','')}\nContent: {r.get('content','')}"
            for r in results
        )
        raw = _anthropic_complete(
            _RELIABILITY_SYSTEM,
            f"Component: {brand} {model}\n\n{snippets}"
        )
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            d = json.loads(m.group(0))
            score = d.get("market_confidence_score")
            if score is not None:
                s = float(score)
                print(f"[Sourcing] Market confidence {brand} {model}: {s:.1f}/10 — {d.get('summary','')}")
                return s
    except Exception as exc:
        print(f"[Sourcing] Market confidence error: {exc}")
    return None
