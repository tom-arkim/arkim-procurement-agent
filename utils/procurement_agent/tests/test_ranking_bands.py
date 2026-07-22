"""
Tests for utils/procurement_agent/ranking_bands.py — evidence-banded ranking
(RANKING_BANDS_V1, spec: RANKING_BANDS_SPEC.md).

T1 scope: flag gating, PN-evidence classification, band assignment (§3),
evidence-quality score (§4), banded ordering (band absolute > onboarded > evidence
quality > TCA-by-stability) with the randomized ordering property test, and the
SourcingAgent flag-on/flag-off integration seam.

Offline: no network, no live DB writes (_resolve_contact / _apollo_clarify patched
out on the integration tests).
"""

import json
import random
from unittest.mock import patch

import pytest

# The api TestClient fixture (temp DB, isolated registries) is reused for the
# cache-policy tests; importing it registers it with this module for pytest.
from utils.procurement_agent.tests.test_api_server import api  # noqa: F401

from utils.procurement_agent import ranking_bands as rb
from utils.procurement_agent.ranking_bands import (
    BAND_A, BAND_B, BAND_C, BAND_C_CAP,
    annotate_candidate,
    apply_ranking_bands,
    assign_band,
    banded_sort_key,
    cap_band_c,
    classify_pn_evidence,
    confidence_from_evidence,
    evidence_quality,
    is_onboarded,
    order_banded,
    pn_evidence_for,
    ranking_bands_active,
    rescope_floor,
)

# The Gusher acceptance identifiers (spec §9) used throughout.
_GUSHER_PN = "84004-28-C238CBC"


# ---------------------------------------------------------------------------
# Candidate factories (evidence shapes from the live Gusher run — run_full.json)
# ---------------------------------------------------------------------------

def _sealit(**over) -> dict:
    """Sealit123: exact PN, real URL, listed price $53.25 (Band A, spec §1)."""
    c = {
        "vendor_name": "Sealit123",
        "source_url": "https://sealit123.com/products/84004-28-c238cbc",
        "found_part_number": "84004-28-C238CBC",
        "pn_match_status": "exact_match",
        "base_price": 53.25,
        "price_tbd": False,
        "suitability_score": 70.0,
        "merchant_type": "Quote Request",
    }
    c.update(over)
    return c


def _us_seal(**over) -> dict:
    """US Seal Manufacturing: canonical base-PN found (84004-28), real URL,
    crude suitability 12.6 — the floor-rejection defect case (Band A)."""
    c = {
        "vendor_name": "US Seal Manufacturing",
        "source_url": "https://www.ussealmfg.com",
        "found_part_number": "84004-28",
        "pn_match_status": "exact_match",
        "base_price": 0.0,
        "price_tbd": True,
        "suitability_score": 12.6,
        "merchant_type": "Quote Request",
    }
    c.update(over)
    return c


def _zoro(**over) -> dict:
    """Zoro: aftermarket PN 84004-28SP found, real URL, crude 10.5 (Band B)."""
    c = {
        "vendor_name": "Zoro",
        "source_url": "https://www.zoro.com/springer-parts-mechanical-seal",
        "found_part_number": "84004-28SP",
        "pn_match_status": "partial_match",
        "base_price": 0.0,
        "price_tbd": True,
        "suitability_score": 10.5,
        "merchant_type": "National Specialist",
    }
    c.update(over)
    return c


def _mock_seed(name: str = "Phoenix Pumps", **over) -> dict:
    """Brand-intelligence distributor seed: is_mock, no URL, no PN (Band C).
    NOTE: carries the legacy fabricated 88.0/75.0 — T1 band assignment must put
    it in Band C regardless (descoring is T3)."""
    c = {
        "vendor_name": name,
        "source_url": None,
        "found_part_number": None,
        "price_tbd": True,
        "base_price": 0.0,
        "suitability_score": 88.0,
        "confidence_score": 75.0,
        "is_mock": True,
        "merchant_type": "OEM Authorized Distributor",
    }
    c.update(over)
    return c


def _dxp(**over) -> dict:
    """DXP Enterprises: onboarded Tier-1 registry class-match, no part found,
    no confirmed quote (Band C — today's category-as-answer case)."""
    c = {
        "vendor_name": "DXP Enterprises",
        "source_url": "https://dxpe.com",
        "found_part_number": None,
        "price_tbd": True,
        "base_price": 0.0,
        "suitability_score": 92.0,
        "merchant_type": "Arkim Network",
        "is_registry_backed": True,
    }
    c.update(over)
    return c


def _pivot(**over) -> dict:
    """Capability-pivot discovery: inferred capability only (Band C)."""
    c = {
        "vendor_name": "Pivot Distributors",
        "source_url": "https://pivotdist.example.com",
        "found_part_number": None,
        "price_tbd": True,
        "base_price": 0.0,
        "suitability_score": 65.0,
        "search_type": "capability_pivot",
        "merchant_type": "Capability Discovery",
    }
    c.update(over)
    return c


# ---------------------------------------------------------------------------
# Flag gating
# ---------------------------------------------------------------------------

class TestFlag:
    @pytest.mark.parametrize("token,expected", [
        ("1", True), ("true", True), ("yes", True), ("on", True), ("TRUE", True),
        ("0", False), ("false", False), ("", False), ("junk", False), (None, False),
    ])
    def test_strict_truthy_parse(self, monkeypatch, token, expected):
        if token is None:
            monkeypatch.delenv("RANKING_BANDS_V1", raising=False)
        else:
            monkeypatch.setenv("RANKING_BANDS_V1", token)
        assert ranking_bands_active() is expected


# ---------------------------------------------------------------------------
# PN evidence classification
# ---------------------------------------------------------------------------

class TestPnEvidence:
    def test_exact_normalized(self):
        assert classify_pn_evidence(_GUSHER_PN, "84004-28-C238CBC") == "exact"
        assert classify_pn_evidence(_GUSHER_PN, "8400428C238CBC") == "exact"
        assert classify_pn_evidence(_GUSHER_PN, "84004 28 c238cbc") == "exact"

    def test_canonical_base_pn_prefix(self):
        # US Seal: found the base PN of the requested configured PN.
        assert classify_pn_evidence(_GUSHER_PN, "84004-28") == "canonical"
        # Symmetric: found a configured variant of the requested base PN.
        assert classify_pn_evidence("84004-28", _GUSHER_PN) == "canonical"

    def test_compatible_aftermarket_variant(self):
        # Zoro: 84004-28SP is NOT a prefix relationship with 84004-28-C238CBC.
        assert classify_pn_evidence(_GUSHER_PN, "84004-28SP") == "compatible"

    def test_short_prefix_does_not_claim_canonical(self):
        # A trivially-short shared base must not earn "canonical".
        assert classify_pn_evidence("AB1-XYZ123", "AB1") == "compatible"

    def test_none_cases(self):
        assert classify_pn_evidence(_GUSHER_PN, None) == "none"
        assert classify_pn_evidence(_GUSHER_PN, "") == "none"
        assert classify_pn_evidence(None, "84004-28") == "none"
        assert classify_pn_evidence("UNKNOWN-PN", "84004-28") == "none"
        assert classify_pn_evidence(_GUSHER_PN, "N/A") == "none"

    def test_no_match_extractor_verdict_overrides(self):
        c = _zoro(pn_match_status="no_match")
        assert pn_evidence_for(c, _GUSHER_PN) == "none"

    def test_exact_match_claim_not_corroborated_earns_nothing(self):
        # Extractor said exact_match but the found PN is unrelated → the string
        # check caps it below exact, and the F1 family guard denies compatible
        # credit too (no shared ≥6-char base, no spec tokens): an unverifiable
        # wrong-family claim earns NOTHING (decision-#3 conservatism).
        c = _zoro(found_part_number="TOTALLY-DIFFERENT-99",
                  pn_match_status="exact_match",
                  source_url="https://www.zoro.com/some-unrelated-listing")
        assert pn_evidence_for(c, _GUSHER_PN) == "none"

    # --- F1 family guard (MATCHING_CLEANUP: wrong-family PNs earn no B credit) ---

    def test_f1_wrong_family_pn_earns_no_credit(self):
        # The Jamieson case: HHI10-36-215TC (10HP, 215TC frame) offered against
        # HHI150-12-447T (150HP, 447T frame). Shared prefix HHI1 = 4 < 6 and no
        # spec-token corroboration → no PN credit.
        c = _zoro(vendor_name="Jamieson Equipment Company",
                  found_part_number="HHI10-36-215TC",
                  pn_match_status="not_visible",
                  source_url="https://www.jamiesonequipment.com")
        assert pn_evidence_for(c, "HHI150-12-447T") == "none"

    def test_f1_spec_corroborated_cross_mfr_pn_keeps_credit(self):
        # The MROSupply case: LAM150-12-447T carries the request's rating/frame
        # tokens (150, 12, 447T) → cross-manufacturer compatible credit stands.
        c = _zoro(vendor_name="MROSupply",
                  found_part_number="LAM150-12-447T",
                  pn_match_status=None,
                  source_url="https://www.mrosupply.com/electric-motors/687438")
        assert pn_evidence_for(c, "HHI150-12-447T") == "compatible"

    def test_f1_gusher_aftermarket_prefix_branch_unchanged(self):
        # Gusher pin: 84004-28SP shares the 7-char base 8400428 → the ≥6-alnum
        # prefix branch keeps the compatible credit exactly as before.
        assert pn_evidence_for(_zoro(), _GUSHER_PN) == "compatible"

    def test_f1_listing_url_path_can_corroborate_but_query_cannot(self):
        # Corroboration may come from the listing URL PATH...
        c = _zoro(found_part_number="XYZ-9",
                  source_url="https://vendor.example.com/motors/150hp-447t-frame")
        assert pn_evidence_for(c, "HHI150-12-447T") == "compatible"
        # ...but never from a query param echoing the searched PN.
        c2 = _zoro(found_part_number="XYZ-9",
                   source_url="https://vendor.example.com/search?q=HHI150-12-447T")
        assert pn_evidence_for(c2, "HHI150-12-447T") == "none"

    def test_f1_short_bearing_family_single_token_still_credits(self):
        # A short-PN family (SKF 6205 vs 6205-2RS): base 6205 < 6 chars so the
        # prefix branch can't fire, but the found PN carries the request's only
        # ≥3-char spec token → family credit stands (no bearing regression).
        c = _zoro(found_part_number="6205",
                  source_url="https://www.nationalprecision.com/brands/skf")
        assert pn_evidence_for(c, "6205-2RS") == "compatible"


