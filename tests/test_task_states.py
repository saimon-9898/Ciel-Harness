"""Unit tests for the deterministic Task state machine.

These verify the transition table is exactly as specified: every listed edge
is legal, every other edge is rejected, and terminal states are absorbing.
"""

import pytest

from app.task_states import (
    CANCELLED,
    COMPLETED,
    CREATED,
    FAILED,
    PLANNED,
    QUEUED,
    RUNNING,
    TASK_STATES,
    TASK_TRANSITIONS,
    TERMINAL_STATES,
    WAITING_FOR_AGENT,
    WAITING_FOR_APPROVAL,
    WAITING_FOR_REVIEW,
    TaskStateError,
    allowed_transitions,
    is_cancellable,
    is_terminal,
    is_valid_state,
    validate_transition,
)


def test_exactly_ten_states_as_specified():
    assert set(TASK_STATES) == {
        CREATED,
        PLANNED,
        QUEUED,
        RUNNING,
        WAITING_FOR_AGENT,
        WAITING_FOR_REVIEW,
        WAITING_FOR_APPROVAL,
        COMPLETED,
        FAILED,
        CANCELLED,
    }
    assert len(TASK_STATES) == 10


def test_terminal_states_are_exactly_three():
    assert TERMINAL_STATES == {COMPLETED, FAILED, CANCELLED}
    for state in TASK_STATES:
        assert is_terminal(state) == (state in TERMINAL_STATES)


@pytest.mark.parametrize(
    "current,next_state",
    [
        (CREATED, PLANNED),
        (CREATED, CANCELLED),
        (PLANNED, QUEUED),
        (PLANNED, CANCELLED),
        (QUEUED, RUNNING),
        (QUEUED, CANCELLED),
        (RUNNING, WAITING_FOR_AGENT),
        (RUNNING, FAILED),
        (RUNNING, CANCELLED),
        (WAITING_FOR_AGENT, RUNNING),
        (WAITING_FOR_AGENT, WAITING_FOR_REVIEW),
        (WAITING_FOR_AGENT, FAILED),
        (WAITING_FOR_AGENT, CANCELLED),
        (WAITING_FOR_REVIEW, COMPLETED),
        (WAITING_FOR_REVIEW, FAILED),
        (WAITING_FOR_REVIEW, CANCELLED),
        (WAITING_FOR_APPROVAL, RUNNING),
        (WAITING_FOR_APPROVAL, FAILED),
        (WAITING_FOR_APPROVAL, CANCELLED),
    ],
)
def test_specification_transitions_are_legal(current, next_state):
    # validate_transition must not raise
    validate_transition(current, next_state)
    assert next_state in allowed_transitions(current)


def test_review_to_approval_edge_makes_approval_reachable():
    # The one edge added to the spec's list so WAITING_FOR_APPROVAL is
    # reachable at all (the spec only used it as a source state).
    validate_transition(WAITING_FOR_REVIEW, WAITING_FOR_APPROVAL)
    assert WAITING_FOR_APPROVAL in allowed_transitions(WAITING_FOR_REVIEW)


def test_full_happy_path_chain_is_legal():
    chain = [
        (CREATED, PLANNED),
        (PLANNED, QUEUED),
        (QUEUED, RUNNING),
        (RUNNING, WAITING_FOR_AGENT),
        (WAITING_FOR_AGENT, WAITING_FOR_REVIEW),
        (WAITING_FOR_REVIEW, COMPLETED),
    ]
    for current, next_state in chain:
        validate_transition(current, next_state)


def test_cancellation_chain_is_legal():
    for state in TASK_STATES:
        if state in TERMINAL_STATES:
            continue
        assert is_cancellable(state)
        validate_transition(state, CANCELLED)


@pytest.mark.parametrize(
    "current,next_state",
    [
        # Reverse edges are illegal
        (PLANNED, CREATED),
        (QUEUED, PLANNED),
        (RUNNING, QUEUED),
        # Skipping stages is illegal
        (CREATED, QUEUED),
        (CREATED, RUNNING),
        (CREATED, COMPLETED),
        (PLANNED, RUNNING),
        (QUEUED, COMPLETED),
        # Terminal states are absorbing: nothing leaves them
        (COMPLETED, RUNNING),
        (COMPLETED, FAILED),
        (COMPLETED, CANCELLED),
        (FAILED, RUNNING),
        (FAILED, COMPLETED),
        (FAILED, CANCELLED),
        (CANCELLED, RUNNING),
        (CANCELLED, COMPLETED),
        (CANCELLED, PLANNED),
        # Approval only follows review
        (CREATED, WAITING_FOR_APPROVAL),
        (RUNNING, WAITING_FOR_APPROVAL),
        (PLANNED, WAITING_FOR_APPROVAL),
        # Review only follows WAITING_FOR_AGENT
        (QUEUED, WAITING_FOR_REVIEW),
        (PLANNED, WAITING_FOR_REVIEW),
        # Running-to-review bypasses the agent handoff
        (RUNNING, WAITING_FOR_REVIEW),
        # Fail is not reachable from pre-execution states
        (CREATED, FAILED),
        (PLANNED, FAILED),
        (QUEUED, FAILED),
    ],
)
def test_illegal_transitions_are_rejected(current, next_state):
    with pytest.raises(TaskStateError):
        validate_transition(current, next_state)


def test_self_transition_always_rejected():
    for state in TASK_STATES:
        with pytest.raises(TaskStateError):
            validate_transition(state, state)


def test_unknown_state_rejected():
    assert not is_valid_state("BOGUS")
    assert allowed_transitions("BOGUS") == frozenset()
    with pytest.raises(TaskStateError):
        validate_transition("BOGUS", CREATED)
    with pytest.raises(TaskStateError):
        validate_transition(CREATED, "BOGUS")


def test_full_transition_matrix_is_exact():
    """Every (state -> state) pair is legal iff declared in the table."""
    for current in TASK_STATES:
        for target in TASK_STATES:
            if target in TASK_TRANSITIONS[current]:
                validate_transition(current, target)
            else:
                with pytest.raises(TaskStateError):
                    validate_transition(current, target)


def test_terminal_states_have_no_outgoing_edges():
    for state in TERMINAL_STATES:
        assert allowed_transitions(state) == frozenset()
        assert not is_cancellable(state)
