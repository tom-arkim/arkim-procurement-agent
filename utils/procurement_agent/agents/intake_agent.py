"""
Intake Agent — multimodal data extraction, sufficiency assessment, clarification.

Brief reference: Section 3.1, Section 8.1.

Rectification Sprint additions:
  - Fix 1: classify_by_units() — units-based classification override post-VLM extraction
  - Fix 3: assess_proceed_state() — asymmetric stop condition (mfg<70, pid≥80 proceeds with caveat)
"""

import base64
import json
import os
import re
import requests
from typing import Optional, Tuple

from utils.models import SourcingRun


def _intake_type_aware() -> bool:
    """Strict opt-in for the intake-type-aware redesign (guardrail 3). Only an
    explicit truthy token (1/true/yes/on) enables the new behavior; anything
    else (None, "", "0", "false", "no", junk) -> False, so the flag fails
    safe/closed and the intake is byte-identical to current behavior. Mirrors
    api_server._env_truthy / email_sender._env_truthy / sourcing_agent's demo gate.
    Read at call time (not import time) so a test/flip mid-process is honored."""
    return (os.environ.get("INTAKE_TYPE_AWARE") or "").strip().lower() in ("1", "true", "yes", "on")


def _detect_media_type(img_bytes: bytes) -> str:
    """Return Anthropic-accepted media_type string inferred from image magic bytes."""
    if img_bytes[:2] == b"\xff\xd8":
        return "image/jpeg"
    if img_bytes[:4] == b"\x89PNG":
        return "image/png"
    if img_bytes[:4] == b"RIFF" and img_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"  # safe default for unknown formats

# Per-category fields that must be populated before sourcing can start.
# Key: substring of detected_type (lowercased).
CATEGORY_REQUIRED_FIELDS: dict[str, list[str]] = {
    "motor":                    ["detected_type", "hp", "voltage", "frame", "rpm"],
    "vfd":                      ["detected_type", "voltage", "hp"],
    "variable frequency drive": ["detected_type", "voltage", "hp"],
    "mechanical seal":          ["detected_type", "shaft_size", "material_spec"],
    "bearing":                  ["detected_type", "bore_diameter"],
    "pressure sensor":          ["detected_type", "psi"],
    "valve":                    ["detected_type", "connection_size"],
}

SUFFICIENCY_THRESHOLD = 70  # both confidences must reach this in the symmetric case

# Hard cap on clarification turns: after this many non-sufficient clarification turns, the
# agent stops asking and commits to spec-based sourcing with whatever specs it has (so a
# vague / unanswerable intake never loops indefinitely). Counted as the turn number of the
# current clarification turn (1-indexed); when it reaches the cap, the turn commits instead
# of asking another question.
INTAKE_TURN_CAP = 3

_NULL_VALUES = {None, "", "null", "N/A", "Unknown", "UNKNOWN-PN", "none", "unknown"}

# Identity fields — when ANY of these is established, the part has an identifier (the opener
# question is skipped and spec-based commit is not "pure spec-based").
_IDENTITY_FIELDS = ("manufacturer", "model", "part_number")

# ---------------------------------------------------------------------------
# Fix 1 — Units-based classification override
# ---------------------------------------------------------------------------

# Each rule: (required_fields_set, new_detected_type, new_category, priority)
# Higher priority wins when multiple rules match.
UNIT_CLASSIFICATION_RULES: list[tuple] = [
    # Three motor signals → most specific label wins.
    (frozenset({"hp", "rpm", "frame"}), "3-Phase Electric Motor", "Equipment", 11),
    # Two motor signals — NEMA frame or RPM alone with HP is sufficient.
    (frozenset({"hp", "frame"}),        "Electric Motor",         "Equipment", 10),
    (frozenset({"hp", "rpm"}),          "Electric Motor",         "Equipment",  9),
    # hp+voltage alone is too ambiguous (pumps, compressors also carry both) — omitted.
    (frozenset({"gpm", "psi"}),         "Centrifugal Pump",       "Equipment",  8),
    (frozenset({"gpm", "head"}),        "Centrifugal Pump",       "Equipment",  7),
    (frozenset({"bore_diameter"}),      "Bearing",                "Part",       6),
    (frozenset({"shaft_size"}),         "Mechanical Seal",        "Part",       6),
]


def classify_by_units(specs: dict) -> tuple:
    """Return (detected_type, category, override_applied) based on present unit fields.

    Fires only when the current detected_type conflicts with what the units imply.
    Returns (None, None, False) when no override is warranted.
    """
    best_priority = -1
    best_type: Optional[str] = None
    best_cat:  Optional[str] = None

    for required_fields, new_type, new_cat, priority in UNIT_CLASSIFICATION_RULES:
        if priority <= best_priority:
            continue
        if all(specs.get(f) not in _NULL_VALUES for f in required_fields):
            best_priority = priority
            best_type = new_type
            best_cat  = new_cat

    if best_type is None:
        return None, None, False

    current_type = (specs.get("detected_type") or "").lower()
    # Skip if the key equipment word (last word of best_type) is already in the current type.
    best_base = best_type.split()[-1].lower()
    if best_base in current_type:
        return None, None, False

    return best_type, best_cat, True


# ---------------------------------------------------------------------------
# Fix 3 — Asymmetric stop condition
# ---------------------------------------------------------------------------

_PROCEED_CAVEAT = (
    "Manufacturer identity could not be confirmed with high confidence. "
    "The part type appears specific — sourcing will proceed with a broader search, "
    "but verify results against original equipment documentation."
)


def _first_missing_required_field(specs: dict) -> Optional[str]:
    detected = (specs.get("detected_type") or "").lower()
    # A confident part number uniquely identifies the part — its catalog entry fixes every
    # category DIMENSION (bore_diameter, shaft_size, material_spec…), so re-asking for them is
    # an over-ask (SKF 6205-2RS1 needs no bore; a Gusher seal needs no material). With a PN
    # present we skip the dimension requirements but still require detected_type (the category,
    # needed to source). Only a PART NUMBER qualifies — a model alone names a family that can
    # still have dimensioned variants. The no-PN path is untouched: spec-based sourcing genuinely
    # needs the dimensions to find equivalents without a catalog id. (part_id_confidence >= the
    # threshold is already guaranteed here — this is only reached from that branch of
    # assess_proceed_state.)
    pn_present = specs.get("part_number") not in _NULL_VALUES
    for key, fields in CATEGORY_REQUIRED_FIELDS.items():
        if key in detected:
            for field_name in fields:
                if pn_present and field_name != "detected_type":
                    continue   # PN is the spec — dimension fields are redundant
                if specs.get(field_name) in _NULL_VALUES:
                    return field_name
    return None


def _has_identity(specs: dict) -> bool:
    """True when any part identifier (manufacturer, model, or part number) is established."""
    return any(specs.get(k) not in _NULL_VALUES for k in _IDENTITY_FIELDS)


