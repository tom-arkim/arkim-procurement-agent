"""
Tests for utils/gmail_client.py — the shared Gmail service builder.

Fail-soft + no-creds discipline: with no GMAIL_* env, build_gmail_service() returns None
(never raises). The google libraries are imported LAZILY, so importing this module does
not pull them in (the suite runs without them installed).
"""

import sys

import pytest

from utils import gmail_client
from utils.gmail_client import build_gmail_service, gmail_sender_address, DEFAULT_SENDER


@pytest.fixture(autouse=True)
def _clear_gmail_env(monkeypatch):
    for var in ("GMAIL_SENDER", "GMAIL_SERVICE_ACCOUNT_JSON", "GMAIL_SERVICE_ACCOUNT_FILE",
                "GMAIL_OAUTH_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)


class TestSenderAddress:
    def test_default(self):
        assert gmail_sender_address() == DEFAULT_SENDER == "procurement@arkim.ai"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("GMAIL_SENDER", "ops@arkim.ai")
        assert gmail_sender_address() == "ops@arkim.ai"


class TestBuildService:
    def test_no_credentials_returns_none(self):
        assert build_gmail_service() is None     # nothing configured -> fail-soft

    def test_creds_set_but_libs_missing_is_failsoft(self, monkeypatch):
        # Credentials present but google libs aren't installed -> caught -> None (no crash).
        monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_JSON", '{"type": "service_account"}')
        assert build_gmail_service() is None


class TestLazyImport:
    def test_module_does_not_import_google_at_load(self):
        # Importing gmail_client must not pull in the heavy google libs.
        assert "googleapiclient" not in sys.modules
        assert "google.oauth2" not in sys.modules
        assert gmail_client.__name__ == "utils.gmail_client"
