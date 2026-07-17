"""
Cache-replay banding regression (RANKING_BANDS_V1 × known_parts replay).

THE BUG (observed live, Gusher re-run, flag on): `_result_from_cached_edges`
applied the LEGACY suitability floor to stored edges and DROPPED the losers
BEFORE the band pass ran, so the band-aware floor re-scope (rescope_floor —
which explicitly protects PN-evidence candidates like Zoro, legacy
suitability 10.5 with found_pn=84004-28SP) never saw them. Result: the second
flag-on run of the same part (cache hit within TTL) surfaced FEWER candidates
than the first run that wrote the edges.

THE FIX: flag-on replay ANNOTATES the floor verdict (annotate-don't-remove,
exactly like fresh discovery) and hands every TTL-fresh edge to the band pass
that always follows on the cache-hit path; rescope_floor then clears or keeps
the rejection by band. Flag-off replay keeps the legacy drop byte-identically.

THE DURABLE GUARANTEE is the parity test below: for the same part, a fresh
flag-on run's surviving FINDINGS equal an immediately-following cache-hit
run's surviving findings. (Findings — Band A/B — are the comparable set: the
Band-C outreach block is structurally never cached by the spec-§6 write gate,
so it is re-derived, not replayed.)
"""
from __future__ import annotations

import json

import pytest

# Importing registers the fixture in this module for pytest.
from utils.procurement_agent.tests.test_api_server import (  # noqa: F401
    _create_run, _mock_sourcing_pipeline, _set_run, api,
)

_GUSHER_PN = "84004-28"


# ---------------------------------------------------------------------------
# Unit level — _result_from_cached_edges floor behavior per mode
# ---------------------------------------------------------------------------

def _zoro_edge(**overrides) -> dict:
    """The live poison shape: PN evidence + real URL, but a legacy suitability
    far below the 30% floor (written back by the flag-on run that RESCUED it)."""
    edge = {
        "supplier_id": "zoro.com", "display_name": "Zoro",
        "purchase_channel": "marketplace", "tier": 2,
        "match_type": "Aftermarket Compatible", "found_pn": "84004-28SP",
        "suitability": 10.5, "source_url": "https://zoro.com/p/84004-28sp",
        "price": 41.99, "lead_days": 3,
    }
    edge.update(overrides)
    return edge


class TestReplayFloorPerMode:
    def test_banded_replay_keeps_floored_edge_annotated(self, api):
        result = api._api_server._result_from_cached_edges(
            [_zoro_edge()], banded=True)
        (cand,) = result["tier_2"]["results"]
        assert cand["vendor_name"] == "Zoro"
        assert cand["rejection_reason"] == "suitability_below_floor"  # pending rescope
        assert cand["found_part_number"] == "84004-28SP"              # bandable evidence

    def test_banded_replay_then_band_pass_rescues_pn_evidence(self, api):
        from utils.procurement_agent.ranking_bands import apply_ranking_bands
        result = api._api_server._result_from_cached_edges(
            [_zoro_edge()], banded=True)
        apply_ranking_bands(result, _GUSHER_PN)
        (cand,) = result["tier_2"]["results"]
        assert cand["rejection_reason"] is None            # the band-aware floor cleared it
        assert cand["band"] in ("A", "B")                  # PN evidence earned the band

    def test_banded_replay_floor_still_stands_without_pn_evidence(self, api):
        # A no-PN-evidence Band-B edge below the floor stays rejected AFTER the
        # band pass — the floor still bites where the band model says it should.
        from utils.procurement_agent.ranking_bands import apply_ranking_bands
        edge = _zoro_edge(supplier_id="springer.com", display_name="Springer Pumps",
                          match_type="Functional Alternative", found_pn=None,
                          suitability=0.5, price=None,
                          source_url="https://springerpumps.com/pumps")
        result = api._api_server._result_from_cached_edges([edge], banded=True)
        apply_ranking_bands(result, _GUSHER_PN)
        cands = result["tier_2"]["results"] + result["tier_3"]["results"]
        (cand,) = cands
        assert cand["band"] == "B"
        assert cand["rejection_reason"] == "suitability_below_floor"

    def test_flag_off_replay_drops_exactly_as_before(self, api):
        # Legacy parity: banded=False keeps the hard drop, byte-identical.
        result = api._api_server._result_from_cached_edges(
            [_zoro_edge()], banded=False)
        assert result["tier_2"]["results"] == []
        assert result["tier_3"]["results"] == []

    def test_stored_rejection_reason_still_drops_in_both_modes(self, api):
        edge = _zoro_edge(rejection_reason="pn_mismatch")
        for banded in (False, True):
            result = api._api_server._result_from_cached_edges([edge], banded=banded)
            assert result["tier_2"]["results"] == []


# ---------------------------------------------------------------------------
# THE DURABLE GUARANTEE — fresh-run vs cache-hit-run surviving-set parity
# ---------------------------------------------------------------------------

