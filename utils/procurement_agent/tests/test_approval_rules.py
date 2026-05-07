"""
Tests for the Approval Rules Engine — get_applicable_rule and determine_approval_path.
"""

import pytest
from utils.procurement_agent.state.approval_rules import (
    get_applicable_rule,
    determine_approval_path,
    DEFAULT_RULES,
)
from utils.procurement_agent.state.persistence import upsert_approval_rule, delete_facility_rules

_FACILITY_A = "aaaaaaaa-0000-0000-0000-000000000000"
_FACILITY_B = "bbbbbbbb-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# Default rules (no custom rules configured for facility)
# ---------------------------------------------------------------------------

class TestDefaultRules:
    def test_no_facility_rules_returns_default_baseline(self, db_url):
        rule = get_applicable_rule(_FACILITY_A, 500.0, db_url=db_url)
        assert rule["approvers_required"] == 1
        assert "any_authorized_user" in rule["approver_roles"]

    def test_default_rules_single_approver_below_5000(self, db_url):
        n, roles = determine_approval_path(_FACILITY_A, 999.99, db_url=db_url)
        assert n == 1
        assert "any_authorized_user" in roles

    def test_default_rules_dual_approver_at_5000(self, db_url):
        n, roles = determine_approval_path(_FACILITY_A, 5_000.0, db_url=db_url)
        assert n == 2

    def test_default_rules_dual_approver_above_5000(self, db_url):
        n, roles = determine_approval_path(_FACILITY_A, 10_000.0, db_url=db_url)
        assert n == 2
        assert "maintenance_director" in roles or "operations_manager" in roles

    def test_default_rules_high_value_uses_25000_threshold(self, db_url):
        n, roles = determine_approval_path(_FACILITY_A, 30_000.0, db_url=db_url)
        assert n == 2
        assert "vp_operations" in roles

    def test_zero_total_uses_baseline_rule(self, db_url):
        n, _ = determine_approval_path(_FACILITY_A, 0.0, db_url=db_url)
        assert n == 1


# ---------------------------------------------------------------------------
# Custom facility rules
# ---------------------------------------------------------------------------

class TestCustomFacilityRules:
    def test_single_rule_below_threshold_returns_baseline(self, db_url):
        """Facility has one rule at $5000. Purchase is $1000 — uses lowest eligible rule."""
        upsert_approval_rule(
            facility_id=_FACILITY_B,
            threshold_usd=5_000,
            approvers_required=2,
            approver_roles=["manager"],
            db_url=db_url,
        )
        # $1000 < $5000 → falls through to the lowest (only) rule
        rule = get_applicable_rule(_FACILITY_B, 1_000.0, db_url=db_url)
        # Only one rule exists and $1000 < $5000, so no eligible rules → use lowest
        assert rule["threshold_usd"] == 5_000
        assert rule["approvers_required"] == 2

    def test_multiple_rules_highest_threshold_wins(self, db_url):
        """Two rules: $0 (1 approver) and $5000 (2 approvers). $7000 → $5000 rule."""
        upsert_approval_rule(
            facility_id=_FACILITY_B,
            threshold_usd=0,
            approvers_required=1,
            approver_roles=["supervisor"],
            db_url=db_url,
        )
        upsert_approval_rule(
            facility_id=_FACILITY_B,
            threshold_usd=5_000,
            approvers_required=2,
            approver_roles=["director"],
            db_url=db_url,
        )
        rule = get_applicable_rule(_FACILITY_B, 7_000.0, db_url=db_url)
        assert rule["threshold_usd"] == 5_000
        assert rule["approvers_required"] == 2

    def test_multiple_rules_baseline_wins_for_low_total(self, db_url):
        """Two rules: $0 and $5000. $500 → $0 rule."""
        upsert_approval_rule(
            facility_id=_FACILITY_B,
            threshold_usd=0,
            approvers_required=1,
            approver_roles=["supervisor"],
            db_url=db_url,
        )
        upsert_approval_rule(
            facility_id=_FACILITY_B,
            threshold_usd=5_000,
            approvers_required=2,
            approver_roles=["director"],
            db_url=db_url,
        )
        rule = get_applicable_rule(_FACILITY_B, 500.0, db_url=db_url)
        assert rule["threshold_usd"] == 0
        assert rule["approvers_required"] == 1

    def test_determine_approval_path_tuple_return(self, db_url):
        upsert_approval_rule(
            facility_id=_FACILITY_B,
            threshold_usd=0,
            approvers_required=1,
            approver_roles=["plant_manager"],
            db_url=db_url,
        )
        n, roles = determine_approval_path(_FACILITY_B, 100.0, db_url=db_url)
        assert n == 1
        assert "plant_manager" in roles


# ---------------------------------------------------------------------------
# DEFAULT_RULES constant sanity checks
# ---------------------------------------------------------------------------

class TestDefaultRulesConstant:
    def test_default_has_baseline_rule(self):
        baseline = next(r for r in DEFAULT_RULES if r["threshold_usd"] == 0)
        assert baseline["approvers_required"] == 1

    def test_default_has_5000_dual_approval(self):
        rule_5k = next(r for r in DEFAULT_RULES if r["threshold_usd"] == 5_000)
        assert rule_5k["approvers_required"] == 2

    def test_default_has_25000_senior_approval(self):
        rule_25k = next(r for r in DEFAULT_RULES if r["threshold_usd"] == 25_000)
        assert rule_25k["approvers_required"] == 2
        assert "vp_operations" in rule_25k["approver_roles"]
