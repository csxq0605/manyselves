"""ConversationStore orchestration for headless runtime clients."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from ..core.conversations import ConversationStore
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
from ..runtime.loops.bus import MessageBus
from .async_ownership import await_owned
from .errors import RuntimeBusyError, RuntimeConsistencyFailedError
from .runtime_facade import RuntimeFacade

_T = TypeVar("_T")


class ConversationNotFoundError(LookupError):
    code = "CONVERSATION_NOT_FOUND"


class ConversationInvalidError(ValueError):
    code = "INVALID_CONVERSATION"


class ConversationProjectMismatchError(RuntimeError):
    code = "CONVERSATION_PROJECT_MISMATCH"


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


@dataclass(frozen=True, slots=True)
class ConversationMutationSnapshot:
    sessions_existed: bool
    sessions_bytes: bytes
    existing_jsonl_paths: frozenset[Path]
    affected_files: dict[Path, tuple[bool, bytes]]
    current_session_ids: dict[str, str]
    backend_histories: dict[str, list[dict[str, Any]]]
    loop_histories: dict[str, list[Any] | None]
    loop_session_ids: dict[str, str | None]
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
        self._project_bound_sessions: set[str] = set()
        self._closed = False
        bus.subscribe(Message, self._on_message)

    def rebind(self, workspace: Path) -> None:
        """Point future conversation reads and writes at an activated project."""
        self.workspace = Path(workspace).resolve()
        self.store = ConversationStore(self.workspace)
        self._streams.clear()
        self._message_sessions.clear()
        self._project_bound_sessions.clear()

    @property
    def project_id(self) -> str:
        return self.workspace.name

    def require_project(self, project_id: str) -> None:
        if str(project_id) != self.project_id:
            raise ConversationProjectMismatchError()

    def require_active_project(
        self,
        agent_id: str,
        project_id: str,
    ) -> None:
        """Validate the request and the active conversation against this workspace."""
        self.require_project(project_id)
        active = self.store.get_current_session_id(agent_id)
        metadata = next(
            (
                item
                for item in self.store._load_sessions_metadata()  # noqa: SLF001
                if item.get("id") == active
            ),
            None,
        )
        if metadata is not None:
            self._bound_metadata(metadata)

    def bind_run_to_active_conversation(
        self,
        run_id: str,
        agent_id: str = "main",
    ) -> str | None:
        """Persist which durable Main conversation owns a newly started Run."""
        session_id = self.store.get_current_session_id(agent_id)
        sessions = self.store._load_sessions_metadata()  # noqa: SLF001
        for item in sessions:
            if item.get("id") != session_id:
                continue
            self._bound_metadata(item)
            run_ids = list(item.get("runIds", []))
            if run_id not in run_ids:
                run_ids.append(run_id)
                item["runIds"] = run_ids
                item.setdefault("projectId", self.project_id)
                self.store._save_sessions_metadata(sessions)  # noqa: SLF001
            self._project_bound_sessions.add(session_id)
            return session_id
        return None

    def run_ids_for_conversation(
        self,
        session_id: str,
        *,
        project_id: str | None = None,
    ) -> frozenset[str]:
        """Return the Runs explicitly owned by one durable Main conversation."""
        self._require_session_project(session_id, project_id)
        metadata = next(
            item
            for item in self.store._load_sessions_metadata()  # noqa: SLF001
            if item.get("id") == session_id
        )
        return frozenset(metadata.get("runIds", []))

    def list(
        self,
        agent_id: str = "main",
        *,
        project_id: str | None = None,
    ) -> tuple[list[dict], str]:
        if project_id is not None:
            self.require_project(project_id)
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
        return [self._bound_metadata(item) for item in items], active

    @property
    def pending_persistence(self) -> bool:
        """Whether a queued or executing bus callback can still write the store."""
        queue = getattr(self._bus, "_queue", None)
        return self._pending_writes > 0 or (queue is not None and not queue.empty())

    async def create(
        self,
        name: str,
        agent_id: str = "main",
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if project_id is not None:
            self.require_project(project_id)
        clean = self._name(name)
        snapshot = self._snapshot_mutation({agent_id})

        async def operation() -> dict[str, Any]:
            # This existing store helper is the only boundary that persists a named,
            # intentionally empty session in the protected sessions.json format.
            session_id = self.store._create_new_session(clean)  # noqa: SLF001
            self._persist_project_binding(session_id)
            if not self.store.switch_session(session_id, agent_id):
                raise AssertionError("newly persisted conversation could not be activated")
            # Record which Agent owns this intentionally empty conversation so a
            # fresh store can restore it before the first user message exists.
            self.store._get_session_file_path(agent_id).touch(exist_ok=True)  # noqa: SLF001
            await self._sync(agent_id, clear_pending=True)
            return self._session(session_id, agent_id)

        return await self._run_mutation(operation, snapshot)

    async def activate(
        self,
        session_id: str,
        agent_id: str = "main",
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_session_project(session_id, project_id)
        snapshot = self._snapshot_mutation({agent_id})

        async def operation() -> dict[str, Any]:
            self._persist_project_binding(session_id)
            if not self.store.switch_session(session_id, agent_id):
                raise AssertionError("prevalidated conversation disappeared")
            await self._sync(agent_id, clear_pending=True)
            return self._session(session_id, agent_id)

        return await self._run_mutation(operation, snapshot)

    def rename(
        self,
        session_id: str,
        name: str,
        agent_id: str = "main",
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_session_project(session_id, project_id)
        if not self.store.rename_session(session_id, self._name(name)):
            raise ConversationNotFoundError(session_id)
        self._persist_project_binding(session_id)
        return self._session(session_id, agent_id)

    async def delete(
        self,
        session_id: str,
        agent_id: str = "main",
        *,
        project_id: str | None = None,
    ) -> str:
        self._require_session_project(session_id, project_id)
        metadata_before = self.store._load_sessions_metadata()  # noqa: SLF001
        if not any(item.get("id") == session_id for item in metadata_before):
            raise ConversationNotFoundError(session_id)
        expected_metadata = [item for item in metadata_before if item.get("id") != session_id]
        current_ids = dict(self.store._current_session_ids)  # noqa: SLF001
        affected = [item for item, active in current_ids.items() if active == session_id]
        snapshot = self._snapshot_mutation(set(affected), deleted_session_id=session_id)
        target_paths = {
            path
            for path, (existed, _durable_bytes) in snapshot.affected_files.items()
            if existed and path.stem == session_id
        }

        async def operation() -> str:
            self.store.delete_session(session_id)
            sessions_path = self.workspace / ".manyselves" / "conversations" / "sessions.json"
            try:
                persisted_metadata = json.loads(sessions_path.read_text("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise RuntimeError("Conversation metadata deletion did not persist") from error
            if persisted_metadata != expected_metadata:
                raise RuntimeError("Conversation metadata deletion did not persist")
            if any(path.exists() for path in target_paths):
                raise RuntimeError("Conversation history deletion did not persist")
            for affected_agent in affected:
                await self._sync(affected_agent, clear_pending=True)
            return self.store.get_current_session_id(agent_id)

        return await self._run_mutation(operation, snapshot)

    async def clear(
        self,
        agent_id: str = "main",
        *,
        project_id: str | None = None,
    ) -> str:
        if project_id is not None:
            self.require_project(project_id)
        snapshot = self._snapshot_mutation({agent_id})

        async def operation() -> str:
            session_id = self.store.new_session(agent_type=agent_id)
            await self._sync(agent_id, clear_pending=True)
            return session_id

        return await self._run_mutation(operation, snapshot)

    def _snapshot_mutation(
        self,
        affected_agents: set[str],
        deleted_session_id: str | None = None,
    ) -> ConversationMutationSnapshot:
        root = self.workspace / ".manyselves" / "conversations"
        sessions_path = root / "sessions.json"
        existing_jsonl_paths = frozenset(root.rglob("*.jsonl"))
        current_session_ids = dict(self.store._current_session_ids)  # noqa: SLF001
        affected_paths: set[Path] = set()
        for affected_agent in affected_agents:
            current_id = current_session_ids.get(affected_agent)
            if current_id is not None:
                affected_paths.add(root / affected_agent / f"{current_id}.jsonl")
        if deleted_session_id is not None:
            affected_paths.update(root.rglob(f"{deleted_session_id}.jsonl"))

        manager = self.facade._host.loop_manager  # noqa: SLF001
        get_loop = getattr(manager, "get_loop", None)
        backend_histories: dict[str, list[dict[str, Any]]] = {}
        loop_histories: dict[str, list[Any] | None] = {}
        loop_session_ids: dict[str, str | None] = {}
        for affected_agent in affected_agents:
            backend_histories[affected_agent] = (
                self._backend_messages(affected_agent)
                if affected_agent in current_session_ids
                else []
            )
            loop = get_loop(affected_agent) if callable(get_loop) else None
            current_history = getattr(loop, "_conversation_history", None)
            loop_histories[affected_agent] = (
                copy.deepcopy(current_history) if isinstance(current_history, list) else None
            )
            if loop is not None and hasattr(loop, "_current_session_id"):
                loop_session_ids[affected_agent] = loop._current_session_id  # noqa: SLF001

        return ConversationMutationSnapshot(
            sessions_existed=sessions_path.exists(),
            sessions_bytes=sessions_path.read_bytes() if sessions_path.exists() else b"",
            existing_jsonl_paths=existing_jsonl_paths,
            affected_files={
                path: (path.exists(), path.read_bytes() if path.exists() else b"")
                for path in affected_paths
            },
            current_session_ids=current_session_ids,
            backend_histories=backend_histories,
            loop_histories=loop_histories,
            loop_session_ids=loop_session_ids,
            streams=dict(self._streams),
            message_sessions=dict(self._message_sessions),
        )

    async def _restore_mutation_snapshot(self, snapshot: ConversationMutationSnapshot) -> None:
        root = self.workspace / ".manyselves" / "conversations"
        root.mkdir(parents=True, exist_ok=True)
        sessions_path = root / "sessions.json"
        if snapshot.sessions_existed:
            sessions_path.write_bytes(snapshot.sessions_bytes)
            with sessions_path.open("rb") as handle:
                os.fsync(handle.fileno())
        else:
            sessions_path.unlink(missing_ok=True)

        for path, (existed, durable_bytes) in snapshot.affected_files.items():
            if existed:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(durable_bytes)
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
            else:
                path.unlink(missing_ok=True)
        for path in root.rglob("*.jsonl"):
            if path not in snapshot.existing_jsonl_paths:
                path.unlink(missing_ok=True)

        self.store._current_session_ids = dict(snapshot.current_session_ids)  # noqa: SLF001
        self._streams = dict(snapshot.streams)
        self._message_sessions = dict(snapshot.message_sessions)
        self._project_bound_sessions.clear()

        manager = self.facade._host.loop_manager  # noqa: SLF001
        get_loop = getattr(manager, "get_loop", None)
        for affected_agent, backend_history in snapshot.backend_histories.items():
            session_id = (
                snapshot.loop_session_ids[affected_agent]
                if affected_agent in snapshot.loop_session_ids
                else snapshot.current_session_ids.get(affected_agent)
            )
            await self.facade._host.backend.sync_agent_conversation(  # noqa: SLF001
                affected_agent,
                copy.deepcopy(backend_history),
                session_id=session_id,
                clear_pending=True,
            )
            loop = get_loop(affected_agent) if callable(get_loop) else None
            current_history = getattr(loop, "_conversation_history", None)
            loop_history = snapshot.loop_histories.get(affected_agent)
            if loop_history is not None and isinstance(current_history, list):
                current_history.clear()
                current_history.extend(copy.deepcopy(loop_history))
            if (
                affected_agent in snapshot.loop_session_ids
                and loop is not None
                and hasattr(loop, "_current_session_id")
            ):
                loop._current_session_id = snapshot.loop_session_ids[  # noqa: SLF001
                    affected_agent
                ]
        self._fsync_directories(root)

    async def _run_mutation(
        self,
        operation: Callable[[], Awaitable[_T]],
        snapshot: ConversationMutationSnapshot,
    ) -> _T:
        async def compensate_on_failure() -> _T:
            try:
                return await operation()
            except BaseException:
                try:
                    await self._restore_mutation_snapshot(snapshot)
                except BaseException as restore_error:
                    restore_error.add_note("Conversation mutation compensation did not complete")
                    cleanup_error = await self.facade.fail_consistency()
                    if cleanup_error is not None:
                        restore_error.add_note("Runtime consistency cleanup did not complete")
                    raise RuntimeConsistencyFailedError() from restore_error
                raise

        outcome = await await_owned(compensate_on_failure())
        if outcome.error is not None:
            raise outcome.error
        if outcome.cancellation_requested:
            raise asyncio.CancelledError
        return outcome.result()

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

    def require_message(self, agent_id: str, message_id: str, *, role: str | None = None) -> None:
        """Validate a durable rollback/edit target before irreversible work."""
        if not any(
            item.get("message_id") == message_id and (role is None or item.get("role") == role)
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
                    self._persist_project_binding(session_id)
                elif isinstance(message, AgentResponse) and not message.internal:
                    self._persist_agent_response(message)
                elif isinstance(message, ToolCallMessage):
                    agent_id = str(message.agent_type)
                    self.store.append_tool_call(agent_id, message.tool_name, message.arguments)
                    self._persist_current_project_binding(agent_id)
                elif isinstance(message, ToolResult):
                    agent_id = str(message.agent_type)
                    self.store.append_tool_result(
                        agent_id,
                        message.tool_name,
                        str(message.result or ""),
                        message.error,
                    )
                    self._persist_current_project_binding(agent_id)
                elif isinstance(message, Checkpoint):
                    agent_id = str(message.agent_type)
                    self.store.append_message(
                        agent_id,
                        "checkpoint",
                        message.description,
                        {
                            "checkpoint_id": message.checkpoint_id,
                            "message_id": message.message_id,
                        },
                    )
                    self._persist_current_project_binding(agent_id)
                elif isinstance(message, (ReportMessage, SystemNotice, Error)):
                    agent_id = str(getattr(message, "agent_type", "main"))
                    if isinstance(message, Error) or (
                        isinstance(message, SystemNotice) and message.kind == "interrupt"
                    ):
                        self._clear_agent_state(agent_id)
                    content = str(
                        getattr(message, "content", "") or getattr(message, "message", "")
                    )
                    self.store.append_message(agent_id, "system", content)
                    self._persist_current_project_binding(agent_id)
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
            self._persist_project_binding(session_id, store=target_store)

    def _clear_agent_state(self, agent_id: str) -> None:
        self._streams = {key: value for key, value in self._streams.items() if key[1] != agent_id}
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
            **self._bound_metadata(metadata),
            "active": self.store.get_current_session_id(agent_id) == session_id,
        }

    def _require_session_project(
        self,
        session_id: str,
        project_id: str | None,
    ) -> None:
        if project_id is not None:
            self.require_project(project_id)
        metadata = next(
            (
                item
                for item in self.store._load_sessions_metadata()  # noqa: SLF001
                if item.get("id") == session_id
            ),
            None,
        )
        if metadata is None:
            raise ConversationNotFoundError(session_id)
        self._bound_metadata(metadata)

    def _bound_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        stored = metadata.get("projectId")
        if stored is not None and stored != self.project_id:
            raise ConversationProjectMismatchError()
        if stored is not None:
            self._project_bound_sessions.add(str(metadata["id"]))
        return {**metadata, "projectId": self.project_id}

    def _persist_current_project_binding(self, agent_id: str) -> None:
        self._persist_project_binding(self.store.get_current_session_id(agent_id))

    def _persist_project_binding(
        self,
        session_id: str,
        *,
        store: ConversationStore | None = None,
    ) -> None:
        if store is None and session_id in self._project_bound_sessions:
            return
        target = self.store if store is None else store
        sessions = target._load_sessions_metadata()  # noqa: SLF001
        for item in sessions:
            if item.get("id") != session_id:
                continue
            stored = item.get("projectId")
            if stored is not None and stored != self.project_id:
                raise ConversationProjectMismatchError()
            if stored is None:
                item["projectId"] = self.project_id
                target._save_sessions_metadata(sessions)  # noqa: SLF001
            if store is None:
                self._project_bound_sessions.add(session_id)
            return
        raise ConversationNotFoundError(session_id)

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
