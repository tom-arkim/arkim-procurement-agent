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
from utils.procurement_agent.agents.sourcing_agent import SourcingAgent


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
        old = (datetime.utcnow() - timedelta(days=5000)).isoformat()
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
        old = (datetime.utcnow() - timedelta(days=200)).isoformat()
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

    def test_confirmed_rescues_floor_reject(self):
        c = {"vendor_name": "Warfield Electric", "suitability_status": "confirmed",
             "rejection_reason": "suitability_below_floor", "suitability_score": 25.0,
             "is_us_confirmed": True}
        out = self._agent()._reconcile_suitability([c])
        assert not c.get("rejection_reason")  # floor reject cleared
        assert c.get("suitability_note") == "rescued_by_apollo_confirmed"
        assert len(out) == 1

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
             "rejection_reason": "suitability_below_floor"},
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
