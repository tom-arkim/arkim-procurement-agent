"""
Tests for utils/reply_processor.py — inbound reply ingestion + the human-review gate.

Invariants asserted (no live calls; Gmail/LLM/OCR mocked; stores isolated to tmp):
  - NO auto-update: extraction only QUEUES; price_db/contact unchanged until confirm.
  - unmatched reply -> nothing queued, flagged.
  - junk reply -> no quote queued (no hallucination).
  - low-confidence -> queued as needs_human_review, not applied.
  - confirm_quote -> price_db updated (source rfq); confirm_contact -> primary promoted
    (resolved / supplier_nominated); reject -> discarded.
  - default stubbed reader -> zero work (no network).
"""

import json

import pytest

import utils.email_sender as email_sender
from utils import supplier_registry, price_db
from utils.inbox_reader import ReplyNotice, InboxReader
from utils import reply_processor
from utils.reply_processor import process_replies, confirm_quote, confirm_contact, reject
from utils.procurement_agent.tests._reply_fixtures import (
    FREE_TEXT_QUOTE, PDF_OCR_TEXT, FORM_PAYLOAD, JUNK_REPLY, NOMINATED_CONTACT_REPLY,
)


@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    monkeypatch.setattr(price_db, "_DB_PATH", str(tmp_path / "price_db.json"))
    return supplier_registry


class _FakeReader(InboxReader):
    def __init__(self, replies):
        self._replies = replies
    def fetch_bounces(self):
        return []
    def fetch_replies(self):
        return list(self._replies)


_QUOTE_JSON = {"has_quote": True, "unit_price": 85, "currency": "USD", "quantity": 4,
               "lead_time": "2 weeks", "min_order": 4, "terms": "Net 30", "confidence": 0.9}
_CONTACT_JSON = {"has_contact": True, "name": "Jane Smith", "position": "Purchasing Manager",
                 "email": "jane.smith@baypower.com", "confidence": 0.92}


def _mk_complete(quote=None, contact=None):
    """Mock LLM: quote JSON for the quote system prompt, contact JSON for the contact one."""
    def _c(system, user):
        if "contact" in system.lower():
            return json.dumps(contact or {"has_contact": False, "email": None, "confidence": 0.0})
        return json.dumps(quote or {"has_quote": False, "unit_price": None, "confidence": 0.0})
    return _c


def _specs(_run_id):
    return {"manufacturer": "Baldor", "part_number": "EM3770T"}


def _seed_rfq(sr):
    """A sent RFQ + a generic contact so replies from baypower.com match + a record exists."""
    sr.record_sent_message(run_id="run1", supplier_domain="baypower.com",
                           vendor_name="Bay Power", to=["sales@baypower.com"])
    sr.upsert_contact("baypower.com", {"contact_email": "sales@baypower.com",
                                       "contact_method": "generic_inbox", "contact_status": "resolved"})


def _reply(**kw):
    kw.setdefault("sender", "sales@baypower.com")
    return ReplyNotice(**kw)


# ---------------------------------------------------------------------------
# Queueing — extraction populates the review queue, applies NOTHING
# ---------------------------------------------------------------------------

