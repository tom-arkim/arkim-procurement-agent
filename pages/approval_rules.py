"""
Arkim — Approval Rules Admin (Phase 3 prototype config tool).

Gated behind SHOW_ADMIN_VIEW=true environment variable.
Approver roles are display labels only — no RBAC enforcement in prototype.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import streamlit as st

st.set_page_config(
    page_title="Arkim · Approval Rules",
    page_icon="⚙",
    layout="wide",
)

# Gate behind env flag
if os.environ.get("SHOW_ADMIN_VIEW", "false").lower() != "true":
    st.error("Admin view is disabled. Set SHOW_ADMIN_VIEW=true to access this page.")
    st.stop()

from utils.procurement_agent.state.persistence import (
    list_approval_rules, upsert_approval_rule, delete_approval_rule, delete_facility_rules,
)
from utils.procurement_agent.state.approval_rules import DEFAULT_RULES

st.markdown("## Approval Rules — Admin Config")
st.caption(
    "Prototype configuration tool. Approver roles are display labels only — "
    "actual user/role enforcement requires identity infrastructure not yet implemented."
)
st.divider()

# ---------------------------------------------------------------------------
# Facility selector
# ---------------------------------------------------------------------------

KNOWN_FACILITIES = {
    "La Mirada (default)": "00000000-0000-0000-0000-000000000000",
    "Test Facility A":     "aaaaaaaa-0000-0000-0000-000000000000",
    "Test Facility B":     "bbbbbbbb-0000-0000-0000-000000000000",
}

selected_name = st.selectbox(
    "Facility",
    list(KNOWN_FACILITIES.keys()),
    key="admin_facility",
)
facility_id = KNOWN_FACILITIES[selected_name]
st.caption(f"Facility ID: `{facility_id}`")
st.divider()

# ---------------------------------------------------------------------------
# Current rules table
# ---------------------------------------------------------------------------

rules = list_approval_rules(facility_id)
is_using_defaults = not rules

if is_using_defaults:
    st.info("No custom rules configured for this facility — system defaults are active.")
    display_rules = DEFAULT_RULES
else:
    display_rules = rules

st.markdown("#### Current Rules")

import pandas as pd

def _rules_to_df(rule_list: list, has_ids: bool) -> pd.DataFrame:
    rows = []
    for r in rule_list:
        roles = r.get("approver_roles") or []
        rows.append({
            "Threshold (USD)":    f"${r['threshold_usd']:,.0f}",
            "Approvers Required": r["approvers_required"],
            "Approver Roles":     ", ".join(roles),
            **({"Rule ID": r["id"][:8] + "…"} if has_ids and "id" in r else {}),
        })
    return pd.DataFrame(rows)

if display_rules:
    st.dataframe(
        _rules_to_df(display_rules, has_ids=not is_using_defaults),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.caption("No rules.")

st.divider()

# ---------------------------------------------------------------------------
# Add / edit rule
# ---------------------------------------------------------------------------

st.markdown("#### Add New Rule")

with st.form("add_rule_form", clear_on_submit=True):
    c1, c2, c3 = st.columns([2, 1, 3])
    new_threshold   = c1.number_input("Threshold USD", min_value=0, value=0, step=1000)
    new_count       = c2.number_input("Approvers", min_value=1, max_value=4, value=1, step=1)
    new_roles_raw   = c3.text_input(
        "Approver Roles (comma-separated)",
        value="any_authorized_user",
        help="e.g., maintenance_director, operations_manager",
    )
    submitted = st.form_submit_button("Add Rule", type="primary")

if submitted:
    new_roles = [r.strip() for r in new_roles_raw.split(",") if r.strip()]
    if not new_roles:
        st.error("At least one approver role is required.")
    else:
        upsert_approval_rule(
            facility_id=facility_id,
            threshold_usd=float(new_threshold),
            approvers_required=int(new_count),
            approver_roles=new_roles,
        )
        st.success(f"Rule added: ${new_threshold:,} → {new_count} approver(s)")
        st.rerun()

st.divider()

# ---------------------------------------------------------------------------
# Delete individual rules
# ---------------------------------------------------------------------------

if rules:
    st.markdown("#### Delete Rule")
    for r in rules:
        roles_str = ", ".join(r.get("approver_roles") or [])
        label     = f"${r['threshold_usd']:,.0f} — {r['approvers_required']} approver(s): {roles_str}"
        if st.button(f"Delete: {label}", key=f"del_{r['id']}"):
            delete_approval_rule(r["id"])
            st.success("Rule deleted.")
            st.rerun()

    st.divider()

# ---------------------------------------------------------------------------
# Reset to defaults
# ---------------------------------------------------------------------------

st.markdown("#### Reset to Defaults")
st.caption(
    "Deletes all custom rules for this facility and restores system defaults on next lookup. "
    "System defaults are applied automatically when no custom rules exist — no data is written."
)

if st.button("Reset to Defaults", type="secondary"):
    count = delete_facility_rules(facility_id)
    st.success(f"Deleted {count} custom rule(s). System defaults are now active for this facility.")
    st.rerun()
