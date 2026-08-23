"""Typed outcomes and interactions owned by Cross-owner reporting runtime."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class DeclarativeCrossOwnerPipelineOutcome(BaseModel):
    """Serializable result retained after one Cross owner branch drains."""

    model_config = ConfigDict(extra="forbid")

    owner_module_id: str
    status: Literal["completed", "failed"]
    pipeline: dict[str, Any] | None = None
    error: str | None = None


class DeclarativeMainExceptionUserInput(BaseModel):
    """Capability-owned decision supplied through the generic Interaction."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept_dispute", "return_to_author", "stop_incomplete"]
    rationale: str


__all__ = [
    "DeclarativeCrossOwnerPipelineOutcome",
    "DeclarativeMainExceptionUserInput",
]