# ---------------------------------------------------------------------------
# F2 — PN-from-URL extraction assist (MATCHING_CLEANUP)
# ---------------------------------------------------------------------------

_GALT_URL = ("https://vfds.com/galt-electric-gpt-motor-gpt15006447tk-150hp-"
             "1200rpm-3-phase-460v-60hz-445-7t")
_TRITON_PN = "HHI150-12-447T"


def _galt(**over) -> dict:
    """Galt Electric (Crown Triton live run): cheapest priced candidate, floored
    because the extractor missed the PN that sits in the URL slug."""
    c = {
        "vendor_name": "Galt Electric",
        "source_url": _GALT_URL,
        "found_part_number": None,
        "pn_match_status": None,
        "base_price": 7941.46,
        "price_tbd": False,
        "suitability_score": 1.0,
        "merchant_type": "Quote Request",
        "rejection_reason": "suitability_below_floor",
    }
    c.update(over)
    return c


class TestPnFromUrl:
    def test_galt_slug_extracts_pn(self):
        assert rb.extract_pn_from_url(_GALT_URL, _TRITON_PN) == "GPT15006447TK"

    def test_marketing_slug_extracts_nothing(self):
        # No spec-token corroboration → no extraction, regardless of shape.
        url = "https://vendor.example.com/industrial-electric-motors-best-prices-2026"
        assert rb.extract_pn_from_url(url, _TRITON_PN) is None

    def test_corroborated_slug_without_pn_shaped_token_extracts_nothing(self):
        # Spec tokens present but no PN-shaped token (unit tokens excluded).
        url = "https://vendor.example.com/motors/150hp-447t-1200rpm-sale"
        assert rb.extract_pn_from_url(url, _TRITON_PN) is None

    def test_query_string_never_corroborates(self):
        url = "https://vendor.example.com/p/abc1234567?q=HHI150-12-447T"
        assert rb.extract_pn_from_url(url, _TRITON_PN) is None

    def test_galt_bands_b_with_url_provenance_and_floor_cleared(self):
        # The full F2 path: assist → classification → F1 guard → rescope.
        result = {"tier_3": {"results": [_galt()]}}
        apply_ranking_bands(result, _TRITON_PN)
        c = result["tier_3"]["results"][0]
        assert c["found_part_number"] == "GPT15006447TK"
        assert c["pn_source"] == "url"
        assert c["band"] == BAND_B
        assert c.get("rejection_reason") is None
        assert c.get("band_note") == "floor_cleared_pn_evidence"

    def test_url_exact_pn_is_capped_at_band_b_never_a(self):
        # A slug echoing the requested PN exactly is part-referencing-URL-grade
        # evidence (spec §3 → Band B); Band A requires a listing-shown PN.
        c = _zoro(found_part_number=None, pn_match_status=None,
                  source_url="https://sealshop.example.com/gusher-8400428c238cbc-seal")
        result = {"tier_2": {"results": [c]}}
        apply_ranking_bands(result, _GUSHER_PN)
        assert c["pn_source"] == "url"
        assert c["found_part_number"] == "8400428C238CBC"
        assert c["band"] == BAND_B

    def test_url_pn_failing_family_guard_credits_nothing(self):
        # A URL PN goes through the SAME F1 guard as extractor PNs: a PN-shaped
        # token in a slug whose spec tokens belong to a DIFFERENT request part
        # earns no credit (the searched PN here shares nothing with the slug).
        c = _galt(source_url="https://vendor.example.com/pumps/xyz9876543-25gpm-2in")
        result = {"tier_3": {"results": [c]}}
        apply_ranking_bands(result, _TRITON_PN)
        assert c.get("pn_source") is None            # extraction never fired
        assert c["found_part_number"] is None
        assert c["rejection_reason"] == "suitability_below_floor"  # floor stands

    def test_assist_never_overwrites_extractor_pn_or_touches_mocks(self):
        keep = _zoro()                                # extractor PN present
        mock = _mock_seed(source_url=_GALT_URL)       # mock: never assisted
        result = {"tier_2": {"results": [keep]}, "tier_3": {"results": [mock]}}
        apply_ranking_bands(result, _TRITON_PN)
        assert keep["found_part_number"] == "84004-28SP"
        assert keep.get("pn_source") is None
        assert mock.get("pn_source") is None
        assert mock["found_part_number"] is None

    def test_no_match_candidates_not_assisted(self):
        c = _galt(pn_match_status="no_match")
        result = {"tier_3": {"results": [c]}}
        apply_ranking_bands(result, _TRITON_PN)
        assert c.get("pn_source") is None
        assert c["found_part_number"] is None


# ---------------------------------------------------------------------------
# F3 — post-band same-registrable-domain dedup (MATCHING_CLEANUP)
# ---------------------------------------------------------------------------

class TestDomainDedup:
    def test_zoro_revival_mechanism_collapses_richest_wins(self):
        """The live seal-run mechanism: BOTH same-vendor copies floored (so the
        pre-band cross-tier dedup claims no slot for either), then rescope_floor
        revives both on PN evidence → two Band-B Zoro cards. The post-band pass
        must keep exactly one — the priced (richest) copy."""
        priced = _zoro(source_url="https://www.zoro.com/springer-parts-seal/i/G406012291",
                       base_price=114.99, price_tbd=False,
                       rejection_reason="suitability_below_floor")
        bare = _zoro(source_url="https://zoro.com/p/84004-28sp",
                     rejection_reason="suitability_below_floor")
        result = {"tier_2": {"results": [priced]}, "tier_3": {"results": [bare]}}
        apply_ranking_bands(result, _GUSHER_PN)
        assert priced.get("rejection_reason") is None          # revived, kept
        assert bare["rejection_reason"] == "duplicate_vendor_domain"
        from utils.procurement_agent.ranking_bands import banded_findings
        zoros = [c for c, _, _ in banded_findings(result)
                 if c["vendor_name"] == "Zoro"]
        assert len(zoros) == 1 and zoros[0]["base_price"] == 114.99

    def test_subdomain_variants_collapse_pn_evidence_breaks_price_tie(self):
        # The live Global Industrial case: fresh www. listing (priced, PN) +
        # seeded static. PDF edge (priced, no PN) → one card, fresh wins.
        fresh = _zoro(vendor_name="Global Industrial",
                      found_part_number="ECP844156TR-5",
                      pn_match_status="partial_match",
                      source_url="https://www.globalindustrial.com/p/severe-duty-"
                                 "motor-ecp844156tr-5-3-ph-150-hp-1190-rpm-447t-frame",
                      base_price=36175.0, price_tbd=False, suitability_score=50.0)
        seeded = _zoro(vendor_name="Global Industrial",
                       found_part_number=None, pn_match_status="not_visible",
                       source_url="https://static.globalindustrial.com/products/"
                                  "pdf/55294/B3085296_BROCHURE-2.pdf",
                       base_price=22051.72, price_tbd=False,
                       suitability_score=35.0, seeded_from_cache=True)
        result = {"tier_2": {"results": [fresh, seeded]}}
        apply_ranking_bands(result, "HHI150-12-447T")
        assert fresh.get("rejection_reason") is None
        assert seeded["rejection_reason"] == "duplicate_vendor_domain"

    def test_non_marketplace_subdomain_variants_collapse(self):
        # catalog.jamiesonequipment.com vs www.jamiesonequipment.com → one card
        # (names need not match on a non-marketplace domain).
        a = _zoro(vendor_name="Jamieson Equipment Company",
                  found_part_number=None, pn_match_status="not_visible",
                  source_url="https://www.jamiesonequipment.com",
                  suitability_score=40.0)
        b = _zoro(vendor_name="Jamieson Catalog",
                  found_part_number=None, pn_match_status="not_visible",
                  source_url="https://catalog.jamiesonequipment.com",
                  suitability_score=35.0)
        result = {"tier_3": {"results": [a, b]}}
        apply_ranking_bands(result, "HHI150-12-447T")
        rejected = [c for c in (a, b)
                    if c.get("rejection_reason") == "duplicate_vendor_domain"]
        assert len(rejected) == 1

    def test_marketplace_different_vendor_names_never_collapse(self):
        # False-collapse guard: two different sellers surfaced via one
        # registry-classified marketplace domain must BOTH survive.
        a = _zoro(vendor_name="Alpha Seals",
                  source_url="https://www.zoro.com/alpha-seals-84004-28sp/i/1")
        b = _zoro(vendor_name="Beta Bearing Supply",
                  source_url="https://www.zoro.com/beta-bearing-84004-28sp/i/2")
        result = {"tier_2": {"results": [a, b]}}
        apply_ranking_bands(result, _GUSHER_PN)
        assert a.get("rejection_reason") is None
        assert b.get("rejection_reason") is None

    def test_different_registrable_domains_never_collapse(self):
        # springerparts.com vs catalog.springerpumps.com: vendor-IDENTITY work
        # (TECH_DEBT.md), out of scope for domain normalization.
        a = _zoro(vendor_name="Springer Parts / Springer Pumps, LLC",
                  source_url="https://www.springerparts.com")
        b = _zoro(vendor_name="Springer Pumps",
                  source_url="https://catalog.springerpumps.com/viewitems/sp-gusher")
        result = {"tier_3": {"results": [a, b]}}
        apply_ranking_bands(result, _GUSHER_PN)
        assert a.get("rejection_reason") is None
        assert b.get("rejection_reason") is None

    def test_onboarded_candidate_never_marked_duplicate(self):
        onboarded = _dxp()                                   # dxpe.com, registry
        discovered = _zoro(vendor_name="DXP Store",
                           source_url="https://shop.dxpe.com/p/84004-28sp",
                           base_price=99.0, price_tbd=False)
        result = {"tier_1": {"results": [onboarded]},
                  "tier_2": {"results": [discovered]}}
        apply_ranking_bands(result, _GUSHER_PN)
        assert onboarded.get("rejection_reason") is None     # richer or not: kept
        assert discovered["rejection_reason"] == "duplicate_vendor_domain"

    def test_mocks_urlless_and_prerejected_do_not_participate(self):
        mock = _mock_seed()
        urlless = _zoro(source_url=None, found_part_number=None,
                        pn_match_status="not_visible")
        rejected = _zoro(rejection_reason="pn_mismatch",
                         source_url="https://www.zoro.com/x/i/1")
        active = _zoro(source_url="https://www.zoro.com/y/i/2")
        result = {"tier_2": {"results": [rejected, active]},
                  "tier_3": {"results": [mock, urlless]}}
        apply_ranking_bands(result, _GUSHER_PN)
        assert rejected["rejection_reason"] == "pn_mismatch"  # first-set wins
        assert active.get("rejection_reason") is None         # no active dup
        assert mock.get("rejection_reason") is None
        assert urlless.get("rejection_reason") is None


