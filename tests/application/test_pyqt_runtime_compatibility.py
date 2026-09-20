"""Gate A compatibility evidence for the existing PyQt runtime boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from manyselves.app import ManyselvesApp
from manyselves.application.backend_api import BackendAPIImpl
from manyselves.application.control import ControlLeaseService
from manyselves.application.models import (
    InterruptCommand,
    RollbackCommand,
    SendMessageCommand,
)
from manyselves.application.runtime_facade import RuntimeFacade
from manyselves.application.runtime_host import RuntimeHost
from manyselves.config import ConfigManager
from manyselves.config.schema import ApiConfig
from manyselves.interfaces.types import RestartRequest, UserMessage
from manyselves.runtime.loops import LoopManager, MessageBus


class _LoopBoundary:
    """Minimal replacement for provider-backed loop work at the runtime edge."""

    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.interrupted_agents: list[str] = []
        self.rollback_calls: list[tuple[str, str, bool]] = []
        self.debug_changes: list[tuple[str, bool]] = []

    async def start(self) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1

    def cancel_current_operation(self, agent_type: str) -> None:
        self.interrupted_agents.append(agent_type)

    async def rollback_to_checkpoint(
        self,
        agent_type: str,
        checkpoint_id: str,
        restore_conversation: bool = True,
    ) -> dict[str, Any]:
        self.rollback_calls.append((agent_type, checkpoint_id, restore_conversation))
        return {
            "restored_files": 3,
            "conversation_history": [{"role": "user", "content": "before"}],
            "checkpoint_marker": "preserved",
        }

    async def prepare_rollback(
        self, agent_type: str, checkpoint_id: str
    ) -> dict[str, Any]:
        return {"checkpoint_id": checkpoint_id, "effect_paths": []}

    def set_agent_debug_mode(self, agent_type: str, enabled: bool) -> None:
        self.debug_changes.append((agent_type, enabled))


def _ignore_project_logging(_workspace: Path) -> None:
    """Avoid adding a process-global log sink during isolated tests."""


def _build_runtime(tmp_path: Path) -> tuple[RuntimeHost, _LoopBoundary]:
    config = ConfigManager(tmp_path / "manyselves.config.yaml")
    config.config.providers.configurations = [
        ApiConfig(
            id="compat-provider",
            name="Compatibility Provider",
            provider="anthropic",
            api_key="test-key",
        )
    ]
    config.config.providers.active = "compat-provider"
    bus = MessageBus()
    backend = BackendAPIImpl(config_manager=config, bus=bus)
    loop_boundary = _LoopBoundary()

    def create_loop_manager(
        workspace: Path,
        config_manager: ConfigManager,
        message_bus: MessageBus,
    ) -> LoopManager:
        assert workspace.is_absolute()
        assert config_manager is config
        assert message_bus is bus
        return cast(LoopManager, loop_boundary)

    host = RuntimeHost(
        config_manager=config,
        bus=bus,
        backend=backend,
        loop_manager_factory=create_loop_manager,
        project_logging_initializer=_ignore_project_logging,
    )
    return host, loop_boundary


async def _start_runtime(tmp_path: Path) -> tuple[RuntimeHost, _LoopBoundary]:
    host, loop_boundary = _build_runtime(tmp_path)
    await host.start(tmp_path / "workspace")
    return host, loop_boundary


def _facade_for(host: RuntimeHost) -> tuple[RuntimeFacade, str]:
    leases = ControlLeaseService()
    lease = leases.acquire(client_id="pyqt-desktop", actor_id="compatibility-test")
    return RuntimeFacade(host, leases=leases), lease.token


@pytest.mark.asyncio
async def test_desktop_startup_preserves_boolean_and_loop_manager(
    tmp_path: Path,
) -> None:
    """Dropping the desktop result or loop mirror must fail the PyQt startup gate."""
    host, loop_boundary = _build_runtime(tmp_path)
    desktop = ManyselvesApp(runtime_host=host)

    try:
        result = await desktop.startup(tmp_path / "workspace")

        assert result is True
        assert desktop.loop_manager is loop_boundary
        assert desktop.backend.loop_manager is loop_boundary
        assert loop_boundary.start_calls == 1
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_desktop_shutdown_preserves_owned_cleanup_boundary(
    tmp_path: Path,
) -> None:
    """Restoring process-wide cancellation must fail this desktop shutdown gate."""
    host, loop_boundary = _build_runtime(tmp_path)
    desktop = ManyselvesApp(runtime_host=host)
    unrelated_release = asyncio.Event()

    async def unrelated_work() -> None:
        await unrelated_release.wait()

    unrelated_task = asyncio.create_task(unrelated_work())
    await desktop.startup(tmp_path / "workspace")

    try:
        await desktop.shutdown()

        assert host.is_ready is False
        assert desktop.loop_manager is loop_boundary
        assert loop_boundary.stop_calls == 1
        assert unrelated_task.done() is False
    finally:
        unrelated_release.set()
        await unrelated_task
        await host.stop()


@pytest.mark.asyncio
async def test_facade_send_matches_direct_backend_observable_message(
    tmp_path: Path,
) -> None:
    """A changed command mapping must differ from the direct PyQt bus message."""
    direct_host, _ = await _start_runtime(tmp_path / "direct")
    facade_host, _ = await _start_runtime(tmp_path / "facade")
    facade, token = _facade_for(facade_host)
    direct_message = asyncio.create_task(
        direct_host.bus.wait_for(UserMessage, lambda _message: True, timeout=1.0)
    )
    facade_message = asyncio.create_task(
        facade_host.bus.wait_for(UserMessage, lambda _message: True, timeout=1.0)
    )
    await asyncio.sleep(0)

    try:
        await direct_host.backend.send_user_message(
            "inspect the report",
            "sub",
            message_id="message-42",
            source="main_agent",
        )
        accepted = await facade.send_user_message(
            SendMessageCommand(
                command_id=UUID("00000000-0000-0000-0000-000000000003"),
                lease_token=token,
                content="inspect the report",
                agent_id="sub",
                message_id="message-42",
                source="main_agent",
            )
        )
        direct_observed, facade_observed = await asyncio.gather(
            direct_message,
            facade_message,
        )

        def observable(message: UserMessage) -> tuple[str, str, str | None, str]:
            return (
                message.content,
                message.agent_type,
                message.message_id,
                message.source,
            )

        assert observable(direct_observed) == observable(facade_observed) == (
            "inspect the report",
            "main",
            "message-42",
            "main_agent",
        )
        assert accepted.status == "accepted"
    finally:
        await asyncio.gather(direct_host.stop(), facade_host.stop())


@pytest.mark.asyncio
async def test_facade_interrupt_matches_direct_backend_target(tmp_path: Path) -> None:
    """Interrupting a different agent through the facade must fail equivalence."""
    direct_host, direct_loop = await _start_runtime(tmp_path / "direct")
    facade_host, facade_loop = await _start_runtime(tmp_path / "facade")
    facade, token = _facade_for(facade_host)

    try:
        await direct_host.backend.interrupt_current_message("main")
        accepted = await facade.interrupt(
            InterruptCommand(
                command_id=UUID("00000000-0000-0000-0000-000000000004"),
                lease_token=token,
                agent_id="main",
            )
        )

        assert direct_loop.interrupted_agents == facade_loop.interrupted_agents == ["main"]
        assert accepted.status == "accepted"
    finally:
        await asyncio.gather(direct_host.stop(), facade_host.stop())


@pytest.mark.asyncio
async def test_facade_rollback_matches_direct_backend_result(tmp_path: Path) -> None:
    """Discarding backend rollback data must fail the facade compatibility gate."""
    direct_host, direct_loop = await _start_runtime(tmp_path / "direct")
    facade_host, facade_loop = await _start_runtime(tmp_path / "facade")
    facade, token = _facade_for(facade_host)

    try:
        direct_result = await direct_host.backend.rollback_to_checkpoint(
            "main",
            "checkpoint-7",
        )
        facade_result = await facade.rollback(
            RollbackCommand(
                command_id=UUID("00000000-0000-0000-0000-000000000005"),
                lease_token=token,
                agent_id="main",
                checkpoint_id="checkpoint-7",
            )
        )

        assert facade_result.model_dump() == direct_result == {
            "restored_files": 3,
            "conversation_history": [{"role": "user", "content": "before"}],
            "checkpoint_marker": "preserved",
        }
        expected_call = [("main", "checkpoint-7", True)]
        assert direct_loop.rollback_calls == facade_loop.rollback_calls == expected_call
    finally:
        await asyncio.gather(direct_host.stop(), facade_host.stop())


@pytest.mark.asyncio
async def test_direct_backend_debug_preserves_loop_manager_behavior(
    tmp_path: Path,
) -> None:
    """Debug changes must continue to flow directly to the loop manager."""
    host, loop_boundary = await _start_runtime(tmp_path)

    try:
        host.backend.set_agent_debug_mode("main", True)
        host.backend.set_agent_debug_mode("main", False)

        assert loop_boundary.debug_changes == [("main", True), ("main", False)]
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_provider_switch_assigns_provider_and_requests_restart(
    tmp_path: Path,
) -> None:
    """Provider changes without the existing restart event must fail this gate."""
    host, _ = await _start_runtime(tmp_path)
    restart = asyncio.create_task(
        host.bus.wait_for(RestartRequest, lambda _message: True, timeout=1.0)
    )
    await asyncio.sleep(0)

    try:
        await host.backend.switch_provider("openai")
        observed = await restart

        assert host.config_manager.config.agents.defaults.provider == "openai"
        assert observed.reason == "config_change"
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_model_switch_assigns_model_without_restart_request(
    tmp_path: Path,
) -> None:
    """Publishing a restart for a model-only change must fail this ordered gate."""
    host, _ = await _start_runtime(tmp_path)
    restart_requests: list[RestartRequest] = []
    host.bus.subscribe(RestartRequest, restart_requests.append)
    barrier = asyncio.create_task(
        host.bus.wait_for(
            UserMessage,
            lambda message: message.message_id == "model-switch-barrier",
            timeout=1.0,
        )
    )
    await asyncio.sleep(0)

    try:
        await host.backend.switch_model("openai/gpt-5")
        await host.bus.publish(
            UserMessage(
                content="barrier",
                agent_type="main",
                message_id="model-switch-barrier",
            )
        )
        await barrier

        assert host.config_manager.config.agents.defaults.model == "openai/gpt-5"
        assert restart_requests == []
    finally:
        await host.stop()