def _spec_based_ready(specs: dict) -> bool:
    """True when the intake can commit to SPEC-BASED sourcing without a manufacturer/PN.

    The manufacturer-refinement gate (over-questioning fix #4): once the part type is
    confidently known AND every category-required dimension is present AND no part identity
    (model or part number) is established, there is no more specific identifier to chase —
    sourcing should proceed spec-based instead of looping on the manufacturer question.
    A model or PN present means we still have a more specific handle, so this returns False
    and the existing caveat / full-confidence paths apply.
    """
    if _has_identity(specs):
        return False
    detected = (specs.get("detected_type") or "").lower()
    if not detected:
        return False
    for key, fields in CATEGORY_REQUIRED_FIELDS.items():
        if key in detected:
            # No PN here (guarded above), so the dimension requirements apply in full.
            return all(specs.get(f) not in _NULL_VALUES for f in fields)
    return False  # unknown category — can't confirm dims, keep asking


def _first_unasked_missing_field(specs: dict, asked: list) -> Optional[str]:
    """First category-required DIMENSION field (excluding detected_type itself) that is still
    missing AND has not already been asked. Used by the never-re-ask picker to find a fresh
    field when the assessor's nominated field was already asked. Returns None when the type
    has no category entry or every missing dim has already been asked."""
    detected = (specs.get("detected_type") or "").lower()
    if not detected:
        return None
    for key, fields in CATEGORY_REQUIRED_FIELDS.items():
        if key in detected:
            for f in fields:
                if f == "detected_type":
                    continue
                if f in asked:
                    continue
                if specs.get(f) in _NULL_VALUES:
                    return f
            return None
    return None


def _field_is_missing(specs: dict, field: str) -> bool:
    """Whether a given question-target field is genuinely still unanswered in specs."""
    if field == "part_type":
        return specs.get("detected_type") in _NULL_VALUES
    if field == "manufacturer":
        return specs.get("manufacturer") in _NULL_VALUES
    if field == "part_identity":
        return not _has_identity(specs)
    return specs.get(field) in _NULL_VALUES


# ---------------------------------------------------------------------------
# Family-level variant disambiguation (detect-and-ask) — the model-no-PN case.
# ---------------------------------------------------------------------------
# A `model` present with NO `part_number` names a FAMILY (e.g. "PowerFlex 40",
# "Goulds 3196", "SKF 6205"), not a specific variant. The clarify-gate keys on
# dimension-field PRESENCE with a PN escape hatch — so a model + any-filled-dim
# passes, and an extractor that hallucinates a rating for a "well-known" family
# satisfies the gate silently. No provenance is tracked, so the only
# hallucination-robust rule is block-regardless-of-extracted: ask the user to
# confirm/supply the variant-selecting attrs even if the extractor filled them.
# Generalizes by PART CLASS (never family strings) via a dual-source lookup:
# the registry profile's variant_selecting_attrs when a known profile is
# classified, else the legacy CATEGORY_REQUIRED_FIELDS dims for profile-less
# classes (e.g. bearing -> bore_diameter). The ask is recorded under the
# synthetic `_q2_variant` ledger key (de-dup, asked once); the BINDING against
# a silent confirm_intake bypass is in api_server.confirm_intake (T3), which
# reads the `_variant_disambig_pending` flag this gate sets.

# Human-readable labels for the variant-selecting attrs in the disambiguation
# question. Falls back to a prettified field name for unmapped attrs.
_VARIANT_ATTR_LABELS: dict[str, str] = {
    "hp":              "HP/kW rating",
    "voltage_phase":   "voltage/phase",
    "voltage":         "voltage",
    "shaft_size":      "shaft size",
    "bore_diameter":   "bore diameter",
    "seal_face_size":  "seal face size",
    "hydraulic_duty":  "duty (flow/head or HP/RPM)",
    "frame":           "NEMA frame",
    "rpm":             "RPM",
}


def _is_family_level(specs: dict) -> bool:
    """True when the request identifies a FAMILY, not a variant: a `model` is
    present AND no real `part_number`. A PN present is the escape hatch (the
    catalog number fixes the variant — `_first_missing_required_field` already
    skips dims on a PN). A request with neither model nor PN is the spec-based
    path, not family-level. Pure; no flag dependence."""
    has_model = specs.get("model") not in _NULL_VALUES
    has_pn = specs.get("part_number") not in _NULL_VALUES
    return has_model and not has_pn


def _variant_selecting_attrs_for(specs: dict) -> list:
    """The attrs that select a VARIANT within an identified family for the
    request's part class — dual-source, class-keyed (never family strings).

    Prefers the registry profile's `variant_selecting_attrs` when a known
    profile is classified (motor_drive -> [hp, voltage_phase]; mechanical_seal
    -> [shaft_size]; pump -> [hydraulic_duty]); returns [] for a known profile
    with no variant-selecting attrs (valve, sensor_instrument). Falls back to
    the legacy CATEGORY_REQUIRED_FIELDS dimension attrs (minus detected_type)
    for profile-less classes (e.g. bearing -> [bore_diameter]) — covers classes
    not yet in the 5-profile registry. Works flag-off (no _classified_type ->
    legacy fallback) and flag-on. Pure; never raises."""
    try:
        from utils.procurement_agent.part_type_registry import (
            get_profile, is_known_type,
        )
        classified = specs.get("_classified_type")
        if is_known_type(classified):
            profile = get_profile(classified)
            return list(profile.variant_selecting_attrs)
    except Exception:
        pass
    # Profile-less class (or flag-off / no classification) — legacy category map.
    detected = (specs.get("detected_type") or "").lower()
    for key, fields in CATEGORY_REQUIRED_FIELDS.items():
        if key in detected:
            return [f for f in fields if f != "detected_type"]
    return []


def _variant_disambiguation_question(specs: dict, vs_attrs: list) -> str:
    """Build the family-disambiguation question: names the model + the
    variant-selecting attrs, phrased as confirmation (block-regardless-of-
    extracted — the user may be confirming an extracted value or correcting a
    hallucinated one). Uses the registry's q2_template verbatim when available
    (INTAKE_TYPE_AWARE + known profile with a template); otherwise constructs
    a focused question off the vs_attrs (works flag-off / profile-less)."""
    model = specs.get("model") or "this model"
    try:
        if _intake_type_aware():
            from utils.procurement_agent.part_type_registry import (
                get_profile, is_known_type,
            )
            classified = specs.get("_classified_type")
            if is_known_type(classified):
                tpl = get_profile(classified).q2_template
                if tpl:
                    return tpl
    except Exception:
        pass
    labels = [_VARIANT_ATTR_LABELS.get(a, a.replace("_", " ")) for a in vs_attrs]
    if len(labels) == 1:
        attrs_phrase = labels[0]
    else:
        attrs_phrase = ", ".join(labels[:-1]) + " and " + labels[-1]
    return (
        f"{model} is a product family, not a specific variant — what {attrs_phrase} "
        f"do you need? (Confirm or correct the rating so we source the right unit, "
        f"or say you don't know and we'll source the family as-is.)"
    )


