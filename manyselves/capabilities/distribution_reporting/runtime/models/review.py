"""Typed review lifecycle state owned by Distribution Reporting."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .agentic import (
    CrossReviewFinding,
    ModuleReviewFinding,
    ModuleSubmission,
    RevisionResponse,
    StrictModel,
    TaskEnvelope,
    WorkflowDecisionSubmission,
)
from .inputs import (
    ModuleReviewInput,
    ModuleRevisionDiff,
    ModuleRevisionInput,
    ReviewCompletionRecord,
)


class ModuleReviewProgress(StrictModel):
    run_id: str
    module_id: str
    next_action: str
    current: ModuleSubmission
    pending: list[ModuleReviewFinding] = Field(default_factory=list)
    responses: list[RevisionResponse] = Field(default_factory=list)
    finding_refs: list[str] = Field(default_factory=list)
    verdict_refs: list[str] = Field(default_factory=list)
    resolved_ids: list[str] = Field(default_factory=list)
    review_round: int = 0
    phase: str = "initial"
    scope: list[str] = Field(default_factory=list)
    reviewer_session_key: str | None = None
    last_reviewed_subject_ref: str | None = None
    review_protocol_version: int = 1


class ModuleReviewPreflightProgress(StrictModel):
    """In-process state for the existing bounded machine-preflight loop."""

    current: ModuleSubmission
    attempts: int = 0
    failure_signatures: list[tuple[tuple[str, str, str], ...]] = Field(
        default_factory=list
    )


class ModuleReviewBatch(StrictModel):
    """One group of submodules reviewed in its own turn of the same Auditor.

    The batch input and result live in files; the lane context keeps only
    their refs so it does not grow with the number of batches.
    """

    index: int = Field(ge=0)
    submodule_ids: list[str] = Field(min_length=1)
    input_ref: str
    result_ref: str
    done: bool = False


class ModuleInitialReviewPreparation(StrictModel):
    """Typed, serializable boundary before one module Auditor turn.

    ``continue_existing`` is intentionally a capability result rather than a
    Kernel decision.  A runtime can use it to skip a duplicate initial Agent
    action and hand the persisted lifecycle to ``run_module_review`` with
    ``resume=True``.
    """

    mode: Literal["invoke_agent", "continue_existing", "preflight_revision"]
    run_id: str
    module_id: str
    lifecycle_id: str
    workflow_id: str
    reviewer_session_key: str
    review_root: str
    progress_ref: str
    review_round: int
    phase: str = "initial"
    scope: list[str]
    current: ModuleSubmission
    subject_ref: str | None = None
    review_input_ref: str | None = None
    review_input: ModuleReviewInput | None = None
    envelope: TaskEnvelope | None = None
    progress: ModuleReviewProgress | None = None
    validation_ref: str | None = None
    validation_target_submodule_ids: list[str] = Field(default_factory=list)
    preflight_progress: ModuleReviewPreflightProgress | None = None
    regression_context: ModuleLocalRegressionContext | None = None
    batches: list[ModuleReviewBatch] = Field(default_factory=list)
    batch_index: int | None = None
    full_review_input_ref: str | None = None
    full_envelope: TaskEnvelope | None = None

    @property
    def review_protocol_version(self) -> int:
        return self.progress.review_protocol_version if self.progress is not None else 3


class ModuleInitialReviewAcceptance(StrictModel):
    """Typed result after accepting an initial module-review submission."""

    run_id: str
    module_id: str
    lifecycle_id: str
    reviewer_session_key: str
    subject_ref: str
    current: ModuleSubmission
    findings: list[ModuleReviewFinding] = Field(default_factory=list)
    finding_refs: list[str] = Field(default_factory=list)
    verdict_refs: list[str] = Field(default_factory=list)
    resolved_ids: list[str] = Field(default_factory=list)
    next_action: Literal["revise", "completed"]
    progress_ref: str
    completion_ref: str | None = None


class ModuleRevisionPreparation(StrictModel):
    """Typed, serializable boundary before one module-author revision turn."""

    run_id: str
    module_id: str
    workflow_id: str
    specialist_id: str
    session_key: str
    subject: ModuleSubmission
    revision_input: ModuleRevisionInput
    input_ref: str
    subject_ref: str
    revision: int
    target_submodule_ids: list[str]
    required_finding_ids: list[str]
    envelope: TaskEnvelope


class ModuleRecheckPreparation(StrictModel):
    """Typed boundary before the first recheck of an accepted module finding."""

    mode: Literal["invoke_agent", "continue_existing", "preflight_revision"]
    run_id: str
    module_id: str
    lifecycle_id: str
    workflow_id: str
    reviewer_session_key: str
    review_root: str
    progress_ref: str
    review_round: int
    scope: list[str]
    current: ModuleSubmission
    pending: list[ModuleReviewFinding] = Field(default_factory=list)
    responses: list[RevisionResponse] = Field(default_factory=list)
    finding_refs: list[str] = Field(default_factory=list)
    verdict_refs: list[str] = Field(default_factory=list)
    resolved_ids: list[str] = Field(default_factory=list)
    last_reviewed_subject_ref: str | None = None
    subject_ref: str | None = None
    review_input_ref: str | None = None
    review_input: ModuleReviewInput | None = None
    envelope: TaskEnvelope | None = None
    progress: ModuleReviewProgress | None = None
    validation_ref: str | None = None
    validation_target_submodule_ids: list[str] = Field(default_factory=list)
    preflight_progress: ModuleReviewPreflightProgress | None = None
    batches: list[ModuleReviewBatch] = Field(default_factory=list)
    batch_index: int | None = None
    full_review_input_ref: str | None = None
    full_envelope: TaskEnvelope | None = None

    @property
    def review_protocol_version(self) -> int:
        return self.progress.review_protocol_version if self.progress is not None else 3


class ModuleRecheckAcceptance(StrictModel):
    """Typed result after accepting one module Auditor recheck."""

    run_id: str
    module_id: str
    lifecycle_id: str
    reviewer_session_key: str
    subject_ref: str
    current: ModuleSubmission
    findings: list[ModuleReviewFinding] = Field(default_factory=list)
    finding_refs: list[str] = Field(default_factory=list)
    verdict_refs: list[str] = Field(default_factory=list)
    resolved_ids: list[str] = Field(default_factory=list)
    next_action: Literal["continue_existing", "completed"]
    progress_ref: str
    completion_ref: str | None = None


class ModuleLocalRegressionContext(StrictModel):
    """Cross-triggered context needed for a scoped local regression review."""

    prior_review_completion_ref: str
    prior_review_completion: ReviewCompletionRecord
    baseline_subject_ref: str
    trigger_cross_findings: list[CrossReviewFinding] = Field(min_length=1)
    trigger_revision_responses: list[RevisionResponse] = Field(min_length=1)
    revision_diff_ref: str
    revision_diff: ModuleRevisionDiff


class MainExceptionDecisionPreparation(StrictModel):
    """Serializable boundary before an existing Main exception decision."""

    mode: Literal["invoke_agent", "continue_existing"]
    run_id: str
    workflow_id: str
    scope: str
    trigger: str
    exception_ids: list[str]
    input_ref: str
    decision_ref: str
    envelope: TaskEnvelope
    existing_result: WorkflowDecisionSubmission | None = None


class MainExceptionDecisionAcceptance(StrictModel):
    """Accepted Main exception decision shared by both runtime paths."""

    run_id: str
    workflow_id: str
    scope: str
    trigger: str
    decision_ref: str
    result: WorkflowDecisionSubmission


