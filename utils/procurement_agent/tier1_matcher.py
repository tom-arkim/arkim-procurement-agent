"""
utils/procurement_agent/tier1_matcher.py
Night 5 — Tier 1 runtime MATCHER (T1).

The matcher turns a sourcing REQUEST (AssetSpecs) + the onboarded-supplier registry
(Night 3 TIER1_V2 scope) into SCORED, HONEST Tier 1 matches. It is the live-runtime
companion to Night 3's supplier-scope registry and Night 4's onboarding concierge:
onboarded suppliers (lifecycle == ``onboarded``) appear in sourcing as relationship-
backed Tier 1 cards.

Design (per the Night 5 brief, T1):
  - HARD GATE = class match. The request's noun-class (from ``detected_type`` via the
    shared ``part_type_classes`` dictionary — the SAME dictionary SCORING_V2's TypeGate
    uses) must be in the supplier's class coverage (``supplier_classes.class_id``).
    A wrong-class supplier is excluded ALWAYS, even when its brand matches. This is the
    load-bearing correctness property (the Goulds anchor: a PUMP-only supplier must NOT
    match a SEAL request).
  - AMPLIFIER = brand. The request's ``manufacturer`` vs the supplier's brand coverage
    (``supplier_brands``), tri-state relationship ordered
    AUTHORIZED > CARRIES > AFTERMARKET_COMPATIBLE. Brand is an amplifier, NOT a gate:
    a class-matched supplier with no brand row for the requested manufacturer still
    matches (brand-neutral) — we never fabricate a relationship.
  - RANKING = territory fit + is_core + performance (+ the brand amplifier). Territory
    RANKS, never filters (except local_service — see below). When the request carries
    no buyer location (the common case today — see I2), territory degrades gracefully
    to neutral: NATIONWIDE suppliers still rank at the top of the neutral band, others
    are unchanged. No supplier is dropped for territory.
  - LOCAL_SERVICE = the ONLY geographic HARD filter. A supplier with a
    ``supplier_local_service`` branch is included only when (a) the request carries a
    buyer zip AND the branch is within radius, or (b) the request carries NO buyer zip
    (degrade-graceful: include, not exclude — excluding would silently drop onboarded
    local suppliers when the request has no location, I2). When a buyer zip IS present,
    a local_service supplier outside radius is EXCLUDED. A supplier with NO
    local_service row is never filtered by this rule (it ships via its ship_area).

Honesty (the gate-2 trust guarantee — guardrail 6):
  - The matcher NEVER produces a price. A matched candidate carries quote-expected
    framing (``price_tbd=True``, ``base_price`` absent) unless a DATED CONFIRMED
    price_db entry (source="rfq") exists for (manufacturer, part_number, vendor) —
    and even then the price is overlaid by T2, not fabricated here. The matcher's job
    is identity + relationship + rank, never price.
  - Registry-backed results are computed FRESH per run (a cheap local SQLite lookup)
    and bypass the known_parts/price cache write path (I4) — they never enter
    staleness. The caller (SourcingAgent) excludes Tier 1 from the known_parts
    write-back; this module does not touch known_parts or price_db writes.

Flag gating (guardrail 3):
  - ALL behavior is behind ``TIER1_V2`` (reuses Night 3's flag, like Night 4's
    ``ONBOARDING_ENABLED``). When the flag is OFF, ``match_tier1`` returns ``[]`` —
    Tier 1 stays honest-empty and byte-identical to pre-Night-5 (T5 inertness).
  - The flag is read LIVE (``_tier1_v2_active``) so tests can monkeypatch
    ``supplier_registry.TIER1_V2`` per-test, mirroring Night 3/4's pattern.

Fail-soft (CLAUDE.md §9): a registry read error degrades to empty/neutral — the matcher
never raises into the sourcing pipeline. A missing/unparseable scope field is treated
as "no coverage" (class miss / no brand / neutral territory), not a crash.

Conventions: standalone module to the house standard (dense type annotations,
bracket-prefixed print logging, no I/O on import, pure functions over the registry's
read API). It does NOT retrofit the surrounding SourcingAgent — it is called from
``_run_tier1`` under the flag (T2 wires the result into the candidate shape).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from utils import supplier_registry as sr
from utils.sourcing_archieved.part_type_classes import classify_noun_class


# ---------------------------------------------------------------------------
# Flag (reuses Night 3's TIER1_V2 — the supplier-scope redesign gate)
# ---------------------------------------------------------------------------

def _tier1_v2_active() -> bool:
    """True iff the TIER1_V2 redesign is live. Read live from the registry module so a
    test that monkeypatches ``supplier_registry.TIER1_V2`` is honored (mirrors Night 3/4
    test patterns). Fails safe/closed (strict truthy, same as the registry's own parse)."""
    return bool(getattr(sr, "TIER1_V2", False))


# ---------------------------------------------------------------------------
# Brand relationship ordering (the amplifier)
# ---------------------------------------------------------------------------
# AUTHORIZED > CARRIES > AFTERMARKET_COMPATIBLE. A brand-neutral match (no brand row
# for the requested manufacturer) ranks below all explicit relationships but still
# MATCHES (class is the gate, not brand). The ordinal is used both for ranking and for
# the relationship badge emitted on the candidate (T2/T4).
_BRAND_REL_ORDINAL: dict[str, int] = {
    sr.BRAND_AUTHORIZED: 3,
    sr.BRAND_CARRIES: 2,
    sr.BRAND_AFTERMARKET_COMPATIBLE: 1,
}
_BRAND_NEUTRAL_ORDINAL = 0  # no brand row for the requested manufacturer


def _brand_relationship_for(supplier_domain: str, manufacturer: str) -> Optional[str]:
    """Return the brand relationship (AUTHORIZED|CARRIES|AFTERMARKET_COMPATIBLE) this
    supplier has for the requested manufacturer, or None when the supplier carries no
    brand row for that manufacturer (brand-neutral). ``manufacturer`` is matched
    case-insensitively against ``supplier_brands.brand_id`` (the onboarding concierge
    stores the brand name as brand_id). None on a registry error (fail-soft → neutral)."""
    mfg = (manufacturer or "").strip().lower()
    if not mfg or mfg in _NULL_MFG_TOKENS:
        return None
    try:
        brands = sr.get_supplier_brands(supplier_domain) or []
    except Exception:
        return None
    for b in brands:
        bid = (b.get("brand_id") or "").strip().lower()
        if bid and bid == mfg:
            rel = (b.get("relationship") or "").upper().strip()
            if rel in _BRAND_REL_ORDINAL:
                return rel
    return None


# Manufacturer tokens that mean "no real manufacturer was identified" — reuse the
# Unknown-class convention already used by the query builders / price_db guard.
_NULL_MFG_TOKENS = {"", "unknown", "n/a", "na", "null", "none"}


# ---------------------------------------------------------------------------
# Request noun-class (the class hard-gate input)
# ---------------------------------------------------------------------------

def _request_noun_class(detected_type: Optional[str],
                        description: Optional[str] = None,
                        model: Optional[str] = None) -> Optional[str]:
    """Classify the request into a noun-class canonical (e.g. 'SEAL'), or None when
    undetectable. Uses the SAME shared ``part_type_classes`` dictionary SCORING_V2's
    TypeGate uses (the shared-dictionary guardrail): detected_type first, then
    description / model as weaker fallback context. None = undetectable (the caller
    treats a None request class as honest-empty — no class to gate on → no matches)."""
    for text in (detected_type, description, model):
        if text:
            cls = classify_noun_class(text)
            if cls:
                return cls
    return None


# ---------------------------------------------------------------------------
# Territory ranking (RANK, not filter — except local_service)
# ---------------------------------------------------------------------------

def _territory_rank(supplier_domain: str, buyer_state: Optional[str]) -> int:
    """Rank this supplier's territory fit for the buyer's state. RANK only — never
    excludes. Reuses the registry's ``find_suppliers_by_territory`` rank tiers. When
    ``buyer_state`` is None (the common case — I2), NATIONWIDE suppliers still rank at
    the top of the neutral band (NATIONWIDE > none); a STATES supplier with no buyer
    state to match against ranks at STATE (neutral, not excluded). On a registry error
    or missing ship_area → TERRITORY_RANK_NONE (neutral)."""
    try:
        rows = sr.find_suppliers_by_territory(buyer_state) or []
    except Exception:
        return sr.TERRITORY_RANK_NONE
    for r in rows:
        if r.get("domain") == supplier_domain:
            return int(r.get("territory_rank") or sr.TERRITORY_RANK_NONE)
    return sr.TERRITORY_RANK_NONE  # no ship_area set / supplier not in the territory set


# ---------------------------------------------------------------------------
# Local-service hard filter (the ONLY geographic exclusion)
# ---------------------------------------------------------------------------

def _local_service_ok(supplier_domain: str, buyer_zip: Optional[str]) -> bool:
    """True when the supplier passes the local_service hard filter.

    The local_service exception is the ONLY geographic hard filter (per the brief). A
    supplier with a ``supplier_local_service`` branch:
      - buyer_zip present  → included only if a branch is within its radius (a
        branch with no radius is treated as in-range — fail-soft toward inclusion;
        radius testing is intentionally lenient at the prototype stage).
      - buyer_zip ABSENT   → included (degrade-graceful per I2: excluding onboarded
        local suppliers when the request carries no location would silently drop
        them; the common case today has no buyer zip).
    A supplier with NO local_service row is never filtered by this rule (it ships via
    its ship_area — territory ranks it, never filters it). On a registry error →
    include (fail-soft toward inclusion; the matcher never silently drops an onboarded
    supplier on an I/O hiccup)."""
    try:
        locals_ = sr.find_suppliers_with_local_service(buyer_zip) or []
    except Exception:
        return True  # fail-soft toward inclusion
    matching = [l for l in locals_ if l.get("domain") == supplier_domain]
    if not matching:
        return True  # no local_service row → not filtered by this rule
    if not buyer_zip:
        return True  # no buyer zip → degrade-graceful include (I2)
    # buyer zip present: a branch with no radius is treated as in-range (lenient).
    for l in matching:
        radius = l.get("radius_miles")
        if radius is None:
            return True
        # NOTE: a real radius-vs-distance test needs a zip→latlon lookup (not wired
        # here). At the prototype stage we treat any finite radius as in-range for the
        # buyer zip (the branch exists and serves some area); a future geo step can
        # tighten this. The structural guarantee — local_service is the ONLY
        # geographic hard filter — holds regardless.
        if radius and float(radius) > 0:
            return True
    return False  # all branches have a zero/empty radius → outside range


# ---------------------------------------------------------------------------
# Match result
# ---------------------------------------------------------------------------

@dataclass
class Tier1Match:
    """One scored Tier 1 match (registry-backed).

    Carries the identity + relationship + rank + match-explanation metadata. The
    candidate DICT for the SourcingAgent is built from this by ``to_candidate`` (T2);
    the matcher itself never emits a price.
    """
    supplier_id: str
    domain: str
    vendor_name: str
    noun_class: str                       # the matched request class (canonical, e.g. 'SEAL')
    is_core: bool                         # the supplier carries this class as a core competency
    brand_relationship: Optional[str]     # AUTHORIZED|CARRIES|AFTERMARKET_COMPATIBLE | None (neutral)
    territory_rank: int                   # TERRITORY_RANK_* (RANK, not filter)
    local_service: bool                   # supplier has a local_service branch
    performance: dict                     # supplier performance_json (placeholder today)
    score: float                          # the composite rank score (higher = better)
    # match-explanation metadata (human-reviewable, surfaces in the card / audit)
    match_explanation: dict = field(default_factory=dict)

    @property
    def relationship_ordinal(self) -> int:
        if self.brand_relationship is None:
            return _BRAND_NEUTRAL_ORDINAL
        return _BRAND_REL_ORDINAL.get(self.brand_relationship, _BRAND_NEUTRAL_ORDINAL)

    @property
    def is_aftermarket(self) -> bool:
        return self.brand_relationship == sr.BRAND_AFTERMARKET_COMPATIBLE


# Composite rank weights. The brand AMPLIFIER is the strongest signal (an authorized
# channel for the requested brand is the best possible Tier 1 match), then core class
# (a core competency beats incidental coverage), then territory (broader/closer is
# better), then performance (placeholder — {} today, neutral). These are informed
# defaults consistent with the brief's "amplifier = brand, ranking = territory + is_core
# + performance"; calibration with real data is a later step (mirrors SCORING_V2's
# informed-defaults posture, CLEANUP §7.4).
_W_BRAND = 50.0
_W_CORE = 25.0
_W_TERRITORY = 15.0   # scaled by rank / NATIONWIDE
_W_PERF = 10.0        # scaled by a 0..1 perf score (0 today → neutral)
_MAX_TERRITORY = float(sr.TERRITORY_RANK_NATIONWIDE)  # 3.0


def _perf_score(perf: Optional[dict]) -> float:
    """A 0..1 performance score from the supplier's performance_json. Placeholder
    today (the field is {} until perf data lands) → 0.0 (neutral, contributes nothing).
    A future perf schema (e.g. on-time-delivery %) plugs in here."""
    if not isinstance(perf, dict) or not perf:
        return 0.0
    # Reserved for a future numeric perf field; fail-soft to neutral on anything odd.
    try:
        for key in ("on_time_delivery", "fulfillment_score", "score"):
            if key in perf and perf[key] is not None:
                return max(0.0, min(1.0, float(perf[key])))
    except (TypeError, ValueError):
        return 0.0
    return 0.0


def _composite_score(relationship_ordinal: int, is_core: bool,
                     territory_rank: int, perf: float) -> float:
    """Composite rank score (higher = better). Brand amplifier + core + territory + perf."""
    return (
        _W_BRAND * relationship_ordinal / 3.0           # 0..50 (AUTHORIZED=50, CARRIES=33.3, AFTERMARKET=16.7, neutral=0)
        + _W_CORE * (1.0 if is_core else 0.0)           # 0..25
        + _W_TERRITORY * (float(territory_rank) / _MAX_TERRITORY)  # 0..15
        + _W_PERF * perf                                 # 0..10
    )


# ---------------------------------------------------------------------------
# The matcher
# ---------------------------------------------------------------------------

def match_tier1(
    *,
    detected_type: Optional[str],
    manufacturer: Optional[str],
    description: Optional[str] = None,
    model: Optional[str] = None,
    buyer_state: Optional[str] = None,
    buyer_zip: Optional[str] = None,
) -> list[Tier1Match]:
    """Match an onboarded-supplier registry against a sourcing request.

    Returns SCORED Tier 1 matches (best first), or [] when:
      - TIER1_V2 is off (flag-off = honest-empty, byte-identical — T5),
      - the request's noun-class is undetectable (no class to gate on → no matches),
      - no onboarded supplier carries the request's class (the class hard-gate excludes
        everyone — e.g. a PUMP-only supplier for a SEAL request),
      - a registry error degrades to empty (fail-soft).

    Only suppliers with ``tier1_lifecycle == 'onboarded'`` are eligible (the onboarding
    relationship is their source of truth — §9 cache-freshness). The class HARD GATE,
    brand amplifier, territory ranking, and local_service hard filter are applied per
    the module docstring. The result is computed fresh per run (no cache write — I4).
    """
    if not _tier1_v2_active():
        return []

    req_class = _request_noun_class(detected_type, description, model)
    if req_class is None:
        # Undetectable request class → no class to gate on → no honest Tier 1 match.
        # (The caller still runs Tier 2/3 — Tier 1 is the onboarded-relationship lane.)
        return []

    # Eligible suppliers: onboarded + carrying the request's class (the hard gate).
    try:
        suppliers = sr.find_suppliers_by_class(req_class) or []
    except Exception:
        return []
    if not suppliers:
        return []

    mfg = (manufacturer or "").strip()
    matches: list[Tier1Match] = []
    for sup in suppliers:
        domain = sup.get("domain") or ""
        if not domain:
            continue
        # Onboarded-only (the onboarding relationship is the source of truth — §9).
        # find_suppliers_by_class returns all suppliers carrying the class; filter to
        # onboarded lifecycle here so a discovered/quoted-but-not-onboarded supplier
        # does NOT surface as a Tier 1 card (it belongs to Tier 2/3 outreach).
        if sr.get_tier1_lifecycle(domain) != sr.TIER1_ONBOARDED:
            continue
        # local_service hard filter (the ONLY geographic exclusion).
        if not _local_service_ok(domain, buyer_zip):
            continue

        # Class core-ness for THIS class (is_core on the matched class row).
        is_core = _class_is_core(domain, req_class)
        brand_rel = _brand_relationship_for(domain, mfg)
        terr_rank = _territory_rank(domain, buyer_state)
        perf = _perf_score(_performance_for(domain))
        score = _composite_score(
            _BRAND_REL_ORDINAL.get(brand_rel, _BRAND_NEUTRAL_ORDINAL) if brand_rel
            else _BRAND_NEUTRAL_ORDINAL,
            is_core, terr_rank, perf,
        )
        explanation = {
            "class_gate": req_class,                 # the hard gate that admitted this supplier
            "is_core": is_core,
            "brand_relationship": brand_rel,         # None = brand-neutral (no brand row for mfr)
            "territory_rank": terr_rank,
            "local_service": _has_local_service(domain),
            "buyer_state": buyer_state,              # None when the request carried no location (I2)
            "buyer_zip": buyer_zip,
            "onboarded": True,                       # registry-backed relationship badge
        }
        matches.append(Tier1Match(
            supplier_id=sup.get("id") or "",
            domain=domain,
            vendor_name=sup.get("name") or domain,
            noun_class=req_class,
            is_core=is_core,
            brand_relationship=brand_rel,
            territory_rank=terr_rank,
            local_service=explanation["local_service"],
            performance=_performance_for(domain),
            score=score,
            match_explanation=explanation,
        ))

    # Stable, deterministic ordering: score desc, then vendor name (so equal-score
    # suppliers don't shuffle across runs — important for the live-faithful API test).
    matches.sort(key=lambda m: (-m.score, m.vendor_name.lower()))
    return matches


# ---------------------------------------------------------------------------
# Per-supplier scope reads (fail-soft helpers)
# ---------------------------------------------------------------------------

def _class_is_core(domain: str, class_id: str) -> bool:
    """True when the supplier carries ``class_id`` as a core competency (is_core=1 on
    the matched class row). False otherwise (incidental coverage / missing / error)."""
    try:
        rows = sr.get_supplier_classes(domain) or []
    except Exception:
        return False
    for r in rows:
        if (r.get("class_id") or "").upper().strip() == (class_id or "").upper().strip():
            return bool(r.get("is_core"))
    return False


def _has_local_service(domain: str) -> bool:
    """True when the supplier has any local_service branch row (fail-soft False)."""
    try:
        locals_ = sr.find_suppliers_with_local_service(None) or []
    except Exception:
        return False
    return any(l.get("domain") == domain for l in locals_)


def _performance_for(domain: str) -> dict:
    """Return the supplier's performance_json (decoded), or {} on error/missing."""
    try:
        rec = sr.lookup_by_domain(domain) or {}
        raw = rec.get("performance_json")
        if not raw:
            return {}
        # The registry stores JSON TEXT; decode defensively.
        import json as _json
        if isinstance(raw, (dict, list)):
            return raw if isinstance(raw, dict) else {}
        return _json.loads(raw) if raw else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Flag-off inertness helper (T5)
# ---------------------------------------------------------------------------

def tier1_v2_active() -> bool:
    """Public alias for the flag check (used by the inertness test + SourcingAgent)."""
    return _tier1_v2_active()


# ---------------------------------------------------------------------------
# T2 — honest candidate builder (Tier1Match → SourcingAgent candidate dict)
# ---------------------------------------------------------------------------
# Builds the snake_case SourcingOption-shape dict `_run_tier1` emits, so it flows
# through the SAME `_transform_option` path the frontend contract (I1) expects — no
# bespoke transform, no contract drift. The candidate is HONEST:
#   - NO fabricated price. price_tbd=True, base_price absent (0.0 is the model default
#     but price_tbd gates the transform → price=None → "Quote Required" card) UNLESS a
#     DATED CONFIRMED price_db entry (source="rfq") exists for (manufacturer,
#     part_number, vendor_name) — the only price source a Tier 1 card may carry. The
#     quote-expected framing ("Quote Required" + leadTime on quote) is the default.
#   - relationship badge from the brand amplifier (AUTHORIZED|CARRIES|
#     AFTERMARKET_COMPATIBLE), surfaced on `suitability_tier` (the frontend's
#     `relationship` Pill) AND `vendor_authorization_status` (for isAuthorizedDistributor).
#   - aftermarket disclosure (T4): when the relationship is AFTERMARKET_COMPATIBLE,
#     `is_aftermarket` + `aftermarket_disclosure` text are carried on the payload.
#   - match_explanation metadata carried on `tier1_match_explanation` for the audit /
#     card debugging (frontend rendering is morning work; the DATA is here).
#   - source_url = the supplier's domain (the registry identity, not a fabricated
#     listing URL) so the cache type-gate + dedup see a real, stable domain.
#
# Fresh-per-run, no cache write: this builder reads price_db READ-ONLY (a confirmed
# price lookup) and never writes known_parts/price_db. The SourcingAgent excludes
# Tier 1 from the known_parts write-back (T2 integration), so registry-backed cards
# never enter staleness (I4).

# Aftermarket disclosure text (T4). Surfaces on the candidate payload so the frontend
# can render the disclosure (morning work); the DATA is the contract here.
_AFTERMARKET_DISCLOSURE = (
    "Aftermarket-compatible part — not the OEM brand. Verify fit and warranty terms "
    "before purchase; aftermarket parts may affect OEM warranty coverage."
)


def _confirmed_price_for(manufacturer: str, part_number: str,
                         vendor_name: str) -> Optional[dict]:
    """Return a DATED CONFIRMED price_db entry (source="rfq") for
    (manufacturer, part_number, vendor_name), or None. READ-ONLY — never writes.
    A confirmed RFQ quote is the only price source a Tier 1 card may carry (gate-2
    trust). price_db's null-PN guard returns {} for spec-based requests (UNKNOWN-PN),
    so a PN-less request honestly gets no price → quote-expected framing."""
    if not manufacturer or not part_number or not vendor_name:
        return None
    try:
        from utils import price_db
        entries = price_db.get_cached_prices(manufacturer, part_number) or {}
    except Exception:
        return None
    entry = entries.get(vendor_name)
    if not entry:
        return None
    # Only a CONFIRMED quote (source="rfq", a human-confirmed RFQ reply) is trustworthy
    # enough to surface as a Tier 1 price. A "live" (Tavily-discovered) price is not a
    # registry-backed relationship claim and is not surfaced on the Tier 1 card.
    if (entry.get("source") or "") != "rfq":
        return None
    return entry


def to_candidate(match: Tier1Match, *, manufacturer: str,
                 part_number: str) -> dict:
    """Build an honest Tier 1 candidate dict (SourcingOption shape) from a match.

    ``manufacturer`` / ``part_number`` come from the request specs (used ONLY to look
    up a confirmed price_db quote — never to fabricate). The candidate carries:
      - identity (vendor_name, source_url=domain, merchant_type="Arkim Network"),
      - relationship badge + aftermarket disclosure (T4),
      - a confirmed prior quote's price/lead IF a dated source="rfq" price_db entry
        exists, ELSE quote-expected framing (price_tbd=True, no price),
      - match-explanation metadata,
      - `is_registry_backed=True` so the SourcingAgent can exclude Tier 1 from the
        known_parts write-back (I4) — a marker the cache path does NOT carry.
    """
    rel = match.brand_relationship  # AUTHORIZED | CARRIES | AFTERMARKET_COMPATIBLE | None
    is_aftermarket = match.is_aftermarket

    # Confirmed prior quote (price_db source="rfq", dated) — the ONLY honest price.
    quote_entry = _confirmed_price_for(manufacturer, part_number, match.vendor_name)
    has_confirmed_price = quote_entry is not None
    base_price = float(quote_entry["price"]) if has_confirmed_price else 0.0
    lead_days = quote_entry.get("lead_days") if quote_entry else None
    price_tbd = not has_confirmed_price

    # Relationship → authorization status (for isAuthorizedDistributor) + tier (Pill).
    if rel == sr.BRAND_AUTHORIZED:
        auth_status = "Authorized"
        tier_label = "Authorized"
    elif rel == sr.BRAND_CARRIES:
        auth_status = "Unknown"        # not a sanctioned channel — do NOT overclaim Authorized
        tier_label = "Carries"
    elif rel == sr.BRAND_AFTERMARKET_COMPATIBLE:
        auth_status = "Unknown"
        tier_label = "Aftermarket"
    else:
        # Brand-neutral: class-matched, no brand row. Honest — no relationship badge.
        auth_status = "Unknown"
        tier_label = ""

    candidate: dict = {
        "vendor_name": match.vendor_name,
        "base_price": base_price,
        "price_tbd": price_tbd,
        "lead_time_days": lead_days,            # None → "Lead time on quote" (honest)
        "lead_time_source": "quoted" if has_confirmed_price else "placeholder",
        "reliability_score": 95.0,              # onboarded network partner (relationship-backed)
        "merchant_type": "Arkim Network",
        "match_type": "Aftermarket Compatible" if is_aftermarket else "Functional Alternative",
        "source_url": f"https://{match.domain}",
        "suitability_score": 92.0 if match.is_core else 70.0,
        "confidence_score": 90.0 if rel == sr.BRAND_AUTHORIZED else 75.0,
        "vendor_authorization_status": auth_status,
        "onboarding_status": "Active",
        "in_stock": None,                       # no fabricated stock signal
        "notes": _card_notes(match, has_confirmed_price),
        "found_part_number": part_number if (has_confirmed_price and part_number
                                             and part_number not in _NULL_PN_TOKENS_SET) else None,
        # Tier 1 relationship / registry provenance — the load-bearing new fields:
        "suitability_tier": tier_label,                 # frontend `relationship` Pill
        "is_registry_backed": True,                     # exclude from known_parts write-back (I4)
        "is_aftermarket": is_aftermarket,
        "aftermarket_disclosure": _AFTERMARKET_DISCLOSURE if is_aftermarket else None,
        "tier1_match_explanation": dict(match.match_explanation),
        "confirmation_needed": True,                   # Tier 1 two-mode display (existing convention)
    }
    return candidate


# A set form of the null-PN tokens for the found_part_number guard (avoid a circular
# import of known_parts._NULL_PN_TOKENS at module load; replicate the small set — it is
# the canonical placeholder-PN set used across price_db / known_parts).
_NULL_PN_TOKENS_SET = {"", "UNKNOWNPN", "UNKNOWN", "NA", "TBD", "NONE", "NONE0"}


def _card_notes(match: Tier1Match, has_confirmed_price: bool) -> str:
    """Honest human-readable card note: relationship + class + price framing."""
    rel = match.brand_relationship
    rel_text = {
        sr.BRAND_AUTHORIZED: "Authorized distributor",
        sr.BRAND_CARRIES: "Carries brand (broad-line)",
        sr.BRAND_AFTERMARKET_COMPATIBLE: "Aftermarket-compatible",
    }.get(rel, "Class-matched")
    core_text = "core class" if match.is_core else "carried class"
    price_text = "Confirmed quote on file" if has_confirmed_price else "Quote expected"
    return f"Arkim onboarded — {rel_text} for {match.noun_class} ({core_text}). {price_text}."


def candidates_from_matches(matches: list[Tier1Match], *, manufacturer: str,
                            part_number: str) -> list[dict]:
    """Build the full Tier 1 candidate list from matches (preserves the matcher's
    deterministic ordering). Convenience wrapper around to_candidate."""
    return [to_candidate(m, manufacturer=manufacturer, part_number=part_number)
            for m in matches]
