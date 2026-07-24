"""
Cache-as-SEEDS regression suite (RANKING_BANDS_V1 × known_parts).

DESIGN CORRECTION (founder decision, restoring RANKING_BANDS_SPEC §6's primary
rule): the known_parts cache ACCELERATES discovery, it never REPLACES it.
Within-TTL replay-only runs are removed — fresh Tier-2/3 discovery runs on
EVERY flag-on run; TTL-fresh edges are merged into today's candidate pool as
SEEDS (dedupe by domain, fresh wins volatile fields, the edge contributes its
stored evidence), then ONE band pass runs over the union.

Why replay-only was wrong (live evidence): a within-TTL Gusher re-run skipped
Tavily entirely — a thinner page (4-5 vs 15+ candidates pre-banding), a
DXP-only outreach block (Band C is never cached, correctly), no ability to
surface newly-listed vendors, and cached classification mistakes vetoing
findings with no fresh evidence to correct them.

THE DURABLE INVARIANT (replaces the old fresh-vs-cache parity test): a second
run of the same part surfaces AT LEAST the first run's findings (seeds
guarantee the floor) and MAY surface more (fresh discovery adds) — traversing
the REAL merge path including the type gate.

Flag OFF: the legacy cache-first replay (_result_from_cached_edges) is
byte-identical — floor drop, type gate, always-serve — pinned below.
"""
from __future__ import annotations

import json

import pytest

# Importing registers the fixture in this module for pytest.
from utils.procurement_agent.tests.test_api_server import (  # noqa: F401
    _create_run, _mock_sourcing_pipeline, _set_run, api,
)

_GUSHER_PN = "84004-28-C238CBC"
_SPECS = {"manufacturer": "Gusher Pumps", "part_number": _GUSHER_PN,
          "detected_type": "mechanical seal", "model": "Type 21"}


def _zoro_edge(**overrides) -> dict:
    """The live shape: PN evidence + real URL, legacy suitability far below the
    30% floor (written back by the flag-on run that rescued it)."""
    edge = {
        "supplier_id": "zoro.com", "display_name": "Zoro",
        "purchase_channel": "marketplace", "tier": 2,
        "match_type": "Aftermarket Compatible", "found_pn": "84004-28SP",
        "suitability": 10.5, "source_url": "https://zoro.com/p/84004-28sp",
        "price": 41.99, "lead_days": 3,
    }
    edge.update(overrides)
    return edge


def _fresh_result(*cands, marker=True) -> dict:
    result = {
        "tier_1": {"results": [], "count": 0},
        "tier_2": {"results": list(cands), "count": len(cands)},
        "tier_3": {"results": [], "count": 0},
        "filters_applied": ["ranking_bands:v1"] if marker else [],
    }
    if marker:
        from utils.procurement_agent.ranking_bands import apply_ranking_bands
        apply_ranking_bands(result, _GUSHER_PN)
    return result


def _seal_it(**over) -> dict:
    c = {"vendor_name": "Seal It", "found_part_number": "84004-28",
         "source_url": "https://sealit.example/gusher/84004-28",
         "base_price": 53.25, "price_tbd": False, "suitability_score": 80.0}
    c.update(over)
    return c


# ---------------------------------------------------------------------------
# Unit level — _seed_candidates_into_result merge rules
# ---------------------------------------------------------------------------

