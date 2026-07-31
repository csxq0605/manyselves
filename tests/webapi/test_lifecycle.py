from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from manyselves.webapi.dependencies import get_runtime_host
from manyselves.webapi.main import create_app
from manyselves.webapi.settings import WebSettings


class FakeRuntimeHost:
    """A lifecycle fake that leaves the facade's snapshot behavior real."""

    def __init__(self) -> None:
        self.start_count = 0
        self.stop_count = 0
        self.is_ready = False
        self.workspace: Path | None = None
        self.loop_manager = SimpleNamespace(
            get_all_agent_statuses=lambda: {"main": "idle"},
            get_agent_session_id=lambda agent_id: None,
        )

    async def start(self, workspace: Path) -> None:
        self.start_count += 1
        self.workspace = workspace
        self.is_ready = True

    async def stop(self) -> None:
        self.stop_count += 1
        self.is_ready = False


@pytest.fixture
def web_settings(tmp_path: Path) -> WebSettings:
    return WebSettings(
        data_root=tmp_path,
        initial_project_id="project-1",
        access_token=SecretStr("test-token"),
    )


@pytest.fixture
def fake_runtime_host() -> FakeRuntimeHost:
    return FakeRuntimeHost()


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_one_runtime(
    web_settings: WebSettings, fake_runtime_host: FakeRuntimeHost
) -> None:
    """Skipping host lifecycle ownership would leave the shared runtime running."""
    app = create_app(web_settings)
    app.dependency_overrides[get_runtime_host] = lambda: fake_runtime_host

    async with app.router.lifespan_context(app):
        assert fake_runtime_host.start_count == 1
        assert app.state.runtime_facade is not None

    assert fake_runtime_host.stop_count == 1


@pytest.mark.asyncio
async def test_ready_is_503_until_runtime_is_ready(web_settings: WebSettings) -> None:
    """Reporting readiness before lifespan startup would route work to no runtime."""
    app = create_app(web_settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_bootstrap_returns_one_coherent_snapshot(
    web_settings: WebSettings, fake_runtime_host: FakeRuntimeHost
) -> None:
    """An incomplete bootstrap would force clients to begin from inconsistent state."""
    app = create_app(web_settings)
    app.dependency_overrides[get_runtime_host] = lambda: fake_runtime_host

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/bootstrap")

    assert response.status_code == 200
    body = response.json()
    assert {"runtime", "project", "conversations", "agents", "settings"} <= set(body)
