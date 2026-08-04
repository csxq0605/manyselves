"""Real HTTP contracts for project, file, range, and preview APIs."""

import asyncio
import hashlib
import io
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from manyselves.application.preview_service import PreviewService
from manyselves.application.workspace_files import WorkspaceFiles
from manyselves.webapi.dependencies import get_runtime_host
from manyselves.webapi.main import create_app
from manyselves.webapi.routes import files as file_routes
from manyselves.webapi.settings import WebSettings
from tests.webapi.auth_helpers import login


class SwitchableRuntimeHost:
    """Lifecycle host with real observable workspace activation behavior."""

    def __init__(self) -> None:
        self.is_ready = False
        self.workspace: Path | None = None
        self.statuses = {"main": "idle"}
        self.failed_calls = 0
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

    async def mark_failed(self) -> None:
        self.failed_calls += 1
        self.is_ready = False


@pytest.fixture
async def api(tmp_path: Path):
    host = SwitchableRuntimeHost()
    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="p1",
            upload_size_limit_bytes=16,
            preview_size_limit_bytes=1024,
        )
    )
    app.dependency_overrides[get_runtime_host] = lambda: host
    host.app = app
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await login(client)
            yield client, host, tmp_path


async def acquire_controller(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/control/lease",
        json={"clientId": "controller"},
    )
    assert response.status_code == 201
    return {
        "X-Control-Lease-Token": response.json()["leaseToken"],
    }


@pytest.mark.asyncio
async def test_project_metadata_crud_never_renames_directories_or_exposes_paths(api) -> None:
    """Pencil edits must update portable metadata without changing a stable project ID."""
    client, _, root = api
    headers = await acquire_controller(client)

    created = await client.post(
        "/api/v1/projects",
        headers=headers,
        json={
            "projectId": "p2",
            "displayName": "  Project two  ",
            "description": "Initial description",
        },
    )
    listed = await client.get("/api/v1/projects")
    updated = await client.patch(
        "/api/v1/projects/p2",
        headers=headers,
        json={
            "displayName": "Project two revised",
            "description": "Current description",
            "revision": created.json()["revision"],
        },
    )
    stale = await client.patch(
        "/api/v1/projects/p2",
        headers=headers,
        json={
            "displayName": "Stale edit",
            "description": "Stale description",
            "revision": created.json()["revision"],
        },
    )
    bootstrap = await client.get("/api/v1/bootstrap")

    assert created.status_code == 201
    assert listed.status_code == 200
    assert created.json()["id"] == "p2"
    assert created.json()["displayName"] == "Project two"
    assert created.json()["description"] == "Initial description"
    assert len(created.json()["revision"]) == 64
    assert listed.json()["projects"] == [
        {
            "id": "p1",
            "displayName": "p1",
            "description": "",
            "revision": listed.json()["projects"][0]["revision"],
            "active": True,
        },
        {
            "id": "p2",
            "displayName": "Project two",
            "description": "Initial description",
            "revision": created.json()["revision"],
            "active": False,
        },
    ]
    assert updated.status_code == 200
    assert updated.json()["id"] == "p2"
    assert updated.json()["displayName"] == "Project two revised"
    assert updated.json()["description"] == "Current description"
    assert (root / "p2").is_dir()
    assert not (root / "renamed").exists()
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "PROJECT_METADATA_REVISION_CONFLICT"
    deleted = await client.delete("/api/v1/projects/p2", headers=headers)
    assert deleted.status_code == 204
    serialized = " ".join(response.text for response in (created, listed, updated, bootstrap))
    assert str(root) not in serialized
    assert "\\" not in created.text


@pytest.mark.asyncio
async def test_legacy_project_metadata_update_uses_a_deterministic_fallback_revision(api) -> None:
    """A project created before sidecars existed must be editable without a migration write first."""
    client, _, root = api
    headers = await acquire_controller(client)
    fallback_revision = hashlib.sha256(
        b'{"displayName":"p1","description":""}'
    ).hexdigest()

    updated = await client.patch(
        "/api/v1/projects/p1",
        headers=headers,
        json={
            "displayName": "Migrated label",
            "description": "Now described",
            "revision": fallback_revision,
        },
    )

    assert updated.status_code == 200
    assert updated.json()["id"] == "p1"
    assert updated.json()["displayName"] == "Migrated label"
    assert updated.json()["description"] == "Now described"
    assert (root / "p1").is_dir()
    assert not (root / "p1").is_symlink()


