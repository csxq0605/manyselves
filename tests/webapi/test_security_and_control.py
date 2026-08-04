"""HTTP contracts for deployment authentication and runtime control leases."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from manyselves.webapi.dependencies import get_runtime_host
from manyselves.webapi.main import create_app
from manyselves.webapi.schemas.control import LeaseAcquireRequest, LeaseResponse, LeaseTokenRequest
from manyselves.webapi.settings import WebSettings
from tests.webapi.auth_helpers import login


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
        )
    )
    app.dependency_overrides[get_runtime_host] = FakeRuntimeHost
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def authed_client(async_client: httpx.AsyncClient):
    """A client authenticated through the public browser-session endpoint."""
    await login(async_client)
    yield async_client


@pytest.mark.asyncio
async def test_business_route_rejects_missing_session(async_client: httpx.AsyncClient) -> None:
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
            "message": "An authenticated session is required",
            "retryable": False,
            "details": {},
        },
        "requestId": "request-auth",
    }


@pytest.mark.asyncio
async def test_business_route_rejects_a_bearer_header_without_a_session(
    async_client: httpx.AsyncClient,
) -> None:
    """A legacy Authorization header must not establish an administrative session."""
    response = await async_client.post(
        "/api/v1/control/lease",
        headers={"Authorization": "Bearer wrong-token"},
        json={"clientId": "c-1"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "issue_code", "issue_location"),
    [
        ({}, "missing", ["body", "clientId"]),
        ({"clientId": 1}, "string_type", ["body", "clientId"]),
    ],
)
async def test_request_validation_errors_use_the_error_envelope(
    authed_client: httpx.AsyncClient,
    payload: dict[str, object],
    issue_code: str,
    issue_location: list[str],
) -> None:
    """Default FastAPI validation JSON would violate the stable API error contract."""
    response = await authed_client.post(
        "/api/v1/control/lease",
        headers={"X-Request-ID": "request-validation"},
        json=payload,
    )

    assert response.status_code == 422
    assert response.headers["x-request-id"] == "request-validation"
    assert response.json() == {
        "error": {
            "code": "REQUEST_VALIDATION_FAILED",
            "message": "Request validation failed",
            "retryable": False,
            "details": {
                "issues": [{"code": issue_code, "location": issue_location}],
            },
        },
        "requestId": "request-validation",
    }


@pytest.mark.asyncio
async def test_malformed_json_uses_the_error_envelope(authed_client: httpx.AsyncClient) -> None:
    """A JSON parser failure must not fall back to FastAPI's `detail` response."""
    response = await authed_client.post(
        "/api/v1/control/lease",
        headers={"Content-Type": "application/json", "X-Request-ID": "request-json"},
        content="{",
    )

    body = response.json()
    assert response.status_code == 422
    assert response.headers["x-request-id"] == "request-json"
    assert body["requestId"] == "request-json"
    assert body["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    assert body["error"]["details"] == {
        "issues": [{"code": "json_invalid", "location": ["body", 1]}]
    }


@pytest.mark.asyncio
async def test_http_errors_use_the_error_envelope(async_client: httpx.AsyncClient) -> None:
    """Default Starlette 404 JSON would omit the API error code and request ID body."""
    response = await async_client.get(
        "/api/v1/no-such-route", headers={"X-Request-ID": "request-not-found"}
    )

    assert response.status_code == 404
    assert response.headers["x-request-id"] == "request-not-found"
    assert response.json() == {
        "error": {
            "code": "HTTP_NOT_FOUND",
            "message": "The requested resource was not found",
            "retryable": False,
            "details": {},
        },
        "requestId": "request-not-found",
    }


@pytest.mark.asyncio
async def test_internal_errors_use_a_non_secret_error_envelope(tmp_path: Path) -> None:
    """An unexpected exception must not leak implementation text through the API."""
    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="project-1",
        )
    )

    @app.get("/api/v1/test/internal-error")
    async def internal_error() -> None:
        raise RuntimeError("secret implementation detail")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/test/internal-error", headers={"X-Request-ID": "request-internal"}
        )

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "request-internal"
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An unexpected server error occurred",
            "retryable": False,
            "details": {},
        },
        "requestId": "request-internal",
    }


def test_lease_tokens_are_excluded_from_schema_representations() -> None:
    """Pydantic representations must not expose bearer-like lease credentials in logs."""
    secret = "lease-token-that-must-not-appear"

    acquire = LeaseAcquireRequest(clientId="c-1", leaseToken=secret)
    token_request = LeaseTokenRequest(leaseToken=secret)
    response = LeaseResponse(
        clientId="c-1",
        actorId="c-1",
        leaseToken=secret,
        expiresAt="2026-08-01T00:00:00Z",
    )

    assert secret not in repr(acquire)
    assert secret not in repr(token_request)
    assert secret not in repr(response)
