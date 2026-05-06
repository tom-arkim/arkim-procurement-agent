"""
Procurement Runs — Phase 1 state visualization page.

Shows recent ProcurementRuns, lets the user create a new stub run,
and lets the user advance a run through phases one step at a time.

Phase 1 purpose: validate the state machine and persistence layer are
working before real agent logic is added in Phases 2-4.
"""

import sys
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import streamlit as st
from datetime import datetime

from utils.procurement_agent.state.persistence import list_runs, create_run, get_run
from utils.procurement_agent.orchestrator.core import Orchestrator, start_new_run
from utils.procurement_agent.state.phases import Phase
from utils.audit_log import recent_entries

st.set_page_config(
    page_title="Arkim · Procurement Runs",
    page_icon="⚙",
    layout="wide",
)

# ---------------------------------------------------------------------------
# CSS (minimal — matches chat_app.py dark theme)
# ---------------------------------------------------------------------------
st.markdown("""
<style>
.a-card {
    background: #161b22; border: 1px solid #30363d; border-radius: 10px;
    padding: 1rem 1.4rem; margin-bottom: .8rem;
}
.a-label {
    font-size: .7rem; text-transform: uppercase; letter-spacing: .1em;
    color: #58a6ff; font-weight: 700; margin-bottom: .4rem;
}
.phase-badge {
    display: inline-block; background: #21262d; border: 1px solid #30363d;
    border-radius: 5px; padding: .1rem .45rem; font-size: .78rem;
    font-family: monospace; color: #c9d1d9;
}
.phase-intake    { border-color: #58a6ff; color: #58a6ff; }
.phase-inventory { border-color: #3fb950; color: #3fb950; }
.phase-sourcing  { border-color: #d29922; color: #d29922; }
.phase-comparison { border-color: #a855f7; color: #a855f7; }
.phase-approved  { border-color: #3fb950; color: #3fb950; }
.phase-executing { border-color: #fb923c; color: #fb923c; }
.phase-completed { border-color: #3fb950; color: #3fb950; background: #1a3a20; }
.phase-cancelled { border-color: #f85149; color: #f85149; }
.phase-error     { border-color: #f85149; color: #f85149; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.markdown("## Procurement Runs")
st.caption("Phase 1 — state machine visualization. All agent calls are stubs.")

# ---------------------------------------------------------------------------
# Sidebar — create new run
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### Create New Run")
    urgency = st.select_slider(
        "Urgency",
        options=[0.0, 0.3, 1.0],
        value=0.3,
        format_func=lambda v: {0.0: "Stocking", 0.3: "Predictive", 1.0: "Emergency"}[v],
    )
    warranty = st.selectbox(
        "Warranty Status",
        ["unknown", "in_warranty", "out_of_warranty", "warranty_waived"],
        index=0,
    )
    if st.button("Create New Run", use_container_width=True, type="primary"):
        orch = start_new_run(
            urgency_factor=urgency,
            warranty_status=warranty,
            asset_specs={"manufacturer": "Stub", "model": "STUB-001", "part_number": "PN-001"},
        )
        st.session_state["selected_run_id"] = orch.run_id
        st.success(f"Run created: `{orch.run_id[:8]}…`")
        st.rerun()

# ---------------------------------------------------------------------------
# Main area — two columns: run list (left), run detail (right)
# ---------------------------------------------------------------------------
col_list, col_detail = st.columns([1, 2], gap="large")

# ── Run list ─────────────────────────────────────────────────────────────────
with col_list:
    st.markdown("#### Recent Runs")

    runs = list_runs(limit=10)
    if not runs:
        st.info("No runs yet. Create one using the sidebar.")
    else:
        for run in runs:
            phase = run["current_phase"]
            phase_css = f"phase-{phase.replace('_', '-').split('-')[0]}"
            label = f"**`{run['id'][:8]}…`**"
            badge = f'<span class="phase-badge {phase_css}">{phase}</span>'
            ts = run["created_at"][:16].replace("T", " ") if run["created_at"] else "—"

            is_selected = st.session_state.get("selected_run_id") == run["id"]
            btn_label = f"{'▶ ' if is_selected else ''}{run['id'][:8]}… · {phase}"
            if st.button(btn_label, key=f"sel_{run['id']}", use_container_width=True):
                st.session_state["selected_run_id"] = run["id"]
                st.rerun()

# ── Run detail ────────────────────────────────────────────────────────────────
with col_detail:
    run_id = st.session_state.get("selected_run_id")

    if not run_id:
        st.info("Select a run from the list, or create a new one.")
    else:
        run = get_run(run_id)
        if run is None:
            st.error(f"Run `{run_id}` not found.")
        else:
            phase = run["current_phase"]
            phase_label = phase.replace("_", " ").title()

            st.markdown(f"#### Run `{run['id'][:8]}…`")

            # Phase status
            _PHASE_ORDER = [p.value for p in Phase]
            _terminal = {Phase.COMPLETED.value, Phase.CANCELLED.value, Phase.ERROR.value}
            phase_idx = _PHASE_ORDER.index(phase) if phase in _PHASE_ORDER else 0

            progress_phases = [p for p in _PHASE_ORDER if p not in _terminal]
            if phase in progress_phases:
                pct = int(100 * progress_phases.index(phase) / max(len(progress_phases) - 1, 1))
                st.progress(pct, text=f"Phase: **{phase_label}**")
            else:
                st.markdown(f"Phase: **{phase_label}**")

            # Key fields
            c1, c2, c3 = st.columns(3)
            uf = run.get("urgency_factor", 0.3)
            urgency_label = {0.0: "Stocking", 0.3: "Predictive", 1.0: "Emergency"}.get(uf, f"{uf:.1f}")
            c1.metric("Urgency", urgency_label)
            c2.metric("Warranty", run.get("warranty_status", "—").replace("_", " ").title())
            c3.metric("Version", run.get("agent_version", "—"))

            ts_created = run["created_at"][:16].replace("T", " ") if run.get("created_at") else "—"
            ts_updated = run["updated_at"][:16].replace("T", " ") if run.get("updated_at") else "—"
            st.caption(f"Created: {ts_created}  ·  Updated: {ts_updated}")

            # Advance phase button (Phase 1 testing only)
            if phase not in _terminal:
                st.divider()
                st.markdown("**Phase 1 Testing Controls**")
                st.caption("In production, phase transitions are triggered automatically by agent outputs.")
                if st.button("▶ Advance Phase (execute current stub)", type="primary"):
                    try:
                        orch = Orchestrator(run_id)
                        orch.execute_current_phase()
                        st.success(f"Advanced from **{phase}** → see updated phase below.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Transition failed: {exc}")

            # Phase output payloads
            st.divider()
            st.markdown("**Phase Outputs**")
            payload_fields = [
                ("Asset Specs", run.get("asset_specs_json")),
                ("Inventory Result", run.get("inventory_result_json")),
                ("Sourcing Results", run.get("sourcing_results_json")),
                ("Selected Candidate", run.get("selected_candidate_json")),
                ("Approval History", run.get("approval_history_json")),
            ]
            for label, value in payload_fields:
                if value:
                    with st.expander(label):
                        st.json(value)
                else:
                    st.caption(f"{label}: —")

            # Audit log entries for this run
            st.divider()
            st.markdown("**Audit Log**")
            all_entries = recent_entries(limit=50)
            run_entries = [e for e in all_entries if e.get("sourcing_run_id") == run_id]
            if run_entries:
                for entry in run_entries:
                    ts = entry.get("created_at", "")[:19].replace("T", " ")
                    summary = entry.get("input_summary", "—")
                    st.markdown(f"- `{ts}` — {summary}")
            else:
                st.caption("No audit entries yet for this run.")
