"""HTTP-level tests for the Agents API (Phase 4).

Covers the four agent-management endpoints (register, list, get, health),
mass-assignment protection, secret redaction, OpenAPI contract, and error
handling.  There is no execute endpoint.
"""

import uuid

from app.agent_providers import UNAVAILABLE, AgentCapability


def _c(client):
    c, _, _ = client
    return c


def _agent_payload(**overrides):
    payload = {
        "name": "test-agent",
        "provider": "openhands",
        "capabilities": ["code", "test"],
        "configuration": {"model": "sonnet", "temperature": "0.2"},
    }
    payload.update(overrides)
    return payload


# ---------- POST /agents ----------


def test_register_agent_returns_201(client):
    resp = _c(client).post("/agents", json=_agent_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "test-agent"
    assert body["provider"] == "openhands"
    assert body["status"] == UNAVAILABLE
    assert body["capabilities"] == ["code", "test"]
    assert body["configuration"] == {"model": "sonnet", "temperature": "0.2"}
    assert "id" in body
    assert "created_at" in body
    assert "updated_at" in body


def test_register_agent_duplicate_name_returns_409(client):
    _c(client).post("/agents", json=_agent_payload())
    resp = _c(client).post("/agents", json=_agent_payload(name="test-agent"))
    assert resp.status_code == 409, resp.text
    assert "already exists" in resp.text


def test_register_agent_unknown_provider_returns_422(client):
    resp = _c(client).post("/agents", json=_agent_payload(provider="nope"))
    assert resp.status_code == 422, resp.text


def test_register_agent_secret_key_returns_422(client):
    resp = _c(client).post("/agents", json=_agent_payload(configuration={"api_key": "sk-live"}))
    assert resp.status_code == 422, resp.text
    assert "secret" in resp.text.lower()


def test_register_agent_overlong_name_returns_422(client):
    resp = _c(client).post("/agents", json=_agent_payload(name="x" * 101))
    assert resp.status_code == 422, resp.text


def test_register_agent_too_many_capabilities_returns_422(client):
    resp = _c(client).post(
        "/agents",
        json=_agent_payload(capabilities=[c.value for c in AgentCapability] * 3),
    )
    assert resp.status_code == 422, resp.text


def test_register_agent_blank_name_returns_422(client):
    resp = _c(client).post("/agents", json=_agent_payload(name="   "))
    assert resp.status_code == 422, resp.text


def test_register_agent_empty_name_returns_422(client):
    resp = _c(client).post("/agents", json=_agent_payload(name=""))
    assert resp.status_code == 422, resp.text


def test_register_agent_unknown_capability_returns_422(client):
    resp = _c(client).post("/agents", json=_agent_payload(capabilities=["deploy"]))
    assert resp.status_code == 422, resp.text


# ---------- mass-assignment protection ----------


def test_register_agent_mass_assignment_is_ignored(client):
    """A client cannot inject status, id, or timestamps."""
    fake_id = str(uuid.uuid4())
    fake_time = "2020-01-01T00:00:00"
    resp = _c(client).post(
        "/agents",
        json={
            "name": "mass-test",
            "provider": "codex",
            "status": "AVAILABLE",
            "id": fake_id,
            "created_at": fake_time,
            "updated_at": fake_time,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == UNAVAILABLE
    assert body["id"] != fake_id
    assert body["created_at"] != fake_time
    assert body["updated_at"] != fake_time


# ---------- GET /agents ----------


def test_list_agents_empty_returns_200(client):
    resp = _c(client).get("/agents")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_agents_returns_registered_agents(client):
    _c(client).post("/agents", json=_agent_payload(name="a1"))
    _c(client).post("/agents", json=_agent_payload(name="a2", provider="codex"))
    resp = _c(client).get("/agents")
    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()]
    assert names == sorted(names)
    assert "a1" in names
    assert "a2" in names


# ---------- GET /agents/{id} ----------


def test_get_agent_returns_200(client):
    created = _c(client).post("/agents", json=_agent_payload()).json()
    resp = _c(client).get(f"/agents/{created['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "test-agent"


def test_get_agent_missing_returns_404(client):
    resp = _c(client).get(f"/agents/{uuid.uuid4()}")
    assert resp.status_code == 404, resp.text


# ---------- GET /agents/{id}/health ----------


def test_get_agent_health_returns_not_configured(client):
    created = _c(client).post("/agents", json=_agent_payload()).json()
    resp = _c(client).get(f"/agents/{created['id']}/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == created["id"]
    assert body["provider"] == "openhands"
    assert body["status"] == "not_configured"
    assert "checked_at" in body


def test_get_agent_health_missing_returns_404(client):
    resp = _c(client).get(f"/agents/{uuid.uuid4()}/health")
    assert resp.status_code == 404, resp.text


# ---------- no execute endpoint ----------


def test_no_execute_endpoint(client):
    resp = _c(client).get("/openapi.json")
    paths = resp.json()["paths"]
    # No agent execution routes exist.
    agent_paths = [p for p in paths if "/agents" in p]
    for path in agent_paths:
        for method in paths[path]:
            # Only GET/POST on the management endpoints are allowed.
            assert method in ("get", "post"), f"unexpected {method.upper()} {path}"
    # The agent collection has no POST for execution.
    assert "post" not in paths.get("/agents/{agent_id}/execute", {})
    assert "post" not in paths.get("/agents/{agent_id}/tasks", {})


# ---------- OpenAPI contract ----------


def test_agent_create_schema_constraints_visible_in_openapi(client):
    schema = _c(client).get("/openapi.json").json()
    props = schema["components"]["schemas"]["AgentCreate"]["properties"]
    assert props["name"]["maxLength"] == 100
    assert props["name"]["minLength"] == 1
    assert props["capabilities"]["maxItems"] == 10
    # Provider is an enum.
    provider_ref = props["provider"]
    assert "allOf" in provider_ref or "$ref" in str(provider_ref)


def test_agent_health_out_schema_declared(client):
    schema = _c(client).get("/openapi.json").json()
    assert "AgentHealthOut" in schema["components"]["schemas"]
    schemas = schema["components"]["schemas"]["AgentHealthOut"]
    assert "agent_id" in schemas["properties"]
    assert "status" in schemas["properties"]
    assert "checked_at" in schemas["properties"]


def test_agent_error_responses_declared(client):
    """Every response code the agent routes return must be declared."""
    schema = _c(client).get("/openapi.json").json()
    paths = schema["paths"]

    # POST /agents
    post_resp = paths["/agents"]["post"]["responses"]
    assert "201" in post_resp
    assert "404" in post_resp
    assert "409" in post_resp
    assert "422" in post_resp

    # GET /agents
    get_resp = paths["/agents"]["get"]["responses"]
    assert "200" in get_resp

    # GET /agents/{agent_id}
    get_item = paths["/agents/{agent_id}"]["get"]["responses"]
    assert "200" in get_item
    assert "404" in get_item
    assert "422" in get_item

    # GET /agents/{agent_id}/health
    get_health = paths["/agents/{agent_id}/health"]["get"]["responses"]
    assert "200" in get_health
    assert "404" in get_health
    assert "422" in get_health


# ---------- defence-in-depth: secret redaction ----------


def test_agent_out_redacts_secrets_via_db(client):
    """Even if a secret key is inserted directly into the DB, GET redacts it."""
    c, _, _ = client
    created = c.post("/agents", json=_agent_payload(provider="codex")).json()
    agent_id = created["id"]

    # Directly inject a secret key into the DB.
    from app.db import get_session_factory
    from app.models import Agent

    session = get_session_factory()()
    try:
        agent = session.get(Agent, uuid.UUID(agent_id))
        agent.configuration = {"model": "sonnet", "api_key": "sk-live"}
        session.commit()
    finally:
        session.close()

    resp = c.get(f"/agents/{agent_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert "api_key" not in body["configuration"]
    assert body["configuration"] == {"model": "sonnet"}
