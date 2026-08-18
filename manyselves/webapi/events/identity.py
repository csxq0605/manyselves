"""Normalize legacy tool event identities at the public event boundary."""

from __future__ import annotations

from collections import deque

from ...interfaces.types import Message, ToolCallMessage, ToolResult


class ToolIdentityNormalizer:
    """Correlate ID-less tool events without changing explicitly supplied IDs."""

    def __init__(self, *, outstanding_capacity: int = 100) -> None:
        if (
            isinstance(outstanding_capacity, bool)
            or not isinstance(outstanding_capacity, int)
            or outstanding_capacity <= 0
        ):
            raise ValueError("Outstanding tool capacity must be a positive integer")
        self._outstanding_capacity = outstanding_capacity
        self._outstanding: dict[tuple[str, str], deque[str]] = {}
        self._outstanding_order: dict[tuple[tuple[str, str], str], None] = {}
        self._legacy_counter = 0

    def normalize(self, message: Message) -> Message:
        """Return a message with a stable ID, leaving explicit IDs untouched."""
        if isinstance(message, ToolCallMessage):
            key = (str(message.agent_type), message.tool_name)
            if message.tool_call_id is not None:
                self._remember(key, message.tool_call_id)
                return message
            tool_call_id = self._next_legacy_id("legacy-call")
            self._remember(key, tool_call_id)
            return message.model_copy(update={"tool_call_id": tool_call_id})

        if isinstance(message, ToolResult):
            key = (str(message.agent_type), message.tool_name)
            if message.tool_call_id is not None:
                self._discard(key, message.tool_call_id)
                return message
            tool_call_id = self._take_next(key)
            if tool_call_id is None:
                tool_call_id = self._next_legacy_id("legacy-orphan")
            return message.model_copy(update={"tool_call_id": tool_call_id})

        return message

    def _remember(self, key: tuple[str, str], tool_call_id: str) -> None:
        entry = (key, tool_call_id)
        if entry in self._outstanding_order:
            self._remove(key, tool_call_id)
        while len(self._outstanding_order) >= self._outstanding_capacity:
            evicted_key, evicted_id = next(iter(self._outstanding_order))
            self._remove(evicted_key, evicted_id)
        queue = self._outstanding.setdefault(key, deque())
        queue.append(tool_call_id)
        self._outstanding_order[entry] = None

    def _discard(self, key: tuple[str, str], tool_call_id: str) -> None:
        self._remove(key, tool_call_id)

    def _remove(self, key: tuple[str, str], tool_call_id: str) -> None:
        self._outstanding_order.pop((key, tool_call_id), None)
        queue = self._outstanding.get(key)
        if queue is None:
            return
        try:
            queue.remove(tool_call_id)
        except ValueError:
            return
        if not queue:
            del self._outstanding[key]

    def _take_next(self, key: tuple[str, str]) -> str | None:
        queue = self._outstanding.get(key)
        if not queue:
            return None
        tool_call_id = queue[0]
        self._remove(key, tool_call_id)
        return tool_call_id

    def _next_legacy_id(self, prefix: str) -> str:
        self._legacy_counter += 1
        return f"{prefix}-{self._legacy_counter}"