class TestSeedMergeRules:
    def _merge(self, api, result, edges, req_cls="SEAL"):
        return api._api_server._seed_candidates_into_result(
            result, edges, req_cls, _GUSHER_PN)

    def test_seed_only_edge_joins_the_pool_and_bands(self, api):
        result = self._merge(api, _fresh_result(_seal_it()), [_zoro_edge()],
                             req_cls=None)
        names = [c["vendor_name"] for c in result["tier_2"]["results"]]
        assert names == ["Seal It", "Zoro"]
        (zoro,) = [c for c in result["tier_2"]["results"]
                   if c["vendor_name"] == "Zoro"]
        assert zoro["seeded_from_cache"] is True
        # Low legacy suitability was ANNOTATED then re-scoped by the band pass
        # (PN evidence) — the seed surfaces, exactly as fresh discovery would.
        assert zoro["rejection_reason"] is None
        assert zoro["band"] in ("A", "B")
        assert result["tier_2"]["count"] == 2

    def test_seed_floor_stands_without_pn_evidence(self, api):
        edge = _zoro_edge(supplier_id="noev.example", display_name="No Evidence Co",
                          found_pn=None, match_type="Functional Alternative",
                          suitability=0.5, price=None,
                          source_url="https://noev.example/catalog")
        result = self._merge(api, _fresh_result(_seal_it()), [edge], req_cls=None)
        (seed,) = [c for c in result["tier_2"]["results"]
                   if c["vendor_name"] == "No Evidence Co"]
        # Band B (discovered listing), no PN evidence: the band-aware floor
        # keeps the rejection — off the UI, still present for audit.
        assert seed["band"] == "B"
        assert seed["rejection_reason"] == "suitability_below_floor"

    def test_vendor_also_found_fresh_dedupes_fresh_wins(self, api):
        # Fresh found Zoro TODAY at a new price and with no visible PN; the
        # seed contributes its stored PN evidence, fresh fields win the rest.
        fresh_zoro = {"vendor_name": "Zoro",
                      "source_url": "https://zoro.com/p/84004-28sp",
                      "base_price": 44.50, "price_tbd": False,
                      "suitability_score": 65.0, "pn_match_status": "not_visible"}
        result = self._merge(api, _fresh_result(_seal_it(), fresh_zoro),
                             [_zoro_edge()], req_cls=None)
        zoros = [c for c in result["tier_2"]["results"]
                 if c["vendor_name"] == "Zoro"]
        assert len(zoros) == 1                       # ONE candidate, deduped
        (zoro,) = zoros
        assert zoro["base_price"] == 44.50           # fresh volatile fields win
        assert zoro["suitability_score"] == 65.0
        assert zoro["found_part_number"] == "84004-28SP"  # seed evidence merged
        assert zoro.get("seeded_from_cache") is None      # it IS the fresh cand

    def test_dedupe_never_overwrites_fresh_pn_evidence(self, api):
        fresh_zoro = {"vendor_name": "Zoro",
                      "source_url": "https://zoro.com/p/84004-28sp",
                      "base_price": 44.50, "price_tbd": False,
                      "suitability_score": 65.0,
                      "found_part_number": "84004-28-C238CBC",
                      "pn_match_status": "exact_match"}
        result = self._merge(api, _fresh_result(fresh_zoro), [_zoro_edge()],
                             req_cls=None)
        (zoro,) = result["tier_2"]["results"]
        assert zoro["found_part_number"] == "84004-28-C238CBC"  # fresh wins
        assert zoro["pn_match_status"] == "exact_match"

    def test_subdomain_variant_edge_merges_instead_of_duplicating(self, api):
        # MATCHING_CLEANUP F3: the live Global Industrial case — a seeded
        # static.globalindustrial.com PDF edge must MERGE into the vendor found
        # fresh at www.globalindustrial.com (registrable-domain keying), not
        # render a second card. Fresh volatile fields win; the edge contributes
        # its stored PN evidence.
        fresh_gi = {"vendor_name": "Global Industrial",
                    "source_url": "https://www.globalindustrial.com/p/severe-duty-motor",
                    "base_price": 36175.0, "price_tbd": False,
                    "suitability_score": 50.0, "pn_match_status": "not_visible"}
        edge = _zoro_edge(
            supplier_id="static.globalindustrial.com",
            display_name="Global Industrial",
            found_pn="ECP844156TR-5",
            source_url="https://static.globalindustrial.com/products/pdf/B3085296.pdf",
            price=22051.72)
        result = self._merge(api, _fresh_result(_seal_it(), fresh_gi), [edge],
                             req_cls=None)
        gis = [c for c in result["tier_2"]["results"]
               if c["vendor_name"] == "Global Industrial"]
        assert len(gis) == 1                              # merged, not appended
        (gi,) = gis
        assert gi["base_price"] == 36175.0                # fresh volatile wins
        assert gi["found_part_number"] == "ECP844156TR-5"  # edge PN contributed
        assert gi.get("seeded_from_cache") is None        # it IS the fresh cand

    def test_stored_rejection_edges_never_seed(self, api):
        edge = _zoro_edge(rejection_reason="pn_mismatch")
        result = self._merge(api, _fresh_result(_seal_it()), [edge], req_cls=None)
        assert [c["vendor_name"] for c in result["tier_2"]["results"]] == ["Seal It"]

    def test_unbanded_result_stays_legacy_shaped(self, api):
        # Marker absent (band pass failed upstream): seeds still merge but no
        # band keys appear — fail-soft consistency.
        result = self._merge(api, _fresh_result(_seal_it(), marker=False),
                             [_zoro_edge()], req_cls=None)
        (zoro,) = [c for c in result["tier_2"]["results"]
                   if c["vendor_name"] == "Zoro"]
        assert "band" not in zoro


