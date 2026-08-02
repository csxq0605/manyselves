"""HTTP contracts for conversations, Agent commands, reporting, and operations."""

import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from manyselves.application.errors import RuntimeConsistencyFailedError
from manyselves.application.models import EditResendCommand
from manyselves.config import ConfigManager
from manyselves.config.schema import ApiConfig, AppConfig, ProvidersConfig
from manyselves.core.loops.bus import MessageBus
from manyselves.core.preset_sync import SyncError
from manyselves.interfaces.types import (
    AgentResponse,
    ApiDebugMessage,
    Error,
    SystemNotice,
    UserMessage,
)
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
        self.send_side_effect = None
        self.sync_failures_remaining = 0
        self.restart_calls: list[str] = []
        self.restart_error: BaseException | None = None
        self.restart_failures_remaining = 0
        self.debug_modes: dict[str, bool] = {}

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
        if self.send_side_effect is not None:
            self.send_side_effect()
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

    async def restart_agents_and_wait(self, reason: str) -> None:
        self.restart_calls.append(reason)
        if self.restart_failures_remaining:
            self.restart_failures_remaining -= 1
            raise RuntimeError("injected transient restart failure")
        if self.restart_error is not None:
            raise self.restart_error

    def set_agent_debug_mode(self, agent_type: str, enabled: bool) -> None:
        if agent_type != "main":
            raise KeyError(agent_type)
        self.debug_modes[agent_type] = enabled


class _ReportingController:
    def __init__(self, workspace: Path) -> None:
        self.service = SimpleNamespace(workspace=workspace)
        self._tasks: dict[str, asyncio.Task] = {}
        self.calls: list[tuple[str, object]] = []
        self.live_runs: set[str] = set()

    def start(self, request) -> dict:
        self.calls.append(("start", request))
        self.live_runs.add("report-new")
        run_root = self.service.workspace / "Work/runs/report-new"
        run_root.mkdir(parents=True, exist_ok=True)
        (run_root / "request.json").write_text(
            json.dumps(request.model_dump(mode="json")), encoding="utf-8"
        )
        return {"status": "running", "run_id": "report-new", "task_id": "task-new"}

    def status(self, run_id: str) -> dict:
        if run_id in self.live_runs:
            return {
                "status": "running",
                "run_id": run_id,
                "active": True,
                "source": "live",
                "task_id": "task-new",
            }
        return {
            "status": "completed",
            "run_id": run_id,
            "active": False,
            "source": "persisted",
            "task_id": "persisted-task",
        }

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
        self.manager_factory_calls = 0
        self.replace_calls: list[bool] = []
        self.replace_error: BaseException | None = None
        self.replace_failures_remaining = 0
        self.recovery_error: BaseException | None = None
        self.loop_manager = self._new_loop_manager()
        self._bus_task: asyncio.Task | None = None

    def _new_loop_manager(self):
        self.manager_factory_calls += 1
        return SimpleNamespace(
            provider_registry={
                item.id: {
                    "provider": item.provider,
                    "api_key": item.api_key,
                    "api_base": item.api_base,
                    "enabled": item.enabled,
                }
                for item in self.config_manager.config.providers.configurations
                if item.enabled and item.api_key
            },
            get_all_agent_statuses=lambda: dict(self.statuses),
            get_agent_session_id=lambda _agent_id: None,
            get_loop=self._get_loop,
            get_agent_debug_mode=lambda agent_id: self.backend.debug_modes.get(agent_id, False),
        )

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

    async def replace_loop_manager(self, *, recovery: bool = False) -> None:
        self.replace_calls.append(recovery)
        if recovery and self.recovery_error is not None:
            self.loop_manager = None
            self.is_ready = False
            raise self.recovery_error
        if not recovery and self.replace_failures_remaining:
            self.replace_failures_remaining -= 1
            self.loop_manager = None
            self.is_ready = False
            raise RuntimeError("injected transient replacement failure")
        if not recovery and self.replace_error is not None:
            self.loop_manager = None
            self.is_ready = False
            raise self.replace_error
        self.loop_manager = self._new_loop_manager()
        self.is_ready = True

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


async def _assert_task_remains_pending(task: asyncio.Task[object]) -> None:
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await asyncio.shield(task)
    assert not task.done()


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
    watcher = asyncio.create_task(asyncio.Event().wait())
    host.reporting_controller._tasks["pending"] = task
    host.reporting_controller._status_watchers = {"pending": watcher}

    await host.app.state.reporting_facade.close()
    await host.app.state.conversation_service.close()

    assert task.done()
    assert watcher.done()


@pytest.mark.asyncio
async def test_shutdown_stops_producers_before_draining_resource_consumers(tmp_path: Path) -> None:
    config = AppConfig()
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
    events: list[str] = []
    original_stop = host.stop

    async def stop_producers() -> None:
        events.append("producers")

    async def stop_bus() -> None:
        events.append("bus")
        await original_stop()

    async def legacy_stop() -> None:
        events.append("legacy-stop")
        await original_stop()

    host.stop_producers = stop_producers
    host.stop_bus = stop_bus
    host.stop = legacy_stop
    async with app.router.lifespan_context(app):
        reporting_close = app.state.reporting_facade.close
        python_close = app.state.python_run_service.close
        conversation_close = app.state.conversation_service.close

        async def close_reporting() -> None:
            events.append("reporting")
            await reporting_close()

        async def close_python() -> None:
            events.append("python")
            await python_close()

        async def close_conversations() -> None:
            events.append("conversations")
            await conversation_close()

        app.state.reporting_facade.close = close_reporting
        app.state.python_run_service.close = close_python
        app.state.conversation_service.close = close_conversations

    assert events == ["producers", "reporting", "python", "conversations", "bus"]


@pytest.mark.asyncio
async def test_shutdown_retries_one_transient_producer_stop_before_draining(
    tmp_path: Path,
) -> None:
    host = ResourceRuntimeHost(AppConfig())
    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="project-1",
            access_token=SecretStr("test-token"),
        )
    )
    app.dependency_overrides[get_runtime_host] = lambda: host
    host.app = app
    events: list[str] = []
    attempts = 0
    original_stop = host.stop

    async def stop_producers() -> None:
        nonlocal attempts
        attempts += 1
        events.append(f"producers-{attempts}")
        if attempts == 1:
            raise RuntimeError("transient producer stop")

    async def stop_bus() -> None:
        events.append("bus")
        await original_stop()

    host.stop_producers = stop_producers
    host.stop_bus = stop_bus
    async with app.router.lifespan_context(app):
        reporting_close = app.state.reporting_facade.close
        python_close = app.state.python_run_service.close
        conversation_close = app.state.conversation_service.close

        async def close_reporting() -> None:
            events.append("reporting")
            await reporting_close()

        async def close_python() -> None:
            events.append("python")
            await python_close()

        async def close_conversations() -> None:
            events.append("conversations")
            await conversation_close()

        app.state.reporting_facade.close = close_reporting
        app.state.python_run_service.close = close_python
        app.state.conversation_service.close = close_conversations

    assert events == [
        "producers-1",
        "producers-2",
        "reporting",
        "python",
        "conversations",
        "bus",
    ]