class TestQueueingNoAutoUpdate:
    def test_free_text_quote_queued_not_applied(self, isolated_stores):
        sr = isolated_stores
        _seed_rfq(sr)
        reader = _FakeReader([_reply(body=FREE_TEXT_QUOTE)])
        summary = process_replies(reader, complete=_mk_complete(quote=_QUOTE_JSON), specs_lookup=_specs)

        assert summary["queued_quotes"] == 1
        items = sr.get_review_items(kind="quote")
        assert len(items) == 1 and items[0]["status"] == "pending"
        assert items[0]["payload"]["unit_price"] == 85
        assert items[0]["raw_source"] == "free_text"
        # NOT auto-applied: price_db still empty.
        assert price_db.get_cached_prices("Baldor", "EM3770T") == {}

    def test_pdf_quote_routed_through_ocr_and_queued(self, isolated_stores):
        sr = isolated_stores
        _seed_rfq(sr)
        reply = _reply(body="See attached.",
                       attachments=[{"filename": "q.pdf", "content_type": "application/pdf"}])
        pdf_quote = dict(_QUOTE_JSON, unit_price=1210.0)
        summary = process_replies(_FakeReader([reply]),
                                  complete=_mk_complete(quote=pdf_quote),
                                  ocr_text=lambda a: PDF_OCR_TEXT, specs_lookup=_specs)
        items = sr.get_review_items(kind="quote")
        assert summary["queued_quotes"] == 1
        assert items[0]["raw_source"] == "pdf" and items[0]["payload"]["unit_price"] == 1210.0

    def test_structured_form_queued(self, isolated_stores):
        sr = isolated_stores
        _seed_rfq(sr)
        process_replies(_FakeReader([_reply(form=dict(FORM_PAYLOAD))]),
                        complete=_mk_complete(), specs_lookup=_specs)
        items = sr.get_review_items(kind="quote")
        assert len(items) == 1 and items[0]["raw_source"] == "form"

    def test_nominated_contact_queued_not_promoted(self, isolated_stores):
        sr = isolated_stores
        _seed_rfq(sr)
        process_replies(_FakeReader([_reply(body=NOMINATED_CONTACT_REPLY)]),
                        complete=_mk_complete(contact=_CONTACT_JSON), specs_lookup=_specs)
        items = sr.get_review_items(kind="contact")
        assert len(items) == 1 and items[0]["status"] == "pending"
        # NOT auto-promoted: primary contact still unset.
        assert sr.lookup_by_domain("baypower.com")["primary_contact_email"] is None

    def test_both_quote_and_contact_extracted(self, isolated_stores):
        sr = isolated_stores
        _seed_rfq(sr)
        summary = process_replies(_FakeReader([_reply(body=FREE_TEXT_QUOTE)]),
                                  complete=_mk_complete(quote=_QUOTE_JSON, contact=_CONTACT_JSON),
                                  specs_lookup=_specs)
        assert summary["queued_quotes"] == 1 and summary["queued_contacts"] == 1

    def test_junk_reply_queues_nothing(self, isolated_stores):
        sr = isolated_stores
        _seed_rfq(sr)
        summary = process_replies(_FakeReader([_reply(body=JUNK_REPLY)]),
                                  complete=_mk_complete(), specs_lookup=_specs)
        assert summary["queued_quotes"] == 0 and summary["queued_contacts"] == 0
        assert sr.get_review_items() == []

    def test_low_confidence_quote_flagged_for_review(self, isolated_stores):
        sr = isolated_stores
        _seed_rfq(sr)
        process_replies(_FakeReader([_reply(body=FREE_TEXT_QUOTE)]),
                        complete=_mk_complete(quote=dict(_QUOTE_JSON, confidence=0.3)),
                        specs_lookup=_specs)
        items = sr.get_review_items(kind="quote")
        assert items[0]["status"] == "needs_human_review"

    def test_unmatched_reply_queued_for_human_review(self, isolated_stores):
        # GAP FIX (State-C 3a, item 2): a reply from a domain we never emailed used to be
        # logged-and-DROPPED. It is now QUEUED as needs_human_review (a supplier reply must
        # not silently vanish once sends are live) — but NOT auto-attributed to a run.
        sr = isolated_stores
        _seed_rfq(sr)
        reply = ReplyNotice(sender="x@unrelated.com", thread_id="other-thread",
                            message_id="ext-1@unrelated.com", body=FREE_TEXT_QUOTE)
        summary = process_replies(_FakeReader([reply]),
                                  complete=_mk_complete(quote=_QUOTE_JSON), specs_lookup=_specs)
        # Still logged/counted...
        assert summary["unmatched"] == ["x@unrelated.com"]
        assert summary["queued_unmatched"] == 1
        # ...but no longer dropped: queued for review.
        items = sr.get_review_items(kind="unmatched_reply")
        assert len(items) == 1
        it = items[0]
        assert it["status"] == "needs_human_review"
        assert it["run_id"] is None          # NOT auto-attributed to a run/candidate
        assert it["supplier_domain"] is None  # the sender domain is unverified -> not a claim
        assert it["payload"]["sender"] == "x@unrelated.com"
        assert it["payload"]["sender_domain"] == "unrelated.com"
        assert it["payload"]["thread_id"] == "other-thread"
        assert it["payload"]["message_id"] == "ext-1@unrelated.com"
        # No quote/contact was attributed to a candidate, and it is absent from run feeds.
        assert sr.get_review_items(kind="quote") == []
        assert sr.get_review_items(run_id="run1") == []


