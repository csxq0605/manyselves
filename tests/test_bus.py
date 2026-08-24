"""Tests for async message bus."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from manyselves.interfaces.types import (
    AgentResponse,
    AgentResultMessage,
    Message,
    UserMessage,
)
from manyselves.runtime.loops.bus import MessageBus


@pytest.fixture
def bus():
    return MessageBus()


@pytest.mark.asyncio
async def test_publish_and_subscribe(bus):
    received = []

    async def callback(msg):
        received.append(msg)

    bus.subscribe(UserMessage, callback)

    msg = UserMessage(content="Hello")
    await bus.publish(msg)
    await bus._notify_subscribers(msg)

    assert len(received) == 1
    assert received[0].content == "Hello"


@pytest.mark.asyncio
async def test_sync_callback(bus):
    received = []

    def callback(msg):
        received.append(msg)

    bus.subscribe(UserMessage, callback)

    msg = UserMessage(content="Sync test")
    await bus._notify_subscribers(msg)

    assert len(received) == 1


@pytest.mark.asyncio
async def test_unsubscribe(bus):
    received = []

    async def callback(msg):
        received.append(msg)

    bus.subscribe(UserMessage, callback)
    bus.unsubscribe(UserMessage, callback)

    msg = UserMessage(content="Should not receive")
    await bus._notify_subscribers(msg)

    assert len(received) == 0


@pytest.mark.asyncio
async def test_multiple_subscribers(bus):
    received_a = []
    received_b = []

    async def callback_a(msg):
        received_a.append(msg)

    async def callback_b(msg):
        received_b.append(msg)

    bus.subscribe(UserMessage, callback_a)
    bus.subscribe(UserMessage, callback_b)

    msg = UserMessage(content="Broadcast")
    await bus._notify_subscribers(msg)

    assert len(received_a) == 1
    assert len(received_b) == 1


@pytest.mark.asyncio
async def test_type_specific_delivery(bus):
    received = []

    async def callback(msg):
        received.append(msg)

    bus.subscribe(AgentResponse, callback)

    msg = UserMessage(content="Wrong type")
    await bus._notify_subscribers(msg)

    assert len(received) == 0


@pytest.mark.asyncio
async def test_cross_type_delivery_for_base_message(bus):
    """Subscribers to Message base class should receive all message types."""
    received = []

    async def callback(msg):
        received.append(msg)

    bus.subscribe(Message, callback)

    msg = UserMessage(content="Any message")
    await bus._notify_subscribers(msg)

    # UserMessage inherits from Message
    assert len(received) == 1


@pytest.mark.asyncio
async def test_callback_error_does_not_crash_bus(bus):
    """An error in one callback should not prevent others from running."""

    async def bad_callback(msg):
        raise RuntimeError("Callback error")

    good_received = []

    async def good_callback(msg):
        good_received.append(msg)

    bus.subscribe(UserMessage, bad_callback)
    bus.subscribe(UserMessage, good_callback)

    msg = UserMessage(content="Test")
    await bus._notify_subscribers(msg)

    assert len(good_received) == 1


@pytest.mark.asyncio
async def test_publish_queues_message(bus):
    msg = UserMessage(content="Queued")
    await bus.publish(msg)
    assert not bus._queue.empty()


@pytest.mark.asyncio
async def test_publish_logs_hot_path_at_trace_and_shutdown_summarizes_once(
    bus,
    monkeypatch,
):
    trace = Mock()
    debug = Mock()
    monkeypatch.setattr(
        "manyselves.runtime.loops.bus.logger",
        SimpleNamespace(trace=trace, debug=debug),
    )
    msg = UserMessage(content="Queued")

    await bus.publish(msg)

    trace.assert_called_once_with("Published message: {}", msg.type)
    debug.assert_not_called()
    assert bus._published_counts == {str(msg.type): 1}

    bus.shutdown()
    bus.shutdown()

    debug.assert_called_once_with(
        "Message bus shutdown; published counts: {}",
        {str(msg.type): 1},
    )


@pytest.mark.asyncio
async def test_wait_for_resolves_only_matching_message_and_unsubscribes(bus):
    processor = asyncio.create_task(bus.process_queue())
    waiter = asyncio.create_task(
        bus.wait_for(
            AgentResultMessage,
            lambda message: message.task_id == "wanted",
            timeout=1.0,
        )
    )
    await asyncio.sleep(0)
    await bus.publish(
        AgentResultMessage(
            workflow_id="wf-1",
            task_id="other",
            run_id="run-1",
            sender="auditor",
            recipient="workflow",
            result_path="other.json",
            status="completed",
        )
    )
    await bus.publish(
        AgentResultMessage(
            workflow_id="wf-1",
            task_id="wanted",
            run_id="run-1",
            sender="auditor",
            recipient="workflow",
            result_path="wanted.json",
            status="completed",
        )
    )

    assert (await waiter).result_path == "wanted.json"
    assert bus._subscribers.get(AgentResultMessage) == []
    bus.shutdown()
    await processor


@pytest.mark.asyncio
async def test_slow_stream_subscriber_does_not_delay_typed_terminal() -> None:
    bus = MessageBus(stream_capacity=2)
    stream_started = asyncio.Event()
    release_stream = asyncio.Event()
    terminal_received = asyncio.Event()

    async def slow_stream(message: AgentResponse) -> None:
        if message.streaming:
            stream_started.set()
            await release_stream.wait()

    def receive_terminal(message: AgentResultMessage) -> None:
        terminal_received.set()

    bus.subscribe(AgentResponse, slow_stream)
    bus.subscribe(AgentResultMessage, receive_terminal)
    processor = asyncio.create_task(bus.process_queue())
    await bus.publish(
        AgentResponse(
            agent_type="runtime-2.1",
            content="delta",
            message_id="module-2.1",
            streaming=True,
            workflow_id="wf",
            run_id="run",
            task_id="module-2.1",
            task_attempt_id="attempt-1",
            session_id="session-1",
        )
    )
    await asyncio.wait_for(stream_started.wait(), timeout=1)
    await bus.publish(
        AgentResultMessage(
            workflow_id="wf",
            run_id="run",
            task_id="module-2.1",
            task_attempt_id="attempt-1",
            sender="module-2.1-specialist",
            recipient="workflow",
            result_path="result.json",
            status="completed",
        )
    )

    await asyncio.wait_for(terminal_received.wait(), timeout=0.2)
    release_stream.set()
    bus.shutdown()
    await processor


@pytest.mark.asyncio
async def test_stream_plane_coalesces_and_terminal_fences_late_delta() -> None:
    bus = MessageBus(stream_capacity=1)
    first = AgentResponse(
        agent_type="runtime-2.1",
        content="a",
        message_id="module-2.1",
        streaming=True,
        workflow_id="wf",
        run_id="run",
        task_id="module-2.1",
        task_attempt_id="attempt-1",
        session_id="session-1",
    )
    await bus.publish(first)
    await bus.publish(first.model_copy(update={"content": "b"}))
    assert len(bus._stream_pending) == 1
    assert next(iter(bus._stream_pending.values())).content == "ab"
    await bus.publish(
        AgentResultMessage(
            workflow_id="wf",
            run_id="run",
            task_id="module-2.1",
            task_attempt_id="attempt-1",
            sender="module-2.1-specialist",
            recipient="workflow",
            result_path="result.json",
            status="completed",
        )
    )
    assert not bus._stream_pending
    await bus.publish(first.model_copy(update={"content": "late"}))
    assert not bus._stream_pending
    assert bus.stream_metrics["coalesced"] == 1
    assert bus.stream_metrics["after_terminal_dropped"] == 1


@pytest.mark.asyncio
async def test_wait_for_timeout_unsubscribes(bus):
    with pytest.raises(asyncio.TimeoutError):
        await bus.wait_for(UserMessage, lambda _: True, timeout=0.01)
    assert bus._subscribers.get(UserMessage) == []
