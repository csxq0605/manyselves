"""Typed state passed between file-defined production module Lane actions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .agentic_models import ModuleSubmission
from .parallel_runtime import LaneCompletion, LaneTaskSpec


class DeclarativeModuleLaneAttempt(BaseModel):
    """Serializable identity of the current legacy-compatible Lane attempt."""

    model_config = ConfigDict(extra="forbid")

    spec: LaneTaskSpec
    spec_ref: str
    lane_attempt_id: str
    started_at_ns: int
    attempt_ref: str


class DeclarativeModuleRuntimeLaneContext(BaseModel):
    """Capability-owned state threaded through one file-defined module Lane."""

    model_config = ConfigDict(extra="forbid")

    module_id: str
    workflow_id: str
    reporting_state: dict[str, Any]
    status: Literal[
        "ready",
        "authored",
        "reviewed",
        "completed",
        "deferred",
        "failed",
    ]
    attempt: DeclarativeModuleLaneAttempt | None = None
    module: ModuleSubmission | None = None
    completion_ref: str | None = None
    completion: LaneCompletion | None = None
    error: str | None = None


__all__ = [
    "DeclarativeModuleLaneAttempt",
    "DeclarativeModuleRuntimeLaneContext",
]
