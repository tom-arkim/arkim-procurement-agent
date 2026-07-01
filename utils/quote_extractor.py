"""
utils/quote_extractor.py
Extract a structured Quote from an inbound RFQ reply — Layer 3.

One extractor, three input shapes (all -> one Quote):
  (a) STRUCTURED FORM  — the supplier used Arkim's quote-form link (clean fields,
      highest confidence; no LLM needed).
  (b) PDF ATTACHMENT   — run through the OCR seam (ocr_text), THEN LLM-extract from
      the OCR text. (Reuses the existing extraction approach; no new PDF engine.)
  (c) FREE-TEXT body   — LLM-extract directly.

Abstention discipline (same as the suitability judgment — never auto-apply a wrong
price): a junk / no-quote reply yields None (NOT a fabricated price); a low-confidence
extraction is returned with needs_human_review=True. Nothing here updates pricing —
that happens only on human confirm (utils/reply_processor).

Dependencies are INJECTABLE and fail-soft:
  complete(system, user) -> str   LLM completion (mocked in tests; default = a thin
                                   local Anthropic call mirroring vision._sonnet_extract).
  ocr_text(attachment) -> str     PDF -> text (mocked in tests; live wiring deferred).
LLM/OCR are never called for the structured-form shape.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional

import requests

# Below this the extraction is too uncertain to apply without a human looking.
_CONFIDENCE_FLOOR = 0.6

_QUOTE_SYSTEM = """You extract a supplier price quote from the text of an email reply
or a quote document. Return ONLY valid JSON with these exact keys:
{
  "has_quote":  boolean,   // false if the text contains no price/quote at all
  "unit_price": number or null,   // unit price as a number, no currency symbol
  "currency":   string,    // e.g. "USD"; default "USD" if not stated
  "quantity":   integer or null,
  "lead_time":  string or null,   // e.g. "2 weeks", "5 business days"
  "min_order":  integer or null,
  "terms":      string or null,   // payment/shipping terms if stated
  "confidence": number     // 0.0-1.0, your confidence the quote is correct
}
Never invent a price. If no price is present, set has_quote=false and unit_price=null.
"""


@dataclass
class Quote:
    unit_price: Optional[float]
    currency: str = "USD"
    quantity: Optional[int] = None
    lead_time: Optional[str] = None
    min_order: Optional[int] = None
    terms: Optional[str] = None
    raw_source: str = "free_text"      # "form" | "pdf" | "free_text"
    confidence: float = 0.0
    needs_human_review: bool = False


def _default_complete(system: str, user: str) -> str:
    """Thin local Anthropic call (mirrors vision._sonnet_extract). Fail-soft: returns
    '' on any error. Tests always inject a mock instead."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return ""
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": os.environ.get("OS_EXTRACTION_MODEL", "claude-haiku-4-5"),
                  "max_tokens": 600, "system": system,
                  "messages": [{"role": "user", "content": user}]},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]
    except Exception as exc:
        print(f"[QuoteExtractor] LLM call failed: {type(exc).__name__}: {exc}")
        return ""


def _default_ocr(attachment: dict) -> str:
    """Default PDF->text seam: extract the TEXT LAYER of a digitally-generated PDF
    (pypdf). Covers the common case — supplier quotes on letterhead from reportlab/Word
    are digital, so the text is present and exact, no true OCR needed.

    Fail-soft by contract: returns "" (never raises) when there is no extractable text
    — a scanned/image-only PDF, missing/corrupt bytes, or pypdf unavailable. The caller
    then abstains via _llm_quote (empty text -> None): no crash, no fabricated quote.
    Scanned-PDF true OCR (an image pipeline) is a deliberate follow-on that slots in
    here behind the same seam. The seam stays injectable; this is just the real default
    in place of the old raising stub (tests still pass a mock ocr_text)."""
    data = (attachment or {}).get("data")
    if not data:
        return ""
    try:
        import io

        from pypdf import PdfReader  # lazy: keep import cost off the module load path

        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return text.strip()
    except Exception as exc:
        print(f"[QuoteExtractor] PDF text-extraction failed: {type(exc).__name__}: {exc}")
        return ""


def _to_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _first_pdf(attachments: list) -> Optional[dict]:
    for a in attachments or []:
        ctype = (a.get("content_type") or "").lower()
        fname = (a.get("filename") or "").lower()
        if "pdf" in ctype or fname.endswith(".pdf"):
            return a
    return None


def _quote_from_form(form: dict) -> Optional[Quote]:
    """Structured form submission -> Quote (no LLM). None if it carries no price."""
    price = _to_float(form.get("unit_price"))
    if price is None:
        return None
    return Quote(
        unit_price=price,
        currency=form.get("currency") or "USD",
        quantity=_to_int(form.get("quantity")),
        lead_time=form.get("lead_time"),
        min_order=_to_int(form.get("min_order")),
        terms=form.get("terms"),
        raw_source="form",
        confidence=0.99,          # supplier-entered structured data
        needs_human_review=False,
    )


def _llm_quote(text: str, complete: Callable[[str, str], str], raw_source: str) -> Optional[Quote]:
    if not (text or "").strip():
        return None
    raw = complete(_QUOTE_SYSTEM, text) or ""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None  # no parseable response -> no quote (never fabricate)
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not data.get("has_quote") or data.get("unit_price") is None:
        return None  # explicit no-quote / no price -> None (no hallucination)
    conf = _to_float(data.get("confidence")) or 0.0
    return Quote(
        unit_price=_to_float(data.get("unit_price")),
        currency=data.get("currency") or "USD",
        quantity=_to_int(data.get("quantity")),
        lead_time=data.get("lead_time"),
        min_order=_to_int(data.get("min_order")),
        terms=data.get("terms"),
        raw_source=raw_source,
        confidence=conf,
        needs_human_review=conf < _CONFIDENCE_FLOOR,
    )


def extract_quote(
    reply,
    *,
    complete: Optional[Callable[[str, str], str]] = None,
    ocr_text: Optional[Callable[[dict], str]] = None,
) -> Optional[Quote]:
    """Extract a Quote from a ReplyNotice, or None if the reply carries no quote.

    Shape precedence: structured form -> PDF attachment (OCR then LLM) -> free-text
    body (LLM). Never raises into the caller; never fabricates a price.
    """
    complete = complete or _default_complete
    ocr_text = ocr_text or _default_ocr

    if getattr(reply, "form", None):
        return _quote_from_form(reply.form)

    pdf = _first_pdf(getattr(reply, "attachments", None) or [])
    if pdf is not None:
        try:
            text = ocr_text(pdf)
        except Exception as exc:
            print(f"[QuoteExtractor] OCR failed: {type(exc).__name__}: {exc}")
            return None
        return _llm_quote(text, complete, raw_source="pdf")

    return _llm_quote(getattr(reply, "body", "") or "", complete, raw_source="free_text")
