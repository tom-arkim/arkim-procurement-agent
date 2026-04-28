"""
Arkim Phase 1.5 — Unified Contact Resolution

Determines the correct outreach action for any vendor card regardless of tier.
Replaces per-tier button divergence with a single decision function.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ContactActionType(str, Enum):
    VIEW_LISTING           = "VIEW_LISTING"        # has URL, go see the listing
    SEND_QUOTE_REQUEST     = "SEND_QUOTE_REQUEST"   # has contact email, send quote req
    GENERATE_OUTREACH_DRAFT = "GENERATE_OUTREACH_DRAFT"  # no URL, no email — draft needed


@dataclass
class ContactAction:
    action_type: ContactActionType
    label: str
    url: Optional[str] = None
    email: Optional[str] = None
    sub_label: Optional[str] = None  # extra context shown beneath the CTA


def resolve_contact_action(option, supplier_registry: dict) -> ContactAction:
    """Unified resolver — applies identically to Tier 1, 2, and 3 vendor cards.

    Priority:
      1. VIEW_LISTING  — has a non-collection source URL
      2. SEND_QUOTE_REQUEST — has contact email (option or registry)
      3. GENERATE_OUTREACH_DRAFT — fallback when neither URL nor email available
    """
    url            = getattr(option, "source_url", None)
    is_collection  = getattr(option, "is_collection_page", False)
    is_oem         = getattr(option, "is_oem_direct", False)
    is_tbd         = getattr(option, "price_tbd", False)
    contact_email  = getattr(option, "contact_email", None)
    vendor_name    = option.vendor_name

    # Check registry for a contact email when option doesn't carry one
    if not contact_email and supplier_registry:
        reg_entry = supplier_registry.get(vendor_name.lower())
        if reg_entry:
            contact_email = reg_entry.get("contact_email")

    # ── 1. VIEW_LISTING ────────────────────────────────────────────────────
    if url and not is_collection:
        if is_oem:
            label = "Request Quote from OEM"
            sub   = "Recommended for warranty compliance"
        elif is_tbd:
            label = "View & Request Price"
            sub   = None
        else:
            label = "View Listing"
            sub   = None
        return ContactAction(
            action_type=ContactActionType.VIEW_LISTING,
            label=label,
            url=url,
            sub_label=sub,
        )

    # ── 2. SEND_QUOTE_REQUEST ───────────────────────────────────────────────
    if contact_email:
        return ContactAction(
            action_type=ContactActionType.SEND_QUOTE_REQUEST,
            label="Send Quote Request",
            email=contact_email,
        )

    # ── 3. GENERATE_OUTREACH_DRAFT (fallback) ──────────────────────────────
    return ContactAction(
        action_type=ContactActionType.GENERATE_OUTREACH_DRAFT,
        label="Generate Outreach Draft",
    )
