"""
Acceptance tests for the per-type question registry (T1).

The registry is PURE DATA — importing it and calling its accessors must make no
network / LLM calls and read no secrets. These tests lock that contract plus the
brief's §4 structural invariants: 5 types present (the 4 priority + motor_drive,
per the brief's "5 profiles" clause), each with non-empty blocking_attrs and
q2_template, blocking/refinement disjoint, and an UNKNOWN sentinel for
off-registry input.
"""

from __future__ import annotations

import sys
import socket

import pytest

from utils.procurement_agent.part_type_registry import (
    KNOWN_PART_TYPES,
    UNKNOWN_PART_TYPE,
    UNKNOWN_PROFILE,
    PartTypeProfile,
    all_profiles,
    get_profile,
    is_known_type,
)


# ---------------------------------------------------------------------------
# Purity — importing the registry opens no sockets and makes no calls.
# We assert the registry module registered no socket-level activity by checking
# that a known sentinel function is untouched (the strongest stdlib assertion
# available without a transport mock). The real transport-mock assertion lives
# in the classifier tests (T2); here we assert the data-only contract directly:
# every accessor returns immediately with no network dependency.
# ---------------------------------------------------------------------------

def _assert_no_network_during(callable_):
    """Run `callable_` and assert no socket was opened while it ran."""
    orig_socket = socket.socket

    class _ProbedSocket:
        def __init__(self, *a, **k):
            raise AssertionError(
                "registry accessor opened a network socket — registry must be pure data"
            )

    socket.socket = _ProbedSocket  # type: ignore[assignment]
    try:
        return callable_()
    finally:
        socket.socket = orig_socket  # type: ignore[assignment]


def test_registry_loads_and_is_pure_data():
    """Registry loads; accessors complete without opening any socket."""
    profiles = _assert_no_network_during(lambda: all_profiles())
    assert isinstance(profiles, dict) and len(profiles) >= 4
    _assert_no_network_during(lambda: get_profile("valve"))
    _assert_no_network_during(lambda: get_profile("nonsense_gibberish"))
    _assert_no_network_during(lambda: is_known_type("pump"))


# ---------------------------------------------------------------------------
# All 4 priority types present (brief §4: mechanical_seal + pump = priority pair,
# plus valve + sensor_instrument; motor_drive is the 5th profile — all carried).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ptype", ["mechanical_seal", "pump", "valve", "sensor_instrument"])
def test_priority_types_present(ptype):
    p = get_profile(ptype)
    assert p.part_type == ptype, f"{ptype} missing from registry"
    assert p is not UNKNOWN_PROFILE, f"{ptype} resolved to UNKNOWN sentinel"


def test_motor_drive_also_present():
    """Brief §4: 'That is 5 profiles' — motor_drive is carried too (do-not-add-more)."""
    p = get_profile("motor_drive")
    assert p.part_type == "motor_drive"
    assert p is not UNKNOWN_PROFILE


def test_known_part_types_match_registry_keys():
    assert set(KNOWN_PART_TYPES) == set(all_profiles().keys())
    assert len(KNOWN_PART_TYPES) == 5


# ---------------------------------------------------------------------------
# Each type has non-empty blocking_attrs and q2_template (the q2_template is the
# batched highest-entropy blocking question the question-flow engine asks verbatim).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ptype", list(KNOWN_PART_TYPES))
def test_each_type_has_blocking_attrs_and_q2(ptype):
    p = get_profile(ptype)
    assert p.blocking_attrs, f"{ptype} has no blocking_attrs"
    assert isinstance(p.blocking_attrs, list) and len(p.blocking_attrs) >= 3
    assert p.q2_template, f"{ptype} has no q2_template"
    assert isinstance(p.q2_template, str) and len(p.q2_template.strip()) > 10


# ---------------------------------------------------------------------------
# blocking_attrs and refinement_attrs must be DISJOINT per type — a field is
# either blocking or refinement, never both.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ptype", list(KNOWN_PART_TYPES))
def test_blocking_and_refinement_are_disjoint(ptype):
    p = get_profile(ptype)
    overlap = set(p.blocking_attrs) & set(p.refinement_attrs)
    assert not overlap, f"{ptype}: blocking/refinement overlap on {overlap}"


# ---------------------------------------------------------------------------
# UNKNOWN sentinel for off-registry / gibberish / empty input — never raises.
# ---------------------------------------------------------------------------

def test_unknown_gibberish_returns_unknown_sentinel():
    p = get_profile("unknown_gibberish_xyz")
    assert p is UNKNOWN_PROFILE
    assert p.part_type == UNKNOWN_PART_TYPE
    assert p.blocking_attrs == []
    assert p.q2_template == ""


@pytest.mark.parametrize("bad", [None, "", "   ", "hydraulic_hose", "light_fixture", 123])
def test_off_registry_and_empty_returns_unknown(bad):
    p = get_profile(bad)  # type: ignore[arg-type]
    assert p is UNKNOWN_PROFILE


def test_get_profile_is_case_insensitive_and_whitespace_tolerant():
    assert get_profile("Valve").part_type == "valve"
    assert get_profile("  MECHANICAL_SEAL  ").part_type == "mechanical_seal"
    assert get_profile("PUMP").part_type == "pump"


# ---------------------------------------------------------------------------
# Inference rules are well-formed dicts: token -> {attr: value}.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ptype", list(KNOWN_PART_TYPES))
def test_inference_rules_well_formed(ptype):
    p = get_profile(ptype)
    for token, mapping in p.inference_rules.items():
        assert isinstance(token, str) and token
        assert isinstance(mapping, dict) and mapping
        for attr, val in mapping.items():
            assert isinstance(attr, str) and attr
            assert val is not None and str(val).strip() != ""


# ---------------------------------------------------------------------------
# Regime / sourcing values are constrained to the documented enums.
# ---------------------------------------------------------------------------

_VALID_REGIMES = {"DIRECT", "ANCHORED"}
_VALID_SOURCING = {"STANDARD", "OEM", "MIXED"}


@pytest.mark.parametrize("ptype", list(KNOWN_PART_TYPES))
def test_regime_and_sourcing_are_valid_enums(ptype):
    p = get_profile(ptype)
    assert p.regime in _VALID_REGIMES, f"{ptype}: bad regime {p.regime!r}"
    assert p.sourcing in _VALID_SOURCING, f"{ptype}: bad sourcing {p.sourcing!r}"


def test_mechanical_seal_is_anchored_to_pump():
    """The F1 component-of-parent case: mechanical_seal regime is ANCHORED."""
    p = get_profile("mechanical_seal")
    assert p.regime == "ANCHORED"


def test_sensor_instrument_is_configurable():
    """sensor_instrument is the order-code family — marked configurable=True tonight
    (Phase 3 adds variant questions; tonight just marked)."""
    p = get_profile("sensor_instrument")
    assert p.configurable is True


def test_only_sensor_instrument_is_configurable():
    cfg = [pt for pt in KNOWN_PART_TYPES if get_profile(pt).configurable]
    assert cfg == ["sensor_instrument"]


# ---------------------------------------------------------------------------
# Profiles are frozen / immutable data — the registry is not mutated at runtime.
# ---------------------------------------------------------------------------

def test_profiles_are_frozen():
    p = get_profile("valve")
    assert isinstance(p, PartTypeProfile)
    # frozen dataclass: setattr raises FrozenInstanceError
    with pytest.raises(Exception):
        p.q2_template = "mutated"  # type: ignore[misc]
