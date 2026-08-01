"""ConversationStore orchestration for headless runtime clients."""

from __future__ import annotations

import os
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
from .runtime_facade import RuntimeFacade


class ConversationNotFoundError(LookupError):
    code = "CONVERSATION_NOT_FOUND"


class ConversationInvalidError(ValueError):
    code = "INVALID_CONVERSATION"


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
        self._streams: dict[str, str] = {}
        bus.subscribe(Message, self._on_message)

    def rebind(self, workspace: Path) -> None:
        """Point future conversation reads and writes at an activated project."""
        self.workspace = Path(workspace).resolve()
        self.store = ConversationStore(self.workspace)
        self._streams.clear()

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
        self.store.delete_session(session_id)
        await self._sync(agent_id, clear_pending=True)
        return self.store.get_current_session_id(agent_id)

    async def clear(self, agent_id: str = "main") -> str:
        session_id = self.store.new_session(agent_type=agent_id)
        await self._sync(agent_id, clear_pending=True)
        return session_id

    def messages(self, agent_id: str = "main") -> list[dict[str, Any]]:
        return self.store.load_messages(agent_id)

    async def prepare_edit_resend(self, agent_id: str, target_message_id: str) -> None:
        if not self.store.truncate_from_message(
            agent_id, message_id=target_message_id, role="user"
        ):
            raise ConversationNotFoundError(target_message_id)
        await self._sync(agent_id, clear_pending=True)

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
        self._streams.pop(agent_id, None)
        await self.facade._host.backend.sync_agent_conversation(  # noqa: SLF001
            agent_id,
            conversation_history,
            session_id=self.store.get_current_session_id(agent_id),
            clear_pending=True,
        )

    def require_message(self, agent_id: str, message_id: str) -> None:
        """Validate a durable rollback/edit target before irreversible work."""
        if not any(
            item.get("message_id") == message_id
            for item in self.store.load_messages(agent_id, limit=10_000)
        ):
            raise ConversationNotFoundError(message_id)

    def flush(self) -> None:
        root = self.workspace / ".manyselves" / "conversations"
        for path in root.rglob("*") if root.exists() else ():
            if path.is_file():
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
        self._fsync_directories(root)

    async def _sync(self, agent_id: str, *, clear_pending: bool = False) -> None:
        if clear_pending:
            self._streams.pop(agent_id, None)
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
                    self.store.append_message(
                        str(message.agent_type),
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
        previous = self._streams.get(agent_id, "")
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
            self._streams[agent_id] = merged
            return
        final = merged or previous
        self._streams.pop(agent_id, None)
        if final:
            self.store.append_message(
                agent_id, "agent", final, {"message_id": message.message_id}
            )

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
