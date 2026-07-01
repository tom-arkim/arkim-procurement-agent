"""
Tests for utils/bounce_parser.py — the pure DSN parser, against representative
fixtures (no I/O, no network).

Covers: hard bounce parsed with recipient + original Message-ID + 5.x.x; soft/
transient bounce classified is_hard=False; a normal vendor reply -> None (no false
bounce); a malformed DSN -> None (no crash, no fabricated recipient).
"""

from utils.bounce_parser import parse_bounce
from utils.procurement_agent.tests._dsn_fixtures import (
    HARD_BOUNCE, SOFT_BOUNCE, NON_BOUNCE, MALFORMED_DSN,
)


class TestHardBounce:
    def test_extracts_recipient_messageid_and_marks_hard(self):
        n = parse_bounce(HARD_BOUNCE)
        assert n is not None
        assert n.failed_recipient == "sales@baypower.com"
        assert n.message_id == "rfq-abc@arkim.ai"   # original, NOT the DSN's own id
        assert n.is_hard is True
        assert n.status_code == "5.1.1"
        assert "550" in (n.reason or "")

    def test_does_not_pick_dsn_own_message_id(self):
        n = parse_bounce(HARD_BOUNCE)
        assert n.message_id != "dsn-own-aaa@mail.gmail.com"


class TestSoftBounce:
    def test_transient_is_not_hard(self):
        n = parse_bounce(SOFT_BOUNCE)
        assert n is not None
        assert n.failed_recipient == "sales@standardelectricsupply.com"
        assert n.is_hard is False           # 4.2.2 / delayed -> soft
        assert n.status_code == "4.2.2"
        assert n.message_id == "rfq-def@arkim.ai"


class TestNonBounceAndMalformed:
    def test_non_bounce_reply_returns_none(self):
        assert parse_bounce(NON_BOUNCE) is None

    def test_malformed_dsn_returns_none(self):
        assert parse_bounce(MALFORMED_DSN) is None

    def test_empty_and_garbage_return_none(self):
        assert parse_bounce("") is None
        assert parse_bounce(None) is None
        assert parse_bounce("just some random text, not an email") is None
