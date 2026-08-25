"""Unit tests for the strict agent data contracts (Phase 4).

Every contract is bounded: request/response fields have explicit limits and
opaque handles are frozen so the engine cannot be tricked into mutating a
provider reference.
"""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agent_contracts import (
    AgentExecutionState,
    AgentHealth,
    AgentHealthState,
    AgentResult,
    AgentStatusResult,
    AgentTaskHandle,
    AgentTaskRequest,
)
from app.agent_providers import AgentProvider


def _task_request(**overrides):
    base = {
        "task_id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "objective": "implement the feature",
        "instructions": "keep it simple",
        "constraints": ["no deps"],
        "success_criteria": ["tests pass"],
    }
    base.update(overrides)
    return base


def test_task_request_accepts_valid_payload():
    req = AgentTaskRequest.model_validate(_task_request())
    assert req.objective == "implement the feature"
    assert req.constraints == ["no deps"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"objective": ""},
        {"objective": "   "},
        {"objective": "x" * 1001},
        {"instructions": "x" * 4001},
        {"constraints": ["x"] * 51},
        {"success_criteria": ["x"] * 51},
    ],
)
def test_task_request_rejects_bad_payload(overrides):
    with pytest.raises(ValidationError):
        AgentTaskRequest.model_validate(_task_request(**overrides))


def test_task_request_defaults():
    req = AgentTaskRequest.model_validate(
        {"task_id": uuid.uuid4(), "project_id": uuid.uuid4(), "objective": "ok"}
    )
    assert req.instructions is None
    assert req.constraints == []
    assert req.success_criteria == []


# ---------- task handle ----------


def test_task_handle_is_frozen():
    handle = AgentTaskHandle(
        task_id=uuid.uuid4(),
        provider=AgentProvider.OPENHANDS,
        reference="session-123",
    )
    with pytest.raises(ValidationError):
        handle.reference = "mutated"


def test_task_handle_rejects_blank_or_overlong_reference():
    with pytest.raises(ValidationError):
        AgentTaskHandle(task_id=uuid.uuid4(), provider=AgentProvider.CODEX, reference="")
    with pytest.raises(ValidationError):
        AgentTaskHandle(task_id=uuid.uuid4(), provider=AgentProvider.CODEX, reference="r" * 513)


# ---------- execution state and results ----------


def test_execution_states_are_closed_set():
    assert {e.value for e in AgentExecutionState} == {
        "queued",
        "running",
        "completed",
        "failed",
        "cancelled",
        "unknown",
    }


def test_status_result_accepts_valid_state():
    result = AgentStatusResult(task_id=uuid.uuid4(), state=AgentExecutionState.RUNNING)
    assert result.state == AgentExecutionState.RUNNING
    assert result.detail == ""


@pytest.mark.parametrize(
    "overrides",
    [{"detail": "x" * 2001}, {"state": "nonsense"}],
)
def test_status_result_rejects_bad_payload(overrides):
    base = {"task_id": uuid.uuid4(), "state": AgentExecutionState.RUNNING}
    base.update(overrides)
    with pytest.raises(ValidationError):
        AgentStatusResult.model_validate(base)


def test_agent_result_accepts_output_and_error():
    result = AgentResult(
        task_id=uuid.uuid4(),
        state=AgentExecutionState.COMPLETED,
        output="done",
        error=None,
    )
    assert result.output == "done"
    assert result.error is None


# ---------- health ----------


def test_health_states_are_closed_set():
    assert {e.value for e in AgentHealthState} == {
        "available",
        "unavailable",
        "error",
        "not_configured",
        "unsupported",
    }


def test_health_accepts_not_configured():
    health = AgentHealth(
        agent_id=uuid.uuid4(),
        provider=AgentProvider.GEMINI,
        status=AgentHealthState.NOT_CONFIGURED,
        detail="not connected",
        checked_at=datetime.now(UTC),
    )
    assert health.status == AgentHealthState.NOT_CONFIGURED


def test_health_rejects_overlong_detail():
    with pytest.raises(ValidationError):
        AgentHealth(
            agent_id=uuid.uuid4(),
            provider=AgentProvider.GEMINI,
            status=AgentHealthState.NOT_CONFIGURED,
            detail="x" * 2001,
            checked_at=datetime.now(UTC),
        )
