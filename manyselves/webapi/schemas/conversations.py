"""Conversation resource DTOs."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(default="main", alias="agentId")


class ConversationRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(default="main", alias="agentId")


class ConversationResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    session_id: str = Field(alias="sessionId")
    name: str
    timestamp: str
    preview: str = ""
    active: bool


class ConversationListResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    conversations: list[ConversationResponse]
    active_session_id: str = Field(alias="activeSessionId")


class ConversationMessagesResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    session_id: str = Field(alias="sessionId")
    messages: list[dict[str, Any]]
