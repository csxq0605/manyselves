"""Agent command and query DTOs."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SendMessageRequest(_Strict):
    content: str = Field(min_length=1)
    source: Literal["user", "main_agent"] = "user"
    message_id: str | None = Field(default=None, alias="messageId")


class EditResendRequest(_Strict):
    content: str = Field(min_length=1)
    message_id: str | None = Field(default=None, alias="messageId")


class FileContextRequest(_Strict):
    type: Literal["file", "selection"]
    file: str = Field(min_length=1)
    start_line: int | None = Field(default=None, alias="startLine", ge=1)
    end_line: int | None = Field(default=None, alias="endLine", ge=1)


class RollbackRequest(_Strict):
    checkpoint_id: str = Field(alias="checkpointId", min_length=1)
    target_message_id: str | None = Field(default=None, alias="targetMessageId")


class AcceptedCommandResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    command_id: UUID = Field(alias="commandId")
    status: Literal["accepted"] = "accepted"


class RollbackResponse(AcceptedCommandResponse):
    restored_files: int = Field(alias="restoredFiles")
    conversation_history: list[dict[str, Any]] = Field(alias="conversationHistory")


class AgentSnapshot(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str
    status: str
    session_id: str | None = Field(default=None, alias="sessionId")


class AgentListResponse(BaseModel):
    agents: list[AgentSnapshot]


class AgentDebugUpdate(_Strict):
    enabled: bool


class AgentDebugEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    model: str
    tokens_in: int = Field(alias="tokensIn")
    tokens_out: int = Field(alias="tokensOut")
    duration_ms: int = Field(alias="durationMs")
    status: str
    timestamp: str
    error: Any = None


class AgentDebugResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(alias="agentId")
    enabled: bool
    entries: list[AgentDebugEntry]
