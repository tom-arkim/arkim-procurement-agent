"""
utils/sourcing/llm_parsing.py
Anthropic HTTP wrapper and LLM-based snippet parsing.

_anthropic_complete  : low-level HTTP call; reads ANTHROPIC_API_KEY and
                       _EXTRACTION_MODEL from the package at call time so that
                       _patch_sourcing_keys() in chat_app.py takes effect.
_llm_parse_results   : batch-parses Tavily search result snippets into
                       structured pricing dicts.
"""

import json
import re
import requests


_PARSE_SYSTEM = """You are a procurement data extractor for industrial parts and equipment.
Given web search result snippets, extract pricing and shipping information.

Input snippets are wrapped in <snippet id="N"> tags.
Return ONLY valid JSON — a list of objects with these exact keys:
  original_snippet_id : integer — the id attribute of the <snippet> tag this item was extracted from
  vendor            : string — normalize to one of: "Grainger", "McMaster-Carr", "MSC Industrial",
                      "Motion Industries", "Applied Industrial", "Pumpman", "Pump Products",
                      "Pump Catalog", "Zoro", "Global Industrial", "Fastenal",
                      or the actual site/brand name if not in this list
  price             : number (USD, unit price) OR null if hidden / "Call for Quote"
  shipping_fee      : number (USD) — 0 if "Free Shipping", null if not shown or requires freight quote
  shipping_terms    : string — one of: "Free Shipping", "Flat Rate", "LTL Freight Required",
                      "S.F.Q." (Subject to Freight Quote), "TBA - Freight", or null if unknown
  is_freight        : boolean — true if page mentions "Freight", "LTL", "Truck Only", "Heavy Item",
                      or the item is clearly too large/heavy for standard parcel shipping
  found_part_number : string or null — the EXACT part number, model number, or stock ID shown on
                      the vendor's page for this item. If the page shows a list, include the specific
                      line item number or stock ID (e.g. "W2333581", "Item #: 123456"). null if not visible.
  exact_match       : boolean — true ONLY if found_part_number matches the part number being searched.
                      false if found_part_number is a different model/PN (functional equivalent).
  lead_days         : integer (business days to ship; "In Stock"->2, unknown->5)
  url               : string (exact source URL — REQUIRED)
  warranty_terms    : string or null — warranty period shown on the page (e.g. "12-month standard",
                      "5-year limited", "90-day parts only"). null if not mentioned.
  weight_lbs        : number or null — product weight in pounds if shown (used for freight guard). null if not visible.

Rules:
- Include entries where a real source URL is present EVEN IF no price is listed.
  Set price to null when hidden, "Call for Quote", or "Login to see price".
- Never invent or estimate prices — null only.
- If "Free Shipping" appears anywhere on the page: shipping_fee = 0, shipping_terms = "Free Shipping".
- If "Freight" / "LTL" appears: is_freight = true, shipping_fee = null, shipping_terms = "LTL Freight Required".
- If shipping cost is unclear / "subject to quote": shipping_terms = "S.F.Q.".
- found_part_number: extract even if it differs from the searched PN — this is the cross-reference key.
- exact_match: compare found_part_number to the searched PN; false if they differ.
- warranty_terms: extract verbatim warranty language; standardize to "N-month standard", "N-year limited", etc.
- weight_lbs: if weight is shown in kg, convert to lbs (x2.205). Any item >100 lbs likely needs freight.
- One entry per vendor — prefer entries that have a price over those that don't.
- Only SKIP entries with no URL at all.
"""


def _anthropic_complete(system: str, user: str) -> str:
    """Call the Anthropic Messages API directly over HTTP.

    Reads ANTHROPIC_API_KEY and _EXTRACTION_MODEL from the package at call time
    so that _patch_sourcing_keys() in chat_app.py takes effect before first use.
    """
    import utils.sourcing as _pkg
    api_key = _pkg.ANTHROPIC_API_KEY
    model   = _pkg._EXTRACTION_MODEL

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    # 2.5 — LLM cost tracking (silent fail so it never breaks sourcing)
    try:
        from utils.llm_tracker import record_call as _llm_rec
        _u = data.get("usage", {})
        _llm_rec(_u.get("input_tokens", 0), _u.get("output_tokens", 0))
    except Exception:
        pass
    return data["content"][0]["text"].strip()


def _llm_parse_results(specs, results: list[dict]) -> list[dict]:
    """Use Claude to extract price/lead_time from Tavily snippets."""
    if not results:
        return []

    import utils.sourcing as _pkg
    if not _pkg.ANTHROPIC_API_KEY:
        print("[Sourcing] No Anthropic API key — cannot parse results.")
        return []

    _BATCH_SIZE = 5
    all_items: list[dict] = []

    for batch_num, batch_start in enumerate(range(0, len(results), _BATCH_SIZE)):
        batch = results[batch_start:batch_start + _BATCH_SIZE]
        snippets_xml = ""
        for local_i, r in enumerate(batch):
            gid = batch_start + local_i
            snippets_xml += (
                f'<snippet id="{gid}">\n'
                f'URL: {r.get("url", "")}\n'
                f'Title: {r.get("title", "")}\n'
                f'Content: {r.get("content", "")}\n'
                f'</snippet>\n\n'
            )
        user_msg = (
            f"Part being searched: {specs.manufacturer} {specs.model} — {specs.part_number}\n\n"
            + snippets_xml.strip()
        )
        print(f"[Sourcing] Parsing batch {batch_num + 1} ({len(batch)} snippets)...")
        try:
            raw = _anthropic_complete(_PARSE_SYSTEM, user_msg)
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                print(f"[Sourcing] Batch {batch_num + 1}: no JSON array in response")
                continue
            items = json.loads(m.group(0))
            if isinstance(items, list):
                all_items.extend(items)
        except Exception as exc:
            print(f"[Sourcing] Batch {batch_num + 1} parse error: {exc}")

    print(f"[Sourcing] LLM extracted {len(all_items)} entries from {len(results)} snippets")
    return all_items
