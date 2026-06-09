"""
Tests for utils/inbox_reader.py — the inbound READ interface + the STUBBED
GmailInboxReader.

Safety focus (mirrors the send-layer assertion): with EMAIL_SEND_ENABLED False (the
default) or no credentials, fetch_bounces() returns [] and makes ZERO real calls;
the live Gmail read is unwired and raises rather than faking a read. No network is
ever touched here.
"""

import pytest

import utils.email_sender as email_sender
from utils.inbox_reader import BounceNotice, GmailInboxReader


class TestBounceNotice:
    def test_defaults_hard_no_ids(self):
        n = BounceNotice(failed_recipient="x@y.com")
        assert n.is_hard is True
        assert n.message_id is None and n.thread_id is None


class TestGmailInboxReaderStubbed:
    def test_flag_false_returns_empty_no_read(self):
        reader = GmailInboxReader(credentials=object())  # creds present, flag False
        assert email_sender.EMAIL_SEND_ENABLED is False
        assert reader.fetch_bounces() == []
        assert reader.configured is False

    def test_flag_true_no_creds_returns_empty(self, monkeypatch):
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        reader = GmailInboxReader(credentials=None)
        assert reader.fetch_bounces() == []
        assert reader.configured is False

    def test_live_path_is_unwired_and_raises(self, monkeypatch):
        monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", True)
        reader = GmailInboxReader(credentials=object())
        assert reader.configured is True
        with pytest.raises(NotImplementedError):
            reader.fetch_bounces()

    def test_no_network_dependency_imported(self):
        import utils.inbox_reader as ir
        with open(ir.__file__, "r", encoding="utf-8") as fh:
            text = fh.read()
        for banned in ("import requests", "import imaplib", "import smtplib",
                       "import http", "urllib.request", "googleapiclient"):
            assert banned not in text
