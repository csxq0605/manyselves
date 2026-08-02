"""Lifecycle contract shared by desktop and future runtime clients."""

import asyncio
import threading
import traceback
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from manyselves.application.backend_api import BackendAPIImpl
from manyselves.application.conversation_service import ConversationService
from manyselves.application.errors import RuntimeConsistencyFailedError, RuntimeStartupError
from manyselves.application.models import RollbackCommand
from manyselves.application.runtime_facade import RuntimeFacade
from manyselves.application.runtime_host import RuntimeHost
from manyselves.application.settings_service import SettingsService
from manyselves.config import ConfigManager
from manyselves.config.schema import ApiConfig, AppConfig, ProvidersConfig
from manyselves.core.loops import LoopManager, MessageBus
from manyselves.interfaces.types import Checkpoint, UserMessage


class _FakeConfigManager:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid

    def validate_api_keys(self) -> tuple[bool, list[str]]:
        return self.valid, ["anthropic"] if self.valid else []


class _FakeBus:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.release = asyncio.Event()
        self.processor_finished = asyncio.Event()
        self.shutdown_calls = 0

    async def process_queue(self) -> None:
        self.events.append("bus:start")
        try:
            await self.release.wait()
        finally:
            self.events.append("bus:finish")
            self.processor_finished.set()

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.events.append("bus:shutdown")
        self.release.set()


class _CancellationOnlyBus(_FakeBus):
    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.events.append("bus:shutdown")


class _FakeLoopManager:
    def __init__(
        self,
        events: list[str],
        *,
        start_error: BaseException | None = None,
        start_entered: asyncio.Event | None = None,
        start_release: asyncio.Event | None = None,
        stop_failures: int = 0,
        stop_errors: list[BaseException | None] | None = None,
    ) -> None:
        self.events = events
        self.start_error = start_error
        self.start_entered = start_entered
        self.start_release = start_release
        self.stop_failures = stop_failures
        self.stop_errors = [] if stop_errors is None else list(stop_errors)
        self.stop_calls = 0
        self.running = False

    async def start(self) -> None:
        self.events.append("loops:start")
        if self.start_entered is not None:
            self.start_entered.set()
        if self.start_release is not None:
            await self.start_release.wait()
        if self.start_error is not None:
            self.running = True
            raise self.start_error
        self.running = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.events.append("loops:stop")
        if self.stop_errors:
            error = self.stop_errors.pop(0)
            if error is not None:
                raise error
        if self.stop_failures > 0:
            self.stop_failures -= 1
            raise RuntimeError("loop cleanup failed")
        self.running = False


class _FakeBackend:
    def __init__(self) -> None:
        self.loop_manager: _FakeLoopManager | None = None

    def set_loop_manager(self, loop_manager: _FakeLoopManager | None) -> None:
        self.loop_manager = loop_manager


class _LoopControl:
    """Explicit lifecycle gates for one factory-created provider manager."""

    def __init__(
        self,
        *,
        start_release: asyncio.Event | None = None,
        start_error: BaseException | None = None,
        stop_release: asyncio.Event | None = None,
        stop_error: BaseException | None = None,
    ) -> None:
        self.start_entered = asyncio.Event()
        self.start_release = start_release
        self.start_error = start_error
        self.stop_entered = asyncio.Event()
        self.stop_release = stop_release
        self.stop_error = stop_error


class _ProviderLoopBoundary:
    """Provider snapshot plus controlled real async lifecycle behavior."""

    def __init__(self, config: AppConfig, control: _LoopControl) -> None:
        self.config = config.model_copy(deep=True)
        self.provider_registry = {
            item.id: {
                "provider": item.provider,
                "api_key": item.api_key,
                "api_base": item.api_base,
                "enabled": item.enabled,
            }
            for item in config.providers.configurations
            if item.enabled and item.api_key
        }
        self.control = control
        self.start_calls = 0
        self.stop_calls = 0
        self.running = False

    async def start(self) -> None:
        self.start_calls += 1
        self.control.start_entered.set()
        if self.control.start_release is not None:
            await self.control.start_release.wait()
        self.running = True
        if self.control.start_error is not None:
            raise self.control.start_error

    async def stop(self) -> None:
        self.stop_calls += 1
        self.control.stop_entered.set()
        if self.control.stop_release is not None:
            await self.control.stop_release.wait()
        if self.control.stop_error is not None:
            raise self.control.stop_error
        self.running = False