def family_disambig_block(specs: dict) -> Optional[dict]:
    """The confirm_intake binding guard's verdict (T3). Returns None when the
    confirm may proceed normally; otherwise a stable, frontend-consumable dict
    naming the variant-selecting attrs still unanswered, for the 422 detail:

        {"reason": "family_variant_unconfirmed",
         "model": <model>,
         "missing_attrs": [<registry attr>, ...],
         "missing_labels": [<human label>, ...]}

    Fires ONLY when ALL hold: family-level (`_is_family_level` — model present,
    no PN), a variant-selecting class (`_variant_selecting_attrs_for` non-empty),
    the variant ask is in flight (`_variant_disambig_pending` — set when the
    chat issued the ask, cleared when the user engaged with it), AND at least
    one variant-selecting attr is still unanswered (resolved through
    `part_type_registry.variant_attr_answered`, which maps registry attr names
    to real AssetSpecs fields — e.g. voltage_phase is answered when `voltage`
    OR `phase` is filled). The pending gate is what makes this
    hallucination-robust: an extractor that filled a rating without the user
    engaging keeps `_variant_disambig_pending=True`, so the guard still blocks
    even though the attr is "filled". Once the user sends a chat turn after the
    ask, the intake clears pending (resolved) and the guard falls back to the
    answered-attr check alone. Clean-PN, spec-described-no-model, and
    non-variant classes return None (byte-identical confirm path). Pure; no
    flag dependence; never raises. The `open_family` opt-in bypasses this guard
    at the call site (an honest open-family commit, not a silent one)."""
    if not _is_family_level(specs):
        return None
    vs_attrs = _variant_selecting_attrs_for(specs)
    if not vs_attrs:
        return None
    # If the variant ask was never issued (no pending flag, never set), the
    # family-level gate either already proceeded on its own dims or this is a
    # pre-fix run shape — do not brick a confirm the chat never gated. The guard
    # only binds once the chat has actually asked.
    if not specs.get("_variant_disambig_pending"):
        return None
    from utils.procurement_agent.part_type_registry import variant_attr_answered
    missing = [a for a in vs_attrs if not variant_attr_answered(specs, a)]
    if not missing:
        return None
    return {
        "reason": "family_variant_unconfirmed",
        "model": specs.get("model"),
        "missing_attrs": missing,
        "missing_labels": [
            _VARIANT_ATTR_LABELS.get(a, a.replace("_", " ")) for a in missing
        ],
    }


def assess_proceed_state(
    specs: dict, mfg_conf: float, part_conf: float
) -> tuple:
    """Return (state, missing_field, caveat_message).

    States:
        proceed_full_confidence          — both confident, required fields present
        proceed_with_manufacturer_caveat — mfg<70, pid≥80; proceed with banner
        proceed_spec_based               — mfg<70, pid≥70, type known + category dims
                                          present + no identity; commit spec-based
                                          (manufacturer-refinement gate, fix #4)
        needs_clarification              — both confident but a required field is missing
        blocked_need_part_id             — mfg≥70, pid<70
        blocked_need_either              — mfg<70 and pid<80
    """
    if mfg_conf >= SUFFICIENCY_THRESHOLD and part_conf >= SUFFICIENCY_THRESHOLD:
        missing = _first_missing_required_field(specs)
        if missing:
            return "needs_clarification", missing, None
        return "proceed_full_confidence", None, None

    # Fix #4 — manufacturer is NOT blocking once the part type is confidently known AND the
    # category-required dimensions are present AND there is no part identity (no model/PN).
    # Commit spec-based instead of looping on the manufacturer question forever. Slotted
    # before the caveat branch so a dims-present case prefers spec-based over the banner.
    if (mfg_conf < SUFFICIENCY_THRESHOLD
            and part_conf >= SUFFICIENCY_THRESHOLD
            and _spec_based_ready(specs)):
        return "proceed_spec_based", None, None

    if mfg_conf < SUFFICIENCY_THRESHOLD and part_conf >= 80:
        return "proceed_with_manufacturer_caveat", None, _PROCEED_CAVEAT

    if mfg_conf >= SUFFICIENCY_THRESHOLD and part_conf < SUFFICIENCY_THRESHOLD:
        return "blocked_need_part_id", "part_type", None

    return "blocked_need_either", "manufacturer", None


# ---------------------------------------------------------------------------
# LLM prompts
# ---------------------------------------------------------------------------

