"""HTTP and signing contracts for the local administrative session."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from manyselves.webapi.dependencies import get_runtime_host
from manyselves.webapi.main import create_app
from manyselves.webapi.session_auth import SessionSigner
from manyselves.webapi.settings import WebSettings


class FakeRuntimeHost:
    """Minimal host required to enter the real application lifespan."""

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
    """Exercise auth with the actual route registration and lifespan setup."""
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


def test_session_signer_rejects_tampering(tmp_path: Path) -> None:
    """A modified signed payload must not authenticate as its original user."""
    signer = SessionSigner(tmp_path / "session.key", ttl_seconds=3600, now=lambda: 100)

    value = signer.issue("admin")

    principal = signer.verify(value)
    assert principal is not None
    assert principal.username == "admin"
    assert signer.verify(value + "x") is None


def test_session_signer_rejects_expired_session(tmp_path: Path) -> None:
    """An expired signed cookie must not remain usable after its TTL."""
    signer = SessionSigner(tmp_path / "session.key", ttl_seconds=1, now=lambda: 100)

    value = signer.issue("admin")
    expired_signer = SessionSigner(tmp_path / "session.key", ttl_seconds=1, now=lambda: 101)

    assert expired_signer.verify(value) is None


@pytest.mark.asyncio
async def test_login_sets_http_only_strict_cookie(async_client: httpx.AsyncClient) -> None:
    """A successful login must store the session in a script-inaccessible strict cookie."""
    response = await async_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "yuanxi@2026"},
    )

    assert response.status_code == 204
    assert "manyselves_session=" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_session_returns_authenticated_principal_after_login(
    async_client: httpx.AsyncClient,
) -> None:
    """A valid issued cookie must expose only its authenticated principal details."""
    await async_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "yuanxi@2026"},
    )

    response = await async_client.get("/api/v1/auth/session")

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert response.json()["username"] == "admin"
    assert response.json()["expiresAt"]
    assert "password" not in response.text


@pytest.mark.asyncio
async def test_session_rejects_absent_cookie(async_client: httpx.AsyncClient) -> None:
    """A caller without a session cookie must not be treated as authenticated."""
    response = await async_client.get("/api/v1/auth/session")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_logout_always_deletes_the_session_cookie(async_client: httpx.AsyncClient) -> None:
    """Logout must clear any browser session whether or not one was issued."""
    response = await async_client.post("/api/v1/auth/logout")

    assert response.status_code == 204
    assert "manyselves_session=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
