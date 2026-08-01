"""HTTP contracts for conversations, Agent commands, reporting, and operations."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from manyselves.application.models import EditResendCommand
from manyselves.config.schema import ApiConfig, AppConfig, ProvidersConfig
from manyselves.core.loops.bus import MessageBus
from manyselves.interfaces.types import AgentResponse, Error, UserMessage
from manyselves.webapi.dependencies import get_runtime_host
from manyselves.webapi.main import create_app
from manyselves.webapi.settings import WebSettings


class _Backend:
    def __init__(self, host: "ResourceRuntimeHost") -> None:
        self.host = host
        self.sent: list[tuple[str, str, str | None, str]] = []
        self.synced: list[tuple[str, list[dict], str | None, bool]] = []
        self.interrupted: list[str] = []
        self.rollbacks: list[tuple[str, str]] = []
        self.rollback_preparations: list[tuple[str, str]] = []
        self.send_attempts = 0
        self.send_error: BaseException | None = None
        self.send_started = asyncio.Event()
        self.send_release: asyncio.Event | None = None
        self.sync_failures_remaining = 0

    async def send_user_message(
        self,
        content: str,
        agent_type: str,
        message_id: str | None = None,
        source: str = "user",
    ) -> None:
        self.send_attempts += 1
        self.send_started.set()
        if self.send_release is not None:
            await self.send_release.wait()
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((content, agent_type, message_id, source))
        await self.host.bus.publish(
            UserMessage(
                content=content,
                agent_type=agent_type,
                message_id=message_id,
                source=source,
            )
        )

    async def send_file_context(self, file_context: dict, agent_type: str) -> None:
        self.sent.append((json.dumps(file_context, sort_keys=True), agent_type, None, "system"))

    async def interrupt_current_message(self, agent_type: str) -> None:
        self.interrupted.append(agent_type)

    async def rollback_to_checkpoint(self, agent_type: str, checkpoint_id: str) -> dict:
        self.rollbacks.append((agent_type, checkpoint_id))
        return {
            "restored_files": 2,
            "conversation_history": [{"role": "user", "content": "before"}],
        }

    async def prepare_rollback(self, agent_type: str, checkpoint_id: str) -> dict:
        self.rollback_preparations.append((agent_type, checkpoint_id))
        if checkpoint_id == "missing-cp":
            raise ValueError("Checkpoint not found: missing-cp")
        return {"checkpoint_id": checkpoint_id, "effect_paths": []}

    async def sync_agent_conversation(
        self,
        agent_type: str,
        messages: list[dict] | None = None,
        session_id: str | None = None,
        clear_pending: bool = False,
    ) -> None:
        if self.sync_failures_remaining:
            self.sync_failures_remaining -= 1
            raise RuntimeError("injected conversation sync failure")
        self.synced.append((agent_type, list(messages or []), session_id, clear_pending))


class _ReportingController:
    def __init__(self, workspace: Path) -> None:
        self.service = SimpleNamespace(workspace=workspace)
        self._tasks: dict[str, asyncio.Task] = {}
        self.calls: list[tuple[str, object]] = []

    def start(self, request) -> dict:
        self.calls.append(("start", request))
        return {"status": "running", "run_id": "report-new", "task_id": "task-new"}

    def status(self, run_id: str) -> dict:
        return {"status": "completed", "run_id": run_id, "active": False, "source": "persisted"}

    def cancel(self, run_id: str) -> bool:
        self.calls.append(("cancel", run_id))
        return True

    def resume_decision(self, decision_id: str, action: str, supplements: list | None) -> dict:
        self.calls.append(("resume_decision", (decision_id, action, supplements)))
        return {"status": "running", "run_id": "report-existing", "task_id": "task-resume"}

    def resume_run(self, run_id: str, **kwargs) -> dict:
        self.calls.append(("resume_run", (run_id, kwargs)))
        return {"status": "running", "run_id": run_id, "task_id": "task-resume"}

    def revise(self, request) -> dict:
        self.calls.append(("revise", request))
        return {"status": "running", "run_id": "report-revision", "task_id": "task-revision"}


class _ToolRegistry:
    def __init__(self, controller: _ReportingController) -> None:
        self.controller = controller

    def get(self, name: str):
        if name == "run_reporting_workflow":
            return SimpleNamespace(controller=self.controller)
        return None


class ResourceRuntimeHost:
    """Lifecycle host with a real bus and existing persistence boundaries."""

    def __init__(self, config: AppConfig) -> None:
        self.is_ready = False
        self.workspace: Path | None = None
        self.statuses = {"main": "idle"}
        self.bus = MessageBus()
        self.config_manager = SimpleNamespace(config=config, save_config=lambda: None)
        self.backend = _Backend(self)
        self.reporting_controller: _ReportingController | None = None
        self.loop_manager = SimpleNamespace(
            get_all_agent_statuses=lambda: dict(self.statuses),
            get_agent_session_id=lambda _agent_id: None,
            get_loop=self._get_loop,
        )
        self._bus_task: asyncio.Task | None = None

    def _get_loop(self, agent_id: str):
        if agent_id != "main" or self.reporting_controller is None:
            return None
        return SimpleNamespace(tools=_ToolRegistry(self.reporting_controller))

    async def start(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.reporting_controller = _ReportingController(self.workspace)
        self.is_ready = True
        self._bus_task = asyncio.create_task(self.bus.process_queue())

    async def stop(self) -> None:
        self.is_ready = False
        self.bus.shutdown()
        if self._bus_task is not None:
            await self._bus_task

    async def switch_workspace(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.reporting_controller = _ReportingController(self.workspace)

    async def mark_failed(self) -> None:
        self.is_ready = False


@pytest.fixture
async def resources(tmp_path: Path):
    secret = "provider-secret-never-return"
    config = AppConfig(
        providers=ProvidersConfig(
            configurations=[
                ApiConfig(
                    id="provider-1",
                    name="Primary",
                    provider="openai",
                    api_key=secret,
                    api_base="https://provider.invalid/v1",
                    enabled=True,
                    default_model="test-model",
                )
            ],
            active="provider-1",
        )
    )
    host = ResourceRuntimeHost(config)
    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="project-1",
            access_token=SecretStr("test-token"),
        )
    )
    app.dependency_overrides[get_runtime_host] = lambda: host
    host.app = app
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            lease = await client.post(
                "/api/v1/control/lease",
                headers={"Authorization": "Bearer test-token"},
                json={"clientId": "browser"},
            )
            assert lease.status_code == 201
            client.headers.update(
                {
                    "Authorization": "Bearer test-token",
                    "X-Control-Lease-Token": lease.json()["leaseToken"],
                }
            )
            yield client, host, tmp_path / "project-1", secret


async def _eventually(predicate, *, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_conversation_round_trip_preserves_store_format(resources) -> None:
    client, host, workspace, _ = resources

    created = await client.post("/api/v1/conversations", json={"name": "Review"})
    session_id = created.json()["sessionId"]
    renamed = await client.patch(
        f"/api/v1/conversations/{session_id}", json={"name": "Final"}
    )

    assert created.status_code == 201
    assert renamed.status_code == 200
    metadata_text = (workspace / ".manyselves/conversations/sessions.json").read_text("utf-8")
    metadata = json.loads(metadata_text)
    assert any(item["id"] == session_id and item["name"] == "Final" for item in metadata)
    assert not metadata_text.endswith("}\n")  # Store keeps its existing indented JSON bytes.

    listed = await client.get("/api/v1/conversations")
    assert any(item["sessionId"] == session_id for item in listed.json()["conversations"])


@pytest.mark.asyncio
async def test_session_activation_syncs_durable_history_under_runtime_lock(resources) -> None:
    client, host, _, _ = resources
    created = await client.post("/api/v1/conversations", json={"name": "Review"})
    session_id = created.json()["sessionId"]

    activated = await client.post(f"/api/v1/conversations/{session_id}/activate")

    assert activated.status_code == 200
    assert activated.json()["active"] is True
    assert host.backend.synced[-1][0] == "main"
    assert host.backend.synced[-1][2] == session_id


@pytest.mark.asyncio
async def test_conversation_activation_rejects_active_agent(resources) -> None:
    client, host, _, _ = resources
    first = await client.post("/api/v1/conversations", json={"name": "First"})
    await client.post("/api/v1/conversations", json={"name": "Second"})
    host.statuses = {"main": "thinking"}

    response = await client.post(
        f"/api/v1/conversations/{first.json()['sessionId']}/activate"
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUNTIME_BUSY"


@pytest.mark.asyncio
async def test_agent_response_is_persisted_to_captured_turn_session(resources) -> None:
    _, host, _, _ = resources
    service = host.app.state.conversation_service
    first = service.create("First", "main")["id"]
    await service._on_message(  # noqa: SLF001
        UserMessage(agent_type="main", content="turn", message_id="captured-turn")
    )
    second = service.create("Second", "main")["id"]

    await service._on_message(  # noqa: SLF001
        AgentResponse(
            agent_type="main",
            content="belongs to first",
            message_id="captured-turn",
            streaming=False,
        )
    )

    assert service.store.switch_session(first, "main") is True
    assert any(item.get("content") == "belongs to first" for item in service.messages("main"))
    assert service.store.switch_session(second, "main") is True
    assert not any(item.get("content") == "belongs to first" for item in service.messages("main"))


@pytest.mark.asyncio
async def test_delete_shared_session_syncs_every_affected_agent(resources) -> None:
    _, host, _, _ = resources
    service = host.app.state.conversation_service
    shared = service.create("Shared", "main")["id"]
    assert service.store.switch_session(shared, "critic") is True
    replacement = service.create("Replacement", "main")["id"]
    host.backend.synced.clear()

    await service.delete(shared, "main")

    assert "critic" in {item[0] for item in host.backend.synced}
    assert service.store.get_current_session_id("main") == replacement


@pytest.mark.asyncio
async def test_resource_services_close_owned_callbacks_and_reporting_tasks(resources) -> None:
    _, host, _, _ = resources
    task = asyncio.create_task(asyncio.Event().wait())
    host.reporting_controller._tasks["pending"] = task

    await host.app.state.reporting_facade.close()
    await host.app.state.conversation_service.close()

    assert task.done()


@pytest.mark.asyncio
async def test_project_activation_rebinds_project_scoped_resource_services(resources) -> None:
    client, host, original_workspace, _ = resources
    created_project = await client.post("/api/v1/projects", json={"projectId": "project-2"})
    activated = await client.post("/api/v1/projects/project-2/activate")
    created_conversation = await client.post(
        "/api/v1/conversations", json={"name": "Project Two"}
    )

    target_workspace = original_workspace.parent / "project-2"
    assert created_project.status_code == 201
    assert activated.status_code == 200
    assert created_conversation.status_code == 201
    assert host.workspace == target_workspace.resolve()
    assert host.app.state.conversation_service.workspace == target_workspace.resolve()
    assert host.app.state.reporting_facade.workspace == target_workspace.resolve()
    assert host.app.state.python_run_service.workspace == target_workspace.resolve()
    assert (target_workspace / ".manyselves/conversations/sessions.json").is_file()
    assert not (original_workspace / ".manyselves/conversations/sessions.json").exists()


@pytest.mark.asyncio
async def test_project_activation_rejects_pending_conversation_persistence(resources) -> None:
    client, host, original_workspace, _ = resources
    await client.post("/api/v1/projects", json={"projectId": "project-2"})
    service = host.app.state.conversation_service
    service._pending_writes = 1  # noqa: SLF001 - deterministic queued callback contract
    try:
        activated = await client.post("/api/v1/projects/project-2/activate")
    finally:
        service._pending_writes = 0  # noqa: SLF001

    assert activated.status_code == 409
    assert activated.json()["error"]["code"] == "RUNTIME_BUSY"
    assert host.workspace == original_workspace.resolve()
    assert host.app.state.project_registry.active_project_id == "project-1"


@pytest.mark.asyncio
async def test_history_replacement_discards_partial_response_buffer(resources) -> None:
    client, host, workspace, _ = resources
    await host.bus.publish(
        AgentResponse(agent_type="main", content="stale-partial", streaming=True)
    )
    await _eventually(lambda: bool(host.app.state.conversation_service._streams))  # noqa: SLF001

    blocked = await client.post("/api/v1/conversations", json={"name": "Fresh"})
    assert blocked.status_code == 409
    await host.bus.publish(
        Error(agent_type="main", source="agent", message="interrupted")
    )
    await _eventually(lambda: not host.app.state.conversation_service._streams)  # noqa: SLF001
    created = await client.post("/api/v1/conversations", json={"name": "Fresh"})
    await host.bus.publish(AgentResponse(agent_type="main", content="fresh", streaming=False))
    await _eventually(
        lambda: (workspace / f".manyselves/conversations/main/{created.json()['sessionId']}.jsonl").exists()
    )

    text = (
        workspace / f".manyselves/conversations/main/{created.json()['sessionId']}.jsonl"
    ).read_text("utf-8")
    assert "fresh" in text
    assert "stale-partial" not in text


@pytest.mark.asyncio
async def test_send_message_is_typed_idempotent_and_persisted(resources) -> None:
    client, host, workspace, _ = resources
    command_id = "92e7fd4c-3d85-4b57-8dd4-8ffcfb16ff11"
    headers = {"Idempotency-Key": command_id}

    first = await client.post(
        "/api/v1/agents/main/messages",
        headers=headers,
        json={"content": "hello", "source": "user", "messageId": "message-1"},
    )
    second = await client.post(
        "/api/v1/agents/main/messages",
        headers=headers,
        json={"content": "hello", "source": "user", "messageId": "message-1"},
    )
    await _eventually(lambda: (workspace / ".manyselves/conversations/sessions.json").exists())

    assert first.status_code == 202
    assert first.json() == {"commandId": command_id, "status": "accepted"}
    assert second.json() == first.json()
    assert host.backend.sent == [("hello", "main", "message-1", "user")]
    sessions = json.loads(
        (workspace / ".manyselves/conversations/sessions.json").read_text("utf-8")
    )
    conversation = workspace / ".manyselves/conversations/main" / f"{sessions[0]['id']}.jsonl"
    assert json.loads(conversation.read_text("utf-8"))["message_id"] == "message-1"


@pytest.mark.asyncio
async def test_unknown_agent_command_returns_not_found(resources) -> None:
    client, _, _, _ = resources

    response = await client.post(
        "/api/v1/agents/missing/messages",
        headers={"Idempotency-Key": "92e7fd4c-3d85-4b57-8dd4-8ffcfb16ff12"},
        json={"content": "hello"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AGENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_edit_resend_truncates_durable_and_in_memory_history_before_send(resources) -> None:
    client, host, workspace, _ = resources
    for message_id, content in (("m1", "first"), ("m2", "replace me")):
        await client.post(
            "/api/v1/agents/main/messages",
            headers={"Idempotency-Key": f"00000000-0000-4000-8000-00000000000{message_id[-1]}"},
            json={"content": content, "messageId": message_id, "source": "user"},
        )
    await host.bus.publish(
        AgentResponse(agent_type="main", content="old answer", message_id="a2")
    )
    await _eventually(lambda: len(host.backend.sent) == 2)

    resent = await client.post(
        "/api/v1/agents/main/messages/m2/edit-resend",
        headers={"Idempotency-Key": "00000000-0000-4000-8000-000000000099"},
        json={"content": "replacement"},
    )
    await _eventually(lambda: len(host.backend.sent) == 3)

    assert resent.status_code == 202
    assert host.backend.synced[-1][3] is True
    session_id = json.loads(
        (workspace / ".manyselves/conversations/sessions.json").read_text("utf-8")
    )[0]["id"]
    records = [
        json.loads(line)
        for line in (workspace / f".manyselves/conversations/main/{session_id}.jsonl")
        .read_text("utf-8")
        .splitlines()
    ]
    assert [record["content"] for record in records if record["role"] == "user"] == [
        "first",
        "replacement",
    ]


@pytest.mark.asyncio
async def test_edit_resend_publish_failure_restores_exact_durable_turn(resources) -> None:
    client, host, workspace, _ = resources
    for message_id, content in (("m1", "first"), ("m2", "keep exact bytes")):
        await client.post(
            "/api/v1/agents/main/messages",
            headers={"Idempotency-Key": f"01000000-0000-4000-8000-00000000000{message_id[-1]}"},
            json={"content": content, "messageId": message_id},
        )
    await _eventually(lambda: len(host.backend.sent) == 2)
    session_id = json.loads(
        (workspace / ".manyselves/conversations/sessions.json").read_text("utf-8")
    )[0]["id"]
    conversation = workspace / f".manyselves/conversations/main/{session_id}.jsonl"
    before = conversation.read_bytes()
    host.backend.send_error = RuntimeError("publish failed before commit")

    response = await client.post(
        "/api/v1/agents/main/messages/m2/edit-resend",
        headers={"Idempotency-Key": "01000000-0000-4000-8000-000000000099"},
        json={"content": "replacement"},
    )

    assert response.status_code == 500
    assert conversation.read_bytes() == before
    assert host.is_ready is True
    assert host.backend.sent == [
        ("first", "main", "m1", "user"),
        ("keep exact bytes", "main", "m2", "user"),
    ]


@pytest.mark.asyncio
async def test_cancelled_edit_resend_waits_for_publish_commit_and_replays_cache(resources) -> None:
    client, host, _, _ = resources
    await client.post(
        "/api/v1/agents/main/messages",
        headers={"Idempotency-Key": "02000000-0000-4000-8000-000000000001"},
        json={"content": "original", "messageId": "m1"},
    )
    await _eventually(lambda: len(host.backend.sent) == 1)
    host.backend.send_started = asyncio.Event()
    host.backend.send_release = asyncio.Event()
    attempts_before = host.backend.send_attempts
    headers = {"Idempotency-Key": "02000000-0000-4000-8000-000000000099"}
    request_task = asyncio.create_task(
        client.post(
            "/api/v1/agents/main/messages/m1/edit-resend",
            headers=headers,
            json={"content": "committed"},
        )
    )
    await host.backend.send_started.wait()
    request_task.cancel()
    host.backend.send_release.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    replay = await client.post(
        "/api/v1/agents/main/messages/m1/edit-resend",
        headers=headers,
        json={"content": "committed"},
    )

    assert replay.status_code == 202
    assert host.backend.send_attempts == attempts_before + 1
    assert host.backend.sent[-1] == ("committed", "main", "m1", "user")


@pytest.mark.asyncio
async def test_facade_cancellation_retains_edit_resend_publish_and_caches_commit(resources) -> None:
    client, host, _, _ = resources
    await client.post(
        "/api/v1/agents/main/messages",
        headers={"Idempotency-Key": "02100000-0000-4000-8000-000000000001"},
        json={"content": "original", "messageId": "m1"},
    )
    service = host.app.state.conversation_service
    await _eventually(lambda: len(service.messages("main")) == 1)
    host.backend.send_started = asyncio.Event()
    host.backend.send_release = asyncio.Event()
    attempts_before = host.backend.send_attempts
    command = EditResendCommand(
        command_id=UUID("02100000-0000-4000-8000-000000000099"),
        lease_token=client.headers["X-Control-Lease-Token"],
        agent_id="main",
        content="committed",
        message_id="m1",
        target_message_id="m1",
    )

    task = asyncio.create_task(
        host.app.state.runtime_facade.edit_resend(
            command,
            prepare=lambda: service.prepare_edit_resend("main", "m1"),
        )
    )
    await host.backend.send_started.wait()
    task.cancel()
    host.backend.send_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    replay = await host.app.state.runtime_facade.edit_resend(
        command,
        prepare=lambda: service.prepare_edit_resend("main", "m1"),
    )

    assert replay.command_id == command.command_id
    assert host.backend.send_attempts == attempts_before + 1
    assert host.backend.sent[-1] == ("committed", "main", "m1", "user")


@pytest.mark.asyncio
async def test_rollback_truncates_durable_tail_after_checkpoint_restore(resources) -> None:
    client, _, workspace, _ = resources
    for message_id, content in (("m1", "before"), ("m2", "rollback target")):
        await client.post(
            "/api/v1/agents/main/messages",
            headers={"Idempotency-Key": f"10000000-0000-4000-8000-00000000000{message_id[-1]}"},
            json={"content": content, "messageId": message_id},
        )

    response = await client.post(
        "/api/v1/agents/main/rollback",
        headers={"Idempotency-Key": "10000000-0000-4000-8000-000000000099"},
        json={"checkpointId": "cp-1", "targetMessageId": "m2"},
    )

    assert response.status_code == 202
    assert response.json()["restoredFiles"] == 2
    session_id = json.loads(
        (workspace / ".manyselves/conversations/sessions.json").read_text("utf-8")
    )[0]["id"]
    text = (workspace / f".manyselves/conversations/main/{session_id}.jsonl").read_text(
        "utf-8"
    )
    assert "before" in text
    assert "rollback target" not in text


@pytest.mark.asyncio
async def test_rollback_prevalidates_durable_target_before_backend_restore(resources) -> None:
    client, host, _, _ = resources

    response = await client.post(
        "/api/v1/agents/main/rollback",
        headers={"Idempotency-Key": "10000000-0000-4000-8000-000000000098"},
        json={"checkpointId": "cp-1", "targetMessageId": "missing"},
    )

    assert response.status_code == 404
    assert host.backend.rollbacks == []


@pytest.mark.asyncio
async def test_missing_checkpoint_fails_before_durable_truncation(resources) -> None:
    client, host, workspace, _ = resources
    await client.post(
        "/api/v1/agents/main/messages",
        headers={"Idempotency-Key": "10000000-0000-4000-8000-000000000097"},
        json={"content": "keep me", "messageId": "m1"},
    )
    await _eventually(lambda: (workspace / ".manyselves/conversations/sessions.json").exists())

    response = await client.post(
        "/api/v1/agents/main/rollback",
        headers={"Idempotency-Key": "10000000-0000-4000-8000-000000000096"},
        json={"checkpointId": "missing-cp", "targetMessageId": "m1"},
    )

    assert response.status_code == 404
    assert host.backend.rollback_preparations == [("main", "missing-cp")]
    assert host.backend.rollbacks == []
    session_id = json.loads(
        (workspace / ".manyselves/conversations/sessions.json").read_text("utf-8")
    )[0]["id"]
    assert "keep me" in (
        workspace / f".manyselves/conversations/main/{session_id}.jsonl"
    ).read_text("utf-8")


@pytest.mark.asyncio
async def test_rollback_postcommit_sync_failure_marks_runtime_consistency_failed(resources) -> None:
    client, host, workspace, _ = resources
    await client.post(
        "/api/v1/agents/main/messages",
        headers={"Idempotency-Key": "11000000-0000-4000-8000-000000000001"},
        json={"content": "durable before", "messageId": "m1"},
    )
    await _eventually(lambda: (workspace / ".manyselves/conversations/sessions.json").exists())
    headers = {"Idempotency-Key": "11000000-0000-4000-8000-000000000099"}
    host.backend.sync_failures_remaining = 1

    first = await client.post(
        "/api/v1/agents/main/rollback",
        headers=headers,
        json={"checkpointId": "cp-1", "targetMessageId": "m1"},
    )
    replay = await client.post(
        "/api/v1/agents/main/rollback",
        headers=headers,
        json={"checkpointId": "cp-1", "targetMessageId": "m1"},
    )

    assert first.status_code == 500
    assert first.json()["error"] == {
        "code": "RUNTIME_CONSISTENCY_FAILED",
        "message": "Runtime consistency could not be guaranteed",
        "retryable": False,
        "details": {},
    }
    assert replay.json()["error"]["code"] == "RUNTIME_CONSISTENCY_FAILED"
    assert host.backend.rollbacks == [("main", "cp-1")]
    assert host.is_ready is False


@pytest.mark.asyncio
async def test_settings_masks_all_provider_secrets(resources) -> None:
    client, _, _, secret = resources

    response = await client.get("/api/v1/settings")

    assert response.status_code == 200
    assert response.json()["providers"][0]["configured"] is True
    assert "apiKey" not in response.json()["providers"][0]
    assert "extraHeaders" not in response.json()["providers"][0]
    assert secret not in response.text


@pytest.mark.asyncio
async def test_settings_rejects_null_required_fields_and_can_clear_secret(resources) -> None:
    client, _, _, _ = resources

    invalid = await client.patch(
        "/api/v1/settings/providers/provider-1", json={"enabled": None}
    )
    cleared = await client.patch(
        "/api/v1/settings/providers/provider-1", json={"apiKey": None}
    )

    assert invalid.status_code == 422
    assert cleared.status_code == 200
    assert cleared.json()["providers"][0]["configured"] is False


@pytest.mark.asyncio
async def test_settings_defaults_reject_explicit_null(resources) -> None:
    client, _, _, _ = resources

    response = await client.patch("/api/v1/settings", json={"model": None})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_reporting_snapshot_covers_durable_state_and_output_metadata(resources) -> None:
    client, _, workspace, _ = resources
    run_id = "report-existing"
    run_root = workspace / "Work/runs" / run_id
    (run_root / "decisions").mkdir(parents=True)
    (workspace / "Outputs/Reports").mkdir(parents=True, exist_ok=True)
    (workspace / "Work/runs" / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "needs_user_decision",
                "output_paths": ["Outputs/Reports/report.docx"],
                "decision_id": "decision-1",
            }
        ),
        encoding="utf-8",
    )
    (run_root / "workflow-state.json").write_text(
        json.dumps({"run_id": run_id, "activity": "coverage", "status": "blocked"}),
        encoding="utf-8",
    )
    (run_root / "decisions/decision-1.json").write_text(
        json.dumps({"decision_id": "decision-1", "status": "pending"}), encoding="utf-8"
    )
    (run_root / "revision-request.json").write_text(
        json.dumps({"baseline_version_id": "version-1", "feedback": "fix"}), encoding="utf-8"
    )
    (run_root / "evidence-choice.json").write_text(
        json.dumps({"selected_action": "supplement"}), encoding="utf-8"
    )
    (workspace / "Outputs/Reports/report.docx").write_bytes(b"docx")

    response = await client.get(f"/api/v1/reporting/runs/{run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["status"] == "needs_user_decision"
    assert body["state"]["activity"] == "coverage"
    assert body["waitingInput"][0]["decision_id"] == "decision-1"
    assert body["checkpoint"]["status"] == "blocked"
    assert body["evidence"]["selected_action"] == "supplement"
    assert body["revision"]["baseline_version_id"] == "version-1"
    assert body["outputs"][0]["path"] == "Outputs/Reports/report.docx"
    assert body["outputs"][0]["size"] == 4
    assert body["outputs"][0]["exists"] is True


@pytest.mark.asyncio
async def test_reporting_persisted_inactive_status_is_explicit(resources) -> None:
    client, _, workspace, _ = resources
    (workspace / "Work/runs").mkdir(parents=True, exist_ok=True)
    (workspace / "Work/runs/inactive.json").write_text(
        json.dumps({"run_id": "inactive", "status": "needs_user_decision"}),
        encoding="utf-8",
    )

    response = await client.get("/api/v1/reporting/runs/inactive")

    assert response.status_code == 200
    assert response.json()["run"]["status"] == "needs_user_decision"
    assert response.json()["run"]["active"] is False


@pytest.mark.asyncio
async def test_reporting_malformed_persisted_state_is_not_silently_empty(resources) -> None:
    client, _, workspace, _ = resources
    (workspace / "Work/runs").mkdir(parents=True, exist_ok=True)
    (workspace / "Work/runs/broken.json").write_text("{broken", encoding="utf-8")

    response = await client.get("/api/v1/reporting/runs/broken")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "REPORT_STATE_INVALID"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="symlink containment contract")
async def test_reporting_rejects_symlinked_run_directory(resources, tmp_path: Path) -> None:
    client, _, workspace, _ = resources
    outside = tmp_path / "outside-run"
    outside.mkdir()
    (outside / "workflow-state.json").write_text(
        json.dumps({"run_id": "escaped", "status": "completed"}), encoding="utf-8"
    )
    runs = workspace / "Work/runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "escaped").symlink_to(outside, target_is_directory=True)

    response = await client.get("/api/v1/reporting/runs/escaped")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REPORT_RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_reporting_named_start_command_returns_accepted(resources) -> None:
    client, host, _, _ = resources

    response = await client.post(
        "/api/v1/reporting/runs",
        headers={"Idempotency-Key": "20000000-0000-4000-8000-000000000001"},
        json={"instruction": "Generate report", "operation": "full_report"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert response.json()["runId"] == "report-new"
    assert host.reporting_controller is not None
    assert host.reporting_controller.calls[0][0] == "start"


@pytest.mark.asyncio
async def test_reporting_cancel_missing_run_returns_not_found(resources) -> None:
    client, _, _, _ = resources

    response = await client.post(
        "/api/v1/reporting/runs/missing/cancel",
        headers={"Idempotency-Key": "20000000-0000-4000-8000-000000000002"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REPORT_RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_python_run_is_project_scoped_bounded_and_queryable(resources) -> None:
    client, host, workspace, _ = resources
    script = workspace / "Work/example.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import os\nprint(os.getcwd())\nprint('x' * 100000)\n",
        encoding="utf-8",
    )

    accepted = await client.post(
        "/api/v1/operations/python",
        headers={"Idempotency-Key": "30000000-0000-4000-8000-000000000001"},
        json={"path": "Work/example.py", "arguments": []},
    )
    assert accepted.status_code == 202
    operation_id = accepted.json()["operationId"]
    await _eventually(
        lambda: host.app.state.python_run_service.get(operation_id).status != "running"
    )
    result = await client.get(f"/api/v1/operations/{operation_id}")

    assert result.status_code == 200
    assert result.json()["status"] == "completed"
    assert str(workspace) in result.json()["stdout"]
    assert result.json()["stdoutTruncated"] is True
    assert len(result.json()["stdout"].encode("utf-8")) <= 64 * 1024


@pytest.mark.asyncio
async def test_python_run_rejects_escape_and_non_python(resources, tmp_path: Path) -> None:
    client, _, workspace, _ = resources
    (workspace / "Work/not-python.txt").parent.mkdir(parents=True, exist_ok=True)
    (workspace / "Work/not-python.txt").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')", encoding="utf-8")

    extension = await client.post(
        "/api/v1/operations/python", json={"path": "Work/not-python.txt", "arguments": []}
    )
    escape = await client.post(
        "/api/v1/operations/python", json={"path": "../outside.py", "arguments": []}
    )

    assert extension.status_code == 422
    assert escape.status_code == 422
    assert extension.json()["error"]["code"] == "INVALID_PYTHON_PATH"
    assert escape.json()["error"]["code"] == "INVALID_PYTHON_PATH"


@pytest.mark.asyncio
async def test_python_run_can_be_interrupted_immediately(resources) -> None:
    client, host, workspace, _ = resources
    script = workspace / "Work/slow.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("import time\nprint('started', flush=True)\ntime.sleep(30)\n", encoding="utf-8")

    accepted = await client.post(
        "/api/v1/operations/python",
        headers={"Idempotency-Key": "30000000-0000-4000-8000-000000000002"},
        json={"path": "Work/slow.py", "arguments": []},
    )
    interrupted = await client.post(
        f"/api/v1/operations/{accepted.json()['operationId']}/interrupt"
    )

    assert interrupted.status_code == 202
    assert interrupted.json()["status"] == "interrupted"
    operation = host.app.state.python_run_service.get(accepted.json()["operationId"])
    assert operation.process is None
    assert operation.task is not None and operation.task.done()
    assert operation.return_code is not None


@pytest.mark.asyncio
async def test_python_status_remains_running_until_pipes_are_drained(resources) -> None:
    client, host, workspace, _ = resources
    script = workspace / "Work/output.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('complete output')\n", encoding="utf-8")
    service = host.app.state.python_run_service
    original_drain = service._drain  # noqa: SLF001
    drain_finished_reading = asyncio.Event()
    release_drain = asyncio.Event()

    async def delayed_drain(stream):
        result = await original_drain(stream)
        drain_finished_reading.set()
        await release_drain.wait()
        return result

    service._drain = delayed_drain  # noqa: SLF001
    accepted = await client.post(
        "/api/v1/operations/python",
        headers={"Idempotency-Key": "30000000-0000-4000-8000-000000000003"},
        json={"path": "Work/output.py", "arguments": []},
    )
    operation = service.get(accepted.json()["operationId"])
    await asyncio.wait_for(drain_finished_reading.wait(), timeout=2)

    assert operation.status == "running"
    assert operation.task is not None and not operation.task.done()
    release_drain.set()
    await operation.task
    assert operation.status == "completed"
    assert operation.stdout == b"complete output\n"


@pytest.mark.asyncio
async def test_windows_termination_uses_owned_job_object(resources, monkeypatch) -> None:
    _, host, _, _ = resources
    service = host.app.state.python_run_service
    service._platform = "nt"  # noqa: SLF001
    service._windows_jobs = {123: 456}  # noqa: SLF001
    closed: list[int] = []
    monkeypatch.setattr(
        service, "_close_windows_job", lambda handle: closed.append(handle)
    )
    monkeypatch.setattr(
        os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(AssertionError("POSIX fallback used")),
    )

    class Process:
        pid = 123
        returncode = None

        async def wait(self):
            self.returncode = -1
            return self.returncode

        def kill(self):
            self.returncode = -1

    await service._terminate(Process())  # noqa: SLF001

    assert closed == [456]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
async def test_python_interrupt_kills_descendants_that_hold_output_pipes(resources) -> None:
    client, _, workspace, _ = resources
    script = workspace / "Work/process-tree.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "ready = pathlib.Path('Work/child-ready')\n"
        "code = \"import pathlib,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path('Work/child-ready').write_text('ready'); time.sleep(3)\"\n"
        "child = subprocess.Popen([sys.executable, '-c', code])\n"
        "while not ready.exists(): time.sleep(0.01)\n"
        "pathlib.Path('Work/child-pid').write_text(str(child.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    accepted = await client.post(
        "/api/v1/operations/python",
        headers={"Idempotency-Key": "30000000-0000-4000-8000-000000000004"},
        json={"path": "Work/process-tree.py", "arguments": []},
    )
    pid_path = workspace / "Work/child-pid"
    await _eventually(pid_path.exists)
    child_pid = int(pid_path.read_text("utf-8"))

    async with asyncio.timeout(1.5):
        interrupted = await client.post(
            f"/api/v1/operations/{accepted.json()['operationId']}/interrupt"
        )

    assert interrupted.status_code == 202
    assert interrupted.json()["status"] == "interrupted"
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
async def test_python_interrupt_kills_descendants_after_parent_exits(resources) -> None:
    client, host, workspace, _ = resources
    script = workspace / "Work/exited-parent.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "ready = pathlib.Path('Work/exited-child-ready')\n"
        "code = \"import pathlib,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path('Work/exited-child-ready').write_text('ready'); time.sleep(3)\"\n"
        "child = subprocess.Popen([sys.executable, '-c', code])\n"
        "while not ready.exists(): time.sleep(0.01)\n"
        "pathlib.Path('Work/exited-child-pid').write_text(str(child.pid))\n",
        encoding="utf-8",
    )
    accepted = await client.post(
        "/api/v1/operations/python",
        headers={"Idempotency-Key": "30000000-0000-4000-8000-000000000005"},
        json={"path": "Work/exited-parent.py", "arguments": []},
    )
    pid_path = workspace / "Work/exited-child-pid"
    await _eventually(pid_path.exists)
    child_pid = int(pid_path.read_text("utf-8"))
    operation = host.app.state.python_run_service.get(accepted.json()["operationId"])
    await _eventually(
        lambda: operation.process is not None and operation.process.returncode is not None
    )

    async with asyncio.timeout(1.5):
        interrupted = await client.post(
            f"/api/v1/operations/{accepted.json()['operationId']}/interrupt"
        )

    assert interrupted.status_code == 202
    assert interrupted.json()["status"] == "interrupted"
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.asyncio
async def test_maintenance_quiesce_rejects_active_runtime(resources) -> None:
    client, host, _, _ = resources
    host.statuses = {"main": "thinking"}

    response = await client.post("/api/v1/maintenance/quiesce")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUNTIME_BUSY"


@pytest.mark.asyncio
async def test_maintenance_requires_matching_opaque_token_and_exposes_state(resources) -> None:
    client, _, _, _ = resources

    quiesced = await client.post("/api/v1/maintenance/quiesce")
    token = quiesced.json()["maintenanceToken"]
    health = await client.get("/api/v1/health/ready")
    rejected = await client.post(
        "/api/v1/maintenance/release", json={"maintenanceToken": "wrong"}
    )
    released = await client.post(
        "/api/v1/maintenance/release", json={"maintenanceToken": token}
    )

    assert quiesced.status_code == 200
    assert health.status_code == 503
    assert health.json() == {"status": "quiesced"}
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "MAINTENANCE_TOKEN_MISMATCH"
    assert released.status_code == 200
    assert released.json()["quiesced"] is False


@pytest.mark.asyncio
async def test_maintenance_flushes_config_file(resources, monkeypatch) -> None:
    _, host, workspace, _ = resources
    config_path = workspace / "manyselves.yaml"
    config_path.write_text("providers: {}\n", encoding="utf-8")
    host.config_manager._settings = SimpleNamespace(config_path=config_path)
    service = host.app.state.maintenance_service
    service.config_manager = host.config_manager
    fsync_calls: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda descriptor: fsync_calls.append(descriptor))

    service._flush_config()

    assert fsync_calls


@pytest.mark.asyncio
async def test_maintenance_refuses_symlinked_config_path(resources, tmp_path: Path) -> None:
    _, host, workspace, _ = resources
    outside = tmp_path / "outside.yaml"
    outside.write_text("providers: {}\n", encoding="utf-8")
    link = workspace / "linked.yaml"
    link.symlink_to(outside)
    host.config_manager._settings = SimpleNamespace(config_path=link)
    service = host.app.state.maintenance_service
    service.config_manager = host.config_manager

    with pytest.raises(ValueError, match="symlink"):
        service._flush_config()


@pytest.mark.asyncio
async def test_quiescence_blocks_late_conversation_persistence(resources) -> None:
    client, host, workspace, _ = resources
    quiesced = await client.post("/api/v1/maintenance/quiesce")

    await host.bus.publish(UserMessage(agent_type="main", content="late"))
    await asyncio.sleep(0.1)

    assert quiesced.status_code == 200
    assert not (workspace / ".manyselves/conversations/sessions.json").exists()