@pytest.mark.asyncio
async def test_shutdown_cancellation_waits_for_definite_producer_cleanup_then_propagates(
    tmp_path: Path,
) -> None:
    """Caller cancellation must neither cancel owned cleanup nor disappear."""
    host = ResourceRuntimeHost(AppConfig())
    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="project-1",
            access_token=SecretStr("test-token"),
        )
    )
    app.dependency_overrides[get_runtime_host] = lambda: host
    host.app = app
    entered = asyncio.Event()
    leave = asyncio.Event()
    producer_started = asyncio.Event()
    producer_release = asyncio.Event()
    attempts = 0
    events: list[str] = []
    original_stop = host.stop

    async def stop_producers() -> None:
        nonlocal attempts
        attempts += 1
        events.append("producer-start")
        producer_started.set()
        await producer_release.wait()
        events.append("producer-complete")

    async def stop_bus() -> None:
        events.append("bus")
        await original_stop()

    host.stop_producers = stop_producers
    host.stop_bus = stop_bus

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            reporting_close = app.state.reporting_facade.close
            python_close = app.state.python_run_service.close
            conversation_close = app.state.conversation_service.close

            async def close_reporting() -> None:
                events.append("reporting")
                await reporting_close()

            async def close_python() -> None:
                events.append("python")
                await python_close()

            async def close_conversations() -> None:
                events.append("conversations")
                await conversation_close()

            app.state.reporting_facade.close = close_reporting
            app.state.python_run_service.close = close_python
            app.state.conversation_service.close = close_conversations
            entered.set()
            await leave.wait()

    task = asyncio.create_task(run_lifespan())
    await entered.wait()
    leave.set()
    await producer_started.wait()
    task.cancel()
    producer_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert attempts == 1
    assert events == [
        "producer-start",
        "producer-complete",
        "reporting",
        "python",
        "conversations",
        "bus",
    ]


@pytest.mark.asyncio
async def test_shutdown_cancellation_during_begin_establishes_orderly_drain_before_stop(
    tmp_path: Path,
) -> None:
    """A queued event must remain durable when cancellation lands at the mutation lock."""
    host = ResourceRuntimeHost(AppConfig())
    host.persistence_ready = True
    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="project-1",
            access_token=SecretStr("test-token"),
        )
    )
    app.dependency_overrides[get_runtime_host] = lambda: host
    host.app = app
    ready = asyncio.Event()
    leave = asyncio.Event()
    begin_waiting = asyncio.Event()
    events: list[str] = []
    state: dict[str, object] = {}
    original_stop = host.stop

    async def begin_orderly_shutdown() -> None:
        events.append("orderly-grant")

    async def stop_producers() -> None:
        service = app.state.conversation_service
        events.append("producers")
        await _eventually(
            lambda: any(
                item.get("content") == "accepted-before-shutdown"
                for item in service.messages("main")
            )
        )
        host.persistence_ready = False

    async def stop_bus() -> None:
        events.append("bus")
        await original_stop()

    host.begin_orderly_shutdown = begin_orderly_shutdown
    host.stop_producers = stop_producers
    host.stop_bus = stop_bus

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            facade = app.state.runtime_facade
            service = app.state.conversation_service
            service.create("Shutdown", "main")
            original_begin = facade.begin_shutdown

            async def begin_shutdown() -> None:
                begin_waiting.set()
                await original_begin()
                events.append("begin-complete")

            facade.begin_shutdown = begin_shutdown
            for name, resource in (
                ("reporting", app.state.reporting_facade),
                ("python", app.state.python_run_service),
                ("conversations", service),
            ):
                original_close = resource.close

                async def close_resource(
                    *, stage: str = name, close=original_close
                ) -> None:
                    await close()
                    events.append(stage)

                resource.close = close_resource
            state["facade"] = facade
            state["service"] = service
            await facade._mutation_lock.acquire()  # noqa: SLF001
            ready.set()
            await leave.wait()

    task = asyncio.create_task(run_lifespan())
    await ready.wait()
    leave.set()
    await begin_waiting.wait()
    await asyncio.sleep(0)
    await host.bus.publish(
        UserMessage(
            agent_type="main",
            content="accepted-before-shutdown",
            message_id="shutdown-message",
        )
    )
    service = state["service"]
    assert hasattr(service, "_pending_writes")
    await _eventually(lambda: service._pending_writes == 1)  # type: ignore[attr-defined]  # noqa: SLF001
    task.cancel()
    await asyncio.sleep(0)
    facade = state["facade"]
    assert hasattr(facade, "_mutation_lock")
    facade._mutation_lock.release()  # type: ignore[attr-defined]  # noqa: SLF001

    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == [
        "orderly-grant",
        "begin-complete",
        "producers",
        "reporting",
        "python",
        "conversations",
        "bus",
    ]
    assert service._pending_writes == 0  # type: ignore[attr-defined]  # noqa: SLF001
    assert any(
        item.get("content") == "accepted-before-shutdown"
        for item in service.messages("main")  # type: ignore[attr-defined]
    )
    assert host._bus_task is not None and host._bus_task.done()  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled_stage", ["reporting", "python", "conversations"])
async def test_shutdown_cancellation_during_resource_close_finishes_pipeline_once(
    tmp_path: Path,
    cancelled_stage: str,
) -> None:
    host = ResourceRuntimeHost(AppConfig())
    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="project-1",
            access_token=SecretStr("test-token"),
        )
    )
    app.dependency_overrides[get_runtime_host] = lambda: host
    host.app = app
    ready = asyncio.Event()
    leave = asyncio.Event()
    stage_started = asyncio.Event()
    stage_release = asyncio.Event()
    events: list[str] = []
    state: dict[str, object] = {}
    original_closes: list[object] = []
    original_stop = host.stop

    async def stop_producers() -> None:
        events.append("producers")

    async def stop_bus() -> None:
        events.append("bus-start")
        await original_stop()
        events.append("bus-complete")

    host.stop_producers = stop_producers
    host.stop_bus = stop_bus

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            reporting_task = asyncio.create_task(asyncio.Event().wait())
            reporting_watcher = asyncio.create_task(asyncio.Event().wait())
            host.reporting_controller._tasks["shutdown-task"] = reporting_task
            host.reporting_controller._status_watchers = {
                "shutdown-task": reporting_watcher
            }
            workspace = app.state.python_run_service.workspace
            script = workspace / "shutdown-process.py"
            script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
            operation = app.state.python_run_service.start(
                UUID("03000000-0000-4000-8000-000000000001"),
                "shutdown-process.py",
                [],
            )
            await operation.launch_ready.wait()
            await host.bus.publish(
                UserMessage(
                    agent_type="main",
                    content="persist-before-close",
                    message_id="resource-close-message",
                )
            )
            await _eventually(
                lambda: any(
                    item.get("content") == "persist-before-close"
                    for item in app.state.conversation_service.messages("main")
                )
            )

            for name, resource in (
                ("reporting", app.state.reporting_facade),
                ("python", app.state.python_run_service),
                ("conversations", app.state.conversation_service),
            ):
                original_close = resource.close
                original_closes.append(original_close)

                async def close_resource(
                    *, stage: str = name, close=original_close
                ) -> None:
                    events.append(f"{stage}-start")
                    if stage == cancelled_stage:
                        stage_started.set()
                        await stage_release.wait()
                    await close()
                    events.append(f"{stage}-complete")

                resource.close = close_resource

            state.update(
                reporting_task=reporting_task,
                reporting_watcher=reporting_watcher,
                operation=operation,
                conversation_service=app.state.conversation_service,
            )
            ready.set()
            await leave.wait()

    task = asyncio.create_task(run_lifespan())
    await ready.wait()
    leave.set()
    await stage_started.wait()
    task.cancel()
    task.cancel()
    stage_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    try:
        assert events == [
            "producers",
            "reporting-start",
            "reporting-complete",
            "python-start",
            "python-complete",
            "conversations-start",
            "conversations-complete",
            "bus-start",
            "bus-complete",
        ]
        assert state["reporting_task"].done()  # type: ignore[union-attr]
        assert state["reporting_watcher"].done()  # type: ignore[union-attr]
        operation = state["operation"]
        assert operation.task is not None and operation.task.done()  # type: ignore[union-attr]
        assert operation.process is None  # type: ignore[union-attr]
        conversation_service = state["conversation_service"]
        assert conversation_service._closed is True  # type: ignore[union-attr]  # noqa: SLF001
        assert conversation_service._pending_writes == 0  # type: ignore[union-attr]  # noqa: SLF001
        assert host._bus_task is not None and host._bus_task.done()  # noqa: SLF001
    finally:
        for close in original_closes:
            await close()  # type: ignore[operator]
        if host._bus_task is not None and not host._bus_task.done():  # noqa: SLF001
            await original_stop()


