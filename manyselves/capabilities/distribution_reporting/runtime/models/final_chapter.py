"""Typed state owned by the declarative Final chapter capability runtime."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from manyselves.core.reporting.agentic_models import (
    FinalChapterLaneFindingSubmission,
    TaskEnvelope,
)
from manyselves.core.reporting.input_contracts import FinalChapterLaneInput


class DeclarativeFinalChapterAgentResult(BaseModel):
    """Typed adapter result that keeps one failed Auditor inside its branch."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: FinalChapterLaneFindingSubmission | None = None
    error: str | None = None


class DeclarativeFinalChapterContext(BaseModel):
    """Serializable initial Final chapter preparation and result."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal["ready", "resumed", "accepted", "skipped", "failed"]
    contract: FinalChapterLaneInput | None = None
    input_ref: str | None = None
    envelope: TaskEnvelope | None = None
    submission: FinalChapterLaneFindingSubmission | None = None
    output_ref: str | None = None
    error: str | None = None


class DeclarativeFinalChapterOutcome(BaseModel):
    """Serializable terminal result joined after the initial Final wave."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal["completed", "skipped", "failed"]
    submission: FinalChapterLaneFindingSubmission | None = None
    output_ref: str | None = None
    error: str | None = None


__all__ = [
    "DeclarativeFinalChapterAgentResult",
    "DeclarativeFinalChapterContext",
    "DeclarativeFinalChapterOutcome",
]
