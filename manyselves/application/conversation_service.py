"""ConversationStore orchestration for headless runtime clients."""

from __future__ import annotations

import asyncio
import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.conversations import ConversationStore
from ..core.loops.bus import MessageBus
from ..interfaces.types import (
    AgentResponse,
    Checkpoint,
    Error,
    Message,
    ReportMessage,
    SystemNotice,
    ToolCallMessage,
    ToolResult,
    UserMessage,
)
from .errors import RuntimeBusyError, RuntimeConsistencyFailedError
from .runtime_facade import RuntimeFacade


class ConversationNotFoundError(LookupError):
    code = "CONVERSATION_NOT_FOUND"


class ConversationInvalidError(ValueError):
    code = "INVALID_CONVERSATION"


@dataclass(frozen=True, slots=True)
class ConversationTransactionSnapshot:
    agent_id: str
    session_id: str
    path: Path
    existed: bool
    durable_bytes: bytes
    sessions_path: Path
    sessions_existed: bool
    sessions_bytes: bytes
    current_session_ids: dict[str, str]
    backend_history: list[dict[str, Any]]
    loop_history: list[Any] | None
    streams: dict[tuple[str, str, str], str]
    message_sessions: dict[tuple[str, str], str]


class ConversationService:
    """Delegate durable records to the existing store and sync runtime history."""

    def __init__(
        self,
        workspace: Path,
        *,
        facade: RuntimeFacade,
        bus: MessageBus,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = ConversationStore(self.workspace)
        self.facade = facade
        self._bus = bus
        self._pending_writes = 0
        self._streams: dict[tuple[str, str, str], str] = {}
        self._message_sessions: dict[tuple[str, str], str] = {}
        self._closed = False
        bus.subscribe(Message, self._on_message)

    def rebind(self, workspace: Path) -> None:
        """Point future conversation reads and writes at an activated project."""
        self.workspace = Path(workspace).resolve()
        self.store = ConversationStore(self.workspace)
        self._streams.clear()
        self._message_sessions.clear()

    def list(self, agent_id: str = "main") -> tuple[list[dict], str]:
        items = self.store.get_sessions(agent_id)
        active = self.store.get_current_session_id(agent_id)
        if not any(item.get("id") == active for item in items):
            metadata = next(
                (
                    item
                    for item in self.store._load_sessions_metadata()  # noqa: SLF001
                    if item.get("id") == active
                ),
                None,
            )
            if metadata is not None:
                items.insert(0, metadata)
        return items, active

    @property
    def pending_persistence(self) -> bool:
        """Whether a queued or executing bus callback can still write the store."""
        queue = getattr(self._bus, "_queue", None)
        return self._pending_writes > 0 or (queue is not None and not queue.empty())

    def create(self, name: str, agent_id: str = "main") -> dict[str, Any]:
        clean = self._name(name)
        # This existing store helper is the only boundary that persists a named,
        # intentionally empty session in the protected sessions.json format.
        session_id = self.store._create_new_session(clean)  # noqa: SLF001
        if not self.store.switch_session(session_id, agent_id):
            raise AssertionError("newly persisted conversation could not be activated")
        return self._session(session_id, agent_id)

    async def activate(self, session_id: str, agent_id: str = "main") -> dict[str, Any]:
        if not self.store.switch_session(session_id, agent_id):
            raise ConversationNotFoundError(session_id)
        await self._sync(agent_id, clear_pending=True)
        return self._session(session_id, agent_id)

    def rename(self, session_id: str, name: str, agent_id: str = "main") -> dict[str, Any]:
        if not self.store.rename_session(session_id, self._name(name)):
            raise ConversationNotFoundError(session_id)
        return self._session(session_id, agent_id)

    async def delete(self, session_id: str, agent_id: str = "main") -> str:
        if not any(item.get("id") == session_id for item in self.store._load_sessions_metadata()):  # noqa: SLF001
            raise ConversationNotFoundError(session_id)
        current_ids = dict(self.store._current_session_ids)  # noqa: SLF001
        affected = [item for item, active in current_ids.items() if active == session_id]
        self.store.delete_session(session_id)
        for affected_agent in affected:
            await self._sync(affected_agent, clear_pending=True)
        return self.store.get_current_session_id(agent_id)

    async def clear(self, agent_id: str = "main") -> str:
        session_id = self.store.new_session(agent_type=agent_id)
        await self._sync(agent_id, clear_pending=True)
        return session_id

    def messages(self, agent_id: str = "main") -> list[dict[str, Any]]:
        return self.store.load_messages(agent_id)

    async def prepare_edit_resend(
        self, agent_id: str, target_message_id: str
    ) -> ConversationTransactionSnapshot:
        self.require_agent_idle(agent_id)
        self.require_message(agent_id, target_message_id, role="user")
        snapshot = self.snapshot(agent_id)
        try:
            if not self.store.truncate_from_message(
                agent_id, message_id=target_message_id, role="user"
            ):
                raise AssertionError("prevalidated edit target disappeared")
            await self._sync(agent_id, clear_pending=True)
        except BaseException as prepare_error:
            try:
                await self.restore(snapshot)
            except BaseException as restore_error:
                restore_error.add_note(
                    f"Edit-resend prepare failed before compensation: {prepare_error!r}"
                )
                cleanup_error = await self.facade.fail_consistency()
                if cleanup_error is not None:
                    restore_error.add_note(
                        f"Consistency producer shutdown failed: {cleanup_error!r}"
                    )
                raise RuntimeConsistencyFailedError() from restore_error
            raise
        return snapshot

    async def apply_rollback(
        self,
        agent_id: str,
        target_message_id: str | None,
        conversation_history: list[dict[str, Any]],
    ) -> None:
        if target_message_id and not self.store.truncate_from_message(
            agent_id, message_id=target_message_id
        ):
            raise AssertionError("prevalidated rollback target disappeared")
        self._clear_agent_state(agent_id)
        await self.facade._host.backend.sync_agent_conversation(  # noqa: SLF001
            agent_id,
            conversation_history,
            session_id=self.store.get_current_session_id(agent_id),
            clear_pending=True,
        )

    def require_message(
        self, agent_id: str, message_id: str, *, role: str | None = None
    ) -> None:
        """Validate a durable rollback/edit target before irreversible work."""
        if not any(
            item.get("message_id") == message_id
            and (role is None or item.get("role") == role)
            for item in self.store.load_messages(agent_id, limit=10_000)
        ):
            raise ConversationNotFoundError(message_id)

    def snapshot(self, agent_id: str) -> ConversationTransactionSnapshot:
        """Capture exact durable bytes and the live loop history before mutation."""
        session_id = self.store.get_current_session_id(agent_id)
        path = self.workspace / ".manyselves" / "conversations" / agent_id / f"{session_id}.jsonl"
        sessions_path = self.workspace / ".manyselves" / "conversations" / "sessions.json"
        loop_history: list[Any] | None = None
        manager = self.facade._host.loop_manager  # noqa: SLF001
        get_loop = getattr(manager, "get_loop", None)
        loop = get_loop(agent_id) if callable(get_loop) else None
        current_history = getattr(loop, "_conversation_history", None)
        if isinstance(current_history, list):
            loop_history = copy.deepcopy(current_history)
        return ConversationTransactionSnapshot(
            agent_id=agent_id,
            session_id=session_id,
            path=path,
            existed=path.exists(),
            durable_bytes=path.read_bytes() if path.exists() else b"",
            sessions_path=sessions_path,
            sessions_existed=sessions_path.exists(),
            sessions_bytes=sessions_path.read_bytes() if sessions_path.exists() else b"",
            current_session_ids=dict(self.store._current_session_ids),  # noqa: SLF001
            backend_history=self._backend_messages(agent_id),
            loop_history=loop_history,
            streams=dict(self._streams),
            message_sessions=dict(self._message_sessions),
        )

    async def prepare_rollback(
        self, agent_id: str, target_message_id: str | None
    ) -> ConversationTransactionSnapshot:
        self.require_agent_idle(agent_id)
        if target_message_id is not None:
            self.require_message(agent_id, target_message_id)
        return self.snapshot(agent_id)

    async def restore(self, snapshot: ConversationTransactionSnapshot) -> None:
        """Restore an application snapshot exactly, including durable JSONL bytes."""
        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.existed:
            snapshot.path.write_bytes(snapshot.durable_bytes)
            with snapshot.path.open("rb") as handle:
                os.fsync(handle.fileno())
        else:
            snapshot.path.unlink(missing_ok=True)
        if snapshot.sessions_existed:
            snapshot.sessions_path.write_bytes(snapshot.sessions_bytes)
            with snapshot.sessions_path.open("rb") as handle:
                os.fsync(handle.fileno())
        else:
            snapshot.sessions_path.unlink(missing_ok=True)
        self.store._current_session_ids = dict(snapshot.current_session_ids)  # noqa: SLF001
        self._streams = dict(snapshot.streams)
        self._message_sessions = dict(snapshot.message_sessions)

        manager = self.facade._host.loop_manager  # noqa: SLF001
        get_loop = getattr(manager, "get_loop", None)
        loop = get_loop(snapshot.agent_id) if callable(get_loop) else None
        current_history = getattr(loop, "_conversation_history", None)
        if snapshot.loop_history is not None and isinstance(current_history, list):
            current_history.clear()
            current_history.extend(copy.deepcopy(snapshot.loop_history))
        else:
            await self.facade._host.backend.sync_agent_conversation(  # noqa: SLF001
                snapshot.agent_id,
                snapshot.backend_history,
                session_id=snapshot.session_id,
                clear_pending=True,
            )

    def require_agent_idle(self, agent_id: str) -> None:
        """Require no active, queued, streaming, or persistence work for an agent."""
        status = self.facade.snapshot().agent_statuses.get(agent_id)
        if status is not None and status != "idle":
            raise RuntimeBusyError()
        if self._pending_writes > 0:
            raise RuntimeBusyError()
        queue = getattr(self._bus, "_queue", None)
        if queue is not None and not queue.empty():
            raise RuntimeBusyError()
        manager = self.facade._host.loop_manager  # noqa: SLF001
        loops = getattr(manager, "_loops", {})
        loop = loops.get(agent_id) if isinstance(loops, dict) else None
        agent_queue = getattr(loop, "_message_queue", None)
        if agent_queue is not None and not agent_queue.empty():
            raise RuntimeBusyError()
        if any(key[1] == agent_id for key in self._streams):
            raise RuntimeBusyError()

    def flush(self) -> None:
        root = self.workspace / ".manyselves" / "conversations"
        for path in root.rglob("*") if root.exists() else ():
            if path.is_file():
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
        self._fsync_directories(root)

    def require_switch_safe(self) -> None:
        """Reject session switches while a turn or persistence callback owns state."""
        statuses = self.facade.snapshot().agent_statuses.values()
        if (
            any(status != "idle" for status in statuses)
            or self.pending_persistence
            or bool(self._streams)
        ):
            raise RuntimeBusyError()

    async def close(self) -> None:
        """Drain queued persistence, unsubscribe, and fsync before host shutdown."""
        if self._closed:
            return
        queue = getattr(self._bus, "_queue", None)
        while self._pending_writes > 0 or (queue is not None and not queue.empty()):
            await asyncio.sleep(0)
        self._bus.unsubscribe(Message, self._on_message)
        self.flush()
        self._closed = True

    async def _sync(self, agent_id: str, *, clear_pending: bool = False) -> None:
        if clear_pending:
            self._clear_agent_state(agent_id)
        await self.facade._host.backend.sync_agent_conversation(  # noqa: SLF001
            agent_id,
            self._backend_messages(agent_id),
            session_id=self.store.get_current_session_id(agent_id),
            clear_pending=clear_pending,
        )

    def _backend_messages(self, agent_id: str) -> list[dict[str, Any]]:
        messages = []
        for record in self.store.load_messages(agent_id, limit=10_000):
            role = str(record.get("role", ""))
            mapped = "assistant" if role == "agent" else role
            if mapped in {"user", "assistant", "system"}:
                messages.append({"role": mapped, "content": str(record.get("content", ""))})
        return messages

    async def _on_message(self, message: Message) -> None:
        self._pending_writes += 1
        try:
            async with self.facade.persistence_transaction():
                if isinstance(message, UserMessage) and not message.internal:
                    agent_id = str(message.agent_type)
                    session_id = self.store.get_current_session_id(agent_id)
                    if message.message_id:
                        self._message_sessions[(agent_id, message.message_id)] = session_id
                    self.store.append_message(
                        agent_id,
                        "user",
                        message.content,
                        {"source": message.source, "message_id": message.message_id},
                    )
                elif isinstance(message, AgentResponse) and not message.internal:
                    self._persist_agent_response(message)
                elif isinstance(message, ToolCallMessage):
                    self.store.append_tool_call(
                        str(message.agent_type), message.tool_name, message.arguments
                    )
                elif isinstance(message, ToolResult):
                    self.store.append_tool_result(
                        str(message.agent_type),
                        message.tool_name,
                        str(message.result or ""),
                        message.error,
                    )
                elif isinstance(message, Checkpoint):
                    self.store.append_message(
                        str(message.agent_type),
                        "checkpoint",
                        message.description,
                        {
                            "checkpoint_id": message.checkpoint_id,
                            "message_id": message.message_id,
                        },
                    )
                elif isinstance(message, (ReportMessage, SystemNotice, Error)):
                    agent_id = str(getattr(message, "agent_type", "main"))
                    if isinstance(message, Error) or (
                        isinstance(message, SystemNotice) and message.kind == "interrupt"
                    ):
                        self._clear_agent_state(agent_id)
                    content = str(
                        getattr(message, "content", "")
                        or getattr(message, "message", "")
                    )
                    self.store.append_message(agent_id, "system", content)
        finally:
            self._pending_writes -= 1

    def _persist_agent_response(self, message: AgentResponse) -> None:
        agent_id = str(message.agent_type)
        if message.thinking is not None:
            return
        message_id = str(message.message_id or "")
        session_id = self._message_sessions.get(
            (agent_id, message_id), self.store.get_current_session_id(agent_id)
        )
        stream_key = (session_id, agent_id, message_id)
        previous = self._streams.get(stream_key, "")
        incoming = message.content
        if not incoming:
            merged = previous
        elif not previous or incoming.startswith(previous):
            merged = incoming
        elif previous.endswith(incoming):
            merged = previous
        else:
            merged = previous + incoming
        if message.streaming:
            self._streams[stream_key] = merged
            return
        final = merged or previous
        self._streams.pop(stream_key, None)
        self._message_sessions.pop((agent_id, message_id), None)
        if final:
            target_store = self.store
            if session_id != self.store.get_current_session_id(agent_id):
                target_store = ConversationStore(self.workspace)
                if not target_store.switch_session(session_id, agent_id):
                    return
            target_store.append_message(
                agent_id, "agent", final, {"message_id": message.message_id}
            )

    def _clear_agent_state(self, agent_id: str) -> None:
        self._streams = {
            key: value for key, value in self._streams.items() if key[1] != agent_id
        }
        self._message_sessions = {
            key: value for key, value in self._message_sessions.items() if key[0] != agent_id
        }

    def _session(self, session_id: str, agent_id: str) -> dict[str, Any]:
        metadata = next(
            (item for item in self.store._load_sessions_metadata() if item.get("id") == session_id),  # noqa: SLF001
            None,
        )
        if metadata is None:
            raise ConversationNotFoundError(session_id)
        return {
            **metadata,
            "active": self.store.get_current_session_id(agent_id) == session_id,
        }

    @staticmethod
    def _name(name: str) -> str:
        clean = str(name).strip()
        if not clean:
            raise ConversationInvalidError("Conversation name must not be blank")
        return clean

    @staticmethod
    def _fsync_directories(root: Path) -> None:
        if os.name != "posix" or not root.exists():
            return
        directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
        for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
