"""
Per-type question registry — the data spine of the intake redesign (Phases 1 + 2).

Pure data. Importing this module makes NO network / LLM calls and reads no secrets.
It is the single source of truth for how each industrial part type is sourced:
its regime (DIRECT vs ANCHORED-to-parent), sourcing lane (STANDARD / OEM / MIXED),
the highest-entropy blocking question (q2_template), the blocking vs refinement
attribute sets, lightweight inference rules (context_token -> {attr: value}), and
nameplate guidance for the operator.

Source of truth: the distilled registry seed in
`arkim-overnight-intake-build-brief.md` §4 (itself distilled from the
parts-matching research). Transcribed, not invented. Five profiles are defined
here (mechanical_seal, pump, valve, sensor_instrument, motor_drive); the brief's
"4 priority types" framing counts mechanical_seal + pump as the priority pair —
all five are carried so the classifier has a complete constrained output space,
and the brief explicitly allows the fifth ("That is 5 profiles ... Do not add
more types tonight").

An UNKNOWN sentinel is provided for off-registry / unclassifiable input — every
call site must tolerate it (UNKNOWN means "fall through to the current generic
intake behavior").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Regime / sourcing enums (string literals — kept as plain constants so the
# constrained classifier output contract can name them without importing here).
# ---------------------------------------------------------------------------

REGIME_DIRECT = "DIRECT"        # the part is sourced on its own identity
REGIME_ANCHORED = "ANCHORED"    # the part is sourced AS a component of a parent asset

SOURCING_STANDARD = "STANDARD"  # broad / cross-vendor sourcing
SOURCING_OEM = "OEM"            # OEM / manufacturer-channel sourcing
SOURCING_MIXED = "MIXED"        # OEM-leaning but cross-vendor possible


# The set of part_type strings the classifier is allowed to emit. Anything
# outside this set (or unparseable) collapses to UNKNOWN. Kept here so the
# classifier's constrained-output prompt and the registry stay in lockstep.
KNOWN_PART_TYPES: tuple[str, ...] = (
    "mechanical_seal",
    "pump",
    "valve",
    "sensor_instrument",
    "motor_drive",
)

UNKNOWN_PART_TYPE = "unknown"


# ---------------------------------------------------------------------------
# Profile dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PartTypeProfile:
    """One part type's sourcing profile — pure data.

    Fields mirror the brief's §4 seed exactly:
      - part_type: the registry key (one of KNOWN_PART_TYPES)
      - regime: DIRECT | ANCHORED (ANCHORED = sourced as a component of a parent)
      - sourcing: STANDARD | OEM | MIXED
      - configurable: True for order-code families (sensor_instrument tonight);
        Phase 3 will add variant/order-code questions — tonight it is just marked.
      - identity_anchor_question: the verbatim opener for this type when identity
        is being established (used as documented reference; the question-flow
        engine tonight uses q2_template for the batched clarification).
      - q2_template: the batched highest-entropy BLOCKING question, asked verbatim
        (no LLM phrasing call) when identity is absent and the type is known. For
        an ANCHORED component this combines a PARENT-identity clause (the parent
        make/model) with a COMPONENT-dims clause; the parent clause is only
        legitimate when the parent is NOT already known — see q2_component_clause.
      - q2_component_clause: ANCHORED types only — the COMPONENT-dimensions half
        of the q2 (shaft size / cartridge-vs-component / single-vs-double / ...),
        asked when the parent identity is ALREADY captured in `_component_of` so
        the intake leads with the genuinely-undetermined component dims instead
        of re-asking the parent (the Goulds-3196 over-clarification fix). Empty
        for DIRECT types (their q2_template is asked verbatim, no split).
      - blocking_attrs: the attributes that must be captured before sourcing.
      - refinement_attrs: nice-to-haves that refine but do not block.
      - variant_selecting_attrs: the subset of blocking_attrs that selects a
        VARIANT within an identified family (e.g. a motor/drive's hp +
        voltage_phase pick the exact catalog number inside "PowerFlex 40";
        a seal's shaft_size; a pump's hydraulic_duty). Empty when the class
        isn't a family-with-variants. The family-disambiguation gate keys on
        this: a model present + no part_number does NOT satisfy these — the
        intake must ask for them (see intake_agent._first_missing_required_field
        + the confirm_intake guard). Distinct from `configurable` (which marks
        order-code families for the Phase 3 variant-resolution work) — this is
        the near-term detect-and-ask marker, buildable without a catalog.
      - inference_rules: {context_token: {attr: proposed_value}} — lightweight
        defaults inferred from context (e.g. sanitary/CIP -> 316L + Tri-Clamp).
      - nameplate_guidance: where on the physical asset to read the identity.
    """
    part_type: str
    regime: str
    sourcing: str
    configurable: bool
    identity_anchor_question: str
    q2_template: str
    q2_component_clause: str = ""   # ANCHORED types: dims half, asked when parent known
    blocking_attrs: List[str] = field(default_factory=list)
    refinement_attrs: List[str] = field(default_factory=list)
    variant_selecting_attrs: List[str] = field(default_factory=list)
    inference_rules: Dict[str, Dict[str, str]] = field(default_factory=dict)
    nameplate_guidance: str = ""


# ---------------------------------------------------------------------------
# UNKNOWN sentinel — returned for off-registry / unclassifiable input.
# Call sites must tolerate it and fall through to the current generic behavior.
# ---------------------------------------------------------------------------

UNKNOWN_PROFILE: PartTypeProfile = PartTypeProfile(
    part_type=UNKNOWN_PART_TYPE,
    regime=REGIME_DIRECT,
    sourcing=SOURCING_STANDARD,
    configurable=False,
    identity_anchor_question="",
    q2_template="",
    blocking_attrs=[],
    refinement_attrs=[],
    inference_rules={},
    nameplate_guidance="",
)


# ---------------------------------------------------------------------------
# The five profiles — transcribed verbatim from the brief's §4 seed.
# Sanitary / CIP / dairy / food inference tokens map to the same wetted +
# connection defaults across the food-contact types; replicated per-type
# deliberately (each profile is self-contained data).
# ---------------------------------------------------------------------------

_SANITARY_INFERENCE: Dict[str, Dict[str, str]] = {
    "CIP":     {"wetted": "316L", "elastomer": "EPDM", "connection": "Tri-Clamp"},
    "dairy":   {"wetted": "316L", "elastomer": "EPDM", "connection": "Tri-Clamp"},
    "sanitary":{"wetted": "316L", "elastomer": "EPDM", "connection": "Tri-Clamp"},
    "food":    {"wetted": "316L", "elastomer": "EPDM", "connection": "Tri-Clamp"},
}

_WASHDOWN_INFERENCE: Dict[str, Dict[str, str]] = {
    "washdown": {"enclosure": "washdown/TEFC stainless-clad"},
    "food":     {"enclosure": "washdown/TEFC stainless-clad"},
}


_PROFILES: Dict[str, PartTypeProfile] = {

    "mechanical_seal": PartTypeProfile(
        part_type="mechanical_seal",
        regime=REGIME_ANCHORED,        # parent = pump
        sourcing=SOURCING_MIXED,
        configurable=False,
        identity_anchor_question=(
            "What pump is it on — brand and model off the pump nameplate? "
            "And is there a seal cartridge tag or old-seal part number?"
        ),
        q2_template=(
            "What pump make/model is it on, and any old-part code? If visible: "
            "shaft size, and is it a cartridge or component seal, single or double?"
        ),
        # The component-dims half of the q2, asked when the parent identity is
        # ALREADY captured in `_component_of` (Goulds-3196 over-clarification fix).
        # Leads with the genuinely-undetermined component dims (shaft_size /
        # cartridge-vs-component / single-vs-double — the registry blocking_attrs
        # this clause phrases), then the old-part code (a COMPONENT identifier,
        # not the parent). No parent-identity ask, no "is it OEM?".
        q2_component_clause=(
            "What's the shaft size, and is it a cartridge or component seal, "
            "single or double? An old-part code helps too, if visible."
        ),
        blocking_attrs=[
            "shaft_size",
            "cartridge_vs_component",
            "single_vs_double",
            "face_material_class",
            "elastomer_product_cip_compatibility",
        ],
        refinement_attrs=[
            "seal_brand",
            "premium_face_upgrades",
        ],
        variant_selecting_attrs=[
            "shaft_size",
        ],
        inference_rules=_SANITARY_INFERENCE,
        nameplate_guidance=(
            "pump casing tag near the shaft; old seal's cartridge tag"
        ),
    ),

    "pump": PartTypeProfile(
        part_type="pump",
        regime=REGIME_DIRECT,
        sourcing=SOURCING_MIXED,        # OEM-leaning MIXED
        configurable=False,
        identity_anchor_question=(
            "What's the make and model on the pump nameplate "
            "(e.g., Alfa Laval LKH-20, Fristam FPX3542)?"
        ),
        q2_template=(
            "Pump type, connection size, and duty — flow/head or HP/RPM? "
            "And wetted material if it's product-contact."
        ),
        blocking_attrs=[
            "type",
            "connection_size",
            "hydraulic_duty",
            "wetted_material",
        ],
        refinement_attrs=[
            "brand_equivalence",
            "impeller_trim_exactness",
        ],
        variant_selecting_attrs=[
            "hydraulic_duty",
        ],
        inference_rules=_SANITARY_INFERENCE,
        nameplate_guidance="pump casing plate",
    ),

    "valve": PartTypeProfile(
        part_type="valve",
        regime=REGIME_DIRECT,           # if tagged, else spec-built
        sourcing=SOURCING_STANDARD,
        configurable=False,
        identity_anchor_question=(
            "Any make/model on the valve body or tag? If not: what type "
            "(ball/butterfly/gate), line size, connection, and is it sanitary?"
        ),
        q2_template=(
            "Type, size, connection, and body material? (And pressure class if known.)"
        ),
        blocking_attrs=[
            "type",
            "size",
            "class_or_cwp",
            "connection",
            "body_and_seat_material",
            "actuation",
        ],
        refinement_attrs=[
            "brand",
            "handle_actuator_accessories",
            "trim_upgrade",
        ],
        inference_rules={
            "sanitary": {"body": "316L", "connection": "Tri-Clamp"},
            "dairy":    {"body": "316L", "connection": "Tri-Clamp"},
            "food":     {"body": "316L", "connection": "Tri-Clamp"},
            "CIP":      {"body": "316L", "connection": "Tri-Clamp"},
        },
        nameplate_guidance="cast/stamped on the body",
    ),

    "sensor_instrument": PartTypeProfile(
        part_type="sensor_instrument",
        regime=REGIME_DIRECT,
        sourcing=SOURCING_OEM,
        configurable=True,             # order-code family — Phase 3 adds variant questions
        identity_anchor_question=(
            "What's the manufacturer and model on the instrument, or its loop tag? "
            "And what does it measure and over what range?"
        ),
        q2_template=(
            "What does it measure and over what range, what output "
            "(4-20mA / 0-10V / HART), and what process connection/thread?"
        ),
        blocking_attrs=[
            "measured_variable",
            "range",
            "output_type",
            "process_connection",
            "wetted_material",
            "hazardous_area_rating",
        ],
        refinement_attrs=[
            "display_option",
            "brand",
            "accuracy_above_requirement",
            "cable_connector_style",
        ],
        inference_rules={
            "sanitary": {"wetted": "316L", "connection": "Tri-Clamp"},
            "dairy":    {"wetted": "316L", "connection": "Tri-Clamp"},
            "food":     {"wetted": "316L", "connection": "Tri-Clamp"},
            "CIP":      {"wetted": "316L", "connection": "Tri-Clamp"},
        },
        nameplate_guidance=(
            "instrument tag/label; the extended order code is printed on the plate "
            "(note in guidance)"
        ),
    ),

    "motor_drive": PartTypeProfile(
        part_type="motor_drive",
        regime=REGIME_DIRECT,
        sourcing=SOURCING_STANDARD,
        configurable=False,
        identity_anchor_question=(
            "Can you read the motor nameplate — HP/kW, RPM, voltage, and frame?"
        ),
        q2_template=(
            "HP, RPM, voltage/phase, and frame? And is it VFD-fed (inverter duty)?"
        ),
        blocking_attrs=[
            "hp",
            "rpm",
            "voltage_phase",
            "frame",
            "enclosure",
            "mount",
            "inverter_duty_if_vfd_fed",
        ],
        refinement_attrs=[
            "brand",
            "efficiency_tier_above_minimum",
            "paint",
        ],
        variant_selecting_attrs=[
            "hp",
            "voltage_phase",
        ],
        inference_rules=_WASHDOWN_INFERENCE,
        nameplate_guidance="motor nameplate on the frame",
    ),
}


# ---------------------------------------------------------------------------
# Public access
# ---------------------------------------------------------------------------

def get_profile(part_type: Optional[str]) -> PartTypeProfile:
    """Return the PartTypeProfile for a part_type string, or the UNKNOWN sentinel.

    Lookup is case-insensitive and whitespace-tolerant. Any off-registry,
    None, empty, or unparseable input returns UNKNOWN_PROFILE — never raises.
    """
    if not isinstance(part_type, str) or not part_type:
        return UNKNOWN_PROFILE
    key = part_type.strip().lower()
    return _PROFILES.get(key, UNKNOWN_PROFILE)


def all_profiles() -> Dict[str, PartTypeProfile]:
    """Return a copy of the full registry (keyed by part_type). Pure data."""
    return dict(_PROFILES)


def is_known_type(part_type: Optional[str]) -> bool:
    """True iff part_type is one of the registry's known types (case-insensitive)."""
    if not isinstance(part_type, str) or not part_type:
        return False
    return part_type.strip().lower() in _PROFILES


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ANCHORED-component clarification helper — the parent-already-known case.
# ---------------------------------------------------------------------------
# When an ANCHORED component's parent identity is already captured in
# `_component_of` (the Goulds-3196 case), the intake must NOT re-ask the
# parent (the q2_template's first clause); it leads with the genuinely-
# undetermined component dims (the profile's q2_component_clause). A confidence
# floor guards a low-confidence parent capture so an uncertain one still asks.
# DIRECT types and the no-parent ANCHORED case keep the verbatim q2_template.
ANCHORED_PARENT_KNOWN_CONF = 60  # classifier "reasonably clear" floor (see §_CLASSIFIER_SYSTEM)