def _provider_runtime(
    tmp_path: Path,
    controls: list[_LoopControl],
) -> tuple[
    RuntimeHost,
    ConfigManager,
    _FakeBus,
    _FakeBackend,
    list[_ProviderLoopBoundary],
    Path,
    bytes,
]:
    config_path = tmp_path / "runtime-config.yaml"
    config = ConfigManager(config_path=config_path)
    config._config = AppConfig(  # noqa: SLF001
        providers=ProvidersConfig(
            configurations=[
                ApiConfig(
                    id="provider-1",
                    name="Primary",
                    provider="openai",
                    api_key="provider-secret",
                    api_base="https://provider.invalid/v1",
                    enabled=True,
                ),
                ApiConfig(
                    id="provider-2",
                    name="Recovery",
                    provider="anthropic",
                    api_key="recovery-secret",
                    enabled=True,
                ),
            ],
            active="provider-1",
        )
    )
    config.save_config()
    original_bytes = b"# exact operator bytes\n" + config_path.read_bytes()
    config_path.write_bytes(original_bytes)

    events: list[str] = []
    bus = _FakeBus(events)
    backend = _FakeBackend()
    created: list[_ProviderLoopBoundary] = []

    def create_loop_manager(
        workspace: Path,
        config_manager: ConfigManager,
        message_bus: MessageBus,
    ) -> LoopManager:
        assert workspace.is_absolute()
        assert config_manager is config
        assert message_bus is bus
        manager = _ProviderLoopBoundary(config.config, controls[len(created)])
        created.append(manager)
        return cast(LoopManager, manager)

    host = RuntimeHost(
        config_manager=config,
        bus=cast(MessageBus, bus),
        backend=cast(BackendAPIImpl, backend),
        loop_manager_factory=create_loop_manager,
        project_logging_initializer=lambda _workspace: None,
        project_structure_initializer=lambda _workspace: None,
    )
    return host, config, bus, backend, created, config_path, original_bytes


def _host(
    events: list[str],
    *,
    valid_config: bool = True,
    start_error: Exception | None = None,
    start_entered: asyncio.Event | None = None,
    start_release: asyncio.Event | None = None,
    cancellation_only_bus: bool = False,
    stop_failures: int = 0,
    start_errors: list[BaseException | None] | None = None,
    stop_errors: list[list[BaseException | None]] | None = None,
) -> tuple[RuntimeHost, _FakeBus, _FakeBackend, list[_FakeLoopManager]]:
    config = _FakeConfigManager(valid=valid_config)
    bus = _CancellationOnlyBus(events) if cancellation_only_bus else _FakeBus(events)
    backend = _FakeBackend()
    created: list[_FakeLoopManager] = []

    def create_loop_manager(
        workspace: Path,
        config_manager: ConfigManager,
        message_bus: MessageBus,
    ) -> LoopManager:
        assert workspace.is_absolute()
        assert config_manager is config
        assert message_bus is bus
        manager_start_error = start_error
        if start_errors is not None:
            manager_start_error = start_errors[len(created)]
        manager = _FakeLoopManager(
            events,
            start_error=manager_start_error,
            start_entered=start_entered,
            start_release=start_release,
            stop_failures=stop_failures,
            stop_errors=None if stop_errors is None else stop_errors[len(created)],
        )
        created.append(manager)
        return cast(LoopManager, manager)

    def add_logging(workspace: Path) -> None:
        assert workspace.is_absolute()
        events.append("project:logging")

    def ensure_structure(workspace: Path) -> None:
        assert workspace.is_absolute()
        events.append("project:structure")

    host = RuntimeHost(
        config_manager=cast(ConfigManager, config),
        bus=cast(MessageBus, bus),
        backend=cast(BackendAPIImpl, backend),
        loop_manager_factory=create_loop_manager,
        project_logging_initializer=add_logging,
        project_structure_initializer=ensure_structure,
    )
    return host, bus, backend, created


def test_create_builds_normal_production_dependencies() -> None:
    """No-argument construction must retain the normal production graph."""
    host = RuntimeHost.create()

    assert isinstance(host.config_manager, ConfigManager)
    assert isinstance(host.bus, MessageBus)
    assert isinstance(host.backend, BackendAPIImpl)
    assert host.backend.config_manager is host.config_manager
    assert host.backend.bus is host.bus


def test_create_uses_the_injected_config_manager_across_the_backend_graph() -> None:
    """Ignoring an injected manager must split client and backend configuration."""
    config_manager = _FakeConfigManager()

    host = RuntimeHost.create(config_manager=cast(ConfigManager, config_manager))

    assert host.config_manager is config_manager
    assert host.backend.config_manager is config_manager
    assert host.backend.bus is host.bus


@pytest.mark.asyncio
async def test_start_exposes_resolved_workspace_after_bus_starts_before_loops(
    tmp_path: Path,
) -> None:
    """Moving loop startup before bus processing must break this ordering contract."""
    events: list[str] = []
    host, _, _, _ = _host(events)
    workspace = tmp_path / "nested" / ".." / "workspace"

    await host.start(workspace)

    assert host.workspace == workspace.resolve()
    assert host.is_ready is True
    assert events == [
        "project:logging",
        "project:structure",
        "bus:start",
        "loops:start",
    ]
    await host.stop()


@pytest.mark.asyncio
async def test_start_wires_the_created_loop_manager_into_backend(tmp_path: Path) -> None:
    """Omitting backend wiring must leave rollback and interruption unavailable."""
    host, _, backend, created = _host([])

    await host.start(tmp_path)

    assert len(created) == 1
    assert host.loop_manager is created[0]
    assert backend.loop_manager is created[0]
    await host.stop()


@pytest.mark.asyncio
async def test_missing_provider_keys_raise_typed_startup_error(tmp_path: Path) -> None:
    """Provider validation must fail before workspace setup or task creation."""
    events: list[str] = []
    host, _, _, created = _host(events, valid_config=False)

    with pytest.raises(RuntimeStartupError) as raised:
        await host.start(tmp_path)

    assert raised.value.code == "NO_PROVIDER_KEYS"
    assert str(raised.value) == "No API keys configured."
    assert events == []
    assert created == []
    assert host.is_ready is False


