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

import random
from unittest.mock import patch

import pytest

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

    def test_exact_match_claim_not_corroborated_stays_string_level(self):
        # Extractor said exact_match but the found PN is unrelated → compatible,
        # never exact (an unverifiable claim doesn't earn Band A alone).
        c = _zoro(found_part_number="TOTALLY-DIFFERENT-99", pn_match_status="exact_match")
        assert pn_evidence_for(c, _GUSHER_PN) == "compatible"


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