def is_anchored_parent_known(specs: dict) -> bool:
    """True when an ANCHORED component's parent identity is already captured at
    reasonable confidence — the condition under which the parent-identity half
    of the q2_template is suppressed. Returns False for DIRECT types, for an
    ANCHORED type with no `_component_of`, or when the classification confidence
    is below the floor (an uncertain parent capture should still be asked for)."""
    part_type = specs.get("_classified_type")
    if not isinstance(part_type, str) or not part_type:
        return False
    profile = get_profile(part_type)
    if profile.regime != REGIME_ANCHORED:
        return False
    component_of = specs.get("_component_of")
    if not isinstance(component_of, str) or not component_of.strip():
        return False
    try:
        conf = int(float(specs.get("_classified_confidence") or 0))
    except (TypeError, ValueError):
        conf = 0
    return conf >= ANCHORED_PARENT_KNOWN_CONF


def anchored_component_question(specs: dict) -> Optional[str]:
    """The clarification question for an ANCHORED component whose parent is
    already known: a short parent acknowledgement (so the user sees their
    stated parent was captured) followed by the profile's component-dims clause.
    Returns None when the parent is NOT known (caller falls back to the
    verbatim q2_template so the parent ask still fires). Never raises."""
    if not is_anchored_parent_known(specs):
        return None
    profile = get_profile(specs.get("_classified_type"))
    if not profile.q2_component_clause:
        return None
    parent = (specs.get("_component_of") or "").strip()
    return f"For the {parent}: {profile.q2_component_clause}"