@pytest.mark.asyncio
async def test_stop_cancels_no_unrelated_tasks(tmp_path: Path) -> None:
    """Restoring process-wide task cancellation must cancel this unrelated worker."""
    host, bus, _, _ = _host([])
    unrelated_release = asyncio.Event()
    unrelated_cancelled = False

    async def unrelated_worker() -> None:
        nonlocal unrelated_cancelled
        try:
            await unrelated_release.wait()
        except asyncio.CancelledError:
            unrelated_cancelled = True
            raise

    unrelated_task = asyncio.create_task(unrelated_worker())
    await asyncio.sleep(0)
    await host.start(tmp_path)

    await host.stop()

    assert unrelated_cancelled is False
    assert unrelated_task.done() is False
    assert bus.processor_finished.is_set()
    unrelated_release.set()
    await unrelated_task


@pytest.mark.asyncio
async def test_stop_is_idempotent(tmp_path: Path) -> None:
    """Repeated client cleanup must not stop runtime dependencies twice."""
    host, bus, _, created = _host([])
    await host.start(tmp_path)

    await host.stop()
    await host.stop()

    assert host.is_ready is False
    assert bus.shutdown_calls == 1
    assert created[0].stop_calls == 1


@pytest.mark.asyncio
async def test_stop_before_start_consumes_the_one_shot_host(tmp_path: Path) -> None:
    """Leaving a stopped host NEW would let a later start create runtime work."""
    events: list[str] = []
    host, _, _, created = _host(events)

    await host.stop()
    try:
        with pytest.raises(RuntimeStartupError) as raised:
            await host.start(tmp_path)
    finally:
        await host.stop()

    assert raised.value.code == "RUNTIME_STOPPED"
    assert host.is_ready is False
    assert created == []
    assert events == []


@pytest.mark.asyncio
async def test_failed_loop_cleanup_can_be_retried_without_hiding_error(
    tmp_path: Path,
) -> None:
    """Premature STOPPED state would prevent retry after a transient cleanup error."""
    host, bus, _, created = _host([], stop_failures=1)
    await host.start(tmp_path)

    with pytest.raises(RuntimeError, match="loop cleanup failed"):
        await host.stop()

    assert host.is_ready is False
    assert bus.shutdown_calls == 1
    assert bus.processor_finished.is_set()
    assert created[0].stop_calls == 1
    with pytest.raises(RuntimeStartupError) as raised:
        await host.start(tmp_path)
    assert raised.value.code == "RUNTIME_STOPPED"

    await host.stop()
    await host.stop()

    assert bus.shutdown_calls == 1
    assert created[0].stop_calls == 2


@pytest.mark.asyncio
async def test_producer_stop_event_remains_persistable_until_bus_shutdown(
    tmp_path: Path,
) -> None:
    """Closing host persistence during producer stop must lose its accepted final event."""
    bus = MessageBus()
    config = _FakeConfigManager()
    backend = _FakeBackend()

    class PublishingManager:
        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            await bus.publish(
                UserMessage(
                    agent_type="main",
                    content="accepted before shutdown",
                    message_id="shutdown-final",
                )
            )

    manager = PublishingManager()
    host = RuntimeHost(
        config_manager=cast(ConfigManager, config),
        bus=bus,
        backend=cast(BackendAPIImpl, backend),
        loop_manager_factory=lambda *_args: cast(LoopManager, manager),
        project_logging_initializer=lambda _workspace: None,
        project_structure_initializer=lambda _workspace: None,
    )
    await host.start(tmp_path)
    facade = RuntimeFacade(host)
    conversations = ConversationService(tmp_path, facade=facade, bus=bus)

    await facade.begin_shutdown()
    await host.stop_producers()
    await conversations.close()
    await host.stop_bus()

    assert [item["content"] for item in conversations.messages("main")] == [
        "accepted before shutdown"
    ]


@pytest.mark.asyncio
async def test_persistence_is_open_only_while_ready_or_orderly_shutdown_drains(
    tmp_path: Path,
) -> None:
    """A stale startup flag must not authorize writes in failed lifecycle states."""
    host, _, _, _ = _host([])
    await host.start(tmp_path)

    try:
        assert host.persistence_ready is True
        await host.begin_orderly_shutdown()
        await host.stop_producers()
        assert host.persistence_ready is True

        await host.mark_failed()
        assert host.persistence_ready is False
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_consistency_failure_closes_persistence_before_stopping_producers(
    tmp_path: Path,
) -> None:
    """Producer cleanup during a consistency failure must not retain write access."""
    observed: list[bool] = []
    host, _, _, created = _host([])
    await host.start(tmp_path)
    original_stop = created[0].stop

    async def observe_stop() -> None:
        observed.append(host.persistence_ready)
        await original_stop()

    created[0].stop = observe_stop
    await host.fail_consistency()

    assert observed == [False]
    assert host.persistence_ready is False
    await host.stop()


