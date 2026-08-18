"""Phase 1 light-web compatibility and scope gates."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from manyselves.webapi.dependencies import get_runtime_host
from manyselves.webapi.main import create_app
from manyselves.webapi.settings import WebSettings
from tests.webapi.auth_helpers import login


class _ReleaseRuntimeHost:
    def __init__(self) -> None:
        self.is_ready = False
        self.loop_manager = SimpleNamespace(
            get_all_agent_statuses=lambda: {"main": "idle"},
            get_agent_session_id=lambda _agent_id: "session-1",
        )

    async def start(self, _workspace: Path) -> None:
        self.is_ready = True

    async def switch_workspace(self, _workspace: Path) -> None:
        return None

    async def stop(self) -> None:
        self.is_ready = False


@pytest.fixture
async def release_client(tmp_path: Path):
    app = create_app(WebSettings(data_root=tmp_path, initial_project_id="p1"))
    app.dependency_overrides[get_runtime_host] = _ReleaseRuntimeHost
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await login(client)
            yield client, tmp_path


def test_workspace_v1_fixture_opens_without_directory_migration(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-v1"
    for directory in ("Inputs", "Knowledge", "Templates", "Outputs"):
        (workspace / directory).mkdir(parents=True, exist_ok=True)
    (workspace / "Inputs" / "brief.md").write_text("legacy input", encoding="utf-8")
    before = sorted(path.relative_to(workspace) for path in workspace.rglob("*"))

    # Opening the Phase 1 fixed roots must not rewrite the legacy project into a
    # second nested `projects/` layout.
    for directory in ("Inputs", "Knowledge", "Templates", "Outputs"):
        assert (workspace / directory).is_dir()
    after = sorted(path.relative_to(workspace) for path in workspace.rglob("*"))

    assert set(before).issubset(after)
    assert not (workspace / "projects").exists()


@pytest.mark.asyncio
async def test_hidden_internal_paths_never_enter_public_file_tree(release_client) -> None:
    client, root = release_client
    hidden = root / "p1" / ".manyselves"
    hidden.mkdir(parents=True, exist_ok=True)
    (hidden / "artifact-gateway.key").write_text("secret-key", encoding="utf-8")
    (root / "p1" / "Inputs" / "visible.md").write_text("visible", encoding="utf-8")

    response = await client.get("/api/v1/projects/p1/files/tree")

    assert response.status_code == 200
    payload = response.json()
    paths = [entry["path"] for entry in payload["entries"]]
    assert "Inputs/visible.md" in paths
    assert all(".manyselves" not in path for path in paths)
    assert "artifact-gateway.key" not in response.text


def test_openapi_contract_contains_no_legacy_bearer_or_secret_examples(tmp_path: Path) -> None:
    schema = create_app(WebSettings(data_root=tmp_path, initial_project_id="p1")).openapi()
    serialized = str(schema)

    assert "DeploymentBearer" not in serialized
    assert "MANYSELVES_ACCESS_TOKEN" not in serialized
    assert "artifact-gateway.key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "SessionCookie" in schema["components"]["securitySchemes"]


@pytest.mark.asyncio
async def test_project_logs_and_global_knowledge_do_not_expose_storage_roots(release_client) -> None:
    client, root = release_client

    logs = await client.get("/api/v1/events/logs", params={"projectId": "p1"})
    global_tree = await client.get("/api/v1/global-knowledge/files/tree")

    serialized = "\n".join([logs.text, global_tree.text])
    assert logs.status_code == 200
    assert global_tree.status_code == 200
    assert str(root) not in serialized
    assert ".manyselves/artifact-gateway.key" not in serialized
    assert "api_key" not in serialized
    assert "environment-secret" not in serialized
