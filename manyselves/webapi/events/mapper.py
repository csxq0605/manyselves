"""Explicit translation from internal Message models to stable public events."""

from __future__ import annotations

from dataclasses import dataclass

from ...interfaces.types import (
    AgentResponse,
    AgentResultMessage,
    ApiDebugMessage,
    BlockedNoticeMessage,
    Checkpoint,
    ConfigChange,
    Error,
    FileRollbackRequest,
    Message,
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
from .models import EventEnvelope
from .sanitizer import EventPayloadSanitizer


class UnsupportedMessageTypeError(TypeError):
    """Raised when a new internal message lacks an intentional public contract."""


@dataclass(frozen=True, slots=True)
class EventContext:
    """Identifiers resolved from live application state at processing time."""

    event_id: str
    stream_id: str
    sequence: int
    project_id: str | None
    session_id: str | None
    agent_id: str | None
    run_id: str | None
    message_id: str | None


class EventMapper:
    """Map every current concrete Message class without silent fallback behavior."""

    def __init__(self, sanitizer: EventPayloadSanitizer | None = None) -> None:
        self._sanitizer = sanitizer or EventPayloadSanitizer()

    def map(self, message: Message, *, context: EventContext) -> EventEnvelope:
        event_type = self._event_type(message)
        dumped = message.model_dump(exclude={"type", "timestamp"})
        payload = self._sanitizer.sanitize_mapping(dumped)
        if isinstance(message, (ToolCallMessage, ToolResult)):
            payload["toolCallId"] = self._sanitizer.sanitize_field(
                "toolCallId", message.tool_call_id
            )
            payload["agentId"] = self._sanitizer.sanitize_field(
                "agentId", str(message.agent_type)
            )
        elif isinstance(message, ApiDebugMessage):
            payload["agentId"] = self._sanitizer.sanitize_field(
                "agentId", str(message.agent_type)
            )
        if type(message) is ConfigChange:
            payload["old_value"] = self._sanitizer.sanitize_field(
                message.config_type, message.old_value
            )
            payload["new_value"] = self._sanitizer.sanitize_field(
                message.config_type, message.new_value
            )
        return EventEnvelope(
            streamId=context.stream_id,
            eventId=context.event_id,
            sequence=context.sequence,
            type=event_type,
            timestamp=message.timestamp,
            projectId=context.project_id,
            sessionId=context.session_id,
            agentId=context.agent_id,
            runId=context.run_id,
            messageId=context.message_id,
            payload=payload,
        )

    @staticmethod
    def _event_type(message: Message) -> str:
        """Use exact classes so a future subtype cannot inherit an accidental mapping."""
        message_type = type(message)
        fixed: dict[type[Message], str] = {
            UserMessage: "user.message.created",
            StatusChange: "agent.status.changed",
            ConfigChange: "configuration.changed",
            RestartRequest: "runtime.restart.requested",
            Checkpoint: "checkpoint.created",
            FileRollbackRequest: "rollback.requested",
            Error: "system.error",
            TaskUpdateMessage: "task.status.changed",
            QueueUpdateMessage: "queue.changed",
            ReportMessage: "report.status.changed",
            WorkflowMessage: "workflow.message",
            PeerQueryMessage: "workflow.peer.query",
            PeerReplyMessage: "workflow.peer.reply",
            ProgressNoteMessage: "workflow.progress.changed",
            ResearchNotePublishedMessage: "workflow.research_note.published",
            BlockedNoticeMessage: "workflow.blocked",
            AgentResultMessage: "workflow.agent_result.changed",
            ToolCallMessage: "tool.started",
        }
        if message_type in fixed:
            return fixed[message_type]
        if message_type is AgentResponse:
            return "agent.message.delta" if message.streaming else "agent.message.completed"
        if message_type is ToolResult:
            return "tool.failed" if message.error else "tool.completed"
        if message_type is RollbackStatus:
            return "rollback.completed" if message.success else "rollback.failed"
        if message_type is ApiDebugMessage:
            return "debug.api.failed" if message.status == "error" else "debug.api.completed"
        if message_type is SystemNotice:
            return "system.interrupted" if message.kind == "interrupt" else "system.notice"
        raise UnsupportedMessageTypeError(
            f"No stable public event mapping for {message_type.__name__}"
        )
