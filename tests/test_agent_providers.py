"""Unit tests for provider, capability, and status definitions (Phase 4).

Covers the closed provider/capability enums, the agent status vocabulary,
configuration validation (bounds and secret-key rejection), and secret
redaction used as defense-in-depth on responses.
"""

import pytest

from app.agent_providers import (
    AVAILABLE,
    BUSY,
    DISABLED,
    ERROR,
    UNAVAILABLE,
    USABLE_AGENT_STATUSES,
    VALID_PROVIDER_VALUES,
    AgentCapability,
    AgentProvider,
    is_usable_agent_status,
    is_valid_agent_status,
    redact_secrets,
    validate_agent_configuration,
)

# ---------- provider enum ----------


def test_provider_enum_is_closed_set():
    assert {e.value for e in AgentProvider} == {"openhands", "claude_code", "codex", "gemini"}
    assert VALID_PROVIDER_VALUES == {"openhands", "claude_code", "codex", "gemini"}


@pytest.mark.parametrize("bad", ["openai", "copilot", "", "OpenHands", " openhands "])
def test_unknown_provider_value_rejected(bad):
    with pytest.raises(ValueError):
        AgentProvider(bad)


def test_capability_enum_is_closed_set():
    assert {e.value for e in AgentCapability} == {"code", "test", "shell", "git", "network"}


@pytest.mark.parametrize("bad", ["deploy", "write", "admin", "CODE"])
def test_unknown_capability_value_rejected(bad):
    with pytest.raises(ValueError):
        AgentCapability(bad)


# ---------- agent status vocabulary ----------


def test_known_statuses_validate():
    for status in (AVAILABLE, BUSY, UNAVAILABLE, ERROR, DISABLED):
        assert is_valid_agent_status(status)
    assert not is_valid_agent_status("READY")
    assert not is_valid_agent_status("")


def test_only_available_is_usable():
    assert USABLE_AGENT_STATUSES == {AVAILABLE}
    assert is_usable_agent_status(AVAILABLE)
    for status in (BUSY, UNAVAILABLE, ERROR, DISABLED):
        assert not is_usable_agent_status(status)


# ---------- configuration validation ----------


def test_configuration_none_becomes_empty():
    assert validate_agent_configuration(None) == {}


def test_configuration_plain_passes_through():
    config = {"model": "sonnet", "cwd": "/workspace"}
    assert validate_agent_configuration(config) == config


def test_configuration_rejects_more_than_20_keys():
    config = {f"k{i}": "v" for i in range(21)}
    with pytest.raises(ValueError, match="at most 20"):
        validate_agent_configuration(config)


def test_configuration_rejects_overlong_key():
    with pytest.raises(ValueError, match="key too long"):
        validate_agent_configuration({"x" * 65: "v"})


def test_configuration_rejects_overlong_value():
    with pytest.raises(ValueError, match="too long"):
        validate_agent_configuration({"model": "x" * 513})


def test_configuration_rejects_non_string_value():
    with pytest.raises(ValueError, match="must be a string"):
        validate_agent_configuration({"model": 123})


def test_configuration_rejects_blank_key():
    with pytest.raises(ValueError, match="invalid configuration key"):
        validate_agent_configuration({"   ": "v"})


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "API_KEY",
        "api-key",
        "openai_api_key",
        "token",
        "auth_token",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "credential",
        "authorization",
        "authorization_header",
        "auth",
        "private_key",
        "access_key",
        "private-key",
    ],
)
def test_configuration_rejects_secret_keys(key):
    with pytest.raises(ValueError, match="secret"):
        validate_agent_configuration({key: "anything"})


@pytest.mark.parametrize(
    "key",
    ["model", "temperature", "workspace", "timeout_seconds", "checkpoint_dir"],
)
def test_configuration_allows_benign_keys(key):
    assert validate_agent_configuration({key: "value"}) == {key: "value"}


# ---------- secret redaction ----------


def test_redact_secrets_removes_secret_keys():
    config = {
        "model": "sonnet",
        "api_key": "sk-live",
        "password": "hunter2",
        "auth_token": "tok",
    }
    redacted = redact_secrets(config)
    assert redacted == {"model": "sonnet"}
    # The original dict is untouched.
    assert config["api_key"] == "sk-live"


def test_redact_secrets_none_becomes_empty():
    assert redact_secrets(None) == {}


def test_redact_secrets_keeps_benign_config():
    config = {"model": "sonnet", "temperature": "0.2"}
    assert redact_secrets(config) == config


def test_configuration_rejects_non_dict():
    with pytest.raises(ValueError, match="must be a dict"):
        validate_agent_configuration("not-a-dict")
    with pytest.raises(ValueError, match="must be a dict"):
        validate_agent_configuration(42)
