"""Typed state owned by the declarative Chief chapter capability runtime."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ChiefChapterLaneSubmission,
    TaskEnvelope,
)
from manyselves.core.reporting.input_contracts import ChiefChapterLaneInput


class DeclarativeChiefChapterAgentResult(BaseModel):
    """Typed adapter result that keeps one failed Agent inside its branch."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: ChiefChapterLaneSubmission | None = None
    error: str | None = None


class DeclarativeChiefChapterContext(BaseModel):
    """Serializable preparation and result for one declared Chief branch."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal[
        "ready",
        "resumed",
        "accepted",
        "skipped",
        "compatibility",
        "failed",
    ]
    contract: ChiefChapterLaneInput | None = None
    input_ref: str | None = None
    envelope: TaskEnvelope | None = None
    submission: ChiefChapterLaneSubmission | None = None
    output_ref: str | None = None
    error: str | None = None


class DeclarativeChiefChapterOutcome(BaseModel):
    """Serializable terminal outcome joined after all Chief branches drain."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal["completed", "skipped", "failed"]
    submission: ChiefChapterLaneSubmission | None = None
    output_ref: str | None = None
    error: str | None = None


__all__ = [
    "DeclarativeChiefChapterAgentResult",
    "DeclarativeChiefChapterContext",
    "DeclarativeChiefChapterOutcome",
]
