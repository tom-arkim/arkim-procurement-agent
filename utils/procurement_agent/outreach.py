"""
Multi-vendor outreach campaign helper (Phase 3 UI feature).

Captures parallel outreach intent for Tier 3 vendors (Quote Required)
in the audit log. This helper only drafts + records intent; it never sends. The
reported `email_send_enabled` is read from the canonical send gate
(utils.email_sender.EMAIL_SEND_ENABLED, default-off) rather than re-declared here.
"""

from __future__ import annotations

from utils import email_sender
from utils.audit_log import write_audit_log

# Version stamp for the RFQ template below (SEND_GOVERNANCE_V1 T6): recorded on
# every governance-active sent_messages row so the ledger shows exactly which
# template text a supplier received. Bump when _make_draft's copy changes.
TEMPLATE_VERSION = "rfq-v1"

# Appended to an RFQ ONLY when it falls back to a generic inbox (no resolved named
# primary). Asks the supplier to nominate the right procurement contact. Capturing
# the reply is Layer 3 (inbound) — out of scope here; this is outbound text only.
CONTACT_NOMINATION_ASK = (
    "We'd like to keep sending procurement requests for parts like this to the right "
    "person. If there's a specific contact these should go to, please reply with their "
    "name, position, and email."
)


def should_request_contact(vendor: dict | None) -> bool:
    """Whether the RFQ should ask the supplier to nominate a procurement contact.

    True unless we already have a RESOLVED named primary contact. So a generic-inbox
    fallback, or a primary in {found_no_email, none, bounced}, gets the ask; a
    primary_contact_status == "resolved" does NOT (we already have the person).
    """
    if not vendor:
        return True
    return vendor.get("primary_contact_status") != "resolved"


def _make_draft(vendor_name: str, specs: dict | None, request_contact: bool = False) -> str:
    """Generate a simple RFQ draft for one vendor.

    When request_contact is True (generic-inbox fallback), a polite contact-nomination
    ask is appended — a template addition, not a rewrite.
    """
    mfg  = (specs or {}).get("manufacturer") or ""
    mdl  = (specs or {}).get("model") or ""
    pn   = (specs or {}).get("part_number") or ""
    subj_part = " ".join(p for p in [mfg, mdl, pn] if p) or "the requested part"
    body = (
        f"Subject: Quote Request — {subj_part}\n\n"
        f"Hello {vendor_name},\n\n"
        f"We are seeking pricing and availability for the following:\n\n"
        f"  Manufacturer : {mfg or '—'}\n"
        f"  Model        : {mdl or '—'}\n"
        f"  Part Number  : {pn or '—'}\n\n"
        f"Please reply with unit price, lead time, and stock availability.\n\n"
        f"Regards,\nArkim Procurement\nprocurement@arkim.ai"
    )
    if request_contact:
        body += f"\n\n{CONTACT_NOMINATION_ASK}"
    return body


def initiate_outreach_campaign(
    run_id: str,
    selected_vendors: list[dict],
    specs: dict | None = None,
) -> dict:
    """Record multi-vendor outreach intent in the audit log and return draft emails.

    Does not send email. Returns a dict with:
      vendors       list[str]        — vendor names in selection order
      drafts        dict[str, str]   — {vendor_name: draft_email_text}
      email_send_enabled  bool       — the canonical send gate (default-off)
    """
    vendor_names = [v.get("vendor_name") or "Unknown" for v in selected_vendors]
    drafts       = {
        (v.get("vendor_name") or "Unknown"): _make_draft(
            v.get("vendor_name") or "Unknown", specs,
            request_contact=should_request_contact(v),
        )
        for v in selected_vendors
    }

    write_audit_log({
        "sourcing_run_id": run_id,
        "input_summary":   (
            f"Multi-vendor outreach initiated — {len(vendor_names)} vendor(s): "
            + ", ".join(vendor_names)
        ),
        "workflow_mode":  "multi_vendor_outreach",
        "user_selection": ", ".join(vendor_names),
        "vendors_surfaced": selected_vendors,
        "agent_version":  "1.0.0-phase3",
    })

    return {
        "vendors":            vendor_names,
        "drafts":             drafts,
        "email_send_enabled": email_sender.EMAIL_SEND_ENABLED,
    }