# ---------------------------------------------------------------------------
# Band assignment (spec §3)
# ---------------------------------------------------------------------------

class TestAssignBand:
    def test_sealit_exact_pn_is_band_a(self):
        assert assign_band(_sealit(), _GUSHER_PN) == BAND_A

    def test_us_seal_canonical_pn_is_band_a(self):
        # THE defect case: exact-PN-family finding must be Band A, not floored.
        assert assign_band(_us_seal(), _GUSHER_PN) == BAND_A

    def test_zoro_aftermarket_pn_is_band_b(self):
        assert assign_band(_zoro(), _GUSHER_PN) == BAND_B

    def test_mock_seed_is_band_c_despite_fabricated_scores(self):
        assert assign_band(_mock_seed(), _GUSHER_PN) == BAND_C

    def test_capability_pivot_is_band_c(self):
        assert assign_band(_pivot(), _GUSHER_PN) == BAND_C

    def test_onboarded_class_match_is_band_c(self):
        # DXP: onboarded, carries SEAL class, no part evidence → outreach, not answer.
        assert assign_band(_dxp(), _GUSHER_PN) == BAND_C

    def test_confirmation_record_promotes_to_band_a(self):
        # §3 band mobility: an onboarded Band-C supplier that CONFIRMS is Band A.
        assert assign_band(_dxp(quote_confirmed=True), _GUSHER_PN) == BAND_A
        assert assign_band(_dxp(supplier_confirmed=True), _GUSHER_PN) == BAND_A

    def test_exact_pn_without_url_is_band_b(self):
        # PN evidence without a verifiable listing: short of CONFIRMED.
        assert assign_band(_sealit(source_url=None), _GUSHER_PN) == BAND_B

    def test_discovered_listing_without_pn_is_band_b(self):
        c = _zoro(found_part_number=None, pn_match_status="not_visible")
        assert assign_band(c, _GUSHER_PN) == BAND_B

    def test_urlless_unseeded_candidate_is_band_c(self):
        c = _zoro(found_part_number=None, pn_match_status="not_visible", source_url=None)
        assert assign_band(c, _GUSHER_PN) == BAND_C

    def test_no_match_pn_listing_is_band_b_not_a(self):
        # A no_match verdict kills the PN evidence; the real listing URL keeps it B.
        c = _sealit(pn_match_status="no_match")
        assert assign_band(c, _GUSHER_PN) == BAND_B


# ---------------------------------------------------------------------------
# Evidence quality (spec §4) + confidence
# ---------------------------------------------------------------------------

class TestEvidenceQuality:
    def test_pn_quality_ordering(self):
        exact = evidence_quality(_sealit(), _GUSHER_PN)
        canonical = evidence_quality(_us_seal(), _GUSHER_PN)
        compatible = evidence_quality(_zoro(), _GUSHER_PN)
        assert exact > canonical > compatible > 0

    def test_price_presence_counts(self):
        priced = evidence_quality(_sealit(), _GUSHER_PN)
        unpriced = evidence_quality(_sealit(base_price=0.0, price_tbd=True), _GUSHER_PN)
        assert priced > unpriced

    def test_confirmation_beats_listed_price(self):
        quoted = evidence_quality(_dxp(quote_confirmed=True), _GUSHER_PN)
        assert quoted > evidence_quality(_dxp(), _GUSHER_PN)

    def test_crude_suitability_is_band_b_ordering_input_only(self):
        # Band B: crude score participates (capped)...
        low = evidence_quality(_zoro(suitability_score=0.0), _GUSHER_PN)
        high = evidence_quality(_zoro(suitability_score=100.0), _GUSHER_PN)
        assert 0 < high - low <= 10.0 + 1e-9
        # ...Band A: it does not (evidence, not keyword echo, ranks answers).
        a_low = evidence_quality(_us_seal(suitability_score=0.0), _GUSHER_PN)
        a_high = evidence_quality(_us_seal(suitability_score=100.0), _GUSHER_PN)
        assert a_low == a_high

    def test_fabricated_mock_scores_earn_nothing(self):
        # is_mock seed with fabricated 88/75: no PN, no URL, no price → minimal EQ.
        assert evidence_quality(_mock_seed(), _GUSHER_PN) == 0.0

    def test_confidence_zero_iff_band_c(self):
        assert confidence_from_evidence(_mock_seed(), _GUSHER_PN) == 0.0
        assert confidence_from_evidence(_dxp(), _GUSHER_PN) == 0.0
        assert confidence_from_evidence(_pivot(), _GUSHER_PN) == 0.0
        assert confidence_from_evidence(_sealit(), _GUSHER_PN) > 0.0
        assert confidence_from_evidence(_us_seal(), _GUSHER_PN) > 0.0
        assert confidence_from_evidence(_zoro(), _GUSHER_PN) > 0.0


# ---------------------------------------------------------------------------
# Ordering — band absolute, onboarded-first within band, EQ, TCA stability
# ---------------------------------------------------------------------------

def _rand_candidate(rng: random.Random, i: int) -> dict:
    """Random candidate spanning all band shapes with randomized (even absurd)
    scores — the property is that scores can NEVER cross a band boundary."""
    shape = rng.choice(["exact", "canonical", "compatible", "listing", "mock",
                        "registry", "pivot", "urlless", "confirmed"])
    c = {
        "vendor_name": f"V{i}-{shape}",
        "suitability_score": rng.uniform(0, 100),
        "confidence_score": rng.uniform(0, 100),
        "base_price": rng.choice([0.0, rng.uniform(1, 500)]),
        "price_tbd": rng.random() < 0.5,
        "source_url": f"https://v{i}.example.com/p",
        "found_part_number": None,
        "reliability_score": rng.uniform(50, 100),
    }
    if shape == "exact":
        c["found_part_number"] = _GUSHER_PN
    elif shape == "canonical":
        c["found_part_number"] = "84004-28"
    elif shape == "compatible":
        c["found_part_number"] = "84004-28SP"
    elif shape == "listing":
        c["pn_match_status"] = "partial_match"
    elif shape == "mock":
        c.update(is_mock=True, source_url=None)
    elif shape == "registry":
        c.update(is_registry_backed=True, merchant_type="Arkim Network")
    elif shape == "pivot":
        c["search_type"] = "capability_pivot"
    elif shape == "urlless":
        c["source_url"] = None
    elif shape == "confirmed":
        c["quote_confirmed"] = True
    if rng.random() < 0.3 and shape not in ("registry",):
        c["merchant_type"] = "Arkim Network"  # random onboarded flag
    return c


