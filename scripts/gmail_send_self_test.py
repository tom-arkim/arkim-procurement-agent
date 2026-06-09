"""
Gated Gmail send-to-SELF probe — the first real-send go-live check.

Sends ONE message from GMAIL_SENDER (procurement@arkim.ai) to ITSELF (optionally CC'ing
your own addresses on other providers via GMAIL_SELFTEST_CC) through the REAL GmailSender.
Use it to verify deliverability + auth land IN-INBOX (not spam) and that message_id/
thread_id populate (so a self-reply matches via fetch_replies) BEFORE any supplier.

SAFETY — this respects the double gate; it does NOT bypass anything:
  - It calls GmailSender().send(), which only actually sends when EMAIL_SEND_ENABLED is
    True. With the repo default (False) it prints a STUBBED result and sends NOTHING.
  - It does NOT flip the flag for you. To actually send, YOU set
    EMAIL_SEND_ENABLED = True in utils/email_sender.py, then run this.
  - Recipient is your OWN mailbox (the sender), never a supplier.

Run:
  uv run python scripts/gmail_send_self_test.py
  # to also CC your other inboxes:
  #   set GMAIL_SELFTEST_CC=you@gmail.com,you@outlook.com   (then run)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

import utils.email_sender as email_sender
from utils.email_sender import EmailMessage, GmailSender
from utils.gmail_client import gmail_sender_address

self_addr = gmail_sender_address()
cc = [a.strip() for a in (os.environ.get("GMAIL_SELFTEST_CC") or "").split(",") if a.strip()]

print("=== Gmail send-to-SELF probe ===")
print(f"EMAIL_SEND_ENABLED : {email_sender.EMAIL_SEND_ENABLED}  "
      f"({'WILL SEND' if email_sender.EMAIL_SEND_ENABLED else 'stub only — nothing will send'})")
print(f"To (self)          : {self_addr}")
print(f"Cc                 : {cc or '(none — set GMAIL_SELFTEST_CC to add your other inboxes)'}")

message = EmailMessage(
    to=[self_addr],
    cc=cc,
    subject="Arkim Gmail deliverability self-test",
    body=(
        "This is an automated send-to-self deliverability check from the Arkim "
        "procurement agent.\n\n"
        "If you received this IN YOUR INBOX (not spam) across providers, SPF/DKIM/DMARC "
        "and the service-account send path are working. You can reply to this message to "
        "exercise inbound reply matching (thread_id / In-Reply-To).\n\n"
        "-- Arkim procurement (procurement@arkim.ai)"
    ),
    metadata={"run_id": "self-test", "supplier_domain": self_addr.split("@")[-1],
              "rfq_id": "gmail-self-test"},
)

result = GmailSender().send(message)   # respects EMAIL_SEND_ENABLED — stubs when False

print("\nSendResult:")
print(f"  status     : {result.status}")
print(f"  message_id : {result.message_id}")
print(f"  thread_id  : {result.thread_id}")
print(f"  error      : {result.error}")

if result.status == "stubbed":
    print("\n=> STUBBED — nothing sent. EMAIL_SEND_ENABLED is False. To do the real "
          "send-to-self, set EMAIL_SEND_ENABLED = True in utils/email_sender.py and re-run.")
    sys.exit(0)
if result.status == "sent":
    print(f"\n=> SENT to {self_addr}. Now check: did it land IN-INBOX (not spam) across "
          "providers? Are message_id/thread_id populated above? If yes, you're clear to "
          "send to a real supplier. (Reply to it to exercise fetch_replies matching.)")
    sys.exit(0)
print(f"\n=> {result.status.upper()} — see error above (creds/misconfig). No supplier was contacted.")
sys.exit(1)
