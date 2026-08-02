"""Recoverable client display state projected outside the protected runtime core."""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from ..interfaces.types import (
    ApiDebugMessage,
    Checkpoint,
    Message,
    QueueUpdateMessage,
    ToolCallMessage,
    ToolResult,
)
from .models import (
    RuntimeCheckpointSnapshot,
    RuntimeDebugSnapshot,
    RuntimeQueueSnapshot,
    RuntimeTaskSnapshot,
    RuntimeToolSnapshot,
)


class RuntimeStateProjection:
    """Keep the bounded transient state that cannot be reconstructed from stores."""

    def __init__(self, *, debug_capacity: int = 100, tool_capacity: int = 100) -> None:
        if debug_capacity <= 0 or tool_capacity <= 0:
            raise ValueError("Debug and tool capacities must be positive")
        self._lock = threading.RLock()
        self._queues: dict[str, list[str]] = {}
        self._tools: dict[str, RuntimeToolSnapshot] = {}
        self._tool_capacity = tool_capacity
        self._debug: deque[RuntimeDebugSnapshot] = deque(maxlen=debug_capacity)
        self._checkpoints: dict[tuple[str, str], RuntimeCheckpointSnapshot] = {}

    def observe(self, message: Message) -> None:
        """Project one already-accepted runtime message into a recoverable read model."""
        with self._lock:
            if isinstance(message, QueueUpdateMessage):
                self._queues[str(message.agent_type)] = list(message.queued_messages)
            elif isinstance(message, ToolCallMessage):
                if message.tool_call_id is None:
                    return
                agent_id = str(message.agent_type)
                self._set_tool(
                    RuntimeToolSnapshot(
                        tool_call_id=message.tool_call_id,
                        agent_id=agent_id,
                        name=message.tool_name,
                        arguments=message.arguments,
                        status="running",
                    )
                )
            elif isinstance(message, ToolResult):
                if message.tool_call_id is None:
                    return
                agent_id = str(message.agent_type)
                previous = self._tools.get(message.tool_call_id)
                self._set_tool(
                    RuntimeToolSnapshot(
                        tool_call_id=message.tool_call_id,
                        agent_id=agent_id,
                        name=message.tool_name,
                        arguments=previous.arguments if previous is not None else {},
                        status="failed" if message.error else "completed",
                        result=message.result,
                        error=message.error,
                    )
                )
            elif isinstance(message, ApiDebugMessage):
                self._debug.append(
                    RuntimeDebugSnapshot(
                        agent_id=str(message.agent_type),
                        model=message.model,
                        tokens_in=message.tokens_in,
                        tokens_out=message.tokens_out,
                        duration_ms=message.duration_ms,
                        status=message.status,
                        timestamp=message.timestamp,
                        error=message.error,
                    )
                )
            elif isinstance(message, Checkpoint):
                agent_id = str(message.agent_type)
                item = RuntimeCheckpointSnapshot(
                    checkpoint_id=message.checkpoint_id,
                    agent_id=agent_id,
                    timestamp=message.timestamp.isoformat(),
                    epoch=0,
                    description=message.description,
                    source="event",
                    message_id=message.message_id,
                )
                self._checkpoints[(agent_id, item.checkpoint_id)] = item

    def _set_tool(self, item: RuntimeToolSnapshot) -> None:
        if item.tool_call_id not in self._tools and len(self._tools) >= self._tool_capacity:
            self._tools.pop(next(iter(self._tools)))
        self._tools[item.tool_call_id] = item

    def build(self, manager: Any, statuses: dict[str, str]) -> dict[str, list[Any]]:
        """Merge transient projection with authoritative manager/store snapshots."""
        with self._lock:
            queues = dict(self._queues)
            tools = list(self._tools.values())
            debug = list(self._debug)
            observed_checkpoints = dict(self._checkpoints)

        queue_rows: list[RuntimeQueueSnapshot] = []
        get_loop = getattr(manager, "get_loop", None)
        for agent_id in sorted(set(statuses) | set(queues)):
            messages = list(queues.get(agent_id, []))
            loop = get_loop(agent_id) if callable(get_loop) else None
            queue = getattr(loop, "_message_queue", None)
            if queue is None and agent_id not in queues:
                continue
            pending_count = queue.qsize() if queue is not None else len(messages)
            queue_rows.append(
                RuntimeQueueSnapshot(
                    agent_id=agent_id,
                    pending_count=pending_count,
                    queued_messages=messages,
                )
            )

        task_rows: list[RuntimeTaskSnapshot] = []
        board = getattr(manager, "_task_board", None)
        get_all = getattr(board, "get_all", None)
        if callable(get_all):
            for item in get_all():
                task_rows.append(
                    RuntimeTaskSnapshot(
                        task_id=item.task_id,
                        source_agent=str(item.source_agent),
                        target_agent=str(item.target_agent),
                        status=str(getattr(item.status, "value", item.status)),
                        brief=item.brief,
                        blocking=item.blocking,
                        session_id=item.session_id,
                    )
                )

        checkpoint_rows = observed_checkpoints
        checkpoint_manager = getattr(manager, "checkpoint_manager", None)
        list_checkpoints = getattr(checkpoint_manager, "list_checkpoints", None)
        if callable(list_checkpoints):
            agent_ids = set(statuses)
            agent_ids.update(item.target_agent for item in task_rows)
            for agent_id in agent_ids:
                for item in list_checkpoints(agent_id):
                    checkpoint_rows[(agent_id, item.id)] = RuntimeCheckpointSnapshot(
                        checkpoint_id=item.id,
                        agent_id=agent_id,
                        timestamp=item.timestamp,
                        epoch=item.epoch,
                        description=item.description,
                        source=item.source,
                        message_id=item.message_id,
                    )

        return {
            "queues": queue_rows,
            "tasks": task_rows,
            "tools": tools,
            "debug": debug,
            "checkpoints": sorted(
                checkpoint_rows.values(), key=lambda item: (item.agent_id, item.epoch)
            ),
        }

    def debug_for(self, agent_id: str) -> list[RuntimeDebugSnapshot]:
        with self._lock:
            return [item for item in self._debug if item.agent_id == agent_id]
