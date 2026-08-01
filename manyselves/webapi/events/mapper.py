"""Explicit translation from internal Message models to stable public events."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, SecretBytes, SecretStr

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


class UnsupportedMessageTypeError(TypeError):
    """Raised when a new internal message lacks an intentional public contract."""


@dataclass(frozen=True, slots=True)
class EventContext:
    """Identifiers resolved from live application state at processing time."""

    event_id: str
    sequence: int
    project_id: str | None
    session_id: str | None
    agent_id: str | None
    run_id: str | None
    message_id: str | None


class EventMapper:
    """Map every current concrete Message class without silent fallback behavior."""

    def map(self, message: Message, *, context: EventContext) -> EventEnvelope:
        event_type = self._event_type(message)
        payload = _json_safe(message.model_dump(exclude={"type", "timestamp"}))
        if not isinstance(payload, dict):
            raise TypeError("Mapped event payload must be an object")
        if type(message) is ConfigChange and _sensitive_key(message.config_type):
            payload["old_value"] = "[REDACTED]"
            payload["new_value"] = "[REDACTED]"
        return EventEnvelope(
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
            WorkflowMessage: "reporting.workflow.message",
            PeerQueryMessage: "reporting.peer.query",
            PeerReplyMessage: "reporting.peer.reply",
            ProgressNoteMessage: "reporting.progress.changed",
            ResearchNotePublishedMessage: "reporting.research_note.published",
            BlockedNoticeMessage: "reporting.blocked",
            AgentResultMessage: "reporting.agent_result.changed",
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


_SENSITIVE_NAMES = {
    "apikey",
    "accesstoken",
    "refreshtoken",
    "authorization",
    "password",
    "secret",
    "credential",
    "credentials",
    "cookie",
    "privatekey",
    "secretaccesskey",
    "authorizationheader",
    "clientkey",
    "accesskeyid",
    "apikeyvalue",
}
_SENSITIVE_SUFFIXES = (
    "apikey",
    "password",
    "secret",
    "token",
    "credential",
    "credentials",
    "cookie",
    "privatekey",
)
_SENSITIVE_STEMS = (
    "secret",
    "password",
    "credential",
    "authorization",
    "privatekey",
    "accesskey",
    "apikey",
    "clientkey",
    "bearer",
    "token",
)
_NON_SECRET_COUNTERS = {
    "cachedtokens",
    "completiontokens",
    "inputtokens",
    "maxtokens",
    "outputtokens",
    "prompttokens",
    "reasoningtokens",
    "tokencount",
    "tokenusage",
    "tokensin",
    "tokensout",
    "totaltokens",
}


def _sensitive_key(value: object) -> bool:
    key = "".join(character for character in str(value).casefold() if character.isalnum())
    if key in _NON_SECRET_COUNTERS:
        return False
    return (
        key in _SENSITIVE_NAMES
        or key.endswith(_SENSITIVE_SUFFIXES)
        or any(stem in key for stem in _SENSITIVE_STEMS)
    )


_CREDENTIAL_TEXT = re.compile(
    r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"
    r"|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}"
    r"|\bAKIA[A-Z0-9]{16}\b"
)


def _redact_credential_text(value: str) -> str:
    return _CREDENTIAL_TEXT.sub("[REDACTED]", value)


def _json_safe(value: Any) -> Any:
    """Recursively normalize arbitrary internal values and redact credential fields."""
    if isinstance(value, (SecretStr, SecretBytes)):
        return "[REDACTED]"
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _sensitive_key(key) else _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_json_safe(item) for item in sorted(value, key=repr)]
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _redact_credential_text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, bytes):
        return _redact_credential_text(value.decode("utf-8", errors="replace"))
    return str(value)