@pytest.mark.asyncio
async def test_project_create_requires_metadata_without_leaving_a_retry_conflict(api) -> None:
    """Omitted metadata must be rejected before it can reserve a project directory."""
    client, _, root = api
    headers = await acquire_controller(client)

    missing_metadata = await client.post(
        "/api/v1/projects",
        headers=headers,
        json={"projectId": "x" * 121},
    )
    assert missing_metadata.status_code == 422
    assert not (root / ("x" * 121)).exists()

    retried = await client.post(
        "/api/v1/projects",
        headers=headers,
        json={
            "projectId": "x" * 121,
            "displayName": "Corrected metadata",
            "description": "",
        },
    )

    assert retried.status_code == 201
    assert (root / ("x" * 121)).is_dir()


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
    assert activated.json()["id"] == "p2"
    assert activated.json()["displayName"] == "p2"
    assert activated.json()["description"] == ""
    assert len(activated.json()["revision"]) == 64
    assert activated.json()["active"] is True
    assert host.workspace == (root / "p2").resolve()
    assert next(item for item in listed.json()["projects"] if item["id"] == "p2")["active"] is True


@pytest.mark.asyncio
async def test_project_activation_resolves_and_commits_under_one_facade_lock(api) -> None:
    """Resolving outside the mutation lock would race queued project deletion or rename."""
    client, host, _ = api
    headers = await acquire_controller(client)
    await client.post("/api/v1/projects", headers=headers, json={"projectId": "p2"})
    registry = host.app.state.project_registry
    facade = host.app.state.runtime_facade
    original_project_root = registry.project_root

    def require_locked_resolution(project_id: str):
        assert facade._mutation_lock.locked()  # noqa: SLF001 - verifies serialization boundary
        return original_project_root(project_id)

    registry.project_root = require_locked_resolution

    response = await client.post("/api/v1/projects/p2/activate", headers=headers)

    assert response.status_code == 200
    assert registry.active_project_id == "p2"


@pytest.mark.asyncio
async def test_project_activation_commit_failure_restores_host_and_registry(api) -> None:
    """A registry commit failure after host switch must restore the previous coherent state."""
    client, host, root = api
    headers = await acquire_controller(client)
    await client.post("/api/v1/projects", headers=headers, json={"projectId": "p2"})
    registry = host.app.state.project_registry
    original_activate = registry.activate

    def fail_activation(project_id: str):
        original_activate(project_id)
        raise RuntimeError("injected registry commit failure")

    registry.activate = fail_activation

    response = await client.post("/api/v1/projects/p2/activate", headers=headers)

    assert response.status_code == 500
    assert host.workspace == (root / "p1").resolve()
    assert registry.active_project_id == "p1"
    assert host.app.state.web_settings.initial_project_id == "p1"


@pytest.mark.asyncio
async def test_activation_reconciles_new_project_when_runtime_rollback_restores_it(api) -> None:
    """A failed switch-back that restores the new runtime must retain matching app state."""
    client, host, root = api
    headers = await acquire_controller(client)
    await client.post("/api/v1/projects", headers=headers, json={"projectId": "p2"})
    registry = host.app.state.project_registry
    original_activate = registry.activate
    original_switch = host.switch_workspace

    def fail_commit(project_id: str):
        original_activate(project_id)
        raise RuntimeError("injected registry commit failure")

    async def restore_new_after_switch_back_failure(workspace: Path) -> None:
        if workspace == (root / "p1").resolve() and host.workspace == (root / "p2").resolve():
            host.is_ready = True
            raise RuntimeError("old workspace restart failed")
        await original_switch(workspace)

    registry.activate = fail_commit
    host.switch_workspace = restore_new_after_switch_back_failure

    response = await client.post("/api/v1/projects/p2/activate", headers=headers)

    assert response.status_code == 500
    assert host.is_ready is True
    assert host.workspace == (root / "p2").resolve()
    assert registry.active_project_id == "p2"
    assert host.app.state.web_settings.initial_project_id == "p2"
    assert host.failed_calls == 0


