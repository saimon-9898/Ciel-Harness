"""Wire-protocol tests for the real OpenHandsAdapter (Phase 5).

These exercise the actual :class:`OpenHandsAdapter` code against a fake
OpenHands Cloud API server over ``httpx``'s ``MockTransport``, proving the
adapter speaks the real V1 wire protocol (endpoints, headers, payload shapes,
status translation) without any network access.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fake_openhands import FakeOpenHandsServer, error_transport, timeout_transport

from app.adapters.openhands import OpenHandsAdapter
from app.agent_contracts import (
    AgentExecutionState,
    AgentHealthState,
    AgentTaskRequest,
)
from app.agent_errors import (
    AgentCancellationError,
    AgentMalformedResponseError,
    AgentProviderError,
    AgentTimeoutError,
    ProviderNotConfiguredError,
)

API_KEY = "test-openhands-key"
BASE_URL = "https://openhands.example.test"
TASK_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()


def _request() -> AgentTaskRequest:
    return AgentTaskRequest(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        objective="Create a file named phase5-proof.txt containing PHASE5_OK",
        instructions="Do not touch anything else.",
        constraints=["no destructive commands"],
        success_criteria=["file exists with exact content"],
    )


def _adapter(server: FakeOpenHandsServer, **kwargs) -> OpenHandsAdapter:
    client = httpx.Client(transport=httpx.MockTransport(server.handle))
    return OpenHandsAdapter(
        base_url=BASE_URL,
        api_key=API_KEY,
        repository="acme/demo",
        branch="main",
        http_client=client,
        poll_interval=0.001,
        **kwargs,
    )


# ---- start_task ------------------------------------------------------------


def test_start_task_posts_verified_payload_and_returns_handle():
    server = FakeOpenHandsServer()
    adapter = _adapter(server)

    handle = adapter.start_task(_request())

    assert handle.task_id == TASK_ID
    assert handle.provider.value == "openhands"
    assert handle.reference == server.latest_conversation_id()

    assert len(server.start_payloads) == 1
    payload = server.start_payloads[0]
    # The exact documented V1 shape: an initial_message with text content that
    # composes the whole task brief (objective, instructions, constraints,
    # success criteria).
    content = payload["initial_message"]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    text = content[0]["text"]
    for fragment in (
        "Create a file named phase5-proof.txt containing PHASE5_OK",
        "Instructions:\nDo not touch anything else.",
        "Constraints:\n- no destructive commands",
        "Success criteria:\n- file exists with exact content",
    ):
        assert fragment in text
    assert payload["selected_repository"] == "acme/demo"
    assert payload["selected_branch"] == "main"
    assert payload["trigger"] == "openhands_api"
    assert payload["title"] == f"task {TASK_ID}"
    # Auth headers: the reference header is X-Access-Token; Bearer is sent
    # too for compatibility.
    start_request = server.requests[0]
    assert start_request.headers["X-Access-Token"] == API_KEY
    assert start_request.headers["Authorization"] == f"Bearer {API_KEY}"


def test_start_task_accepts_immediately_ready_start():
    server = FakeOpenHandsServer(start_statuses=["READY"])
    adapter = _adapter(server)
    handle = adapter.start_task(_request())
    assert handle.reference == server.latest_conversation_id()
    # No start-task polls needed when the first response is already READY.
    assert len(server.requests) == 1


def test_start_task_polls_until_ready():
    server = FakeOpenHandsServer(start_statuses=["WORKING", "WAITING_FOR_SANDBOX", "READY"])
    adapter = _adapter(server)
    handle = adapter.start_task(_request())
    assert handle.reference == server.latest_conversation_id()
    # POST + 2 polls (WORKING, then WAITING_FOR_SANDBOX -> next returns READY).
    assert len(server.requests) == 3


def test_start_task_requires_configured_provider():
    adapter = OpenHandsAdapter(base_url=BASE_URL, api_key=None)
    with pytest.raises(ProviderNotConfiguredError):
        adapter.start_task(_request())


def test_start_task_requires_repository():
    server = FakeOpenHandsServer()
    client = httpx.Client(transport=httpx.MockTransport(server.handle))
    adapter = OpenHandsAdapter(
        base_url=BASE_URL, api_key=API_KEY, repository=None, http_client=client
    )
    with pytest.raises(ProviderNotConfiguredError):
        adapter.start_task(_request())


def test_start_task_authentication_failure():
    server = FakeOpenHandsServer(require_auth=True, api_key="different-key")
    adapter = _adapter(server)
    with pytest.raises(AgentProviderError, match="authentication"):
        adapter.start_task(_request())


def test_start_task_provider_http_error():
    server = FakeOpenHandsServer(start_http_status=500)
    adapter = _adapter(server)
    with pytest.raises(AgentProviderError):
        adapter.start_task(_request())


def test_start_task_timeout():
    adapter = OpenHandsAdapter(
        base_url=BASE_URL,
        api_key=API_KEY,
        repository="acme/demo",
        branch="main",
        http_client=httpx.Client(transport=timeout_transport()),
        timeout=1.0,
    )
    with pytest.raises(AgentTimeoutError):
        adapter.start_task(_request())


def test_start_task_start_status_error():
    server = FakeOpenHandsServer(start_statuses=["ERROR"])
    adapter = _adapter(server)
    with pytest.raises(AgentProviderError):
        adapter.start_task(_request())


def test_start_task_malformed_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    adapter = OpenHandsAdapter(
        base_url=BASE_URL,
        api_key=API_KEY,
        repository="acme/demo",
        branch="main",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(AgentMalformedResponseError):
        adapter.start_task(_request())


def test_start_task_connection_error():
    adapter = OpenHandsAdapter(
        base_url=BASE_URL,
        api_key=API_KEY,
        repository="acme/demo",
        branch="main",
        http_client=httpx.Client(
            transport=error_transport(httpx.ConnectError("refused", request=None))
        ),
    )
    with pytest.raises(AgentProviderError):
        adapter.start_task(_request())


# ---- get_status ------------------------------------------------------------


def test_get_status_finished_maps_completed():
    server = FakeOpenHandsServer(execution_statuses=["finished"])
    adapter = _adapter(server)
    handle = adapter.start_task(_request())
    status = adapter.get_status(handle)
    assert status.state is AgentExecutionState.COMPLETED
    assert status.task_id == TASK_ID


def test_get_status_running_maps_running():
    server = FakeOpenHandsServer(execution_statuses=["running"])
    adapter = _adapter(server)
    handle = adapter.start_task(_request())
    status = adapter.get_status(handle)
    assert status.state is AgentExecutionState.RUNNING


def test_get_status_error_maps_failed():
    server = FakeOpenHandsServer(execution_statuses=["error"])
    adapter = _adapter(server)
    handle = adapter.start_task(_request())
    status = adapter.get_status(handle)
    assert status.state is AgentExecutionState.FAILED


def test_get_status_stuck_maps_failed():
    server = FakeOpenHandsServer(execution_statuses=["stuck"])
    adapter = _adapter(server)
    handle = adapter.start_task(_request())
    status = adapter.get_status(handle)
    assert status.state is AgentExecutionState.FAILED


def test_get_status_waiting_confirmation_maps_running_blocked():
    server = FakeOpenHandsServer(execution_statuses=["waiting_for_confirmation"])
    adapter = _adapter(server)
    handle = adapter.start_task(_request())
    status = adapter.get_status(handle)
    assert status.state is AgentExecutionState.RUNNING
    assert "waiting" in status.detail


def test_get_status_missing_sandbox_maps_failed():
    server = FakeOpenHandsServer()
    adapter = _adapter(server)
    handle = adapter.start_task(_request())
    server.mark_conversation_error()
    status = adapter.get_status(handle)
    assert status.state is AgentExecutionState.FAILED


def test_get_status_unknown_status_maps_unknown():
    server = FakeOpenHandsServer(execution_statuses=["banana"])
    adapter = _adapter(server)
    handle = adapter.start_task(_request())
    status = adapter.get_status(handle)
    assert status.state is AgentExecutionState.UNKNOWN


def test_get_status_malformed_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"sandbox_status": "RUNNING"}])

    adapter = OpenHandsAdapter(
        base_url=BASE_URL,
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    from app.agent_contracts import AgentTaskHandle

    handle = AgentTaskHandle(task_id=TASK_ID, provider="openhands", reference="conv-1")
    status = adapter.get_status(handle)
    assert status.state is AgentExecutionState.UNKNOWN


# ---- get_result ------------------------------------------------------------


def test_get_result_finished_never_inferred_from_http():
    # A 200 with execution_status running must NOT be reported as success.
    server = FakeOpenHandsServer(execution_statuses=["running"])
    adapter = _adapter(server)
    handle = adapter.start_task(_request())
    result = adapter.get_result(handle)
    assert result.state is AgentExecutionState.RUNNING
    assert result.output is None


def test_get_result_finished_reports_completed():
    server = FakeOpenHandsServer(execution_statuses=["finished"])
    adapter = _adapter(server)
    handle = adapter.start_task(_request())
    result = adapter.get_result(handle)
    assert result.state is AgentExecutionState.COMPLETED
    assert "finished" in (result.output or "")


def test_get_result_failed_reports_error():
    server = FakeOpenHandsServer(execution_statuses=["error"])
    adapter = _adapter(server)
    handle = adapter.start_task(_request())
    result = adapter.get_result(handle)
    assert result.state is AgentExecutionState.FAILED
    assert result.error


# ---- cancel_task -----------------------------------------------------------


def test_cancel_task_truthfully_unsupported():
    server = FakeOpenHandsServer()
    adapter = _adapter(server)
    handle = adapter.start_task(_request())
    with pytest.raises(AgentCancellationError, match="unsupported"):
        adapter.cancel_task(handle)


# ---- check_health ----------------------------------------------------------


def test_check_health_not_configured():
    adapter = OpenHandsAdapter(base_url=BASE_URL, api_key=None)
    health = adapter.check_health(uuid.uuid4())
    assert health.status is AgentHealthState.NOT_CONFIGURED


def test_check_health_available_on_real_probe():
    server = FakeOpenHandsServer()
    adapter = _adapter(server)
    health = adapter.check_health(uuid.uuid4())
    assert health.status is AgentHealthState.AVAILABLE
    # The probe made a real authenticated request.
    assert server.requests


def test_check_health_auth_failure_is_unavailable_not_available():
    server = FakeOpenHandsServer(require_auth=True, api_key="different-key")
    adapter = _adapter(server)
    health = adapter.check_health(uuid.uuid4())
    assert health.status is AgentHealthState.UNAVAILABLE


def test_check_health_server_error_is_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    adapter = OpenHandsAdapter(
        base_url=BASE_URL,
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    health = adapter.check_health(uuid.uuid4())
    assert health.status is AgentHealthState.ERROR


def test_check_health_timeout_is_error():
    adapter = OpenHandsAdapter(
        base_url=BASE_URL,
        api_key=API_KEY,
        http_client=httpx.Client(transport=timeout_transport()),
        timeout=1.0,
    )
    health = adapter.check_health(uuid.uuid4())
    assert health.status is AgentHealthState.ERROR


# ---- secrets ---------------------------------------------------------------


def test_api_key_never_appears_in_errors():
    server = FakeOpenHandsServer(start_http_status=422, start_http_body={"detail": "bad"})
    adapter = _adapter(server)
    with pytest.raises(AgentProviderError) as exc_info:
        adapter.start_task(_request())
    assert API_KEY not in str(exc_info.value)
