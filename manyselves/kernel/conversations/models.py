"""Opaque conversation identities understood by the Kernel."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ConversationMode(StrEnum):
    EPHEMERAL = "ephemeral"
    RUN = "run"
    PERSISTENT = "persistent"


class ConversationKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(min_length=1)
    value: str = Field(min_length=1)
    mode: ConversationMode


class ConversationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1)
    key: ConversationKey
    run_id: str | None = None
    external_session_id: str | None = None
