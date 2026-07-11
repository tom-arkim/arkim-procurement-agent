"""
Tests for the Apollo Tier 3 suitability clarifier (SourcingAgent._apollo_clarify).

Apollo is MOCKED throughout (live auth proven separately) and supplier_registry
is isolated to a tmp sqlite file. Proves the four guarantees: store-check-first
(no call on a fresh hit), annotate-don't-remove (count unchanged), fail-soft
(Apollo/LLM failure never blocks), and no-op-without-key.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from utils import supplier_registry
from utils.models import AssetSpecs, SourcingRun
from utils.procurement_agent.agents.sourcing_agent import (
    SourcingAgent, _names_plausibly_match, _pick_sales_contact, _is_sales_title,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    return supplier_registry


def _specs() -> AssetSpecs:
    return AssetSpecs(
        manufacturer="Gusher Pumps", model="Type 21", part_number="TYPE21",
        voltage="N/A", category="Part", detected_type="mechanical seal",
    )


def _org(**over) -> dict:
    base = {
        "name": "Mesco Corporation", "industry": "wholesale",
        "country": "United States", "state": "Texas",
        "raw_address": "5226 Manor Glen Dr, Houston, TX 77345, US",
        "description": "industrial and commercial pump repair products",
        "keywords": ["pump rebuild kits", "mechanical seals"],
    }
    base.update(over)
    return base


def _enabled_apollo(org_return=None, side_effect=None) -> MagicMock:
    m = MagicMock()
    m.enabled = True
    if side_effect is not None:
        m.org_enrich.side_effect = side_effect
    else:
        m.org_enrich.return_value = org_return
    return m


def _agent(apollo=None, requirement=None) -> SourcingAgent:
    a = SourcingAgent(apollo_api_key=None)  # real client is disabled unless replaced
    if apollo is not None:
        a._apollo = apollo
    if requirement is not None:
        a._requirement_match = requirement  # type: ignore[method-assign]
    return a


# ---------------------------------------------------------------------------
# Store-check-first (the cost guarantee)
# ---------------------------------------------------------------------------

class TestStoreCheckFirst:
    def test_fresh_cache_hit_skips_apollo(self, isolated_registry):
        """Fresh cached record => verdict reused, org_enrich NOT called."""
        isolated_registry.upsert_apollo_data("mescocorp.com", {
            "suitability_status": "confirmed", "apollo_industry": "wholesale",
            "apollo_country": "United States", "is_us_confirmed": True,
        })  # apollo_enriched_at stamped to now -> fresh
        apollo = _enabled_apollo(org_return=_org())
        agent = _agent(apollo=apollo)

        cands = [{"vendor_name": "Mesco", "source_url": "https://www.mescocorp.com/x"}]
        out = agent._apollo_clarify(cands, _specs())

        apollo.org_enrich.assert_not_called()  # COST GUARANTEE
        assert cands[0]["suitability_status"] == "confirmed"
        assert cands[0]["apollo_industry"] == "wholesale"
        assert len(out) == 1

    def test_onboarded_exempt_no_call_even_if_ancient(self, isolated_registry):
        sr = isolated_registry
        old = (datetime.now(timezone.utc) - timedelta(days=5000)).isoformat()
        sr.upsert_apollo_data("x.com", {"suitability_status": "confirmed", "apollo_enriched_at": old})
        sr.update_supplier("x.com", onboarding_status="onboarded_arkim_supplier")  # name == "x.com"
        apollo = _enabled_apollo(org_return=_org())
        agent = _agent(apollo=apollo)

        cands = [{"vendor_name": "X", "source_url": "https://x.com"}]
        agent._apollo_clarify(cands, _specs())

        apollo.org_enrich.assert_not_called()  # onboarded exempt
        assert cands[0]["suitability_status"] == "confirmed"  # cached verdict reused

    def test_stale_not_onboarded_reenriches(self, isolated_registry):
        sr = isolated_registry
        old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        sr.upsert_apollo_data("x.com", {"suitability_status": "confirmed", "apollo_enriched_at": old})
        apollo = _enabled_apollo(org_return=_org(name="X"))
        agent = _agent(apollo=apollo, requirement=lambda s, o, u: "confirmed")

        agent._apollo_clarify([{"vendor_name": "X", "source_url": "https://x.com"}], _specs())

        apollo.org_enrich.assert_called_once_with("x.com")  # stale -> re-enriched


# ---------------------------------------------------------------------------
# Cache miss -> enrich -> write-back
# ---------------------------------------------------------------------------

class TestEnrichOnMiss:
    def test_miss_enriches_writes_back_and_annotates(self, isolated_registry):
        sr = isolated_registry
        apollo = _enabled_apollo(org_return=_org())
        agent = _agent(apollo=apollo, requirement=lambda s, o, u: "confirmed")

        cands = [{"vendor_name": "Mesco", "source_url": "https://mescocorp.com/contact"}]
        agent._apollo_clarify(cands, _specs())

        apollo.org_enrich.assert_called_once_with("mescocorp.com")
        # write-back so next time is a cache hit
        rec = sr.lookup_by_domain("mescocorp.com")
        assert rec is not None
        assert rec["suitability_status"] == "confirmed"
        assert rec["apollo_industry"] == "wholesale"
        assert rec["is_us_confirmed"] == 1
        assert rec["apollo_enriched_at"]
        # candidate annotated
        assert cands[0]["suitability_status"] == "confirmed"
        assert cands[0]["is_us_confirmed"] is True

    def test_second_pass_is_a_cache_hit(self, isolated_registry):
        """After a miss writes back, a second clarify must NOT call Apollo again."""
        apollo = _enabled_apollo(org_return=_org())
        agent = _agent(apollo=apollo, requirement=lambda s, o, u: "confirmed")

        agent._apollo_clarify([{"source_url": "https://mescocorp.com"}], _specs())
        assert apollo.org_enrich.call_count == 1
        agent._apollo_clarify([{"source_url": "https://mescocorp.com"}], _specs())
        assert apollo.org_enrich.call_count == 1  # still 1 — served from cache


# ---------------------------------------------------------------------------
# Annotate-don't-remove / miss / disabled / fail-soft
# ---------------------------------------------------------------------------

class TestAnnotateDontRemove:
    def test_apollo_miss_flags_not_removed(self, isolated_registry):
        sr = isolated_registry
        apollo = _enabled_apollo(org_return=None)  # no coverage
        agent = _agent(apollo=apollo)

        cands = [{"vendor_name": "X", "source_url": "https://x.com"}]
        out = agent._apollo_clarify(cands, _specs())

        assert cands[0]["suitability_status"] == "unconfirmed_flag_human"
        assert len(out) == 1  # not removed
        # real miss is cached so we don't re-pay
        assert sr.lookup_by_domain("x.com")["suitability_status"] == "unconfirmed_flag_human"

    def test_disabled_no_key_noops_no_store_write(self, isolated_registry):
        sr = isolated_registry
        # Force "no key" explicitly ("" => disabled) — the test process may have
        # APOLLO_API_KEY loaded from .env, so we can't rely on the env being empty.
        agent = SourcingAgent(apollo_api_key="")  # real client, disabled
        assert agent._apollo.enabled is False

        cands = [{"vendor_name": "X", "source_url": "https://x.com"}]
        out = agent._apollo_clarify(cands, _specs())

        assert cands[0]["suitability_status"] == "unconfirmed_flag_human"
        assert len(out) == 1
        assert sr.lookup_by_domain("x.com") is None  # store not poisoned -> later enrich still fires

    def test_apollo_error_failsoft_not_blocked(self, isolated_registry):
        apollo = _enabled_apollo(side_effect=RuntimeError("boom"))
        agent = _agent(apollo=apollo)

        cands = [{"vendor_name": "X", "source_url": "https://x.com"}]
        out = agent._apollo_clarify(cands, _specs())  # must not raise

        assert cands[0]["suitability_status"] == "unconfirmed_flag_human"
        assert len(out) == 1

    def test_count_unchanged_mixed_list_nothing_dropped(self, isolated_registry):
        apollo = _enabled_apollo(org_return=None)
        agent = _agent(apollo=apollo)
        cands = [
            {"vendor_name": "A", "source_url": "https://a.com"},
            {"vendor_name": "B", "source_url": None},          # seeded, no domain
            {"vendor_name": "C", "source_url": "https://c.com"},
        ]
        out = agent._apollo_clarify(cands, _specs())
        assert len(out) == 3 and len(cands) == 3
        assert cands[1].get("suitability_status") is None  # no-url candidate untouched
        # nothing carries rejection_reason (clarifier never excludes)
        assert all("rejection_reason" not in c for c in cands)


# ---------------------------------------------------------------------------
# Requirement-match (US check + LLM verdict mapping)
# ---------------------------------------------------------------------------

class TestRequirementMatch:
    def test_non_us_rejected_without_llm(self):
        agent = SourcingAgent(apollo_api_key=None)
        verdict = agent._requirement_match(_specs(), _org(country="Germany"), is_us=False)
        assert verdict == "rejected_unsuitable"

    def test_us_confirmed(self, monkeypatch):
        agent = SourcingAgent(apollo_api_key=None)
        monkeypatch.setattr(
            "utils.sourcing_archieved.llm_parsing._anthropic_complete",
            lambda system, user: "CONFIRMED",
        )
        assert agent._requirement_match(_specs(), _org(), is_us=True) == "confirmed"

    def test_us_rejected_by_llm(self, monkeypatch):
        agent = SourcingAgent(apollo_api_key=None)
        monkeypatch.setattr(
            "utils.sourcing_archieved.llm_parsing._anthropic_complete",
            lambda system, user: "REJECTED — clearly a software company",
        )
        assert agent._requirement_match(_specs(), _org(industry="software"), is_us=True) == "rejected_unsuitable"

    def test_ambiguous_llm_is_unconfirmed(self, monkeypatch):
        agent = SourcingAgent(apollo_api_key=None)
        monkeypatch.setattr(
            "utils.sourcing_archieved.llm_parsing._anthropic_complete",
            lambda system, user: "UNSURE",
        )
        assert agent._requirement_match(_specs(), _org(), is_us=True) == "unconfirmed_flag_human"

    def test_llm_error_failsoft(self, monkeypatch):
        agent = SourcingAgent(apollo_api_key=None)
        def boom(system, user):
            raise RuntimeError("llm down")
        monkeypatch.setattr("utils.sourcing_archieved.llm_parsing._anthropic_complete", boom)
        assert agent._requirement_match(_specs(), _org(), is_us=True) == "unconfirmed_flag_human"


# ---------------------------------------------------------------------------
# Wiring: clarifier runs in run() AFTER tier collection, NOT inside _run_tier3
# (so its latency can't trip the per-tier timeout and drop all of Tier 3).
# ---------------------------------------------------------------------------

class TestRunTier3Wiring:
    def test_run_tier3_does_not_clarify_inline(self, isolated_registry, monkeypatch):
        """_run_tier3 must NOT call the clarifier (it runs inside the 30s tier
        timeout future; clarifier latency there can zero out Tier 3)."""
        from utils.sourcing_archieved import enterprise_search

        agent = SourcingAgent(apollo_api_key="")
        monkeypatch.setattr(enterprise_search, "_discover_national_specialists", lambda specs, e: [])
        monkeypatch.setattr(enterprise_search, "_discover_aftermarket_specialists", lambda specs, n: [])
        monkeypatch.setattr(agent, "_seeded_tier3_candidates",
                            lambda specs: [{"vendor_name": "Seeded", "source_url": "https://seeded.com"}])

        calls = {"n": 0}
        monkeypatch.setattr(agent, "_apollo_clarify",
                            lambda cands, specs: calls.__setitem__("n", calls["n"] + 1) or cands)

        weights = {"price": 0.4, "speed": 0.35, "reliability": 0.25}
        out = agent._run_tier3(_specs(), weights, "unknown", tier2_count=5)

        assert calls["n"] == 0          # clarifier NOT invoked inside the timeout-wrapped tier
        assert len(out) == 1            # discovery result intact

    def test_run_clarifies_collected_tier3_after_timeout(self, isolated_registry, monkeypatch):
        """run() clarifies the COLLECTED tier_3 results (post-_collect), preserving them."""
        agent = SourcingAgent(apollo_api_key="")
        monkeypatch.setattr(agent, "_run_tier1", lambda specs, w: [])
        monkeypatch.setattr(agent, "_run_tier2", lambda specs, w: [])
        t3 = [{"vendor_name": "A", "source_url": "https://a.com"}]
        monkeypatch.setattr(agent, "_run_tier3", lambda specs, w, warranty, t2c: t3)

        seen = {}

        def spy(cands, specs):
            seen["cands"] = cands
            return cands
        monkeypatch.setattr(agent, "_apollo_clarify", spy)

        run = SourcingRun(
            id="wiring-test", facility_id="fac", initiated_by_user_id="t",
            initiated_at=datetime.now(timezone.utc), current_phase="sourcing",
            urgency_factor=0.5, warranty_status="unknown",
            asset_specs_json={"manufacturer": "Gusher Pumps", "model": "Type 21",
                              "part_number": "TYPE21", "voltage": "N/A",
                              "category": "Part", "detected_type": "mechanical seal"},
        )
        res = agent.run(run)

        # Clarifier ran in run() on the collected tier_3 results, which survive intact.
        assert seen.get("cands") is res["tier_3"]["results"]
        assert res["tier_3"]["count"] == 1


# ---------------------------------------------------------------------------
# Suitability reconciliation (asymmetric; removes nothing)
# ---------------------------------------------------------------------------

class TestReconcileSuitability:
    @staticmethod
    def _agent():
        return SourcingAgent(apollo_api_key="")

    # --- RESCUE (confirmed clears ONLY the floor reject) ---

    def test_confirmed_rescues_floor_reject_when_names_match(self):
        c = {"vendor_name": "Warfield Electric", "suitability_status": "confirmed",
             "rejection_reason": "suitability_below_floor", "suitability_score": 25.0,
             "is_us_confirmed": True, "apollo_org_name": "Warfield Electric Products Inc"}
        out = self._agent()._reconcile_suitability([c])
        assert not c.get("rejection_reason")  # floor reject cleared
        assert c.get("suitability_note") == "rescued_by_apollo_confirmed"
        assert c.get("apollo_name_match") is True  # persisted for reuse by ranking
        assert len(out) == 1

    def test_confirmed_rescue_withheld_on_name_mismatch(self):
        # J&D case: Apollo confirmed, but the verdict belongs to a different org.
        c = {"vendor_name": "J&D Manufacturing", "suitability_status": "confirmed",
             "rejection_reason": "suitability_below_floor", "apollo_org_name": "QC Supply"}
        out = self._agent()._reconcile_suitability([c])
        assert c["rejection_reason"] == "suitability_below_floor"  # NOT rescued
        assert c.get("suitability_note") == "rescue_withheld_name_mismatch"
        assert c.get("apollo_name_match") is False  # persisted for reuse by ranking
        assert "QC Supply" in c.get("apollo_flag", "")
        assert len(out) == 1  # not removed

    def test_confirmed_rescue_withheld_when_apollo_name_missing(self):
        c = {"vendor_name": "Warfield Electric", "suitability_status": "confirmed",
             "rejection_reason": "suitability_below_floor"}  # no apollo_org_name -> fail safe
        self._agent()._reconcile_suitability([c])
        assert c["rejection_reason"] == "suitability_below_floor"  # withheld
        assert c.get("suitability_note") == "rescue_withheld_name_mismatch"

    def test_confirmed_does_not_clear_other_rejection_types(self):
        for other in ("duplicate_in_higher_tier", "pn_mismatch", "in_warranty"):
            c = {"vendor_name": "X", "suitability_status": "confirmed", "rejection_reason": other}
            self._agent()._reconcile_suitability([c])
            assert c["rejection_reason"] == other  # untouched — only the floor reject is overridden
            assert "suitability_note" not in c

    def test_confirmed_without_floor_reject_is_noop(self):
        c = {"vendor_name": "X", "suitability_status": "confirmed", "rejection_reason": None}
        self._agent()._reconcile_suitability([c])
        assert not c.get("rejection_reason")
        assert "suitability_note" not in c

    # --- FLAG ONLY (rejected never drops / never sets rejection_reason) ---

    def test_rejected_flags_only_non_us(self):
        c = {"vendor_name": "Victor Seals", "suitability_status": "rejected_unsuitable",
             "is_us_confirmed": False, "apollo_country": "China", "apollo_industry": "machinery"}
        out = self._agent()._reconcile_suitability([c])
        assert not c.get("rejection_reason")  # this step did NOT reject it
        assert "non-US" in c.get("apollo_flag", "")
        assert "China" in c.get("apollo_flag", "")
        assert len(out) == 1  # not removed

    def test_rejected_flags_us_but_wrong_business(self):
        c = {"vendor_name": "Water Works Pools", "suitability_status": "rejected_unsuitable",
             "is_us_confirmed": True, "apollo_country": "United States", "apollo_industry": "recreation"}
        self._agent()._reconcile_suitability([c])
        assert "business mismatch" in c.get("apollo_flag", "")
        assert not c.get("rejection_reason")

    def test_rejected_does_not_clear_existing_floor_reject(self):
        # IBT: floor-rejected AND apollo-rejected -> stays floor-rejected, gains a flag.
        c = {"vendor_name": "IBT", "suitability_status": "rejected_unsuitable",
             "rejection_reason": "suitability_below_floor", "is_us_confirmed": False,
             "apollo_country": "Pakistan", "apollo_industry": "professional training & coaching"}
        self._agent()._reconcile_suitability([c])
        assert c["rejection_reason"] == "suitability_below_floor"  # not cleared (only confirmed rescues)
        assert "apollo_flag" in c

    # --- UNCONFIRMED (pass through; never rescues) ---

    def test_unconfirmed_flags_and_does_not_rescue(self):
        c = {"vendor_name": "Seal-It", "suitability_status": "unconfirmed_flag_human",
             "rejection_reason": "suitability_below_floor"}
        self._agent()._reconcile_suitability([c])
        assert c["rejection_reason"] == "suitability_below_floor"  # unconfirmed does NOT rescue
        assert "unconfirmed" in c.get("apollo_flag", "")

    # --- NO STATUS (seeded OEM etc.) ---

    def test_no_status_untouched(self):
        c = {"vendor_name": "Phoenix Pumps", "source_url": None,
             "rejection_reason": "duplicate_in_higher_tier"}
        self._agent()._reconcile_suitability([c])
        assert c["rejection_reason"] == "duplicate_in_higher_tier"
        assert "apollo_flag" not in c
        assert "suitability_note" not in c

    # --- INVARIANT: removes nothing ---

    def test_count_invariant_mixed_list(self):
        cands = [
            {"vendor_name": "rescue", "suitability_status": "confirmed",
             "rejection_reason": "suitability_below_floor", "apollo_org_name": "rescue"},
            {"vendor_name": "flag", "suitability_status": "rejected_unsuitable", "is_us_confirmed": False},
            {"vendor_name": "unconf", "suitability_status": "unconfirmed_flag_human"},
            {"vendor_name": "seeded", "source_url": None},
            {"vendor_name": "dup", "suitability_status": "confirmed",
             "rejection_reason": "duplicate_in_higher_tier"},
        ]
        out = self._agent()._reconcile_suitability(cands)
        assert len(out) == 5 and len(cands) == 5  # nothing removed
        # dedup reject preserved; floor reject on the confirmed one rescued
        assert cands[4]["rejection_reason"] == "duplicate_in_higher_tier"
        assert not cands[0].get("rejection_reason")


# ---------------------------------------------------------------------------
# Name-consistency check (gates the rescue)
# ---------------------------------------------------------------------------

class TestNamesPlausiblyMatch:
    def test_legal_suffix_variants_match(self):
        assert _names_plausibly_match("All Seals Inc", "All Seals Incorporated")
        assert _names_plausibly_match("Bay Power", "Bay Power Inc")

    def test_extra_words_subset_match(self):
        assert _names_plausibly_match("Warfield Electric", "Warfield Electric Products Inc")
        assert _names_plausibly_match("J&D", "J&D Manufacturing LLC")

    def test_despaced_match(self):
        # C1: same tokens, only internal spacing differs -> match.
        assert _names_plausibly_match("MROSupply", "MRO Supply")
        assert _names_plausibly_match("MRO Supply", "MROSupply")        # symmetric

    def test_despaced_does_not_wave_through_near_miss(self):
        # Guard the loosening: a genuinely-different org whose tokens ALMOST
        # concatenate alike (one char off) must still be rejected — despaced match
        # requires identical character composition, not fuzzy overlap.
        assert not _names_plausibly_match("MROSupply", "MRP Supply")

    def test_gross_mismatch(self):
        assert not _names_plausibly_match("J&D Manufacturing", "QC Supply")
        assert not _names_plausibly_match("IBT Industrial Solutions", "High Q Tower Training Institute")

    def test_empty_or_none_is_no_match(self):
        assert not _names_plausibly_match("", "All Seals")
        assert not _names_plausibly_match("All Seals", "")
        assert not _names_plausibly_match(None, "All Seals")
        assert not _names_plausibly_match("All Seals", None)


# ---------------------------------------------------------------------------
# Suitability-driven down-ranking + outreach selection (removes nothing)
# ---------------------------------------------------------------------------

class TestRankAndSelectTier3:
    @staticmethod
    def _agent():
        return SourcingAgent(apollo_api_key="")

    def test_bucket_assignment(self):
        cands = [
            {"vendor_name": "good", "suitability_status": "confirmed", "apollo_name_match": True},
            {"vendor_name": "bad", "suitability_status": "rejected_unsuitable",
             "apollo_name_match": True, "is_us_confirmed": False, "apollo_country": "China"},
            {"vendor_name": "unconf", "suitability_status": "unconfirmed_flag_human"},
            {"vendor_name": "seeded", "source_url": None},                       # no status
            {"vendor_name": "conf_mismatch", "suitability_status": "confirmed", "apollo_name_match": False},
            {"vendor_name": "rej_mismatch", "suitability_status": "rejected_unsuitable", "apollo_name_match": False},
        ]
        self._agent()._rank_and_select_tier3(cands)
        b = {c["vendor_name"]: c["suitability_rank_tier"] for c in cands}
        assert b["good"] == "top"
        assert b["bad"] == "bottom"
        assert b["unconf"] == "middle"
        assert b["seeded"] == "middle"
        assert b["conf_mismatch"] == "middle"   # confirmed but name-mismatch -> neutral, not top
        assert b["rej_mismatch"] == "middle"     # reject but name-mismatch -> neutral, not bottom

    def test_default_selection(self):
        top = {"vendor_name": "t", "suitability_status": "confirmed", "apollo_name_match": True}
        mid = {"vendor_name": "m", "suitability_status": "unconfirmed_flag_human"}
        mid_rejected = {"vendor_name": "mr", "suitability_status": "unconfirmed_flag_human",
                        "rejection_reason": "suitability_below_floor"}
        bottom = {"vendor_name": "b", "suitability_status": "rejected_unsuitable",
                  "apollo_name_match": True, "is_us_confirmed": False}
        self._agent()._rank_and_select_tier3([top, mid, mid_rejected, bottom])
        assert top["default_outreach_selected"] is True
        assert mid["default_outreach_selected"] is True
        assert mid_rejected["default_outreach_selected"] is False  # floor-rejected not pre-selected
        assert bottom["default_outreach_selected"] is False

    def test_requires_confirmation_bottom_only_with_reason(self):
        bottom_cn = {"vendor_name": "cn", "suitability_status": "rejected_unsuitable",
                     "apollo_name_match": True, "is_us_confirmed": False, "apollo_country": "China"}
        bottom_biz = {"vendor_name": "pool", "suitability_status": "rejected_unsuitable",
                      "apollo_name_match": True, "is_us_confirmed": True, "apollo_industry": "recreation"}
        top = {"vendor_name": "t", "suitability_status": "confirmed", "apollo_name_match": True}
        rej_mismatch = {"vendor_name": "mm", "suitability_status": "rejected_unsuitable", "apollo_name_match": False}
        self._agent()._rank_and_select_tier3([bottom_cn, bottom_biz, top, rej_mismatch])
        assert bottom_cn["requires_outreach_confirmation"] is True
        assert "non-US" in bottom_cn["outreach_confirmation_reason"]
        assert "China" in bottom_cn["outreach_confirmation_reason"]
        assert bottom_biz["requires_outreach_confirmation"] is True
        assert "business mismatch" in bottom_biz["outreach_confirmation_reason"]
        assert top["requires_outreach_confirmation"] is False
        assert rej_mismatch["requires_outreach_confirmation"] is False  # untrusted reject not gated

    def test_name_mismatch_reject_neutral_not_penalized(self):
        c = {"vendor_name": "x", "suitability_status": "rejected_unsuitable",
             "apollo_name_match": False, "apollo_flag": "apollo: non-US ... (review)"}
        self._agent()._rank_and_select_tier3([c])
        assert c["suitability_rank_tier"] == "middle"
        assert c["default_outreach_selected"] is True
        assert c["requires_outreach_confirmation"] is False
        assert c["apollo_flag"]  # flag preserved for visibility

    def test_stable_sort_order_and_count_unchanged(self):
        cands = [
            {"vendor_name": "b1", "suitability_status": "rejected_unsuitable", "apollo_name_match": True, "is_us_confirmed": False},
            {"vendor_name": "t1", "suitability_status": "confirmed", "apollo_name_match": True},
            {"vendor_name": "m1", "suitability_status": "unconfirmed_flag_human"},
            {"vendor_name": "t2", "suitability_status": "confirmed", "apollo_name_match": True},
            {"vendor_name": "m2", "suitability_status": "unconfirmed_flag_human"},
            {"vendor_name": "b2", "suitability_status": "rejected_unsuitable", "apollo_name_match": True, "is_us_confirmed": False},
        ]
        out = self._agent()._rank_and_select_tier3(cands)
        # buckets ordered TOP->MIDDLE->BOTTOM; input order preserved within each bucket
        assert [c["vendor_name"] for c in out] == ["t1", "t2", "m1", "m2", "b1", "b2"]
        assert len(out) == 6  # removes nothing


# ---------------------------------------------------------------------------
# Contact resolution — free path (store -> generic inbox -> human-flag)
# ---------------------------------------------------------------------------

class TestResolveContact:
    @staticmethod
    def _agent():
        a = SourcingAgent(apollo_api_key="")
        a._apollo = MagicMock()  # so we can assert it is never called during resolution
        return a

    def test_generic_inbox_is_default(self, isolated_registry):
        sr = isolated_registry
        agent = self._agent()
        c = {"vendor_name": "Mesco", "source_url": "https://www.mescocorp.com/x",
             "default_outreach_selected": True}
        out = agent._resolve_contact([c])
        assert c["resolved_contact_email"] == "sales@mescocorp.com"
        assert c["contact_method"] == "generic_inbox"
        assert c["contact_status"] == "resolved"
        assert c["contact_email_fallback"] == "info@mescocorp.com"
        # written back to the store
        rec = sr.lookup_by_domain("mescocorp.com")
        assert rec["contact_email"] == "sales@mescocorp.com"
        assert rec["contact_method"] == "generic_inbox"
        assert rec["contact_resolved_at"]
        # zero Apollo calls
        agent._apollo.org_enrich.assert_not_called()
        agent._apollo.people_search.assert_not_called()
        assert len(out) == 1

    def test_store_reuse(self, isolated_registry):
        sr = isolated_registry
        sr.upsert_contact("mescocorp.com", {"contact_email": "buyer@mescocorp.com",
                                            "contact_method": "generic_inbox", "contact_status": "resolved"})
        agent = self._agent()
        c = {"vendor_name": "Mesco", "source_url": "https://mescocorp.com", "default_outreach_selected": True}
        agent._resolve_contact([c])
        assert c["resolved_contact_email"] == "buyer@mescocorp.com"
        assert c["contact_method"] == "store"     # reused, not reconstructed
        assert c["contact_status"] == "resolved"

    def test_human_flag_when_no_domain(self, isolated_registry):
        agent = self._agent()
        c = {"vendor_name": "Phoenix Pumps", "source_url": None, "default_outreach_selected": True}
        agent._resolve_contact([c])
        assert c["resolved_contact_email"] is None
        assert c["contact_method"] == "human_flag"
        assert c["contact_status"] == "needs_human"

    def test_not_selected_is_skipped(self, isolated_registry):
        agent = self._agent()
        c = {"vendor_name": "X", "source_url": "https://x.com", "default_outreach_selected": False}
        agent._resolve_contact([c])
        assert "resolved_contact_email" not in c
        assert "contact_method" not in c

    def test_bounce_clears_then_reresolves(self, isolated_registry):
        sr = isolated_registry
        sr.upsert_contact("x.com", {"contact_email": "sales@x.com",
                                    "contact_method": "generic_inbox", "contact_status": "resolved"})
        assert sr.mark_contact_bounced("x.com") is True
        assert sr.lookup_by_domain("x.com")["contact_email"] is None

        agent = self._agent()
        c = {"vendor_name": "X", "source_url": "https://x.com", "default_outreach_selected": True}
        agent._resolve_contact([c])
        # did NOT reuse the bounced store entry — fell through to re-construct the inbox
        assert c["contact_method"] == "generic_inbox"
        assert c["resolved_contact_email"] == "sales@x.com"
        rec = sr.lookup_by_domain("x.com")
        assert rec["contact_status"] == "resolved"
        assert rec["contact_email"] == "sales@x.com"

    def test_count_invariant_and_no_apollo(self, isolated_registry):
        agent = self._agent()
        cands = [
            {"vendor_name": "a", "source_url": "https://a.com", "default_outreach_selected": True},
            {"vendor_name": "b", "source_url": None, "default_outreach_selected": True},
            {"vendor_name": "c", "source_url": "https://c.com", "default_outreach_selected": False},
        ]
        out = agent._resolve_contact(cands)
        assert len(out) == 3 and len(cands) == 3
        agent._apollo.org_enrich.assert_not_called()
        agent._apollo.people_search.assert_not_called()


# ---------------------------------------------------------------------------
# Named-contact escalation — pick heuristic
# ---------------------------------------------------------------------------

class TestPickSalesContact:
    def test_is_sales_title(self):
        assert _is_sales_title("Director of Sales")
        assert _is_sales_title("Account Executive")
        assert not _is_sales_title("Operations Manager")
        assert not _is_sales_title(None)

    def test_picks_highest_seniority_sales(self):
        people = [
            {"title": "Sales Rep", "seniority": "entry", "name": "a"},
            {"title": "VP of Sales", "seniority": "vp", "name": "b"},
            {"title": "Sales Manager", "seniority": "manager", "name": "c"},
        ]
        assert _pick_sales_contact(people)["name"] == "b"  # vp most senior

    def test_ignores_non_sales(self):
        people = [{"title": "Operations Manager", "seniority": "director", "name": "ops"},
                  {"title": "Account Executive", "seniority": "entry", "name": "ae"}]
        assert _pick_sales_contact(people)["name"] == "ae"  # only sales-titled considered

    def test_no_sales_returns_none(self):
        assert _pick_sales_contact([{"title": "Engineer"}]) is None
        assert _pick_sales_contact([]) is None
        assert _pick_sales_contact(None) is None

    def test_tiebreak_is_stable(self):
        people = [{"title": "Sales", "seniority": "manager", "name": "first"},
                  {"title": "Sales", "seniority": "manager", "name": "second"}]
        assert _pick_sales_contact(people)["name"] == "first"


# ---------------------------------------------------------------------------
# Named-contact escalation — triggered only, credit-gated, fail-soft
# ---------------------------------------------------------------------------

class TestEscalateContact:
    @staticmethod
    def _agent(apollo):
        a = SourcingAgent(apollo_api_key="")
        a._apollo = apollo
        return a

    @staticmethod
    def _sales_people():
        return [
            {"person_id": "p1", "name": "Sam Rep", "first_name": "Sam", "last_name": "Rep",
             "title": "Sales Representative", "seniority": "entry"},
            {"person_id": "p2", "name": "Dana Dir", "first_name": "Dana", "last_name": "Dir",
             "title": "Director of Sales", "seniority": "director"},
            {"person_id": "p3", "name": "Pat Ops", "title": "Operations Manager", "seniority": "manager"},
        ]

    def test_happy_path_one_enrich_stores_primary(self, isolated_registry):
        sr = isolated_registry
        apollo = MagicMock()
        apollo.people_search.return_value = self._sales_people()
        apollo.people_match.return_value = {"name": "Dana Dir", "title": "Director of Sales",
                                            "email": "dana@x.com", "email_status": "verified", "person_id": "p2"}
        agent = self._agent(apollo)
        c = {"vendor_name": "X", "source_url": "https://x.com"}
        agent._escalate_contact(c)

        apollo.people_match.assert_called_once()  # exactly ONE enrich
        assert apollo.people_match.call_args.kwargs["person_id"] == "p2"  # highest-seniority sales
        assert c["primary_contact_email"] == "dana@x.com"
        assert c["primary_contact_status"] == "resolved"
        assert c["primary_contact_source"] == "apollo_enriched"
        assert c["primary_contact_person_id"] == "p2"
        rec = sr.lookup_by_domain("x.com")
        assert rec["primary_contact_email"] == "dana@x.com"
        assert rec["primary_contact_status"] == "resolved"
        assert rec["primary_contact_person_id"] == "p2"

    def test_cache_hard_reuses_primary_zero_apollo(self, isolated_registry):
        sr = isolated_registry
        sr.upsert_primary_contact("x.com", {"primary_contact_email": "jane@x.com",
                                            "primary_contact_name": "Jane",
                                            "primary_contact_source": "apollo_enriched",
                                            "primary_contact_status": "resolved"})
        apollo = MagicMock()
        agent = self._agent(apollo)
        c = {"vendor_name": "X", "source_url": "https://x.com"}
        agent._escalate_contact(c)
        apollo.people_search.assert_not_called()
        apollo.people_match.assert_not_called()
        assert c["primary_contact_email"] == "jane@x.com"

    def test_no_sales_person_no_enrich(self, isolated_registry):
        apollo = MagicMock()
        apollo.people_search.return_value = [{"person_id": "p", "title": "Operations Manager", "seniority": "manager"}]
        agent = self._agent(apollo)
        c = {"vendor_name": "X", "source_url": "https://x.com"}
        agent._escalate_contact(c)
        apollo.people_match.assert_not_called()
        assert c["primary_contact_status"] == "none"
        assert c["primary_contact_email"] is None

    def test_search_empty_no_primary(self, isolated_registry):
        apollo = MagicMock()
        apollo.people_search.return_value = []
        agent = self._agent(apollo)
        c = {"vendor_name": "X", "source_url": "https://x.com"}
        agent._escalate_contact(c)
        apollo.people_match.assert_not_called()
        assert c["primary_contact_status"] == "none"

    def test_enrich_miss_persists_found_no_email(self, isolated_registry):
        """Search-hit + enrich-miss: keep WHO we found (name/title/person_id) under a
        distinct 'found_no_email' status — but no email, and the generic fallback is
        unchanged (found_no_email is NOT resolved)."""
        sr = isolated_registry
        apollo = MagicMock()
        apollo.people_search.return_value = self._sales_people()
        apollo.people_match.return_value = None
        agent = self._agent(apollo)
        c = {"vendor_name": "X", "source_url": "https://x.com"}
        agent._escalate_contact(c)

        apollo.people_match.assert_called_once()  # tried exactly once, then fail-soft
        assert c["primary_contact_status"] == "found_no_email"
        assert c["primary_contact_email"] is None
        assert c["primary_contact_name"] == "Dana Dir"          # highest-seniority sales
        assert c["primary_contact_title"] == "Director of Sales"
        assert c["primary_contact_person_id"] == "p2"
        assert c["primary_contact_source"] == "apollo_search"
        # Persisted to the store, and still NOT the effective contact.
        rec = sr.lookup_by_domain("x.com")
        assert rec["primary_contact_status"] == "found_no_email"
        assert rec["primary_contact_name"] == "Dana Dir"
        assert rec["primary_contact_person_id"] == "p2"
        assert rec["primary_contact_email"] is None
        assert sr.effective_contact(rec)["source"] != "primary"  # fallback unchanged

    def test_three_states_are_distinct(self, isolated_registry):
        """resolved (email) vs found_no_email (person, no email) vs none (no person)."""
        # resolved
        a1 = MagicMock()
        a1.people_search.return_value = self._sales_people()
        a1.people_match.return_value = {"name": "Dana Dir", "title": "Director of Sales",
                                        "email": "dana@a.com", "person_id": "p2"}
        c1 = {"vendor_name": "A", "source_url": "https://a.com"}
        self._agent(a1)._escalate_contact(c1)
        # found_no_email
        a2 = MagicMock()
        a2.people_search.return_value = self._sales_people()
        a2.people_match.return_value = None
        c2 = {"vendor_name": "B", "source_url": "https://b.com"}
        self._agent(a2)._escalate_contact(c2)
        # none
        a3 = MagicMock()
        a3.people_search.return_value = [{"title": "Operations Manager", "seniority": "manager"}]
        c3 = {"vendor_name": "C", "source_url": "https://c.com"}
        self._agent(a3)._escalate_contact(c3)

        assert c1["primary_contact_status"] == "resolved"
        assert c2["primary_contact_status"] == "found_no_email"
        assert c3["primary_contact_status"] == "none"

    def test_no_domain_no_primary_no_calls(self, isolated_registry):
        apollo = MagicMock()
        agent = self._agent(apollo)
        c = {"vendor_name": "Seeded", "source_url": None}
        agent._escalate_contact(c)
        apollo.people_search.assert_not_called()
        assert c["primary_contact_status"] == "none"

    def test_disabled_apollo_noops(self, isolated_registry):
        agent = SourcingAgent(apollo_api_key="")  # real, disabled -> people_search returns []
        c = {"vendor_name": "X", "source_url": "https://x.com"}
        agent._escalate_contact(c)
        assert c["primary_contact_status"] == "none"

    def test_default_resolve_makes_zero_people_calls(self, isolated_registry):
        apollo = MagicMock()
        agent = self._agent(apollo)
        cands = [{"vendor_name": "X", "source_url": "https://x.com", "default_outreach_selected": True}]
        agent._resolve_contact(cands)
        apollo.people_search.assert_not_called()
        apollo.people_match.assert_not_called()