# ---------------------------------------------------------------------------
# State-C prerequisite (3a): carry the deterministic thread-join key onto the quote
# ---------------------------------------------------------------------------

class TestDeterministicKeyCarry:
    def _seed_threaded_rfq(self, sr):
        """A sent RFQ carrying a real thread_id + message_id, plus a generic contact so a
        same-domain reply also matches. Returns the persisted sent_messages row."""
        sr.record_sent_message(run_id="run1", supplier_domain="baypower.com",
                               vendor_name="Bay Power", to=["sales@baypower.com"],
                               message_id="rfq-abc@arkim.ai", thread_id="thread-1")
        sr.upsert_contact("baypower.com", {"contact_email": "sales@baypower.com",
                                           "contact_method": "generic_inbox",
                                           "contact_status": "resolved"})
        return sr.get_sent_messages(run_id="run1")[0]

    def test_matched_quote_carries_thread_keys(self, isolated_stores):
        # The matched sent_messages row's id/thread_id/message_id are CARRIED onto the
        # quote review_item (previously dropped at the forward in process_replies).
        sr = isolated_stores
        sent = self._seed_threaded_rfq(sr)
        reply = ReplyNotice(sender="someone@baypower.com", thread_id="thread-1",
                            body=FREE_TEXT_QUOTE)
        process_replies(_FakeReader([reply]),
                        complete=_mk_complete(quote=_QUOTE_JSON), specs_lookup=_specs)
        item = sr.get_review_items(kind="quote")[0]
        assert item["thread_id"] == "thread-1"
        assert item["sent_message_id"] == sent["id"]
        assert item["message_id"] == "rfq-abc@arkim.ai"

    def test_deterministic_key_round_trips_through_store(self, isolated_stores):
        # Given a sent row with a known thread_id, a reply on that thread -> the queued
        # quote round-trips that exact thread_id + sent_message_id back out of the store.
        sr = isolated_stores
        sent = self._seed_threaded_rfq(sr)
        reply = ReplyNotice(sender="jeff@baypower.com", thread_id="thread-1",
                            body=FREE_TEXT_QUOTE)
        process_replies(_FakeReader([reply]),
                        complete=_mk_complete(quote=_QUOTE_JSON), specs_lookup=_specs)
        item = sr.get_review_items(kind="quote")[0]
        # The key ties the quote to the EXACT outbound (not just the supplier domain).
        assert item["thread_id"] == sent["thread_id"] == "thread-1"
        assert item["sent_message_id"] == sent["id"]
        assert item["supplier_domain"] == "baypower.com"  # domain still present as fallback

    def test_legacy_quote_row_without_link_keys_reads_fine(self, isolated_stores):
        # Back-compat: a quote recorded WITHOUT the new link fields (legacy / out-of-thread)
        # still reads cleanly — the new columns are nullable and the domain join is intact.
        sr = isolated_stores
        item_id = sr.record_review_item(
            "quote", {"unit_price": 50.0}, status="pending", run_id="run1",
            supplier_domain="baypower.com", vendor_name="Bay Power",
            manufacturer="Baldor", part_number="EM3770T")
        it = sr.get_review_item(item_id)
        assert it["thread_id"] is None
        assert it["sent_message_id"] is None
        assert it["message_id"] is None
        assert it["supplier_domain"] == "baypower.com"  # domain-join fallback unaffected


