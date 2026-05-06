"""
utils/sourcing/constants.py
All hardcoded sets, lists, dicts, and thresholds for the sourcing pipeline.

Centralised here so that future replacement by brand_intelligence or dynamic
discovery is a clean substitution in one file rather than scattered edits.
"""

# ---------------------------------------------------------------------------
# Tier 1 / 1.5 vendor identity
# ---------------------------------------------------------------------------

TARGET_VENDORS = [
    # Tier 1: Generalist Enterprise
    "Grainger", "McMaster-Carr", "MSC Industrial",
    # Tier 1.5: Industrial & Equipment Specialists
    "Motion Industries", "Applied Industrial", "Pumpman",
    "Pump Products", "Pump Catalog", "Zoro", "Global Industrial", "Fastenal",
]

_VENDOR_DOMAINS = [
    # Tier 1
    "grainger.com", "mcmaster.com", "mscdirect.com",
    # Tier 1.5: industrial specialists
    "motionindustries.com", "applied.com", "pumpman.com",
    # Tier 1.5: pump & equipment specialists
    "pumpproducts.com", "pumpcatalog.com",
    # Tier 1.5: broad industrial
    "zoro.com", "globalindustrial.com", "fastenal.com",
]

# Vendors whose prices appear on sites without login walls -> "Enterprise"
# Everything else (including Tier 1.5 specialists) -> "National Specialist"
_TIER1_VENDORS = {"Grainger", "McMaster-Carr", "MSC Industrial"}

# ---------------------------------------------------------------------------
# Dynamic Tier 1 discovery
# ---------------------------------------------------------------------------

# Domains excluded from Tier 1 dynamic discovery (consumer / marketplace platforms).
_BLACKLISTED_DOMAINS = (
    "amazon", "ebay", "aliexpress", "alibaba", "walmart", "etsy",
    "craigslist", "offerup", "mercari",
)

_AUTHORITY_VIABLE_THRESHOLD = 30.0  # minimum authority score to count as viable
_DYNAMIC_FALLBACK_MIN_VIABLE = 3    # fall back to domain-list if fewer viable than this

# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------

# URL patterns that indicate a list/category page rather than a direct product page.
_COLLECTION_URL_PATTERNS = ("/collections/", "/search", "/catalog/", "/category/",
                             "/browse/", "?q=", "&q=", "/results")

# URL subdomains that indicate marketing / informational pages with no product data.
_LOW_VALUE_SUBDOMAINS = (
    "info.", "lp.", "marketing.", "blog.", "support.", "news.", "resources.",
)

# ---------------------------------------------------------------------------
# Vendor network
# ---------------------------------------------------------------------------

_VERIFIED_PARTNERS: set[str] = set()   # populated from DB in production; empty = no Gold partners yet

# Reliability by merchant tier — no per-vendor hardcoding; MCS refines at quote time.
_MERCHANT_RELIABILITY: dict[str, float] = {
    "Enterprise":           90.0,
    "National Specialist":  82.0,
    "Direct Buy via Arkim": 78.0,
    "Quote Request":        75.0,
}

# ---------------------------------------------------------------------------
# Risk / compliance
# ---------------------------------------------------------------------------

# 1.8 — High-risk electrical categories (require voltage/phase confirmation)
HIGH_RISK_ELECTRICAL_CATEGORIES = {
    "motor", "drive", "controller", "transformer",
    "power_supply", "vfd", "variable frequency drive",
}

# ---------------------------------------------------------------------------
# Quality filter thresholds
# ---------------------------------------------------------------------------

# Minimum confidence_score (0-100) for a result to surface in the TCA comparison table.
# Results below this are annotated with rejection_reason="confidence_below_floor" and
# excluded from the priced-option table, but NOT from Tier 3 outreach (see Fix 1).
#
# Calibration: the maximum achievable confidence for a "Functional Alternative" result
# with no OEM PN in the snippet is 42.5 (suit_pts=22.5 + match_pts=10 + spec_pts=10).
# Setting the floor at 40.0 allows these legitimate candidates to surface while still
# filtering genuinely low-quality results (confidence < 40 means poor suitability AND
# no PN confirmation AND incomplete specs).
#
# The extreme_price_outlier filter and pn_mismatch annotation are the primary defenses
# against wrong-product results. This floor is a secondary backstop, not the first line.
#
# Future refinement (Option B): apply a tighter floor (50) only to priced TCA candidates
# and a looser floor (35) to price_tbd inquiry candidates, with no floor for Tier 3.
TIER_SURFACE_MIN_CONFIDENCE: float = 40.0

# ---------------------------------------------------------------------------
# Aftermarket sourcing
# ---------------------------------------------------------------------------

# Part categories for which spec-based aftermarket discovery runs (when not in-warranty).
AFTERMARKET_VIABLE_CATEGORIES: set[str] = {
    "mechanical seal", "seal kit", "bearing", "gasket",
    "o-ring", "belt", "coupling", "motor", "valve",
    "shaft seal", "lip seal", "v-belt",
}

# 1.9 — Categories with elevated counterfeit risk in the industrial supply chain.
_HIGH_COUNTERFEIT_RISK_CATEGORIES: set[str] = {
    "bearing", "seal", "vfd", "variable frequency drive", "contactor",
    "relay", "sensor", "belt", "coupling", "motor starter",
}

# 1.9 — Marketplace domains with elevated counterfeit risk.
_MARKETPLACE_DOMAINS: set[str] = {
    "amazon.com", "ebay.com", "aliexpress.com", "alibaba.com",
    "wish.com", "dhgate.com", "made-in-china.com",
}