@pytest.mark.asyncio
async def test_ordinary_stop_never_opens_orderly_persistence_drain(tmp_path: Path) -> None:
    """Desktop and direct host shutdown must not acquire the service drain grant."""
    observed: list[bool] = []
    host, _, _, created = _host([])
    await host.start(tmp_path)
    original_stop = created[0].stop

    async def observe_stop() -> None:
        observed.append(host.persistence_ready)
        await original_stop()

    created[0].stop = observe_stop
    await host.stop()

    assert observed == [False]
    assert host.persistence_ready is False


@pytest.mark.asyncio
async def test_failed_workspace_reconciliation_closes_persistence(tmp_path: Path) -> None:
    """A replacement plus restoration failure must fail closed before cleanup."""
    host, _, _, _ = _host(
        [],
        start_errors=[None, RuntimeError("replacement failed"), RuntimeError("restore failed")],
    )
    await host.start(tmp_path / "first")

    with pytest.raises(RuntimeError, match="replacement failed"):
        await host.switch_workspace(tmp_path / "second")

    assert host.persistence_ready is False
    await host.stop()


@pytest.mark.asyncio
async def test_late_checkpoint_cannot_overwrite_rollback_compensation(
    tmp_path: Path,
) -> None:
    """A failed durable rollback must close the bus persistence boundary after restore."""
    bus = MessageBus()
    config = _FakeConfigManager()
    backend = _FakeBackend()
    manager = _FakeLoopManager([])
    manager.get_all_agent_statuses = lambda: {"main": "idle"}
    manager.get_agent_session_id = lambda _agent_id: None

    async def prepare_rollback(_agent_id: str, checkpoint_id: str) -> dict:
        return {"checkpoint_id": checkpoint_id, "effect_paths": []}

    async def rollback_to_checkpoint(_agent_id: str, _checkpoint_id: str) -> dict:
        return {"restored_files": 1, "conversation_history": []}

    async def sync_agent_conversation(*_args, **_kwargs) -> None:
        return None

    backend.prepare_rollback = prepare_rollback
    backend.rollback_to_checkpoint = rollback_to_checkpoint
    backend.sync_agent_conversation = sync_agent_conversation
    host = RuntimeHost(
        config_manager=cast(ConfigManager, config),
        bus=bus,
        backend=cast(BackendAPIImpl, backend),
        loop_manager_factory=lambda *_args: cast(LoopManager, manager),
        project_logging_initializer=lambda _workspace: None,
        project_structure_initializer=lambda _workspace: None,
    )
    await host.start(tmp_path)
    facade = RuntimeFacade(host)
    conversations = ConversationService(tmp_path, facade=facade, bus=bus)
    conversations.store.append_message(
        "main", "user", "preserve", {"message_id": "message-1"}
    )
    lease = facade.leases.acquire(client_id="rollback-test", actor_id="test")

    async def fail_after_durable_restore(result, snapshot) -> None:
        await conversations.apply_rollback(
            "main", "message-1", result.conversation_history
        )
        raise RuntimeError("durable rollback write failed")

    try:
        with pytest.raises(RuntimeConsistencyFailedError):
            await facade.rollback(
                RollbackCommand(
                    command_id=uuid4(),
                    lease_token=lease.token,
                    agent_id="main",
                    checkpoint_id="checkpoint-1",
                ),
                before_restore=lambda: conversations.prepare_rollback(
                    "main", "message-1"
                ),
                after_restore=fail_after_durable_restore,
                restore=conversations.restore,
            )

        await bus.publish(
            Checkpoint(
                agent_type="main",
                checkpoint_id="late-checkpoint",
                description="must not persist",
            )
        )
        await asyncio.sleep(0.05)

        assert [item["content"] for item in conversations.messages("main")] == [
            "preserve"
        ]
    finally:
        await conversations.close()
        await host.stop()


@pytest.mark.asyncio
async def test_partial_start_cleans_up_the_owned_bus_task(tmp_path: Path) -> None:
    """A loop startup failure must not leak the already-started bus processor."""
    host, bus, _, created = _host([], start_error=RuntimeError("loop bootstrap failed"))

    with pytest.raises(RuntimeError, match="loop bootstrap failed"):
        await host.start(tmp_path)

    assert host.is_ready is False
    assert bus.shutdown_calls == 1
    assert created[0].stop_calls == 1
    assert bus.processor_finished.is_set()


@pytest.mark.asyncio
async def test_stopped_host_rejects_restart_without_false_ready_state(tmp_path: Path) -> None:
    """A permanently shut down bus must never be presented as a ready restart."""
    host, _, _, created = _host([])
    await host.start(tmp_path)
    await host.stop()

    try:
        with pytest.raises(RuntimeStartupError) as raised:
            await host.start(tmp_path / "second")
    finally:
        await host.stop()

    assert raised.value.code == "RUNTIME_STOPPED"
    assert host.is_ready is False
    assert len(created) == 1


@pytest.mark.asyncio
async def test_partial_start_consumes_one_shot_host_lifecycle(tmp_path: Path) -> None:
    """Retrying a partially started host must not reuse its shut down bus."""
    host, _, _, created = _host([], start_error=RuntimeError("bootstrap failed"))
    with pytest.raises(RuntimeError, match="bootstrap failed"):
        await host.start(tmp_path)

    with pytest.raises(RuntimeStartupError) as raised:
        await host.start(tmp_path)

    assert raised.value.code == "RUNTIME_STOPPED"
    assert host.is_ready is False
    assert len(created) == 1


