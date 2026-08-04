"""Event broker backpressure, reconnect, SSE, and lifecycle contracts."""

from __future__ import annotations

import asyncio
import json
import traceback
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from manyselves.core.loops.bus import MessageBus
from manyselves.interfaces.types import (
    AgentResponse,
    Error,
    Message,
    PeerQueryMessage,
    PeerReplyMessage,
    ProgressNoteMessage,
    SystemNotice,
    ToolCallMessage,
    ToolResult,
)
from manyselves.webapi import lifespan as lifespan_module
from manyselves.webapi.dependencies import get_runtime_host
from manyselves.webapi.errors import ApiError
from manyselves.webapi.events import broker as broker_module
from manyselves.webapi.events.broker import EventBroker
from manyselves.webapi.events.identity import ToolIdentityNormalizer
from manyselves.webapi.events.mapper import EventContext
from manyselves.webapi.events.models import EventEnvelope
from manyselves.webapi.main import create_app
from manyselves.webapi.routes import events as event_routes
from manyselves.webapi.settings import WebSettings
from tests.webapi.auth_helpers import login

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
        streamId="test-stream",
        eventId=f"test-stream:evt-{sequence}",
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
        event_id=f"test-stream:evt-{sequence}",
        stream_id="test-stream",
        sequence=sequence,
        project_id="p1",
        session_id="session-1",
        agent_id=str(getattr(message, "agent_type", "main")),
        run_id=None,
        message_id=getattr(message, "message_id", None),
    )


@pytest.mark.asyncio
async def test_sensitive_payload_is_absent_from_live_and_replayed_events() -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=4,
        client_capacity=4,
    )
    live = await broker.register(None)
    await broker.publish_internal(
        Error(
            source="provider",
            message="failed",
            details={
                "authenticationConfig": {"opaque": "auth-secret-123"},
                "token_usage": {"mystery": "usage-secret-123", "input_tokens": 1},
            },
        )
    )
    live_event = await live.get()
    replayed = await broker.register("test-stream:evt-0")
    replay_event = await replayed.get()

    for event in (live_event, replay_event):
        wire = event.to_json()
        assert "auth-secret-123" not in wire
        assert "usage-secret-123" not in wire
        assert '"input_tokens":1' in wire

    await broker.close()


@pytest.mark.asyncio
async def test_broker_correlates_legacy_tool_call_and_result_once_at_its_boundary() -> None:
    """A legacy result without an ID must follow its matching legacy call."""
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=4,
        client_capacity=4,
        stream_id="fixed-stream",
    )

    await broker.publish_internal(
        ToolCallMessage(agent_type="main", tool_name="read", arguments={"path": "a"})
    )
    await broker.publish_internal(
        ToolResult(agent_type="main", tool_name="read", result="ok")
    )

    events = broker.replay.snapshot()
    call_id = events[0].payload["toolCallId"]
    assert call_id
    assert events[1].payload["toolCallId"] == call_id


@pytest.mark.asyncio
async def test_broker_keeps_same_name_legacy_calls_distinct_and_fifo_correlated() -> None:
    """Collapsing same-name calls would attach a completion to the wrong invocation."""
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=8,
        client_capacity=4,
        stream_id="fixed-stream",
    )

    await broker.publish_internal(
        ToolCallMessage(agent_type="main", tool_name="read", arguments={"path": "a"})
    )
    await broker.publish_internal(
        ToolCallMessage(agent_type="main", tool_name="read", arguments={"path": "b"})
    )
    await broker.publish_internal(ToolResult(agent_type="main", tool_name="read", result="a"))
    await broker.publish_internal(ToolResult(agent_type="main", tool_name="read", result="b"))

    events = broker.replay.snapshot()
    assert events[0].payload["toolCallId"] != events[1].payload["toolCallId"]
    assert events[2].payload["toolCallId"] == events[0].payload["toolCallId"]
    assert events[3].payload["toolCallId"] == events[1].payload["toolCallId"]


