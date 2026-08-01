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
        if type(message) is ConfigChange:
            if _sensitive_key(message.config_type, message.old_value):
                payload["old_value"] = "[REDACTED]"
            if _sensitive_key(message.config_type, message.new_value):
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
)
_TOKEN_METRIC_CONTAINERS = {
    "tokenusage",
}
_TOKEN_METRIC_SCALARS = {
    "agentcumulativeinputtokens",
    "agentcumulativeoutputtokens",
    "cachedtokens",
    "completiontokens",
    "inputtokens",
    "maxtokens",
    "maxtotaltokens",
    "outputtokens",
    "prompttokens",
    "reasoningtokens",
    "tokencount",
    "tokensin",
    "tokensout",
    "totaltokens",
    "workingmemorytokens",
}
_AUTH_METADATA_KEYS = {
    "configured",
    "description",
    "enabled",
    "method",
    "provider",
    "scheme",
    "type",
}
_AUTH_CONTAINER_TYPES = (dict, list, tuple, set, frozenset)
_AUTH_SECRET_MARKERS = (
    "authorization",
    "credential",
    "header",
    "jwt",
    "password",
    "secret",
    "token",
)
_MISSING = object()


def _normalized_key(value: object) -> str:
    if type(value) is not str:
        return ""
    return "".join(character for character in value.casefold() if character.isalnum())


def _token_metric_key(key: str) -> bool:
    return key in _TOKEN_METRIC_SCALARS


def _auth_prefixed_key(value: object) -> bool:
    if type(value) is not str:
        return False
    folded = value.casefold()
    return (
        folded in {"auth", "authentication"}
        or folded.startswith(("auth_", "auth-", "auth.", "auth:", "auth/"))
        or (folded.startswith("auth") and len(value) > 4 and value[4].isupper())
    )


def _auth_context_container(value: object, item: object) -> bool:
    key = _normalized_key(value)
    return (
        bool(key)
        and _auth_prefixed_key(value)
        and type(item) in _AUTH_CONTAINER_TYPES
        and not any(marker in key for marker in _AUTH_SECRET_MARKERS)
    )


def _sensitive_key(value: object, item: object = _MISSING) -> bool:
    key = _normalized_key(value)
    if not key:
        return True
    if "authheader" in key or "jwt" in key:
        return True
    if _auth_prefixed_key(value) and key != "authentication":
        return True
    if any(stem in key for stem in _SENSITIVE_STEMS):
        return True
    if "token" in key:
        if _token_metric_key(key) and item is None:
            return False
        numeric_metric = (
            item is not _MISSING
            and isinstance(item, (int, float))
            and not isinstance(item, bool)
            and _token_metric_key(key)
        )
        if numeric_metric:
            return False
        if key in _TOKEN_METRIC_CONTAINERS and (
            item is None or type(item) in (dict, list, tuple)
        ):
            return False
        return True
    return key in _SENSITIVE_NAMES or key.endswith(_SENSITIVE_SUFFIXES)


_CREDENTIAL_TEXT = re.compile(
    r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"
    r"|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}"
    r"|\bAKIA[A-Z0-9]{16}\b"
)


def _redact_credential_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        parts = match.group(0).split(maxsplit=1)
        if len(parts) == 2 and parts[1].casefold() in {
            "authentication",
            "authorization",
            "scheme",
        }:
            return match.group(0)
        return "[REDACTED]"

    return _CREDENTIAL_TEXT.sub(replace, value)


def _json_key(value: object) -> str:
    """Return a JSON object key without invoking arbitrary conversion hooks."""
    if type(value) is str:
        return value
    if value is None:
        return "None"
    if type(value) in (bool, int, float):
        return str(value)
    return "[REDACTED]"


def _auth_json_safe(value: object) -> Any:
    """Sanitize an authentication container using metadata-only scalar semantics."""
    if type(value) is dict:
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            output_key = _json_key(key)
            if type(item) in _AUTH_CONTAINER_TYPES:
                sanitized[output_key] = _auth_json_safe(item)
            elif _normalized_key(key) in _AUTH_METADATA_KEYS and (
                item is None or type(item) in (str, bool, int, float)
            ):
                sanitized[output_key] = (
                    _redact_credential_text(item) if type(item) is str else item
                )
            else:
                sanitized[output_key] = "[REDACTED]"
        return sanitized
    if type(value) in (list, tuple):
        return [
            _auth_json_safe(item)
            if type(item) in _AUTH_CONTAINER_TYPES
            else "[REDACTED]"
            for item in value
        ]
    if type(value) in (set, frozenset):
        sanitized_items = [
            _auth_json_safe(item)
            if type(item) in _AUTH_CONTAINER_TYPES
            else "[REDACTED]"
            for item in value
        ]
        return sorted(sanitized_items, key=repr)
    return "[REDACTED]"


def _json_safe(value: Any) -> Any:
    """Recursively normalize arbitrary internal values and redact credential fields."""
    if isinstance(value, (SecretStr, SecretBytes)):
        return "[REDACTED]"
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump(mode="python"))
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            output_key = _json_key(key)
            if _auth_context_container(key, item):
                sanitized[output_key] = _auth_json_safe(item)
            elif _sensitive_key(key, item):
                sanitized[output_key] = "[REDACTED]"
            else:
                sanitized[output_key] = _json_safe(item)
        return sanitized
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
    return _redact_credential_text(str(value))
