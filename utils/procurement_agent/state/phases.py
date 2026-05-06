"""
Phase enum and transition validator for ProcurementRun state machine.

Brief reference: Section 5 (state transitions) and Section 2 (workflow phases).
"""

import logging
from enum import Enum
from typing import Set

logger = logging.getLogger(__name__)


class Phase(str, Enum):
    """All legal states of a ProcurementRun, matching the brief Section 5 schema."""
    INTAKE = "intake"
    INVENTORY = "inventory"
    SOURCING = "sourcing"
    COMPARISON = "comparison"
    PENDING_FIRST_APPROVAL = "pending_first_approval"
    PENDING_SECOND_APPROVAL = "pending_second_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    FULFILLING = "fulfilling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


# Legal forward transitions. Every phase also allows → CANCELLED and → ERROR
# (added programmatically below) so this dict only lists the happy-path edges
# plus the error-recovery edge (ERROR → INTAKE).
_FORWARD: dict[Phase, Set[Phase]] = {
    Phase.INTAKE:                   {Phase.INVENTORY},
    Phase.INVENTORY:                {Phase.SOURCING},
    Phase.SOURCING:                 {Phase.COMPARISON},
    Phase.COMPARISON:               {Phase.PENDING_FIRST_APPROVAL},
    # Single-approver path:  pending_first_approval → approved
    # Dual-approver path:    pending_first_approval → pending_second_approval → approved
    # The Approval Rules Engine (Phase 3) chooses the path based on the dollar
    # threshold in the facility's approval config. Both paths are legal here.
    Phase.PENDING_FIRST_APPROVAL:   {Phase.PENDING_SECOND_APPROVAL, Phase.APPROVED},
    Phase.PENDING_SECOND_APPROVAL:  {Phase.APPROVED},
    Phase.APPROVED:                 {Phase.EXECUTING},
    Phase.EXECUTING:                {Phase.FULFILLING},
    Phase.FULFILLING:               {Phase.COMPLETED},
    Phase.COMPLETED:                set(),
    Phase.CANCELLED:                set(),
    Phase.ERROR:                    {Phase.INTAKE},  # allow retry from error state
}

# Build the full VALID_TRANSITIONS dict: forward edges + universal escape hatches.
_ESCAPE: Set[Phase] = {Phase.CANCELLED, Phase.ERROR}

VALID_TRANSITIONS: dict[Phase, Set[Phase]] = {
    phase: (_FORWARD[phase] | _ESCAPE) - {phase}
    for phase in Phase
}
# Terminal states cannot transition anywhere (not even to themselves).
VALID_TRANSITIONS[Phase.COMPLETED] = set()
VALID_TRANSITIONS[Phase.CANCELLED] = set()


def validate_transition(from_phase: Phase, to_phase: Phase) -> bool:
    """Return True if the transition is legal; log a warning and return False if not."""
    if to_phase in VALID_TRANSITIONS.get(from_phase, set()):
        return True
    logger.warning(
        "Invalid phase transition attempted: %s → %s", from_phase.value, to_phase.value
    )
    return False