@pytest.mark.asyncio
async def test_broker_preserves_explicit_tool_ids_and_marks_unmatched_legacy_results_orphaned() -> None:
    """Replacing provider IDs or matching another tool's result would corrupt recovery."""
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=8,
        client_capacity=4,
        stream_id="fixed-stream",
    )

    await broker.publish_internal(
        ToolCallMessage(
            agent_type="main",
            tool_name="read",
            tool_call_id="provider-call-1",
            arguments={},
        )
    )
    await broker.publish_internal(
        ToolResult(
            agent_type="main",
            tool_name="read",
            tool_call_id="provider-call-1",
            result="ok",
        )
    )
    await broker.publish_internal(ToolResult(agent_type="main", tool_name="write", result="orphan"))

    events = broker.replay.snapshot()
    assert events[0].payload["toolCallId"] == "provider-call-1"
    assert events[1].payload["toolCallId"] == "provider-call-1"
    assert events[2].payload["toolCallId"].startswith("legacy-orphan-")
    assert events[2].payload["toolCallId"] != "provider-call-1"


def test_tool_identity_capacity_evicts_globally_across_agent_and_tool_keys() -> None:
    """Per-key limits would let old calls survive an unbounded stream of new keys."""
    normalizer = ToolIdentityNormalizer(outstanding_capacity=2)
    normalizer.normalize(
        ToolCallMessage(
            agent_type="main",
            tool_name="read",
            tool_call_id="call-1",
            arguments={},
        )
    )
    normalizer.normalize(
        ToolCallMessage(
            agent_type="researcher",
            tool_name="read",
            tool_call_id="call-2",
            arguments={},
        )
    )
    normalizer.normalize(
        ToolCallMessage(
            agent_type="main",
            tool_name="write",
            tool_call_id="call-3",
            arguments={},
        )
    )

    evicted_result = normalizer.normalize(
        ToolResult(agent_type="main", tool_name="read", result="late")
    )
    second_result = normalizer.normalize(
        ToolResult(agent_type="researcher", tool_name="read", result="second")
    )
    third_result = normalizer.normalize(
        ToolResult(agent_type="main", tool_name="write", result="third")
    )

    assert evicted_result.tool_call_id.startswith("legacy-orphan-")
    assert evicted_result.tool_call_id != "call-1"
    assert second_result.tool_call_id == "call-2"
    assert third_result.tool_call_id == "call-3"


@pytest.mark.asyncio
async def test_stream_epoch_rejects_an_old_process_cursor_without_collision() -> None:
    """A low sequence from a previous process must force exactly one resync."""
    first = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=4,
        client_capacity=4,
        stream_id="boot-a",
    )
    await first.publish_internal(SystemNotice(agent_type="main", content="old"))
    old_cursor = (await first.register("boot-a:evt-0"))._events[0].event_id

    second = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=4,
        client_capacity=4,
        stream_id="boot-b",
    )
    await second.publish_internal(SystemNotice(agent_type="main", content="new"))
    client = await second.register(old_cursor)

    event = await client.get()
    assert event.type == "stream.resync_required"
    assert event.stream_id == "boot-b"
    assert event.event_id == "boot-b:evt-1"
    assert event.payload["reason"] == "epoch_mismatch"
    with pytest.raises(broker_module.EventClientClosed):
        await client.get()


@pytest.mark.asyncio
async def test_legacy_cursor_forces_exactly_one_malformed_resync() -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=4,
        client_capacity=4,
        stream_id="boot-b",
    )
    await broker.publish_internal(SystemNotice(agent_type="main", content="new"))

    client = await broker.register("evt-1")
    event = await client.get()

    assert event.type == "stream.resync_required"
    assert event.payload["reason"] == "malformed"
    with pytest.raises(broker_module.EventClientClosed):
        await client.get()