@pytest.mark.asyncio
async def test_no_provider_failure_can_retry_before_lifecycle_is_consumed(tmp_path: Path) -> None:
    """Desktop configuration may add a key and retry after validation failure."""
    host, _, _, created = _host([], valid_config=False)
    with pytest.raises(RuntimeStartupError) as raised:
        await host.start(tmp_path)
    assert raised.value.code == "NO_PROVIDER_KEYS"

    config = cast(_FakeConfigManager, host.config_manager)
    config.valid = True
    await host.start(tmp_path)

    assert host.is_ready is True
    assert len(created) == 1
    await host.stop()


@pytest.mark.asyncio
async def test_concurrent_starts_create_only_one_runtime(tmp_path: Path) -> None:
    """Removing start serialization must create duplicate loop managers and tasks."""
    entered = asyncio.Event()
    release = asyncio.Event()
    host, _, _, created = _host([], start_entered=entered, start_release=release)
    first = asyncio.create_task(host.start(tmp_path))
    await entered.wait()
    second = asyncio.create_task(host.start(tmp_path))

    try:
        await asyncio.sleep(0)
        assert len(created) == 1
    finally:
        release.set()
        await asyncio.gather(first, second)

    assert host.is_ready is True
    assert len(created) == 1
    await host.stop()


@pytest.mark.asyncio
async def test_stop_waits_for_in_progress_start_then_leaves_host_stopped(tmp_path: Path) -> None:
    """A stop racing startup must not complete early and allow false-ready state."""
    entered = asyncio.Event()
    release = asyncio.Event()
    host, bus, _, created = _host([], start_entered=entered, start_release=release)
    start_task = asyncio.create_task(host.start(tmp_path))
    await entered.wait()
    stop_task = asyncio.create_task(host.stop())

    try:
        await asyncio.sleep(0)
        assert stop_task.done() is False
    finally:
        release.set()
        await asyncio.gather(start_task, stop_task)

    assert host.is_ready is False
    assert bus.shutdown_calls == 1
    assert created[0].stop_calls == 1
    assert bus.processor_finished.is_set()


@pytest.mark.asyncio
async def test_stop_cancels_blocked_owned_bus_task_but_not_unrelated_work(
    tmp_path: Path,
) -> None:
    """Owned cleanup must not rely on bus shutdown waking the processor."""
    host, bus, _, _ = _host([], cancellation_only_bus=True)
    unrelated_release = asyncio.Event()
    unrelated_cancelled = False

    async def unrelated_worker() -> None:
        nonlocal unrelated_cancelled
        try:
            await unrelated_release.wait()
        except asyncio.CancelledError:
            unrelated_cancelled = True
            raise

    unrelated_task = asyncio.create_task(unrelated_worker())
    await host.start(tmp_path)

    await host.stop()

    assert bus.processor_finished.is_set()
    assert unrelated_task.done() is False
    assert unrelated_cancelled is False
    unrelated_release.set()
    await unrelated_task


@pytest.mark.asyncio
async def test_switch_workspace_replaces_only_loops_and_keeps_bus_running(
    tmp_path: Path,
) -> None:
    """Project activation must not consume the process-local host or start a second bus."""
    events: list[str] = []
    host, bus, backend, created = _host(events)
    first = tmp_path / "first"
    second = tmp_path / "second"
    await host.start(first)

    await host.switch_workspace(second)

    assert host.workspace == second.resolve()
    assert host.is_ready is True
    assert len(created) == 2
    assert created[0].stop_calls == 1
    assert created[0].running is False
    assert created[1].running is True
    assert host.loop_manager is created[1]
    assert backend.loop_manager is created[1]
    assert events.count("bus:start") == 1
    assert bus.shutdown_calls == 0
    await host.stop()


@pytest.mark.asyncio
async def test_switch_workspace_failure_restores_coherent_previous_runtime(
    tmp_path: Path,
) -> None:
    """A failed activation must not split host workspace from backend loop ownership."""
    events: list[str] = []
    host, bus, backend, created = _host(
        events,
        start_errors=[None, RuntimeError("new workspace failed"), None],
    )
    first = tmp_path / "first"
    await host.start(first)

    with pytest.raises(RuntimeError, match="new workspace failed"):
        await host.switch_workspace(tmp_path / "second")

    assert host.is_ready is True
    assert host.workspace == first.resolve()
    assert len(created) == 3
    assert [manager.running for manager in created] == [False, False, True]
    assert created[1].stop_calls == 1
    assert host.loop_manager is created[2]
    assert backend.loop_manager is created[2]
    assert events.count("bus:start") == 1
    assert bus.shutdown_calls == 0
    await host.stop()


@pytest.mark.asyncio
async def test_switch_workspace_rollback_failure_enters_clean_non_ready_state(
    tmp_path: Path,
) -> None:
    """Failed replacement and restoration must leave no untracked backend manager."""
    host, bus, backend, created = _host(
        [],
        start_errors=[None, RuntimeError("replacement failed"), RuntimeError("restore failed")],
    )
    await host.start(tmp_path / "first")

    with pytest.raises(RuntimeError, match="replacement failed"):
        await host.switch_workspace(tmp_path / "second")

    assert host.is_ready is False
    assert host.workspace is None
    assert host.loop_manager is None
    assert backend.loop_manager is None
    assert [manager.running for manager in created] == [False, False, False]
    assert [manager.stop_calls for manager in created] == [1, 1, 1]
    assert bus.shutdown_calls == 0

    await host.stop()

    assert bus.shutdown_calls == 1
    assert host.is_ready is False