@pytest.mark.asyncio
async def test_shutdown_cancellation_during_stop_bus_finishes_bus_once(tmp_path: Path) -> None:
    host = ResourceRuntimeHost(AppConfig())
    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="project-1",
            access_token=SecretStr("test-token"),
        )
    )
    app.dependency_overrides[get_runtime_host] = lambda: host
    host.app = app
    ready = asyncio.Event()
    leave = asyncio.Event()
    bus_started = asyncio.Event()
    bus_release = asyncio.Event()
    bus_attempts = 0
    original_stop = host.stop

    async def stop_producers() -> None:
        return None

    async def stop_bus() -> None:
        nonlocal bus_attempts
        bus_attempts += 1
        bus_started.set()
        await bus_release.wait()
        await original_stop()

    host.stop_producers = stop_producers
    host.stop_bus = stop_bus

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            ready.set()
            await leave.wait()

    task = asyncio.create_task(run_lifespan())
    await ready.wait()
    leave.set()
    await bus_started.wait()
    task.cancel()
    task.cancel()
    bus_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    try:
        assert bus_attempts == 1
        assert host._bus_task is not None and host._bus_task.done()  # noqa: SLF001
    finally:
        if host._bus_task is not None and not host._bus_task.done():  # noqa: SLF001
            await original_stop()


@pytest.mark.asyncio
async def test_shutdown_cancellation_during_legacy_stop_finishes_host_once(tmp_path: Path) -> None:
    host = ResourceRuntimeHost(AppConfig())
    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="project-1",
            access_token=SecretStr("test-token"),
        )
    )
    app.dependency_overrides[get_runtime_host] = lambda: host
    host.app = app
    ready = asyncio.Event()
    leave = asyncio.Event()
    stop_started = asyncio.Event()
    stop_release = asyncio.Event()
    stop_attempts = 0
    original_stop = host.stop

    async def legacy_stop() -> None:
        nonlocal stop_attempts
        stop_attempts += 1
        stop_started.set()
        await stop_release.wait()
        await original_stop()

    host.stop = legacy_stop

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            ready.set()
            await leave.wait()

    task = asyncio.create_task(run_lifespan())
    await ready.wait()
    leave.set()
    await stop_started.wait()
    task.cancel()
    stop_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    try:
        assert stop_attempts == 1
        assert host._bus_task is not None and host._bus_task.done()  # noqa: SLF001
    finally:
        if host._bus_task is not None and not host._bus_task.done():  # noqa: SLF001
            await original_stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_stage", "error_type"),
    [
        (stage, error_type)
        for stage in ("begin", "producers", "reporting", "python", "conversations", "bus")
        for error_type in (RuntimeError, asyncio.CancelledError)
    ],
)
async def test_shutdown_owned_stage_failure_stops_before_dependencies(
    tmp_path: Path,
    failed_stage: str,
    error_type: type[BaseException],
) -> None:
    host = ResourceRuntimeHost(AppConfig())
    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="project-1",
            access_token=SecretStr("test-token"),
        )
    )
    app.dependency_overrides[get_runtime_host] = lambda: host
    host.app = app
    events: list[str] = []
    originals: dict[str, object] = {"host_stop": host.stop}

    def fail_or_continue(stage: str) -> None:
        events.append(stage)
        if stage == failed_stage:
            raise error_type(f"owned {stage} failure")

    async def stop_producers() -> None:
        fail_or_continue("producers")

    async def stop_bus() -> None:
        fail_or_continue("bus")
        await originals["host_stop"]()  # type: ignore[operator]

    host.stop_producers = stop_producers
    host.stop_bus = stop_bus

    expected_error = (
        pytest.raises(asyncio.CancelledError)
        if error_type is asyncio.CancelledError
        else pytest.raises(RuntimeError, match=f"owned {failed_stage} failure")
    )
    try:
        with expected_error:
            async with app.router.lifespan_context(app):
                facade = app.state.runtime_facade
                original_begin = facade.begin_shutdown
                originals["begin"] = original_begin

                async def begin_shutdown() -> None:
                    fail_or_continue("begin")
                    await original_begin()

                facade.begin_shutdown = begin_shutdown
                for name, resource in (
                    ("reporting", app.state.reporting_facade),
                    ("python", app.state.python_run_service),
                    ("conversations", app.state.conversation_service),
                ):
                    original_close = resource.close
                    originals[name] = original_close

                    async def close_resource(
                        *, stage: str = name, close=original_close
                    ) -> None:
                        fail_or_continue(stage)
                        await close()

                    resource.close = close_resource

        expected = ["begin"]
        if failed_stage != "begin":
            expected.append("producers")
            if failed_stage == "producers":
                expected.append("producers")
            else:
                for stage in ("reporting", "python", "conversations", "bus"):
                    expected.append(stage)
                    if stage == failed_stage:
                        break
        assert events == expected
        assert host._bus_task is not None and not host._bus_task.done()  # noqa: SLF001
    finally:
        reporting = getattr(app.state, "reporting_facade", None)
        python_runs = getattr(app.state, "python_run_service", None)
        conversations = getattr(app.state, "conversation_service", None)
        if reporting is not None and "reporting" in originals:
            await originals["reporting"]()  # type: ignore[operator]
        if python_runs is not None and "python" in originals:
            await originals["python"]()  # type: ignore[operator]
        if conversations is not None and "conversations" in originals:
            await originals["conversations"]()  # type: ignore[operator]
        if host._bus_task is not None and not host._bus_task.done():  # noqa: SLF001
            await originals["host_stop"]()  # type: ignore[operator]


