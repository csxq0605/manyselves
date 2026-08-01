"""Event broker backpressure, reconnect, SSE, and lifecycle contracts."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from manyselves.core.loops.bus import MessageBus
from manyselves.interfaces.types import AgentResponse, Message, ProgressNoteMessage, SystemNotice
from manyselves.webapi.dependencies import get_runtime_host
from manyselves.webapi.events.broker import EventBroker
from manyselves.webapi.events.mapper import EventContext
from manyselves.webapi.events.models import EventEnvelope
from manyselves.webapi.main import create_app
from manyselves.webapi.routes import events as event_routes
from manyselves.webapi.settings import WebSettings

NOW = datetime(2026, 7, 31, 10, 30, tzinfo=UTC)


def make_event(
    sequence: int,
    event_type: str = "system.notice",
    *,
    message_id: str | None = None,
    content: str | None = None,
) -> EventEnvelope:
    payload: dict[str, object] = {"sequence": sequence}
    if content is not None:
        payload["content"] = content
    return EventEnvelope(
        eventId=f"evt-{sequence}",
        sequence=sequence,
        type=event_type,
        timestamp=NOW,
        projectId="p1",
        sessionId="session-1",
        agentId="main",
        runId=None,
        messageId=message_id,
        payload=payload,
    )


class TrackingBus:
    def __init__(self) -> None:
        self.subscriptions: list[tuple[type[Message], object]] = []
        self.unsubscriptions: list[tuple[type[Message], object]] = []

    def subscribe(self, message_type: type[Message], callback: object) -> None:
        self.subscriptions.append((message_type, callback))

    def unsubscribe(self, message_type: type[Message], callback: object) -> None:
        self.unsubscriptions.append((message_type, callback))


def resolve_context(message: Message, sequence: int) -> EventContext:
    return EventContext(
        event_id=f"evt-{sequence}",
        sequence=sequence,
        project_id="p1",
        session_id="session-1",
        agent_id=str(getattr(message, "agent_type", "main")),
        run_id=None,
        message_id=getattr(message, "message_id", None),
    )


@pytest.mark.asyncio
async def test_broker_subscribes_once_maps_once_and_unsubscribes_on_close() -> None:
    bus = TrackingBus()
    broker = EventBroker(
        bus=bus,
        context_resolver=resolve_context,
        replay_capacity=4,
        client_capacity=2,
    )

    broker.start()
    broker.start()
    await broker.publish_internal(SystemNotice(agent_type="main", content="ready"))
    await broker.close()
    await broker.close()

    assert len(bus.subscriptions) == 1
    assert len(bus.unsubscriptions) == 1
    assert broker.replay.snapshot()[0].sequence == 1


@pytest.mark.asyncio
async def test_broker_delivers_monotonic_order_to_multiple_clients_without_cross_blocking() -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=10,
        client_capacity=4,
    )
    first = await broker.register(None)
    second = await broker.register(None)

    await broker.publish_internal(SystemNotice(agent_type="main", content="one"))
    await broker.publish_internal(SystemNotice(agent_type="main", content="two"))

    assert [(await first.get()).event_id, (await first.get()).event_id] == ["evt-1", "evt-2"]
    assert [(await second.get()).sequence, (await second.get()).sequence] == [1, 2]


@pytest.mark.asyncio
async def test_slow_client_coalesces_only_same_message_deltas_with_explicit_range() -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=10,
        client_capacity=1,
    )
    client = await broker.register(None)

    await broker.publish_internal(
        AgentResponse(agent_type="main", message_id="message-1", content="a", streaming=True)
    )
    await broker.publish_internal(
        AgentResponse(agent_type="main", message_id="message-1", content="b", streaming=True)
    )

    coalesced = await client.get()
    assert coalesced.type == "agent.message.delta"
    assert coalesced.sequence == 2
    assert coalesced.payload["content"] == "ab"
    assert coalesced.payload["delivery"] == {
        "coalescedCount": 2,
        "fromSequence": 1,
        "toSequence": 2,
    }


@pytest.mark.asyncio
async def test_full_client_preserves_terminal_by_replacing_same_message_delta() -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=10,
        client_capacity=1,
    )
    client = await broker.register(None)

    await broker.publish_internal(
        AgentResponse(agent_type="main", message_id="message-1", content="part", streaming=True)
    )
    await broker.publish_internal(
        AgentResponse(agent_type="main", message_id="message-1", content="whole", streaming=False)
    )

    terminal = await client.get()
    assert terminal.type == "agent.message.completed"
    assert terminal.payload["content"] == "whole"
    assert terminal.payload["delivery"] == {
        "coalescedCount": 1,
        "fromSequence": 1,
        "toSequence": 2,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_non_tail_delta_is_not_reordered_during_coalescing(streaming: bool) -> None:
    """Moving a newer delta past an intervening protected event would corrupt event order."""
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=10,
        client_capacity=2,
    )
    client = await broker.register(None)

    await broker.publish_internal(
        AgentResponse(agent_type="main", message_id="message-1", content="a", streaming=True)
    )
    await broker.publish_internal(SystemNotice(agent_type="main", content="between"))
    await broker.publish_internal(
        AgentResponse(
            agent_type="main",
            message_id="message-1",
            content="b",
            streaming=streaming,
        )
    )

    assert (await client.get()).type == "stream.resync_required"
    assert client.closed is True


@pytest.mark.asyncio
async def test_full_client_with_no_coalescible_delta_fails_closed_to_resync() -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=10,
        client_capacity=1,
    )
    slow = await broker.register(None)
    fast = await broker.register(None)

    await broker.publish_internal(SystemNotice(agent_type="main", content="protected-one"))
    assert (await fast.get()).type == "system.notice"
    await broker.publish_internal(SystemNotice(agent_type="main", content="protected-two"))

    assert (await slow.get()).type == "stream.resync_required"
    assert (await fast.get()).type == "system.notice"
    assert slow.closed is True


@pytest.mark.asyncio
async def test_register_replays_present_cursor_or_emits_non_replayed_resync() -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=2,
        client_capacity=4,
    )
    for number in range(1, 4):
        await broker.publish_internal(SystemNotice(agent_type="main", content=str(number)))

    replayed = await broker.register("evt-2")
    stale = await broker.register("evt-1")

    assert (await replayed.get()).event_id == "evt-3"
    resync = await stale.get()
    assert resync.type == "stream.resync_required"
    assert resync.payload["reason"] == "evicted"
    assert all(item.type != "stream.resync_required" for item in broker.replay.snapshot())


class FakeRuntimeHost:
    def __init__(self) -> None:
        self.is_ready = False
        self.workspace: Path | None = None
        self.bus = MessageBus()
        self.order: list[str] = []
        self.session_id = "session-1"
        self.loop_manager = SimpleNamespace(
            get_all_agent_statuses=lambda: {"main": "idle"},
            get_agent_session_id=lambda agent_id: self.session_id,
        )

    async def start(self, workspace: Path) -> None:
        self.workspace = workspace
        self.is_ready = True

    async def stop_producers(self) -> None:
        self.order.append("producers")

    async def stop_bus(self) -> None:
        self.order.append("bus")
        self.is_ready = False


def settings(tmp_path: Path, **updates: object) -> WebSettings:
    values = {
        "data_root": tmp_path,
        "initial_project_id": "p1",
        "access_token": SecretStr("test-token"),
        "sse_replay_capacity": 2,
        "sse_client_queue_capacity": 2,
    }
    values.update(updates)
    return WebSettings(**values)


@pytest.mark.asyncio
async def test_events_endpoint_requires_bearer_authentication(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/events")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_last_event_id_eviction_emits_one_line_resync_sse_and_headers(tmp_path: Path) -> None:
    host = FakeRuntimeHost()
    app = create_app(settings(tmp_path))
    app.dependency_overrides[get_runtime_host] = lambda: host
    async with app.router.lifespan_context(app):
        for number in range(1, 4):
            await app.state.event_broker.publish_internal(
                SystemNotice(agent_type="main", content=str(number))
            )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/events",
                headers={"Authorization": "Bearer test-token", "Last-Event-ID": "evt-1"},
            )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    lines = response.text.splitlines()
    assert lines[0].startswith("id: evt-")
    assert lines[1] == "event: stream.resync_required"
    assert lines[2].startswith("data: {")
    assert "\n" not in lines[2][6:]
    assert json.loads(lines[2][6:])["payload"]["reason"] == "evicted"


@pytest.mark.asyncio
async def test_sse_present_cursor_replays_only_later_events_and_unregisters() -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=4,
        client_capacity=4,
    )
    await broker.publish_internal(SystemNotice(agent_type="main", content="one"))
    await broker.publish_internal(SystemNotice(agent_type="main", content="two"))

    class RequestStub:
        app = SimpleNamespace(state=SimpleNamespace(event_broker=broker))

        async def is_disconnected(self) -> bool:
            return False

    response = await event_routes.stream_events(RequestStub(), last_event_id="evt-1")
    iterator = response.body_iterator
    frame = await anext(iterator)
    await iterator.aclose()

    assert frame.startswith("id: evt-2\nevent: system.notice\ndata: {")
    assert broker.client_count == 0


@pytest.mark.asyncio
async def test_new_client_without_cursor_does_not_receive_preconnection_backlog() -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=4,
        client_capacity=4,
    )
    await broker.publish_internal(SystemNotice(agent_type="main", content="before"))
    client = await broker.register(None)
    await broker.publish_internal(SystemNotice(agent_type="main", content="after"))

    assert (await client.get()).payload["content"] == "after"


@pytest.mark.asyncio
async def test_event_context_reads_active_project_and_session_at_processing_time(
    tmp_path: Path,
) -> None:
    host = FakeRuntimeHost()
    app = create_app(settings(tmp_path, sse_replay_capacity=4))
    app.dependency_overrides[get_runtime_host] = lambda: host

    async with app.router.lifespan_context(app):
        await app.state.event_broker.publish_internal(
            SystemNotice(agent_type="main", content="first")
        )
        app.state.project_registry.create("p2")
        app.state.project_registry.activate("p2")
        host.session_id = "session-2"
        await app.state.event_broker.publish_internal(
            ProgressNoteMessage(
                task_id="task-1",
                sender="main",
                workflow_id="workflow-run-2",
                content="second",
            )
        )
        first, second = app.state.event_broker.replay.snapshot()

    assert (first.project_id, first.session_id) == ("p1", "session-1")
    assert (second.project_id, second.session_id) == ("p2", "session-2")
    assert second.run_id == "workflow-run-2"


@pytest.mark.asyncio
async def test_heartbeat_is_not_replayed_and_cancel_always_unregisters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=2,
        client_capacity=2,
    )

    class RequestStub:
        app = SimpleNamespace(state=SimpleNamespace(event_broker=broker))

        async def is_disconnected(self) -> bool:
            return False

    monkeypatch.setattr(event_routes, "SSE_HEARTBEAT_SECONDS", 0.01)
    response = await event_routes.stream_events(RequestStub(), last_event_id=None)
    iterator = response.body_iterator
    heartbeat = await anext(iterator)
    await iterator.aclose()
    await asyncio.sleep(0)

    assert heartbeat == ": heartbeat\n\n"
    assert broker.client_count == 0
    assert broker.replay.snapshot() == ()


@pytest.mark.asyncio
async def test_lifespan_closes_broker_after_conversation_and_before_bus(tmp_path: Path) -> None:
    host = FakeRuntimeHost()
    app = create_app(settings(tmp_path))
    app.dependency_overrides[get_runtime_host] = lambda: host

    async with app.router.lifespan_context(app):

        async def close_reporting() -> None:
            host.order.append("reporting")

        async def close_python() -> None:
            host.order.append("python")

        original_conversations = app.state.conversation_service.close
        original_broker = app.state.event_broker.close

        async def close_conversations() -> None:
            host.order.append("conversations")
            await original_conversations()

        async def close_broker() -> None:
            host.order.append("broker")
            await original_broker()

        app.state.reporting_facade.close = close_reporting
        app.state.python_run_service.close = close_python
        app.state.conversation_service.close = close_conversations
        app.state.event_broker.close = close_broker

    assert host.order == ["producers", "reporting", "python", "conversations", "broker", "bus"]


@pytest.mark.asyncio
async def test_broker_close_failure_prevents_bus_teardown(tmp_path: Path) -> None:
    host = FakeRuntimeHost()
    app = create_app(settings(tmp_path))
    app.dependency_overrides[get_runtime_host] = lambda: host

    with pytest.raises(RuntimeError, match="broker close failed"):
        async with app.router.lifespan_context(app):

            async def fail_close() -> None:
                host.order.append("broker")
                raise RuntimeError("broker close failed")

            app.state.event_broker.close = fail_close

    assert "broker" in host.order
    assert "bus" not in host.order
