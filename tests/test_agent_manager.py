"""Service-level tests for the agent registry, adapter resolution, and health
probes (Phase 4).

The registry stores agent records, resolves providers to adapters, and probes
health.  Adapters are honest: they report ``not_configured`` and every
execution method raises ``ProviderNotConfiguredError`` instead of faking
success.
"""

import uuid

import pytest

from app.adapters import (
    AgentAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    GeminiAdapter,
    OpenHandsAdapter,
)
from app.agent_contracts import AgentHealthState, AgentTaskRequest
from app.agent_errors import (
    AgentNameConflictError,
    AgentNotFoundError,
    InvalidAgentConfigurationError,
    ProviderNotConfiguredError,
    UnsupportedProviderError,
)
from app.agent_manager import ADAPTER_REGISTRY, AgentManager, resolve_adapter
from app.agent_providers import UNAVAILABLE, AgentCapability, AgentProvider
from app.db import get_session_factory
from app.models import Agent

manager = AgentManager()


def _new_session():
    return get_session_factory()()


def _register(session, name="agent-a", provider=AgentProvider.OPENHANDS, **overrides):
    return manager.register_agent(
        session,
        name=name,
        provider=provider,
        capabilities=overrides.pop("capabilities", [AgentCapability.CODE]),
        configuration=overrides.pop("configuration", {"model": "default"}),
        **overrides,
    )


# ---------- registration ----------


def test_register_agent_starts_unavailable(client):
    session = _new_session()
    try:
        agent = _register(session)
        assert agent.name == "agent-a"
        assert agent.provider == AgentProvider.OPENHANDS.value
        assert agent.status == UNAVAILABLE
        assert agent.capabilities == ["code"]
        assert agent.configuration == {"model": "default"}
        # Persisted in the DB.
        fetched = session.get(Agent, agent.id)
        assert fetched is not None
        assert fetched.status == UNAVAILABLE
    finally:
        session.close()


def test_register_agent_rejects_duplicate_name(client):
    session = _new_session()
    try:
        _register(session, name="dup")
        with pytest.raises(AgentNameConflictError):
            _register(session, name="dup", provider=AgentProvider.CODEX)
    finally:
        session.close()


def test_register_agent_rejects_secret_configuration(client):
    session = _new_session()
    try:
        with pytest.raises(InvalidAgentConfigurationError, match="secret"):
            _register(session, configuration={"api_key": "sk-live"})
    finally:
        session.close()


def test_register_agent_accepts_no_capabilities_or_config(client):
    session = _new_session()
    try:
        agent = manager.register_agent(session, name="bare", provider=AgentProvider.CODEX)
        assert agent.capabilities == []
        assert agent.configuration == {}
    finally:
        session.close()


# ---------- queries ----------


def test_get_agent_returns_record(client):
    session = _new_session()
    try:
        agent = _register(session)
        assert manager.get_agent(session, agent.id).id == agent.id
    finally:
        session.close()


def test_get_agent_missing_raises(client):
    session = _new_session()
    try:
        with pytest.raises(AgentNotFoundError):
            manager.get_agent(session, uuid.uuid4())
    finally:
        session.close()


def test_list_agents_is_deterministic(client):
    session = _new_session()
    try:
        a1 = _register(session, name="zeta")
        a2 = _register(session, name="alpha", provider=AgentProvider.CODEX)
        a3 = _register(session, name="middle", provider=AgentProvider.GEMINI)
        names = [a.name for a in manager.list_agents(session)]
        assert names == sorted(names)
        assert set(names) == {"zeta", "alpha", "middle"}
        assert {a.id for a in manager.list_agents(session)} == {a1.id, a2.id, a3.id}
    finally:
        session.close()


# ---------- adapter resolution ----------


def test_adapter_registry_covers_all_providers():
    assert set(ADAPTER_REGISTRY) == set(AgentProvider)
    assert all(issubclass(cls, AgentAdapter) for cls in ADAPTER_REGISTRY.values())


