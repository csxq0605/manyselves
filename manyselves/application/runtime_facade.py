"""Stable serialized command and query boundary for runtime clients."""

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, TypeVar, cast
from uuid import UUID

from .control import ControlLeaseService
from .errors import CommandIdConflictError, RuntimeNotReadyError
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
SemanticPayload = tuple[str | None, ...]
MutationCommand = (
    SendMessageCommand | SendFileContextCommand | InterruptCommand | RollbackCommand
)
OperationKind = Literal[
    "send_user_message",
    "send_file_context",
    "interrupt",
    "rollback",
]


@dataclass(frozen=True, slots=True)
class _CachedCommand:
    operation: OperationKind
    payload: SemanticPayload
    response: CommandResponse


def _backend_context_text(value: Any) -> str:
    """Normalize a context value exactly as editor-context prompt building does."""
    return str(value).strip()


def _semantic_payload(command: MutationCommand) -> SemanticPayload:
    """Capture only immutable values consumed by the matching backend operation."""
    if isinstance(command, SendMessageCommand):
        return (
            command.content,
            command.agent_id,
            command.message_id,
            command.source,
        )
    if isinstance(command, SendFileContextCommand):
        context = command.file_context
        context_type = context.get("type")
        if context_type == "selection":
            selected_lines = (
                f"{context.get('start_line', '')}-{context.get('end_line', '')}"
            ).strip()
            return (
                command.agent_id,
                "selection",
                _backend_context_text(context.get("file", "")),
                selected_lines,
            )
        if context_type == "file":
            return (
                command.agent_id,
                "file",
                _backend_context_text(context.get("file", "")),
            )
        return (command.agent_id, "ignored")
    if isinstance(command, InterruptCommand):
        return (command.agent_id,)
    if isinstance(command, RollbackCommand):
        return (command.agent_id, command.checkpoint_id)
    raise AssertionError("Unsupported runtime command type")


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
        self._command_cache: OrderedDict[UUID, _CachedCommand] = OrderedDict()
        self._mutation_lock = asyncio.Lock()

    def snapshot(self) -> RuntimeSnapshot:
        """Return a point-in-time read-only runtime view."""
        return self._adapter.snapshot(
            controller_client_id=self.leases.current_controller_client_id
        )

    @asynccontextmanager
    async def read_transaction(self) -> AsyncIterator[None]:
        """Serialize one coherent read with every runtime mutation."""
        async with self._mutation_lock:
            yield

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

        return await self._mutate(
            "send_user_message",
            command,
            AcceptedCommand,
            invoke,
        )

    async def send_file_context(self, command: SendFileContextCommand) -> AcceptedCommand:
        """Send structured file context under the shared mutation lock."""

        async def invoke() -> AcceptedCommand:
            await self._host.backend.send_file_context(command.file_context, command.agent_id)
            return AcceptedCommand(command_id=command.command_id)

        return await self._mutate(
            "send_file_context",
            command,
            AcceptedCommand,
            invoke,
        )

    async def interrupt(self, command: InterruptCommand) -> AcceptedCommand:
        """Interrupt an agent under the shared mutation lock."""

        async def invoke() -> AcceptedCommand:
            await self._host.backend.interrupt_current_message(command.agent_id)
            return AcceptedCommand(command_id=command.command_id)

        return await self._mutate(
            "interrupt",
            command,
            AcceptedCommand,
            invoke,
        )

    async def rollback(self, command: RollbackCommand) -> RollbackResult:
        """Rollback an agent and preserve the backend's client-facing result data."""

        async def invoke() -> RollbackResult:
            result = await self._host.backend.rollback_to_checkpoint(
                command.agent_id,
                command.checkpoint_id,
            )
            return RollbackResult.model_validate(result)

        return await self._mutate(
            "rollback",
            command,
            RollbackResult,
            invoke,
        )

    async def _mutate(
        self,
        operation: OperationKind,
        command: MutationCommand,
        response_type: type[ResponseT],
        invoke: Callable[[], Awaitable[ResponseT]],
    ) -> ResponseT:
        async with self._mutation_lock:
            self.leases.require(command.lease_token)
            if not self._host.is_ready:
                raise RuntimeNotReadyError()

            payload = _semantic_payload(command)
            cached = self._command_cache.get(command.command_id)
            if cached is not None:
                if cached.operation != operation or cached.payload != payload:
                    raise CommandIdConflictError()
                if not isinstance(cached.response, response_type):
                    raise AssertionError("Command cache response type invariant violated")
                return cast(ResponseT, cached.response)

            response = await invoke()
            if not isinstance(response, response_type):
                raise AssertionError("Command response type invariant violated")
            self._command_cache[command.command_id] = _CachedCommand(
                operation=operation,
                payload=payload,
                response=response,
            )
            while len(self._command_cache) > self._command_cache_size:
                self._command_cache.popitem(last=False)
            return response
