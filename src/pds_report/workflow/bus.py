from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable

from pds_report.workflow.events import Event, EventType

Subscriber = Callable[[Event], None | Awaitable[None]]
Unsubscribe = Callable[[], None]


class MessageBus:
    def __init__(self) -> None:
        self._subscribers: dict[EventType, list[Subscriber]] = defaultdict(list)
        self._history: list[Event] = []

    @property
    def history(self) -> list[Event]:
        return list(self._history)

    def subscribe(self, event_type: EventType, subscriber: Subscriber) -> Unsubscribe:
        self._subscribers[event_type].append(subscriber)

        def unsubscribe() -> None:
            subscribers = self._subscribers[event_type]
            if subscriber in subscribers:
                subscribers.remove(subscriber)

        return unsubscribe

    async def publish(self, event: Event) -> None:
        self._history.append(event)
        for subscriber in tuple(self._subscribers[event.type]):
            result = subscriber(event)
            if inspect.isawaitable(result):
                await result