@pytest.mark.parametrize(
    "provider, adapter_cls",
    [
        (AgentProvider.OPENHANDS, OpenHandsAdapter),
        (AgentProvider.CLAUDE_CODE, ClaudeCodeAdapter),
        (AgentProvider.CODEX, CodexAdapter),
        (AgentProvider.GEMINI, GeminiAdapter),
    ],
)
def test_resolve_adapter_returns_correct_adapter(provider, adapter_cls):
    adapter = resolve_adapter(provider)
    assert isinstance(adapter, adapter_cls)
    assert adapter.provider == provider


def test_resolve_adapter_accepts_raw_strings():
    assert isinstance(resolve_adapter("openhands"), OpenHandsAdapter)
    assert isinstance(resolve_adapter("codex"), CodexAdapter)


@pytest.mark.parametrize("bad", ["openai", "gemini-pro", ""])
def test_resolve_adapter_unknown_provider_raises(bad):
    with pytest.raises(UnsupportedProviderError):
        resolve_adapter(bad)


def test_get_agent_with_adapter_returns_abstraction(client):
    session = _new_session()
    try:
        agent = _register(session, provider=AgentProvider.CLAUDE_CODE)
        record, adapter = manager.get_agent_with_adapter(session, agent.id)
        assert record.id == agent.id
        assert isinstance(adapter, ClaudeCodeAdapter)
    finally:
        session.close()


def test_get_agent_with_adapter_unsupported_stored_provider(client):
    """A record whose provider lost its adapter fails safely."""
    session = _new_session()
    try:
        agent = _register(session)
        session.execute(
            __import__("sqlalchemy")
            .update(Agent)
            .where(Agent.id == agent.id)
            .values(provider="retired_provider")
        )
        session.commit()
        with pytest.raises(UnsupportedProviderError):
            manager.get_agent_with_adapter(session, agent.id)
    finally:
        session.close()


def test_agent_adapter_is_abstract():
    with pytest.raises(TypeError):
        AgentAdapter()


# ---------- health probes ----------


@pytest.mark.parametrize(
    "provider, adapter_cls",
    [
        (AgentProvider.OPENHANDS, OpenHandsAdapter),
        (AgentProvider.CLAUDE_CODE, ClaudeCodeAdapter),
        (AgentProvider.CODEX, CodexAdapter),
        (AgentProvider.GEMINI, GeminiAdapter),
    ],
)
def test_all_adapters_report_not_configured(client, provider, adapter_cls):
    session = _new_session()
    try:
        agent = _register(session, name=f"probe-{provider.value}", provider=provider)
        health = manager.check_health(session, agent.id)
        assert health.agent_id == agent.id
        assert health.provider == provider
        assert health.status == AgentHealthState.NOT_CONFIGURED
        assert isinstance(adapter_cls().check_health(agent.id), type(health))
    finally:
        session.close()


def test_check_health_missing_agent_raises(client):
    session = _new_session()
    try:
        with pytest.raises(AgentNotFoundError):
            manager.check_health(session, uuid.uuid4())
    finally:
        session.close()


# ---------- adapter honesty: execution never faked ----------


def _handle():
    from app.agent_contracts import AgentTaskHandle

    return AgentTaskHandle(task_id=uuid.uuid4(), provider=AgentProvider.CODEX, reference="handle-1")


def _request():
    return AgentTaskRequest(
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        objective="do work",
        success_criteria=["done"],
    )


@pytest.mark.parametrize(
    "adapter_cls", [OpenHandsAdapter, ClaudeCodeAdapter, CodexAdapter, GeminiAdapter]
)
def test_adapter_execution_methods_raise_not_configured(adapter_cls):
    adapter = adapter_cls()
    with pytest.raises(ProviderNotConfiguredError, match="not configured"):
        adapter.start_task(_request())
    with pytest.raises(ProviderNotConfiguredError, match="not configured"):
        adapter.get_status(_handle())
    with pytest.raises(ProviderNotConfiguredError, match="not configured"):
        adapter.get_result(_handle())
    with pytest.raises(ProviderNotConfiguredError, match="not configured"):
        adapter.cancel_task(_handle())
