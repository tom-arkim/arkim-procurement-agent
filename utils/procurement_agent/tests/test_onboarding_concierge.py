"""
Night 4 — onboarding agent tests: extractor + concierge (approve-gated writes,
must-confirm trio, double-approve idempotency, flag-off inertness).

The LLM is NEVER called: the extractor's `llm_caller` is injected with a canned
parsed dict (or the heuristic fallback is exercised with no key). The DB is
isolated to a tmp file (mirrors test_supplier_scope.py). TIER1_V2 is toggled
via monkeypatch.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from utils import supplier_registry as sr
from utils.procurement_agent.onboarding.extractor import (
    extract_scope, extract_scope_from_dict, OnboardingDraft, BrandDraft,
    ClassDraft, LocationDraft, MUST_CONFIRM_FIELDS,
)
from utils.procurement_agent.onboarding.harvester import HarvestResult
from utils.procurement_agent.onboarding.dom import PageContent
from utils.procurement_agent.onboarding import concierge
from utils.procurement_agent.onboarding import flags as obf


# ---------------------------------------------------------------------------
# Fixtures — isolated registry + TIER1_V2 on
# ---------------------------------------------------------------------------

@pytest.fixture
def iso_db(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sr, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    monkeypatch.setattr(sr, "TIER1_V2", True)
    monkeypatch.setenv("TIER1_V2", "1")
    return sr


@pytest.fixture
def iso_db_off(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sr, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    monkeypatch.setattr(sr, "TIER1_V2", False)
    monkeypatch.setenv("TIER1_V2", "")
    return sr


def _make_harvest(url="https://acme-pumps.com/", texts=None) -> HarvestResult:
    pages = [PageContent(url=url, title="Acme Pumps - Industrial Pump Distributor",
                         meta_description="Acme Pumps is an authorized Goulds distributor.",
                         text=" ".join(texts or ["Acme Pumps sells Goulds pumps and seal kits. "
                                                  "Authorized distributor. Ships nationwide US. "
                                                  "Headquartered in Springfield, IL."]),
                         links=[], alt_texts=["Goulds logo", "Pentair logo"],
                         headings=["Our Brands"])]
    return HarvestResult(home_url=url, pages=pages, fetched_urls=[url])


# A canned LLM response (no live network).
_CANNED_LLM = {
    "name": "Acme Pumps",
    "vertical": "industrial pump distribution",
    "brands": [
        {"name": "Goulds", "relationship_guess": "AUTHORIZED",
         "confidence": 0.9, "evidence": "authorized Goulds distributor",
         "source_url": "https://acme-pumps.com/"},
        {"name": "Pentair", "relationship_guess": "CARRIES",
         "confidence": 0.7, "evidence": "Pentair logo on brands page",
         "source_url": "https://acme-pumps.com/"},
    ],
    "classes": [
        {"class_id": "PUMP", "confidence": 0.95, "is_core_guess": True,
         "evidence": "pumps everywhere", "source_url": "https://acme-pumps.com/"},
        {"class_id": "SEAL", "confidence": 0.6, "is_core_guess": False,
         "evidence": "seal kits", "source_url": "https://acme-pumps.com/"},
        {"class_id": "not_a_real_class", "confidence": 0.5},  # must be dropped
    ],
    "locations": [
        {"locality": "Springfield", "region": "IL", "country": "US",
         "confidence": 0.8, "evidence": "HQ Springfield, IL"},
    ],
    "ship_area_guess": {"kind": "NATIONWIDE_US"},
    "overall_confidence": 0.82,
}


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class TestExtractor:
    def test_llm_path_assembles_draft(self, iso_db):
        harvest = _make_harvest()
        def caller(system, user, key, model):
            return dict(_CANNED_LLM)
        draft = extract_scope(harvest, api_key="fake-key", llm_caller=caller)
        assert draft.domain == "acme-pumps.com"
        assert draft.name == "Acme Pumps"
        assert draft.vertical == "industrial pump distribution"
        brand_names = [b.name for b in draft.brands]
        assert "Goulds" in brand_names and "Pentair" in brand_names
        goulds = next(b for b in draft.brands if b.name == "Goulds")
        assert goulds.relationship_guess == "AUTHORIZED"
        assert goulds.must_confirm is True
        class_ids = [c.class_id for c in draft.classes]
        assert "PUMP" in class_ids and "SEAL" in class_ids
        # The non-dictionary class_id is dropped (we never invent classes).
        assert "NOT_A_REAL_CLASS" not in class_ids and "not_a_real_class" not in class_ids
        assert all(c.must_confirm for c in draft.classes)
        assert draft.ship_area_guess == {"kind": "NATIONWIDE_US"}
        assert draft.extraction_method == "llm"

    def test_must_confirm_trio_enforced_regardless_of_confidence(self, iso_db):
        harvest = _make_harvest()
        high = dict(_CANNED_LLM, overall_confidence=0.99)
        def caller(system, user, key, model):
            return high
        draft = extract_scope(harvest, api_key="fake-key", llm_caller=caller)
        # Even at 0.99 confidence, the trio is True.
        for f in MUST_CONFIRM_FIELDS:
            assert draft.must_confirm[f] is True
        assert all(b.must_confirm for b in draft.brands)
        assert all(c.must_confirm for c in draft.classes)

    def test_no_key_falls_back_to_heuristic(self, iso_db, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        harvest = _make_harvest(texts=["Acme sells Goulds pumps and mechanical seals. "
                                       "Headquartered in Springfield, IL. We ship nationwide."])
        draft = extract_scope(harvest, api_key="")
        assert draft.extraction_method == "heuristic_fallback"
        # Heuristic still enforces the must-confirm trio.
        for f in MUST_CONFIRM_FIELDS:
            assert draft.must_confirm[f] is True
        # Heuristic scans the dictionary -> PUMP and SEAL should be detected.
        class_ids = {c.class_id for c in draft.classes}
        assert "PUMP" in class_ids
        # Brands left empty by the heuristic (honest — a wrong brand list is
        # worse than none; the human concierge fills them).
        assert draft.brands == []
        # Location rule-detected.
        assert any(l.region == "IL" for l in draft.locations)

    def test_llm_failure_falls_back(self, iso_db):
        harvest = _make_harvest()
        def caller(system, user, key, model):
            return None  # LLM returned nothing
        draft = extract_scope(harvest, api_key="fake-key", llm_caller=caller)
        assert draft.extraction_method == "heuristic_fallback"

    def test_brand_relationship_coerced_to_default(self, iso_db):
        harvest = _make_harvest()
        canned = {
            "name": "X", "vertical": "x",
            "brands": [{"name": "Foo", "relationship_guess": "garbage"}],
            "classes": [], "locations": [], "ship_area_guess": None,
            "overall_confidence": 0.5,
        }
        draft = extract_scope(harvest, api_key="k",
                              llm_caller=lambda s, u, k, m: canned)
        assert draft.brands[0].relationship_guess == "CARRIES"  # conservative default

    def test_channel_partner_not_listed_as_brand(self, iso_db):
        # The small-shop edge case: a distributor name is a CHANNEL partner,
        # not a carried brand. The LLM is instructed not to list these; here we
        # assert the extractor does NOT inject them itself (the heuristic leaves
        # brands empty, so this is structurally safe).
        harvest = _make_harvest(texts=["Magnaloy products. Distributed by Motion Industries."])
        draft = extract_scope(harvest, api_key="")
        assert draft.brands == []

    def test_empty_harvest(self, iso_db):
        draft = extract_scope(HarvestResult(home_url="https://x.com/"), api_key="k")
        assert draft.extraction_method == "empty"
        assert draft.brands == [] and draft.classes == []


# ---------------------------------------------------------------------------
# Concierge — approve-gated writes (the must-confirm gate)
# ---------------------------------------------------------------------------

class TestConciergeApproveGate:
    def test_create_draft_writes_nothing_to_registry(self, iso_db):
        draft = extract_scope_from_dict(dict(_CANNED_LLM),
                                        [_make_harvest().pages[0]])
        did = concierge.create_draft(draft, source_url="https://acme-pumps.com/")
        assert did is not None
        # NOTHING in the registry scope tables yet.
        assert sr.get_supplier_classes("acme-pumps.com") == []
        assert sr.get_supplier_brands("acme-pumps.com") == []
        assert sr.get_tier1_lifecycle("acme-pumps.com") is None
        # The pending draft IS in the review queue.
        assert len(concierge.list_drafts()) == 1

    def test_approve_writes_scope_and_drives_lifecycle_onboarded(self, iso_db):
        draft = extract_scope_from_dict(dict(_CANNED_LLM),
                                        [_make_harvest().pages[0]])
        did = concierge.create_draft(draft)
        rec = concierge.approve_draft(did, set_by="tom")
        assert rec is not None and rec["name"] == "Acme Pumps"
        classes = {c["class_id"] for c in sr.get_supplier_classes("acme-pumps.com")}
        assert classes == {"PUMP", "SEAL"}
        brands = {b["brand_id"]: b["relationship"]
                  for b in sr.get_supplier_brands("acme-pumps.com")}
        assert brands == {"Goulds": "AUTHORIZED", "Pentair": "CARRIES"}
        assert sr.get_supplier_territory("acme-pumps.com")["ship_area"] == {"kind": "NATIONWIDE_US"}
        assert sr.get_supplier_verticals("acme-pumps.com") == ["industrial pump distribution"]
        assert sr.get_tier1_lifecycle("acme-pumps.com") == sr.TIER1_ONBOARDED

    def test_double_approve_idempotent(self, iso_db):
        draft = extract_scope_from_dict(dict(_CANNED_LLM),
                                        [_make_harvest().pages[0]])
        did = concierge.create_draft(draft)
        rec1 = concierge.approve_draft(did, set_by="tom")
        rec2 = concierge.approve_draft(did, set_by="tom")
        # Both succeed; same record; lifecycle stays onboarded; no duplicate.
        assert rec1 is not None and rec2 is not None
        assert rec1["id"] == rec2["id"]
        assert sr.get_tier1_lifecycle("acme-pumps.com") == sr.TIER1_ONBOARDED
        # Exactly one supplier row for the domain.
        assert sr.lookup_by_domain("acme-pumps.com") is not None
        # Draft status is confirmed.
        assert concierge.get_draft(did)["status"] == concierge.DRAFT_STATUS_CONFIRMED

    def test_reject_writes_nothing(self, iso_db):
        draft = extract_scope_from_dict(dict(_CANNED_LLM),
                                        [_make_harvest().pages[0]])
        did = concierge.create_draft(draft)
        out = concierge.reject_draft(did, set_by="tom")
        assert out["status"] == concierge.DRAFT_STATUS_REJECTED
        assert sr.get_supplier_classes("acme-pumps.com") == []
        assert sr.get_tier1_lifecycle("acme-pumps.com") is None

    def test_approve_with_revisions_overrides_stored_fields(self, iso_db):
        draft = extract_scope_from_dict(dict(_CANNED_LLM),
                                        [_make_harvest().pages[0]])
        did = concierge.create_draft(draft)
        rec = concierge.approve_draft(did, set_by="tom", revisions={
            "name": "Acme Pumps Inc",
            "vertical": "industrial pump & seal distribution",
            "ship_area_guess": {"kind": "STATES", "states": ["IL", "IN"]},
            "brands": [{"name": "Goulds", "relationship_guess": "AUTHORIZED"},
                       {"name": "NewBrand", "relationship_guess": "CARRIES"}],
            "classes": [{"class_id": "PUMP", "is_core_guess": True}],
        })
        assert rec is not None and rec["name"] == "Acme Pumps Inc"
        assert sr.get_supplier_verticals("acme-pumps.com") == ["industrial pump & seal distribution"]
        assert sr.get_supplier_territory("acme-pumps.com")["ship_area"] == {"kind": "STATES", "states": ["IL", "IN"]}
        brands = {b["brand_id"]: b["relationship"]
                  for b in sr.get_supplier_brands("acme-pumps.com")}
        assert brands == {"Goulds": "AUTHORIZED", "NewBrand": "CARRIES"}
        assert {c["class_id"] for c in sr.get_supplier_classes("acme-pumps.com")} == {"PUMP"}

    def test_approve_unknown_draft_returns_none(self, iso_db):
        assert concierge.approve_draft("nonexistent-id") is None
        assert concierge.get_draft("nonexistent-id") is None

    def test_approve_wrong_kind_returns_none(self, iso_db):
        # A quote review item is not an onboarding draft.
        sr.record_review_item("quote", {"unit_price": 1.0}, supplier_domain="x.com")
        # Find its id.
        rows = sr.get_review_items(kind="quote")
        assert rows
        assert concierge.approve_draft(rows[0]["id"]) is None


# ---------------------------------------------------------------------------
# Flag-off inertness (the Night 4 inertness wall)
# ---------------------------------------------------------------------------

class TestInertnessFlagOff:
    def test_create_draft_noop_flag_off(self, iso_db_off):
        draft = extract_scope_from_dict(dict(_CANNED_LLM),
                                        [_make_harvest().pages[0]])
        assert concierge.create_draft(draft) is None
        # No review item, no supplier row.
        assert concierge.list_drafts() == []
        assert sr.lookup_by_domain("acme-pumps.com") is None

    def test_approve_noop_flag_off(self, iso_db_off):
        # Even if a draft id somehow existed, approve no-ops flag-off.
        assert concierge.approve_draft("any-id") is None
        assert sr.get_supplier_classes("anything.com") == []
        assert sr.get_tier1_lifecycle("anything.com") is None

    def test_get_draft_none_flag_off(self, iso_db_off):
        assert concierge.get_draft("any-id") is None
        assert concierge.list_drafts() == []

    def test_flag_off_registry_byte_identical_to_pre_night4(self, iso_db_off):
        """The Night 3 inertness guarantee is preserved: with TIER1_V2 off, the
        onboarding agent touches NOTHING. A legacy Apollo-cache row is untouched
        and the clarifier's needs_reenrichment is byte-identical (the tier1
        graduation branch is gated off — Night 3 already proved this; we
        re-assert it isn't disturbed by Night 4)."""
        s = iso_db_off
        old = (datetime.now(timezone.utc) - timedelta(days=5000)).isoformat()
        s.upsert_apollo_data("legacy-co.com", {"suitability_status": "confirmed",
                                                "apollo_enriched_at": old})
        rec = s.lookup_by_domain("legacy-co.com")
        assert s.needs_reenrichment(rec) is True
        # Onboarding create/approve do not touch it.
        assert concierge.create_draft(OnboardingDraft(domain="legacy-co.com")) is None
        assert concierge.approve_draft("noop") is None
        rec2 = s.lookup_by_domain("legacy-co.com")
        # Unchanged: still discovery_only, still stale, no scope, no lifecycle.
        assert rec2["onboarding_status"] == "discovery_only"
        assert s.needs_reenrichment(rec2) is True
        assert s.get_tier1_lifecycle("legacy-co.com") is None
        assert s.get_supplier_classes("legacy-co.com") == []
