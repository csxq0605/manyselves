"""Typed application commands and runtime responses."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _Command(BaseModel):
    """Fields shared by every mutating runtime command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: UUID
    lease_token: str


class SendMessageCommand(_Command):
    """Send user-authored content to one runtime agent."""

    content: str = Field(min_length=1)
    agent_id: str
    message_id: str | None = None
    source: Literal["user", "main_agent"] = "user"


class EditResendCommand(SendMessageCommand):
    """Replace one durable user turn and send its edited content once."""

    target_message_id: str


class SendFileContextCommand(_Command):
    """Attach structured editor/file context to an agent."""

    file_context: dict[str, Any]
    agent_id: str


class InterruptCommand(_Command):
    """Interrupt the current operation for one agent."""

    agent_id: str


class RollbackCommand(_Command):
    """Restore one agent workspace to a checkpoint."""

    agent_id: str
    checkpoint_id: str


class AcceptedCommand(BaseModel):
    """Acknowledgement returned after a command completes successfully."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: UUID
    status: Literal["accepted"] = "accepted"


class RollbackResult(BaseModel):
    """Existing backend rollback data exposed through a typed boundary."""

    model_config = ConfigDict(extra="allow", frozen=True)

    restored_files: int
    conversation_history: list[dict[str, Any]]


class RuntimeSnapshot(BaseModel):
    """Minimal query view shared by desktop and service clients."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ready: bool
    workspace: str | None
    agent_statuses: dict[str, str]
    active_session_id: str | None
    controller_client_id: str | None