@pytest.mark.asyncio
async def test_same_epoch_cursor_replays_in_order() -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=4,
        client_capacity=4,
        stream_id="fixed-epoch",
    )
    await broker.publish_internal(SystemNotice(agent_type="main", content="one"))
    await broker.publish_internal(SystemNotice(agent_type="main", content="two"))
    replay = await broker.register("fixed-epoch:evt-1")
    assert (await replay.get()).event_id == "fixed-epoch:evt-2"


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

    assert [(await first.get()).event_id, (await first.get()).event_id] == [
        "test-stream:evt-1",
        "test-stream:evt-2",
    ]
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
async def test_full_queue_coalesces_the_matching_tail_delta_not_an_earlier_match() -> None:
    """A same-message delta earlier in the queue must not hide a safe matching tail delta."""
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=10,
        client_capacity=3,
    )
    client = await broker.register(None)
    await broker.publish_internal(
        AgentResponse(agent_type="main", message_id="message-1", content="a", streaming=True)
    )
    await broker.publish_internal(SystemNotice(agent_type="main", content="between"))
    await broker.publish_internal(
        AgentResponse(agent_type="main", message_id="message-1", content="c", streaming=True)
    )
    await broker.publish_internal(
        AgentResponse(agent_type="main", message_id="message-1", content="d", streaming=True)
    )

    delivered = [
        await asyncio.wait_for(client.get(), timeout=0.1),
        await asyncio.wait_for(client.get(), timeout=0.1),
        await asyncio.wait_for(client.get(), timeout=0.1),
    ]

    assert [item.sequence for item in delivered] == [1, 2, 4]
    assert delivered[-1].payload["content"] == "cd"
    assert delivered[-1].payload["delivery"] == {
        "coalescedCount": 2,
        "fromSequence": 3,
        "toSequence": 4,
    }


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
async def test_closed_client_raises_after_its_resync_is_consumed_without_pending_wait() -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=4,
        client_capacity=1,
    )
    client = await broker.register(None)
    await broker.publish_internal(SystemNotice(agent_type="main", content="one"))
    await broker.publish_internal(SystemNotice(agent_type="main", content="two"))

    assert (await client.get()).type == "stream.resync_required"
    with pytest.raises(broker_module.EventClientClosed):
        await asyncio.wait_for(client.get(), timeout=0.05)


@pytest.mark.asyncio
async def test_client_close_is_idempotent_and_preserves_first_resync_reason() -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=4,
        client_capacity=1,
    )
    client = await broker.register(None)
    await broker.publish_internal(SystemNotice(agent_type="main", content="one"))
    await broker.publish_internal(SystemNotice(agent_type="main", content="two"))
    await broker.close()

    event = await client.get()
    assert event.payload["reason"] == "client_overflow"


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

    replayed = await broker.register("test-stream:evt-2")
    stale = await broker.register("test-stream:evt-1")

    assert (await replayed.get()).event_id == "test-stream:evt-3"
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


class LegacyFakeRuntimeHost:
    """A pre-split host whose single stop boundary still owns the bus."""

    def __init__(self) -> None:
        self.is_ready = False
        self.workspace: Path | None = None
        self.bus = MessageBus()
        self.stop_count = 0
        self.session_id = "session-1"
        self.loop_manager = SimpleNamespace(
            get_all_agent_statuses=lambda: {"main": "idle"},
            get_agent_session_id=lambda agent_id: self.session_id,
        )

    async def start(self, workspace: Path) -> None:
        self.workspace = workspace
        self.is_ready = True

    async def stop(self) -> None:
        self.stop_count += 1
        self.is_ready = False


def message_subscriber_count(bus: MessageBus) -> int:
    return len(bus._subscribers.get(Message, ()))  # noqa: SLF001


def settings(tmp_path: Path, **updates: object) -> WebSettings:
    values = {
        "data_root": tmp_path,
        "initial_project_id": "p1",
        "sse_replay_capacity": 2,
        "sse_client_queue_capacity": 2,
    }
    values.update(updates)
    return WebSettings(**values)


