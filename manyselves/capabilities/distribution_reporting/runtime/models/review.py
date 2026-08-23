"""Typed review lifecycle state owned by Distribution Reporting."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ..state.parallel import CrossOwnerCompletion
from .agentic import (
    CrossOwnerFindingSubmission,
    CrossOwnerVerdictSubmission,
    CrossReviewFinding,
    EditedReportSubmission,
    FinalReviewFinding,
    ModuleReviewFinding,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
    StrictModel,
    TaskEnvelope,
    WorkflowDecisionSubmission,
)
from .inputs import (
    CrossOwnerInput,
    ModuleReviewInput,
    ModuleRevisionDiff,
    ModuleRevisionInput,
    ReviewCompletionRecord,
)
from .reporting import UserSupplement


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


class CrossReviewProgress(StrictModel):
    run_id: str
    next_action: str
    modules: dict[str, ModuleSubmission]
    pending: list[CrossReviewFinding] = Field(default_factory=list)
    responses_by_module: dict[str, list[RevisionResponse]] = Field(default_factory=dict)
    local_review_refs: dict[str, str] = Field(default_factory=dict)
    machine_refs: list[str] = Field(default_factory=list)
    finding_refs: list[str] = Field(default_factory=list)
    verdict_refs: list[str] = Field(default_factory=list)
    resolved_ids: list[str] = Field(default_factory=list)
    prior_synthesis: list = Field(default_factory=list)
    phase: str = "initial"
    review_round: int = 0
    revised_owner_ids: list[str] = Field(default_factory=list)
    cross_owner_barrier_ref: str | None = None


class FinalReviewProgress(StrictModel):
    run_id: str
    next_action: str
    current: EditedReportSubmission
    pending: list[FinalReviewFinding] = Field(default_factory=list)
    responses: list[RevisionResponse] = Field(default_factory=list)
    finding_refs: list[str] = Field(default_factory=list)
    verdict_refs: list[str] = Field(default_factory=list)
    resolved_ids: list[str] = Field(default_factory=list)
    residual_risks: list[str] = Field(default_factory=list)
    phase: str = "initial"
    review_round: int = 0
    chief_revision_number: int = 0


class _CrossOwnerLaneResult(StrictModel):
    module: ModuleSubmission
    responses: list[RevisionResponse]
    local_review_ref: str
    completion_ref: str
    completion: CrossOwnerCompletion


class _CrossOwnerPipelineResult(StrictModel):
    """One fully closed owner pipeline, safe to promote at the exact-five barrier."""

    owner_module_id: str
    initial_input_ref: str
    initial_result_ref: str
    initial_result: CrossOwnerFindingSubmission
    lane: _CrossOwnerLaneResult
    verdict_ref: str | None = None
    verdict: CrossOwnerVerdictSubmission | None = None
    finding_refs: list[str] = Field(default_factory=list)
    verdict_refs: list[str] = Field(default_factory=list)
    findings: list[CrossReviewFinding] = Field(default_factory=list)
    verdicts: list[ResolutionVerdict] = Field(default_factory=list)


class CrossOwnerInitialReviewPreparation(StrictModel):
    """Typed boundary before one Cross-owner initial Agent turn.

    A declarative runtime can dispatch ``envelope`` when ``mode`` is
    ``invoke_agent``.  If the typed finding submission is already present,
    preparation returns ``continue_existing`` with that persisted result so a
    reconstructed Action does not repeat the reviewer turn.
    """

    mode: Literal["invoke_agent", "continue_existing"]
    run_id: str
    workflow_id: str
    owner_module_id: str
    review_round: int
    reviewer_session_key: str
    owner_input_ref: str
    owner_input: CrossOwnerInput
    user_supplements: list[UserSupplement] = Field(default_factory=list)
    prior_module_review_completion_ref: str | None = None
    envelope: TaskEnvelope | None = None
    existing_result_ref: str | None = None
    existing_result: CrossOwnerFindingSubmission | None = None


class CrossOwnerInitialReviewAcceptance(StrictModel):
    """Typed boundary after accepting one Cross-owner initial result."""

    run_id: str
    workflow_id: str
    owner_module_id: str
    reviewer_session_key: str
    owner_input_ref: str
    user_supplements: list[UserSupplement] = Field(default_factory=list)
    prior_module_review_completion_ref: str | None = None
    result_ref: str
    result: CrossOwnerFindingSubmission
    next_action: Literal["continue_existing"] = "continue_existing"


class CrossOwnerRevisionPreparation(StrictModel):
    """Typed boundary before the original owner Author revises Cross findings."""

    mode: Literal["invoke_agent", "continue_existing"]
    run_id: str
    workflow_id: str
    owner_module_id: str
    review_round: int
    owner_input_ref: str
    current: ModuleSubmission
    reviewed_baseline: ModuleSubmission | None = None
    findings: list[CrossReviewFinding]
    finding_refs: list[str]
    user_supplements: list[UserSupplement] = Field(default_factory=list)
    prior_completion_ref: str | None = None
    prepared: ModuleRevisionPreparation | None = None
    existing_candidate: ModuleSubmission | None = None
    existing_candidate_ref: str | None = None


class CrossOwnerRevisionAcceptance(StrictModel):
    """Typed accepted Author candidate passed into owner-local regression."""

    run_id: str
    workflow_id: str
    owner_module_id: str
    review_round: int
    owner_input_ref: str
    current: ModuleSubmission
    findings: list[CrossReviewFinding]
    finding_refs: list[str]
    user_supplements: list[UserSupplement] = Field(default_factory=list)
    prior_completion_ref: str | None = None
    revised: ModuleSubmission
    candidate_ref: str


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


class CrossOwnerLocalReviewPreparation(StrictModel):
    """Typed boundary before the original module Auditor local regression."""

    mode: Literal["invoke_agent", "continue_existing", "preflight_revision"]
    run_id: str
    workflow_id: str
    owner_module_id: str
    review_round: int
    owner_input_ref: str
    reviewed_baseline: ModuleSubmission
    cross_responses: list[RevisionResponse]
    regression_context: ModuleLocalRegressionContext | None = None
    prepared: ModuleInitialReviewPreparation | None = None
    existing_review: ModuleInitialReviewAcceptance | None = None


class CrossOwnerLocalReviewAcceptance(StrictModel):
    """Typed accepted local-regression result passed into owner closure."""

    run_id: str
    workflow_id: str
    owner_module_id: str
    review_round: int
    owner_input_ref: str
    reviewed_baseline: ModuleSubmission
    cross_responses: list[RevisionResponse]
    regression_context: ModuleLocalRegressionContext
    review: ModuleInitialReviewAcceptance | ModuleRecheckAcceptance


class CrossOwnerRecheckPreparation(StrictModel):
    """Typed boundary before the original Cross owner reviewer recheck."""

    mode: Literal["invoke_agent", "continue_existing"]
    run_id: str
    workflow_id: str
    owner_module_id: str
    review_round: int
    reviewer_session_key: str
    initial_result_ref: str
    initial_result: CrossOwnerFindingSubmission
    lane: _CrossOwnerLaneResult
    owner_input_ref: str
    required_findings: list[CrossReviewFinding]
    envelope: TaskEnvelope | None = None
    existing_result_ref: str | None = None
    existing_result: CrossOwnerVerdictSubmission | None = None


class CrossOwnerRecheckAcceptance(StrictModel):
    """Typed accepted Cross owner verdict passed into owner completion."""

    run_id: str
    workflow_id: str
    owner_module_id: str
    review_round: int
    reviewer_session_key: str
    initial_result_ref: str
    initial_result: CrossOwnerFindingSubmission
    lane: _CrossOwnerLaneResult
    owner_input_ref: str
    required_findings: list[CrossReviewFinding]
    result_ref: str
    result: CrossOwnerVerdictSubmission


class CrossOwnerRoundProgress(StrictModel):
    """Typed owner state after one accepted Cross reviewer verdict."""

    run_id: str
    workflow_id: str
    owner_module_id: str
    initial_input_ref: str
    initial_result_ref: str
    initial_result: CrossOwnerFindingSubmission
    review_round: int
    next_review_round: int
    next_owner_input_ref: str
    next_action: Literal["revise", "completed"]
    pending: list[CrossReviewFinding]
    resolved_ids: list[str]
    finding_refs: list[str]
    verdict_refs: list[str]
    findings: list[CrossReviewFinding]
    verdicts: list[ResolutionVerdict]
    lane: _CrossOwnerLaneResult
    verdict_ref: str
    verdict: CrossOwnerVerdictSubmission
