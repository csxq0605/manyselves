from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    USER_MESSAGE = "user_message"
    AGENT_MESSAGE = "agent_message"
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    PHASE_STARTED = "phase_started"
    PHASE_COMPLETED = "phase_completed"
    PHASE_FAILED = "phase_failed"
    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    TASK_UPDATED = "task_updated"
    ARTIFACT_CREATED = "artifact_created"


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: EventType
    source: str = Field(min_length=1)
    run_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

