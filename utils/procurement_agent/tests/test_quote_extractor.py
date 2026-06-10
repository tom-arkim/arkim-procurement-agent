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
from utils.quote_extractor import extract_quote, Quote, _default_ocr
from utils.procurement_agent.tests._reply_fixtures import (
    PDF_OCR_TEXT, FREE_TEXT_QUOTE, FORM_PAYLOAD, JUNK_REPLY,
)


def _make_text_pdf(lines: list[str]) -> bytes:
    """Build a minimal, valid single-page PDF with a real TEXT LAYER (no external dep) —
    a stand-in for a digitally-generated supplier quote (reportlab/Word output). pypdf
    extracts the lines back. An empty `lines` yields a page with no text-showing
    operators -> models a scanned/image-only PDF (no extractable text)."""
    parts = ["BT", "/F1 12 Tf", "72 720 Td"]
    for i, line in enumerate(lines):
        esc = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        if i > 0:
            parts.append("0 -16 Td")
        parts.append(f"({esc}) Tj")
    parts.append("ET")
    content = "\n".join(parts).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_pos = len(pdf)
    n = len(objs) + 1
    pdf += b"xref\n0 %d\n" % n + b"0000000000 65535 f \n"
    for off in offsets:
        pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (n, xref_pos)
    return bytes(pdf)


# A digitally-generated quote PDF carrying the canonical PDF_OCR_TEXT content (text layer).
_QUOTE_PDF_BYTES = _make_text_pdf([ln for ln in PDF_OCR_TEXT.splitlines() if ln.strip()])


def _pdf_reply(data: bytes, *, body: str = "See attached.") -> ReplyNotice:
    return ReplyNotice(sender="sales@acme.com", body=body,
                       attachments=[{"filename": "quote.pdf",
                                     "content_type": "application/pdf", "data": data}])


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


class TestPdfRealExtraction:
    """The DEFAULT ocr_text now does real PDF text-layer extraction (pypdf). Extraction
    logic / Quote shape / abstention are unchanged — only the text SOURCE changed from a
    raising stub to real text. LLM stays mocked here; no external calls."""

    def test_default_ocr_extracts_text_layer(self):
        text = _default_ocr({"filename": "quote.pdf", "content_type": "application/pdf",
                             "data": _QUOTE_PDF_BYTES})
        assert "ACME MOTOR SUPPLY" in text
        assert "Baldor EM3770T" in text
        assert "$1,210.00" in text

    def test_pdf_extracted_via_default_ocr_then_mocked_llm(self):
        # No ocr_text injected -> the REAL default runs; the LLM (mocked) receives the
        # extracted text, NOT the email body. Digital PDF -> text -> extractor -> Quote.
        complete = _complete_returning(_GOOD_PDF)
        q = extract_quote(_pdf_reply(_QUOTE_PDF_BYTES), complete=complete)
        assert isinstance(q, Quote)
        assert q.raw_source == "pdf"
        assert q.unit_price == 1210.0
        assert "Baldor EM3770T" in complete.calls["last_user"]
        assert "See attached." not in complete.calls["last_user"]

    def test_no_text_layer_pdf_abstains(self):
        # Scanned/image-only (no text layer) -> empty extraction -> no quote, no crash,
        # and the LLM is never even consulted (so it cannot fabricate). True OCR is a
        # flagged follow-on that would slot in here.
        blank = _make_text_pdf([])
        complete = _complete_returning(_GOOD_PDF)
        assert extract_quote(_pdf_reply(blank), complete=complete) is None
        assert complete.calls["count"] == 0

    def test_corrupt_pdf_bytes_abstain_no_crash(self):
        complete = _complete_returning(_GOOD_PDF)
        assert extract_quote(_pdf_reply(b"not a real pdf"), complete=complete) is None
        assert complete.calls["count"] == 0

    def test_default_ocr_missing_data_returns_empty(self):
        assert _default_ocr({"filename": "q.pdf", "content_type": "application/pdf"}) == ""
        assert _default_ocr({}) == ""

    def test_seam_still_injectable_overrides_default(self):
        complete = _complete_returning(_GOOD_PDF)
        q = extract_quote(_pdf_reply(_QUOTE_PDF_BYTES), complete=complete,
                          ocr_text=lambda a: "INJECTED TEXT")
        assert q.raw_source == "pdf"
        assert complete.calls["last_user"] == "INJECTED TEXT"

    def test_junk_pdf_no_hallucinated_quote(self):
        # Real extraction of a junk PDF -> mocked LLM verdict no-quote -> None (same
        # abstention as free-text; no fabricated price).
        junk_pdf = _make_text_pdf(["Out of office until Monday.", "I will reply then."])
        assert extract_quote(_pdf_reply(junk_pdf, body=""),
                             complete=_complete_returning(_NO_QUOTE)) is None
