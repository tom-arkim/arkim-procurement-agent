"""
Approval Rules Engine — configuration-driven approval routing.

Brief reference: Section 3.5, Section 8.5.

Rules are stored per facility in the approval_rules SQLite table.
When no custom rules exist for a facility, DEFAULT_RULES are used.

Rule evaluation: find the highest threshold_usd rule where
threshold_usd <= candidate total_usd. If no rule qualifies (total
is below all thresholds), use the lowest-threshold rule (the $0
baseline in DEFAULT_RULES always qualifies).
"""

from typing import Optional

# ---------------------------------------------------------------------------
# Default rules — applied when a facility has no custom rules configured
# ---------------------------------------------------------------------------

DEFAULT_RULES: list[dict] = [
    {
        "threshold_usd":      0,
        "approvers_required": 1,
        "approver_roles":     ["any_authorized_user"],
    },
    {
        "threshold_usd":      5_000,
        "approvers_required": 2,
        "approver_roles":     ["maintenance_director", "operations_manager"],
    },
    {
        "threshold_usd":      25_000,
        "approvers_required": 2,
        "approver_roles":     ["operations_manager", "vp_operations"],
    },
]

_SYSTEM_DEFAULT = DEFAULT_RULES[0]


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------

def get_applicable_rule(
    facility_id: str,
    total_usd: float,
    db_url: Optional[str] = None,
) -> dict:
    """Return the applicable rule dict for a given facility and purchase total.

    Logic: among all rules where threshold_usd <= total_usd, return the one
    with the highest threshold_usd (most specific match). If no rules are
    configured for the facility, fall back to DEFAULT_RULES.
    """
    from utils.procurement_agent.state.persistence import list_approval_rules

    facility_rules = list_approval_rules(facility_id, db_url=db_url)
    rules = facility_rules if facility_rules else DEFAULT_RULES

    eligible = [r for r in rules if r["threshold_usd"] <= total_usd]
    if not eligible:
        # total_usd below all thresholds — use lowest threshold rule
        sorted_rules = sorted(rules, key=lambda r: r["threshold_usd"])
        return sorted_rules[0] if sorted_rules else _SYSTEM_DEFAULT

    return max(eligible, key=lambda r: r["threshold_usd"])


def determine_approval_path(
    facility_id: str,
    total_usd: float,
    db_url: Optional[str] = None,
) -> tuple[int, list[str]]:
    """Return (approvers_required, approver_roles) for the candidate purchase.

    Args:
        facility_id: UUID string for the facility
        total_usd:   Grand total purchase price in USD

    Returns:
        (approvers_required, approver_roles) — roles are display labels only
        for prototype; RBAC enforcement is post-seed infrastructure work.
    """
    rule = get_applicable_rule(facility_id, total_usd, db_url=db_url)
    roles = rule.get("approver_roles") or ["any_authorized_user"]
    return int(rule.get("approvers_required", 1)), list(roles)
