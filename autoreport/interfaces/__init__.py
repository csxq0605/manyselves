"""Interface layer for GUI-backend communication."""

from .protocol import GUIAPI, BackendAPI, MessageChannel
from .types import (
    AgentResponse,
    AgentId,
    AgentStatus,
    AgentType,
    Checkpoint,
    ConfigChange,
    Error,
    Message,
    MessageType,
    RestartRequest,
    StatusChange,
    ToolCallMessage,
    ToolResult,
    UserMessage,
    normalize_agent_id,
)

__all__ = [
    "MessageType",
    "AgentType",
    "AgentId",
    "normalize_agent_id",
    "AgentStatus",
    "Message",
    "UserMessage",
    "AgentResponse",
    "ToolCallMessage",
    "ToolResult",
    "StatusChange",
    "ConfigChange",
    "RestartRequest",
    "Checkpoint",
    "Error",
    "MessageChannel",
    "BackendAPI",
    "GUIAPI",
]
