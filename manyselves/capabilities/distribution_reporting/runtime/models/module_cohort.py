"""Typed outcome reduced after one distribution-reporting module Lane."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import ModuleSubmission


class DeclarativeModuleLaneOutcome(BaseModel):
    """Business outcome joined after every sibling branch has drained."""

    model_config = ConfigDict(extra="forbid")

    module_id: str
    status: Literal["completed", "deferred", "failed"]
    module: ModuleSubmission | None = None
    error: str | None = None
    lane_state: dict[str, Any] | None = None
    completion_ref: str | None = None
    completion: dict[str, Any] | None = None
    retry_requested: bool = False


__all__ = ["DeclarativeModuleLaneOutcome"]