@pytest.mark.asyncio
async def test_activation_marks_runtime_failed_when_new_state_cannot_reconcile(api) -> None:
    """A ready runtime must become unavailable if persistence cannot match its workspace."""
    client, host, root = api
    headers = await acquire_controller(client)
    await client.post("/api/v1/projects", headers=headers, json={"projectId": "p2"})
    registry = host.app.state.project_registry
    original_activate = registry.activate
    original_restore = registry.restore_active
    original_switch = host.switch_workspace

    def fail_commit(project_id: str):
        original_activate(project_id)
        raise RuntimeError("injected registry commit failure")

    def fail_new_reconciliation(project_id: str):
        if project_id == "p2":
            raise RuntimeError("persistence unavailable")
        return original_restore(project_id)

    async def restore_new_after_switch_back_failure(workspace: Path) -> None:
        if workspace == (root / "p1").resolve() and host.workspace == (root / "p2").resolve():
            host.is_ready = True
            raise RuntimeError("old workspace restart failed")
        await original_switch(workspace)

    registry.activate = fail_commit
    registry.restore_active = fail_new_reconciliation
    host.switch_workspace = restore_new_after_switch_back_failure

    response = await client.post("/api/v1/projects/p2/activate", headers=headers)

    assert response.status_code == 500
    assert host.is_ready is False
    assert host.workspace == (root / "p2").resolve()
    assert host.loop_manager is not None
    assert registry.active_project_id == "p2"
    assert host.app.state.web_settings.initial_project_id == "p1"
    assert host.failed_calls == 1


@pytest.mark.asyncio
async def test_queued_activation_snapshots_rollback_state_only_after_lock(api) -> None:
    """A queued activation must not capture rollback state before an earlier commit."""
    client, host, root = api
    headers = await acquire_controller(client)
    await client.post("/api/v1/projects", headers=headers, json={"projectId": "p2"})
    await client.post("/api/v1/projects", headers=headers, json={"projectId": "p3"})
    registry = host.app.state.project_registry
    facade = host.app.state.runtime_facade
    first_switched = asyncio.Event()
    release_first = asyncio.Event()
    second_called = asyncio.Event()
    original_switch = host.switch_workspace
    original_facade_activation = facade.activate_workspace
    original_registry_activation = registry.activate
    facade_call_count = 0

    async def delayed_first_switch(workspace: Path) -> None:
        await original_switch(workspace)
        if workspace == (root / "p2").resolve() and not first_switched.is_set():
            first_switched.set()
            await release_first.wait()

    async def tracked_activation(**kwargs):
        nonlocal facade_call_count
        facade_call_count += 1
        if facade_call_count == 2:
            second_called.set()
        return await original_facade_activation(**kwargs)

    def fail_p3_commit(project_id: str):
        record = original_registry_activation(project_id)
        if project_id == "p3":
            raise RuntimeError("injected p3 commit failure")
        return record

    host.switch_workspace = delayed_first_switch
    facade.activate_workspace = tracked_activation
    registry.activate = fail_p3_commit
    first = asyncio.create_task(client.post("/api/v1/projects/p2/activate", headers=headers))
    await first_switched.wait()
    second = asyncio.create_task(client.post("/api/v1/projects/p3/activate", headers=headers))
    await second_called.wait()
    release_first.set()

    first_response, second_response = await asyncio.gather(first, second)

    assert first_response.status_code == 200
    assert second_response.status_code == 500
    assert host.workspace == (root / "p2").resolve()
    assert registry.active_project_id == "p2"
    assert host.app.state.web_settings.initial_project_id == "p2"


