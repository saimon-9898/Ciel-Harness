"""OpenHands Cloud API adapter (Phase 5 real integration).

Targets the **OpenHands Cloud API V1**, verified against the authoritative
documentation (docs.openhands.dev, 2026-08-25). Nothing here is guessed:

- ``POST /api/v1/app-conversations`` starts a conversation asynchronously and
  returns a *start task* (``id``, ``status``, eventual ``app_conversation_id``).
- ``GET /api/v1/app-conversations/start-tasks?ids=ID`` polls the start task
  until ``READY`` (``app_conversation_id`` populated) or ``ERROR``.
- ``GET /api/v1/app-conversations?ids=ID`` polls the conversation; the
  ``execution_status`` field reports ``idle``/``running``/``paused``/
  ``waiting_for_confirmation``/``finished``/``error``/``stuck``/``deleting``.
- Auth: the API reference authenticates with an ``X-Access-Token`` header;
  the overview guide also shows ``Authorization: Bearer``. Both are sent.
- There is **no documented cancellation endpoint**, so ``cancel_task``
  truthfully raises :class:`AgentCancellationError`.

The runtime is asynchronous and session-based. The adapter never blocks an
HTTP request on the agent's work: ``start_task`` returns an opaque handle
(the ``app_conversation_id``) and ``get_status`` reports the provider's own
execution state. Success is only reported when the provider itself reports
``finished`` -- never inferred from an HTTP 200.

Provider-specific concepts (conversation ids, sandbox ids, start-task
statuses) stay inside this module. The rest of the system sees only the
provider-independent contracts (``AgentTaskHandle``, ``AgentStatusResult``,
``AgentResult``, ``AgentHealth``).
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from ..agent_contracts import (
    AgentExecutionState,
    AgentHealth,
    AgentHealthState,
    AgentResult,
    AgentStatusResult,
    AgentTaskHandle,
    AgentTaskRequest,
)
from ..agent_errors import (
    AgentCancellationError,
    AgentMalformedResponseError,
    AgentProviderError,
    AgentTimeoutError,
    ProviderNotConfiguredError,
)
from ..agent_providers import AgentProvider
from ..config import get_settings
from .base import AgentAdapter

logger = logging.getLogger(__name__)

#: Start-task statuses that mean "still starting, keep polling".
_STARTING_STATUSES = frozenset(
    {
        "WORKING",
        "WAITING_FOR_SANDBOX",
        "PREPARING_REPOSITORY",
        "RUNNING_SETUP_SCRIPT",
        "SETTING_UP_GIT_HOOKS",
        "SETTING_UP_SKILLS",
        "STARTING_CONVERSATION",
    }
)

#: Provider execution_status values that mean "agent is actively working".
_RUNNING_STATUSES = frozenset({"idle", "running", "paused", "waiting_for_confirmation"})


class OpenHandsAdapter(AgentAdapter):
    """Adapter for the ``openhands`` provider (OpenHands Cloud API V1).

    ``repository`` and ``branch`` are the workspace-derived execution context
    (owner/repo + git branch) resolved by the execution layer from the
    project's validated workspace. They are only needed by ``start_task``.
    """

    provider = AgentProvider.OPENHANDS

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        start_timeout: float | None = None,
        poll_interval: float | None = None,
        max_execution_seconds: float | None = None,
        repository: str | None = None,
        branch: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        """Configure the adapter.

        Any ``None`` value falls back to the application settings, so a bare
        ``OpenHandsAdapter()`` is always truthful about being unconfigured in
        environments without ``OPENHANDS_API_KEY``.

        ``http_client`` is for tests: inject a client backed by a fake
        transport so the real wire protocol can be exercised offline.
        """
        settings = get_settings()
        self.base_url = base_url or settings.openhands_base_url
        self.api_key = api_key if api_key is not None else settings.openhands_api_key
        self.timeout = timeout if timeout is not None else settings.openhands_timeout
        self.start_timeout = (
            start_timeout if start_timeout is not None else settings.openhands_start_timeout
        )
        self.poll_interval = (
            poll_interval if poll_interval is not None else settings.openhands_poll_interval
        )
        self.max_execution_seconds = (
            max_execution_seconds
            if max_execution_seconds is not None
            else settings.openhands_max_execution_seconds
        )
        self.repository = repository
        self.branch = branch
        self._client: httpx.Client | None = http_client

    # ---- configuration -------------------------------------------------------

    def is_configured(self) -> bool:
        """True when a base URL and API key are set (i.e. a real provider)."""
        return bool(self.base_url and self.api_key)

    def _http(self) -> httpx.Client:
        """Return the shared HTTP client, creating it lazily.

        The reference schema uses X-Access-Token; the overview guide also
        shows Authorization: Bearer. Sending both maximizes compatibility
        with either server variant; the token value is the same either way.

        Injected clients (tests) keep their transport but are given the same
        base URL, timeout and auth headers so the real wire path is exercised.
        """
        if self._client is None:
            headers: dict[str, str] = {}
            if self.api_key:
                headers["X-Access-Token"] = self.api_key
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
                headers=headers,
            )
        else:
            self._client.base_url = self.base_url
            self._client.timeout = httpx.Timeout(self.timeout)
            if self.api_key and "X-Access-Token" not in self._client.headers:
                self._client.headers["X-Access-Token"] = self.api_key
                self._client.headers["Authorization"] = f"Bearer {self.api_key}"
        return self._client

    def _require_configured(self, operation: str) -> None:
        if not self.is_configured():
            raise ProviderNotConfiguredError(
                "openhands provider is not configured: set OPENHANDS_API_KEY "
                f"and OPENHANDS_BASE_URL to enable {operation}"
            )

    # ---- health --------------------------------------------------------------

    def check_health(self, agent_id: uuid.UUID) -> AgentHealth:
        """Probe the OpenHands Cloud API with a real authenticated request.

        Reports ``AVAILABLE`` only when the API answers 200 with a valid key.
        ``UNAVAILABLE`` means the API is reachable but authentication failed;
        ``ERROR`` means the API is unreachable, failed, or the response was
        malformed; ``NOT_CONFIGURED`` means no API key is set at all.
        """
        now = datetime.now(UTC)
        if not self.is_configured():
            return AgentHealth(
                agent_id=agent_id,
                provider=self.provider,
                status=AgentHealthState.NOT_CONFIGURED,
                detail=(
                    "openhands provider is not configured: set OPENHANDS_API_KEY "
                    "to enable health checks"
                ),
                checked_at=now,
            )
        try:
            response = self._http().get("/api/v1/app-conversations/search", params={"limit": 1})
        except httpx.TimeoutException:
            return AgentHealth(
                agent_id=agent_id,
                provider=self.provider,
                status=AgentHealthState.ERROR,
                detail="openhands health check timed out",
                checked_at=now,
            )
        except httpx.HTTPError as exc:
            return AgentHealth(
                agent_id=agent_id,
                provider=self.provider,
                status=AgentHealthState.ERROR,
                detail=f"openhands provider unreachable: {type(exc).__name__}",
                checked_at=now,
            )
        if response.status_code == 200:
            return AgentHealth(
                agent_id=agent_id,
                provider=self.provider,
                status=AgentHealthState.AVAILABLE,
                detail="openhands Cloud API reachable and authenticated",
                checked_at=now,
            )
        if response.status_code in (401, 403):
            return AgentHealth(
                agent_id=agent_id,
                provider=self.provider,
                status=AgentHealthState.UNAVAILABLE,
                detail="openhands authentication rejected (bad or missing API key)",
                checked_at=now,
            )
        return AgentHealth(
            agent_id=agent_id,
            provider=self.provider,
            status=AgentHealthState.ERROR,
            detail=f"openhands API returned HTTP {response.status_code}",
            checked_at=now,
        )

    # ---- execution -----------------------------------------------------------

    def start_task(self, request: AgentTaskRequest) -> AgentTaskHandle:
        """Start a real OpenHands conversation for ``request``.

        Returns an opaque :class:`AgentTaskHandle` whose ``reference`` is the
        provider's ``app_conversation_id``. The conversation starts
        asynchronously; this method polls the start task only until READY
        (bounded by ``start_timeout``), never until the agent finishes.
        """
        self._require_configured("execution")
        if not self.repository:
            raise ProviderNotConfiguredError(
                "openhands execution has no repository: the project workspace "
                "must be a git clone with an 'origin' remote so the target "
                "repository can be derived server-side"
            )

        payload: dict[str, Any] = {
            "initial_message": {"content": [{"type": "text", "text": _task_prompt(request)}]},
            "selected_repository": self.repository,
            "selected_branch": self.branch or "main",
            "title": f"task {request.task_id}",
            "trigger": "openhands_api",
        }
        logger.info(
            "openhands start conversation",
            extra={"task_id": str(request.task_id), "repository": self.repository},
        )
        response = self._request(
            "post",
            "/api/v1/app-conversations",
            json=payload,
        )
        start = _json_dict(response)
        start_id = start.get("id")
        if not start_id:
            raise AgentMalformedResponseError("openhands start response missing 'id'")
        if start.get("status") == "ERROR":
            raise AgentProviderError(f"openhands failed to start conversation: {_detail(start)}")

        deadline = time.monotonic() + self.start_timeout
        while True:
            reference = start.get("app_conversation_id")
            if start.get("status") == "READY" and reference:
                logger.info(
                    "openhands conversation ready",
                    extra={"task_id": str(request.task_id), "start_task_id": start_id},
                )
                return AgentTaskHandle(
                    task_id=request.task_id,
                    provider=AgentProvider.OPENHANDS,
                    reference=reference,
                )
            if start.get("status") not in _STARTING_STATUSES and start.get("status") != "READY":
                raise AgentProviderError(
                    f"openhands start task reached unexpected status {start.get('status')!r}"
                )
            if time.monotonic() >= deadline:
                raise AgentTimeoutError(
                    "timed out waiting for openhands conversation to become ready"
                )
            time.sleep(self.poll_interval)
            poll = self._request(
                "get",
                "/api/v1/app-conversations/start-tasks",
                params={"ids": start_id},
            )
            items = _json_list(poll)
            if not items:
                continue
            start = items[0]
            if start.get("status") == "ERROR":
                raise AgentProviderError(
                    f"openhands failed to start conversation: {_detail(start)}"
                )

    def get_status(self, handle: AgentTaskHandle) -> AgentStatusResult:
        """Return the provider-reported execution status for ``handle``.

        The OpenHands Cloud API reports ``execution_status`` on the
        conversation resource. Terminal success is only ``finished``; errors
        are ``error``/``stuck`` or a failed/missing sandbox.
        """
        self._require_configured("status polling")
        response = self._request(
            "get",
            "/api/v1/app-conversations",
            params={"ids": handle.reference},
        )
        items = _json_list(response)
        conversation = items[0] if items else None
        if conversation is None:
            return AgentStatusResult(
                task_id=handle.task_id,
                state=AgentExecutionState.UNKNOWN,
                detail="openhands conversation not found",
            )

        sandbox_status = conversation.get("sandbox_status")
        if sandbox_status in ("ERROR", "MISSING"):
            return AgentStatusResult(
                task_id=handle.task_id,
                state=AgentExecutionState.FAILED,
                detail=f"openhands sandbox {sandbox_status.lower()}",
            )
        execution_status = conversation.get("execution_status")
        if execution_status == "finished":
            return AgentStatusResult(
                task_id=handle.task_id,
                state=AgentExecutionState.COMPLETED,
                detail="openhands agent finished execution",
            )
        if execution_status in ("error", "stuck"):
            return AgentStatusResult(
                task_id=handle.task_id,
                state=AgentExecutionState.FAILED,
                detail=f"openhands agent reported {execution_status}",
            )
        if execution_status == "waiting_for_confirmation":
            return AgentStatusResult(
                task_id=handle.task_id,
                state=AgentExecutionState.RUNNING,
                detail="openhands agent is waiting for user confirmation (blocking)",
            )
        if execution_status in _RUNNING_STATUSES:
            return AgentStatusResult(
                task_id=handle.task_id,
                state=AgentExecutionState.RUNNING,
                detail=f"openhands agent is {execution_status}",
            )
        if execution_status == "deleting":
            return AgentStatusResult(
                task_id=handle.task_id,
                state=AgentExecutionState.UNKNOWN,
                detail="openhands conversation is deleting",
            )
        return AgentStatusResult(
            task_id=handle.task_id,
            state=AgentExecutionState.UNKNOWN,
            detail=f"openhands reported unknown execution status {execution_status!r}",
        )

    def get_result(self, handle: AgentTaskHandle) -> AgentResult:
        """Return the provider-reported result for ``handle``.

        The Cloud API does not expose a message/result-text endpoint in its
        documented V1 reference, so the result is the provider's own terminal
        execution state: ``COMPLETED`` only when ``execution_status`` is
        ``finished``, never inferred from HTTP status. Non-terminal results
        are returned truthfully with their current state.
        """
        status = self.get_status(handle)
        if status.state is AgentExecutionState.COMPLETED:
            return AgentResult(
                task_id=handle.task_id,
                state=AgentExecutionState.COMPLETED,
                output="openhands agent finished execution (execution_status=finished)",
            )
        if status.state is AgentExecutionState.FAILED:
            return AgentResult(
                task_id=handle.task_id,
                state=AgentExecutionState.FAILED,
                error=status.detail,
            )
        if status.state is AgentExecutionState.CANCELLED:
            return AgentResult(task_id=handle.task_id, state=AgentExecutionState.CANCELLED)
        return AgentResult(task_id=handle.task_id, state=status.state)

    def cancel_task(self, handle: AgentTaskHandle) -> None:
        """Cancelling an in-flight OpenHands Cloud execution is unsupported.

        The OpenHands Cloud API V1 documentation defines no endpoint for
        stopping an app conversation. Rather than fake a cancellation, this
        raises :class:`AgentCancellationError` truthfully.
        """
        raise AgentCancellationError(
            "openhands cloud API does not document a cancellation endpoint; "
            "cancelling an in-flight execution is unsupported"
        )

    # ---- internals -----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Perform an OpenHands API call, mapping transport failures."""
        client = self._http()
        try:
            response = client.request(method, path, json=json, params=params)
        except httpx.TimeoutException as exc:
            raise AgentTimeoutError(
                f"openhands request timed out after {self.timeout}s ({method} {path})"
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentProviderError(
                f"openhands request failed: {type(exc).__name__} ({method} {path})"
            ) from exc

        if response.status_code in (401, 403):
            raise AgentProviderError("openhands authentication failed (bad or missing API key)")
        if response.status_code == 404:
            raise AgentProviderError(f"openhands endpoint not found (HTTP 404): {path}")
        if response.status_code == 422:
            raise AgentProviderError(
                f"openhands rejected the request (HTTP 422): {_safe_body(response)}"
            )
        if response.status_code >= 500:
            raise AgentProviderError(f"openhands server error (HTTP {response.status_code})")
        if response.status_code != 200:
            raise AgentProviderError(
                f"openhands returned HTTP {response.status_code}: {_safe_body(response)}"
            )
        return response


def _task_prompt(request: AgentTaskRequest) -> str:
    """Compose the OpenHands initial message from the provider-independent task."""
    parts = [request.objective]
    if request.instructions:
        parts.append(f"Instructions:\n{request.instructions}")
    if request.constraints:
        parts.append("Constraints:\n- " + "\n- ".join(request.constraints))
    if request.success_criteria:
        parts.append("Success criteria:\n- " + "\n- ".join(request.success_criteria))
    return "\n\n".join(parts)


def _json_dict(response: httpx.Response) -> dict[str, Any]:
    """Parse a response body that must be a JSON object."""
    try:
        data = response.json()
    except ValueError as exc:
        raise AgentMalformedResponseError("openhands returned a non-JSON response") from exc
    if not isinstance(data, dict):
        raise AgentMalformedResponseError(
            f"openhands returned an unexpected response shape ({type(data).__name__})"
        )
    return data


def _json_list(response: httpx.Response) -> list[dict[str, Any]]:
    """Parse a response body that is a JSON list (or a single object).

    The start-task and conversation endpoints return lists in the documented
    examples; a single object is tolerated defensively.
    """
    try:
        data = response.json()
    except ValueError as exc:
        raise AgentMalformedResponseError("openhands returned a non-JSON response") from exc
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    raise AgentMalformedResponseError(
        f"openhands returned an unexpected response shape ({type(data).__name__})"
    )


def _detail(start: dict[str, Any]) -> str:
    """Return a bounded, safe detail string from a start-task object."""
    detail = start.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail[:500]
    return "unknown error"


def _safe_body(response: httpx.Response) -> str:
    """Return a truncated, safe excerpt of an error response body."""
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("detail"):
            return str(data["detail"])[:500]
    except ValueError:
        pass
    try:
        text = response.text
    except Exception:
        return ""
    return (text or "")[:500]
