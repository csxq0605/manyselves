"""Bounded, concurrency-safe process-local event replay."""

from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass

from .models import EventEnvelope

_EVENT_ID = re.compile(r"evt-(0|[1-9][0-9]*)\Z")


@dataclass(frozen=True, slots=True)
class ReplayResult:
    events: tuple[EventEnvelope, ...] = ()
    requires_resync: bool = False
    reason: str | None = None


class ReplayBuffer:
    """Retain a monotonic suffix of public events for Last-Event-ID recovery."""

    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("Replay capacity must be a positive integer")
        self.capacity = capacity
        self._events: deque[EventEnvelope] = deque(maxlen=capacity)
        self._lock = threading.RLock()

    def append(self, event: EventEnvelope) -> None:
        with self._lock:
            if event.event_id != f"evt-{event.sequence}":
                raise ValueError("Replay event ID must match its sequence")
            if self._events and event.sequence <= self._events[-1].sequence:
                raise ValueError("Replay event sequence must be strictly monotonic")
            self._events.append(event)

    def snapshot(self) -> tuple[EventEnvelope, ...]:
        with self._lock:
            return tuple(self._events)

    def after(self, cursor: str | None) -> ReplayResult:
        """Return only events after a retained cursor, otherwise require bootstrap resync."""
        with self._lock:
            events = tuple(self._events)
            if cursor is None:
                return ReplayResult()
            match = _EVENT_ID.fullmatch(cursor)
            if match is None:
                return ReplayResult(requires_resync=True, reason="malformed")
            cursor_sequence = int(match.group(1))
            if cursor_sequence == 0:
                return ReplayResult(events=events)
            if not events:
                return ReplayResult(requires_resync=True, reason="future")
            oldest = events[0].sequence
            latest = events[-1].sequence
            if cursor_sequence < oldest:
                return ReplayResult(requires_resync=True, reason="evicted")
            if cursor_sequence > latest:
                return ReplayResult(requires_resync=True, reason="future")
            for index, item in enumerate(events):
                if item.event_id == cursor:
                    return ReplayResult(events=events[index + 1 :])
            return ReplayResult(requires_resync=True, reason="unknown")