@pytest.mark.asyncio
async def test_every_project_and_file_mutation_requires_auth_and_lease(api) -> None:
    """A session alone must not authorize workspace mutations."""
    client, _, _ = api
    client.cookies.clear()
    no_auth = await client.post("/api/v1/projects", json={"projectId": "p2"})
    await login(client)
    no_lease = await client.post(
        "/api/v1/projects",
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
        json={
            "source": "Inputs/a.txt",
            "destination": "Inputs/b.txt",
            "baseRevision": saved.json()["revision"],
        },
    )
    deleted = await client.delete(
        "/api/v1/projects/p1/files/entries",
        params={"path": "Inputs/b.txt"},
        headers={**headers, "If-Match": renamed.json()["revision"]},
    )

    assert created.status_code == 201
    assert read.json()["content"] == "one"
    assert len(read.json()["revision"]) == 64
    assert saved.json()["content"] == "two"
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "FILE_REVISION_CONFLICT"
    tree_entry = next(node for node in tree.json()["entries"] if node["path"] == "Inputs/a.txt")
    assert tree_entry["revision"] == saved.json()["revision"]
    assert renamed.status_code == 200
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_http_rename_and_delete_reject_stale_revisions_without_side_effects(api) -> None:
    """Stale destructive requests must return 409 and preserve the current entry."""
    client, _, _ = api
    headers = await acquire_controller(client)
    created = await client.post(
        "/api/v1/projects/p1/files/entries",
        headers=headers,
        json={"path": "Inputs/a.txt", "kind": "file", "content": "one"},
    )
    stale_revision = created.json()["revision"]
    saved = await client.put(
        "/api/v1/projects/p1/files/content",
        params={"path": "Inputs/a.txt"},
        headers=headers,
        json={"baseRevision": stale_revision, "content": "two"},
    )

    renamed = await client.post(
        "/api/v1/projects/p1/files/rename",
        headers=headers,
        json={
            "source": "Inputs/a.txt",
            "destination": "Inputs/b.txt",
            "baseRevision": stale_revision,
        },
    )
    deleted = await client.delete(
        "/api/v1/projects/p1/files/entries",
        params={"path": "Inputs/a.txt"},
        headers={**headers, "If-Match": f'"{stale_revision}"'},
    )
    current = await client.get(
        "/api/v1/projects/p1/files/content", params={"path": "Inputs/a.txt"}
    )

    assert saved.status_code == 200
    assert renamed.status_code == 409
    assert renamed.json()["error"]["code"] == "FILE_REVISION_CONFLICT"
    assert deleted.status_code == 409
    assert deleted.json()["error"]["code"] == "FILE_REVISION_CONFLICT"
    assert current.json()["content"] == "two"


