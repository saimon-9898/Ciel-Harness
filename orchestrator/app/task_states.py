"""Deterministic Task state machine (Phase 3).

Defines the exact set of task states and the only legal transitions between
them. The Task engine (``task_service``) and the API enforce this machine;
arbitrary status assignment is never allowed.

The transition table is the single source of truth: adding a state here does
not make it reachable until an explicit transition edge exists.
"""

from __future__ import annotations

# The ten Phase 3 states, exactly as specified. Order matters only for
# documentation and determinism in error messages.
TASK_STATES: tuple[str, ...] = (
    "CREATED",
    "PLANNED",
    "QUEUED",
    "RUNNING",
    "WAITING_FOR_AGENT",
    "WAITING_FOR_REVIEW",
    "WAITING_FOR_APPROVAL",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
)

# Named constants for the states most commonly referenced in code.
CREATED = "CREATED"
PLANNED = "PLANNED"
QUEUED = "QUEUED"
RUNNING = "RUNNING"
WAITING_FOR_AGENT = "WAITING_FOR_AGENT"
WAITING_FOR_REVIEW = "WAITING_FOR_REVIEW"
WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

TERMINAL_STATES: frozenset[str] = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

# States from which cancellation is legal (any non-terminal state).
CANCELLABLE_STATES: frozenset[str] = frozenset(
    {
        "CREATED",
        "PLANNED",
        "QUEUED",
        "RUNNING",
        "WAITING_FOR_AGENT",
        "WAITING_FOR_REVIEW",
        "WAITING_FOR_APPROVAL",
    }
)

# Legal transitions: current state -> set of allowed next states.
# Terminal states map to the empty set: no outgoing edges.
#
# One edge was added to the specification's recommended list so that
# WAITING_FOR_APPROVAL is reachable at all: WAITING_FOR_REVIEW ->
# WAITING_FOR_APPROVAL (a review outcome can require human approval). The
# spec's list only ever used WAITING_FOR_APPROVAL as a source state; without
# this edge the state would be unreachable dead weight.
TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"PLANNED", "CANCELLED"}),
    "PLANNED": frozenset({"QUEUED", "CANCELLED"}),
    "QUEUED": frozenset({"RUNNING", "CANCELLED"}),
    "RUNNING": frozenset({"WAITING_FOR_AGENT", "FAILED", "CANCELLED"}),
    "WAITING_FOR_AGENT": frozenset({"RUNNING", "WAITING_FOR_REVIEW", "FAILED", "CANCELLED"}),
    "WAITING_FOR_REVIEW": frozenset({"COMPLETED", "WAITING_FOR_APPROVAL", "FAILED", "CANCELLED"}),
    "WAITING_FOR_APPROVAL": frozenset({"RUNNING", "FAILED", "CANCELLED"}),
    "COMPLETED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
}


class TaskStateError(ValueError):
    """Raised when a transition is not permitted by the state machine."""


def is_valid_state(state: str) -> bool:
    """Return True when ``state`` is one of the known task states."""
    return state in TASK_STATES


def is_terminal(state: str) -> bool:
    """Return True when ``state`` is a terminal (COMPLETED/FAILED/CANCELLED) state."""
    return state in TERMINAL_STATES


def is_cancellable(state: str) -> bool:
    """Return True when a task in ``state`` may transition to CANCELLED."""
    return "CANCELLED" in allowed_transitions(state)


def allowed_transitions(state: str) -> frozenset[str]:
    """Return the set of states reachable from ``state``.

    Unknown states yield the empty set (nothing is allowed).
    """
    if not is_valid_state(state):
        return frozenset()
    return TASK_TRANSITIONS[state]


def validate_transition(current: str, new: str) -> None:
    """Raise :class:`TaskStateError` unless ``current -> new`` is legal.

    This is the only gate the API and service use; no code path may set a
    task's status without passing through this function first.
    """
    if current == new:
        raise TaskStateError(f"task is already in state {current!r}")
    if new not in allowed_transitions(current):
        raise TaskStateError(f"invalid task transition: {current!r} -> {new!r}")
