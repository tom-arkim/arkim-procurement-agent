"""
Tests for Phase enum and transition validator.
"""

import pytest
from utils.procurement_agent.state.phases import Phase, VALID_TRANSITIONS, validate_transition


def test_all_phases_have_string_value():
    for phase in Phase:
        assert isinstance(phase.value, str)
        assert len(phase.value) > 0


def test_phase_enum_from_string():
    assert Phase("intake") is Phase.INTAKE
    assert Phase("completed") is Phase.COMPLETED


def test_happy_path_transitions_are_valid():
    happy_path = [
        (Phase.INTAKE, Phase.INVENTORY),
        (Phase.INVENTORY, Phase.SOURCING),
        (Phase.SOURCING, Phase.COMPARISON),
        (Phase.COMPARISON, Phase.PENDING_FIRST_APPROVAL),
        (Phase.PENDING_FIRST_APPROVAL, Phase.APPROVED),
        (Phase.PENDING_FIRST_APPROVAL, Phase.PENDING_SECOND_APPROVAL),
        (Phase.PENDING_SECOND_APPROVAL, Phase.APPROVED),
        (Phase.APPROVED, Phase.EXECUTING),
        (Phase.EXECUTING, Phase.FULFILLING),
        (Phase.FULFILLING, Phase.COMPLETED),
    ]
    for from_p, to_p in happy_path:
        assert validate_transition(from_p, to_p), f"Expected {from_p}→{to_p} to be valid"


def test_any_phase_can_cancel():
    non_terminal = [p for p in Phase if p not in (Phase.COMPLETED, Phase.CANCELLED)]
    for phase in non_terminal:
        assert validate_transition(phase, Phase.CANCELLED), f"{phase} should allow → CANCELLED"


def test_any_phase_can_error():
    # All phases except terminal ones (and ERROR itself) can transition to ERROR.
    eligible = [p for p in Phase if p not in (Phase.COMPLETED, Phase.CANCELLED, Phase.ERROR)]
    for phase in eligible:
        assert validate_transition(phase, Phase.ERROR), f"{phase} should allow → ERROR"


def test_skip_phase_is_invalid():
    # Cannot jump from INTAKE directly to SOURCING.
    assert not validate_transition(Phase.INTAKE, Phase.SOURCING)


def test_backwards_transition_is_invalid():
    assert not validate_transition(Phase.SOURCING, Phase.INTAKE)
    assert not validate_transition(Phase.APPROVED, Phase.COMPARISON)


def test_terminal_phase_has_no_transitions():
    assert VALID_TRANSITIONS[Phase.COMPLETED] == set()
    assert VALID_TRANSITIONS[Phase.CANCELLED] == set()


def test_error_phase_can_retry():
    # ERROR → INTAKE allows recovery.
    assert validate_transition(Phase.ERROR, Phase.INTAKE)


def test_validate_transition_returns_false_for_nonsense():
    # Invalid phase object would cause KeyError, but with correct phases it just returns False.
    assert not validate_transition(Phase.COMPLETED, Phase.INTAKE)
    assert not validate_transition(Phase.INTAKE, Phase.COMPLETED)  # skip too far