# ---------------------------------------------------------------------------
# Type gate on seeds — the PN-evidence exception (the Springer case)
# ---------------------------------------------------------------------------

class TestSeedTypeGatePnEvidenceException:
    """Live evidence: 'Rejected cached edge (type_gate): Springer Pumps
    result_class=PUMP != request_class=SEAL' — while the edge carried
    found_pn=84004-28SP (the requested seal-kit family; a kit listed on a
    pump-parts page, classed PUMP at write from vendor+url only — the weakest
    signal). Rule: PN evidence reaches banding regardless of stored class
    (annotate, don't remove); no PN evidence + confirmed mismatch keeps the
    Fix-B1 drop. (The FRESH path never had this hard-drop — its TypeGate is
    multiplicative and sees full snippet context; this closes the asymmetry.)"""

    _SPRINGER = {
        "supplier_id": "springerpumps.com", "display_name": "Springer Pumps",
        "purchase_channel": "rfq", "tier": 3,
        "match_type": "Aftermarket Compatible", "found_pn": "84004-28SP",
        "suitability": 0.5, "price": None,
        "source_url": "https://www.springerpumps.com/gusher-84004-28sp",
    }
    _PUMP_JUNK = {
        "supplier_id": "pumpworld.example", "display_name": "Pump World",
        "purchase_channel": "rfq", "tier": 3,
        "match_type": "Functional Alternative", "found_pn": None,
        "suitability": 55.0, "price": None,
        "source_url": "https://pumpworld.example/centrifugal-pumps",
    }

    def _query_cls(self, api):
        from utils.sourcing_archieved.scoring import _query_noun_class
        return _query_noun_class(api._api_server._specs_from_dict(_SPECS))

    def test_springer_shaped_seed_reaches_banding_and_surfaces(self, api):
        # PN evidence + class mismatch: the gate stands down (annotated), the
        # floor is rescoped by the band pass — Band B, on the page.
        q_cls = self._query_cls(api)
        assert q_cls == "SEAL"  # the live request class; premise of the case
        result = api._api_server._seed_candidates_into_result(
            _fresh_result(_seal_it()), [dict(self._SPRINGER)], q_cls, _GUSHER_PN)
        (springer,) = [c for c in result["tier_3"]["results"]
                       if c["vendor_name"] == "Springer Pumps"]
        assert springer["type_gate_note"] == "class_mismatch_pn_evidence"
        assert springer["band"] == "B"                 # compatible-PN evidence
        assert springer["rejection_reason"] is None    # floor rescoped
        # Genuine cross-class junk (no PN evidence) keeps the Fix-B1 drop.
        result2 = api._api_server._seed_candidates_into_result(
            _fresh_result(_seal_it()), [dict(self._PUMP_JUNK)], q_cls, _GUSHER_PN)
        assert not any(c["vendor_name"] == "Pump World"
                       for tk in ("tier_1", "tier_2", "tier_3")
                       for c in result2[tk]["results"])

    def test_springer_surfaces_end_to_end_junk_stays_off(self, api, monkeypatch,
                                                         tmp_path):
        # Through the REAL background path: both seed shapes in the cache; the
        # PN-evidence one lands in findings, the junk one never renders.
        import sqlite3  # noqa: F401  (parity with sibling tests' imports)
        from utils import known_parts
        from utils.procurement_agent.ranking_bands import MATCHER_VERSION
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        monkeypatch.setattr(known_parts, "_DB_PATH",
                            str(tmp_path / "known_parts.json"))
        pk = known_parts.canonical_part_key("Gusher Pumps", _GUSHER_PN)
        # Seed the store directly in the on-disk shape (current version stamp).
        import json as _json
        with open(tmp_path / "known_parts.json", "w", encoding="utf-8") as f:
            _json.dump({pk: {"edges": {
                "springerpumps.com": {**self._SPRINGER, "first_seen": "x",
                                      "last_seen": known_parts._now(),
                                      "matcher_version": MATCHER_VERSION},
                "pumpworld.example": {**self._PUMP_JUNK, "first_seen": "x",
                                      "last_seen": known_parts._now(),
                                      "matcher_version": MATCHER_VERSION},
            }, "updated_at": "x"}}, f)
        _mock_sourcing_pipeline(monkeypatch,
                                sourcing_result=_fresh_result(_seal_it()),
                                artifact=None)
        rid = _create_run(api)
        _set_run(api, rid, asset_specs_json=json.dumps(_SPECS))
        assert api.post(f"/api/runs/{rid}/confirm-intake").status_code == 200
        sr = api.get(f"/api/runs/{rid}").json()["sourcing_results"]
        names = {f["vendorName"] for f in sr["findings"]}
        assert "Springer Pumps" in names               # PN evidence surfaced
        assert "Pump World" not in names               # junk gated off
        assert "Pump World" not in {
            s["vendorName"] for s in sr["outreachTargets"]["suppliers"]}


