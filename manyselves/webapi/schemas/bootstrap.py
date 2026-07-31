"""Bootstrap snapshot response shapes."""

from typing import Any

from pydantic import BaseModel

from ...application.models import RuntimeSnapshot


class ProjectSnapshot(BaseModel):
    """The project selected for the process-local runtime."""

    id: str
    path: str


class BootstrapSettings(BaseModel):
    """Non-sensitive client configuration required during initial load."""

    sse_replay_capacity: int
    sse_client_queue_capacity: int
    control_lease_seconds: int


class BootstrapSnapshot(BaseModel):
    """One read of the client-visible runtime state."""

    runtime: RuntimeSnapshot
    project: ProjectSnapshot
    conversations: list[dict[str, Any]]
    agents: dict[str, str]
    settings: BootstrapSettings