# ---------------------------------------------------------------------------
# Human-confirm applies; reject discards
# ---------------------------------------------------------------------------

class TestConfirmApply:
    def test_confirm_quote_updates_price_db(self, isolated_stores):
        sr = isolated_stores
        item_id = sr.record_review_item(
            "quote", {"unit_price": 85.0, "currency": "USD"}, status="pending",
            run_id="run1", supplier_domain="baypower.com", vendor_name="Bay Power",
            manufacturer="Baldor", part_number="EM3770T", confidence=0.9, raw_source="free_text")

        assert confirm_quote(item_id) is True
        prices = price_db.get_cached_prices("Baldor", "EM3770T")
        assert "Bay Power" in prices and prices["Bay Power"]["price"] == 85.0
        assert prices["Bay Power"]["source"] == "rfq"
        assert sr.get_review_item(item_id)["status"] == "confirmed"

    def test_confirm_quote_without_part_identity_does_not_apply(self, isolated_stores):
        sr = isolated_stores
        item_id = sr.record_review_item(
            "quote", {"unit_price": 85.0}, status="pending", supplier_domain="baypower.com",
            vendor_name="Bay Power")  # no manufacturer/part_number
        assert confirm_quote(item_id) is False
        assert price_db.all_entries() == {}
        assert sr.get_review_item(item_id)["status"] == "pending"  # untouched

    def test_confirm_contact_promotes_primary(self, isolated_stores):
        sr = isolated_stores
        sr.upsert_contact("baypower.com", {"contact_email": "sales@baypower.com",
                                           "contact_method": "generic_inbox", "contact_status": "resolved"})
        item_id = sr.record_review_item(
            "contact", {"name": "Jane Smith", "position": "Purchasing Manager",
                        "email": "jane.smith@baypower.com"},
            status="pending", supplier_domain="baypower.com", vendor_name="Bay Power")

        assert confirm_contact(item_id) is True
        rec = sr.lookup_by_domain("baypower.com")
        assert rec["primary_contact_email"] == "jane.smith@baypower.com"
        assert rec["primary_contact_status"] == "resolved"
        assert rec["primary_contact_source"] == "supplier_nominated"
        assert rec["contact_email"] == "sales@baypower.com"  # generic fallback intact

    def test_reject_discards_no_change(self, isolated_stores):
        sr = isolated_stores
        item_id = sr.record_review_item("quote", {"unit_price": 85.0}, manufacturer="Baldor",
                                        part_number="EM3770T", vendor_name="Bay Power")
        assert reject(item_id) is True
        assert sr.get_review_item(item_id)["status"] == "rejected"
        assert price_db.all_entries() == {}
        # A rejected item cannot then be confirmed.
        assert confirm_quote(item_id) is False

    def test_confirm_wrong_kind_is_rejected(self, isolated_stores):
        sr = isolated_stores
        item_id = sr.record_review_item("contact", {"email": "j@x.com"},
                                        supplier_domain="x.com")
        assert confirm_quote(item_id) is False  # it's a contact, not a quote


# ---------------------------------------------------------------------------
# Default stubbed reader -> zero work
# ---------------------------------------------------------------------------

class TestDefaultReaderStubbed:
    def test_default_reader_processes_nothing(self, isolated_stores):
        assert email_sender.EMAIL_SEND_ENABLED is False
        summary = process_replies()  # default GmailInboxReader, stubbed
        assert summary["processed"] == 0
        assert summary["queued_quotes"] == 0 and summary["queued_contacts"] == 0