# ---------------------------------------------------------------------------
# THE DURABLE INVARIANT — second run surfaces AT LEAST the first run's findings
# ---------------------------------------------------------------------------

def _findings(api, rid) -> set:
    sr = api.get(f"/api/runs/{rid}").json()["sourcing_results"]
    return {f["vendorName"] for f in sr.get("findings", [])}


class TestSecondRunSupersetInvariant:
    def _run(self, api, monkeypatch, sourcing_result) -> str:
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=sourcing_result,
                                artifact=None)
        rid = _create_run(api)
        _set_run(api, rid, asset_specs_json=json.dumps(_SPECS))
        assert api.post(f"/api/runs/{rid}/confirm-intake").status_code == 200
        return rid

    def test_second_run_surfaces_at_least_the_first_runs_findings(
            self, api, monkeypatch, tmp_path):
        from utils import known_parts
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        monkeypatch.setattr(known_parts, "_DB_PATH",
                            str(tmp_path / "known_parts.json"))

        # Run 1 — fresh discovery finds Seal It + the Zoro shape (rescued by
        # the band pass); the write-back stores both WITH Zoro's low legacy
        # suitability (the poison shape the old replay floored).
        zoro_fresh = {"vendor_name": "Zoro", "found_part_number": "84004-28SP",
                      "source_url": "https://zoro.com/p/84004-28sp",
                      "base_price": 41.99, "price_tbd": False,
                      "suitability_score": 10.5,
                      "rejection_reason": "suitability_below_floor"}
        rid1 = self._run(api, monkeypatch,
                         _fresh_result(_seal_it(), zoro_fresh))
        first = _findings(api, rid1)
        assert first == {"Seal It", "Zoro"}
        pk = known_parts.canonical_part_key("Gusher Pumps", _GUSHER_PN)
        assert {e["display_name"] for e in known_parts.get_edges(pk)} == \
            {"Seal It", "Zoro"}

        # Run 2 — today's search finds only a NEW vendor. Discovery must run
        # (the new vendor proves it) AND the seeds guarantee the floor: the
        # second run's findings are a SUPERSET of the first run's. This
        # traverses the REAL merge path — request class SEAL detected from the
        # specs, the type gate live in-path for every seed.
        fresh_find = {"vendor_name": "Fresh Find Co",
                      "found_part_number": _GUSHER_PN,
                      "source_url": "https://freshfind.example/p/84004-28-c238cbc",
                      "base_price": 49.0, "price_tbd": False,
                      "suitability_score": 70.0}
        rid2 = self._run(api, monkeypatch, _fresh_result(fresh_find))
        second = _findings(api, rid2)
        assert second >= first                       # seeds: the guaranteed floor
        assert "Fresh Find Co" in second             # fresh discovery adds

    def test_seeds_never_self_refresh_their_ttl(self, api, monkeypatch,
                                                tmp_path):
        # The merge runs AFTER the write-back: a seed-only vendor absent from
        # today's discovery must NOT get its last_seen/matcher stamps renewed
        # by merely being seeded (else an edge could never expire).
        from utils import known_parts
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        monkeypatch.setattr(known_parts, "_DB_PATH",
                            str(tmp_path / "known_parts.json"))
        rid1 = self._run(api, monkeypatch, _fresh_result(_seal_it()))
        pk = known_parts.canonical_part_key("Gusher Pumps", _GUSHER_PN)
        (edge_before,) = known_parts.get_edges(pk)
        rid2 = self._run(api, monkeypatch, _fresh_result(
            {"vendor_name": "Other Co", "found_part_number": _GUSHER_PN,
             "source_url": "https://other.example/p/1", "base_price": 60.0,
             "price_tbd": False, "suitability_score": 70.0}))
        assert "Seal It" in _findings(api, rid2)     # it seeded...
        sealit_after = next(e for e in known_parts.get_edges(pk)
                            if e["display_name"] == "Seal It")
        assert sealit_after["last_seen"] == edge_before["last_seen"]  # ...untouched