@pytest.mark.asyncio
async def test_shutdown_retries_producer_self_cancellation_without_cancelling_caller(
    tmp_path: Path,
) -> None:
    """A cancelled cleanup task is an owned failure, not caller cancellation."""
    host = ResourceRuntimeHost(AppConfig())
    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="project-1",
            access_token=SecretStr("test-token"),
        )
    )
    app.dependency_overrides[get_runtime_host] = lambda: host
    host.app = app
    attempts = 0
    original_stop = host.stop

    async def stop_producers() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError

    async def stop_bus() -> None:
        await original_stop()

    host.stop_producers = stop_producers
    host.stop_bus = stop_bus

    async with app.router.lifespan_context(app):
        pass

    assert attempts == 2
    assert host._bus_task is not None and host._bus_task.done()  # noqa: SLF001


@pytest.mark.asyncio
async def test_shutdown_persistent_producer_failure_retains_downstream_for_retry(
    tmp_path: Path,
) -> None:
    host = ResourceRuntimeHost(AppConfig())
    app = create_app(
        WebSettings(
            data_root=tmp_path,
            initial_project_id="project-1",
            access_token=SecretStr("test-token"),
        )
    )
    app.dependency_overrides[get_runtime_host] = lambda: host
    host.app = app
    events: list[str] = []
    first_error = RuntimeError("first producer stop")
    producer_errors: list[BaseException | None] = [
        first_error,
        RuntimeError("second producer stop"),
        None,
    ]
    original_stop = host.stop

    async def stop_producers() -> None:
        events.append("producers")
        error = producer_errors.pop(0)
        if error is not None:
            raise error

    async def stop_bus() -> None:
        events.append("bus")
        await original_stop()

    host.stop_producers = stop_producers
    host.stop_bus = stop_bus
    with pytest.raises(RuntimeError, match="first producer stop") as raised:
        async with app.router.lifespan_context(app):
            reporting_close = app.state.reporting_facade.close
            python_close = app.state.python_run_service.close
            conversation_close = app.state.conversation_service.close

            async def close_reporting() -> None:
                events.append("reporting")
                await reporting_close()

            async def close_python() -> None:
                events.append("python")
                await python_close()

            async def close_conversations() -> None:
                events.append("conversations")
                await conversation_close()

            app.state.reporting_facade.close = close_reporting
            app.state.python_run_service.close = close_python
            app.state.conversation_service.close = close_conversations

    assert raised.value is first_error
    assert events == ["producers", "producers"]
    assert app.state.conversation_service._closed is False  # noqa: SLF001
    assert host._bus_task is not None and not host._bus_task.done()  # noqa: SLF001

    await host.stop_producers()
    await app.state.reporting_facade.close()
    await app.state.python_run_service.close()
    await app.state.conversation_service.close()
    await host.stop_bus()

    assert events == [
        "producers",
        "producers",
        "producers",
        "reporting",
        "python",
        "conversations",
        "bus",
    ]


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
async def test_project_activation_rejects_queued_agent_work(resources) -> None:
    client, host, original_workspace, _ = resources
    await client.post("/api/v1/projects", json={"projectId": "project-2"})
    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait("old-workspace-turn")
    host.loop_manager._loops = {"main": SimpleNamespace(_message_queue=queue)}

    activated = await client.post("/api/v1/projects/project-2/activate")

    assert activated.status_code == 409
    assert activated.json()["error"]["code"] == "RUNTIME_BUSY"
    assert host.workspace == original_workspace.resolve()


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
async def test_interrupt_system_notice_clears_captured_stream_state(resources) -> None:
    _, host, _, _ = resources
    service = host.app.state.conversation_service
    session = service.store.get_current_session_id("main")
    service._streams[(session, "main", "m1")] = "partial"  # noqa: SLF001
    service._message_sessions[("main", "m1")] = session  # noqa: SLF001

    await service._on_message(  # noqa: SLF001
        SystemNotice(agent_type="main", content="Interrupted", kind="interrupt")
    )

    assert not any(key[1] == "main" for key in service._streams)  # noqa: SLF001
    assert not any(key[0] == "main" for key in service._message_sessions)  # noqa: SLF001


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
@pytest.mark.parametrize("busy_kind", ["active", "stream", "persistence"])
async def test_edit_resend_rejects_non_idle_agent_state(resources, busy_kind: str) -> None:
    client, host, _, _ = resources
    await client.post(
        "/api/v1/agents/main/messages",
        headers={"Idempotency-Key": "01100000-0000-4000-8000-000000000001"},
        json={"content": "original", "messageId": "m1"},
    )
    service = host.app.state.conversation_service
    await _eventually(lambda: len(service.messages("main")) == 1)
    attempts = host.backend.send_attempts
    if busy_kind == "active":
        host.statuses = {"main": "thinking"}
    elif busy_kind == "stream":
        session = service.store.get_current_session_id("main")
        service._streams[(session, "main", "m1")] = "partial"  # noqa: SLF001
    else:
        service._pending_writes = 1  # noqa: SLF001

    try:
        response = await client.post(
            "/api/v1/agents/main/messages/m1/edit-resend",
            headers={"Idempotency-Key": f"01100000-0000-4000-8000-00000000000{2 + ['active', 'stream', 'persistence'].index(busy_kind)}"},
            json={"content": "replacement"},
        )
    finally:
        if busy_kind == "persistence":
            service._pending_writes = 0  # noqa: SLF001

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUNTIME_BUSY"
    assert host.backend.send_attempts == attempts


@pytest.mark.asyncio
async def test_edit_resend_compensation_restores_sessions_and_application_state(resources) -> None:
    client, host, workspace, _ = resources
    await client.post(
        "/api/v1/agents/main/messages",
        headers={"Idempotency-Key": "01200000-0000-4000-8000-000000000001"},
        json={"content": "original", "messageId": "m1"},
    )
    service = host.app.state.conversation_service
    await _eventually(lambda: len(service.messages("main")) == 1)
    sessions_path = workspace / ".manyselves/conversations/sessions.json"
    sessions_before = sessions_path.read_bytes()
    service._message_sessions[("main", "keep")] = "captured"  # noqa: SLF001
    message_sessions_before = dict(service._message_sessions)  # noqa: SLF001

    def corrupt_application_state() -> None:
        sessions_path.write_bytes(b"[]")
        service._message_sessions.clear()  # noqa: SLF001

    host.backend.send_side_effect = corrupt_application_state
    host.backend.send_error = RuntimeError("publish failed")
    response = await client.post(
        "/api/v1/agents/main/messages/m1/edit-resend",
        headers={"Idempotency-Key": "01200000-0000-4000-8000-000000000099"},
        json={"content": "replacement"},
    )

    assert response.status_code == 500
    assert sessions_path.read_bytes() == sessions_before
    assert service._message_sessions == message_sessions_before  # noqa: SLF001