_EXTRACTION_SYSTEM = """You are an industrial procurement data extractor.
Given user input (text and/or images of equipment nameplates), extract structured specifications.

Return ONLY valid JSON with exactly these keys — no additional keys, no markdown fences:
{
  "manufacturer": string or null,
  "model": string or null,
  "part_number": string or null,
  "voltage": string or null,
  "category": "Part" or "Equipment",
  "hp": string or null,
  "serial_number": string or null,
  "description": string or null,
  "gpm": string or null,
  "psi": string or null,
  "frame": string or null,
  "phase": string or null,
  "detected_type": string or null,
  "rpm": string or null,
  "shaft_size": string or null,
  "bore_diameter": string or null,
  "seal_face_size": string or null,
  "connection_size": string or null,
  "material_spec": string or null,
  "use_case": string or null,
  "manufacturer_confidence": integer 0-100,
  "part_id_confidence": integer 0-100,
  "confidence_reasoning": string
}

category rules:
  "Equipment" — full assembled units: pump, motor, compressor, blower, conveyor
  "Part"      — replacement components: VFD, bearing, seal, relay, sensor, valve, belt, coupling

detected_type: specific equipment/part type e.g. "centrifugal pump", "induction motor", "mechanical seal"

manufacturer_confidence scoring:
  95+  manufacturer name is explicitly stated or clearly visible in image
  75-94 inferred from recognizable part number prefix or well-known model number
  50-74 guessed from partial information or context clues
  0-29  unknown or unrecognizable — manufacturer not determinable

part_id_confidence scoring:
  90+  exact part number explicitly provided and unambiguous
  70-89 part type clearly identified with key specs present
  50-69 part type inferred but missing key specs
  <50  part type uncertain — cannot reliably source without more info

Rules:
  - Extract ALL visible technical specs; be thorough
  - Set fields to null if not determinable — never invent or estimate values
  - description: one line summarizing what the item is, from the input
  - If prior specs are provided, merge carefully: only update fields with new information

COMPONENT-OF A PART (critical — do not attribute the parent's OEM to the component):
  When the input describes a COMPONENT of a named parent machine — a replacement
  part FOR a specific pump/motor/etc. (e.g. "impeller for a Goulds 3196",
  "seal kit for an Alfa Laval LKH-10", "drive chain for a Hytrol conveyor",
  "shaft sleeve for a Waukesha 060") — the `manufacturer` and `model` fields
  describe the COMPONENT'S OWN make, which is usually NOT known at intake.
  Set `manufacturer` and `model` to null in that case — do NOT copy the parent
  machine's OEM or model number into them. The parent machine's identity
  (brand + model) goes in `use_case` and/or `description` (e.g. use_case =
  "for Goulds 3196 pump"), NOT in manufacturer/model. The part being sourced is
  the COMPONENT (the impeller/seal/chain), not the parent machine — sourcing
  must never collapse to a bare query for the parent machine itself.
  A parent OEM/model is only the component's make when the input states the
  component is made BY that OEM (e.g. "Goulds 3196 impeller, OEM part" — still
  attribute only if the OEM is explicitly the component's manufacturer, not just
  the parent's). When in doubt, leave manufacturer/model null.

MULTIPLE DISTINCT PARTS -> JSON ARRAY:
If the input describes TWO OR MORE distinct parts/components — items that would each be
ordered separately (different part numbers, or different component types) — return a JSON
ARRAY of objects, one object per part, each using exactly the key set above. Example:
"SKF 6205-2RS1, FLOWSIC610" is a bearing AND a gas-flow analyzer -> return TWO objects.

ONE PART (even with several specs) -> ONE OBJECT:
A single part described with multiple attributes/specifications separated by commas is still
ONE object, NOT an array. Commas that separate SPECS (size, bore, material, thread, voltage,
HP, frame, rating, connection) belong to one part. Examples — each is ONE object:
  "1/2 inch ball valve, NPT threaded"        -> one valve
  "deep groove ball bearing, 25mm bore"      -> one bearing
  "460V 30HP induction motor, 256T frame"    -> one motor

Decide by part IDENTITY, not by the presence of commas: split ONLY when each side names a
separate component to source; never split one part's spec list. If a single part is given,
return a single object exactly as specified above.

When the user is responding to a specific clarification question from the agent
(indicated by "Agent asked: ..." in the input), treat their reply as an authoritative
direct answer to that question. Score confidence accordingly:
  - User provides a manufacturer name in response to "Who is the manufacturer?" → manufacturer_confidence = 95
  - User provides dimensions in response to a sizing question → high confidence in those fields
  - User provides a generic or evasive answer → moderate confidence
The prior context summary shows what specs were already extracted. Confidence in those
fields persists from the prior turn unless the user explicitly contradicts them."""

_CLARIFICATION_SYSTEM = """You are an industrial procurement assistant.
The user is trying to source a replacement part or piece of equipment.
Given the partial specs and the specific missing field, generate exactly ONE focused question.
The question must be specific and actionable — ask for precisely one piece of information.
Return ONLY the question text. No preamble, no explanation, no numbered list."""

# Default fallback questions when the LLM is unavailable.
_DEFAULT_QUESTIONS: dict[str, str] = {
    "manufacturer":    "Who is the manufacturer of this equipment? (Check the nameplate or documentation.)",
    "part_type":       "What type of part or equipment is this? (e.g., motor, pump, bearing, mechanical seal)",
    "hp":              "What is the horsepower (HP) rating shown on the nameplate?",
    "voltage":         "What is the operating voltage? (e.g., 460V, 230V, 208V)",
    "frame":           "What is the NEMA frame size? (usually a 3-4 digit code like 213T, 256T, 447T — found on the nameplate)",
    "rpm":             "What is the RPM (speed) rating shown on the nameplate? (typically 1200, 1800, or 3600)",
    "shaft_size":      "What is the shaft diameter? (e.g., 1.5 inch, 42mm — critical for seal sizing)",
    "bore_diameter":   "What is the bore diameter of this bearing? (the inner diameter in mm or inches)",
    "material_spec":   "What are the seal face materials? (e.g., Carbon/Silicon Carbide, Viton elastomers)",
    "psi":             "What is the operating pressure rating in PSI?",
    "connection_size": "What is the connection size or pipe diameter? (e.g., 2 inch NPT, DN50)",
    "detected_type":   "What type of equipment or part is this exactly? (e.g., centrifugal pump, induction motor, mechanical seal)",
    # Identity-first opener: asked verbatim (NOT via the haiku clarification generator) on the
    # first clarification turn when no part identity is established yet. Gets the user reading
    # the nameplate / old part markings before anything else — the single highest-value question.
    "part_identity":   ("Is there a manufacturer name, model, or part number visible — on the part "
                        "itself, its nameplate, or the equipment it's mounted on? If you can read a "
                        "plate or an old part's markings, type what you see."),
}


# ---------------------------------------------------------------------------
# IntakeAgent
# ---------------------------------------------------------------------------