# ---------------------------------------------------------------------------
# Flag OFF — the legacy replay is byte-identical (short-circuit, floor, gate)
# ---------------------------------------------------------------------------

class TestFlagOffLegacyReplayUnchanged:
    def test_legacy_replay_still_floors_below_30(self, api):
        result = api._api_server._result_from_cached_edges([_zoro_edge()])
        assert result["tier_2"]["results"] == []
        assert result["tier_3"]["results"] == []

    def test_legacy_replay_still_drops_stored_rejections(self, api):
        result = api._api_server._result_from_cached_edges(
            [_zoro_edge(rejection_reason="pn_mismatch", suitability=90.0)])
        assert result["tier_2"]["results"] == []

    def test_flag_off_cache_hit_short_circuits_exactly_as_before(
            self, api, monkeypatch, tmp_path):
        # End-to-end legacy: write (no band gate), replay (always-serve,
        # below-floor dropped, discovery skipped) — pre-band behavior.
        from utils import known_parts
        monkeypatch.setattr(known_parts, "_DB_PATH",
                            str(tmp_path / "known_parts.json"))
        legacy = {
            "tier_1": {"results": [], "count": 0},
            "tier_2": {"results": [
                _seal_it(),
                {"vendor_name": "Zoro", "found_part_number": "84004-28SP",
                 "source_url": "https://zoro.com/p/84004-28sp",
                 "base_price": 41.99, "price_tbd": False,
                 "suitability_score": 10.5},
            ], "count": 2},
            "tier_3": {"results": [], "count": 0},
            "filters_applied": [],
        }
        _mock_sourcing_pipeline(monkeypatch, sourcing_result=legacy, artifact=None)
        rid1 = _create_run(api)
        _set_run(api, rid1, asset_specs_json=json.dumps(_SPECS))
        assert api.post(f"/api/runs/{rid1}/confirm-intake").status_code == 200

        import utils.procurement_agent.agents.sourcing_agent as sa_mod
        from unittest.mock import Mock
        monkeypatch.setattr(sa_mod, "SourcingAgent", Mock(side_effect=AssertionError(
            "flag-off cache hit must skip discovery (legacy short-circuit)")))
        rid2 = _create_run(api)
        _set_run(api, rid2, asset_specs_json=json.dumps(_SPECS))
        assert api.post(f"/api/runs/{rid2}/confirm-intake").status_code == 200
        sr = api.get(f"/api/runs/{rid2}").json()["sourcing_results"]
        assert "findings" not in sr                       # no banded keys flag-off
        tier2_names = [c["vendorName"] for c in sr["tier2"]]
        assert "Zoro" not in tier2_names                  # legacy floor still drops
        assert "Seal It" in tier2_names
