"""Authenticated HTTP contracts for server-managed global knowledge files."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from manyselves.webapi.dependencies import get_runtime_host
from manyselves.webapi.main import create_app
from manyselves.webapi.settings import WebSettings
from tests.webapi.auth_helpers import login


class GlobalKnowledgeRuntimeHost:
    def __init__(self) -> None:
        self.is_ready = False
        self.loop_manager = SimpleNamespace(
            get_all_agent_statuses=lambda: {"main": "idle"},
            get_agent_session_id=lambda _agent_id: None,
        )

    async def start(self, _workspace: Path) -> None:
        self.is_ready = True

    async def switch_workspace(self, _workspace: Path) -> None:
        return None

    async def stop(self) -> None:
        self.is_ready = False


@pytest.fixture
async def global_api(tmp_path: Path):
    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="p1",
            upload_size_limit_bytes=1_024,
            preview_size_limit_bytes=4_096,
        )
    )
    app.dependency_overrides[get_runtime_host] = GlobalKnowledgeRuntimeHost
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, tmp_path


async def acquire_controller(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post("/api/v1/control/lease", json={"clientId": "browser"})
    assert response.status_code == 201
    return {"X-Control-Lease-Token": response.json()["leaseToken"]}


@pytest.mark.asyncio
async def test_global_knowledge_requires_a_session(global_api) -> None:
    client, _ = global_api

    response = await client.get("/api/v1/global-knowledge/files/tree")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_global_upload_never_creates_a_project(global_api) -> None:
    client, root = global_api
    await login(client)
    headers = await acquire_controller(client)

    response = await client.post(
        "/api/v1/global-knowledge/files/upload",
        params={"path": "standard.md", "conflict": "reject"},
        headers=headers,
        content=b"rule",
    )

    assert response.status_code == 201
    assert response.json()["path"] == "standard.md"
    assert (root / ".manyselves" / "global-knowledge" / "standard.md").read_bytes() == b"rule"
    assert not (root / ".manyselves" / "Inputs").exists()
    projects = await client.get("/api/v1/projects")
    assert [project["id"] for project in projects.json()["projects"]] == ["p1"]
    assert str(root) not in response.text
    assert ".manyselves" not in response.text


@pytest.mark.asyncio
async def test_global_files_support_list_edit_preview_replace_download_and_delete(global_api) -> None:
    client, _ = global_api
    await login(client)
    headers = await acquire_controller(client)
    created = await client.post(
        "/api/v1/global-knowledge/files/upload",
        params={"path": "guide.md", "conflict": "reject"},
        headers=headers,
        content=b"# First",
    )
    revision = created.json()["revision"]

    tree = await client.get("/api/v1/global-knowledge/files/tree")
    content = await client.get(
        "/api/v1/global-knowledge/files/content", params={"path": "guide.md"}
    )
    saved = await client.put(
        "/api/v1/global-knowledge/files/content",
        params={"path": "guide.md"},
        headers=headers,
        json={"baseRevision": revision, "content": "# Updated"},
    )
    preview = await client.get(
        "/api/v1/global-knowledge/files/preview", params={"path": "guide.md"}
    )
    downloaded = await client.get(
        "/api/v1/global-knowledge/files/download", params={"path": "guide.md"}
    )
    replaced = await client.post(
        "/api/v1/global-knowledge/files/upload",
        params={
            "path": "guide.md",
            "conflict": "replace",
            "baseRevision": saved.json()["revision"],
        },
        headers=headers,
        content=b"replacement",
    )
    deleted = await client.delete(
        "/api/v1/global-knowledge/files/entries",
        params={"path": "guide.md"},
        headers={**headers, "If-Match": replaced.json()["revision"]},
    )

    assert [entry["path"] for entry in tree.json()["entries"]] == ["guide.md"]
    assert content.json()["content"] == "# First"
    assert saved.status_code == 200
    assert saved.json()["content"] == "# Updated"
    assert preview.status_code == 200
    assert preview.json() == {
        "path": "guide.md",
        "kind": "markdown",
        "content": "# Updated",
        "truncated": False,
    }
    assert downloaded.status_code == 200
    assert downloaded.content == b"# Updated"
    assert downloaded.headers["accept-ranges"] == "bytes"
    assert replaced.status_code == 201
    assert deleted.status_code == 204
    assert not (await client.get("/api/v1/global-knowledge/files/tree")).json()["entries"]


@pytest.mark.asyncio
async def test_global_file_paths_cannot_escape_the_hidden_library(global_api) -> None:
    client, root = global_api
    await login(client)

    response = await client.get(
        "/api/v1/global-knowledge/files/content",
        params={"path": "../p1/Knowledge/private.md"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNSAFE_WORKSPACE_PATH"
    assert str(root) not in response.text