class TestBandOrderingProperty:
    def test_no_lower_band_ever_ranks_above_higher_band(self):
        """Spec §9 criterion 7 (property test): under randomized scores, ordering
        is monotone in band — no C above B, no B above A, ever."""
        for seed in range(50):
            rng = random.Random(seed)
            cands = [_rand_candidate(rng, i) for i in range(rng.randint(2, 30))]
            for c in cands:
                annotate_candidate(c, _GUSHER_PN)
            ordered = order_banded(cands)
            ranks = [{"A": 0, "B": 1, "C": 2}[c["band"]] for c in ordered]
            assert ranks == sorted(ranks), (
                f"seed {seed}: band order violated: "
                f"{[(c['vendor_name'], c['band']) for c in ordered]}"
            )

    def test_onboarded_first_within_band_only(self):
        for seed in range(50):
            rng = random.Random(1000 + seed)
            cands = [_rand_candidate(rng, i) for i in range(rng.randint(2, 30))]
            for c in cands:
                annotate_candidate(c, _GUSHER_PN)
            ordered = order_banded(cands)
            # Within each band, all onboarded candidates precede non-onboarded.
            for band in (BAND_A, BAND_B, BAND_C):
                flags = [is_onboarded(c) for c in ordered if c["band"] == band]
                assert flags == sorted(flags, reverse=True), f"seed {seed} band {band}"

    def test_onboarded_never_jumps_band(self):
        # An onboarded Band-C candidate must NOT outrank a non-onboarded Band-A one.
        a = annotate_candidate(_sealit(), _GUSHER_PN)
        c = annotate_candidate(_dxp(), _GUSHER_PN)
        ordered = order_banded([c, a])
        assert [x["vendor_name"] for x in ordered] == ["Sealit123", "DXP Enterprises"]

    def test_tca_tiebreak_by_stability(self):
        # Two same-band same-evidence candidates keep their input (TCA) order.
        c1 = annotate_candidate(_zoro(vendor_name="Zoro-First"), _GUSHER_PN)
        c2 = annotate_candidate(_zoro(vendor_name="Zoro-Second"), _GUSHER_PN)
        assert banded_sort_key(c1) == banded_sort_key(c2)
        ordered = order_banded([c1, c2])
        assert [x["vendor_name"] for x in ordered] == ["Zoro-First", "Zoro-Second"]

    def test_apply_ranking_bands_annotates_and_orders_each_tier(self):
        result = {
            "tier_1": {"results": [], "count": 0, "status": "ok"},
            "tier_2": {"results": [_zoro(), _sealit()], "count": 2, "status": "ok"},
            "tier_3": {"results": [_mock_seed(), _us_seal()], "count": 2, "status": "ok"},
        }
        apply_ranking_bands(result, _GUSHER_PN)
        t2 = result["tier_2"]["results"]
        t3 = result["tier_3"]["results"]
        assert all(c.get("banded") for c in t2 + t3)
        # Band A (Sealit) before Band B (Zoro); Band A (US Seal) before C (mock).
        assert [c["vendor_name"] for c in t2] == ["Sealit123", "Zoro"]
        assert [c["vendor_name"] for c in t3] == ["US Seal Manufacturing", "Phoenix Pumps"]
        # Membership never changes (annotate-don't-remove).
        assert result["tier_2"]["count"] == 2 and len(t2) == 2
        assert result["tier_3"]["count"] == 2 and len(t3) == 2


# ---------------------------------------------------------------------------
# Floor re-scoping (spec §5) — T2
# ---------------------------------------------------------------------------

class TestFloorRescope:
    """The Gusher floor-inversion regressions: the floor rejected the vendors who
    FOUND the part (US Seal 12.6, Zoro 10.5) while hardcoded-88 mocks passed."""

    def _floored(self, factory, **over):
        c = factory(rejection_reason="suitability_below_floor", **over)
        annotate_candidate(c, _GUSHER_PN)
        rescope_floor(c, _GUSHER_PN)
        return c

    def test_us_seal_band_a_floor_rejection_cleared(self):
        c = self._floored(_us_seal)
        assert c["rejection_reason"] is None
        assert c["band_note"] == "floor_cleared_band_a"

    def test_sealit_band_a_never_floor_rejected_even_at_zero(self):
        c = self._floored(_sealit, suitability_score=0.0)
        assert c["rejection_reason"] is None

    def test_zoro_band_b_compatible_pn_unfloored(self):
        c = self._floored(_zoro)  # suitability 10.5, compatible PN found
        assert c["rejection_reason"] is None
        assert c["band_note"] == "floor_cleared_pn_evidence"

    def test_band_b_listing_without_pn_evidence_keeps_floor(self):
        # The floor's legitimate job: junk listings with no PN evidence stay out.
        c = self._floored(_zoro, found_part_number=None, pn_match_status="not_visible")
        assert c["band"] == BAND_B
        assert c["rejection_reason"] == "suitability_below_floor"

    def test_band_c_floor_not_applicable(self):
        c = self._floored(_pivot)
        assert c["rejection_reason"] is None
        assert c["band_note"] == "floor_not_applicable_band_c"

    def test_non_floor_rejections_never_cleared(self):
        for reason in ("pn_mismatch", "duplicate_in_higher_tier"):
            c = _us_seal(rejection_reason=reason)
            annotate_candidate(c, _GUSHER_PN)
            rescope_floor(c, _GUSHER_PN)
            assert c["rejection_reason"] == reason


# ---------------------------------------------------------------------------
# Band-C count cap (spec §5) — T2
# ---------------------------------------------------------------------------

class TestBandCCap:
    def test_cap_keeps_top_n_by_evidence_and_flags_rest(self):
        seeds = [annotate_candidate(_mock_seed(f"Seed{i}"), _GUSHER_PN)
                 for i in range(BAND_C_CAP + 3)]
        # Give a couple of them URL evidence so ordering by EQ is observable.
        pivots = [annotate_candidate(_pivot(vendor_name=f"Pivot{i}"), _GUSHER_PN)
                  for i in range(2)]
        cands = seeds + pivots
        cap_band_c(cands)
        kept = [c for c in cands if c.get("band_c_capped") is False]
        capped = [c for c in cands if c.get("band_c_capped") is True]
        assert len(kept) == BAND_C_CAP
        assert len(capped) == len(cands) - BAND_C_CAP
        # URL-bearing pivots have higher EQ than URL-less seeds — they keep slots.
        assert all(p in kept for p in pivots)

    def test_onboarded_never_capped_and_never_counts_against_cap(self):
        dxp = annotate_candidate(_dxp(), _GUSHER_PN)
        seeds = [annotate_candidate(_mock_seed(f"Seed{i}"), _GUSHER_PN)
                 for i in range(BAND_C_CAP)]
        cands = [dxp] + seeds
        cap_band_c(cands)
        assert "band_c_capped" not in dxp  # onboarded: not in cap competition at all
        assert all(c.get("band_c_capped") is False for c in seeds)  # full cap available

    def test_band_a_b_untouched_by_cap(self):
        a = annotate_candidate(_sealit(), _GUSHER_PN)
        b = annotate_candidate(_zoro(), _GUSHER_PN)
        cap_band_c([a, b])
        assert "band_c_capped" not in a and "band_c_capped" not in b

    def test_apply_ranking_bands_caps_across_tiers(self):
        result = {
            "tier_1": {"results": [_dxp()], "count": 1, "status": "ok"},
            "tier_2": {"results": [], "count": 0, "status": "ok"},
            "tier_3": {"results": [_mock_seed(f"S{i}") for i in range(BAND_C_CAP + 2)],
                       "count": BAND_C_CAP + 2, "status": "ok"},
        }
        apply_ranking_bands(result, _GUSHER_PN)
        t3 = result["tier_3"]["results"]
        assert sum(1 for c in t3 if c.get("band_c_capped") is True) == 2
        assert "band_c_capped" not in result["tier_1"]["results"][0]


# ---------------------------------------------------------------------------
# Mock descoring + honest confidence (spec §4) — T3
# ---------------------------------------------------------------------------

class TestMockDescoring:
    def test_mock_carries_no_suitability_or_confidence_number(self):
        c = annotate_candidate(_mock_seed(), _GUSHER_PN)
        assert c["suitability_score"] is None
        assert c["confidence_score"] is None
        assert c["band"] == BAND_C
        assert c["provenance"] == "Authorized distributor per brand intelligence"

    def test_mock_with_spurious_confirmation_stays_band_c(self):
        # A fabricated candidate can never be a finding, whatever flags it carries.
        c = annotate_candidate(_mock_seed(quote_confirmed=True), _GUSHER_PN)
        assert c["band"] == BAND_C

    def test_confidence_becomes_evidence_derived_for_real_candidates(self):
        us = annotate_candidate(_us_seal(confidence_score=46.3), _GUSHER_PN)
        assert us["confidence_score"] == confidence_from_evidence(us, _GUSHER_PN)
        assert us["confidence_score"] > 0
        dxp = annotate_candidate(_dxp(confidence_score=90.0), _GUSHER_PN)
        assert dxp["confidence_score"] == 0.0  # class-match only: nothing verified

    def test_provenance_shapes(self):
        assert annotate_candidate(_pivot(), _GUSHER_PN)["provenance"] == "Capability discovery"
        assert annotate_candidate(_dxp(), _GUSHER_PN)["provenance"] == "Onboarded supplier — class match"
        assert annotate_candidate(_zoro(), _GUSHER_PN)["provenance"] == "Discovered listing"


