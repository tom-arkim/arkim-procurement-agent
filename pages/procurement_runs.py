"""
Arkim Procurement Runs — Phase 2 UI.

Phase-specific rendering:
  INTAKE      — chat interface with IntakeAgent (multimodal, clarification loop)
  SOURCING    — "Execute Sourcing" button (auto-run from intake or manual advance)
  COMPARISON+ — three-tier sourcing results display
  Other       — generic phase detail with stub advance button
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import json
import streamlit as st
from datetime import datetime, timezone

from utils.procurement_agent.state.persistence import list_runs, get_run, update_run
from utils.procurement_agent.orchestrator.core import Orchestrator, start_new_run
from utils.procurement_agent.state.phases import Phase
from utils.models import ProcurementRun
from utils.audit_log import recent_entries

st.set_page_config(
    page_title="Arkim · Procurement Runs",
    page_icon="⚙",
    layout="wide",
)

# ---------------------------------------------------------------------------
# CSS
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
.phase-intake     { border-color: #58a6ff; color: #58a6ff; }
.phase-inventory  { border-color: #3fb950; color: #3fb950; }
.phase-sourcing   { border-color: #d29922; color: #d29922; }
.phase-comparison { border-color: #a855f7; color: #a855f7; }
.phase-approved   { border-color: #3fb950; color: #3fb950; }
.phase-executing  { border-color: #fb923c; color: #fb923c; }
.phase-completed  { border-color: #3fb950; color: #3fb950; background: #1a3a20; }
.phase-cancelled  { border-color: #f85149; color: #f85149; }
.phase-error      { border-color: #f85149; color: #f85149; }
.vendor-card {
    background: #0d1117; border: 1px solid #30363d; border-radius: 8px;
    padding: .8rem 1rem; margin-bottom: .6rem;
}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

_PHASE_ORDER  = [p.value for p in Phase]
_TERMINAL     = {Phase.COMPLETED.value, Phase.CANCELLED.value, Phase.ERROR.value}
_NULL_VALUES  = {None, "", "null", "N/A", "Unknown", "UNKNOWN-PN", "none", "unknown"}


def _phase_label(phase: str) -> str:
    return phase.replace("_", " ").title()


def _urgency_label(uf: float) -> str:
    return {0.0: "Stocking", 0.3: "Predictive", 1.0: "Emergency"}.get(uf, f"{uf:.1f}")


def _render_specs_table(specs: dict) -> None:
    import pandas as pd
    key_order = [
        "manufacturer", "model", "part_number", "category", "detected_type",
        "voltage", "hp", "frame", "rpm", "phase", "gpm", "psi",
        "shaft_size", "bore_diameter", "material_spec", "connection_size",
        "serial_number", "description", "use_case",
    ]
    rows = []
    for key in key_order:
        val = specs.get(key)
        if val not in _NULL_VALUES:
            rows.append({"Field": key.replace("_", " ").title(), "Value": str(val)})
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption("No specifications extracted yet.")


def _dict_to_procurement_run(d: dict) -> ProcurementRun:
    def _dt(v):
        if v is None:
            return datetime.now(timezone.utc)
        if isinstance(v, datetime):
            return v
        try:
            return datetime.fromisoformat(v)
        except Exception:
            return datetime.now(timezone.utc)

    return ProcurementRun(
        id=d["id"],
        facility_id=d["facility_id"],
        initiated_by_user_id=d.get("initiated_by_user_id"),
        initiated_at=_dt(d.get("initiated_at")),
        current_phase=d["current_phase"],
        urgency_factor=d.get("urgency_factor", 0.3),
        warranty_status=d.get("warranty_status", "unknown"),
        asset_specs_json=d.get("asset_specs_json"),
        inventory_result_json=d.get("inventory_result_json"),
        sourcing_results_json=d.get("sourcing_results_json"),
        selected_candidate_json=d.get("selected_candidate_json"),
        approval_history_json=d.get("approval_history_json") or [],
        vendor_order_id=d.get("vendor_order_id"),
        fulfillment_status=d.get("fulfillment_status"),
        inventory_update_json=d.get("inventory_update_json"),
        work_order_link=d.get("work_order_link"),
        audit_log_run_id=d.get("audit_log_run_id"),
        agent_version=d.get("agent_version", "2.0.0-phase2"),
        created_at=_dt(d.get("created_at")),
        updated_at=_dt(d.get("updated_at")),
    )


def _advance_run_through_inventory_to_sourcing(run_id: str) -> None:
    """Transition INTAKE -> INVENTORY -> SOURCING -> COMPARISON in one call."""
    orch = Orchestrator(run_id)
    orch.transition_to(Phase.INVENTORY)
    orch.execute_current_phase()   # INVENTORY stub -> SOURCING
    orch2 = Orchestrator(run_id)
    orch2.execute_current_phase()  # SOURCING (real agent) -> COMPARISON


# ---------------------------------------------------------------------------
# Phase renderers
# ---------------------------------------------------------------------------

def _render_intake(run_id: str, run: dict) -> None:
    """Intake phase: chat interface with multi-turn clarification."""
    chat_key       = f"intake_chat_{run_id}"
    specs_key      = f"intake_specs_{run_id}"
    followup_key   = f"intake_followup_{run_id}"
    sufficient_key = f"intake_sufficient_{run_id}"

    if chat_key not in st.session_state:
        st.session_state[chat_key] = []
    if specs_key not in st.session_state:
        st.session_state[specs_key] = run.get("asset_specs_json") or {}
    if followup_key not in st.session_state:
        st.session_state[followup_key] = None
    if sufficient_key not in st.session_state:
        st.session_state[sufficient_key] = False

    st.markdown("#### Intake — Part Specification")
    st.caption(
        "Describe the part or equipment needed. Upload a nameplate photo for faster extraction. "
        "Follow the agent's clarifying questions until all required specs are confirmed."
    )
    st.divider()

    # --- Sufficient: show confirmation and launch ---
    if st.session_state[sufficient_key]:
        specs = st.session_state[specs_key]
        st.success("All required specifications confirmed — ready to source.")
        col_go, col_edit = st.columns([1, 1])
        with col_go:
            if st.button("Start Sourcing", type="primary", use_container_width=True):
                update_run(run_id, {"asset_specs_json": specs})
                with st.spinner("Running sourcing — this may take 30–60 seconds..."):
                    _advance_run_through_inventory_to_sourcing(run_id)
                st.rerun()
        with col_edit:
            if st.button("Edit Specs", use_container_width=True):
                st.session_state[sufficient_key] = False
                st.session_state[followup_key]   = None
                st.rerun()

        st.markdown("**Extracted Specifications**")
        _render_specs_table(specs)

        conf   = float(specs.get("manufacturer_confidence") or 0)
        pconf  = float(specs.get("part_id_confidence") or 0)
        mc1, mc2 = st.columns(2)
        mc1.metric("Manufacturer Confidence", f"{conf:.0f}%")
        mc2.metric("Part ID Confidence", f"{pconf:.0f}%")
        return

    # --- Chat history ---
    for msg in st.session_state[chat_key]:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    # --- Clarification prompt ---
    followup = st.session_state[followup_key]
    if followup:
        st.info(f"**Question:** {followup}")

    # --- Image upload ---
    uploaded_files = st.file_uploader(
        "Upload nameplate photo(s) (JPG / PNG)",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
        key=f"img_upload_{run_id}",
    )

    # --- Chat input ---
    user_text = st.chat_input("Describe the part or equipment...")

    if user_text or uploaded_files:
        text_to_show = user_text or "(uploaded image)"
        st.session_state[chat_key].append({"role": "user", "content": text_to_show})

        images = [f.read() for f in (uploaded_files or [])]

        from utils.procurement_agent.agents.intake_agent import IntakeAgent
        agent    = IntakeAgent(anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"))
        run_model = _dict_to_procurement_run(run)
        run_model.asset_specs_json = st.session_state[specs_key]

        with st.spinner("Extracting specifications..."):
            result = agent.run(run_model, {
                "text":         user_text or "",
                "images":       images,
                "force_proceed": False,
            })

        st.session_state[specs_key]      = result["asset_specs"]
        st.session_state[followup_key]   = result.get("follow_up_question")
        st.session_state[sufficient_key] = result["sufficient"]

        if result["sufficient"]:
            assistant_msg = "All specifications collected. Click **Start Sourcing** to find suppliers."
        else:
            assistant_msg = result.get("follow_up_question") or "Could you provide more details?"

        st.session_state[chat_key].append({"role": "assistant", "content": assistant_msg})
        st.rerun()

    # --- Partial specs + force proceed ---
    if st.session_state[specs_key]:
        st.divider()
        conf  = float(st.session_state[specs_key].get("manufacturer_confidence") or 0)
        pconf = float(st.session_state[specs_key].get("part_id_confidence") or 0)
        sc1, sc2 = st.columns(2)
        sc1.metric("Manufacturer Confidence", f"{conf:.0f}%")
        sc2.metric("Part ID Confidence", f"{pconf:.0f}%")

        with st.expander("Specifications collected so far"):
            _render_specs_table(st.session_state[specs_key])

        if st.button("Search Anyway (proceed with partial specs)", type="secondary"):
            specs = st.session_state[specs_key]
            update_run(run_id, {"asset_specs_json": specs})
            with st.spinner("Running sourcing..."):
                _advance_run_through_inventory_to_sourcing(run_id)
            st.rerun()


def _render_vendor_card(opt: dict) -> None:
    vendor      = opt.get("vendor_name") or "Unknown"
    price       = opt.get("base_price") or 0
    price_tbd   = opt.get("price_tbd", False)
    lead        = opt.get("lead_time_days") or "—"
    reliability = float(opt.get("reliability_score") or 0)
    suitability = float(opt.get("suitability_score") or 0)
    confidence  = float(opt.get("confidence_score") or 0)
    match_type  = opt.get("match_type") or "—"
    merchant    = opt.get("merchant_type") or "—"
    url         = opt.get("source_url")
    auth        = opt.get("vendor_authorization_status") or ""
    in_stock    = opt.get("in_stock")

    price_str = "Quote Required" if price_tbd or not price else f"${price:,.2f}"
    auth_tag  = " ✓ Authorized" if auth == "Authorized" else ""
    stock_tag = ""
    if in_stock is True:
        stock_tag = " · In Stock"
    elif in_stock is False:
        stock_tag = " · Lead Time"

    with st.container():
        c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
        with c1:
            if url:
                st.markdown(f"**[{vendor}]({url})**{auth_tag}")
            else:
                st.markdown(f"**{vendor}**{auth_tag}")
            st.caption(f"{merchant} · {match_type}{stock_tag}")
        with c2:
            st.metric("Price", price_str)
        with c3:
            st.metric("Lead Time", f"{lead}d" if isinstance(lead, int) else str(lead))
        with c4:
            st.metric("Confidence", f"{confidence:.0f}%")
        if suitability > 0:
            st.progress(min(1.0, suitability / 100.0), text=f"Suitability: {suitability:.0f}%")
        st.markdown("---")


def _render_sourcing_results(run: dict) -> None:
    """Three-tier sourcing results display."""
    sr = run.get("sourcing_results_json") or {}
    if not sr:
        st.info("No sourcing results available.")
        return

    # Warranty banner
    banner = sr.get("warranty_banner")
    if banner:
        st.warning(f"**Warranty Notice:** {banner}")

    # Summary metrics
    urgency_applied = (sr.get("urgency_applied") or "—").title()
    t1 = sr.get("tier_1") or {"results": [], "count": 0, "status": "—"}
    t2 = sr.get("tier_2") or {"results": [], "count": 0, "status": "—"}
    t3 = sr.get("tier_3") or {"results": [], "count": 0, "status": "—"}

    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric("Urgency Mode", urgency_applied)
    mc2.metric("Tier 1 — Arkim Network", t1.get("count", 0))
    mc3.metric("Tier 2 — Marketplace",   t2.get("count", 0))
    mc4.metric("Tier 3 — Broader Market", t3.get("count", 0))

    st.divider()

    # Tier 1 — Arkim Network
    st.markdown("### Tier 1 — Arkim Network")
    st.caption("Arkim onboarded suppliers with pre-negotiated availability.")
    t1_results = t1.get("results") or []
    if t1.get("status") == "timeout":
        st.warning("Tier 1 timed out.")
    elif not t1_results:
        st.caption("No Arkim Network vendors matched this part.")
    else:
        for opt in t1_results:
            _render_vendor_card(opt)

    st.divider()

    # Tier 2 — Digital Marketplace
    st.markdown("### Tier 2 — Available Now (Digital Marketplace)")
    st.caption("National distributors with live pricing — Grainger, McMaster-Carr, MSC Industrial, etc.")
    t2_results = t2.get("results") or []
    if t2.get("status") == "timeout":
        st.warning("Tier 2 timed out.")
    elif not t2_results:
        st.caption("No marketplace results found.")
    else:
        for opt in t2_results:
            _render_vendor_card(opt)

    st.divider()

    # Tier 3 — Broader Market
    st.markdown("### Tier 3 — Broader Market")
    st.caption("National specialists and aftermarket equivalents.")
    filters    = sr.get("filters_applied") or []
    t3_results = t3.get("results") or []
    if any("aftermarket excluded" in f for f in filters):
        st.info("Aftermarket options excluded — asset is under warranty.")
    if t3.get("status") == "timeout":
        st.warning("Tier 3 timed out.")
    elif not t3_results:
        st.caption("No broader market results found.")
    else:
        for opt in t3_results:
            _render_vendor_card(opt)


def _render_phase_detail(run_id: str, run: dict) -> None:
    """Generic phase detail: advance button + payload viewer."""
    phase = run["current_phase"]
    if phase not in _TERMINAL:
        st.markdown("**Phase Controls**")
        st.caption("In production, phase transitions are triggered automatically by agent outputs.")
        if st.button("Advance Phase (stub)", type="primary"):
            try:
                orch = Orchestrator(run_id)
                orch.execute_current_phase()
                st.success(f"Advanced from **{_phase_label(phase)}**.")
                st.rerun()
            except Exception as exc:
                st.error(f"Transition failed: {exc}")
        st.divider()

    st.markdown("**Phase Outputs**")
    for label, value in [
        ("Asset Specs",       run.get("asset_specs_json")),
        ("Inventory Result",  run.get("inventory_result_json")),
        ("Sourcing Results",  run.get("sourcing_results_json")),
        ("Selected Candidate", run.get("selected_candidate_json")),
        ("Approval History",  run.get("approval_history_json")),
    ]:
        if value:
            with st.expander(label):
                st.json(value)
        else:
            st.caption(f"{label}: —")


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### Arkim Procurement")
    st.divider()

    st.markdown("**New Run**")
    u_cols = st.columns(3)
    _urgency_val = st.session_state.get("new_run_urgency", 0.3)
    if u_cols[0].button("Stocking",   use_container_width=True,
                         type="primary" if _urgency_val == 0.0 else "secondary"):
        st.session_state["new_run_urgency"] = 0.0
        st.rerun()
    if u_cols[1].button("Predictive", use_container_width=True,
                         type="primary" if _urgency_val == 0.3 else "secondary"):
        st.session_state["new_run_urgency"] = 0.3
        st.rerun()
    if u_cols[2].button("Emergency",  use_container_width=True,
                         type="primary" if _urgency_val == 1.0 else "secondary"):
        st.session_state["new_run_urgency"] = 1.0
        st.rerun()

    warranty_new = st.selectbox(
        "Warranty Status",
        ["unknown", "in_warranty", "out_of_warranty", "warranty_waived"],
        index=0,
        key="new_run_warranty",
    )
    if st.button("Create New Run", use_container_width=True, type="primary"):
        orch = start_new_run(
            urgency_factor=st.session_state.get("new_run_urgency", 0.3),
            warranty_status=warranty_new,
        )
        st.session_state["selected_run_id"] = orch.run_id
        st.rerun()

    st.divider()
    st.markdown("**Recent Runs**")
    _runs = list_runs(limit=12)
    for _r in _runs:
        _ph  = _r["current_phase"]
        _sel = st.session_state.get("selected_run_id") == _r["id"]
        _lbl = f"{'> ' if _sel else ''}`{_r['id'][:8]}` {_ph}"
        if st.button(_lbl, key=f"sel_{_r['id']}", use_container_width=True):
            st.session_state["selected_run_id"] = _r["id"]
            st.rerun()


# ---------------------------------------------------------------------------
# Main area
# ---------------------------------------------------------------------------

run_id = st.session_state.get("selected_run_id")

if not run_id:
    st.markdown("## Procurement Runs")
    st.info("Create a new run using the sidebar to get started.")
else:
    run = get_run(run_id)
    if run is None:
        st.error(f"Run `{run_id}` not found.")
    else:
        phase        = run["current_phase"]
        phase_lbl    = _phase_label(phase)
        uf           = run.get("urgency_factor", 0.3)
        warranty_lbl = (run.get("warranty_status") or "unknown").replace("_", " ").title()

        # --- Header ---
        st.markdown(f"## Run `{run['id'][:8]}…`")
        hc1, hc2, hc3, hc4 = st.columns(4)
        hc1.metric("Phase",   phase_lbl)
        hc2.metric("Urgency", _urgency_label(uf))
        hc3.metric("Warranty", warranty_lbl)
        hc4.metric("Version",  run.get("agent_version", "—"))

        # Phase progress bar
        _progress_phases = [p for p in _PHASE_ORDER if p not in _TERMINAL]
        if phase in _progress_phases:
            pct = int(100 * _progress_phases.index(phase) / max(len(_progress_phases) - 1, 1))
            st.progress(pct, text=f"Phase: **{phase_lbl}**")

        st.divider()

        # --- Phase-specific content ---
        if phase == Phase.INTAKE.value:
            _render_intake(run_id, run)

        elif phase in (
            Phase.COMPARISON.value,
            Phase.PENDING_FIRST_APPROVAL.value,
            Phase.PENDING_SECOND_APPROVAL.value,
            Phase.APPROVED.value,
            Phase.EXECUTING.value,
            Phase.FULFILLING.value,
            Phase.COMPLETED.value,
        ):
            if run.get("sourcing_results_json"):
                _render_sourcing_results(run)
                st.divider()

            if run.get("asset_specs_json"):
                with st.expander("Asset Specifications", expanded=False):
                    _render_specs_table(run["asset_specs_json"])
                st.divider()

            if phase not in _TERMINAL:
                _render_phase_detail(run_id, run)

        else:
            # INVENTORY, SOURCING, CANCELLED, ERROR
            _render_phase_detail(run_id, run)

        # --- Audit log ---
        st.divider()
        with st.expander("Audit Log", expanded=False):
            all_entries  = recent_entries(limit=50)
            run_entries  = [e for e in all_entries if e.get("sourcing_run_id") == run_id]
            if run_entries:
                for entry in run_entries:
                    ts      = (entry.get("created_at") or "")[:19].replace("T", " ")
                    summary = entry.get("input_summary", "—")
                    st.markdown(f"- `{ts}` — {summary}")
            else:
                st.caption("No audit entries yet.")
