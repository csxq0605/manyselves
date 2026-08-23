"""Typed outcomes and interactions owned by Cross-owner reporting runtime."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .agentic import (
    CrossOwnerFindingSubmission,
    CrossOwnerVerdictSubmission,
    WorkflowDecisionSubmission,
)
from .review import (
    CrossOwnerInitialReviewAcceptance,
    CrossOwnerInitialReviewPreparation,
    CrossOwnerLocalReviewAcceptance,
    CrossOwnerLocalReviewPreparation,
    CrossOwnerRecheckAcceptance,
    CrossOwnerRecheckPreparation,
    CrossOwnerRevisionAcceptance,
    CrossOwnerRevisionPreparation,
    CrossOwnerRoundProgress,
    MainExceptionDecisionAcceptance,
    MainExceptionDecisionPreparation,
)


class DeclarativeCrossOwnerInitialAgentResult(BaseModel):
    """Typed result returned by the declared initial Cross reviewer."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: CrossOwnerFindingSubmission | None = None
    error: str | None = None


class DeclarativeCrossOwnerRecheckAgentResult(BaseModel):
    """Typed result returned by the original Cross owner reviewer."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: CrossOwnerVerdictSubmission | None = None
    error: str | None = None


class DeclarativeMainExceptionAgentResult(BaseModel):
    """Typed result returned by the declared Main exception Agent."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: WorkflowDecisionSubmission | None = None
    error: str | None = None


class DeclarativeCrossOwnerRuntimeContext(BaseModel):
    """Capability-owned state threaded through one Cross owner workflow."""

    model_config = ConfigDict(extra="forbid")

    owner_module_id: str
    status: Literal[
        "initial_ready",
        "initial_resumed",
        "initial_accepted",
        "revision_ready",
        "revision_resumed",
        "revision_accepted",
        "local_review_ready",
        "local_review_resumed",
        "local_review_accepted",
        "recheck_ready",
        "recheck_resumed",
        "recheck_accepted",
        "author_exception_ready",
        "author_exception_resumed",
        "author_exception_accepted",
        "author_exception_not_required",
        "reviewer_exception_ready",
        "reviewer_exception_resumed",
        "reviewer_exception_accepted",
        "reviewer_exception_not_required",
        "round_revision_pending",
        "round_completed",
        "failed",
    ]
    preparation: CrossOwnerInitialReviewPreparation | None = None
    acceptance: CrossOwnerInitialReviewAcceptance | None = None
    revision_preparation: CrossOwnerRevisionPreparation | None = None
    revision_acceptance: CrossOwnerRevisionAcceptance | None = None
    local_review_preparation: CrossOwnerLocalReviewPreparation | None = None
    local_review_acceptance: CrossOwnerLocalReviewAcceptance | None = None
    recheck_preparation: CrossOwnerRecheckPreparation | None = None
    recheck_acceptance: CrossOwnerRecheckAcceptance | None = None
    main_preparation: MainExceptionDecisionPreparation | None = None
    main_acceptance: MainExceptionDecisionAcceptance | None = None
    round_progress: CrossOwnerRoundProgress | None = None
    error: str | None = None


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
    "DeclarativeCrossOwnerInitialAgentResult",
    "DeclarativeCrossOwnerPipelineOutcome",
    "DeclarativeCrossOwnerRecheckAgentResult",
    "DeclarativeCrossOwnerRuntimeContext",
    "DeclarativeMainExceptionUserInput",
    "DeclarativeMainExceptionAgentResult",
]