# ---------------------------------------------------------------------------
# Findings vs outreach targets (spec §7) — T3
# ---------------------------------------------------------------------------

def _banded_gusher_result() -> dict:
    """A banded result dict shaped like the live Gusher run: real findings in
    tiers 2/3, DXP class-match in tier 1, five mock seeds in tier 3."""
    from utils.procurement_agent.ranking_bands import apply_ranking_bands
    result = {
        "tier_1": {"results": [_dxp()], "count": 1, "status": "ok"},
        "tier_2": {"results": [_zoro()], "count": 1, "status": "ok"},
        "tier_3": {"results": [_mock_seed(f"Seed{i}") for i in range(5)]
                              + [_sealit(), _us_seal()],
                   "count": 7, "status": "ok"},
        "filters_applied": ["suitability_floor:30%", "ranking_bands:v1"],
        "warranty_banner": None,
        "tier3_capability_pivot": False,
    }
    return apply_ranking_bands(result, _GUSHER_PN)


class TestFindingsAndOutreach:
    def test_findings_are_band_a_then_b_across_tiers(self):
        from utils.procurement_agent.ranking_bands import banded_findings
        entries = banded_findings(_banded_gusher_result())
        names = [c["vendor_name"] for c, _, _ in entries]
        bands = [c["band"] for c, _, _ in entries]
        assert names == ["Sealit123", "US Seal Manufacturing", "Zoro"]
        assert bands == [BAND_A, BAND_A, BAND_B]

    def test_no_mock_ever_a_finding(self):
        from utils.procurement_agent.ranking_bands import banded_findings
        entries = banded_findings(_banded_gusher_result())
        assert not any(c.get("is_mock") for c, _, _ in entries)

    def test_rejected_candidates_are_not_findings(self):
        from utils.procurement_agent.ranking_bands import banded_findings
        result = _banded_gusher_result()
        for c, _, _ in banded_findings(result):
            if c["vendor_name"] == "Zoro":
                c["rejection_reason"] = "pn_mismatch"
        assert "Zoro" not in [c["vendor_name"] for c, _, _ in banded_findings(result)]

    def test_outreach_targets_onboarded_first_capped_excluded(self):
        from utils.procurement_agent.ranking_bands import outreach_targets
        result = _banded_gusher_result()
        targets = outreach_targets(result)
        assert targets[0]["vendor_name"] == "DXP Enterprises"  # named first, yours
        assert all(t["band"] == BAND_C for t in targets)
        # 5 seeds fit within the cap of 5 (DXP is onboarded, doesn't count).
        assert len(targets) == 6
        # Push over the cap: a 6th seed must be excluded from the block.
        result["tier_3"]["results"].append(_mock_seed("Seed5"))
        from utils.procurement_agent.ranking_bands import apply_ranking_bands
        apply_ranking_bands(result, _GUSHER_PN)
        targets2 = outreach_targets(result)
        assert len(targets2) == 6  # DXP + capped-at-5 seeds
        assert sum(1 for c in result["tier_3"]["results"]
                   if c.get("band_c_capped") is True) == 1


# ---------------------------------------------------------------------------
# Band promotion (spec §3 mobility) — T4
# ---------------------------------------------------------------------------

class TestBandPromotion:
    def test_promote_confirmed_rebands_to_a(self):
        from utils.procurement_agent.ranking_bands import promote_confirmed
        dxp = annotate_candidate(_dxp(), _GUSHER_PN)
        assert dxp["band"] == BAND_C
        promote_confirmed(dxp, _GUSHER_PN)
        assert dxp["band"] == BAND_A
        assert dxp["band_note"] == "promoted_confirmed_quote"
        assert dxp["confidence_score"] > 0  # confirmed: no longer nothing-verified

    def test_promoted_onboarded_supplier_tops_band_a(self):
        from utils.procurement_agent.ranking_bands import promote_confirmed
        sealit = annotate_candidate(_sealit(), _GUSHER_PN)
        dxp = promote_confirmed(annotate_candidate(_dxp(), _GUSHER_PN), _GUSHER_PN)
        ordered = order_banded([sealit, dxp])
        assert [c["vendor_name"] for c in ordered] == ["DXP Enterprises", "Sealit123"]

    def test_mock_is_never_promotable(self):
        from utils.procurement_agent.ranking_bands import promote_confirmed
        seed = annotate_candidate(_mock_seed(), _GUSHER_PN)
        promote_confirmed(seed, _GUSHER_PN)
        assert seed["band"] == BAND_C

    def _dxp_quote_index(self, price=189.0):
        return {
            "by_thread": {},
            "domain_threads": {},
            "by_domain": {"dxpe.com": {
                "status": "confirmed", "supplier_domain": "dxpe.com",
                "thread_id": "t-123", "confidence": 0.9,
                "payload": {"unit_price": price, "lead_time": "3 days", "currency": "USD"},
            }},
        }

    def test_simulated_dxp_confirmation_promotes_to_top_of_findings(self):
        """Acceptance criterion 5: DXP appears in the outreach block (Band C);
        a simulated DXP confirmation (structured quote with part+price) promotes
        it to the TOP of Band A — visibly the onboarding benefit working."""
        from api_server import _transform_sourcing_results
        # WITHOUT confirmation: Sealit123 is #1; DXP leads the outreach block.
        out = _transform_sourcing_results(_banded_gusher_result())
        assert out["findings"][0]["vendorName"] == "Sealit123"
        assert out["outreachTargets"]["suppliers"][0]["vendorName"] == "DXP Enterprises"
        # WITH a confirmed DXP quote: DXP tops the findings in Band A position,
        # carries the quoted price, and leaves the outreach block.
        out2 = _transform_sourcing_results(_banded_gusher_result(),
                                           quote_index=self._dxp_quote_index())
        assert out2["findings"][0]["vendorName"] == "DXP Enterprises"
        assert out2["findings"][0]["band"] == "A"
        assert out2["findings"][0]["quoteConfirmed"] is True
        assert out2["findings"][0]["price"] == 189.0
        assert out2["findings"][1]["vendorName"] == "Sealit123"
        assert "DXP Enterprises" not in [
            s["vendorName"] for s in out2["outreachTargets"]["suppliers"]]

    def test_confirmed_quote_without_price_does_not_promote(self):
        from api_server import _transform_sourcing_results
        idx = self._dxp_quote_index()
        idx["by_domain"]["dxpe.com"]["payload"] = {"lead_time": "3 days"}  # no price
        out = _transform_sourcing_results(_banded_gusher_result(), quote_index=idx)
        assert "DXP Enterprises" not in [f["vendorName"] for f in out["findings"]]
        assert out["outreachTargets"]["suppliers"][0]["vendorName"] == "DXP Enterprises"


# ---------------------------------------------------------------------------
# API response shape (flag-on findings/outreachTargets; flag-off untouched) — T3
# ---------------------------------------------------------------------------

class TestApiResponseShape:
    def _transform(self, raw):
        from api_server import _transform_sourcing_results
        return _transform_sourcing_results(raw)

    def test_banded_result_gains_findings_and_outreach(self):
        out = self._transform(_banded_gusher_result())
        assert "findings" in out and "outreachTargets" in out
        names = [f["vendorName"] for f in out["findings"]]
        assert names == ["Sealit123", "US Seal Manufacturing", "Zoro"]
        assert [f["band"] for f in out["findings"]] == ["A", "A", "B"]
        # CONTRACT (spec §4, test-enforced): is_mock in findings = violation.
        assert not any(f["isMock"] for f in out["findings"])

    def test_outreach_block_no_numbers_onboarded_first(self):
        out = self._transform(_banded_gusher_result())
        suppliers = out["outreachTargets"]["suppliers"]
        assert suppliers[0] == {
            "vendorName": "DXP Enterprises", "onboarded": True,
            "provenance": "Onboarded supplier — class match",
        }
        assert out["outreachTargets"]["requestedCount"] == len(suppliers) == 6
        for s in suppliers:
            # Provenance strings only — never a fabricated number.
            assert set(s.keys()) == {"vendorName", "onboarded", "provenance"}

    def test_unbanded_result_response_shape_unchanged(self):
        raw = _banded_gusher_result()
        raw["filters_applied"] = ["suitability_floor:30%"]  # no ranking_bands marker
        out = self._transform(raw)
        assert "findings" not in out
        assert "outreachTargets" not in out

    def test_pipeline_contract_no_mock_in_findings(self, monkeypatch):
        """End-to-end contract: flag-on agent.run over seeds + real findings →
        transformed response findings contain NO is_mock candidate and every mock
        carries no suitability/confidence number."""
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        tier3 = [_mock_seed(f"Seed{i}") for i in range(5)] + [_sealit(), _us_seal()]
        raw = _run_agent_with(tier3, monkeypatch)
        out = self._transform(raw)
        assert not any(f["isMock"] for f in out["findings"])
        for c in raw["tier_3"]["results"]:
            if c.get("is_mock"):
                assert c["suitability_score"] is None
                assert c["confidence_score"] is None


