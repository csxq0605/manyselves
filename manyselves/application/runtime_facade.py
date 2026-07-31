"""Stable serialized command and query boundary for runtime clients."""

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TypeVar, cast
from uuid import UUID

from .control import ControlLeaseService
from .errors import RuntimeNotReadyError
from .legacy_runtime_adapter import LegacyRuntimeAdapter
from .models import (
    AcceptedCommand,
    InterruptCommand,
    RollbackCommand,
    RollbackResult,
    RuntimeSnapshot,
    SendFileContextCommand,
    SendMessageCommand,
)
from .runtime_host import RuntimeHost

CommandResponse = AcceptedCommand | RollbackResult
ResponseT = TypeVar("ResponseT", bound=CommandResponse)


class RuntimeFacade:
    """Serialize all mutations and expose a small typed runtime surface."""

    def __init__(
        self,
        host: RuntimeHost,
        *,
        leases: ControlLeaseService | None = None,
        adapter: LegacyRuntimeAdapter | None = None,
        command_cache_size: int = 256,
    ) -> None:
        if command_cache_size <= 0:
            raise ValueError("Command cache size must be positive")
        self._host = host
        self.leases = leases or ControlLeaseService()
        self._adapter = adapter or LegacyRuntimeAdapter(host)
        self._command_cache_size = command_cache_size
        self._command_cache: OrderedDict[UUID, CommandResponse] = OrderedDict()
        self._mutation_lock = asyncio.Lock()

    def snapshot(self) -> RuntimeSnapshot:
        """Return a point-in-time read-only runtime view."""
        return self._adapter.snapshot(
            controller_client_id=self.leases.current_controller_client_id
        )

    async def send_user_message(self, command: SendMessageCommand) -> AcceptedCommand:
        """Send one message after control, readiness, and idempotency checks."""

        async def invoke() -> AcceptedCommand:
            await self._host.backend.send_user_message(
                command.content,
                command.agent_id,
                message_id=command.message_id,
                source=command.source,
            )
            return AcceptedCommand(command_id=command.command_id)

        return cast(
            AcceptedCommand,
            await self._mutate(command.command_id, command.lease_token, invoke),
        )

    async def send_file_context(self, command: SendFileContextCommand) -> AcceptedCommand:
        """Send structured file context under the shared mutation lock."""

        async def invoke() -> AcceptedCommand:
            await self._host.backend.send_file_context(command.file_context, command.agent_id)
            return AcceptedCommand(command_id=command.command_id)

        return cast(
            AcceptedCommand,
            await self._mutate(command.command_id, command.lease_token, invoke),
        )

    async def interrupt(self, command: InterruptCommand) -> AcceptedCommand:
        """Interrupt an agent under the shared mutation lock."""

        async def invoke() -> AcceptedCommand:
            await self._host.backend.interrupt_current_message(command.agent_id)
            return AcceptedCommand(command_id=command.command_id)

        return cast(
            AcceptedCommand,
            await self._mutate(command.command_id, command.lease_token, invoke),
        )

    async def rollback(self, command: RollbackCommand) -> RollbackResult:
        """Rollback an agent and preserve the backend's client-facing result data."""

        async def invoke() -> RollbackResult:
            result = await self._host.backend.rollback_to_checkpoint(
                command.agent_id,
                command.checkpoint_id,
            )
            return RollbackResult.model_validate(result)

        return cast(
            RollbackResult,
            await self._mutate(command.command_id, command.lease_token, invoke),
        )

    async def _mutate(
        self,
        command_id: UUID,
        lease_token: str,
        invoke: Callable[[], Awaitable[ResponseT]],
    ) -> CommandResponse:
        async with self._mutation_lock:
            self.leases.require(lease_token)
            if not self._host.is_ready:
                raise RuntimeNotReadyError()

            cached = self._command_cache.get(command_id)
            if cached is not None:
                return cached

            response = await invoke()
            self._command_cache[command_id] = response
            while len(self._command_cache) > self._command_cache_size:
                self._command_cache.popitem(last=False)
            return response