@pytest.mark.asyncio
async def test_prepare_compensation_failure_marks_runtime_consistency_failed(
    resources, monkeypatch
) -> None:
    client, host, _, _ = resources
    await client.post(
        "/api/v1/agents/main/messages",
        headers={"Idempotency-Key": "01300000-0000-4000-8000-000000000001"},
        json={"content": "original", "messageId": "m1"},
    )
    service = host.app.state.conversation_service
    await _eventually(lambda: len(service.messages("main")) == 1)

    async def fail_sync(*_args, **_kwargs):
        raise RuntimeError("prepare sync failed")

    async def fail_restore(_snapshot):
        raise RuntimeError("prepare compensation failed")

    monkeypatch.setattr(service, "_sync", fail_sync)
    monkeypatch.setattr(service, "restore", fail_restore)
    cleanup_error = RuntimeError("producer stop failed")

    async def fail_consistency() -> None:
        host.is_ready = False
        raise cleanup_error

    host.fail_consistency = fail_consistency
    headers = {"Idempotency-Key": "01300000-0000-4000-8000-000000000099"}
    response = await client.post(
        "/api/v1/agents/main/messages/m1/edit-resend",
        headers=headers,
        json={"content": "replacement"},
    )
    replay = await client.post(
        "/api/v1/agents/main/messages/m1/edit-resend",
        headers=headers,
        json={"content": "replacement"},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "RUNTIME_CONSISTENCY_FAILED"
    assert replay.status_code == 500
    assert replay.json()["error"]["code"] == "RUNTIME_CONSISTENCY_FAILED"
    assert host.is_ready is False


@pytest.mark.asyncio
async def test_prepare_compensation_failure_retains_producer_cleanup_diagnostic(
    resources, monkeypatch
) -> None:
    """The exposed consistency failure chain must retain producer-stop diagnostics."""
    client, host, _, _ = resources
    await client.post(
        "/api/v1/agents/main/messages",
        headers={"Idempotency-Key": "01310000-0000-4000-8000-000000000001"},
        json={"content": "original", "messageId": "m1"},
    )
    service = host.app.state.conversation_service
    await _eventually(lambda: len(service.messages("main")) == 1)

    async def fail_sync(*_args, **_kwargs) -> None:
        raise RuntimeError("prepare sync failed")

    restore_error = RuntimeError("prepare compensation failed")

    async def fail_restore(_snapshot) -> None:
        raise restore_error

    async def fail_consistency() -> None:
        host.is_ready = False
        raise RuntimeError("producer stop failed")

    monkeypatch.setattr(service, "_sync", fail_sync)
    monkeypatch.setattr(service, "restore", fail_restore)
    host.fail_consistency = fail_consistency

    with pytest.raises(RuntimeConsistencyFailedError) as raised:
        await service.prepare_edit_resend("main", "m1")

    assert raised.value.__cause__ is restore_error
    assert any(
        "producer stop failed" in note
        for note in getattr(restore_error, "__notes__", ())
    )


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
async def test_rollback_requires_public_preflight_before_side_effects(resources) -> None:
    client, host, _, _ = resources
    host.backend.prepare_rollback = None

    response = await client.post(
        "/api/v1/agents/main/rollback",
        headers={"Idempotency-Key": "10100000-0000-4000-8000-000000000099"},
        json={"checkpointId": "cp-1"},
    )

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "ROLLBACK_PREFLIGHT_UNSUPPORTED"
    assert host.backend.rollbacks == []


@pytest.mark.asyncio
async def test_rollback_maps_backend_unsupported_preflight_to_stable_501(resources) -> None:
    client, host, _, _ = resources

    async def unsupported_preflight(_agent_id: str, _checkpoint_id: str) -> dict:
        raise NotImplementedError("Rollback preflight is unavailable")

    host.backend.prepare_rollback = unsupported_preflight
    response = await client.post(
        "/api/v1/agents/main/rollback",
        headers={"Idempotency-Key": "10100000-0000-4000-8000-000000000098"},
        json={"checkpointId": "cp-1"},
    )

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "ROLLBACK_PREFLIGHT_UNSUPPORTED"
    assert host.backend.rollbacks == []


@pytest.mark.asyncio
async def test_committed_rollback_not_implemented_is_not_misclassified_as_preflight(
    resources,
) -> None:
    """Only the preflight boundary owns the stable unsupported classification."""
    client, host, _, _ = resources

    async def unsupported_commit(_agent_id: str, _checkpoint_id: str) -> dict:
        raise NotImplementedError("Committed rollback is unavailable")

    host.backend.rollback_to_checkpoint = unsupported_commit
    response = await client.post(
        "/api/v1/agents/main/rollback",
        headers={"Idempotency-Key": "10100000-0000-4000-8000-000000000097"},
        json={"checkpointId": "cp-1"},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert host.backend.rollback_preparations == [("main", "cp-1")]


@pytest.mark.asyncio
async def test_rollback_rejects_pending_agent_stream_before_preflight(resources) -> None:
    client, host, _, _ = resources
    service = host.app.state.conversation_service
    session = service.store.get_current_session_id("main")
    service._streams[(session, "main", "m1")] = "partial"  # noqa: SLF001

    response = await client.post(
        "/api/v1/agents/main/rollback",
        headers={"Idempotency-Key": "10110000-0000-4000-8000-000000000099"},
        json={"checkpointId": "cp-1"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUNTIME_BUSY"
    assert host.backend.rollback_preparations == []
    assert host.backend.rollbacks == []


@pytest.mark.asyncio
async def test_rollback_clears_tuple_keyed_stream_and_message_state(resources) -> None:
    _, host, _, _ = resources
    service = host.app.state.conversation_service
    session = service.store.get_current_session_id("main")
    service._streams[(session, "main", "m1")] = "partial"  # noqa: SLF001
    service._message_sessions[("main", "m1")] = session  # noqa: SLF001

    await service.apply_rollback("main", None, [])

    assert not any(key[1] == "main" for key in service._streams)  # noqa: SLF001
    assert not any(key[0] == "main" for key in service._message_sessions)  # noqa: SLF001


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
    assert body["run"]["status"] == "completed"
    assert body["run"]["active"] is False
    assert body["run"]["source"] == "persisted"
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
    assert response.json()["run"]["status"] == "completed"
    assert response.json()["run"]["active"] is False
    assert response.json()["run"]["source"] == "persisted"
    assert response.json()["run"]["task_id"] == "persisted-task"


@pytest.mark.asyncio
async def test_reporting_malformed_persisted_state_is_not_silently_empty(resources) -> None:
    client, _, workspace, _ = resources
    (workspace / "Work/runs").mkdir(parents=True, exist_ok=True)
    (workspace / "Work/runs/broken.json").write_text("{broken", encoding="utf-8")

    response = await client.get("/api/v1/reporting/runs/broken")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "REPORT_STATE_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_text",
    ["{broken", "[]"],
    ids=["malformed", "non-object"],
)
async def test_reporting_request_artifact_must_be_a_valid_json_object(
    resources, request_text: str
) -> None:
    """A request-backed run must validate the artifact that recognizes it."""
    client, _, workspace, _ = resources
    root = workspace / "Work/runs/request-only"
    root.mkdir(parents=True)
    (root / "request.json").write_text(request_text, encoding="utf-8")

    response = await client.get("/api/v1/reporting/runs/request-only")

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
async def test_reporting_empty_run_directory_is_not_a_run(resources) -> None:
    client, _, workspace, _ = resources
    (workspace / "Work/runs/empty").mkdir(parents=True, exist_ok=True)

    response = await client.get("/api/v1/reporting/runs/empty")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REPORT_RUN_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="symlink containment contract")
async def test_reporting_rejects_symlinked_runs_root(resources, tmp_path: Path) -> None:
    client, _, workspace, _ = resources
    runs = workspace / "Work/runs"
    if runs.exists():
        runs.rmdir()
    outside = tmp_path / "outside-runs"
    outside.mkdir()
    (outside / "escaped.json").write_text(
        json.dumps({"run_id": "escaped", "status": "completed"}), encoding="utf-8"
    )
    runs.symlink_to(outside, target_is_directory=True)

    response = await client.get("/api/v1/reporting/runs/escaped")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REPORT_RUN_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="symlink containment contract")
async def test_reporting_rejects_symlinked_decisions_directory(resources, tmp_path: Path) -> None:
    client, _, workspace, _ = resources
    root = workspace / "Work/runs/run-decisions"
    root.mkdir(parents=True)
    (root / "workflow-state.json").write_text(
        json.dumps({"run_id": "run-decisions", "status": "blocked"}), encoding="utf-8"
    )
    outside = tmp_path / "outside-decisions"
    outside.mkdir()
    (outside / "secret.json").write_text(
        json.dumps({"decision_id": "secret", "status": "pending"}), encoding="utf-8"
    )
    (root / "decisions").symlink_to(outside, target_is_directory=True)

    response = await client.get("/api/v1/reporting/runs/run-decisions")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "REPORT_STATE_INVALID"


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
async def test_fresh_reporting_run_is_immediately_queryable_listed_and_cancellable(
    resources,
) -> None:
    client, _, _, _ = resources
    accepted = await client.post(
        "/api/v1/reporting/runs",
        headers={"Idempotency-Key": "20000000-0000-4000-8000-000000000011"},
        json={"instruction": "Generate report", "operation": "full_report"},
    )
    run_id = accepted.json()["runId"]

    snapshot = await client.get(f"/api/v1/reporting/runs/{run_id}")
    listed = await client.get("/api/v1/reporting/runs")
    cancelled = await client.post(
        f"/api/v1/reporting/runs/{run_id}/cancel",
        headers={"Idempotency-Key": "20000000-0000-4000-8000-000000000012"},
    )

    assert accepted.status_code == 202
    assert snapshot.status_code == 200
    assert snapshot.json()["run"]["status"] == "running"
    assert any(item["run_id"] == run_id for item in listed.json()["runs"])
    assert cancelled.status_code == 202


@pytest.mark.asyncio
async def test_live_reporting_run_without_artifacts_is_queryable_and_listed(
    resources,
) -> None:
    client, host, _, _ = resources
    run_id = "live-only"
    host.reporting_controller.live_runs.add(run_id)
    host.reporting_controller._tasks[run_id] = asyncio.create_task(  # noqa: SLF001
        asyncio.Event().wait()
    )

    snapshot = await client.get(f"/api/v1/reporting/runs/{run_id}")
    listed = await client.get("/api/v1/reporting/runs")
    cancelled = await client.post(
        f"/api/v1/reporting/runs/{run_id}/cancel",
        headers={"Idempotency-Key": "20000000-0000-4000-8000-000000000013"},
    )

    assert snapshot.status_code == 200
    assert snapshot.json()["run"]["source"] == "live"
    assert any(item["run_id"] == run_id for item in listed.json()["runs"])
    assert cancelled.status_code == 202


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
@pytest.mark.skipif(os.name != "posix", reason="Linux deployment contract")
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
@pytest.mark.skipif(os.name != "posix", reason="Linux deployment contract")
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
@pytest.mark.skipif(os.name != "posix", reason="Linux deployment contract")
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
@pytest.mark.skipif(os.name != "posix", reason="Linux deployment contract")
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
async def test_python_run_is_unsupported_on_windows_before_process_creation(
    resources, monkeypatch
) -> None:
    client, host, workspace, _ = resources
    service = host.app.state.python_run_service
    service._platform = "nt"  # noqa: SLF001
    script = workspace / "Work/windows.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('must not run')\n", encoding="utf-8")
    created = False

    async def reject_creation(*_args, **_kwargs):
        nonlocal created
        created = True
        raise AssertionError("Windows subprocess creation must not be attempted")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", reject_creation)
    response = await client.post(
        "/api/v1/operations/python",
        headers={"Idempotency-Key": "31000000-0000-4000-8000-000000000001"},
        json={"path": "Work/windows.py", "arguments": []},
    )

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "PYTHON_RUN_UNSUPPORTED"
    assert response.json()["error"]["details"] == {
        "supportedPlatform": "posix",
        "deploymentTarget": "linux-compose",
    }
    assert created is False


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
    def host_process_exited() -> bool:
        return operation.process is not None and operation.process.returncode is not None

    await _eventually(host_process_exited)

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
async def test_settings_mutations_apply_to_live_runtime_and_model_only_does_not_restart(
    resources,
) -> None:
    client, host, _, secret = resources
    previous = host.loop_manager
    previous_registry = previous.provider_registry
    bus_task = host._bus_task  # noqa: SLF001

    provider = await client.patch(
        "/api/v1/settings/providers/provider-1",
        json={"apiKey": "replacement-secret", "apiBase": "https://new.invalid/v1"},
    )
    replacement = host.loop_manager
    model = await client.patch("/api/v1/settings", json={"model": "next-model"})

    assert provider.status_code == 200
    assert model.status_code == 200
    assert host.replace_calls == [False]
    assert host.backend.restart_calls == []
    assert replacement is not previous
    assert replacement.provider_registry is not previous_registry
    assert host.loop_manager is replacement
    assert host.manager_factory_calls == 2
    assert host._bus_task is bus_task  # noqa: SLF001
    assert bus_task is not None and bus_task.done() is False
    assert "replacement-secret" not in provider.text
    assert secret not in provider.text
    assert provider.json()["providers"][0]["configured"] is True
    assert model.json()["defaults"]["model"] == "next-model"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "payload", "provider_absent", "active_cleared"),
    [
        ("patch", {"apiKey": None}, True, False),
        ("patch", {"enabled": False}, True, False),
        ("patch", {"apiBase": "https://changed.invalid/v1"}, False, False),
        ("delete", None, True, True),
    ],
    ids=["clear-credentials", "disable-provider", "connection-fields", "remove-active"],
)
async def test_provider_affecting_mutations_rebuild_complete_manager(
    resources,
    method: str,
    payload: dict | None,
    provider_absent: bool,
    active_cleared: bool,
) -> None:
    """Every provider mutation must replace stale provider registry state."""
    client, host, _, _ = resources
    previous = host.loop_manager
    previous_registry = previous.provider_registry
    if method == "delete":
        response = await client.delete("/api/v1/settings/providers/provider-1")
    else:
        response = await client.patch(
            "/api/v1/settings/providers/provider-1",
            json=payload,
        )

    assert response.status_code == 200
    assert host.loop_manager is not previous
    assert host.loop_manager.provider_registry is not previous_registry
    assert host.manager_factory_calls == 2
    assert host.replace_calls == [False]
    assert host.backend.restart_calls == []
    if provider_absent:
        assert "provider-1" not in host.loop_manager.provider_registry
    if active_cleared:
        assert host.config_manager.config.providers.active is None