# ---------------------------------------------------------------------------
# Cache policy (spec §6) — T5: identity persists, vendor verdicts are TTL'd hints
# ---------------------------------------------------------------------------

_KP_PART_KEY_ARGS = ("Gusher Pumps", "84004-28-C238CBC")


@pytest.fixture
def kp(monkeypatch, tmp_path):
    """utils.known_parts pointed at a tmp fixture store (the live
    utils/known_parts.json is DATA and is never touched by tests)."""
    from utils import known_parts
    monkeypatch.setattr(known_parts, "_DB_PATH", str(tmp_path / "kp.json"))
    return known_parts


def _banded_a_candidate(**over):
    c = _sealit(band="A", tier=2)
    c.update(over)
    return c


class TestCacheWriteGate:
    def test_flag_on_only_band_a_b_written(self, kp, monkeypatch):
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        pk = kp.canonical_part_key(*_KP_PART_KEY_ARGS)
        n = kp.upsert_edges(pk, [
            _banded_a_candidate(),                                   # A → written
            _zoro(band="B", tier=2),                                 # B → written
            _dxp(band="C", tier=1),                                  # C → never
            _mock_seed(band="C", tier=3),                            # mock → never
            _mock_seed("Sneaky", band="A", tier=3),                  # mock claims A → never
            {**_us_seal(), "tier": 3},                               # un-banded → never
        ])
        assert n == 2
        ids = {e["supplier_id"] for e in kp.get_edges(pk)}
        assert ids == {"sealit123.com", "zoro.com"}

    def test_flag_on_edges_carry_matcher_version(self, kp, monkeypatch):
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        pk = kp.canonical_part_key(*_KP_PART_KEY_ARGS)
        kp.upsert_edges(pk, [_banded_a_candidate()])
        edge = kp.get_edges(pk)[0]
        assert edge["matcher_version"] == rb.MATCHER_VERSION
        assert edge["edge_stale"] is False

    def test_flag_off_writes_are_legacy_byte_identical(self, kp, monkeypatch):
        monkeypatch.delenv("RANKING_BANDS_V1", raising=False)
        pk = kp.canonical_part_key(*_KP_PART_KEY_ARGS)
        n = kp.upsert_edges(pk, [
            {**_sealit(), "tier": 2},          # un-banded: legacy write path
            {**_mock_seed(), "tier": 3},       # legacy wrote mocks too (the defect)
        ])
        assert n == 2
        for e in kp.get_edges(pk):
            assert "matcher_version" not in e
            assert "edge_stale" not in e   # flag-off read shape unchanged


class TestEdgeStaleness:
    def _seed_fresh(self, kp, monkeypatch):
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        pk = kp.canonical_part_key(*_KP_PART_KEY_ARGS)
        kp.upsert_edges(pk, [_banded_a_candidate()])
        return pk

    def test_fresh_current_version_edge_not_stale(self, kp, monkeypatch):
        pk = self._seed_fresh(kp, monkeypatch)
        assert kp.get_edges(pk)[0]["edge_stale"] is False

    def test_legacy_versionless_edge_is_stale(self, kp, monkeypatch):
        # Written flag-OFF (no matcher_version) → read flag-ON as stale hint.
        monkeypatch.delenv("RANKING_BANDS_V1", raising=False)
        pk = kp.canonical_part_key(*_KP_PART_KEY_ARGS)
        kp.upsert_edges(pk, [{**_sealit(), "tier": 2}])
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        assert kp.get_edges(pk)[0]["edge_stale"] is True

    def test_matcher_version_bump_invalidates(self, kp, monkeypatch):
        pk = self._seed_fresh(kp, monkeypatch)
        monkeypatch.setattr(rb, "MATCHER_VERSION", rb.MATCHER_VERSION + 1)
        assert kp.get_edges(pk)[0]["edge_stale"] is True

    def test_edge_older_than_ttl_is_stale(self, kp, monkeypatch):
        from datetime import datetime, timedelta, timezone
        pk = self._seed_fresh(kp, monkeypatch)
        db = kp.all_entries()
        edge = db[pk]["edges"]["sealit123.com"]
        edge["last_seen"] = (datetime.now(timezone.utc)
                             - timedelta(days=kp.EDGE_TTL_DAYS + 1)).isoformat()
        kp._save(db)
        assert kp.get_edges(pk)[0]["edge_stale"] is True

    def test_ttl_env_override(self, kp, monkeypatch):
        from datetime import datetime, timedelta, timezone
        pk = self._seed_fresh(kp, monkeypatch)
        db = kp.all_entries()
        db[pk]["edges"]["sealit123.com"]["last_seen"] = (
            datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        kp._save(db)
        assert kp.get_edges(pk)[0]["edge_stale"] is False   # 3d < default 7d
        monkeypatch.setenv("KNOWN_PARTS_EDGE_TTL_DAYS", "2")
        assert kp.get_edges(pk)[0]["edge_stale"] is True    # 3d > 2d

    def test_identity_key_retained_when_edges_stale(self, kp, monkeypatch):
        # Staleness is annotation-at-read: the part-key identity entry and its
        # edges REMAIN in the store (hints), never deleted.
        pk = self._seed_fresh(kp, monkeypatch)
        monkeypatch.setattr(rb, "MATCHER_VERSION", rb.MATCHER_VERSION + 1)
        assert pk in kp.all_entries()
        assert len(kp.get_edges(pk)) == 1


class TestMigration:
    def test_marks_legacy_edges_stale_hint_idempotently(self, kp, monkeypatch):
        # Legacy store: flag-off writes (no matcher_version).
        monkeypatch.delenv("RANKING_BANDS_V1", raising=False)
        pk = kp.canonical_part_key(*_KP_PART_KEY_ARGS)
        kp.upsert_edges(pk, [{**_sealit(), "tier": 2}, {**_zoro(), "tier": 2}])
        assert kp.migrate_vendor_edges_stale_hint() == 2
        assert kp.migrate_vendor_edges_stale_hint() == 0  # idempotent
        db = kp.all_entries()
        assert pk in db  # identity retained
        assert all(e["stale_hint"] is True for e in db[pk]["edges"].values())
        # Under the flag the hinted edges read stale.
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        assert all(e["edge_stale"] for e in kp.get_edges(pk))

    def test_current_version_edges_not_marked(self, kp, monkeypatch):
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        pk = kp.canonical_part_key(*_KP_PART_KEY_ARGS)
        kp.upsert_edges(pk, [_banded_a_candidate()])
        assert kp.migrate_vendor_edges_stale_hint() == 0
        assert kp.get_edges(pk)[0]["edge_stale"] is False


class TestCacheFirstReadPolicy:
    """Spec §9 criterion 6 at the API seam, under the DESIGN CORRECTION (spec §6
    primary rule): the cache ACCELERATES discovery, it never replaces it. A
    repeat search never replays a frozen verdict — fresh discovery runs on
    EVERY flag-on run; stale/invalidated edges don't even seed; TTL-fresh
    current-version edges SEED the merge alongside today's discovery."""

    def _specs_json(self):
        return json.dumps({
            "manufacturer": "Gusher Pumps", "part_number": "84004-28-C238CBC",
            "detected_type": "mechanical seal", "model": "Type 21", "voltage": "N/A",
        })

    def _discovery_result(self):
        from utils.procurement_agent.tests.test_api_server import _empty_sourcing
        s = _empty_sourcing()
        s["tier_2"]["results"] = [{
            "vendor_name": "Fresh Discovery Vendor", "base_price": 61.0,
            "source_url": "https://freshvendor.com/x", "suitability_score": 75,
            "match_type": "Exact OEM", "found_part_number": "84004-28-C238CBC",
        }]
        s["filters_applied"] = ["ranking_bands:v1"]
        for c in s["tier_2"]["results"]:
            c.update(band="A", evidence_quality=70.0, banded=True)
        return s

    def test_stale_edges_do_not_short_circuit_discovery(self, api, kp, monkeypatch):
        from utils.procurement_agent.tests.test_api_server import (
            _create_run, _set_run, _mock_sourcing_pipeline)
        # Seed a LEGACY (versionless → stale under flag) edge.
        monkeypatch.delenv("RANKING_BANDS_V1", raising=False)
        pk = kp.canonical_part_key(*_KP_PART_KEY_ARGS)
        kp.upsert_edges(pk, [{**_sealit(), "tier": 2}])
        # Flag ON: the stale edge must NOT be served; discovery runs.
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        rid = _create_run(api)
        _set_run(api, rid, asset_specs_json=self._specs_json())
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=self._discovery_result())
        assert api.post(f"/api/runs/{rid}/confirm-intake").status_code == 200
        detail = api.get(f"/api/runs/{rid}").json()
        vendors = [c["vendorName"] for c in detail["sourcing_results"]["tier2"]]
        assert "Fresh Discovery Vendor" in vendors   # discovery ran
        assert "Sealit123" not in vendors            # frozen verdict NOT replayed

    def test_fresh_edges_seed_the_discovery_merge(self, api, kp, monkeypatch):
        # DESIGN CORRECTION: TTL-fresh edges no longer short-circuit discovery —
        # discovery runs AND the cached vendor still surfaces (seeded into the
        # union), with the banded response shape over the merged pool.
        from utils.procurement_agent.tests.test_api_server import (
            _create_run, _set_run, _mock_sourcing_pipeline)
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        pk = kp.canonical_part_key(*_KP_PART_KEY_ARGS)
        kp.upsert_edges(pk, [_banded_a_candidate()])   # fresh, current version
        rid = _create_run(api)
        _set_run(api, rid, asset_specs_json=self._specs_json())
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=self._discovery_result())
        assert api.post(f"/api/runs/{rid}/confirm-intake").status_code == 200
        detail = api.get(f"/api/runs/{rid}").json()
        sr = detail["sourcing_results"]
        vendors = [c["vendorName"] for c in sr["tier2"]]
        assert "Fresh Discovery Vendor" in vendors       # discovery ALWAYS runs
        assert any("ealit123" in v for v in vendors)     # the seed still surfaces
        # The merged response is banded: findings/outreach shape present.
        assert "findings" in sr and "outreachTargets" in sr

    def test_version_bump_forces_rediscovery(self, api, kp, monkeypatch):
        from utils.procurement_agent.tests.test_api_server import (
            _create_run, _set_run, _mock_sourcing_pipeline)
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        pk = kp.canonical_part_key(*_KP_PART_KEY_ARGS)
        kp.upsert_edges(pk, [_banded_a_candidate()])
        monkeypatch.setattr(rb, "MATCHER_VERSION", rb.MATCHER_VERSION + 1)  # bump
        rid = _create_run(api)
        _set_run(api, rid, asset_specs_json=self._specs_json())
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=self._discovery_result())
        assert api.post(f"/api/runs/{rid}/confirm-intake").status_code == 200
        detail = api.get(f"/api/runs/{rid}").json()
        vendors = [c["vendorName"] for c in detail["sourcing_results"]["tier2"]]
        assert "Fresh Discovery Vendor" in vendors   # invalidated → rediscovered
        # ...and the version-invalidated edge does not even SEED the merge.
        assert not any("ealit123" in v for v in vendors)

    def test_flag_off_cache_behavior_unchanged(self, api, kp, monkeypatch):
        from utils.procurement_agent.tests.test_api_server import (
            _create_run, _set_run, _mock_sourcing_pipeline)
        monkeypatch.delenv("RANKING_BANDS_V1", raising=False)
        pk = kp.canonical_part_key(*_KP_PART_KEY_ARGS)
        kp.upsert_edges(pk, [{**_sealit(), "tier": 2}])
        rid = _create_run(api)
        _set_run(api, rid, asset_specs_json=self._specs_json())
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=self._discovery_result())
        assert api.post(f"/api/runs/{rid}/confirm-intake").status_code == 200
        detail = api.get(f"/api/runs/{rid}").json()
        sr = detail["sourcing_results"]
        vendors = [c["vendorName"] for c in sr["tier2"]]
        assert "Fresh Discovery Vendor" not in vendors   # legacy: cache always serves
        assert "findings" not in sr and "outreachTargets" not in sr


