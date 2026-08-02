"""Public, sanitized DTOs for recoverable runtime state."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ...application.models import RuntimeSnapshot
from ..events.sanitizer import EventPayloadSanitizer


class _RuntimeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class RuntimeQueueResponse(_RuntimeResponse):
    agent_id: str
    pending_count: int
    queued_messages: list[str]


class RuntimeTaskResponse(_RuntimeResponse):
    task_id: str
    source_agent: str
    target_agent: str
    status: str
    brief: str
    blocking: bool
    session_id: str | None = None


class RuntimeToolResponse(_RuntimeResponse):
    tool_call_id: str = Field(alias="toolCallId")
    agent_id: str = Field(alias="agentId")
    name: str
    arguments: Any
    status: Literal["running", "completed", "failed"]
    result: Any = None
    error: Any = None


class RuntimeDebugResponse(_RuntimeResponse):
    agent_id: str = Field(alias="agentId")
    model: str
    tokens_in: int
    tokens_out: int
    duration_ms: int
    status: str
    timestamp: str
    error: Any = None


class RuntimeCheckpointResponse(_RuntimeResponse):
    checkpoint_id: str
    agent_id: str
    timestamp: str
    epoch: int
    description: str
    source: str
    message_id: str | None = None


class RuntimeSnapshotResponse(_RuntimeResponse):
    ready: bool
    workspace: str | None
    agent_statuses: dict[str, str]
    active_session_id: str | None
    controller_client_id: str | None
    queues: list[RuntimeQueueResponse]
    tasks: list[RuntimeTaskResponse]
    tools: list[RuntimeToolResponse]
    debug: list[RuntimeDebugResponse]
    checkpoints: list[RuntimeCheckpointResponse]

    @classmethod
    def from_runtime(cls, snapshot: RuntimeSnapshot) -> "RuntimeSnapshotResponse":
        """Copy a runtime snapshot into the public schema without mutating it."""
        sanitizer = EventPayloadSanitizer()
        return cls(
            ready=snapshot.ready,
            workspace=snapshot.workspace,
            agent_statuses=dict(snapshot.agent_statuses),
            active_session_id=snapshot.active_session_id,
            controller_client_id=snapshot.controller_client_id,
            queues=[
                RuntimeQueueResponse(
                    agent_id=item.agent_id,
                    pending_count=item.pending_count,
                    queued_messages=list(item.queued_messages),
                )
                for item in snapshot.queues
            ],
            tasks=[
                RuntimeTaskResponse(
                    task_id=item.task_id,
                    source_agent=item.source_agent,
                    target_agent=item.target_agent,
                    status=item.status,
                    brief=item.brief,
                    blocking=item.blocking,
                    session_id=item.session_id,
                )
                for item in snapshot.tasks
            ],
            tools=[
                RuntimeToolResponse(
                    toolCallId=item.tool_call_id,
                    agentId=item.agent_id,
                    name=item.name,
                    arguments=sanitizer.sanitize_field("arguments", item.arguments),
                    status=item.status,
                    result=sanitizer.sanitize_field("result", item.result),
                    error=sanitizer.sanitize_field("error", item.error),
                )
                for item in snapshot.tools
            ],
            debug=[
                RuntimeDebugResponse(
                    agentId=item.agent_id,
                    model=item.model,
                    tokens_in=item.tokens_in,
                    tokens_out=item.tokens_out,
                    duration_ms=item.duration_ms,
                    status=item.status,
                    timestamp=item.timestamp.isoformat(),
                    error=sanitizer.sanitize_field("error", item.error),
                )
                for item in snapshot.debug
            ],
            checkpoints=[
                RuntimeCheckpointResponse(
                    checkpoint_id=item.checkpoint_id,
                    agent_id=item.agent_id,
                    timestamp=item.timestamp,
                    epoch=item.epoch,
                    description=item.description,
                    source=item.source,
                    message_id=item.message_id,
                )
                for item in snapshot.checkpoints
            ],
        )
