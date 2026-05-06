"""
utils/sourcing/tier3_outreach.py
Tier 3: RFQ email draft generation.

EMAIL_SEND_ENABLED must remain False until legal review and explicit sign-off.
See comments below.
"""

# Outbound email is not implemented. This constant must remain False until:
# (a) email templates are reviewed by legal counsel, and (b) a deliberate
# enabling decision is made and documented.
EMAIL_SEND_ENABLED = False  # Outbound email is not implemented.
                            # When SMTP/email API integration is added, this
                            # constant must remain False until:
                            # (a) email templates are reviewed by legal counsel
                            #     for accuracy of representations and CAN-SPAM compliance.
                            # (b) a deliberate enabling decision is made and documented.

_EMAIL_DEMO_FOOTER = """
---
[DEMO PREVIEW — This is a rendered example of the email Arkim's procurement agent will send
when the supplier outreach workflow is activated. No outbound email is currently sent from
this system.]"""


# IMPORTANT: These email templates describe Arkim's prototype-stage product.
# Before any outbound email send capability is enabled:
# 1. EMAIL_SEND_ENABLED above must be reviewed and updated.
# 2. Templates must be reviewed by legal counsel for accuracy of representations
#    and CAN-SPAM compliance.
# 3. The two-template structure (quote_request vs. partner_invitation) must be
#    preserved — do not recombine them into a single multi-purpose email.
def draft_rfq_email(specs, vendor, email_type: str = "quote_request") -> str:
    """Generate a supplier outreach email.

    email_type:
      "quote_request"      — requests a specific quote for the part being sourced (default).
      "partner_invitation" — invites the vendor to join the Arkim supplier network.

    These are intentionally two separate emails. Do not recombine them.
    """
    to_address = vendor.contact_email or "[vendor email — to be sourced via partner onboarding]"

    if email_type == "partner_invitation":
        body = f"""To: {to_address}
Subject: Arkim AI — partnership opportunity for industrial parts suppliers

Hello,

Arkim AI is building a maintenance and procurement platform for industrial facilities.
We're inviting parts suppliers and distributors to join our early supplier network.

What we're building:

We connect industrial maintenance teams with suppliers when they need replacement parts.
Our diagnostic AI identifies the part, and we route the sourcing request to suppliers
in our network.

What we're offering early partners:

  • Free placement in our supplier network — no fees to onboard
  • Direct sourcing requests from facilities in your service area
  • Early input into how the platform serves your business

If this is interesting, learn more at https://partners.arkim.ai

Thanks for your time.

Arkim AI
partners@arkim.ai"""
    else:
        pn   = specs.part_number if specs.part_number not in ("N/A", "UNKNOWN-PN", "Unknown", None) else "—"
        desc = specs.description or "—"
        qty  = "1"

        body = f"""To: {to_address}
Subject: Quote request — {specs.manufacturer} {specs.model}, PN {pn}

Hello,

I'm reaching out from Arkim AI on behalf of one of our facility customers who is
sourcing the part below. Your company appeared in our search for specialists in
this category.

Item: {specs.manufacturer} {specs.model}
Part Number: {pn}
Description: {desc}
Quantity: {qty}

If you stock or can supply this part, we'd appreciate:

  • Unit price
  • Lead time
  • Shipping terms (FOB origin or destination)
  • Stock availability

Please reply directly to this email or contact procurement@arkim.ai.

Thanks for your time.

Arkim AI
procurement@arkim.ai"""

    return body.strip() + _EMAIL_DEMO_FOOTER