def _flag_on_fresh_result() -> dict:
    """What a flag-on SourcingAgent emits for the Gusher shape: candidates
    annotated by the REAL band pass (bands assigned, legacy floor verdicts
    re-scoped) + the ranking_bands:v1 marker. Zoro is the regression pivot:
    legacy floor annotated it, the band pass rescued it (PN evidence)."""
    from utils.procurement_agent.ranking_bands import apply_ranking_bands
    result = {
        "tier_1": {"results": [], "count": 0},
        "tier_2": {"results": [
            {
                "vendor_name": "Seal It", "found_part_number": "84004-28",
                "source_url": "https://sealit.example/gusher/84004-28",
                "base_price": 53.25, "price_tbd": False,
                "suitability_score": 80.0,
            },
            {
                # The Zoro shape: PN evidence, real URL, legacy floor verdict.
                "vendor_name": "Zoro", "found_part_number": "84004-28SP",
                "source_url": "https://zoro.com/p/84004-28sp",
                "base_price": 41.99, "price_tbd": False,
                "suitability_score": 10.5,
                "rejection_reason": "suitability_below_floor",
            },
        ], "count": 2},
        "tier_3": {"results": [
            {
                # No PN evidence, below floor: the band-aware floor stands —
                # never surfaced, never written back.
                "vendor_name": "Springer Pumps", "found_part_number": None,
                "source_url": "https://springerpumps.com/pumps",
                "base_price": 0.0, "price_tbd": True,
                "suitability_score": 0.5,
                "rejection_reason": "suitability_below_floor",
            },
        ], "count": 1},
        "filters_applied": ["ranking_bands:v1"],
    }
    apply_ranking_bands(result, _GUSHER_PN)
    return result


def _surviving_findings(api, rid) -> list:
    sr = api.get(f"/api/runs/{rid}").json()["sourcing_results"]
    return [f["vendorName"] for f in sr.get("findings", [])]


class TestFreshVsCacheHitParity:
    """Criterion: a cache-hit run of a part whose edges include a PN-evidence
    Band-B/A candidate with LOW legacy suitability (the Zoro shape) surfaces
    the same finding set the fresh run did."""

    def _run(self, api, monkeypatch, specs, sourcing_result=None,
             forbid_discovery=False) -> str:
        if forbid_discovery:
            import utils.procurement_agent.agents.sourcing_agent as sa_mod
            from unittest.mock import Mock
            def _no_discovery(*a, **kw):
                raise AssertionError(
                    "discovery ran — the cache hit did not short-circuit")
            monkeypatch.setattr(sa_mod, "SourcingAgent", Mock(side_effect=_no_discovery))
        else:
            _mock_sourcing_pipeline(monkeypatch, sourcing_result=sourcing_result,
                                    artifact=None)
        rid = _create_run(api)
        _set_run(api, rid, asset_specs_json=json.dumps(specs))
        assert api.post(f"/api/runs/{rid}/confirm-intake").status_code == 200
        return rid

    def test_cache_hit_surviving_set_matches_fresh_run(self, api, monkeypatch,
                                                       tmp_path):
        from utils import known_parts
        monkeypatch.setenv("RANKING_BANDS_V1", "1")
        monkeypatch.setattr(known_parts, "_DB_PATH",
                            str(tmp_path / "known_parts.json"))
        specs = {"manufacturer": "Gusher Pumps", "part_number": _GUSHER_PN}

        # Run 1 — fresh discovery (mocked flag-on agent output), writes edges.
        rid1 = self._run(api, monkeypatch, specs,
                         sourcing_result=_flag_on_fresh_result())
        fresh_findings = _surviving_findings(api, rid1)
        assert sorted(fresh_findings) == ["Seal It", "Zoro"]

        # The write-back stored Zoro WITH its low legacy suitability — the
        # exact poison shape the replay floor used to drop.
        part_key = known_parts.canonical_part_key("Gusher Pumps", _GUSHER_PN)
        edges = {e["display_name"]: e for e in known_parts.get_edges(part_key)}
        assert edges["Zoro"]["suitability"] == 10.5
        assert "Springer Pumps" not in edges     # floored-no-evidence never cached

        # Run 2 — cache hit (discovery forbidden): the surviving finding set
        # must equal the fresh run's. Before the fix this was ["Seal It"].
        rid2 = self._run(api, monkeypatch, specs, forbid_discovery=True)
        cache_findings = _surviving_findings(api, rid2)
        assert sorted(cache_findings) == sorted(fresh_findings)

        # And the rescued candidate is a real banded finding, price intact.
        sr2 = api.get(f"/api/runs/{rid2}").json()["sourcing_results"]
        (zoro,) = [f for f in sr2["findings"] if f["vendorName"] == "Zoro"]
        assert zoro["band"] in ("A", "B")
        assert zoro["price"] == 41.99

    def test_flag_off_cache_hit_unchanged(self, api, monkeypatch, tmp_path):
        # Flag OFF end-to-end: legacy write (no band gate), legacy replay
        # (below-floor edges dropped) — the pre-band behavior, byte-identical.
        from utils import known_parts
        monkeypatch.setattr(known_parts, "_DB_PATH",
                            str(tmp_path / "known_parts.json"))
        specs = {"manufacturer": "Gusher Pumps", "part_number": _GUSHER_PN}
        legacy_result = {
            "tier_1": {"results": [], "count": 0},
            "tier_2": {"results": [
                {"vendor_name": "Seal It", "found_part_number": "84004-28",
                 "source_url": "https://sealit.example/gusher/84004-28",
                 "base_price": 53.25, "price_tbd": False,
                 "suitability_score": 80.0},
                {"vendor_name": "Zoro", "found_part_number": "84004-28SP",
                 "source_url": "https://zoro.com/p/84004-28sp",
                 "base_price": 41.99, "price_tbd": False,
                 "suitability_score": 10.5},
            ], "count": 2},
            "tier_3": {"results": [], "count": 0},
            "filters_applied": [],
        }
        rid1 = self._run(api, monkeypatch, specs, sourcing_result=legacy_result)
        rid2 = self._run(api, monkeypatch, specs, forbid_discovery=True)
        sr2 = api.get(f"/api/runs/{rid2}").json()["sourcing_results"]
        assert "findings" not in sr2                      # no banded keys flag-off
        tier2_names = [c["vendorName"] for c in sr2["tier2"]]
        assert "Zoro" not in tier2_names                  # legacy floor still drops
        assert "Seal It" in tier2_names
