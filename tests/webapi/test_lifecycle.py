import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from manyselves.interfaces.types import ApiDebugMessage, ToolCallMessage, ToolResult
from manyselves.webapi.dependencies import get_runtime_host
from manyselves.webapi.main import app as exported_app
from manyselves.webapi.main import create_app
from manyselves.webapi.settings import WebSettings


class FakeRuntimeHost:
    """A lifecycle fake that leaves the facade's snapshot behavior real."""

    def __init__(
        self,
        *,
        start_error: BaseException | None = None,
        stop_error: BaseException | None = None,
    ) -> None:
        self.start_count = 0
        self.stop_count = 0
        self.is_ready = False
        self.start_error = start_error
        self.stop_error = stop_error
        self.workspace: Path | None = None
        self.loop_manager = SimpleNamespace(
            get_all_agent_statuses=lambda: {"main": "idle"},
            get_agent_session_id=lambda agent_id: None,
        )

    async def start(self, workspace: Path) -> None:
        self.start_count += 1
        self.workspace = workspace
        if self.start_error is not None:
            raise self.start_error
        self.is_ready = True

    async def stop(self) -> None:
        self.stop_count += 1
        self.is_ready = False
        if self.stop_error is not None:
            raise self.stop_error


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
async def test_live_does_not_claim_runtime_readiness(web_settings: WebSettings) -> None:
    app = create_app(web_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "live"}


async def test_ready_is_503_until_runtime_is_ready(web_settings: WebSettings) -> None:
    """Reporting readiness before lifespan startup would route work to no runtime."""
    app = create_app(web_settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "RUNTIME_NOT_READY",
        "message": "Runtime is not ready",
        "retryable": True,
        "details": {},
    }
    assert response.json()["requestId"]


