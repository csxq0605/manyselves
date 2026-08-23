"""Typed state passed between file-defined production module Lane actions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
    LaneCompletion,
    LaneTaskSpec,
)
from manyselves.core.reporting.review_lifecycle import (
    MainExceptionDecisionAcceptance,
    MainExceptionDecisionPreparation,
    ModuleInitialReviewAcceptance,
    ModuleInitialReviewPreparation,
    ModuleRecheckAcceptance,
    ModuleRecheckPreparation,
    ModuleRevisionPreparation,
)


class DeclarativeModuleLaneAttempt(BaseModel):
    """Serializable identity of the current module Lane attempt."""

    model_config = ConfigDict(extra="forbid")

    spec: LaneTaskSpec
    spec_ref: str
    lane_attempt_id: str
    started_at_ns: int
    attempt_ref: str


class DeclarativeModuleAuthoringPreparation(BaseModel):
    """Serializable current TaskEnvelope and same-run authoring reuse state."""

    model_config = ConfigDict(extra="forbid")

    specialist_id: str
    envelope: TaskEnvelope | None
    resumed_payload: ModuleSubmission | None = None
    revision: int
    review: bool
    checkpoint: bool


class DeclarativeModuleAuthoringAgentResult(BaseModel):
    """Typed business result returned by the current module Agent adapter."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    module: Any = None
    error: str | None = None


class DeclarativeModuleReviewPreparation(BaseModel):
    """Serializable exact initial Auditor turn prepared by Reporting."""

    model_config = ConfigDict(extra="forbid")

    envelope: TaskEnvelope | None
    reviewer_session_key: str
    prepared: ModuleInitialReviewPreparation
    acceptance: ModuleInitialReviewAcceptance | ModuleRecheckAcceptance | None = None


class DeclarativeModuleReviewAgentResult(BaseModel):
    """Typed business result returned by the current module Auditor adapter."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: ModuleReviewFindingSubmission | None = None
    error: str | None = None


class DeclarativeModuleRevisionAgentResult(BaseModel):
    """Typed business result returned by the original module Author adapter."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: ModuleRevisionSubmission | None = None
    error: str | None = None


class DeclarativeModuleRecheckAgentResult(BaseModel):
    """Typed business result returned by the original module Auditor."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: ModuleReviewVerdictSubmission | None = None
    error: str | None = None


class DeclarativeModuleRevisionPreparation(BaseModel):
    """Serializable exact original-Author revision prepared by Reporting."""

    model_config = ConfigDict(extra="forbid")

    prepared: ModuleRevisionPreparation


class DeclarativeModuleRecheckPreparation(BaseModel):
    """Serializable exact original-Auditor recheck prepared by Reporting."""

    model_config = ConfigDict(extra="forbid")

    prepared: ModuleRecheckPreparation
    submission: ModuleReviewVerdictSubmission | None = None
    acceptance: ModuleRecheckAcceptance | None = None


DeclarativeModuleRuntimeLaneStatus = Literal[
    "ready",
    "author_ready",
    "author_resumed",
    "authored",
    "review_ready",
    "review_resumed",
    "preflight_revision_pending",
    "preflight_revision_ready",
    "revision_pending",
    "revision_ready",
    "recheck_pending",
    "recheck_ready",
    "author_exception_deferred",
    "author_exception_ready",
    "author_exception_resumed",
    "author_exception_accepted",
    "reviewer_exception_deferred",
    "reviewer_exception_ready",
    "reviewer_exception_resumed",
    "reviewer_exception_accepted",
    "reviewed",
    "completed",
    "deferred",
    "failed",
]


class DeclarativeModuleRuntimeLaneContext(BaseModel):
    """Capability-owned state threaded through one file-defined module Lane."""

    model_config = ConfigDict(extra="forbid")

    module_id: str
    workflow_id: str
    reporting_state: dict[str, Any]
    status: DeclarativeModuleRuntimeLaneStatus
    resume_status: DeclarativeModuleRuntimeLaneStatus | None = None
    attempt: DeclarativeModuleLaneAttempt | None = None
    authoring: DeclarativeModuleAuthoringPreparation | None = None
    review: DeclarativeModuleReviewPreparation | None = None
    revision: DeclarativeModuleRevisionPreparation | None = None
    recheck: DeclarativeModuleRecheckPreparation | None = None
    main_preparation: MainExceptionDecisionPreparation | None = None
    main_acceptance: MainExceptionDecisionAcceptance | None = None
    module: ModuleSubmission | None = None
    completion_ref: str | None = None
    completion: LaneCompletion | None = None
    error: str | None = None


__all__ = [
    "DeclarativeModuleAuthoringAgentResult",
    "DeclarativeModuleAuthoringPreparation",
    "DeclarativeModuleLaneAttempt",
    "DeclarativeModuleReviewAgentResult",
    "DeclarativeModuleReviewPreparation",
    "DeclarativeModuleRecheckAgentResult",
    "DeclarativeModuleRecheckPreparation",
    "DeclarativeModuleRevisionAgentResult",
    "DeclarativeModuleRevisionPreparation",
    "DeclarativeModuleRuntimeLaneContext",
]