@pytest.mark.asyncio
async def test_tree_revision_scan_runs_off_event_loop_under_read_transaction(
    api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tree hashing must use a worker thread while retaining facade mutation serialization."""
    client, host, root = api
    (root / "p1" / "Inputs" / "nested").mkdir()
    (root / "p1" / "Inputs" / "nested" / "a.txt").write_text("a", encoding="utf-8")
    facade = host.app.state.runtime_facade
    event_loop_thread = threading.get_ident()
    observations: list[tuple[int, bool]] = []
    original_list_tree = WorkspaceFiles.list_tree

    def observed_list_tree(files: WorkspaceFiles, path: str = ""):
        observations.append((threading.get_ident(), facade._mutation_lock.locked()))  # noqa: SLF001
        return original_list_tree(files, path)

    monkeypatch.setattr(WorkspaceFiles, "list_tree", observed_list_tree)

    response = await client.get("/api/v1/projects/p1/files/tree")

    assert response.status_code == 200
    assert len(observations) == 1
    assert observations[0][0] != event_loop_thread
    assert observations[0][1] is True


@pytest.mark.asyncio
async def test_cancelled_tree_scan_retains_lock_until_worker_terminates(
    api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation must not abandon a hashing worker outside the facade read lock."""
    client, host, root = api
    (root / "p1" / "Inputs" / "nested").mkdir()
    (root / "p1" / "Inputs" / "nested" / "a.txt").write_text("a", encoding="utf-8")
    headers = await acquire_controller(client)
    facade = host.app.state.runtime_facade
    first_started = threading.Event()
    first_release = threading.Event()
    first_finished = threading.Event()
    second_started = threading.Event()
    second_saw_first_finished: list[bool] = []
    call_count = 0
    call_lock = threading.Lock()
    original_list_tree = WorkspaceFiles.list_tree

    def blocking_list_tree(files: WorkspaceFiles, path: str = ""):
        nonlocal call_count
        with call_lock:
            call_count += 1
            call_number = call_count
        if call_number == 1:
            first_started.set()
            try:
                assert first_release.wait(timeout=5)
            finally:
                first_finished.set()
        else:
            second_saw_first_finished.append(first_finished.is_set())
            second_started.set()
        return original_list_tree(files, path)

    monkeypatch.setattr(WorkspaceFiles, "list_tree", blocking_list_tree)
    first_request = asyncio.create_task(client.get("/api/v1/projects/p1/files/tree"))
    assert await asyncio.to_thread(first_started.wait, 5)

    first_request.cancel()
    await asyncio.sleep(0)
    first_request.cancel()
    second_request = asyncio.create_task(client.get("/api/v1/projects/p1/files/tree"))
    await asyncio.sleep(0)
    mutation_entered = asyncio.Event()

    async def mutation_probe() -> None:
        async with facade.mutation_transaction(headers["X-Control-Lease-Token"]):
            mutation_entered.set()

    mutation = asyncio.create_task(mutation_probe())
    await asyncio.sleep(0.05)
    first_done_before_release = first_request.done()
    second_entered_before_release = second_started.is_set()
    mutation_entered_before_release = mutation_entered.is_set()
    first_release.set()

    with pytest.raises(asyncio.CancelledError):
        await first_request
    second_response = await asyncio.wait_for(second_request, timeout=5)
    await asyncio.wait_for(mutation, timeout=5)
    async with asyncio.timeout(1), facade.read_transaction():
        lock_reacquired = True

    assert first_done_before_release is False
    assert second_entered_before_release is False
    assert mutation_entered_before_release is False
    assert second_saw_first_finished == [True]
    assert second_response.status_code == 200
    assert lock_reacquired is True


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
async def test_download_range_matrix_and_416_content_range(api) -> None:
    """Range parsing must be strict and every unsatisfied response must expose total size."""
    client, _, _ = api
    headers = await acquire_controller(client)
    await client.post(
        "/api/v1/projects/p1/files/upload",
        params={"path": "Inputs/data.bin"},
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=b"0123456789",
    )
    await client.post(
        "/api/v1/projects/p1/files/upload",
        params={"path": "Inputs/empty.bin"},
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=b"",
    )

    open_ended = await client.get(
        "/api/v1/projects/p1/files/download",
        params={"path": "Inputs/data.bin"},
        headers={"Range": "bytes=7-"},
    )
    suffix = await client.get(
        "/api/v1/projects/p1/files/download",
        params={"path": "Inputs/data.bin"},
        headers={"Range": "bytes=-3"},
    )
    oversized_end = await client.get(
        "/api/v1/projects/p1/files/download",
        params={"path": "Inputs/data.bin"},
        headers={"Range": "bytes=8-999"},
    )
    invalid = [
        await client.get(
            "/api/v1/projects/p1/files/download",
            params={"path": "Inputs/data.bin"},
            headers={"Range": value},
        )
        for value in ("bytes=", "bytes=+1-2", "bytes=1 -2", "bytes=20-30", "bytes=0-1,3-4")
    ]
    empty = await client.get(
        "/api/v1/projects/p1/files/download",
        params={"path": "Inputs/empty.bin"},
        headers={"Range": "bytes=0-0"},
    )

    assert (open_ended.status_code, open_ended.content) == (206, b"789")
    assert (suffix.status_code, suffix.content) == (206, b"789")
    assert oversized_end.content == b"89"
    assert oversized_end.headers["content-range"] == "bytes 8-9/10"
    assert all(response.status_code == 416 for response in invalid)
    assert all(response.headers["content-range"] == "bytes */10" for response in invalid)
    assert all(response.json()["error"]["code"] == "INVALID_BYTE_RANGE" for response in invalid)
    assert empty.status_code == 416
    assert empty.headers["content-range"] == "bytes */0"


@pytest.mark.asyncio
async def test_download_uses_open_descriptor_and_closes_stream(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save after descriptor open must not change the in-flight download bytes."""
    client, _, _ = api
    headers = await acquire_controller(client)
    created = await client.post(
        "/api/v1/projects/p1/files/entries",
        headers=headers,
        json={"path": "Inputs/a.txt", "kind": "file", "content": "old"},
    )
    started = asyncio.Event()
    release = asyncio.Event()
    original_stream_range = file_routes._stream_range

    async def delayed_stream(stream, start: int, length: int):
        started.set()
        await release.wait()
        async for chunk in original_stream_range(stream, start, length):
            yield chunk

    monkeypatch.setattr(file_routes, "_stream_range", delayed_stream)
    download_task = asyncio.create_task(
        client.get("/api/v1/projects/p1/files/download", params={"path": "Inputs/a.txt"})
    )
    await started.wait()
    saved = await client.put(
        "/api/v1/projects/p1/files/content",
        params={"path": "Inputs/a.txt"},
        headers=headers,
        json={"baseRevision": created.json()["revision"], "content": "new"},
    )
    release.set()
    downloaded = await download_task

    assert saved.status_code == 200
    assert downloaded.content == b"old"
    assert downloaded.headers["etag"] == f'"{created.json()["revision"]}"'

    stream = io.BytesIO(b"abcdef")
    iterator = original_stream_range(stream, 0, 6)
    assert await anext(iterator) == b"abcdef"
    await iterator.aclose()
    assert stream.closed is True


@pytest.mark.asyncio
async def test_download_uses_rfc5987_filename_and_invalid_project_is_400(api) -> None:
    """Non-Latin filenames need a portable fallback and invalid IDs are client errors."""
    client, _, _ = api
    headers = await acquire_controller(client)
    await client.post(
        "/api/v1/projects/p1/files/entries",
        headers=headers,
        json={"path": "Inputs/报告.txt", "kind": "file", "content": "x"},
    )

    download = await client.get(
        "/api/v1/projects/p1/files/download", params={"path": "Inputs/报告.txt"}
    )
    invalid = await client.get("/api/v1/projects/%25bad/files/tree")

    disposition = download.headers["content-disposition"]
    assert 'filename="__.txt"' in disposition
    assert "filename*=UTF-8''%E6%8A%A5%E5%91%8A.txt" in disposition
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "INVALID_PROJECT_ID"


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
    assert response.json()["kind"] == "pdf"
    assert response.json()["rangeUrl"].startswith("/api/v1/projects/p1/files/download?")
    assert str(root) not in response.text


@pytest.mark.asyncio
async def test_preview_captures_under_lock_then_parses_off_lock(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CPU parsing must not hold the facade lock, and must use the captured revision bytes."""
    client, _, _ = api
    headers = await acquire_controller(client)
    created = await client.post(
        "/api/v1/projects/p1/files/entries",
        headers=headers,
        json={"path": "Inputs/a.md", "kind": "file", "content": "old"},
    )
    parser_started = threading.Event()
    parser_release = threading.Event()
    original_preview_capture = PreviewService.preview_capture

    def delayed_preview_capture(self, capture, *, content_url: str):
        parser_started.set()
        assert parser_release.wait(timeout=5)
        return original_preview_capture(self, capture, content_url=content_url)

    monkeypatch.setattr(PreviewService, "preview_capture", delayed_preview_capture)
    preview_task = asyncio.create_task(
        client.get("/api/v1/projects/p1/files/preview", params={"path": "Inputs/a.md"})
    )
    assert await asyncio.to_thread(parser_started.wait, 5)
    try:
        saved = await asyncio.wait_for(
            client.put(
                "/api/v1/projects/p1/files/content",
                params={"path": "Inputs/a.md"},
                headers=headers,
                json={"baseRevision": created.json()["revision"], "content": "new"},
            ),
            timeout=2,
        )
    finally:
        parser_release.set()
    preview = await preview_task

    assert saved.status_code == 200
    assert preview.status_code == 200
    assert preview.json()["content"] == "old"
