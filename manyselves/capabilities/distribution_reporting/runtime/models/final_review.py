"""Typed state owned by the declarative Final review capability runtime."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from manyselves.core.reporting.agentic_models import (
    ChapterScopedFinalReviewFinding,
    ChiefChapterLaneRevisionSubmission,
    EditedReportSubmission,
    FinalChapterLaneVerdictSubmission,
    RevisionResponse,
    TaskEnvelope,
)
from manyselves.core.reporting.input_contracts import (
    ChiefChapterLaneInput,
    FinalChapterLaneInput,
)


class DeclarativeFinalVerdictRecord(BaseModel):
    """One persisted Final recheck verdict retained across declared rounds."""

    model_config = ConfigDict(extra="forbid")

    submission: FinalChapterLaneVerdictSubmission
    output_ref: str


class DeclarativeFinalReviewContext(BaseModel):
    """Serializable business state threaded through Final review rounds."""

    model_config = ConfigDict(extra="forbid")

    state: dict[str, Any]
    current: EditedReportSubmission
    subject_ref: str
    findings_by_chapter: dict[str, list[ChapterScopedFinalReviewFinding]]
    pending_by_chapter: dict[str, list[ChapterScopedFinalReviewFinding]]
    initial_lane_refs: dict[str, str]
    initial_residual_risks: list[str] = Field(default_factory=list)
    revision_responses: dict[str, list[RevisionResponse]] = Field(default_factory=dict)
    verdict_history: list[DeclarativeFinalVerdictRecord] = Field(default_factory=list)
    latest_verdict_refs: dict[str, str] = Field(default_factory=dict)
    revision_number: int = 0
    already_completed: bool = False


class DeclarativeFinalChiefRevisionAgentResult(BaseModel):
    """Typed result from one affected Chief revision Agent invocation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: ChiefChapterLaneRevisionSubmission | None = None
    error: str | None = None


class DeclarativeFinalChiefRevisionContext(BaseModel):
    """Prepared or accepted state for one affected Chief revision branch."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal["ready", "resumed", "accepted", "skipped", "failed"]
    contract: ChiefChapterLaneInput | None = None
    input_ref: str | None = None
    envelope: TaskEnvelope | None = None
    submission: ChiefChapterLaneRevisionSubmission | None = None
    output_ref: str | None = None
    parts: dict[str, str] = Field(default_factory=dict)
    error: str | None = None


class DeclarativeFinalChiefRevisionOutcome(BaseModel):
    """Terminal result from one drained Chief revision branch."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal["completed", "skipped", "failed"]
    submission: ChiefChapterLaneRevisionSubmission | None = None
    output_ref: str | None = None
    parts: dict[str, str] = Field(default_factory=dict)
    error: str | None = None


class DeclarativeFinalRecheckAgentResult(BaseModel):
    """Typed result from one affected Final recheck Agent invocation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: FinalChapterLaneVerdictSubmission | None = None
    error: str | None = None


class DeclarativeFinalRecheckContext(BaseModel):
    """Prepared or accepted state for one affected Final recheck branch."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal["ready", "resumed", "accepted", "skipped", "failed"]
    contract: FinalChapterLaneInput | None = None
    input_ref: str | None = None
    envelope: TaskEnvelope | None = None
    submission: FinalChapterLaneVerdictSubmission | None = None
    output_ref: str | None = None
    error: str | None = None


class DeclarativeFinalRecheckOutcome(BaseModel):
    """Terminal result from one drained Final recheck branch."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal["completed", "skipped", "failed"]
    submission: FinalChapterLaneVerdictSubmission | None = None
    output_ref: str | None = None
    error: str | None = None


__all__ = [
    "DeclarativeFinalChiefRevisionAgentResult",
    "DeclarativeFinalChiefRevisionContext",
    "DeclarativeFinalChiefRevisionOutcome",
    "DeclarativeFinalRecheckAgentResult",
    "DeclarativeFinalRecheckContext",
    "DeclarativeFinalRecheckOutcome",
    "DeclarativeFinalReviewContext",
    "DeclarativeFinalVerdictRecord",
]
