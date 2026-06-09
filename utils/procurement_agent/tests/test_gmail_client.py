"""
Tests for utils/gmail_client.py — the shared Gmail service builder.

Fail-soft + no-creds discipline: with no GMAIL_* env, build_gmail_service() returns None
(never raises). The google libraries are imported LAZILY, so importing this module does
not pull them in (the suite runs without them installed).
"""

import os
import subprocess
import sys

import pytest

from utils import gmail_client
from utils.gmail_client import build_gmail_service, gmail_sender_address, DEFAULT_SENDER

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))


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
        # Importing gmail_client must not pull in the heavy google libs (they are lazy,
        # imported inside the builder functions). Checked in a FRESH interpreter: asserting
        # on this process's sys.modules is order-dependent — another test that exercises the
        # build path (e.g. the libs-missing fail-soft case) imports google first and would
        # make this spuriously fail once the google libs are actually installed.
        code = (
            "import sys\n"
            "import utils.gmail_client as m\n"
            "assert m.__name__ == 'utils.gmail_client'\n"
            "assert 'googleapiclient' not in sys.modules, 'googleapiclient eagerly imported'\n"
            "assert 'google.oauth2' not in sys.modules, 'google.oauth2 eagerly imported'\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], cwd=_ROOT,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr or proc.stdout
