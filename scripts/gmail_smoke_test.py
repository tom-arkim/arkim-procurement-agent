"""
Standalone Gmail auth smoke-test — READ-ONLY. Sends NOTHING.

Verifies the service-account credential chain (key file + domain-wide delegation +
scopes) BEFORE EMAIL_SEND_ENABLED is flipped. Mirrors utils/gmail_client.py's auth, but
builds inline so the REAL exception surfaces (the helper is fail-soft and returns None,
which hides the diagnostic). Not wired into the app — a one-off check.

Run:  uv run python scripts/gmail_smoke_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

key_file = os.environ.get("GMAIL_SERVICE_ACCOUNT_FILE")
sender = os.environ.get("GMAIL_SENDER")

print("=== Gmail auth smoke-test (read-only, sends nothing) ===")
print(f"GMAIL_SENDER               : {sender or 'MISSING'}")
print(f"GMAIL_SERVICE_ACCOUNT_FILE : {key_file or 'MISSING'}")

if not sender:
    sys.exit("FAIL: GMAIL_SENDER not set in .env")
if not key_file:
    sys.exit("FAIL: GMAIL_SERVICE_ACCOUNT_FILE not set in .env")
if not os.path.exists(key_file):
    sys.exit(f"FAIL [key path]: file not found at GMAIL_SERVICE_ACCOUNT_FILE -> {key_file!r}")
print(f"key file exists            : yes ({os.path.getsize(key_file)} bytes)")

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
except ImportError as exc:
    sys.exit(f"FAIL [libs]: {exc} -- run: uv add google-api-python-client google-auth")

try:
    creds = service_account.Credentials.from_service_account_file(
        key_file, scopes=SCOPES
    ).with_subject(sender)            # domain-wide delegation: impersonate the mailbox
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    print("\nCalling users().getProfile(userId='me')  [READ-ONLY] ...")
    profile = service.users().getProfile(userId="me").execute()
    print("\nSUCCESS — the full chain works (key + delegation + scopes):")
    print(f"  emailAddress  : {profile.get('emailAddress')}")
    print(f"  messagesTotal : {profile.get('messagesTotal')}")
    print(f"  threadsTotal  : {profile.get('threadsTotal')}")
    print(f"  historyId     : {profile.get('historyId')}")
    if (profile.get("emailAddress") or "").lower() == (sender or "").lower():
        print(f"\n=> Impersonating {sender} confirmed. Clear for the first send-to-self.")
    else:
        print(f"\n=> NOTE: profile email {profile.get('emailAddress')!r} != GMAIL_SENDER {sender!r}.")
except Exception as exc:
    msg = str(exc)
    low = msg.lower()
    print("\nFAILED:")
    print(f"  {type(exc).__name__}: {msg[:600]}")
    if "unauthorized_client" in low or "access_denied" in low or "delegation" in low:
        print("\n=> DELEGATION issue (domain-wide delegation). In Google Workspace Admin "
              "(Security > API controls > Domain-wide delegation): authorize this service "
              "account's CLIENT ID for exactly these scopes:")
        for s in SCOPES:
            print(f"     {s}")
        print("   Also confirm GMAIL_SENDER is a real mailbox in the Workspace domain.")
    elif "403" in low or "insufficient" in low or "forbidden" in low or "scope" in low:
        print("\n=> SCOPE/permission issue — the delegated scopes don't cover getProfile, "
              "or the Gmail API isn't enabled for the project. Enable the Gmail API and "
              "ensure gmail.readonly is in the delegated scope list.")
    else:
        print("\n=> Not a clear delegation/scope signature — see the error above "
              "(could be a malformed key file, clock skew, or network).")
    sys.exit(1)
