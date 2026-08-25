"""In-memory fake of the OpenHands Cloud API V1 (tests only).

This is a *provider* fake: it implements the same wire protocol the real
OpenHands Cloud API exposes (verified against docs.openhands.dev), so the real
:class:`OpenHandsAdapter` can be exercised offline through ``httpx``'s
``MockTransport``. It is never registered as a production provider and lives
exclusively under ``tests/``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

#: Default start-task status sequence: first poll says WORKING, next says READY.
DEFAULT_START_STATUSES = ["WORKING", "READY"]
#: Default conversation execution_status sequence.
DEFAULT_EXECUTION_STATUSES = ["finished"]


class FakeOpenHandsServer:
    """Scripted fake of the OpenHands Cloud API V1 endpoints."""

    def __init__(
        self,
        *,
        api_key: str = "test-openhands-key",
        require_auth: bool = True,
        start_statuses: list[str] | None = None,
        execution_statuses: list[str] | None = None,
        start_http_status: int | None = None,
        start_http_body: dict[str, Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.require_auth = require_auth
        self.start_statuses = list(start_statuses or DEFAULT_START_STATUSES)
        self.execution_statuses = list(execution_statuses or DEFAULT_EXECUTION_STATUSES)
        # When set, POST /api/v1/app-conversations returns this status instead
        # of starting a conversation (for provider-error tests).
        self.start_http_status = start_http_status
        self.start_http_body = start_http_body or {"detail": "provider exploded"}

        self.start_tasks: dict[str, dict[str, Any]] = {}
        self.conversations: dict[str, dict[str, Any]] = {}
        #: Every request received, for assertions.
        self.requests: list[httpx.Request] = []
        #: Every start-conversation request body, for assertions.
        self.start_payloads: list[dict[str, Any]] = []

    # ---- httpx transport handler --------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.require_auth and request.headers.get("X-Access-Token") != self.api_key:
            return httpx.Response(401, json={"detail": "unauthorized"})

        path = request.url.path
        if request.method == "POST" and path == "/api/v1/app-conversations":
            return self._handle_start(request)
        if request.method == "GET" and path == "/api/v1/app-conversations/start-tasks":
            return self._handle_start_tasks(request)
        if request.method == "GET" and path == "/api/v1/app-conversations":
            return self._handle_get_conversations(request)
        if request.method == "GET" and path == "/api/v1/app-conversations/search":
            return self._handle_search(request)
        return httpx.Response(404, json={"detail": f"unexpected {request.method} {path}"})

    # ---- endpoints -----------------------------------------------------------

    def _handle_start(self, request: httpx.Request) -> httpx.Response:
        if self.start_http_status is not None:
            return httpx.Response(self.start_http_status, json=self.start_http_body)
        try:
            payload = json.loads(request.content or b"{}")
        except ValueError:
            payload = {}
        self.start_payloads.append(payload)

        conversation_id = str(uuid.uuid4())
        start_id = str(uuid.uuid4())
        self.start_tasks[start_id] = {
            "id": start_id,
            "queue": list(self.start_statuses),
            "conversation_id": conversation_id,
        }
        self.conversations[conversation_id] = {
            "id": conversation_id,
            "selected_repository": payload.get("selected_repository"),
            "selected_branch": payload.get("selected_branch"),
            "title": payload.get("title"),
            "sandbox_status": "RUNNING",
            "execution_status": "idle",
            "statuses": list(self.execution_statuses),
            "status_index": 0,
        }
        return httpx.Response(200, json=self._start_snapshot(start_id))

    def _handle_start_tasks(self, request: httpx.Request) -> httpx.Response:
        ids = _query_ids(request)
        items = []
        for start_id in ids:
            start = self.start_tasks.get(start_id)
            if start is None:
                continue
            # Advance to the next status so the current poll observes it.
            if len(start["queue"]) > 1:
                start["queue"].pop(0)
            items.append(self._start_snapshot(start_id))
        return httpx.Response(200, json=items)

    def _handle_get_conversations(self, request: httpx.Request) -> httpx.Response:
        ids = _query_ids(request)
        items = []
        for conversation_id in ids:
            conv = self.conversations.get(conversation_id)
            if conv is None:
                items.append(None)
                continue
            statuses = conv.get("statuses") or ["unknown"]
            index = int(conv.get("status_index", 0))
            current = statuses[min(index, len(statuses) - 1)]
            conv["status_index"] = index + 1
            item = dict(conv)
            item.pop("statuses", None)
            item.pop("status_index", None)
            item["execution_status"] = current
            items.append(item)
        return httpx.Response(200, json=items)

    def _handle_search(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": list(self.conversations.values())})

    def _start_snapshot(self, start_id: str) -> dict[str, Any]:
        start = self.start_tasks[start_id]
        status = start["queue"][0] if start["queue"] else "READY"
        snapshot: dict[str, Any] = {"id": start_id, "status": status}
        if status == "READY":
            snapshot["app_conversation_id"] = start["conversation_id"]
            snapshot["sandbox_id"] = "sandbox-1"
            snapshot["agent_server_url"] = "https://agent.example.test"
        else:
            snapshot["app_conversation_id"] = None
            snapshot["detail"] = None
        return snapshot

    # ---- test helpers ---------------------------------------------------------

    def latest_conversation_id(self) -> str:
        """Return the id of the most recently started conversation."""
        assert self.start_tasks, "no conversation was started"
        latest = max(self.start_tasks.values(), key=lambda s: s["queue"])
        return latest["conversation_id"]

    def mark_conversation_error(self) -> None:
        """Force all conversations to report a failed sandbox."""
        for conv in self.conversations.values():
            conv["sandbox_status"] = "ERROR"


def _query_ids(request: httpx.Request) -> list[str]:
    raw = request.url.params.get("ids")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(i) for i in raw]
    return [i for i in str(raw).split(",") if i]


def timeout_transport() -> httpx.MockTransport:
    """Return a transport that always raises a timeout (provider timeout tests)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("connection timed out", request=request)

    return httpx.MockTransport(handler)


def error_transport(exception: Exception) -> httpx.MockTransport:
    """Return a transport that raises ``exception`` on every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exception

    return httpx.MockTransport(handler)