@pytest.mark.asyncio
async def test_switch_workspace_cancellation_restores_previous_runtime(tmp_path: Path) -> None:
    """Cancellation during replacement startup must clean it and restore the old workspace."""
    host, bus, backend, created = _host(
        [],
        start_errors=[None, asyncio.CancelledError(), None],
    )
    first = tmp_path / "first"
    await host.start(first)

    with pytest.raises(asyncio.CancelledError):
        await host.switch_workspace(tmp_path / "second")

    assert host.is_ready is True
    assert host.workspace == first.resolve()
    assert host.loop_manager is created[2]
    assert backend.loop_manager is created[2]
    assert [manager.running for manager in created] == [False, False, True]
    assert created[1].stop_calls == 1
    assert bus.shutdown_calls == 0
    await host.stop()


@pytest.mark.asyncio
async def test_switch_workspace_rollback_cancellation_cleans_candidate_and_stops(
    tmp_path: Path,
) -> None:
    """Cancellation during restoration must leave a tracked, stoppable failed host."""
    host, bus, backend, created = _host(
        [],
        start_errors=[None, RuntimeError("replacement failed"), asyncio.CancelledError()],
    )
    await host.start(tmp_path / "first")

    with pytest.raises(RuntimeError, match="replacement failed"):
        await host.switch_workspace(tmp_path / "second")

    assert host.is_ready is False
    assert host.workspace is None
    assert host.loop_manager is None
    assert backend.loop_manager is None
    assert created[2].stop_calls == 1

    await host.stop()
    assert bus.shutdown_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cleanup_error",
    [RuntimeError("candidate cleanup failed"), asyncio.CancelledError()],
    ids=["failure", "cancellation"],
)
async def test_switch_workspace_retains_candidate_when_cleanup_does_not_finish(
    tmp_path: Path,
    cleanup_error: BaseException,
) -> None:
    """A possibly-running candidate must remain owned until stop can retry cleanup."""
    host, bus, backend, created = _host(
        [],
        start_errors=[None, RuntimeError("replacement failed")],
        stop_errors=[[], [cleanup_error, None]],
    )
    await host.start(tmp_path / "first")

    with pytest.raises(RuntimeError, match="replacement failed"):
        await host.switch_workspace(tmp_path / "second")

    assert host.is_ready is False
    assert host.workspace == (tmp_path / "second").resolve()
    assert len(created) == 2
    assert host.loop_manager is created[1]
    assert backend.loop_manager is created[1]
    assert created[1].running is True
    assert created[1].stop_calls == 1
    assert bus.shutdown_calls == 0

    await host.stop()

    assert created[1].stop_calls == 2
    assert created[1].running is False
    assert bus.shutdown_calls == 1


