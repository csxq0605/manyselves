"""Business-neutral tool invocation port and outcome."""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class ToolInvocationOutcome(BaseModel):
    status: Literal["ok", "correction", "failed", "blocked"] = "ok"
    result: Any = None
    error: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    reused: bool = False


class ToolInvoker(Protocol):
    async def invoke(
        self,
        arguments: Any,
        *,
        task_id: str,
    ) -> ToolInvocationOutcome: ...
