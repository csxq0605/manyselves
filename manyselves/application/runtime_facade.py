"""Stable serialized command and query boundary for runtime clients."""

import asyncio
import secrets
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar, cast
from uuid import UUID

from .control import ControlLeaseService
from .errors import (
    AgentNotFoundError,
    CheckpointNotFoundError,
    CommandIdConflictError,
    MaintenanceQuiescedError,
    MaintenanceTokenMismatchError,
    RuntimeBusyError,
    RuntimeNotReadyError,
)
from .legacy_runtime_adapter import LegacyRuntimeAdapter
from .models import (
    AcceptedCommand,
    EditResendCommand,
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
ActivationResponseT = TypeVar("ActivationResponseT")
SemanticPayload = tuple[str | None, ...]
MutationCommand = (
    SendMessageCommand
    | EditResendCommand
    | SendFileContextCommand
    | InterruptCommand
    | RollbackCommand
)
OperationKind = Literal[
    "send_user_message",
    "edit_resend",
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
        payload = (
            command.content,
            command.agent_id,
            command.message_id,
            command.source,
        )
        if isinstance(command, EditResendCommand):
            return (*payload, command.target_message_id)
        return payload
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
        self._maintenance_token: str | None = None

    def snapshot(self) -> RuntimeSnapshot:
        """Return a point-in-time read-only runtime view."""
        return self._adapter.snapshot(
            controller_client_id=self.leases.current_controller_client_id
        )

    @property
    def is_quiesced(self) -> bool:
        """Whether maintenance currently rejects runtime mutations."""
        return self._maintenance_token is not None

    @asynccontextmanager
    async def read_transaction(self) -> AsyncIterator[None]:
        """Serialize one coherent read with every runtime mutation."""
        async with self._mutation_lock:
            yield

    @asynccontextmanager
    async def mutation_transaction(self, lease_token: str) -> AsyncIterator[None]:
        """Serialize an application mutation and require the current controller."""
        async with self._mutation_lock:
            self.leases.require(lease_token)
            self._require_mutable()
            if not self._host.is_ready:
                raise RuntimeNotReadyError()
            yield

    @asynccontextmanager
    async def persistence_transaction(self) -> AsyncIterator[None]:
        """Serialize one durable event write and reject it after quiescence."""
        async with self._mutation_lock:
            self._require_mutable()
            if not self._host.is_ready:
                raise RuntimeNotReadyError()
            yield

    async def activate_workspace(
        self,
        *,
        lease_token: str,
        resolve_workspace: Callable[[], Path],
        commit: Callable[[], ActivationResponseT],
        rollback: Callable[[], None],
        reconcile: Callable[[Path], None],
    ) -> ActivationResponseT:
        """Resolve, switch, and commit project state as one serialized transaction."""
        async with self._mutation_lock:
            self.leases.require(lease_token)
            self._require_mutable()
            if not self._host.is_ready:
                raise RuntimeNotReadyError()
            if any(status != "idle" for status in self.snapshot().agent_statuses.values()):
                raise RuntimeBusyError()
            workspace = resolve_workspace()
            previous_workspace = self._host.workspace
            await self._host.switch_workspace(workspace)
            try:
                return commit()
            except BaseException as commit_error:
                if previous_workspace is not None:
                    try:
                        await self._host.switch_workspace(previous_workspace)
                    except BaseException as host_rollback_error:
                        commit_error.add_note(
                            f"Activation runtime rollback failed: {host_rollback_error!r}"
                        )
                actual_workspace = self._host.workspace
                if self._host.is_ready and actual_workspace == previous_workspace:
                    try:
                        rollback()
                    except BaseException as rollback_error:
                        commit_error.add_note(
                            f"Activation state rollback failed: {rollback_error!r}"
                        )
                        await self._host.mark_failed()
                elif self._host.is_ready and actual_workspace is not None:
                    try:
                        reconcile(actual_workspace)
                    except BaseException as reconcile_error:
                        commit_error.add_note(
                            f"Activation state reconciliation failed: {reconcile_error!r}"
                        )
                        await self._host.mark_failed()
                raise

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

    async def edit_resend(
        self,
        command: EditResendCommand,
        *,
        prepare: Callable[[], Awaitable[None]],
    ) -> AcceptedCommand:
        """Atomically truncate/synchronize a turn before publishing its replacement."""

        async def invoke() -> AcceptedCommand:
            await prepare()
            await self._host.backend.send_user_message(
                command.content,
                command.agent_id,
                message_id=command.message_id,
                source=command.source,
            )
            return AcceptedCommand(command_id=command.command_id)

        return await self._mutate("edit_resend", command, AcceptedCommand, invoke)

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

    async def rollback(
        self,
        command: RollbackCommand,
        *,
        before_restore: Callable[[], None] | None = None,
        after_restore: Callable[[RollbackResult], Awaitable[None]] | None = None,
    ) -> RollbackResult:
        """Rollback an agent and preserve the backend's client-facing result data."""

        async def invoke() -> RollbackResult:
            if before_restore is not None:
                before_restore()
            try:
                result = await self._host.backend.rollback_to_checkpoint(
                    command.agent_id,
                    command.checkpoint_id,
                )
            except ValueError as exc:
                if "checkpoint" in str(exc).casefold() and "not found" in str(exc).casefold():
                    raise CheckpointNotFoundError(command.checkpoint_id) from exc
                raise
            restored = RollbackResult.model_validate(result)
            if after_restore is not None:
                await after_restore(restored)
            return restored

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
            self._require_mutable()
            if not self._host.is_ready:
                raise RuntimeNotReadyError()
            self._require_known_agent(command.agent_id)

            workspace = str(self._host.workspace) if self._host.workspace is not None else None
            payload = (workspace, *_semantic_payload(command))
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

    async def quiesce(
        self,
        *,
        lease_token: str,
        busy: Callable[[], bool],
        flush: Callable[[], None],
    ) -> str:
        """Enter maintenance under the shared mutation lock after a busy check."""
        async with self._mutation_lock:
            self.leases.require(lease_token)
            if not self._host.is_ready:
                raise RuntimeNotReadyError()
            if self._maintenance_token is not None:
                raise MaintenanceQuiescedError()
            if busy():
                raise RuntimeBusyError()
            flush()
            self._maintenance_token = secrets.token_urlsafe(32)
            return self._maintenance_token

    async def release_quiesce(self, *, lease_token: str, maintenance_token: str) -> None:
        """Release maintenance only for the matching opaque token."""
        async with self._mutation_lock:
            self.leases.require(lease_token)
            if self._maintenance_token is None or not secrets.compare_digest(
                self._maintenance_token.encode(), maintenance_token.encode()
            ):
                raise MaintenanceTokenMismatchError()
            self._maintenance_token = None

    def _require_mutable(self) -> None:
        if self._maintenance_token is not None:
            raise MaintenanceQuiescedError()

    def _require_known_agent(self, agent_id: str) -> None:
        manager = self._host.loop_manager
        get_loop = getattr(manager, "get_loop", None)
        if callable(get_loop):
            if get_loop(agent_id) is None:
                raise AgentNotFoundError(agent_id)
            return
        get_statuses = getattr(manager, "get_all_agent_statuses", None)
        if callable(get_statuses) and agent_id not in get_statuses():
            raise AgentNotFoundError(agent_id)