@pytest.mark.asyncio
async def test_replace_loop_manager_uses_factory_and_keeps_shared_bus(
    tmp_path: Path,
) -> None:
    """Reusing the manager/provider registry or restarting the bus breaks isolation."""
    host, _, bus, backend, created, _, _ = _provider_runtime(
        tmp_path,
        [_LoopControl(), _LoopControl()],
    )
    await host.start(tmp_path / "workspace")
    previous = created[0]
    previous_registry = previous.provider_registry

    try:
        await host.replace_loop_manager()

        assert host.is_ready is True
        assert host.loop_manager is created[1]
        assert backend.loop_manager is created[1]
        assert created[1] is not previous
        assert created[1].provider_registry is not previous_registry
        assert previous.stop_calls == 1
        assert created[1].running is True
        assert bus.events.count("bus:start") == 1
        assert bus.shutdown_calls == 0
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_replace_loop_manager_cleanup_diagnostic_does_not_expose_api_key(
    tmp_path: Path,
) -> None:
    """A cleanup cause must not smuggle provider credentials into diagnostics."""
    secret = "cleanup-api-key"
    replacement_error = RuntimeError("replacement failed")
    host, _, _, created = _host(
        [],
        start_errors=[None, replacement_error],
        stop_errors=[[], [RuntimeError(f"cleanup failed with {secret}"), None]],
    )
    await host.start(tmp_path / "workspace")

    try:
        with pytest.raises(RuntimeError, match="replacement failed") as raised:
            await host.replace_loop_manager()

        diagnostic = "".join(traceback.format_exception(raised.value))
        assert raised.value is replacement_error
        assert "candidate cleanup did not finish" in diagnostic
        assert secret not in diagnostic
        assert host.is_ready is False
        assert host.loop_manager is created[1]
    finally:
        await host.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel_point",
    ["old_manager_stop", "candidate_start"],
)
async def test_provider_commit_finishes_before_repeated_caller_cancellation(
    tmp_path: Path,
    cancel_point: str,
) -> None:
    """Caller cancellation must not strand a successful provider commit mid-transition."""
    old_stop_release = asyncio.Event() if cancel_point == "old_manager_stop" else None
    candidate_start_release = (
        asyncio.Event() if cancel_point == "candidate_start" else None
    )
    controls = [
        _LoopControl(stop_release=old_stop_release),
        _LoopControl(start_release=candidate_start_release),
    ]
    host, config, bus, backend, created, config_path, original_bytes = _provider_runtime(
        tmp_path,
        controls,
    )
    await host.start(tmp_path / "workspace")
    previous = created[0]
    service = SettingsService(host)

    def mutation(current: AppConfig) -> None:
        current.providers.configurations[0].api_base = "https://committed.invalid/v1"

    task = asyncio.create_task(
        service.mutate(mutation, restart_reason="provider_configuration_changed")
    )
    entered = (
        controls[0].stop_entered
        if cancel_point == "old_manager_stop"
        else controls[1].start_entered
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    task.cancel()
    task.cancel()
    if old_stop_release is not None:
        old_stop_release.set()
    if candidate_start_release is not None:
        candidate_start_release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)

    try:
        assert host.is_ready is True
        assert host.loop_manager is created[1]
        assert backend.loop_manager is created[1]
        assert created[1] is not previous
        assert previous.stop_calls == 1
        assert created[1].running is True
        assert config.config.providers.configurations[0].api_base == (
            "https://committed.invalid/v1"
        )
        assert config_path.read_bytes() != original_bytes
        assert bus.shutdown_calls == 0
    finally:
        await host.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel_point",
    ["failed_candidate_cleanup", "recovery_start"],
)
async def test_provider_failure_recovers_before_repeated_caller_cancellation(
    tmp_path: Path,
    cancel_point: str,
) -> None:
    """Replacement failure must restore exact config and a fresh live manager first."""
    cleanup_release = (
        asyncio.Event() if cancel_point == "failed_candidate_cleanup" else None
    )
    recovery_start_release = (
        asyncio.Event() if cancel_point == "recovery_start" else None
    )
    replacement_error = RuntimeError("replacement failed")
    controls = [
        _LoopControl(),
        _LoopControl(start_error=replacement_error, stop_release=cleanup_release),
        _LoopControl(start_release=recovery_start_release),
    ]
    host, config, bus, backend, created, config_path, original_bytes = _provider_runtime(
        tmp_path,
        controls,
    )
    await host.start(tmp_path / "workspace")
    previous = created[0]
    service = SettingsService(host)

    def mutation(current: AppConfig) -> None:
        current.providers.configurations[0].api_key = "transient-secret"

    task = asyncio.create_task(
        service.mutate(mutation, restart_reason="provider_configuration_changed")
    )
    entered = (
        controls[1].stop_entered
        if cancel_point == "failed_candidate_cleanup"
        else controls[2].start_entered
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    task.cancel()
    task.cancel()
    if cleanup_release is not None:
        cleanup_release.set()
    if recovery_start_release is not None:
        recovery_start_release.set()

    with pytest.raises(RuntimeError, match="replacement failed") as raised:
        await asyncio.wait_for(task, timeout=1.0)

    try:
        assert raised.value is replacement_error
        assert config.config.providers.configurations[0].api_key == "provider-secret"
        assert config_path.read_bytes() == original_bytes
        assert host.is_ready is True
        assert host.loop_manager is created[2]
        assert backend.loop_manager is created[2]
        assert created[2] is not previous
        assert created[1].stop_calls == 1
        assert created[1].running is False
        assert created[2].provider_registry["provider-1"]["api_key"] == (
            "provider-secret"
        )
        assert bus.shutdown_calls == 0
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_provider_recovery_failure_is_safe_and_leaves_host_failed(
    tmp_path: Path,
) -> None:
    """Recovery diagnostics must not expose keys or publish a false READY state."""
    secret = "provider-secret"
    replacement_error = RuntimeError("replacement failed")
    recovery_error = RuntimeError(f"recovery failed with {secret}")
    controls = [
        _LoopControl(),
        _LoopControl(start_error=replacement_error),
        _LoopControl(start_error=recovery_error),
    ]
    host, config, bus, backend, created, config_path, original_bytes = _provider_runtime(
        tmp_path,
        controls,
    )
    await host.start(tmp_path / "workspace")
    service = SettingsService(host)

    def mutation(current: AppConfig) -> None:
        current.providers.configurations[0].api_key = "transient-secret"

    try:
        with pytest.raises(RuntimeConsistencyFailedError) as raised:
            await service.mutate(
                mutation,
                restart_reason="provider_configuration_changed",
            )

        diagnostics = "".join(traceback.format_exception(raised.value))
        assert str(raised.value) == "Runtime consistency could not be guaranteed"
        assert raised.value.__cause__ is replacement_error
        assert raised.value.__context__ is None
        assert replacement_error.__cause__ is None
        assert replacement_error.__context__ is None
        assert recovery_error not in {
            raised.value.__cause__,
            raised.value.__context__,
            replacement_error.__cause__,
            replacement_error.__context__,
        }
        assert "recovery" in diagnostics.lower()
        assert secret not in diagnostics
        assert "transient-secret" not in diagnostics
        assert config.config.providers.configurations[0].api_key == secret
        assert config_path.read_bytes() == original_bytes
        assert host.is_ready is False
        assert host.loop_manager is None
        assert backend.loop_manager is None
        assert created[1].stop_calls == 1
        assert created[2].stop_calls == 1
        assert bus.shutdown_calls == 0
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_persisted_rollback_failure_blocks_runtime_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh manager must not become READY when exact bytes remain unrolled back."""
    rollback_secret = "rollback-write-api-key"
    replacement_error = RuntimeError("replacement failed")
    rollback_error = OSError(f"rollback failed with {rollback_secret}")
    controls = [
        _LoopControl(),
        _LoopControl(start_error=replacement_error),
        _LoopControl(),
    ]
    host, config, _, backend, created, config_path, original_bytes = _provider_runtime(
        tmp_path,
        controls,
    )
    await host.start(tmp_path / "workspace")
    service = SettingsService(host)
    original_write_bytes = Path.write_bytes

    def reject_exact_restore(path: Path, content: bytes) -> int:
        if path == config_path and content == original_bytes:
            raise rollback_error
        return original_write_bytes(path, content)

    monkeypatch.setattr(Path, "write_bytes", reject_exact_restore)

    def mutation(current: AppConfig) -> None:
        current.providers.configurations[0].api_key = "transient-secret"

    try:
        with pytest.raises(RuntimeConsistencyFailedError) as raised:
            await service.mutate(
                mutation,
                restart_reason="provider_configuration_changed",
            )

        diagnostic = "".join(traceback.format_exception(raised.value))
        assert str(raised.value) == "Runtime consistency could not be guaranteed"
        assert raised.value.__cause__ is replacement_error
        assert raised.value.__context__ is None
        assert replacement_error.__cause__ is None
        assert replacement_error.__context__ is None
        assert rollback_error not in {
            raised.value.__cause__,
            raised.value.__context__,
            replacement_error.__cause__,
            replacement_error.__context__,
        }
        assert "persistence rollback did not finish" in diagnostic
        assert rollback_secret not in diagnostic
        assert "transient-secret" not in diagnostic
        assert "provider-secret" not in diagnostic
        assert config.config.providers.configurations[0].api_key == "provider-secret"
        assert config_path.read_bytes() != original_bytes
        assert b"transient-secret" in config_path.read_bytes()
        assert len(created) == 2
        assert host.is_ready is False
        assert host.loop_manager is None
        assert backend.loop_manager is None
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_stopped_host_rejects_recovery_after_clean_recovery_failure(
    tmp_path: Path,
) -> None:
    """Closing the shared bus must make a later recovery unable to revive loops."""
    events: list[str] = []
    host, bus, backend, created = _host(
        events,
        start_errors=[
            None,
            RuntimeError("replacement failed"),
            RuntimeError("recovery failed"),
            None,
        ],
    )
    await host.start(tmp_path / "workspace")

    try:
        with pytest.raises(RuntimeError, match="replacement failed"):
            await host.replace_loop_manager()
        with pytest.raises(RuntimeError, match="recovery failed"):
            await host.replace_loop_manager(recovery=True)

        assert host.is_ready is False
        assert host.loop_manager is None
        assert backend.loop_manager is None
        assert created[1].stop_calls == 1
        assert created[2].stop_calls == 1

        await host.stop()

        assert bus.shutdown_calls == 1
        with pytest.raises(RuntimeStartupError) as raised:
            await host.replace_loop_manager(recovery=True)
        assert raised.value.code == "RUNTIME_NOT_READY"
        assert len(created) == 3
        assert events.count("bus:start") == 1
        assert host.is_ready is False
        assert host.loop_manager is None
        assert backend.loop_manager is None
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_to_thread_non_abandoning_joins_worker_before_rethrowing_cancellation() -> None:
    """A cancelled request must not leave its owned worker thread running detached."""
    from manyselves.application.async_ownership import to_thread_non_abandoning

    started = threading.Event()
    release = threading.Event()

    def worker() -> str:
        started.set()
        release.wait(timeout=2.0)
        return "finished"

    task = asyncio.create_task(to_thread_non_abandoning(worker))
    async with asyncio.timeout(1.0):
        while not started.is_set():
            await asyncio.sleep(0.01)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_manyselves_app_keeps_false_for_missing_provider_keys(tmp_path: Path) -> None:
    """The desktop compatibility layer must retain its legacy boolean outcome."""
    from manyselves.app import ManyselvesApp

    host, _, _, _ = _host([], valid_config=False)
    desktop = ManyselvesApp(runtime_host=host)

    assert await desktop.startup(tmp_path) is False
    assert desktop.config_manager is host.config_manager
    assert desktop.bus is host.bus
    assert desktop.backend is host.backend
    assert desktop.loop_manager is None


@pytest.mark.asyncio
async def test_manyselves_app_does_not_hide_other_startup_failures(tmp_path: Path) -> None:
    """Only the documented no-provider error may become a False result."""
    from manyselves.app import ManyselvesApp

    host, _, _, _ = _host([], start_error=RuntimeError("unexpected bootstrap failure"))
    desktop = ManyselvesApp(runtime_host=host)

    with pytest.raises(RuntimeError, match="unexpected bootstrap failure"):
        await desktop.startup(tmp_path)
