"""
utils/sourcing/__init__.py
Multi-Tier Sourcing Engine — package entry point.

Mutable module-level state (TAVILY_API_KEY, ANTHROPIC_API_KEY, _EXTRACTION_MODEL, _tavily)
is defined here so that chat_app._patch_sourcing_keys() can assign to them directly.
All submodules that need these values do so lazily inside function bodies via:
    import utils.sourcing as _pkg
    value = _pkg.ANTHROPIC_API_KEY
which avoids circular imports because function bodies execute after __init__.py finishes.
"""

import os

try:
    from tavily import TavilyClient
except ImportError:
    from tavily import Client as TavilyClient

# ---------------------------------------------------------------------------
# Mutable state — patched at runtime by chat_app._patch_sourcing_keys()
# ---------------------------------------------------------------------------

TAVILY_API_KEY    = os.environ.get("TAVILY_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
_EXTRACTION_MODEL = os.environ.get("OS_EXTRACTION_MODEL", "claude-haiku-4-5-20251001")

_tavily = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None

# ---------------------------------------------------------------------------
# Re-exports — all symbols that external callers import from utils.sourcing
# ---------------------------------------------------------------------------

from utils.sourcing.constants import (           # noqa: E402
    TARGET_VENDORS,
    _VENDOR_DOMAINS,
    HIGH_RISK_ELECTRICAL_CATEGORIES,
    _VERIFIED_PARTNERS,
    _MERCHANT_RELIABILITY,
    _TIER1_VENDORS,
    _BLACKLISTED_DOMAINS,
    _AUTHORITY_VIABLE_THRESHOLD,
    _DYNAMIC_FALLBACK_MIN_VIABLE,
    _COLLECTION_URL_PATTERNS,
    _HIGH_COUNTERFEIT_RISK_CATEGORIES,
    _MARKETPLACE_DOMAINS,
    TIER_SURFACE_MIN_CONFIDENCE,
    AFTERMARKET_VIABLE_CATEGORIES,
)
from utils.sourcing.tier3_outreach import (      # noqa: E402
    draft_rfq_email,
    EMAIL_SEND_ENABLED,
)
from utils.sourcing.vendor_tokens import (       # noqa: E402
    _onboarding_url,
    _get_vendor_token,
)
from utils.sourcing.tavily_client import (       # noqa: E402
    _build_search_query,
    _vendor_authority_score,
    _build_tier2_query,
    _search_vendor_prices,
)
from utils.sourcing.scoring import (             # noqa: E402
    _is_collection_url,
    _compute_suitability_score,
    _suitability_tier,
    _home_field_bonus,
    _compute_confidence_score,
    _detect_equip_type,
    _counterfeit_suitability_penalty,
)
from utils.sourcing.llm_parsing import (         # noqa: E402
    _anthropic_complete,
    _llm_parse_results,
)
from utils.sourcing.price_sanity import (        # noqa: E402
    _apply_price_sanity,
    _sanity_threshold,
    _apply_extreme_price_filter,
)
from utils.sourcing.market_confidence import (   # noqa: E402
    _fetch_market_confidence,
)
from utils.sourcing.filtering import (           # noqa: E402
    _counterfeit_risk_flag,
    _apply_warranty_filter,
    _apply_registry_enrichment,
    _apply_confidence_floor,
    _apply_category_mismatch_guard,
)
from utils.sourcing.enterprise_search import (   # noqa: E402
    _call_enterprise_api,
    _discover_national_specialists,
    _discover_aftermarket_specialists,
    _vendor_merchant_type,
    _base_reliability,
    _is_heavy_item,
)
from utils.sourcing.orchestrator import (        # noqa: E402
    find_vendors,
)