@pytest.mark.asyncio
async def test_settings_apply_failure_rolls_back_memory_and_persisted_config(resources) -> None:
    client, host, _, secret = resources
    saved_keys: list[str | None] = []

    def save_config() -> None:
        saved_keys.append(host.config_manager.config.providers.configurations[0].api_key)

    host.config_manager.save_config = save_config
    host.replace_error = RuntimeError("injected replacement failure")

    response = await client.patch(
        "/api/v1/settings/providers/provider-1",
        json={"apiKey": "must-roll-back"},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert host.config_manager.config.providers.configurations[0].api_key == secret
    assert saved_keys == ["must-roll-back", secret]
    assert "must-roll-back" not in response.text


@pytest.mark.asyncio
async def test_settings_apply_failure_restores_exact_persisted_config_bytes(
    resources,
) -> None:
    client, host, workspace, _ = resources
    manager = ConfigManager(config_path=workspace / "runtime-config.yaml")
    manager._config = host.config_manager.config.model_copy(deep=True)  # noqa: SLF001
    manager.save_config()
    config_path = manager._settings.config_path  # noqa: SLF001
    original = b"# retain operator comment\n" + config_path.read_bytes()
    config_path.write_bytes(original)
    host.config_manager = manager
    host.replace_error = RuntimeError("injected replacement failure")

    response = await client.patch(
        "/api/v1/settings/providers/provider-1",
        json={"apiBase": "https://must-roll-back.invalid/v1"},
    )

    assert response.status_code == 500
    assert config_path.read_bytes() == original
    assert manager.config.providers.configurations[0].api_base == (
        "https://provider.invalid/v1"
    )


@pytest.mark.asyncio
async def test_settings_replacement_failure_recovers_live_runtime_with_old_config(
    resources,
) -> None:
    client, host, _, secret = resources
    host.replace_failures_remaining = 1

    response = await client.patch(
        "/api/v1/settings/providers/provider-1",
        json={"apiKey": "transient-value"},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert host.replace_calls == [False, True]
    assert host.backend.restart_calls == []
    assert host.is_ready is True
    assert host.loop_manager.provider_registry["provider-1"]["api_key"] == secret
    assert host.config_manager.config.providers.configurations[0].api_key == secret


@pytest.mark.asyncio
async def test_settings_recovery_failure_returns_consistency_failure_without_secret(
    resources,
) -> None:
    client, host, _, secret = resources
    recovery_secret = "recovery-api-key-never-return"
    host.replace_error = RuntimeError("injected replacement failure")
    host.recovery_error = RuntimeError(f"recovery failed with {recovery_secret}")

    response = await client.patch(
        "/api/v1/settings/providers/provider-1",
        json={"apiKey": "transient-api-key-never-return"},
    )

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "RUNTIME_CONSISTENCY_FAILED",
        "message": "Runtime consistency could not be guaranteed",
        "retryable": False,
        "details": {},
    }
    assert host.replace_calls == [False, True]
    assert host.is_ready is False
    assert host.loop_manager is None
    assert secret not in response.text
    assert recovery_secret not in response.text
    assert "transient-api-key-never-return" not in response.text


@pytest.mark.asyncio
async def test_settings_durable_rollback_failure_returns_consistency_failure_without_recovery(
    resources,
) -> None:
    client, host, _, secret = resources
    rollback_secret = "rollback-api-key-never-return"
    saved_keys: list[str | None] = []

    def save_config() -> None:
        key = host.config_manager.config.providers.configurations[0].api_key
        saved_keys.append(key)
        if len(saved_keys) == 2:
            raise OSError(f"rollback failed with {rollback_secret}")

    host.config_manager.save_config = save_config
    host.replace_error = RuntimeError("injected replacement failure")

    response = await client.patch(
        "/api/v1/settings/providers/provider-1",
        json={"apiKey": "transient-api-key-never-return"},
    )

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "RUNTIME_CONSISTENCY_FAILED",
        "message": "Runtime consistency could not be guaranteed",
        "retryable": False,
        "details": {},
    }
    assert saved_keys == ["transient-api-key-never-return", secret]
    assert host.replace_calls == [False]
    assert host.is_ready is False
    assert host.loop_manager is None
    assert secret not in response.text
    assert rollback_secret not in response.text
    assert "transient-api-key-never-return" not in response.text


