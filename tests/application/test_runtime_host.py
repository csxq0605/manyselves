"""Lifecycle contract shared by desktop and future runtime clients."""

import asyncio
from pathlib import Path
from typing import cast

import pytest

from manyselves.application.backend_api import BackendAPIImpl
from manyselves.application.errors import RuntimeStartupError
from manyselves.application.runtime_host import RuntimeHost
from manyselves.config import ConfigManager
from manyselves.core.loops import LoopManager, MessageBus


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
    ) -> None:
        self.events = events
        self.start_error = start_error
        self.start_entered = start_entered
        self.start_release = start_release
        self.stop_failures = stop_failures
        self.stop_calls = 0
        self.running = False

    async def start(self) -> None:
        self.events.append("loops:start")
        if self.start_entered is not None:
            self.start_entered.set()
        if self.start_release is not None:
            await self.start_release.wait()
        if self.start_error is not None:
            raise self.start_error
        self.running = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.events.append("loops:stop")
        self.running = False
        if self.stop_failures > 0:
            self.stop_failures -= 1
            raise RuntimeError("loop cleanup failed")


class _FakeBackend:
    def __init__(self) -> None:
        self.loop_manager: _FakeLoopManager | None = None

    def set_loop_manager(self, loop_manager: _FakeLoopManager | None) -> None:
        self.loop_manager = loop_manager


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
