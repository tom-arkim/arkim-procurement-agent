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
