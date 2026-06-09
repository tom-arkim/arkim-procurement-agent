"""
Tests for utils/quote_extractor.py — three input shapes -> one Quote, with abstention.
LLM (`complete`) and PDF OCR (`ocr_text`) are injected mocks; no live calls.

Covers: free-text quote; PDF quote (OCR -> LLM, correct routing); structured form
(no LLM); junk reply -> None (no hallucination); low-confidence -> needs_human_review;
unparseable LLM output -> None; form without price -> None.
"""

import json

import pytest

from utils.inbox_reader import ReplyNotice
from utils.quote_extractor import extract_quote, Quote
from utils.procurement_agent.tests._reply_fixtures import (
    PDF_OCR_TEXT, FREE_TEXT_QUOTE, FORM_PAYLOAD, JUNK_REPLY,
)


def _complete_returning(payload: dict):
    """Mock LLM completion that returns `payload` as JSON, recording the user text."""
    calls = {"count": 0, "last_user": None}

    def _c(system, user):
        calls["count"] += 1
        calls["last_user"] = user
        return json.dumps(payload)

    _c.calls = calls
    return _c


_GOOD_FREE = {"has_quote": True, "unit_price": 85, "currency": "USD", "quantity": 4,
              "lead_time": "2 weeks", "min_order": 4, "terms": "Net 30", "confidence": 0.9}
_GOOD_PDF = {"has_quote": True, "unit_price": 1210.0, "currency": "USD", "quantity": 1,
             "lead_time": "5 business days", "terms": "FOB Origin, Net 30", "confidence": 0.88}
_NO_QUOTE = {"has_quote": False, "unit_price": None, "confidence": 0.0}


class TestFreeText:
    def test_free_text_quote_extracted(self):
        reply = ReplyNotice(sender="sales@baypower.com", body=FREE_TEXT_QUOTE)
        q = extract_quote(reply, complete=_complete_returning(_GOOD_FREE))
        assert isinstance(q, Quote)
        assert q.unit_price == 85.0
        assert q.quantity == 4 and q.min_order == 4
        assert q.raw_source == "free_text"
        assert q.needs_human_review is False


class TestPdf:
    def test_pdf_routed_through_ocr_then_llm(self):
        reply = ReplyNotice(sender="sales@acme.com", body="See attached.",
                            attachments=[{"filename": "quote.pdf",
                                          "content_type": "application/pdf", "data": b"%PDF-1.4"}])
        ocr = lambda a: PDF_OCR_TEXT
        complete = _complete_returning(_GOOD_PDF)
        q = extract_quote(reply, complete=complete, ocr_text=ocr)

        assert q.raw_source == "pdf"
        assert q.unit_price == 1210.0
        # The LLM was fed the OCR text (PDF -> OCR -> LLM), not the email body.
        assert complete.calls["last_user"] == PDF_OCR_TEXT

    def test_ocr_failure_returns_none(self):
        reply = ReplyNotice(sender="s@acme.com",
                            attachments=[{"filename": "q.pdf", "content_type": "application/pdf"}])
        def _boom(a):
            raise RuntimeError("ocr down")
        assert extract_quote(reply, complete=_complete_returning(_GOOD_PDF), ocr_text=_boom) is None


class TestForm:
    def test_structured_form_no_llm_call(self):
        reply = ReplyNotice(sender="sales@baypower.com", form=dict(FORM_PAYLOAD))
        def _must_not_call(s, u):
            raise AssertionError("LLM must not be called for a structured form")
        q = extract_quote(reply, complete=_must_not_call, ocr_text=_must_not_call)
        assert q.raw_source == "form"
        assert q.unit_price == 85.0 and q.confidence == 0.99
        assert q.needs_human_review is False

    def test_form_without_price_returns_none(self):
        reply = ReplyNotice(sender="s@x.com", form={"lead_time": "2 weeks"})  # no price
        assert extract_quote(reply, complete=_complete_returning(_GOOD_FREE)) is None


class TestAbstention:
    def test_junk_reply_no_hallucinated_quote(self):
        reply = ReplyNotice(sender="sales@baypower.com", body=JUNK_REPLY)
        assert extract_quote(reply, complete=_complete_returning(_NO_QUOTE)) is None

    def test_low_confidence_flags_review_not_applied(self):
        reply = ReplyNotice(sender="sales@baypower.com", body=FREE_TEXT_QUOTE)
        low = dict(_GOOD_FREE, confidence=0.3)
        q = extract_quote(reply, complete=_complete_returning(low))
        assert q is not None
        assert q.needs_human_review is True

    def test_unparseable_llm_output_returns_none(self):
        reply = ReplyNotice(sender="s@x.com", body="anything")
        assert extract_quote(reply, complete=lambda s, u: "sorry, I cannot help") is None

    def test_empty_body_returns_none(self):
        reply = ReplyNotice(sender="s@x.com", body="")
        assert extract_quote(reply, complete=_complete_returning(_GOOD_FREE)) is None