# variant_selecting_attr -> real AssetSpecs field mapping
# ---------------------------------------------------------------------------
# A `variant_selecting_attr` is a registry-side label; the actual AssetSpecs
# fields it maps to can differ (e.g. motor_drive's "voltage_phase" is carried
# by the SEPARATE spec fields `voltage` and `phase`, not by any field named
# "voltage_phase"). The confirm_intake binding guard (api_server) checks
# whether a variant-selecting attr is ANSWERED post-ask — it MUST resolve
# through this table, never by checking the registry name as a literal spec
# key (that would make family-level requests permanently unconfirmable except
# via open_family — the ask-then-brick failure).
#
# INVARIANT: anyone adding an entry to a profile's `variant_selecting_attrs`
# MUST add its mapping here. The test suite asserts a mapping exists for every
# variant_selecting_attr, so a missing row fails loudly rather than silently
# bricking confirmations.
VARIANT_ATTR_TO_SPEC_FIELDS: Dict[str, tuple] = {
    "hp":             ("hp",),
    "voltage_phase":  ("voltage", "phase"),          # either field answers it
    "shaft_size":     ("shaft_size",),
    "bore_diameter":  ("bore_diameter",),
    "hydraulic_duty": ("gpm", "psi", "head", "hp"),  # any one duty signal answers it
}


def variant_attr_answered(specs: dict, attr: str) -> bool:
    """True when a variant-selecting attr is present in `specs` under any of its
    mapped real fields. ``specs`` is an AssetSpecs-shaped dict; null/placeholder
    values (None / "" / "Unknown" / "UNKNOWN-PN" / "N/A") do NOT count as
    answered. Returns False for an attr with no mapping row (fail-safe: an
    unmapped attr is treated as unanswered rather than bricking confirm — but
    the test suite asserts a mapping exists for every variant_selecting_attr,
    so a missing row is caught in dev)."""
    mapped = VARIANT_ATTR_TO_SPEC_FIELDS.get(attr)
    if not mapped:
        return False
    _null = {None, "", "null", "N/A", "Unknown", "UNKNOWN-PN", "none", "unknown"}
    return any(specs.get(f) not in _null for f in mapped)
