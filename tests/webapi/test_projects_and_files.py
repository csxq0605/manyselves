"""Real HTTP contracts for project, file, range, and preview APIs."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from manyselves.webapi.dependencies import get_runtime_host
from manyselves.webapi.main import create_app
from manyselves.webapi.settings import WebSettings


class SwitchableRuntimeHost:
    """Lifecycle host with real observable workspace activation behavior."""

    def __init__(self) -> None:
        self.is_ready = False
        self.workspace: Path | None = None
        self.statuses = {"main": "idle"}
        self.loop_manager = SimpleNamespace(
            get_all_agent_statuses=lambda: dict(self.statuses),
            get_agent_session_id=lambda agent_id: None,
        )

    async def start(self, workspace: Path) -> None:
        self.is_ready = True
        self.workspace = Path(workspace).resolve()

    async def switch_workspace(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()

    async def stop(self) -> None:
        self.is_ready = False


@pytest.fixture
async def api(tmp_path: Path):
    host = SwitchableRuntimeHost()
    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="p1",
            access_token=SecretStr("test-token"),
            upload_size_limit_bytes=16,
            preview_size_limit_bytes=1024,
        )
    )
    app.dependency_overrides[get_runtime_host] = lambda: host
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, host, tmp_path


async def acquire_controller(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/control/lease",
        headers={"Authorization": "Bearer test-token"},
        json={"clientId": "controller"},
    )
    assert response.status_code == 201
    return {
        "Authorization": "Bearer test-token",
        "X-Control-Lease-Token": response.json()["leaseToken"],
    }


@pytest.mark.asyncio
async def test_project_crud_and_bootstrap_never_expose_absolute_paths(api) -> None:
    """Project DTOs must remain portable and must not disclose server filesystem layout."""
    client, _, root = api
    headers = await acquire_controller(client)

    created = await client.post("/api/v1/projects", headers=headers, json={"projectId": "p2"})
    listed = await client.get("/api/v1/projects")
    renamed = await client.patch(
        "/api/v1/projects/p2", headers=headers, json={"projectId": "renamed"}
    )
    bootstrap = await client.get("/api/v1/bootstrap")
    deleted = await client.delete("/api/v1/projects/renamed", headers=headers)

    assert created.status_code == 201
    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()["projects"]} == {"p1", "p2"}
    assert renamed.status_code == 200
    assert deleted.status_code == 204
    serialized = " ".join(response.text for response in (created, listed, renamed, bootstrap))
    assert str(root) not in serialized
    assert "\\" not in created.text


@pytest.mark.asyncio
async def test_project_activation_requires_idle_runtime(api) -> None:
    """Switching workspaces while an agent runs could split runtime and file state."""
    client, host, _ = api
    headers = await acquire_controller(client)
    assert (await client.post("/api/v1/projects", headers=headers, json={"projectId": "p2"})).status_code == 201
    host.statuses = {"main": "thinking"}

    response = await client.post("/api/v1/projects/p2/activate", headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUNTIME_BUSY"


@pytest.mark.asyncio
async def test_project_activation_switches_runtime_before_active_project(api) -> None:
    """Successful activation must align subsequent runtime and file operations."""
    client, host, root = api
    headers = await acquire_controller(client)
    await client.post("/api/v1/projects", headers=headers, json={"projectId": "p2"})

    activated = await client.post("/api/v1/projects/p2/activate", headers=headers)
    listed = await client.get("/api/v1/projects")

    assert activated.status_code == 200
    assert activated.json() == {"id": "p2", "active": True}
    assert host.workspace == (root / "p2").resolve()
    assert next(item for item in listed.json()["projects"] if item["id"] == "p2")["active"] is True


@pytest.mark.asyncio
async def test_every_project_and_file_mutation_requires_auth_and_lease(api) -> None:
    """Deployment authentication alone must not authorize workspace mutations."""
    client, _, _ = api
    no_auth = await client.post("/api/v1/projects", json={"projectId": "p2"})
    no_lease = await client.post(
        "/api/v1/projects",
        headers={"Authorization": "Bearer test-token"},
        json={"projectId": "p2"},
    )

    assert no_auth.status_code == 401
    assert no_auth.json()["error"]["code"] == "AUTH_REQUIRED"
    assert no_lease.status_code == 423
    assert no_lease.json()["error"]["code"] == "CONTROL_LEASE_REQUIRED"


@pytest.mark.asyncio
async def test_file_create_read_revision_save_tree_rename_and_delete(api) -> None:
    """The complete file lifecycle must preserve relative DTOs and revision checks."""
    client, _, _ = api
    headers = await acquire_controller(client)
    created = await client.post(
        "/api/v1/projects/p1/files/entries",
        headers=headers,
        json={"path": "Inputs/a.txt", "kind": "file", "content": "one"},
    )
    read = await client.get(
        "/api/v1/projects/p1/files/content", params={"path": "Inputs/a.txt"}
    )
    saved = await client.put(
        "/api/v1/projects/p1/files/content",
        params={"path": "Inputs/a.txt"},
        headers=headers,
        json={"baseRevision": read.json()["revision"], "content": "two"},
    )
    stale = await client.put(
        "/api/v1/projects/p1/files/content",
        params={"path": "Inputs/a.txt"},
        headers=headers,
        json={"baseRevision": read.json()["revision"], "content": "stale"},
    )
    tree = await client.get("/api/v1/projects/p1/files/tree")
    renamed = await client.post(
        "/api/v1/projects/p1/files/rename",
        headers=headers,
        json={"source": "Inputs/a.txt", "destination": "Inputs/b.txt"},
    )
    deleted = await client.delete(
        "/api/v1/projects/p1/files/entries",
        params={"path": "Inputs/b.txt"},
        headers=headers,
    )

    assert created.status_code == 201
    assert read.json()["content"] == "one"
    assert len(read.json()["revision"]) == 64
    assert saved.json()["content"] == "two"
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "FILE_REVISION_CONFLICT"
    assert any(node["path"] == "Inputs/a.txt" for node in tree.json()["entries"])
    assert renamed.status_code == 200
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_http_rejects_encoded_separator_and_duplicate_destination(api) -> None:
    """Encoded path tricks and duplicate creates must return structured safe errors."""
    client, _, _ = api
    headers = await acquire_controller(client)
    first = await client.post(
        "/api/v1/projects/p1/files/entries",
        headers=headers,
        json={"path": "Inputs/a.txt", "kind": "file"},
    )
    duplicate = await client.post(
        "/api/v1/projects/p1/files/entries",
        headers=headers,
        json={"path": "Inputs/a.txt", "kind": "file"},
    )
    traversal = await client.get(
        "/api/v1/projects/p1/files/content", params={"path": "%2e%2e%2fsecret"}
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "DESTINATION_EXISTS"
    assert traversal.status_code == 400
    assert traversal.json()["error"]["code"] == "UNSAFE_WORKSPACE_PATH"


@pytest.mark.asyncio
async def test_upload_is_bounded_and_download_supports_byte_ranges(api) -> None:
    """Uploads must enforce the configured stream limit and downloads must honor ranges."""
    client, _, _ = api
    headers = await acquire_controller(client)
    uploaded = await client.post(
        "/api/v1/projects/p1/files/upload",
        params={"path": "Inputs/data.bin"},
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=b"0123456789",
    )
    partial = await client.get(
        "/api/v1/projects/p1/files/download",
        params={"path": "Inputs/data.bin"},
        headers={"Range": "bytes=2-5"},
    )
    too_large = await client.post(
        "/api/v1/projects/p1/files/upload",
        params={"path": "Inputs/too-large.bin"},
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=b"x" * 17,
    )

    assert uploaded.status_code == 201
    assert partial.status_code == 206
    assert partial.content == b"2345"
    assert partial.headers["content-range"] == "bytes 2-5/10"
    assert partial.headers["accept-ranges"] == "bytes"
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "UPLOAD_TOO_LARGE"


@pytest.mark.asyncio
async def test_preview_endpoint_is_bounded_and_has_only_relative_client_urls(api) -> None:
    """Preview DTOs must use range/content URLs without filesystem disclosure."""
    client, _, root = api
    headers = await acquire_controller(client)
    await client.post(
        "/api/v1/projects/p1/files/upload",
        params={"path": "Inputs/a.pdf"},
        headers={**headers, "Content-Type": "application/pdf"},
        content=b"%PDF-1.7\n%%EOF",
    )

    response = await client.get(
        "/api/v1/projects/p1/files/preview", params={"path": "Inputs/a.pdf"}
    )

    assert response.status_code == 200
    assert response.json()["type"] == "pdf"
    assert response.json()["rangeUrl"].startswith("/api/v1/projects/p1/files/download?")
    assert str(root) not in response.text
