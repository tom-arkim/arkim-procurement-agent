"""
Multi-vendor outreach campaign helper (Phase 3 UI feature).

Captures parallel outreach intent for Tier 3 vendors (Quote Required)
in the audit log. Email sending is always disabled at this prototype stage.
"""

from __future__ import annotations

from utils.audit_log import write_audit_log


def _make_draft(vendor_name: str, specs: dict | None) -> str:
    """Generate a simple RFQ draft for one vendor."""
    mfg  = (specs or {}).get("manufacturer") or ""
    mdl  = (specs or {}).get("model") or ""
    pn   = (specs or {}).get("part_number") or ""
    subj_part = " ".join(p for p in [mfg, mdl, pn] if p) or "the requested part"
    return (
        f"Subject: Quote Request — {subj_part}\n\n"
        f"Hello {vendor_name},\n\n"
        f"We are seeking pricing and availability for the following:\n\n"
        f"  Manufacturer : {mfg or '—'}\n"
        f"  Model        : {mdl or '—'}\n"
        f"  Part Number  : {pn or '—'}\n\n"
        f"Please reply with unit price, lead time, and stock availability.\n\n"
        f"Regards,\nArkim Procurement\nprocurement@arkim.ai"
    )


def initiate_outreach_campaign(
    run_id: str,
    selected_vendors: list[dict],
    specs: dict | None = None,
) -> dict:
    """Record multi-vendor outreach intent in the audit log and return draft emails.

    Does not send email. Returns a dict with:
      vendors       list[str]        — vendor names in selection order
      drafts        dict[str, str]   — {vendor_name: draft_email_text}
      email_send_enabled  bool       — always False in prototype
    """
    vendor_names = [v.get("vendor_name") or "Unknown" for v in selected_vendors]
    drafts       = {name: _make_draft(name, specs) for name in vendor_names}

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
        "email_send_enabled": False,
    }
