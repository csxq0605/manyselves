"""Non-blocking event fanout with bounded per-client delivery buffers."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from ...interfaces.types import Message
from .mapper import EventContext, EventMapper
from .models import EventEnvelope
from .replay import ReplayBuffer, ReplayResult


class EventBus(Protocol):
    def subscribe(self, message_type: type[Message], callback: object) -> None: ...

    def unsubscribe(self, message_type: type[Message], callback: object) -> None: ...


ContextResolver = Callable[[Message, int], EventContext]


class EventClient:
    """One loop-local bounded delivery buffer that never blocks publication."""

    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("Client capacity must be a positive integer")
        self.capacity = capacity
        self._events: deque[EventEnvelope] = deque()
        self._available = asyncio.Event()
        self.closed = False

    async def get(self) -> EventEnvelope:
        while True:
            if self._events:
                event = self._events.popleft()
                if not self._events:
                    self._available.clear()
                return event
            await self._available.wait()

    def seed(
        self,
        outcome: ReplayResult,
        resync_factory: Callable[[str], EventEnvelope],
    ) -> None:
        if outcome.requires_resync:
            self._fail_closed(resync_factory(str(outcome.reason or "unknown")))
        elif len(outcome.events) > self.capacity:
            self._fail_closed(resync_factory("replay_overflow"))
        else:
            self._events.extend(outcome.events)
            if self._events:
                self._available.set()

    def offer(
        self,
        event: EventEnvelope,
        resync_factory: Callable[[str], EventEnvelope],
    ) -> None:
        if self.closed:
            return
        if len(self._events) < self.capacity:
            self._events.append(event)
            self._available.set()
            return

        matching_delta = self._matching_delta(event.message_id)
        if (
            matching_delta is not None
            and matching_delta is self._events[-1]
            and event.type
            in {
                "agent.message.delta",
                "agent.message.completed",
            }
        ):
            self._events.remove(matching_delta)
            self._events.append(_absorb_delta(matching_delta, event))
            self._available.set()
            return

        self._fail_closed(resync_factory("client_overflow"))

    def close(self, event: EventEnvelope) -> None:
        self._fail_closed(event)

    def _matching_delta(self, message_id: str | None) -> EventEnvelope | None:
        if message_id is None:
            return None
        return next(
            (
                event
                for event in self._events
                if event.type == "agent.message.delta" and event.message_id == message_id
            ),
            None,
        )

    def _fail_closed(self, event: EventEnvelope) -> None:
        self._events.clear()
        self._events.append(event)
        self.closed = True
        self._available.set()


class EventBroker:
    """Map every processed bus message once and fan it out without slow-client waits."""

    def __init__(
        self,
        *,
        bus: EventBus,
        context_resolver: ContextResolver,
        replay_capacity: int,
        client_capacity: int,
        mapper: EventMapper | None = None,
    ) -> None:
        if (
            isinstance(client_capacity, bool)
            or not isinstance(client_capacity, int)
            or client_capacity <= 0
        ):
            raise ValueError("Client capacity must be a positive integer")
        self._bus = bus
        self._context_resolver = context_resolver
        self._mapper = mapper or EventMapper()
        self._client_capacity = client_capacity
        self._clients: set[EventClient] = set()
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._started = False
        self._closed = False
        self.replay = ReplayBuffer(replay_capacity)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def start(self) -> None:
        """Subscribe exactly once to the one shared runtime bus."""
        if self._closed:
            raise RuntimeError("Event broker is closed")
        if self._started:
            return
        self._bus.subscribe(Message, self.publish_internal)
        self._started = True

    async def publish_internal(self, message: Message) -> None:
        """Map and publish without awaiting any individual client consumer."""
        async with self._lock:
            if self._closed:
                return
            sequence = self._sequence + 1
            context = self._context_resolver(message, sequence)
            event = self._mapper.map(message, context=context)
            if event.sequence != sequence or event.event_id != f"evt-{sequence}":
                raise ValueError("Event context must use the broker sequence and event ID")
            self._sequence = sequence
            self.replay.append(event)
            for client in tuple(self._clients):
                client.offer(event, self._resync_event)

    async def register(self, cursor: str | None) -> EventClient:
        """Atomically capture replay and register for all later publications."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("Event broker is closed")
            client = EventClient(self._client_capacity)
            client.seed(self.replay.after(cursor), self._resync_event)
            self._clients.add(client)
            return client

    async def unregister(self, client: EventClient) -> None:
        async with self._lock:
            self._clients.discard(client)

    async def close(self) -> None:
        """Unsubscribe and wake clients before the shared MessageBus is stopped."""
        async with self._lock:
            if self._closed:
                return
            if self._started:
                self._bus.unsubscribe(Message, self.publish_internal)
            closing = self._resync_event("broker_closed")
            for client in tuple(self._clients):
                client.close(closing)
            self._clients.clear()
            self._closed = True

    def _resync_event(self, reason: str) -> EventEnvelope:
        latest = self.replay.snapshot()
        previous = latest[-1] if latest else None
        return EventEnvelope(
            eventId=previous.event_id if previous is not None else "evt-0",
            sequence=previous.sequence if previous is not None else 0,
            type="stream.resync_required",
            timestamp=datetime.now(UTC),
            projectId=previous.project_id if previous is not None else None,
            sessionId=previous.session_id if previous is not None else None,
            agentId=previous.agent_id if previous is not None else None,
            runId=previous.run_id if previous is not None else None,
            messageId=previous.message_id if previous is not None else None,
            payload={"reason": reason},
        )


def _absorb_delta(delta: EventEnvelope, incoming: EventEnvelope) -> EventEnvelope:
    previous_content = str(delta.payload.get("content", ""))
    incoming_content = str(incoming.payload.get("content", ""))
    if incoming.type == "agent.message.completed":
        content = incoming_content or previous_content
    elif not previous_content or incoming_content.startswith(previous_content):
        content = incoming_content
    elif previous_content.endswith(incoming_content):
        content = previous_content
    else:
        content = previous_content + incoming_content

    prior_delivery = delta.payload.get("delivery", {})
    prior_count = (
        int(prior_delivery.get("coalescedCount", 1)) if isinstance(prior_delivery, dict) else 1
    )
    first_sequence = (
        int(prior_delivery.get("fromSequence", delta.sequence))
        if isinstance(prior_delivery, dict)
        else delta.sequence
    )
    payload = dict(incoming.payload)
    payload["content"] = content
    payload["delivery"] = {
        "coalescedCount": prior_count + (1 if incoming.type == "agent.message.delta" else 0),
        "fromSequence": first_sequence,
        "toSequence": incoming.sequence,
    }
    return incoming.model_copy(update={"payload": payload})
