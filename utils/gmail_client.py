"""
utils/gmail_client.py
Shared Gmail API service builder for the real GmailSender / GmailInboxReader.

Credentials come from the ENVIRONMENT, never hardcoded and never committed. Two modes,
tried in order:
  1. Service account with domain-wide delegation, impersonating the sending mailbox
     (GMAIL_SENDER, default procurement@arkim.ai):
       GMAIL_SERVICE_ACCOUNT_FILE = /path/to/sa.json   (or)
       GMAIL_SERVICE_ACCOUNT_JSON = {inline service-account JSON}
  2. Authorized-user OAuth token (single mailbox):
       GMAIL_OAUTH_TOKEN_FILE = /path/to/authorized_user.json

The google libraries are imported LAZILY inside the builder, so this module (and the
whole test suite) imports without them installed. Go-live installs them:
    uv add google-api-python-client google-auth

Fail-soft by contract: build_gmail_service() returns None on missing libs / missing
creds / any error (never raises) — callers degrade (sender -> error result, reader -> []).
"""

from __future__ import annotations

import json
import os
from typing import Optional

# The mailbox we send/read as (Workspace, warmed-up outreach domain).
DEFAULT_SENDER = "procurement@arkim.ai"

# Send + read scopes.
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def gmail_sender_address() -> str:
    return os.environ.get("GMAIL_SENDER") or DEFAULT_SENDER


def _service_account_credentials(scopes: list[str], subject: str):
    """Build delegated service-account credentials, or None if not configured."""
    raw_json = os.environ.get("GMAIL_SERVICE_ACCOUNT_JSON")
    path = os.environ.get("GMAIL_SERVICE_ACCOUNT_FILE")
    if not (raw_json or path):
        return None
    from google.oauth2 import service_account  # lazy
    if raw_json:
        info = json.loads(raw_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_file(path, scopes=scopes)
    # Domain-wide delegation: act as the sending mailbox.
    return creds.with_subject(subject)


def _authorized_user_credentials(scopes: list[str]):
    """Build OAuth authorized-user credentials, or None if not configured."""
    path = os.environ.get("GMAIL_OAUTH_TOKEN_FILE")
    if not path:
        return None
    from google.oauth2.credentials import Credentials  # lazy
    return Credentials.from_authorized_user_file(path, scopes)


def build_gmail_service(scopes: Optional[list[str]] = None):
    """Return an authenticated Gmail API service, or None (fail-soft).

    Resolves credentials from env (service account DWD first, then OAuth token),
    builds the v1 Gmail service. Returns None — never raises — if the google libs
    aren't installed, no credentials are configured, or anything fails.
    """
    scopes = scopes or GMAIL_SCOPES
    subject = gmail_sender_address()
    try:
        creds = _service_account_credentials(scopes, subject) or \
            _authorized_user_credentials(scopes)
        if creds is None:
            print("[Gmail] No credentials configured (GMAIL_SERVICE_ACCOUNT_* / GMAIL_OAUTH_TOKEN_FILE)")
            return None
        from googleapiclient.discovery import build  # lazy
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as exc:
        print(f"[Gmail] service build failed: {type(exc).__name__}: {exc}")
        return None