@pytest.mark.asyncio
async def test_ready_is_200_with_exact_ready_body(
    web_settings: WebSettings, fake_runtime_host: FakeRuntimeHost
) -> None:
    fake_runtime_host.is_ready = True
    app = create_app(web_settings)
    app.state.runtime_host = fake_runtime_host
    app.state.maintenance_service = SimpleNamespace(quiesced=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_ready_is_503_with_quiesced_error_envelope(
    web_settings: WebSettings, fake_runtime_host: FakeRuntimeHost
) -> None:
    """Readiness must not expose raw maintenance state while work is quiesced."""
    fake_runtime_host.is_ready = True
    app = create_app(web_settings)
    app.state.runtime_host = fake_runtime_host
    app.state.maintenance_service = SimpleNamespace(quiesced=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "MAINTENANCE_QUIESCED",
        "message": "Runtime mutations are disabled during maintenance",
        "retryable": True,
        "details": {},
    }
    assert response.json()["requestId"]
    assert set(response.json()) == {"error", "requestId"}


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
            stream_id = app.state.event_broker.stream_id

    assert response.status_code == 200
    body = response.json()
    assert {"runtime", "project", "conversations", "agents", "settings"} <= set(body)
    assert body["streamId"] == stream_id


@pytest.mark.asyncio
async def test_bootstrap_sanitizes_recoverable_runtime_tool_snapshot(
    web_settings: WebSettings, fake_runtime_host: FakeRuntimeHost
) -> None:
    """Returning raw projection values would leak credentials or object representations."""
    class OpaqueValue:
        def __repr__(self) -> str:
            return "opaque-repr-secret"

    cycle: dict[str, object] = {}
    cycle["again"] = cycle
    deep: object = "depth-secret"
    for _ in range(32):
        deep = {"next": deep}

    app = create_app(web_settings)
    app.dependency_overrides[get_runtime_host] = lambda: fake_runtime_host
    async with app.router.lifespan_context(app):
        await app.state.event_broker.publish_internal(
            ToolCallMessage(
                agent_type="main",
                tool_name="read",
                tool_call_id="call-1",
                arguments={
                    "authorization": "Bearer bootstrap-secret",
                    "api_key": "sk-bootstrap-secret-123456",
                    "opaque": OpaqueValue(),
                    "cycle": cycle,
                    "deep": deep,
                },
            )
        )
        await app.state.event_broker.publish_internal(
            ToolResult(
                agent_type="main",
                tool_name="read",
                tool_call_id="call-1",
                result={"apiKey": "sk-result-secret-123456", "opaque": OpaqueValue()},
                error="Bearer result-secret",
            )
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/bootstrap")

    assert response.status_code == 200
    wire = response.text
    for secret in (
        "bootstrap-secret",
        "sk-bootstrap-secret-123456",
        "opaque-repr-secret",
        "depth-secret",
        "sk-result-secret-123456",
        "result-secret",
    ):
        assert secret not in wire
    assert "[REDACTED]" in wire
    tool = response.json()["runtime"]["tools"][0]
    assert tool["toolCallId"] == "call-1"
    assert tool["arguments"]["cycle"]["again"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_bootstrap_sanitizes_tool_and_debug_identity_strings(
    web_settings: WebSettings, fake_runtime_host: FakeRuntimeHost
) -> None:
    """Bootstrap must not leak identities that the SSE boundary redacts."""
    app = create_app(web_settings)
    app.dependency_overrides[get_runtime_host] = lambda: fake_runtime_host
    async with app.router.lifespan_context(app):
        await app.state.event_broker.publish_internal(
            ToolCallMessage(
                agent_type="Bearer tool-agent-secret",
                tool_name="Bearer tool-name-secret",
                tool_call_id="Bearer tool-call-secret",
                arguments={},
            )
        )
        await app.state.event_broker.publish_internal(
            ApiDebugMessage(
                agent_type="Bearer debug-agent-secret",
                model="Bearer debug-model-secret",
                tokens_in=1,
                tokens_out=2,
                duration_ms=3,
                status="Bearer debug-status-secret",
            )
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/bootstrap")

    assert response.status_code == 200
    runtime = response.json()["runtime"]
    assert runtime["tools"][0]["toolCallId"] == "[REDACTED]"
    assert runtime["tools"][0]["agentId"] == "[REDACTED]"
    assert runtime["tools"][0]["name"] == "[REDACTED]"
    assert runtime["debug"][0]["agentId"] == "[REDACTED]"
    assert runtime["debug"][0]["model"] == "[REDACTED]"
    assert runtime["debug"][0]["status"] == "[REDACTED]"
    for secret in (
        "tool-agent-secret",
        "tool-name-secret",
        "tool-call-secret",
        "debug-agent-secret",
        "debug-model-secret",
        "debug-status-secret",
    ):
        assert secret not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("start_error", [RuntimeError("startup failed"), asyncio.CancelledError()])
async def test_failed_or_cancelled_startup_stops_the_created_host(
    web_settings: WebSettings, start_error: BaseException
) -> None:
    """Abandoning a partially-started host would permit a duplicate runtime later."""
    host = FakeRuntimeHost(start_error=start_error)
    app = create_app(web_settings)
    app.dependency_overrides[get_runtime_host] = lambda: host

    with pytest.raises(type(start_error)):
        async with app.router.lifespan_context(app):
            pass

    assert host.stop_count == 1
    assert app.state.lifecycle_active is False


@pytest.mark.asyncio
@pytest.mark.parametrize("start_error", [RuntimeError("startup failed"), asyncio.CancelledError()])
async def test_failed_startup_preserves_its_error_when_cleanup_also_fails(
    web_settings: WebSettings, start_error: BaseException
) -> None:
    """Cleanup failure must not mask startup failure or permanently lock out a new lifespan."""
    host = FakeRuntimeHost(start_error=start_error, stop_error=RuntimeError("cleanup failed"))
    app = create_app(web_settings)
    app.dependency_overrides[get_runtime_host] = lambda: host

    with pytest.raises(type(start_error)) as raised:
        async with app.router.lifespan_context(app):
            pass

    if isinstance(start_error, RuntimeError):
        assert str(raised.value) == "startup failed"
    assert app.state.lifecycle_active is False
    host.start_error = None
    host.stop_error = None
    async with app.router.lifespan_context(app):
        assert host.start_count == 2


@pytest.mark.asyncio
async def test_reentrant_lifespan_rejects_a_second_runtime(
    web_settings: WebSettings, fake_runtime_host: FakeRuntimeHost
) -> None:
    """Starting another lifespan while active would create a second process-local runtime."""
    app = create_app(web_settings)
    app.dependency_overrides[get_runtime_host] = lambda: fake_runtime_host

    async with app.router.lifespan_context(app):
        with pytest.raises(RuntimeError, match="already active"):
            async with app.router.lifespan_context(app):
                pass

        assert fake_runtime_host.start_count == 1


@pytest.mark.asyncio
async def test_app_registers_only_versioned_api_routes(web_settings: WebSettings) -> None:
    """Unversioned documentation routes would violate the versioned API boundary."""
    app = create_app(web_settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(
            *(client.get(path) for path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"))
        )

    assert [response.status_code for response in responses] == [404, 404, 404, 404]


@pytest.mark.asyncio
async def test_exported_app_applies_env_loaded_cors_origins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Deferring settings must not silently discard production CORS origins."""
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("INITIAL_PROJECT_ID", "project-1")
    monkeypatch.setenv("ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_ORIGINS", '["https://client.example"]')
    host = FakeRuntimeHost()
    exported_app.state.web_settings = None
    exported_app.dependency_overrides[get_runtime_host] = lambda: host

    try:
        async with exported_app.router.lifespan_context(exported_app):
            transport = httpx.ASGITransport(app=exported_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/api/v1/health/ready",
                    headers={"Origin": "https://client.example"},
                )
    finally:
        exported_app.dependency_overrides.pop(get_runtime_host, None)
        exported_app.state.web_settings = None

    assert response.headers["access-control-allow-origin"] == "https://client.example"
