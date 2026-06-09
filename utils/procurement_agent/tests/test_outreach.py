"""
Tests for multi-vendor outreach campaign (Issue 3 — Phase 3 UI feature).

Validates that initiate_outreach_campaign() correctly captures all selected
vendors in the audit log and returns well-formed draft emails.
"""

import pytest
from utils.procurement_agent.outreach import (
    initiate_outreach_campaign, _make_draft, should_request_contact,
    CONTACT_NOMINATION_ASK,
)
from utils.audit_log import recent_entries

import uuid

_ASK_MARKER = "name, position, and email"  # distinctive phrase from the nomination ask


def _run_id() -> str:
    return str(uuid.uuid4())


def _vendor(name: str, price_tbd: bool = True) -> dict:
    return {
        "vendor_name":   name,
        "base_price":    0.0 if price_tbd else 500.0,
        "price_tbd":     price_tbd,
        "lead_time_days": 7,
        "source_url":    f"https://{name.lower().replace(' ', '')}.com",
    }


class TestOutreachCampaignAuditCapture:
    def test_single_vendor_captured_in_audit_log(self):
        run_id   = _run_id()
        vendors  = [_vendor("Phoenix Pumps")]
        campaign = initiate_outreach_campaign(run_id, vendors)

        entries    = recent_entries(limit=50)
        run_entries = [e for e in entries if e.get("sourcing_run_id") == run_id]
        assert len(run_entries) == 1
        assert "Phoenix Pumps" in run_entries[0]["input_summary"]

    def test_three_vendors_all_captured_in_audit_log(self):
        run_id  = _run_id()
        vendors = [
            _vendor("Phoenix Pumps"),
            _vendor("Anderson Process"),
            _vendor("OTC Industrial"),
        ]
        campaign = initiate_outreach_campaign(run_id, vendors)

        entries     = recent_entries(limit=50)
        run_entries = [e for e in entries if e.get("sourcing_run_id") == run_id]
        assert len(run_entries) == 1
        summary = run_entries[0]["input_summary"]
        assert "Phoenix Pumps" in summary
        assert "Anderson Process" in summary
        assert "OTC Industrial" in summary

    def test_audit_log_workflow_mode_is_multi_vendor_outreach(self):
        run_id   = _run_id()
        campaign = initiate_outreach_campaign(run_id, [_vendor("Carotek")])

        entries     = recent_entries(limit=50)
        run_entries = [e for e in entries if e.get("sourcing_run_id") == run_id]
        assert run_entries[0]["workflow_mode"] == "multi_vendor_outreach"

    def test_audit_log_user_selection_contains_all_vendor_names(self):
        run_id  = _run_id()
        vendors = [_vendor("Crane Engineering"), _vendor("Tencarva Machinery")]
        initiate_outreach_campaign(run_id, vendors)

        entries     = recent_entries(limit=50)
        run_entries = [e for e in entries if e.get("sourcing_run_id") == run_id]
        selection = run_entries[0]["user_selection"] or ""
        assert "Crane Engineering" in selection
        assert "Tencarva Machinery" in selection


class TestOutreachCampaignReturn:
    def test_returns_all_vendor_names(self):
        vendors  = [_vendor("National Seal"), _vendor("Gulf Coast Motor")]
        campaign = initiate_outreach_campaign(_run_id(), vendors)
        assert "National Seal" in campaign["vendors"]
        assert "Gulf Coast Motor" in campaign["vendors"]
        assert len(campaign["vendors"]) == 2

    def test_returns_draft_for_each_vendor(self):
        vendors  = [_vendor("Phoenix Pumps"), _vendor("Anderson Process")]
        campaign = initiate_outreach_campaign(_run_id(), vendors)
        assert "Phoenix Pumps" in campaign["drafts"]
        assert "Anderson Process" in campaign["drafts"]

    def test_draft_contains_vendor_name(self):
        vendors  = [_vendor("Carotek")]
        campaign = initiate_outreach_campaign(_run_id(), vendors)
        assert "Carotek" in campaign["drafts"]["Carotek"]

    def test_draft_contains_specs_when_provided(self):
        specs   = {"manufacturer": "Endress+Hauser", "model": "PMC11", "part_number": "PMC11-AA1"}
        vendors = [_vendor("Eastern Controls")]
        campaign = initiate_outreach_campaign(_run_id(), vendors, specs=specs)
        draft = campaign["drafts"]["Eastern Controls"]
        assert "Endress+Hauser" in draft or "PMC11" in draft

    def test_email_send_enabled_is_false(self):
        campaign = initiate_outreach_campaign(_run_id(), [_vendor("Test Vendor")])
        assert campaign["email_send_enabled"] is False

    def test_empty_vendor_list_still_writes_audit_log(self):
        run_id   = _run_id()
        campaign = initiate_outreach_campaign(run_id, [])
        entries     = recent_entries(limit=50)
        run_entries = [e for e in entries if e.get("sourcing_run_id") == run_id]
        assert len(run_entries) == 1
        assert campaign["vendors"] == []
        assert campaign["drafts"] == {}


class TestContactNominationAsk:
    def test_make_draft_includes_ask_when_requested(self):
        draft = _make_draft("Bay Power", {"manufacturer": "Baldor"}, request_contact=True)
        assert _ASK_MARKER in draft
        assert CONTACT_NOMINATION_ASK in draft

    def test_make_draft_omits_ask_by_default(self):
        draft = _make_draft("Bay Power", {"manufacturer": "Baldor"})
        assert _ASK_MARKER not in draft

    def test_should_request_contact_predicate(self):
        # Resolved named primary => do NOT ask (we have the person).
        assert should_request_contact({"primary_contact_status": "resolved"}) is False
        # Everything else => ask.
        for status in ("found_no_email", "none", "bounced", None):
            assert should_request_contact({"primary_contact_status": status}) is True
        assert should_request_contact({}) is True       # generic inbox, no primary
        assert should_request_contact(None) is True

    def test_resolved_primary_draft_has_no_ask(self):
        vendors = [{"vendor_name": "Bay Power", "primary_contact_status": "resolved"}]
        campaign = initiate_outreach_campaign(_run_id(), vendors)
        assert _ASK_MARKER not in campaign["drafts"]["Bay Power"]

    def test_found_no_email_draft_includes_ask(self):
        vendors = [{"vendor_name": "EMW", "primary_contact_status": "found_no_email"}]
        campaign = initiate_outreach_campaign(_run_id(), vendors)
        assert _ASK_MARKER in campaign["drafts"]["EMW"]

    def test_generic_inbox_fallback_draft_includes_ask(self):
        # No primary contact at all -> generic-inbox fallback -> ask.
        vendors = [{"vendor_name": "Standard Electric"}]
        campaign = initiate_outreach_campaign(_run_id(), vendors)
        assert _ASK_MARKER in campaign["drafts"]["Standard Electric"]