@pytest.mark.asyncio
async def test_provider_lifecycle_presets_validation_and_agent_debug_are_exposed(
    resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, host, _, _ = resources
    monkeypatch.setattr("manyselves.webapi.routes.settings.sync_presets", lambda: 3)

    created = await client.post(
        "/api/v1/settings/providers",
        json={
            "name": "Backup",
            "provider": "openai",
            "apiKey": "backup-secret",
            "enabled": True,
        },
    )
    presets = await client.get("/api/v1/settings/presets")
    rejected_sync = await client.post(
        "/api/v1/settings/presets/sync",
        headers={"X-Control-Lease-Token": "not-the-controller"},
    )
    synced = await client.post("/api/v1/settings/presets/sync")
    validation = await client.post("/api/v1/settings/validate")
    debug_enabled = await client.patch("/api/v1/agents/main/debug", json={"enabled": True})
    debug = await client.get("/api/v1/agents/main/debug")
    removed = await client.delete(
        f"/api/v1/settings/providers/{created.json()['providers'][-1]['id']}"
    )

    assert created.status_code == 201
    assert "backup-secret" not in created.text
    assert presets.status_code == 200 and presets.json()["presets"]
    assert rejected_sync.status_code == 423
    assert synced.json()["downloaded"] == 3
    assert validation.json()["valid"] is True
    assert debug_enabled.status_code == 200
    assert debug.json() == {"agentId": "main", "enabled": True, "entries": []}
    assert removed.status_code == 200
    assert host.replace_calls == [False, False]
    assert host.backend.restart_calls == []


@pytest.mark.asyncio
async def test_preset_sync_holds_mutation_ownership_until_worker_completes(
    resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving the sync write outside its transaction lets a later mutation enter."""
    client, _, _, _ = resources
    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocking_sync() -> int:
        worker_started.set()
        release_worker.wait()
        return 7

    monkeypatch.setattr("manyselves.webapi.routes.settings.sync_presets", blocking_sync)
    sync_task = asyncio.create_task(client.post("/api/v1/settings/presets/sync"))
    mutation_task: asyncio.Task[httpx.Response] | None = None
    try:
        await _eventually(worker_started.is_set)
        mutation_task = asyncio.create_task(
            client.patch("/api/v1/settings", json={"model": "serialized-model"})
        )

        await _assert_task_remains_pending(mutation_task)
    finally:
        release_worker.set()
        await asyncio.gather(
            sync_task,
            *(() if mutation_task is None else (mutation_task,)),
            return_exceptions=True,
        )

    assert (await sync_task).json() == {"downloaded": 7}
    assert (await mutation_task).status_code == 200


@pytest.mark.asyncio
async def test_preset_sync_rejects_invalid_lease_before_starting_worker(
    resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lease validation must happen before the sync write can begin."""
    client, _, _, _ = resources
    worker_started = threading.Event()

    def sync_that_must_not_run() -> int:
        worker_started.set()
        return 5

    monkeypatch.setattr(
        "manyselves.webapi.routes.settings.sync_presets", sync_that_must_not_run
    )

    response = await client.post(
        "/api/v1/settings/presets/sync",
        headers={"X-Control-Lease-Token": "not-the-controller"},
    )

    assert response.status_code == 423
    assert response.json()["error"]["code"] == "CONTROL_LEASE_REQUIRED"
    assert not worker_started.is_set()


@pytest.mark.asyncio
async def test_preset_sync_sanitizes_sync_error(
    resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker failures must retain the route's sanitized public envelope."""
    client, _, _, _ = resources
    secret = "Bearer preset-sync-secret"

    def failing_sync() -> int:
        raise SyncError(secret)

    monkeypatch.setattr("manyselves.webapi.routes.settings.sync_presets", failing_sync)

    response = await client.post("/api/v1/settings/presets/sync")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "PRESET_SYNC_FAILED"
    assert error["message"] == "Provider presets could not be synchronized"
    assert error["retryable"] is True
    assert error["details"] == {}
    assert secret not in response.text


@pytest.mark.asyncio
async def test_preset_sync_cancellation_keeps_mutation_ownership_until_worker_completes(
    resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling the caller must not release the sync write's mutation lock."""
    client, _, _, _ = resources
    worker_started = threading.Event()
    worker_completed = threading.Event()
    release_worker = threading.Event()

    def blocking_sync() -> int:
        worker_started.set()
        release_worker.wait()
        worker_completed.set()
        return 11

    monkeypatch.setattr("manyselves.webapi.routes.settings.sync_presets", blocking_sync)
    sync_task = asyncio.create_task(client.post("/api/v1/settings/presets/sync"))
    mutation_task: asyncio.Task[httpx.Response] | None = None
    try:
        await _eventually(worker_started.is_set)
        sync_task.cancel()
        await _assert_task_remains_pending(sync_task)

        mutation_task = asyncio.create_task(
            client.patch("/api/v1/settings", json={"model": "after-cancel"})
        )
        await _assert_task_remains_pending(mutation_task)
    finally:
        release_worker.set()
        await asyncio.gather(
            sync_task,
            *(() if mutation_task is None else (mutation_task,)),
            return_exceptions=True,
        )

    assert worker_completed.is_set()
    with pytest.raises(asyncio.CancelledError):
        await sync_task
    assert (await mutation_task).status_code == 200


@pytest.mark.asyncio
async def test_agent_debug_groups_dynamic_agent_events_and_sanitizes_error(resources) -> None:
    """A debug event must stay with its emitting Agent and never return provider secrets."""
    client, host, _, _ = resources
    host.statuses["researcher"] = "idle"
    host.loop_manager = SimpleNamespace(
        get_all_agent_statuses=lambda: dict(host.statuses),
        get_agent_session_id=lambda _agent_id: None,
        get_loop=lambda agent_id: SimpleNamespace() if agent_id in host.statuses else None,
        get_agent_debug_mode=lambda agent_id: host.backend.debug_modes.get(agent_id, False),
    )
    await host.app.state.event_broker.publish_internal(
        ApiDebugMessage(
            agent_type="researcher",
            model="test-model",
            tokens_in=10,
            tokens_out=3,
            duration_ms=25,
            status="error",
            error="Bearer dynamic-debug-secret",
        )
    )

    researcher = await client.get("/api/v1/agents/researcher/debug")
    main = await client.get("/api/v1/agents/main/debug")

    assert researcher.status_code == 200
    assert researcher.json()["agentId"] == "researcher"
    assert researcher.json()["entries"] == [
        {
            "model": "test-model",
            "tokensIn": 10,
            "tokensOut": 3,
            "durationMs": 25,
            "status": "error",
            "timestamp": researcher.json()["entries"][0]["timestamp"],
            "error": "[REDACTED]",
        }
    ]
    assert "dynamic-debug-secret" not in researcher.text
    assert main.status_code == 200
    assert main.json()["entries"] == []


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
        service._flush()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="dangling symlink contract")
async def test_maintenance_refuses_dangling_symlinked_config_path(
    resources, tmp_path: Path
) -> None:
    """A dangling final symlink must not bypass the no-follow config boundary."""
    _, host, workspace, _ = resources
    link = workspace / "dangling.yaml"
    link.symlink_to(tmp_path / "missing-outside.yaml")
    host.config_manager._settings = SimpleNamespace(config_path=link)
    service = host.app.state.maintenance_service
    service.config_manager = host.config_manager

    with pytest.raises(ValueError, match="symlink"):
        service._flush()


@pytest.mark.asyncio
async def test_maintenance_flushes_authoritative_workspace_without_following_symlinks(
    resources, tmp_path: Path, monkeypatch
) -> None:
    _, host, workspace, _ = resources
    durable = {
        workspace / ".checkpoints/main/checkpoint.json",
        workspace / ".manyselves/tasks/task.json",
        workspace / "Work/runs/run-1/workflow-state.json",
        workspace / "Outputs/Reports/manifest.json",
        workspace / "manyselves.yaml",
    }
    for path in durable:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    host.config_manager._settings = SimpleNamespace(
        config_path=workspace / "manyselves.yaml"
    )
    service = host.app.state.maintenance_service
    service.config_manager = host.config_manager
    outside = tmp_path / "outside-durable"
    outside.mkdir()
    outside_file = outside / "secret.json"
    outside_file.write_text("{}", encoding="utf-8")
    (workspace / "escaped-durable").symlink_to(outside, target_is_directory=True)
    flushed: set[Path] = set()

    def record_fsync(descriptor: int) -> None:
        try:
            flushed.add(Path(os.readlink(f"/proc/self/fd/{descriptor}")).resolve())
        except OSError:
            pass

    monkeypatch.setattr(os, "fsync", record_fsync)
    service._flush()

    assert durable <= flushed
    assert outside_file.resolve() not in flushed


@pytest.mark.asyncio
async def test_quiescence_blocks_late_conversation_persistence(resources) -> None:
    client, host, workspace, _ = resources
    quiesced = await client.post("/api/v1/maintenance/quiesce")

    await host.bus.publish(UserMessage(agent_type="main", content="late"))
    await asyncio.sleep(0.1)

    assert quiesced.status_code == 200
    assert not (workspace / ".manyselves/conversations/sessions.json").exists()
