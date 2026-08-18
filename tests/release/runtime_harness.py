from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from manyselves.application.backend_api import BackendAPIImpl
from manyselves.application.control import ControlLeaseService
from manyselves.application.models import InterruptCommand, RollbackCommand, SendMessageCommand
from manyselves.application.runtime_facade import RuntimeFacade
from manyselves.application.runtime_host import RuntimeHost
from manyselves.config import ConfigManager
from manyselves.config.schema import ApiConfig
from manyselves.core.loops import LoopManager, MessageBus
from manyselves.interfaces.types import UserMessage


class LoopBoundary:
    def __init__(self) -> None:
        self.interrupted: list[str] = []
        self.rollbacks: list[tuple[str, str, bool]] = []

    async def start(self) -> None: pass
    async def stop(self) -> None: pass
    def cancel_current_operation(self, agent_type: str) -> None: self.interrupted.append(agent_type)
    async def prepare_rollback(self, agent_type: str, checkpoint_id: str) -> dict[str, Any]:
        return {"checkpoint_id": checkpoint_id, "effect_paths": []}
    async def rollback_to_checkpoint(self, agent_type: str, checkpoint_id: str, restore_conversation: bool = True) -> dict[str, Any]:
        self.rollbacks.append((agent_type, checkpoint_id, restore_conversation))
        return {"restored_files": 1, "conversation_history": [{"role": "user", "content": "before"}]}


@dataclass(frozen=True)
class GoldenResult:
    event: tuple[str, str, str | None, str]
    interrupted: tuple[str, ...]
    rollback: dict[str, Any]
    workspace_manifest: dict[str, str]


def _runtime(root: Path) -> tuple[RuntimeHost, LoopBoundary]:
    config = ConfigManager(root / "manyselves.config.yaml")
    config.config.providers.configurations = [ApiConfig(id="fake", name="Fake", provider="openai", api_key="test-key")]
    config.config.providers.active = "fake"
    bus = MessageBus()
    backend = BackendAPIImpl(config_manager=config, bus=bus)
    loop = LoopBoundary()
    host = RuntimeHost(
        config_manager=config,
        bus=bus,
        backend=backend,
        loop_manager_factory=lambda _workspace, _config, _bus: cast(LoopManager, loop),
        project_logging_initializer=lambda _workspace: None,
    )
    return host, loop


def _manifest(workspace: Path) -> dict[str, str]:
    return {
        path.relative_to(workspace).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(workspace.rglob("*")) if path.is_file()
    }


async def run_legacy_flow(root: Path) -> GoldenResult:
    host, loop = _runtime(root)
    workspace = root / "workspace"
    await host.start(workspace)
    observed = asyncio.create_task(host.bus.wait_for(UserMessage, lambda _message: True, timeout=2))
    await asyncio.sleep(0)
    try:
        await host.backend.send_user_message("inspect", "main", message_id="message-1", source="user")
        message = await observed
        await host.backend.interrupt_current_message("main")
        rollback = await host.backend.rollback_to_checkpoint("main", "checkpoint-1")
        return GoldenResult(
            event=(message.content, message.agent_type, message.message_id, message.source),
            interrupted=tuple(loop.interrupted), rollback=rollback, workspace_manifest=_manifest(workspace),
        )
    finally:
        await host.stop()


async def run_service_flow(root: Path) -> GoldenResult:
    host, loop = _runtime(root)
    workspace = root / "workspace"
    await host.start(workspace)
    leases = ControlLeaseService()
    lease = leases.acquire(client_id="service", actor_id="golden")
    facade = RuntimeFacade(host, leases=leases)
    observed = asyncio.create_task(host.bus.wait_for(UserMessage, lambda _message: True, timeout=2))
    await asyncio.sleep(0)
    try:
        await facade.send_user_message(SendMessageCommand(
            command_id=UUID("00000000-0000-4000-8000-000000000001"), lease_token=lease.token,
            content="inspect", agent_id="main", message_id="message-1", source="user",
        ))
        message = await observed
        await facade.interrupt(InterruptCommand(
            command_id=UUID("00000000-0000-4000-8000-000000000002"), lease_token=lease.token, agent_id="main",
        ))
        result = await facade.rollback(RollbackCommand(
            command_id=UUID("00000000-0000-4000-8000-000000000003"), lease_token=lease.token,
            agent_id="main", checkpoint_id="checkpoint-1",
        ))
        return GoldenResult(
            event=(message.content, message.agent_type, message.message_id, message.source),
            interrupted=tuple(loop.interrupted),
            rollback=result.model_dump(),
            workspace_manifest=_manifest(workspace),
        )
    finally:
        await host.stop()
