"""Stable public event mapping and process-local replay contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

import pytest
from pydantic import BaseModel, SecretStr

from manyselves.interfaces import types as message_types
from manyselves.interfaces.types import (
    AgentResponse,
    AgentResultMessage,
    ApiDebugMessage,
    BlockedNoticeMessage,
    Checkpoint,
    ConfigChange,
    Error,
    FileRollbackRequest,
    Message,
    MessageType,
    PeerQueryMessage,
    PeerReplyMessage,
    ProgressNoteMessage,
    QueueUpdateMessage,
    ReportMessage,
    ResearchNotePublishedMessage,
    RestartRequest,
    RollbackStatus,
    StatusChange,
    SystemNotice,
    TaskUpdateMessage,
    ToolCallMessage,
    ToolResult,
    UserMessage,
    WorkflowMessage,
)
from manyselves.webapi.events.mapper import (
    EventContext,
    EventMapper,
    UnsupportedMessageTypeError,
)
from manyselves.webapi.events.models import EventEnvelope
from manyselves.webapi.events.replay import ReplayBuffer

NOW = datetime(2026, 7, 31, 10, 30, tzinfo=UTC)


def context(sequence: int = 1) -> EventContext:
    return EventContext(
        event_id=f"evt-{sequence}",
        sequence=sequence,
        project_id="project-1",
        session_id="session-1",
        agent_id="main",
        run_id="run-1",
        message_id="message-1",
    )


def event(sequence: int, *, event_type: str = "system.notice") -> EventEnvelope:
    return EventEnvelope(
        eventId=f"evt-{sequence}",
        sequence=sequence,
        type=event_type,
        timestamp=NOW,
        projectId="project-1",
        sessionId="session-1",
        agentId="main",
        runId=None,
        messageId=None,
        payload={"sequence": sequence},
    )


def concrete_message_types() -> set[type[Message]]:
    """Return every constructible Message class declared by the interface module."""
    return {
        value
        for value in vars(message_types).values()
        if isinstance(value, type) and issubclass(value, Message) and value is not Message
    }


MAPPER_CASES: dict[type[Message], tuple[Message, str]] = {
    UserMessage: (UserMessage(content="hello", timestamp=NOW), "user.message.created"),
    AgentResponse: (
        AgentResponse(agent_type="main", content="done", timestamp=NOW),
        "agent.message.completed",
    ),
    ToolCallMessage: (
        ToolCallMessage(agent_type="main", tool_name="read_file", arguments={}, timestamp=NOW),
        "tool.started",
    ),
    ToolResult: (
        ToolResult(agent_type="main", tool_name="read_file", result="ok", timestamp=NOW),
        "tool.completed",
    ),
    StatusChange: (
        StatusChange(agent_type="main", status="thinking", timestamp=NOW),
        "agent.status.changed",
    ),
    ConfigChange: (
        ConfigChange(config_type="model", old_value="old", new_value="new", timestamp=NOW),
        "configuration.changed",
    ),
    RestartRequest: (
        RestartRequest(reason="config_change", timestamp=NOW),
        "runtime.restart.requested",
    ),
    Checkpoint: (
        Checkpoint(agent_type="main", checkpoint_id="cp-1", description="saved", timestamp=NOW),
        "checkpoint.created",
    ),
    FileRollbackRequest: (
        FileRollbackRequest(agent_type="main", checkpoint_id="cp-1", timestamp=NOW),
        "rollback.requested",
    ),
    RollbackStatus: (
        RollbackStatus(agent_type="main", checkpoint_id="cp-1", success=True, timestamp=NOW),
        "rollback.completed",
    ),
    Error: (Error(source="system", message="failed", timestamp=NOW), "system.error"),
    TaskUpdateMessage: (
        TaskUpdateMessage(
            task_id="task-1",
            action="created",
            source_agent="main",
            target_agent="analyst",
            timestamp=NOW,
        ),
        "task.status.changed",
    ),
    QueueUpdateMessage: (
        QueueUpdateMessage(agent_type="main", queued_messages=["next"], timestamp=NOW),
        "queue.changed",
    ),
    ApiDebugMessage: (
        ApiDebugMessage(
            model="model",
            tokens_in=1,
            tokens_out=2,
            duration_ms=3,
            status="success",
            timestamp=NOW,
        ),
        "debug.api.completed",
    ),
    ReportMessage: (
        ReportMessage(agent_type="report", task_id="task-1", report_type="progress", timestamp=NOW),
        "report.status.changed",
    ),
    SystemNotice: (
        SystemNotice(agent_type="main", content="waiting", timestamp=NOW),
        "system.notice",
    ),
    WorkflowMessage: (
        WorkflowMessage(type=MessageType.REPORT, task_id="task-1", sender="report", timestamp=NOW),
        "reporting.workflow.message",
    ),
    PeerQueryMessage: (
        PeerQueryMessage(
            task_id="task-1",
            sender="report",
            recipient="analyst",
            query_id="query-1",
            source_session_id="session-1",
            question="why?",
            timestamp=NOW,
        ),
        "reporting.peer.query",
    ),
    PeerReplyMessage: (
        PeerReplyMessage(
            task_id="task-1",
            sender="analyst",
            recipient="report",
            query_id="query-1",
            target_session_id="session-1",
            answer="because",
            timestamp=NOW,
        ),
        "reporting.peer.reply",
    ),
    ProgressNoteMessage: (
        ProgressNoteMessage(task_id="task-1", sender="analyst", timestamp=NOW),
        "reporting.progress.changed",
    ),
    ResearchNotePublishedMessage: (
        ResearchNotePublishedMessage(
            task_id="task-1", sender="analyst", note_id="note-1", timestamp=NOW
        ),
        "reporting.research_note.published",
    ),
    BlockedNoticeMessage: (
        BlockedNoticeMessage(task_id="task-1", sender="analyst", reason="missing", timestamp=NOW),
        "reporting.blocked",
    ),
    AgentResultMessage: (
        AgentResultMessage(
            task_id="task-1",
            sender="analyst",
            run_id="run-1",
            result_path="Work/runs/run-1/result.md",
            timestamp=NOW,
        ),
        "reporting.agent_result.changed",
    ),
}


def test_mapper_cases_cover_every_concrete_interface_message() -> None:
    """Adding a new concrete bus message without a public mapping would hide it from clients."""
    assert set(MAPPER_CASES) == concrete_message_types()


@pytest.mark.parametrize(
    ("message", "expected_type"),
    MAPPER_CASES.values(),
    ids=[message_type.__name__ for message_type in MAPPER_CASES],
)
def test_internal_message_maps_to_stable_public_event(message: Message, expected_type: str) -> None:
    """Renaming or silently ignoring a mapped event would break React reconnect behavior."""
    mapped = EventMapper().map(message, context=context())

    assert mapped.type == expected_type
    assert mapped.schema_version == 1
    assert mapped.timestamp == NOW


@pytest.mark.parametrize(
    ("message", "expected_type"),
    [
        (AgentResponse(agent_type="main", content="part", streaming=True), "agent.message.delta"),
        (ToolResult(agent_type="main", tool_name="run", result=None, error="boom"), "tool.failed"),
        (RollbackStatus(agent_type="main", checkpoint_id="cp-1", success=False), "rollback.failed"),
        (
            ApiDebugMessage(model="m", tokens_in=0, tokens_out=0, duration_ms=1, status="error"),
            "debug.api.failed",
        ),
        (
            SystemNotice(agent_type="main", content="Interrupted", kind="interrupt"),
            "system.interrupted",
        ),
    ],
)
def test_mapper_preserves_branch_specific_terminal_semantics(
    message: Message, expected_type: str
) -> None:
    """Collapsing deltas, failures, and interrupts into generic events would confuse UI state."""
    assert EventMapper().map(message, context=context()).type == expected_type


class ExampleEnum(str, Enum):
    VALUE = "enum-value"


class ExampleModel(BaseModel):
    path: Path


def test_event_payload_is_json_safe_deterministic_and_secret_redacted(tmp_path: Path) -> None:
    """Non-native values or provider credentials must not break or leak through SSE JSON."""
    raw_secret = "provider-secret-value"
    message = ConfigChange(
        config_type="provider",
        old_value={"api_key": raw_secret, "when": NOW, "kind": ExampleEnum.VALUE},
        new_value={
            "credential": SecretStr(raw_secret),
            "auth": {
                "providerToken": raw_secret,
                "nested": {
                    "bearerToken": raw_secret,
                    "privateKey": raw_secret,
                },
            },
            "model": ExampleModel(path=tmp_path / "artifact.txt"),
        },
        timestamp=NOW,
    )

    mapped = EventMapper().map(message, context=context())
    first = mapped.to_json()
    second = mapped.to_json()
    decoded = json.loads(first)

    assert first == second
    assert "\n" not in first
    assert raw_secret not in first
    assert decoded["payload"]["old_value"]["api_key"] == "[REDACTED]"
    assert decoded["payload"]["new_value"]["credential"] == "[REDACTED]"
    assert decoded["payload"]["new_value"]["auth"]["providerToken"] == "[REDACTED]"
    assert decoded["payload"]["new_value"]["auth"]["nested"]["bearerToken"] == "[REDACTED]"
    assert decoded["payload"]["new_value"]["auth"]["nested"]["privateKey"] == "[REDACTED]"
    assert decoded["payload"]["old_value"]["when"] == "2026-07-31T10:30:00Z"
    assert decoded["payload"]["old_value"]["kind"] == "enum-value"
    assert decoded["payload"]["new_value"]["model"]["path"] == str(tmp_path / "artifact.txt")


def test_sensitive_config_change_redacts_scalar_old_and_new_values() -> None:
    """Credential rotations often carry scalar secrets rather than keyed nested mappings."""
    mapped = EventMapper().map(
        ConfigChange(
            config_type="providerToken",
            old_value="old-provider-secret",
            new_value="new-provider-secret",
            timestamp=NOW,
        ),
        context=context(),
    )

    assert mapped.payload["old_value"] == "[REDACTED]"
    assert mapped.payload["new_value"] == "[REDACTED]"
    assert "provider-secret" not in mapped.to_json()


def test_event_envelope_uses_locked_aliases_and_explicit_context() -> None:
    """Snake-case wire fields or stale inferred identifiers would violate the frontend contract."""
    mapped = EventMapper().map(
        AgentResponse(agent_type="other", content="done", message_id="internal-id", timestamp=NOW),
        context=context(sequence=7),
    )
    body = json.loads(mapped.to_json())

    assert list(body) == [
        "schemaVersion",
        "eventId",
        "sequence",
        "type",
        "timestamp",
        "projectId",
        "sessionId",
        "agentId",
        "runId",
        "messageId",
        "payload",
    ]
    assert body | {"payload": None} == {
        "schemaVersion": 1,
        "eventId": "evt-7",
        "sequence": 7,
        "type": "agent.message.completed",
        "timestamp": "2026-07-31T10:30:00Z",
        "projectId": "project-1",
        "sessionId": "session-1",
        "agentId": "main",
        "runId": "run-1",
        "messageId": "message-1",
        "payload": None,
    }


def test_unknown_future_message_subtype_fails_visibly() -> None:
    """A future internal message must fail mapper coverage instead of disappearing silently."""

    class FutureMessage(Message):
        type: MessageType = MessageType.SYSTEM_NOTICE

    with pytest.raises(UnsupportedMessageTypeError, match="FutureMessage"):
        EventMapper().map(FutureMessage(), context=context())


def test_replay_returns_only_events_strictly_after_a_present_cursor() -> None:
    replay = ReplayBuffer(capacity=4)
    for sequence in range(1, 4):
        replay.append(event(sequence))

    assert [item.sequence for item in replay.after("evt-1").events] == [2, 3]
    assert replay.after("evt-3").events == ()
    assert replay.after("evt-0").events == tuple(event(item) for item in range(1, 4))


@pytest.mark.parametrize(
    ("cursor", "reason"),
    [
        ("evt-1", "evicted"),
        ("evt-3", "unknown"),
        ("evt-99", "future"),
        ("not-an-event", "malformed"),
    ],
)
def test_replay_requests_resync_for_unrecoverable_cursor(cursor: str, reason: str) -> None:
    replay = ReplayBuffer(capacity=2)
    replay.append(event(2))
    replay.append(event(4))

    outcome = replay.after(cursor)

    assert outcome.events == ()
    assert outcome.requires_resync is True
    assert outcome.reason == reason


@pytest.mark.parametrize("capacity", [0, -1, True])
def test_replay_rejects_invalid_capacity(capacity: int) -> None:
    with pytest.raises(ValueError, match="capacity"):
        ReplayBuffer(capacity=capacity)


def test_replay_is_bounded_and_requires_strict_monotonic_sequence() -> None:
    replay = ReplayBuffer(capacity=2)
    replay.append(event(1))
    replay.append(event(2))
    replay.append(event(3))

    assert replay.snapshot() == (event(2), event(3))
    with pytest.raises(ValueError, match="monotonic"):
        replay.append(event(3))


def test_replay_rejects_event_id_that_disagrees_with_sequence() -> None:
    """A mismatched cursor ID would make a retained event impossible to recover exactly."""
    replay = ReplayBuffer(capacity=2)
    mismatched = event(1).model_copy(update={"event_id": "evt-99"})

    with pytest.raises(ValueError, match="event ID"):
        replay.append(mismatched)
