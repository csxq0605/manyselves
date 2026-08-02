"""Locked version-one wire model for runtime events."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventEnvelope(BaseModel):
    """One stable, JSON-safe event sent to React and Electron clients."""

    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    stream_id: str = Field(alias="streamId", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    event_id: str = Field(alias="eventId")
    sequence: int
    type: str
    timestamp: datetime
    project_id: str | None = Field(default=None, alias="projectId")
    session_id: str | None = Field(default=None, alias="sessionId")
    agent_id: str | None = Field(default=None, alias="agentId")
    run_id: str | None = Field(default=None, alias="runId")
    message_id: str | None = Field(default=None, alias="messageId")
    payload: dict[str, Any]

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    @field_validator("timestamp", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        """Expose one unambiguous UTC clock on every public event."""
        return value.astimezone(UTC)

    def to_json(self) -> str:
        """Serialize deterministically as the single JSON line used by SSE."""
        return json.dumps(
            self.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            separators=(",", ":"),
        )
