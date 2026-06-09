"""
Tests for utils/contact_extractor.py — extract a nominated procurement contact from a
reply (LLM mocked). Covers: extracted with email; no-contact -> None; named-but-no-
email -> None (not promotable); low-confidence -> needs_human_review.
"""

import json

from utils.inbox_reader import ReplyNotice
from utils.contact_extractor import extract_nominated_contact, NominatedContact
from utils.procurement_agent.tests._reply_fixtures import NOMINATED_CONTACT_REPLY, JUNK_REPLY


def _complete(payload: dict):
    return lambda system, user: json.dumps(payload)


_GOOD = {"has_contact": True, "name": "Jane Smith", "position": "Purchasing Manager",
         "email": "jane.smith@baypower.com", "confidence": 0.92}


class TestExtractNominatedContact:
    def test_extracts_contact_with_email(self):
        reply = ReplyNotice(sender="sales@baypower.com", body=NOMINATED_CONTACT_REPLY)
        c = extract_nominated_contact(reply, complete=_complete(_GOOD))
        assert isinstance(c, NominatedContact)
        assert c.name == "Jane Smith"
        assert c.position == "Purchasing Manager"
        assert c.email == "jane.smith@baypower.com"
        assert c.needs_human_review is False

    def test_no_contact_returns_none(self):
        reply = ReplyNotice(sender="sales@baypower.com", body=JUNK_REPLY)
        c = extract_nominated_contact(reply, complete=_complete(
            {"has_contact": False, "email": None, "confidence": 0.0}))
        assert c is None

    def test_named_but_no_email_returns_none(self):
        reply = ReplyNotice(sender="sales@baypower.com", body="Talk to Jane in purchasing.")
        c = extract_nominated_contact(reply, complete=_complete(
            {"has_contact": True, "name": "Jane", "position": "Purchasing", "email": None,
             "confidence": 0.8}))
        assert c is None  # no email -> not promotable to a resolved primary

    def test_low_confidence_flags_review(self):
        reply = ReplyNotice(sender="sales@baypower.com", body=NOMINATED_CONTACT_REPLY)
        c = extract_nominated_contact(reply, complete=_complete(dict(_GOOD, confidence=0.4)))
        assert c is not None and c.needs_human_review is True

    def test_unparseable_and_empty_return_none(self):
        reply = ReplyNotice(sender="s@x.com", body="hello")
        assert extract_nominated_contact(reply, complete=lambda s, u: "no json here") is None
        assert extract_nominated_contact(ReplyNotice(sender="s@x.com", body=""),
                                         complete=_complete(_GOOD)) is None
