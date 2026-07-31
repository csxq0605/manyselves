"""HTTP contracts for deployment authentication and runtime control leases."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from manyselves.webapi.dependencies import get_runtime_host
from manyselves.webapi.main import create_app
from manyselves.webapi.settings import WebSettings


class FakeRuntimeHost:
    """Minimal lifecycle host; control leases do not need a runtime backend."""

    def __init__(self) -> None:
        self.is_ready = False
        self.loop_manager = SimpleNamespace(
            get_all_agent_statuses=lambda: {"main": "idle"},
            get_agent_session_id=lambda agent_id: None,
        )

    async def start(self, workspace: Path) -> None:
        self.is_ready = True

    async def stop(self) -> None:
        self.is_ready = False


@pytest.fixture
async def async_client(tmp_path: Path):
    """Exercise the installed routes through an active application lifespan."""
    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="project-1",
            access_token=SecretStr("test-token"),
        )
    )
    app.dependency_overrides[get_runtime_host] = FakeRuntimeHost
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def authed_client(async_client: httpx.AsyncClient):
    """A client carrying the deployment bearer credential."""
    async_client.headers["Authorization"] = "Bearer test-token"
    yield async_client


@pytest.mark.asyncio
async def test_mutation_rejects_missing_access_token(async_client: httpx.AsyncClient) -> None:
    """Without authentication, any caller could become the runtime controller."""
    response = await async_client.post(
        "/api/v1/control/lease",
        headers={"X-Request-ID": "request-auth"},
        json={"clientId": "c-1"},
    )

    assert response.status_code == 401
    assert response.headers["x-request-id"] == "request-auth"
    assert response.json() == {
        "error": {
            "code": "AUTH_REQUIRED",
            "message": "Bearer access token is required",
            "retryable": False,
            "details": {},
        },
        "requestId": "request-auth",
    }


@pytest.mark.asyncio
async def test_mutation_rejects_invalid_access_token(async_client: httpx.AsyncClient) -> None:
    """Accepting a non-matching bearer credential would expose mutations."""
    response = await async_client.post(
        "/api/v1/control/lease",
        headers={"Authorization": "Bearer wrong-token"},
        json={"clientId": "c-1"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_INVALID"
    assert response.json()["requestId"] == response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_second_client_receives_lease_conflict(authed_client: httpx.AsyncClient) -> None:
    """A second client must not acquire the live controller lease."""
    first = await authed_client.post("/api/v1/control/lease", json={"clientId": "c-1"})
    second = await authed_client.post("/api/v1/control/lease", json={"clientId": "c-2"})

    assert first.status_code == 201
    assert "leaseToken" in first.json()
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONTROL_LEASE_HELD"
    assert second.json()["error"]["details"] == {"controllerClientId": "c-1"}


@pytest.mark.asyncio
async def test_heartbeat_and_release_require_the_issued_lease_token(
    authed_client: httpx.AsyncClient,
) -> None:
    """A caller without the lease token cannot extend or release another controller."""
    acquired = await authed_client.post("/api/v1/control/lease", json={"clientId": "c-1"})
    lease_token = acquired.json()["leaseToken"]

    rejected = await authed_client.post("/api/v1/control/lease/heartbeat", json={})
    heartbeated = await authed_client.post(
        "/api/v1/control/lease/heartbeat", json={"leaseToken": lease_token}
    )
    released = await authed_client.request(
        "DELETE", "/api/v1/control/lease", json={"leaseToken": lease_token}
    )
    next_client = await authed_client.post("/api/v1/control/lease", json={"clientId": "c-2"})

    assert rejected.status_code == 423
    assert rejected.json()["error"]["code"] == "CONTROL_LEASE_REQUIRED"
    assert heartbeated.status_code == 200
    assert heartbeated.json()["leaseToken"] == lease_token
    assert released.status_code == 204
    assert next_client.status_code == 201
