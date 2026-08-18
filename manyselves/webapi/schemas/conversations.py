"""Conversation resource DTOs."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str | None = Field(default=None, alias="projectId", min_length=1)
    name: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(default="main", alias="agentId")


class ConversationRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str | None = Field(default=None, alias="projectId", min_length=1)
    name: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(default="main", alias="agentId")


class ConversationResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    session_id: str = Field(alias="sessionId")
    project_id: str = Field(alias="projectId")
    name: str
    timestamp: str
    preview: str = ""
    active: bool


class ConversationListResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    project_id: str = Field(alias="projectId")
    conversations: list[ConversationResponse]
    active_session_id: str = Field(alias="activeSessionId")


class ConversationMessagesResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    session_id: str = Field(alias="sessionId")
    project_id: str = Field(alias="projectId")
    messages: list[dict[str, Any]]


class ConversationActiveSessionResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project_id: str = Field(alias="projectId")
    active_session_id: str = Field(alias="activeSessionId")
