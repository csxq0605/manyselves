import asyncio

import pytest

from pds_report.workflow.bus import MessageBus
from pds_report.workflow.events import Event, EventType


@pytest.mark.asyncio
async def test_publish_delivers_event_to_typed_subscriber() -> None:
    bus = MessageBus()
    received: list[Event] = []
    bus.subscribe(EventType.AGENT_STARTED, received.append)

    event = Event(type=EventType.AGENT_STARTED, source="planner", run_id="run-1")
    await bus.publish(event)

    assert [item.source for item in received] == ["planner"]
    assert bus.history == [event]


@pytest.mark.asyncio
async def test_publish_awaits_async_subscriber() -> None:
    bus = MessageBus()
    finished = asyncio.Event()

    async def subscriber(event: Event) -> None:
        assert event.type is EventType.RUN_COMPLETED
        await asyncio.sleep(0)
        finished.set()

    bus.subscribe(EventType.RUN_COMPLETED, subscriber)
    await bus.publish(Event(type=EventType.RUN_COMPLETED, source="runner"))

    assert finished.is_set()


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery() -> None:
    bus = MessageBus()
    received: list[Event] = []
    unsubscribe = bus.subscribe(EventType.TASK_UPDATED, received.append)
    unsubscribe()

    await bus.publish(Event(type=EventType.TASK_UPDATED, source="board"))

    assert received == []