# ---------------------------------------------------------------------------
# T6 — ACCEPTANCE SUITE: spec §9 verbatim, Gusher as the acceptance case
# ---------------------------------------------------------------------------

def _seals_direct(**over) -> dict:
    """Seals-Direct: aftermarket PN found, real URL (Band B, criterion 3)."""
    c = {
        "vendor_name": "Seals-Direct",
        "source_url": "https://www.sealsdirect.com/gusher-84004-28",
        "found_part_number": "84004-28SP",
        "pn_match_status": "partial_match",
        "base_price": 0.0,
        "price_tbd": True,
        "suitability_score": 18.0,
        "merchant_type": "Quote Request",
    }
    c.update(over)
    return c


_FIVE_MOCKS = ("Phoenix Pumps", "Anderson Process", "OTC Industrial",
               "Great Lakes", "Wagner Process")


def _gusher_live_tiers() -> tuple[list, list, list]:
    """The live Gusher run's candidate shapes (run 37e6104d / run_full.json):
    DXP class-match in Tier 1; Zoro aftermarket in Tier 2; Sealit123 exact-PN
    $53.25, US Seal exact-PN (crude 12.6), Seals-Direct aftermarket and the five
    brand-intelligence mock seeds in Tier 3."""
    tier1 = [_dxp()]
    tier2 = [_zoro()]
    tier3 = [_mock_seed(n) for n in _FIVE_MOCKS] + [_sealit(), _us_seal(), _seals_direct()]
    return tier1, tier2, tier3


def _run_gusher_pipeline(monkeypatch, flag_on: bool = True) -> dict:
    """Drive the REAL SourcingAgent.run (floor, dedup, tier-3 ranking, band pass)
    over the Gusher fixture, tiers mocked at the tier-runner seam."""
    from utils.models import SourcingRun
    from utils.procurement_agent.agents.sourcing_agent import SourcingAgent

    if flag_on:
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
    else:
        monkeypatch.delenv("RANKING_BANDS_V1", raising=False)
    tier1, tier2, tier3 = _gusher_live_tiers()
    agent = SourcingAgent()
    run = SourcingRun(
        asset_specs_json={
            "manufacturer": "Gusher Pumps", "model": "Type 21",
            "part_number": _GUSHER_PN, "voltage": "N/A",
            "category": "Part", "detected_type": "mechanical seal",
            "shaft_size": '1-5/8"',
        },
        urgency_factor=0.3,
        warranty_status="unknown",
    )
    with patch.object(agent, "_run_tier1", return_value=tier1), \
         patch.object(agent, "_run_tier2", return_value=tier2), \
         patch.object(agent, "_run_tier3", return_value=tier3), \
         patch.object(agent, "_apollo_clarify", return_value=tier3), \
         patch.object(agent, "_resolve_contact", return_value=tier3):
        return agent.run(run)


class TestAcceptanceGusher:
    """RANKING_BANDS_SPEC.md §9, criteria 1-8, on the live-run fixture."""

    @pytest.fixture
    def response(self, monkeypatch):
        from api_server import _transform_sourcing_results
        return _transform_sourcing_results(_run_gusher_pipeline(monkeypatch))

    def test_criterion_1_sealit123_ranks_first_when_dxp_unconfirmed(self, response):
        """Exact PN + real URL + $53.25 ranks #1 overall — above every
        category-only and mock candidate."""
        assert response["findings"][0]["vendorName"] == "Sealit123"
        assert response["findings"][0]["band"] == "A"
        assert response["findings"][0]["price"] == 53.25

    def test_criterion_1b_sealit123_is_2_when_dxp_confirmed(self, monkeypatch):
        from api_server import _transform_sourcing_results
        quote_index = {"by_thread": {}, "domain_threads": {}, "by_domain": {
            "dxpe.com": {"status": "confirmed", "supplier_domain": "dxpe.com",
                         "thread_id": "t-dxp", "confidence": 0.9,
                         "payload": {"unit_price": 149.0, "lead_time": "2 days"}}}}
        out = _transform_sourcing_results(_run_gusher_pipeline(monkeypatch),
                                          quote_index=quote_index)
        names = [f["vendorName"] for f in out["findings"]]
        assert names[0] == "DXP Enterprises"
        assert names[1] == "Sealit123"   # ranks #1 or #2 overall — criterion 1

    def test_criterion_2_us_seal_not_rejected_band_a(self, monkeypatch):
        """US Seal Manufacturing (exact PN) is NOT rejected — Band A, surfaced."""
        raw = _run_gusher_pipeline(monkeypatch)
        us = next(c for c in raw["tier_3"]["results"]
                  if c["vendor_name"] == "US Seal Manufacturing")
        assert us["band"] == BAND_A
        assert not us.get("rejection_reason")
        from api_server import _transform_sourcing_results
        out = _transform_sourcing_results(raw)
        assert "US Seal Manufacturing" in [f["vendorName"] for f in out["findings"]]

    def test_criterion_3_aftermarket_surface_band_b_unfloored(self, monkeypatch):
        """Zoro / Seals-Direct (compatible PN found) surface in Band B — not
        floor-rejected at 10.5."""
        raw = _run_gusher_pipeline(monkeypatch)
        cands = {c["vendor_name"]: c
                 for t in ("tier_2", "tier_3") for c in raw[t]["results"]}
        for name in ("Zoro", "Seals-Direct"):
            assert cands[name]["band"] == BAND_B, name
            assert not cands[name].get("rejection_reason"), name

    def test_criterion_4_no_mock_ranked_no_mock_numbers_seeds_in_outreach_only(self, response, monkeypatch):
        """No is_mock candidate appears as a ranked option card; no mock carries a
        suitability/confidence number; the five seeds appear (capped) in the
        outreach block only."""
        assert not any(f["isMock"] for f in response["findings"])
        finding_names = {f["vendorName"] for f in response["findings"]}
        assert finding_names.isdisjoint(set(_FIVE_MOCKS))
        raw = _run_gusher_pipeline(monkeypatch)
        mocks = [c for c in raw["tier_3"]["results"] if c.get("is_mock")]
        assert len(mocks) == 5
        for m in mocks:
            assert m["suitability_score"] is None    # the 88.0 died
            assert m["confidence_score"] is None     # the 75.0 died
        outreach_names = [s["vendorName"] for s in response["outreachTargets"]["suppliers"]]
        assert set(_FIVE_MOCKS).issubset(set(outreach_names))  # within the 5-cap

    def test_criterion_5_dxp_named_onboarded_in_outreach_and_promotable(self, response, monkeypatch):
        """DXP appears as the named onboarded supplier in the outreach block
        (first-look RFQ); a simulated confirmation promotes it to top of Band A."""
        suppliers = response["outreachTargets"]["suppliers"]
        assert suppliers[0]["vendorName"] == "DXP Enterprises"
        assert suppliers[0]["onboarded"] is True
        # Promotion re-asserted end-to-end in test_criterion_1b (DXP tops findings).

    def test_criterion_7_band_order_absolute_in_findings(self, response):
        """No Band-B finding above Band A (Band C never a finding at all);
        the randomized property lives in TestBandOrderingProperty."""
        bands = [f["band"] for f in response["findings"]]
        assert bands == sorted(bands)  # A... then B...
        assert set(bands) <= {"A", "B"}

    def test_criterion_8_confidence_from_evidence_zero_implies_band_c(self, monkeypatch):
        """Confidence is evidence-derived; 0-confidence implies Band C — asserted
        across the full fixture AND a randomized candidate bank."""
        raw = _run_gusher_pipeline(monkeypatch)
        for t in ("tier_1", "tier_2", "tier_3"):
            for c in raw[t]["results"]:
                if c.get("is_mock"):
                    assert c["confidence_score"] is None
                    continue
                conf = c["confidence_score"]
                if conf == 0.0:
                    assert c["band"] == BAND_C, c["vendor_name"]
                if c["band"] in (BAND_A, BAND_B):
                    assert conf > 0.0, c["vendor_name"]
        # The eval bank: randomized candidates spanning every evidence shape.
        for seed in range(30):
            rng = random.Random(7000 + seed)
            for i in range(20):
                c = annotate_candidate(_rand_candidate(rng, i), _GUSHER_PN)
                if c.get("is_mock"):
                    assert c["confidence_score"] is None
                elif c["confidence_score"] == 0.0:
                    assert c["band"] == BAND_C
                else:
                    assert c["band"] in (BAND_A, BAND_B)

    # Criterion 6 (no frozen-verdict replay; version bump invalidates) is encoded
    # at the API seam in TestCacheFirstReadPolicy (T5). Criterion 9 (suite green,
    # Night-7 eval cases pass) is the suite itself.