@pytest.mark.asyncio
async def test_events_endpoint_requires_session_authentication(tmp_path: Path) -> None:
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
        stream_id = app.state.event_broker.stream_id
        for number in range(1, 4):
            await app.state.event_broker.publish_internal(
                SystemNotice(agent_type="main", content=str(number))
            )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await login(client)
            response = await client.get(
                "/api/v1/events",
                headers={
                    "Last-Event-ID": f"{stream_id}:evt-1",
                },
            )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    lines = response.text.splitlines()
    assert lines[0].startswith(f"id: {stream_id}:evt-")
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

    response = await event_routes.stream_events(
        RequestStub(), last_event_id="test-stream:evt-1"
    )
    iterator = response.body_iterator
    frame = await anext(iterator)
    await iterator.aclose()

    assert frame.startswith("id: test-stream:evt-2\nevent: system.notice\ndata: {")
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
async def test_reporting_workflow_uses_explicit_query_and_reply_sessions(tmp_path: Path) -> None:
    host = FakeRuntimeHost()
    host.session_id = None
    app = create_app(settings(tmp_path, sse_replay_capacity=4))
    app.dependency_overrides[get_runtime_host] = lambda: host

    async with app.router.lifespan_context(app):
        await app.state.event_broker.publish_internal(
            PeerQueryMessage(
                task_id="task-1",
                sender="dynamic-researcher",
                recipient="report",
                query_id="query-1",
                source_session_id="source-session",
                target_session_id="target-session",
                question="why?",
            )
        )
        await app.state.event_broker.publish_internal(
            PeerReplyMessage(
                task_id="task-1",
                sender="dynamic-researcher",
                recipient="report",
                query_id="query-1",
                target_session_id="reply-session",
                answer="because",
            )
        )
        query, reply = app.state.event_broker.replay.snapshot()

    assert query.session_id == "source-session"
    assert reply.session_id == "reply-session"


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
async def test_sse_registers_before_response_and_cleans_up_after_stream_cancel() -> None:
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

    response = await event_routes.stream_events(RequestStub(), last_event_id=None)
    assert broker.client_count == 1
    await broker.publish_internal(SystemNotice(agent_type="main", content="ready"))
    frame = await asyncio.wait_for(anext(response.body_iterator), timeout=0.2)
    await response.body_iterator.aclose()
    await asyncio.sleep(0)

    assert "event: system.notice" in frame
    assert broker.client_count == 0


@pytest.mark.asyncio
async def test_sse_body_close_before_first_iteration_unregisters_idempotently() -> None:
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

    response = await event_routes.stream_events(RequestStub(), last_event_id=None)
    assert broker.client_count == 1

    await response.body_iterator.aclose()
    await response.body_iterator.aclose()

    assert broker.client_count == 0


@pytest.mark.asyncio
async def test_sse_body_close_racing_blocked_next_ends_reader_without_pending_task() -> None:
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

    response = await event_routes.stream_events(RequestStub(), last_event_id=None)
    reader = asyncio.create_task(anext(response.body_iterator))
    await asyncio.sleep(0)

    await response.body_iterator.aclose()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(reader, timeout=0.1)
    assert broker.client_count == 0


@pytest.mark.asyncio
async def test_sse_close_during_disconnect_probe_never_starts_event_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=2,
        client_capacity=2,
    )
    probe_entered = asyncio.Event()
    probe_release = asyncio.Event()

    class RequestStub:
        app = SimpleNamespace(state=SimpleNamespace(event_broker=broker))

        async def is_disconnected(self) -> bool:
            probe_entered.set()
            await probe_release.wait()
            return False

    response = await event_routes.stream_events(RequestStub(), last_event_id=None)
    client = next(iter(broker._clients))  # noqa: SLF001
    get_calls = 0
    original_get = client.get

    async def tracked_get():
        nonlocal get_calls
        get_calls += 1
        return await original_get()

    monkeypatch.setattr(client, "get", tracked_get)
    reader = asyncio.create_task(anext(response.body_iterator))
    await probe_entered.wait()

    await response.body_iterator.aclose()
    probe_release.set()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(reader, timeout=0.1)
    assert broker.client_count == 0
    assert get_calls == 0


