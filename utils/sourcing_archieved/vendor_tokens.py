"""
utils/sourcing/vendor_tokens.py
Per-vendor UUID4 token generation and partner onboarding URL construction.
"""

import re
import uuid


_VENDOR_TOKENS: dict[str, str] = {}


def _get_vendor_token(vendor_name: str) -> str:
    """Return a stable UUID4 token for this vendor, creating one on first access."""
    key = vendor_name.lower()
    if key not in _VENDOR_TOKENS:
        _VENDOR_TOKENS[key] = str(uuid.uuid4())
    return _VENDOR_TOKENS[key]


def _onboarding_url(vendor_name: str, specs) -> str:
    """Generate a partner onboarding link using a random UUID4 token (not vendor-name-derived)."""
    token = _get_vendor_token(vendor_name)
    slug  = re.sub(r"[^a-z0-9]+", "-", vendor_name.lower()).strip("-")
    rfq   = re.sub(r"[^a-z0-9]+", "-", (specs.part_number or "rfq").lower())
    return f"https://partners.arkim.ai/claim?v={slug}&t={token}&rfq={rfq}"