# ---------------------------------------------------------------------------
# T6 — flag-off byte-identical parity
# ---------------------------------------------------------------------------

_BAND_KEYS = {"band", "evidence_quality", "banded", "provenance", "band_note",
              "band_c_capped", "edge_stale", "price_stale", "matcher_version",
              "stale_hint", "pn_source"}


def _collect_keys(obj, found: set) -> None:
    if isinstance(obj, dict):
        found.update(obj.keys())
        for v in obj.values():
            _collect_keys(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _collect_keys(v, found)


class TestFlagOffParity:
    """RANKING_BANDS_V1 OFF ⇒ behavior byte-identical to pre-band code: no band
    vocabulary anywhere, fabricated scores and floor verdicts exactly as before,
    legacy response shape. (The wider parity net is the pre-existing suite, which
    runs entirely flag-off and stays green.)"""

    def test_pipeline_result_carries_no_band_vocabulary(self, monkeypatch):
        raw = _run_gusher_pipeline(monkeypatch, flag_on=False)
        keys: set = set()
        _collect_keys(raw, keys)
        assert keys.isdisjoint(_BAND_KEYS), keys & _BAND_KEYS

    def test_legacy_scores_and_floor_verdicts_preserved(self, monkeypatch):
        raw = _run_gusher_pipeline(monkeypatch, flag_on=False)
        by_name = {c["vendor_name"]: c
                   for t in ("tier_1", "tier_2", "tier_3") for c in raw[t]["results"]}
        # The fabricated constants are UNTOUCHED flag-off (today's behavior).
        for name in _FIVE_MOCKS:
            assert by_name[name]["suitability_score"] == 88.0
            assert by_name[name]["confidence_score"] == 75.0
        # The floor inversion is UNTOUCHED flag-off (today's defect, preserved).
        assert by_name["US Seal Manufacturing"]["rejection_reason"] == "suitability_below_floor"
        assert by_name["Zoro"]["rejection_reason"] == "suitability_below_floor"
        assert "ranking_bands:v1" not in raw["filters_applied"]

    def test_legacy_response_shape(self, monkeypatch):
        from api_server import _transform_sourcing_results
        out = _transform_sourcing_results(_run_gusher_pipeline(monkeypatch, flag_on=False))
        assert set(out.keys()) == {"tier1", "tier2", "tier3",
                                   "warrantyBanner", "tier3CapabilityPivot"}

    def test_flag_off_result_json_stable_across_runs(self, monkeypatch):
        """Determinism guard: two identical flag-off runs serialize identically —
        the strongest cheap byte-parity statement available without a pre-change
        binary to diff against."""
        a = json.dumps(_run_gusher_pipeline(monkeypatch, flag_on=False), sort_keys=True, default=str)
        b = json.dumps(_run_gusher_pipeline(monkeypatch, flag_on=False), sort_keys=True, default=str)
        assert a == b


# ---------------------------------------------------------------------------
# SourcingAgent integration seam (flag-on annotates; flag-off untouched)
# ---------------------------------------------------------------------------

def _run_agent_with(tier3: list[dict], monkeypatch) -> dict:
    from utils.models import SourcingRun
    from utils.procurement_agent.agents.sourcing_agent import SourcingAgent

    agent = SourcingAgent()
    run = SourcingRun(
        asset_specs_json={
            "manufacturer": "Gusher Pumps",
            "model": "Type 21",
            "part_number": _GUSHER_PN,
            "voltage": "N/A",
            "category": "Part",
            "detected_type": "mechanical seal",
        },
        urgency_factor=0.3,
        warranty_status="unknown",
    )
    with patch.object(agent, "_run_tier1", return_value=[]), \
         patch.object(agent, "_run_tier2", return_value=[]), \
         patch.object(agent, "_run_tier3", return_value=tier3), \
         patch.object(agent, "_apollo_clarify", return_value=tier3), \
         patch.object(agent, "_resolve_contact", return_value=tier3):
        return agent.run(run)


class TestSourcingAgentSeam:
    def test_flag_off_result_carries_no_band_annotations(self, monkeypatch):
        monkeypatch.delenv("RANKING_BANDS_V1", raising=False)
        result = _run_agent_with([_sealit(), _zoro()], monkeypatch)
        for c in result["tier_3"]["results"]:
            assert "band" not in c
            assert "evidence_quality" not in c
            assert "banded" not in c
        assert "ranking_bands:v1" not in result["filters_applied"]

    def test_flag_on_result_is_banded(self, monkeypatch):
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        result = _run_agent_with([_zoro(), _sealit()], monkeypatch)
        t3 = result["tier_3"]["results"]
        assert all(c.get("band") in (BAND_A, BAND_B, BAND_C) for c in t3)
        assert "ranking_bands:v1" in result["filters_applied"]
        # Band-A Sealit ordered above Band-B Zoro despite Zoro entering first.
        assert [c["vendor_name"] for c in t3] == ["Sealit123", "Zoro"]

    def test_flag_on_pipeline_floor_fires_then_bands_clear_it(self, monkeypatch):
        """End-to-end Gusher floor regression: the LIVE pipeline floor (30) rejects
        US Seal (12.6) and Zoro (10.5); the flag-on band pass re-scopes it —
        neither part-finder ends the run rejected."""
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        result = _run_agent_with([_us_seal(), _zoro()], monkeypatch)
        by_name = {c["vendor_name"]: c for c in result["tier_3"]["results"]}
        us, zoro = by_name["US Seal Manufacturing"], by_name["Zoro"]
        assert us["band"] == BAND_A and not us.get("rejection_reason")
        assert us["band_note"] == "floor_cleared_band_a"
        assert zoro["band"] == BAND_B and not zoro.get("rejection_reason")
        assert zoro["band_note"] == "floor_cleared_pn_evidence"

    def test_flag_off_pipeline_floor_still_rejects(self, monkeypatch):
        """Parity guard: flag OFF, the floor behaves exactly as today (US Seal and
        Zoro rejected below the 30 floor)."""
        monkeypatch.delenv("RANKING_BANDS_V1", raising=False)
        result = _run_agent_with([_us_seal(), _zoro()], monkeypatch)
        by_name = {c["vendor_name"]: c for c in result["tier_3"]["results"]}
        assert by_name["US Seal Manufacturing"]["rejection_reason"] == "suitability_below_floor"
        assert by_name["Zoro"]["rejection_reason"] == "suitability_below_floor"