@pytest.mark.asyncio
async def test_sse_close_after_event_read_completes_suppresses_racing_frame(
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

    response = await event_routes.stream_events(RequestStub(), last_event_id=None)
    client = next(iter(broker._clients))  # noqa: SLF001
    read_entered = asyncio.Event()
    read_release = asyncio.Event()
    read_returning = asyncio.Event()

    async def controlled_get():
        read_entered.set()
        await read_release.wait()
        read_returning.set()
        return make_event(1)

    monkeypatch.setattr(client, "get", controlled_get)
    reader = asyncio.create_task(anext(response.body_iterator))
    await read_entered.wait()
    read_release.set()
    await read_returning.wait()

    await response.body_iterator.aclose()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(reader, timeout=0.1)
    assert broker.client_count == 0


@pytest.mark.asyncio
async def test_reader_caller_cancel_wins_over_simultaneous_owner_close() -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=2,
        client_capacity=2,
    )
    probe_entered = asyncio.Event()
    probe_release = asyncio.Event()

    class RequestStub:
        app = SimpleNamespace(state=SimpleNamespace(event_broker=broker))

        async def is_disconnected(self) -> bool:
            probe_entered.set()
            await probe_release.wait()
            return False

    response = await event_routes.stream_events(RequestStub(), last_event_id=None)
    reader = asyncio.create_task(anext(response.body_iterator))
    await probe_entered.wait()

    await response.body_iterator.aclose()
    reader.cancel()
    probe_release.set()

    with pytest.raises(asyncio.CancelledError):
        await reader
    assert reader.cancelled() is True
    assert broker.client_count == 0


@pytest.mark.asyncio
async def test_owner_close_cancels_blocked_client_read_as_eof(
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

    response = await event_routes.stream_events(RequestStub(), last_event_id=None)
    client = next(iter(broker._clients))  # noqa: SLF001
    read_entered = asyncio.Event()

    async def blocked_get():
        read_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "get", blocked_get)
    reader = asyncio.create_task(anext(response.body_iterator))
    await read_entered.wait()

    await response.body_iterator.aclose()

    with pytest.raises(StopAsyncIteration):
        await reader
    assert reader.cancelled() is False
    assert broker.client_count == 0


