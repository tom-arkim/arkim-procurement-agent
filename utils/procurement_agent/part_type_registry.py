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
        (no LLM phrasing call) when identity is absent and the type is known.
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