class IntakeAgent:
    """Extracts structured AssetSpecs from user input and assesses sufficiency.

    Supports multi-turn clarification: on each call, prior specs from
    run.asset_specs_json are merged with newly extracted fields (new values win).
    """

    def __init__(self, anthropic_api_key: Optional[str] = None):
        self._api_key = anthropic_api_key

    # ------------------------------------------------------------------
    # Phase 1 — part-type classification (gated, internal `_-keys, fail-soft)
    # ------------------------------------------------------------------

    def _maybe_classify(self, merged: dict, text: str) -> None:
        """Classify the first message under INTAKE_TYPE_AWARE and store the result
        as internal `_`-prefixed keys on the merged specs. Fail-soft: any error or
        UNKNOWN result leaves the specs untouched (intake proceeds generically).
        The classifier uses the raw api.anthropic.com requests.post pattern (via
        its own default llm_call) — never ANTHROPIC_BASE_URL, never the SDK. A
        missing key -> the classifier returns UNKNOWN silently (no network)."""
        try:
            from utils.procurement_agent.part_type_classifier import classify_part_type
            from utils.procurement_agent.langsmith_client import traced_llm, record_llm_output
            # T7 — nested 'llm' span around the classifier call (parent = root trace).
            with traced_llm("classify_part_type",
                            model="claude-haiku-4-5-20251001",
                            parent=getattr(self, "_ls_root", None),
                            inputs={"text": text}):
                classification = classify_part_type(text)
                record_llm_output(getattr(self, "_ls_root", None),
                                  classification.as_dict())
        except Exception as exc:
            # Belt-and-suspenders: the classifier itself never raises, but a
            # defensive guard ensures an import/transport error can't crash intake.
            print(f"[IntakeAgent] part-type classification failed: {exc}")
            return
        merged["_classified_type"] = classification.part_type
        merged["_classified_regime"] = classification.regime
        merged["_component_of"] = classification.component_of
        merged["_classified_confidence"] = classification.confidence

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, run: SourcingRun, user_input: dict) -> dict:
        """Extract AssetSpecs from user_input and assess sufficiency.

        Args:
            run: current SourcingRun (provides prior context on follow-up turns)
            user_input: dict with keys:
                - "text": str — the user's chat message
                - "images": list[bytes] — uploaded image data (optional)
                - "force_proceed": bool — bypass sufficiency gate if True

        Returns:
            dict with:
                - "asset_specs": dict — merged AssetSpecs fields
                - "manufacturer_confidence": float 0-100
                - "part_id_confidence": float 0-100
                - "sufficient": bool
                - "follow_up_question": str | None
                - "manufacturer_caveat": str | None
                - "confidence_summary": dict
        """
        text           = user_input.get("text", "") or ""
        images         = user_input.get("images") or []
        force_proceed  = bool(user_input.get("force_proceed", False))
        prior_specs    = run.asset_specs_json or {}
        prior_question = user_input.get("prior_question")

        # T7 — open a LangSmith root trace around the intake run. Carries run_id
        # in metadata (the cross-HTTP-boundary filter convention). Offline-inert:
        # with LANGSMITH_API_KEY unset, trace() is a no-op context manager and
        # the body runs unchanged. The root span is stashed on `self._ls_root` so
        # the nested LLM-call sites (extraction / multimodal / clarification /
        # classify_part_type) can open `traced_llm` children attached to it
        # without changing their signatures. Cleared in `finally`. The sourcing
        # pipeline is NOT instrumented tonight (supervised follow-up).
        from utils.procurement_agent.langsmith_client import trace as _ls_trace
        self._ls_root = None
        with _ls_trace("intake run", run_id=getattr(run, "id", None),
                       inputs={"text": text, "has_images": bool(images)}) as self._ls_root:
            return self._run_body(run, user_input, text, images,
                                  force_proceed, prior_specs, prior_question)

    def _run_body(self, run: SourcingRun, user_input: dict, text: str, images: list,
                  force_proceed: bool, prior_specs: dict, prior_question: str) -> dict:
        """The intake run body, executed inside the T7 root LangSmith trace
        (`self._ls_root` is set by the caller and cleared on exit). All nested
        LLM calls open `traced_llm` children attached to that root."""
        # Extract from this turn's input
        if images:
            extracted = self._extract_multimodal(text, images, prior_specs)
        else:
            extracted = self._extract_text(text, prior_specs, prior_question=prior_question)

        # The extractor's contract is ONE spec object (a dict). A LIST means the LLM saw
        # MULTIPLE parts (it emits one object per part) — single-part intake can't merge that.
        # It is a describable user situation, NOT a server error: ask for one part at a time
        # (no silent data loss, no 502). A list-wrapped single part is unwrapped; an empty/
        # malformed list degrades to {} so the merge below asks for clarification, never crashes.
        if isinstance(extracted, list):
            parts = [p for p in extracted if isinstance(p, dict)]
            if len(parts) >= 2:
                return self._multi_part_response(prior_specs, parts)
            extracted = parts[0] if parts else {}
        elif not isinstance(extracted, dict):
            extracted = {}

        # Merge with prior specs — new non-null values win
        merged = dict(prior_specs)
        for k, v in extracted.items():
            if not isinstance(v, (list, dict)) and v not in _NULL_VALUES:
                merged[k] = v

        # Phase 1 — quantity capture (gated behind INTAKE_TYPE_AWARE). Inert when
        # the flag is off: zero new keys, byte-identical specs. When on, a stated
        # quantity ("I need 6 …") is captured, else defaulted to 1 with an internal
        # `_quantity_assumed` marker. The marker is `_`-prefixed so the existing
        # context-summary + RunDetail filters strip it; `quantity` itself surfaces.
        if _intake_type_aware():
            from utils.procurement_agent.quantity_capture import apply_quantity
            apply_quantity(merged, text)

        # Phase 1 — part-type classification (gated behind INTAKE_TYPE_AWARE). Run
        # ONCE, against the user's FIRST message (no prior specs yet), so the type
        # is known before the first clarification and the registry's q2_template
        # can drive the next question (T5). The result rides as internal `_`-keys
        # on asset_specs_json (filtered from context summary + RunDetail). A
        # classification failure (UNKNOWN) is non-fatal: intake proceeds exactly
        # as the current generic behavior (T4/T5 fall through on UNKNOWN). The
        # classifier's llm_call is fail-soft — it never raises into the pipeline.
        if _intake_type_aware() and not prior_specs and not images:
            self._maybe_classify(merged, text)

        # Fix 1: units-based classification override (runs after VLM merge)
        new_type, new_cat, override_applied = classify_by_units(merged)
        if override_applied:
            old_type = merged.get("detected_type")
            merged["detected_type"]          = new_type
            merged["category"]               = new_cat
            merged["classification_override"] = True
            print(f"[IntakeAgent] Units classification override: {old_type!r} -> {new_type!r}")

        prior_mfg_conf      = float(prior_specs.get("manufacturer_confidence") or 0)
        prior_part_conf     = float(prior_specs.get("part_id_confidence") or 0)
        extracted_mfg_conf  = float(extracted.get("manufacturer_confidence") or 0)
        extracted_part_conf = float(extracted.get("part_id_confidence") or 0)
        # Confidence is monotonic: answering a clarification question can only add information,
        # not remove previously-established confidence.
        mfg_conf  = max(prior_mfg_conf, extracted_mfg_conf)
        part_conf = max(prior_part_conf, extracted_part_conf)
        # Write floor-corrected values back so next turn's prior_specs has correct baseline
        merged["manufacturer_confidence"] = mfg_conf
        merged["part_id_confidence"]      = part_conf

        if force_proceed:
            return {
                "asset_specs":             merged,
                "manufacturer_confidence": mfg_conf,
                "part_id_confidence":      part_conf,
                "sufficient":              True,
                "follow_up_question":      None,
                "manufacturer_caveat":     None,
                "confidence_summary": {
                    "manufacturer_confidence": mfg_conf,
                    "part_id_confidence":      part_conf,
                    "forced":                  True,
                    "reasoning":               extracted.get("confidence_reasoning"),
                    "proceed_state":           "forced",
                    "caveat":                  None,
                },
            }

        # Fix 3: asymmetric stop condition
        state, missing_field, caveat = assess_proceed_state(merged, mfg_conf, part_conf)
        if state == "proceed_spec_based":
            merged["spec_based_sourcing"] = True

        # Family-level variant disambiguation (detect-and-ask) — runs AFTER the
        # base assessment and BEFORE the sufficient decision. A model present
        # with no PN names a FAMILY; for a variant-selecting class the intake
        # must ask for the variant-selecting attrs (HP/kW+voltage for a drive,
        # shaft size for a seal, ...) regardless of whether the extractor filled
        # them (block-regardless-of-extracted — no provenance exists, so this is
        # the only hallucination-robust rule). Asked once (`_q2_variant` de-dup);
        # the BINDING against a silent confirm_intake bypass is the
        # `_variant_disambig_pending` flag, read by api_server.confirm_intake (T3).
        # A PRIOR pending ask + this new user turn => the user engaged => resolved.
        if prior_specs.get("_variant_disambig_pending"):
            # The user sent a message after the variant ask — they engaged with
            # it (answered, corrected, or will opt into open-family via the
            # confirm affordance). Clear the pending flag so confirm_intake no
            # longer blocks on it. The de-dup (`_q2_variant` in asked) prevents
            # re-asking; the base assessment decides sufficiency from here.
            merged["_variant_disambig_pending"] = False
        elif "_q2_variant" not in (prior_specs.get("_asked_fields") or []):
            vs_attrs = _variant_selecting_attrs_for(merged)
            if vs_attrs and _is_family_level(merged):
                # Override to needs_clarification — the variant ask is due, even
                # if the base assessment said proceed (a hallucinated rating
                # must not satisfy the gate silently).
                state = "needs_clarification"
                missing_field = "variant_disambig"
                merged["spec_based_sourcing"] = False

        sufficient = state in (
            "proceed_full_confidence",
            "proceed_with_manufacturer_caveat",
            "proceed_spec_based",
        )

        follow_up = None
        commit_message = None
        if not sufficient:
            follow_up, commit_message = self._next_clarification(
                merged, prior_specs, missing_field or "detected_type"
            )
            if commit_message is not None:
                # Over-questioning fix #3: cap reached OR nothing left to ask — commit to
                # spec-based sourcing with the specs we have instead of looping.
                merged["spec_based_sourcing"] = True
                state = "forced_commit"
                sufficient = True
                follow_up = None

        # Carry the pending flag forward into the persisted specs. When the
        # variant ask is issued this turn, _next_clarification sets it True; when
        # the user engaged (cleared above) it is already False on merged; when
        # neither applies, inherit the prior value (a non-variant ask turn should
        # not silently clear a pending variant ask). The flag is the binding
        # signal for api_server.confirm_intake (T3): pending + unanswered variant
        # attrs -> 422 unless open_family.
        if "_variant_disambig_pending" not in merged:
            merged["_variant_disambig_pending"] = bool(
                prior_specs.get("_variant_disambig_pending")
            )
        return {
            "asset_specs":             merged,
            "manufacturer_confidence": mfg_conf,
            "part_id_confidence":      part_conf,
            "sufficient":              sufficient,
            "follow_up_question":      follow_up,
            "manufacturer_caveat":     caveat,
            "commit_message":          commit_message,
            "confidence_summary": {
                "manufacturer_confidence": mfg_conf,
                "part_id_confidence":      part_conf,
                "missing_field":           missing_field,
                "reasoning":               extracted.get("confidence_reasoning"),
                "proceed_state":           state,
                "caveat":                  caveat,
            },
        }

    @staticmethod
    def _multi_part_response(prior_specs: dict, parts: list[dict]) -> dict:
        """Honest intake result when the user described MULTIPLE parts (the extractor returned a
        list of >1). Returns the normal run() contract with sufficient=False so send_message
        replies with the message at HTTP 200 — the run stays in intake, no specs are merged from
        the multi-part list (no silent data loss), and it is NOT a 502 (not a server error).

        The parsed parts ride along as `multi_part_specs` (the full per-part extraction dicts) so
        a caller CAN fan them into N seeded cards — still merging NOTHING into THIS run's specs."""
        prior_specs = prior_specs or {}
        n = len(parts)
        mfg_conf = float(prior_specs.get("manufacturer_confidence") or 0)
        part_conf = float(prior_specs.get("part_id_confidence") or 0)
        message = (
            f"It looks like you've described several parts ({n} detected). "
            f"Please submit one part at a time for now."
        )
        return {
            "asset_specs":             dict(prior_specs),   # unchanged — nothing merged from the list
            "manufacturer_confidence": mfg_conf,
            "part_id_confidence":      part_conf,
            "sufficient":              False,
            "follow_up_question":      message,
            "manufacturer_caveat":     None,
            "multi_part_specs":        list(parts),          # the N per-part dicts, for fan-out
            "confidence_summary": {
                "manufacturer_confidence": mfg_conf,
                "part_id_confidence":      part_conf,
                "missing_field":           None,
                "reasoning":               f"Multiple parts detected ({n}) — single-part intake.",
                "proceed_state":           "multi_part_detected",
                "caveat":                  None,
            },
        }

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _build_context_summary(self, prior_specs: dict) -> dict:
        """Return all populated prior spec fields including confidence scores.

        Confidence scores are intentionally included so the LLM knows what's already
        established and can score new information relative to that baseline. Internal
        `_`-prefixed keys (turn counter, asked-fields ledger) are excluded so they never
        leak into the extractor context.
        """
        return {
            k: v for k, v in prior_specs.items()
            if not isinstance(v, (list, dict)) and v not in _NULL_VALUES
            and not k.startswith("_")
        }

    def _pn_prefix_hint(self, text: str, prior_specs: dict) -> Optional[tuple]:
        """Scan user text and prior specs for a known PN prefix.

        Returns (manufacturer, confidence) if a match is found, None otherwise.
        Checks prior_specs.part_number first (most reliable), then raw text tokens.
        """
        from utils.brand_intelligence import lookup_manufacturer_from_pn

        existing_pn = prior_specs.get("part_number")
        if existing_pn and existing_pn not in _NULL_VALUES:
            mfg = lookup_manufacturer_from_pn(existing_pn)
            if mfg:
                return (mfg, 92)

        tokens = re.findall(r'\b[A-Z0-9][A-Z0-9\-\.]{3,}\b', text.upper())
        for token in tokens:
            mfg = lookup_manufacturer_from_pn(token)
            if mfg:
                return (mfg, 92)

        return None

    def _apply_pn_manufacturer(self, obj: dict, prior_specs: Optional[dict] = None) -> None:
        """Infer a manufacturer for ONE extracted part from a known PN prefix, applied PER-PART
        after extraction (not as a whole-input prompt bias — which used to collapse a multi-part
        input to one part). Reuses _pn_prefix_hint's prefix matching, scoped to this object's OWN
        identifier fields (part_number / model / description) plus, when given, prior_specs (the
        established PN from a prior turn — for a single-part follow-up). Sets manufacturer only
        when the LLM left it missing or lower-confidence than the prefix lookup; mutates in place.
        Pass prior_specs ONLY for the single-part case — for a multi-part array each element
        carries its own PN, and a prior single PN must not re-bias the others."""
        if not isinstance(obj, dict):
            return
        own_text = " ".join(str(obj.get(k) or "") for k in ("part_number", "model", "description"))
        hint = self._pn_prefix_hint(own_text, prior_specs or {})
        if not hint:
            return
        mfg_name, mfg_conf = hint
        llm_mfg = obj.get("manufacturer") or ""
        llm_conf = float(obj.get("manufacturer_confidence") or 0)
        if llm_mfg in _NULL_VALUES or llm_conf < mfg_conf:
            obj["manufacturer"] = mfg_name
            obj["manufacturer_confidence"] = mfg_conf

    def _extract_text(self, text: str, prior_specs: dict, prior_question: str | None = None) -> dict:
        if not self._api_key:
            return self._fallback_extract(prior_specs)

        # NOTE: the pn-prefix manufacturer hint is NOT injected into the prompt — the LLM's
        # array/attribute rule decides part-count UNBIASED; the manufacturer is inferred PER-PART
        # after extraction (via _apply_pn_manufacturer). A whole-input hint used to collapse a
        # multi-part input to one part.
        context = ""
        if prior_specs:
            summary = self._build_context_summary(prior_specs)
            if prior_question:
                context = (
                    f"Previously extracted specs:\n{json.dumps(summary)}\n\n"
                    f"Agent asked: \"{prior_question}\"\n"
                    f"User replied: "
                )
            else:
                context = f"Previously extracted specs:\n{json.dumps(summary)}\n\nUser input: "

        try:
            from utils.procurement_agent.langsmith_client import traced_llm, record_llm_output
            # T7 — nested 'llm' span around the text-extraction call (parent = root trace).
            with traced_llm("extract_text", model="claude-sonnet-4-6",
                            parent=getattr(self, "_ls_root", None),
                            inputs={"text": text, "has_prior": bool(prior_specs)}):
                resp = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key":           self._api_key,
                        "anthropic-version":   "2023-06-01",
                        "content-type":        "application/json",
                    },
                    json={
                        "model":      "claude-sonnet-4-6",
                        "max_tokens": 1024,
                        "system":     _EXTRACTION_SYSTEM,
                        "messages":   [{"role": "user", "content": f"{context}{text}"}],
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                raw_text = resp.json()["content"][0]["text"]
                record_llm_output(getattr(self, "_ls_root", None), raw_text)
            extracted = self._parse_llm_json(raw_text)
            # Infer the manufacturer PER-PART from a known PN prefix (prefix lookup beats LLM
            # inference for known families). A single dict -> apply once, considering the prior
            # PN too; a LIST (multi-part) -> apply to EACH element on its OWN PN (no prior re-bias).
            if isinstance(extracted, dict):
                self._apply_pn_manufacturer(extracted, prior_specs)
            elif isinstance(extracted, list):
                for part in extracted:
                    self._apply_pn_manufacturer(part)
            return extracted
        except Exception as exc:
            print(f"[IntakeAgent] Text extraction failed: {exc}")
            return self._fallback_extract(prior_specs)

    def _extract_multimodal(self, text: str, images: list, prior_specs: dict) -> dict:
        if not self._api_key:
            return self._fallback_extract(prior_specs)

        # See _extract_text: no pn-prefix hint in the prompt — inferred PER-PART after extraction.
        content: list = []

        for img_bytes in images[:4]:
            media_type = _detect_media_type(img_bytes)
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            content.append({
                "type":   "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            })

        context = ""
        if prior_specs:
            summary = {k: v for k, v in prior_specs.items()
                       if not isinstance(v, (list, dict))
                       and k not in ("manufacturer_confidence", "part_id_confidence",
                                     "confidence_reasoning")
                       and not k.startswith("_") and v not in _NULL_VALUES}
            context = f"Previously extracted specs:\n{json.dumps(summary)}\n\n"

        content.append({
            "type": "text",
            "text": (
                f"{context}Extract all equipment/part specifications visible in the "
                f"image(s) and from the text below.\n\n{text or '(no additional text)'}"
            ),
        })

        try:
            from utils.procurement_agent.langsmith_client import traced_llm, record_llm_output
            # T7 — nested 'llm' span around the multimodal extraction call.
            with traced_llm("extract_multimodal", model="claude-sonnet-4-6",
                            parent=getattr(self, "_ls_root", None),
                            inputs={"text": text, "n_images": len(images)}):
                resp = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key":           self._api_key,
                        "anthropic-version":   "2023-06-01",
                        "content-type":        "application/json",
                    },
                    json={
                        "model":      "claude-sonnet-4-6",
                        "max_tokens": 1024,
                        "system":     _EXTRACTION_SYSTEM,
                        "messages":   [{"role": "user", "content": content}],
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                raw_text = resp.json()["content"][0]["text"]
                record_llm_output(getattr(self, "_ls_root", None), raw_text)
            extracted = self._parse_llm_json(raw_text)
            # Per-part manufacturer inference (see _extract_text): dict -> once (with prior PN);
            # list -> each element on its own PN.
            if isinstance(extracted, dict):
                self._apply_pn_manufacturer(extracted, prior_specs)
            elif isinstance(extracted, list):
                for part in extracted:
                    self._apply_pn_manufacturer(part)
            return extracted
        except Exception as exc:
            detail = ""
            if hasattr(exc, "response") and exc.response is not None:
                detail = f" — API response: {exc.response.text[:300]}"
            print(f"[IntakeAgent] Multimodal extraction failed: {exc}{detail}")
            return self._fallback_extract(prior_specs)

    def _fallback_extract(self, prior_specs: dict) -> dict:
        return {
            **(prior_specs or {}),
            "manufacturer_confidence": 0,
            "part_id_confidence":      0,
            "confidence_reasoning":    "No API key — extraction skipped",
        }

    @staticmethod
    def _parse_llm_json(raw: str) -> dict:
        raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw)
        try:
            return json.loads(raw)
        except Exception:
            return {"manufacturer_confidence": 0, "part_id_confidence": 0,
                    "confidence_reasoning": "JSON parse failed"}

    # ------------------------------------------------------------------
    # Clarification generation
    # ------------------------------------------------------------------

    _COMMIT_MESSAGE = (
        "I'll search on the specs we have; I couldn't confirm the exact manufacturer "
        "— you can refine from the results."
    )

    def _pick_question_field(self, specs: dict, asked: list, missing_field: str) -> tuple:
        """Choose which field to ask about this turn, never re-asking a field already asked
        and still unanswered. Returns (field, do_commit): when do_commit is True there is
        nothing fresh left to ask and the caller should commit to spec-based sourcing."""
        # 1. The assessor's nominated field, if it hasn't been asked yet.
        if missing_field and missing_field not in asked:
            return missing_field, False
        # 2. A different still-missing category-required dimension that hasn't been asked.
        alt = _first_unasked_missing_field(specs, asked)
        if alt:
            return alt, False
        # 3. Broader identity/type fields for the blocked (no-category) case.
        for f in ("part_type", "manufacturer", "part_identity"):
            if f not in asked and _field_is_missing(specs, f):
                return f, False
        # 4. Nothing fresh to ask — commit spec-based rather than re-ask.
        return None, True

    def _next_clarification(self, merged: dict, prior_specs: dict, missing_field: str) -> tuple:
        """Decide this clarification turn's question OR a spec-based commit.

        Returns (follow_up_question, commit_message): exactly one is non-None.
        Implements the three over-questioning fixes:
          - identity-first opener (#1): first clarification turn + no identity → ask the
            part_identity question verbatim (not via the haiku generator).
          - never re-ask (#2): a field already asked-and-still-unanswered is skipped for a
            fresh missing field, or, if none remains, the turn commits.
          - hard cap (#3): when the clarification-turn counter reaches INTAKE_TURN_CAP and
            the intake is still not sufficient, commit to spec-based sourcing.

        Internal `_`-prefixed ledger keys (`_asked_fields`, `_intake_turns`) ride on
        asset_specs_json (the existing state vehicle — no new column) and persist across
        turns; they are filtered out of the extractor context and the frontend specs display.
        """
        prior_turns = int(prior_specs.get("_intake_turns") or 0)
        asked = list(prior_specs.get("_asked_fields") or [])
        this_turn = prior_turns + 1   # 1-indexed clarification turn counter

        # Fix #3 — hard cap: this turn reaches the cap and the intake is still insufficient.
        if this_turn >= INTAKE_TURN_CAP:
            merged["_asked_fields"] = asked
            merged["_intake_turns"] = this_turn
            return None, self._COMMIT_MESSAGE

        # Family-level variant disambiguation — fires when the base assessment
        # overrode to needs_clarification with missing_field="variant_disambig"
        # (a model present + no PN + a variant-selecting class + the ask not yet
        # issued). Issues the family-disambiguation question (registry q2_template
        # when available, else a constructed confirm-the-rating question), records
        # the `_q2_variant` de-dup key, and sets `_variant_disambig_pending` so
        # confirm_intake (T3) can block a silent bypass until the user engages.
        # Block-regardless-of-extracted: the question is asked even if the
        # extractor filled the variant attrs (no provenance — the user must
        # confirm/correct). Placed AFTER the cap so a runaway family ask still
        # terminates at the turn cap; placed BEFORE the q2_template/identity
        # paths so a family-level ask takes precedence over the generic walk.
        if missing_field == "variant_disambig" and "_q2_variant" not in asked:
            vs_attrs = _variant_selecting_attrs_for(merged)
            if vs_attrs and _is_family_level(merged):
                asked.append("_q2_variant")
                merged["_asked_fields"] = asked
                merged["_intake_turns"] = this_turn
                merged["_variant_disambig_pending"] = True
                return _variant_disambiguation_question(merged, vs_attrs), None

        # Phase 2 — type-aware Q2 (gated behind INTAKE_TYPE_AWARE). When identity
        # is absent AND a known part type was classified, ask the registry's
        # q2_template VERBATIM (no LLM phrasing call) instead of the generic
        # missing-field walk. Respects the `_asked_fields` de-dup (asked once) and
        # the INTAKE_TURN_CAP (cap checked above). UNKNOWN type -> current generic
        # behavior (falls through to the identity-first opener / picker below).
        # The q2_template is recorded under a synthetic field key `_q2_<type>` so
        # the never-re-ask ledger prevents repeats without colliding with real
        # spec fields (it is `_-prefixed -> filtered from context/display).
        if _intake_type_aware():
            from utils.procurement_agent.part_type_registry import get_profile, is_known_type
            classified_type = merged.get("_classified_type")
            if (is_known_type(classified_type)
                    and not _has_identity(merged)
                    and "_q2_asked" not in asked):
                profile = get_profile(classified_type)
                if profile.q2_template:
                    asked.append("_q2_asked")
                    merged["_asked_fields"] = asked
                    merged["_intake_turns"] = this_turn
                    return profile.q2_template, None

        # Fix #1 — identity-first opener: first clarification turn + no part identity.
        if prior_turns == 0 and not _has_identity(merged):
            field = "part_identity"
        else:
            field, do_commit = self._pick_question_field(merged, asked, missing_field)
            if do_commit:
                merged["_asked_fields"] = asked
                merged["_intake_turns"] = this_turn
                return None, self._COMMIT_MESSAGE

        # part_identity is asked verbatim (never via the haiku generator).
        if field == "part_identity":
            follow_up = _DEFAULT_QUESTIONS["part_identity"]
        else:
            follow_up = self._generate_clarification(merged, field)

        # Fix #2 — record the asked field so it is never re-asked while still unanswered.
        asked.append(field)
        merged["_asked_fields"] = asked
        merged["_intake_turns"] = this_turn
        return follow_up, None

    def _generate_clarification(self, specs: dict, missing_field: str) -> str:
        if not self._api_key:
            return _DEFAULT_QUESTIONS.get(missing_field,
                                          f"Can you provide the {missing_field.replace('_', ' ')}?")

        specs_summary = {k: v for k, v in specs.items()
                         if not isinstance(v, (list, dict)) and v not in _NULL_VALUES
                         and not k.startswith("_")
                         and k not in ("manufacturer_confidence", "part_id_confidence",
                                       "confidence_reasoning")}
        user_msg = (
            f"Current specs: {json.dumps(specs_summary)}\n"
            f"Missing field: {missing_field}\n"
            f"Equipment type: {specs.get('detected_type', 'unknown')}\n\n"
            f"Ask one focused question to obtain the {missing_field.replace('_', ' ')} value."
        )

        try:
            from utils.procurement_agent.langsmith_client import traced_llm, record_llm_output
            # T7 — nested 'llm' span around the clarification-generation call.
            with traced_llm("generate_clarification", model="claude-haiku-4-5-20251001",
                            parent=getattr(self, "_ls_root", None),
                            inputs={"missing_field": missing_field}):
                resp = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key":           self._api_key,
                        "anthropic-version":   "2023-06-01",
                        "content-type":        "application/json",
                    },
                    json={
                        "model":      "claude-haiku-4-5-20251001",
                        "max_tokens": 150,
                        "system":     _CLARIFICATION_SYSTEM,
                        "messages":   [{"role": "user", "content": user_msg}],
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                clar_text = resp.json()["content"][0]["text"].strip()
                record_llm_output(getattr(self, "_ls_root", None), clar_text)
            return clar_text
        except Exception as exc:
            print(f"[IntakeAgent] Clarification generation failed: {exc}")
            return _DEFAULT_QUESTIONS.get(missing_field,
                                          f"Can you provide the {missing_field.replace('_', ' ')}?")