@pytest.mark.asyncio
async def test_aclose_caller_cancel_waits_for_unregister_then_propagates(
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

    response = await event_routes.stream_events(RequestStub(), last_event_id=None)
    unregister_entered = asyncio.Event()
    unregister_release = asyncio.Event()
    original_unregister = broker.unregister

    async def blocking_unregister(client) -> None:
        unregister_entered.set()
        await unregister_release.wait()
        await original_unregister(client)

    monkeypatch.setattr(broker, "unregister", blocking_unregister)
    closer = asyncio.create_task(response.body_iterator.aclose())
    await unregister_entered.wait()
    closer.cancel()
    unregister_release.set()

    with pytest.raises(asyncio.CancelledError):
        await closer
    assert closer.cancelled() is True
    assert broker.client_count == 0


@pytest.mark.asyncio
async def test_closed_broker_returns_stable_503_before_sse_response_starts(tmp_path: Path) -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=2,
        client_capacity=2,
    )
    broker.start()
    await broker.close()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(event_broker=broker)))

    with pytest.raises(ApiError) as captured:
        await event_routes.stream_events(request, last_event_id=None)

    assert captured.value.status_code == 503
    assert captured.value.code == "EVENT_STREAM_NOT_READY"
    assert captured.value.message == "Event stream is not ready"
    assert captured.value.retryable is True
    assert captured.value.details == {}


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


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stage", ["reporting", "python", "broker"])
async def test_staged_startup_failure_cleans_subscriptions_and_same_app_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed_stage: str,
) -> None:
    host = FakeRuntimeHost()
    app = create_app(settings(tmp_path))
    app.dependency_overrides[get_runtime_host] = lambda: host
    failed = False

    if failed_stage == "reporting":
        original = lifespan_module.ReportingFacade.from_runtime

        def create_reporting(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("reporting startup failed")
            return original(*args, **kwargs)

        monkeypatch.setattr(lifespan_module.ReportingFacade, "from_runtime", create_reporting)
    elif failed_stage == "python":
        original = lifespan_module.PythonRunService

        def create_python(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("python startup failed")
            return original(*args, **kwargs)

        monkeypatch.setattr(lifespan_module, "PythonRunService", create_python)
    else:
        original = lifespan_module.EventBroker.start

        def start_broker(broker) -> None:
            nonlocal failed
            original(broker)
            if not failed:
                failed = True
                raise RuntimeError("broker startup failed")

        monkeypatch.setattr(lifespan_module.EventBroker, "start", start_broker)

    with pytest.raises(RuntimeError, match=f"{failed_stage} startup failed"):
        async with app.router.lifespan_context(app):
            pass

    assert message_subscriber_count(host.bus) == 0
    assert app.state.lifecycle_active is False

    async with app.router.lifespan_context(app):
        assert message_subscriber_count(host.bus) == 2

    assert message_subscriber_count(host.bus) == 0


@pytest.mark.asyncio
async def test_cancelled_staged_startup_definitely_cleans_owned_subscriptions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = FakeRuntimeHost()
    app = create_app(settings(tmp_path))
    app.dependency_overrides[get_runtime_host] = lambda: host
    original = lifespan_module.EventBroker.start
    cancel_once = True

    def cancel_after_subscribe(broker) -> None:
        nonlocal cancel_once
        original(broker)
        if cancel_once:
            cancel_once = False
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            raise asyncio.CancelledError

    monkeypatch.setattr(lifespan_module.EventBroker, "start", cancel_after_subscribe)

    async def start_lifespan() -> None:
        async with app.router.lifespan_context(app):
            pass

    startup = asyncio.create_task(start_lifespan())
    with pytest.raises(asyncio.CancelledError):
        await startup

    assert message_subscriber_count(host.bus) == 0
    assert app.state.lifecycle_active is False

    async with app.router.lifespan_context(app):
        assert message_subscriber_count(host.bus) == 2


@pytest.mark.asyncio
async def test_staged_startup_preserves_original_error_when_resource_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = FakeRuntimeHost()
    app = create_app(settings(tmp_path))
    app.dependency_overrides[get_runtime_host] = lambda: host
    original_close = lifespan_module.ConversationService.close

    def fail_reporting(*args, **kwargs):
        raise RuntimeError("reporting startup failed")

    async def close_then_fail(conversations) -> None:
        await original_close(conversations)
        raise RuntimeError("conversation cleanup failed")

    monkeypatch.setattr(lifespan_module.ReportingFacade, "from_runtime", fail_reporting)
    monkeypatch.setattr(lifespan_module.ConversationService, "close", close_then_fail)

    with pytest.raises(RuntimeError, match="reporting startup failed") as captured:
        async with app.router.lifespan_context(app):
            pass

    assert str(captured.value) == "reporting startup failed"
    assert message_subscriber_count(host.bus) == 0


@pytest.mark.asyncio
async def test_startup_and_pending_cleanup_secondary_diagnostics_are_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = FakeRuntimeHost()
    app = create_app(settings(tmp_path))
    app.dependency_overrides[get_runtime_host] = lambda: host
    secret = "startup-pending-secondary-secret"
    logger_calls: list[tuple[object, ...]] = []
    original_broker_start = lifespan_module.EventBroker.start
    original_reporting_close = lifespan_module.ReportingFacade.close
    original_python_close = lifespan_module.PythonRunService.close
    startup_failed = False

    def fail_broker_once(broker) -> None:
        nonlocal startup_failed
        original_broker_start(broker)
        if not startup_failed:
            startup_failed = True
            raise RuntimeError("broker startup primary")

    async def fail_reporting_close(_reporting) -> None:
        raise RuntimeError("reporting cleanup primary")

    async def fail_python_close(_python_runs) -> None:
        raise RuntimeError(f"Python cleanup contained {secret}")

    monkeypatch.setattr(lifespan_module.EventBroker, "start", fail_broker_once)
    monkeypatch.setattr(
        lifespan_module.ReportingFacade, "close", fail_reporting_close
    )
    monkeypatch.setattr(lifespan_module.PythonRunService, "close", fail_python_close)
    monkeypatch.setattr(
        lifespan_module,
        "logger",
        SimpleNamespace(error=lambda *args, **_kwargs: logger_calls.append(args)),
    )

    with pytest.raises(RuntimeError, match="broker startup primary") as startup:
        async with app.router.lifespan_context(app):
            pass

    startup_diagnostics = "\n".join(
        [
            *getattr(startup.value, "__notes__", ()),
            "".join(traceback.format_exception(startup.value)),
            repr(logger_calls),
        ]
    )
    assert secret not in startup_diagnostics
    assert "Python cleanup failed" in startup_diagnostics

    logger_calls.clear()
    with pytest.raises(
        RuntimeError, match="Previous lifespan cleanup is incomplete"
    ) as pending:
        async with app.router.lifespan_context(app):
            pass

    pending_diagnostics = "\n".join(
        [
            *getattr(pending.value, "__notes__", ()),
            "".join(traceback.format_exception(pending.value)),
            repr(logger_calls),
        ]
    )
    assert secret not in pending_diagnostics
    assert "Python cleanup failed" in pending_diagnostics

    monkeypatch.setattr(
        lifespan_module.ReportingFacade, "close", original_reporting_close
    )
    monkeypatch.setattr(
        lifespan_module.PythonRunService, "close", original_python_close
    )
    async with app.router.lifespan_context(app):
        pass


@pytest.mark.asyncio
async def test_normal_shutdown_secondary_diagnostics_are_secret_safe(
    tmp_path: Path,
) -> None:
    host = FakeRuntimeHost()
    app = create_app(settings(tmp_path))
    app.dependency_overrides[get_runtime_host] = lambda: host
    secret = "normal-shutdown-secondary-secret"

    with pytest.raises(RuntimeError, match="reporting cleanup primary") as captured:
        async with app.router.lifespan_context(app):
            reporting = app.state.reporting_facade
            python_runs = app.state.python_run_service
            original_reporting_close = reporting.close
            original_python_close = python_runs.close

            async def fail_reporting_close() -> None:
                raise RuntimeError("reporting cleanup primary")

            async def fail_python_close() -> None:
                raise RuntimeError(f"Python cleanup contained {secret}")

            reporting.close = fail_reporting_close
            python_runs.close = fail_python_close

    diagnostics = "\n".join(
        [
            *getattr(captured.value, "__notes__", ()),
            "".join(traceback.format_exception(captured.value)),
        ]
    )
    assert secret not in diagnostics
    assert "Python cleanup failed" in diagnostics

    reporting.close = original_reporting_close
    python_runs.close = original_python_close
    async with app.router.lifespan_context(app):
        pass


@pytest.mark.asyncio
async def test_staged_startup_cleanup_supports_legacy_single_stop_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = LegacyFakeRuntimeHost()
    app = create_app(settings(tmp_path))
    app.dependency_overrides[get_runtime_host] = lambda: host

    def fail_reporting(*args, **kwargs):
        raise RuntimeError("reporting startup failed")

    monkeypatch.setattr(lifespan_module.ReportingFacade, "from_runtime", fail_reporting)

    with pytest.raises(RuntimeError, match="reporting startup failed"):
        async with app.router.lifespan_context(app):
            pass

    assert host.stop_count == 1
    assert message_subscriber_count(host.bus) == 0


@pytest.mark.asyncio
async def test_failed_startup_cleanup_retains_dependencies_and_retries_before_new_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pre-unsubscribe failure must keep the broker and bus owned for cleanup retry."""
    host = FakeRuntimeHost()
    app = create_app(settings(tmp_path))
    app.dependency_overrides[get_runtime_host] = lambda: host
    original_start = lifespan_module.EventBroker.start
    original_close = lifespan_module.ConversationService.close
    startup_failed = False
    cleanup_failed = False

    def fail_start_once(broker) -> None:
        nonlocal startup_failed
        original_start(broker)
        if not startup_failed:
            startup_failed = True
            raise RuntimeError("broker startup failed")

    async def fail_close_before_unsubscribe_once(conversations) -> None:
        nonlocal cleanup_failed
        if not cleanup_failed:
            cleanup_failed = True
            raise RuntimeError("conversation cleanup failed")
        await original_close(conversations)

    monkeypatch.setattr(lifespan_module.EventBroker, "start", fail_start_once)
    monkeypatch.setattr(
        lifespan_module.ConversationService,
        "close",
        fail_close_before_unsubscribe_once,
    )

    with pytest.raises(RuntimeError, match="broker startup failed") as captured:
        async with app.router.lifespan_context(app):
            pass

    assert str(captured.value) == "broker startup failed"
    assert "bus" not in host.order
    assert message_subscriber_count(host.bus) == 1
    assert app.state.conversation_service is not None
    assert app.state.event_broker is not None
    assert app.state.event_broker._closed is True  # noqa: SLF001

    async with app.router.lifespan_context(app):
        assert message_subscriber_count(host.bus) == 2

    assert message_subscriber_count(host.bus) == 0


@pytest.mark.asyncio
async def test_pending_cleanup_cancellation_finishes_cleanup_without_starting_new_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = FakeRuntimeHost()
    app = create_app(settings(tmp_path))
    app.dependency_overrides[get_runtime_host] = lambda: host
    original_start = host.start
    original_broker_start = lifespan_module.EventBroker.start
    original_close = lifespan_module.ConversationService.close
    start_count = 0
    broker_failed = False
    cleanup_failed = False

    async def count_start(workspace: Path) -> None:
        nonlocal start_count
        start_count += 1
        await original_start(workspace)

    def fail_broker_once(broker) -> None:
        nonlocal broker_failed
        original_broker_start(broker)
        if not broker_failed:
            broker_failed = True
            raise RuntimeError("broker startup failed")

    async def leave_pending_once(conversations) -> None:
        nonlocal cleanup_failed
        if not cleanup_failed:
            cleanup_failed = True
            raise RuntimeError("conversation cleanup failed")
        await original_close(conversations)

    monkeypatch.setattr(host, "start", count_start)
    monkeypatch.setattr(lifespan_module.EventBroker, "start", fail_broker_once)
    monkeypatch.setattr(lifespan_module.ConversationService, "close", leave_pending_once)

    with pytest.raises(RuntimeError, match="broker startup failed"):
        async with app.router.lifespan_context(app):
            pass
    assert start_count == 1

    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def blocking_close(conversations) -> None:
        cleanup_entered.set()
        await cleanup_release.wait()
        await original_close(conversations)

    monkeypatch.setattr(lifespan_module.ConversationService, "close", blocking_close)

    async def retry_lifespan() -> None:
        async with app.router.lifespan_context(app):
            pass

    retry = asyncio.create_task(retry_lifespan())
    await cleanup_entered.wait()
    retry.cancel()
    cleanup_release.set()

    with pytest.raises(asyncio.CancelledError):
        await retry

    assert retry.cancelled() is True
    assert start_count == 1
    assert app.state._lifecycle_cleanup_pending is None  # noqa: SLF001
    assert app.state.lifecycle_active is False
    assert message_subscriber_count(host.bus) == 0


@pytest.mark.asyncio
async def test_failed_startup_cleanup_grants_shutdown_before_producers_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = FakeRuntimeHost()
    app = create_app(settings(tmp_path))
    app.dependency_overrides[get_runtime_host] = lambda: host
    original_broker_start = lifespan_module.EventBroker.start
    original_begin = lifespan_module.RuntimeFacade.begin_shutdown
    broker_failed = False
    grant_failed = False
    order: list[str] = []

    def fail_broker_once(broker) -> None:
        nonlocal broker_failed
        original_broker_start(broker)
        if not broker_failed:
            broker_failed = True
            raise RuntimeError("broker startup failed")

    async def fail_grant_once(facade) -> None:
        nonlocal grant_failed
        order.append("grant")
        if not grant_failed:
            grant_failed = True
            raise RuntimeError("shutdown grant failed")
        await original_begin(facade)

    original_stop_producers = host.stop_producers

    async def stop_producers() -> None:
        order.append("producers")
        await original_stop_producers()

    monkeypatch.setattr(lifespan_module.EventBroker, "start", fail_broker_once)
    monkeypatch.setattr(lifespan_module.RuntimeFacade, "begin_shutdown", fail_grant_once)
    monkeypatch.setattr(host, "stop_producers", stop_producers)

    with pytest.raises(RuntimeError, match="broker startup failed") as captured:
        async with app.router.lifespan_context(app):
            pass

    assert str(captured.value) == "broker startup failed"
    assert order == ["grant"]
    assert "bus" not in host.order
    assert app.state.runtime_facade is not None
    assert app.state._lifecycle_cleanup_pending.facade is app.state.runtime_facade  # noqa: SLF001

    async with app.router.lifespan_context(app):
        assert order[:3] == ["grant", "grant", "producers"]

    assert message_subscriber_count(host.bus) == 0
