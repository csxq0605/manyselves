"""Finding/response/verdict review lifecycles with no mutable-issue compatibility."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Literal, cast
from uuid import uuid4

from pydantic import Field

from .agentic_models import (
    CROSS_REVIEW_DIMENSIONS,
    ChiefRevisionSubmission,
    ClaimRecord,
    CrossOwnerFindingSubmission,
    CrossOwnerVerdictSubmission,
    CrossReviewCoverageEntry,
    CrossReviewFinding,
    CrossReviewFindingSubmission,
    CrossReviewVerdictSubmission,
    CrossSynthesisInput,
    EditedReportSubmission,
    FinalReviewFinding,
    FinalReviewFindingSubmission,
    FinalReviewVerdictSubmission,
    ModuleReviewFinding,
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
    StrictModel,
    TaskEnvelope,
    WorkflowDecisionSubmission,
)
from .assets import (
    validate_aggregate_retention,
    validate_editor_protection,
    validate_editor_quality,
)
from .chapter_parallel import CHAPTER_SECTION_IDS
from .cross_specialization import cross_lane_specialization
from .input_contracts import (
    AggregateFinalReviewInput,
    ChiefRevisionInput,
    CrossDecisionPackView,
    CrossOwnerInput,
    CrossOwnerRelatedModuleView,
    CrossReviewInput,
    FinalAuditSnapshot,
    FinalReviewInput,
    ModuleReviewInput,
    ModuleRevisionDiff,
    ModuleRevisionInput,
    RequestedModuleChange,
    ReviewClaimStatement,
    ReviewCompletionRecord,
    ReviewEvidenceExcerpt,
    ValidationReport,
    WorkflowExceptionInput,
    final_audit_metadata_view,
    module_content_view,
    strip_runtime_claim_markers,
)
from .models import (
    CHIEF_SECTION_RESULT_PART_IDS,
    FINAL_SUMMARY_CONCLUSION_AUDIT_SECTION_IDS,
)
from .parallel_runtime import (
    AggregateState,
    ArtifactRef,
    CrossOwnerBarrier,
    CrossOwnerCompletion,
    LaneExceptionCandidate,
    RecoveryStateStore,
    TaskAttemptStore,
    WorkflowReducer,
)
from .review_preflight import evaluate_module_review_preflight
from .revision_diff import build_revision_diff
from .scheduling import (
    AdaptiveTaskScheduler,
    SchedulingCandidate,
    TaskTimingHistory,
)
from .source_ledger import SourceLedger
from .taxonomy import REPORT_TAXONOMY

if TYPE_CHECKING:
    from .workflow import ReportWorkflowRunner


class ReviewLifecycleError(RuntimeError):
    """A deterministic review-protocol failure that requires code or model correction."""


class DeferredMainDecision(ReviewLifecycleError):
    """A private lane reached Main work that must wait for cohort drain."""


def _require_validation_binding(
    runner: "ReportWorkflowRunner",
    report: ValidationReport,
    *,
    subject_ref: str,
    subject_revision: int,
) -> None:
    """Require the validator's typed business identity, not a file digest."""

    parts = Path(subject_ref).parts
    try:
        subject_run_id = parts[parts.index("runs") + 1]
    except (ValueError, IndexError):
        subject_run_id = ""
    if (
        report.validation_protocol_version < 2
        or report.run_id != subject_run_id
        or report.subject_ref != subject_ref
        or report.subject_revision != subject_revision
    ):
        raise ReviewLifecycleError(
            "validation report does not match the final subject identity; "
            f"subject_ref={subject_ref}; subject_revision={subject_revision}"
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


class ModuleInitialReviewPreparation(StrictModel):
    """Typed, serializable boundary before one module Auditor turn.

    ``continue_existing`` is intentionally a capability result rather than a
    Kernel decision.  A runtime can use it to skip a duplicate initial Agent
    action and hand the persisted lifecycle to ``run_module_review`` with
    ``resume=True``.
    """

    mode: Literal["invoke_agent", "continue_existing"]
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
    subject_ref: str | None = None
    review_input_ref: str | None = None
    review_input: ModuleReviewInput | None = None
    envelope: TaskEnvelope | None = None
    progress: ModuleReviewProgress | None = None


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

    mode: Literal["invoke_agent", "continue_existing"]
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
    preflight_ref: str | None = None


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


def _relative(runner: "ReportWorkflowRunner", path: Path) -> str:
    return path.relative_to(runner.service.workspace).as_posix()


def _write_model(
    runner: "ReportWorkflowRunner",
    relative: str,
    model,
) -> str:
    return _relative(
        runner,
        runner.service.store.write_json(relative, model.model_dump(mode="json")),
    )


def _write_immutable_model(
    runner: "ReportWorkflowRunner",
    relative: str,
    model,
) -> str:
    """Persist semantic audit evidence once; identical resume replay is allowed."""

    path = runner.service.workspace / relative
    payload = model.model_dump(mode="json")
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReviewLifecycleError(
                f"immutable audit artifact is unreadable: {relative}"
            ) from exc
        if existing != payload:
            raise ReviewLifecycleError(
                f"refusing to overwrite immutable audit artifact: {relative}"
            )
        return _relative(runner, path)
    return _relative(runner, runner.service.store.write_json(relative, payload))


def _load_progress(
    runner: "ReportWorkflowRunner",
    relative: str,
    model,
):
    path = runner.service.workspace / relative
    if not path.is_file():
        return None
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _unique_ids(values: Iterable[str], *, label: str) -> set[str]:
    items = list(values)
    if len(items) != len(set(items)):
        raise ReviewLifecycleError(f"{label} contains duplicate ids")
    return set(items)


def _validate_responses(
    responses: list[RevisionResponse],
    required_ids: set[str],
    allowed_targets: set[str],
) -> None:
    response_ids = _unique_ids(
        (response.finding_id for response in responses),
        label="revision responses",
    )
    if response_ids != required_ids:
        raise ReviewLifecycleError(
            "revision responses must cover exactly the assigned findings; "
            f"missing={sorted(required_ids - response_ids)}; "
            f"unexpected={sorted(response_ids - required_ids)}"
        )
    invalid_targets = sorted(
        {
            target_id
            for response in responses
            for target_id in response.changed_target_ids
            if target_id not in allowed_targets
        }
    )
    if invalid_targets:
        raise ReviewLifecycleError(
            f"revision responses declare out-of-scope targets: {invalid_targets}"
        )


def _apply_chief_patch(
    baseline: EditedReportSubmission,
    patch: ChiefRevisionSubmission,
    *,
    target_section_ids: set[str],
    required_finding_ids: set[str],
) -> EditedReportSubmission:
    """Merge a compact chief patch while inheriting all unassigned report state."""

    if set(patch.section_bodies) != target_section_ids:
        raise ReviewLifecycleError("chief patch must contain exactly the assigned final sections")
    _validate_responses(
        patch.revision_responses,
        required_finding_ids,
        target_section_ids,
    )
    updates = {
        CHIEF_SECTION_RESULT_PART_IDS[section_id]: body
        for section_id, body in patch.section_bodies.items()
    }
    updates["revision_responses"] = list(patch.revision_responses)
    payload = baseline.model_dump(mode="python")
    payload.update(updates)
    return EditedReportSubmission.model_validate(payload)


def _final_audit_markdown(canonical_markdown: str) -> str:
    """Return only Chief-owned Chapter 1/3 prose for semantic final audit.

    Chapter 2 is immutable module prose and Chapter 4 is structural-only for
    the later deterministic gate; neither is sent to the Final reviewer.
    """

    chapter_two = "\n## 2. 评估内容描述"
    chapter_three = "\n## 3. 结论与建议"
    start = canonical_markdown.find(chapter_two)
    end = canonical_markdown.find(chapter_three)
    if start < 0 or end < 0 or end <= start:
        raise ReviewLifecycleError("canonical report is missing the fixed Chapter 2/3 boundary")
    audit_markdown = canonical_markdown[:start] + canonical_markdown[end:]
    chapter_four = re.search(r"\n#{1,6}\s+4\.\s+", audit_markdown)
    if chapter_four is not None:
        audit_markdown = audit_markdown[: chapter_four.start()]
    return audit_markdown.rstrip()


def _final_audit_section_bodies(
    subject: EditedReportSubmission,
) -> dict[str, str]:
    """Return the fixed chief-owned section bodies without repeating Chapter 2."""

    return {
        section_id: strip_runtime_claim_markers(
            getattr(subject, CHIEF_SECTION_RESULT_PART_IDS[section_id]) or ""
        ).rstrip()
        for section_id in FINAL_SUMMARY_CONCLUSION_AUDIT_SECTION_IDS
    }


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _model_sha256(model: StrictModel) -> str:
    payload = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _text_sha256(payload)


def _review_claim_statement(claim: ClaimRecord) -> ReviewClaimStatement:
    return ReviewClaimStatement(
        statement_ref=("statement-" + hashlib.sha256(claim.id.encode("utf-8")).hexdigest()[:12]),
        submodule_id=claim.submodule_id,
        text=claim.text,
        statement_type=claim.claim_type,
        evidence_ids=claim.source_ids,
        confidence=claim.confidence,
        unresolved=claim.unresolved,
    )


def _module_recheck_delta(
    baseline: ModuleSubmission,
    current: ModuleSubmission,
    scope: set[str],
) -> dict[str, object]:
    """Build a bounded delta from the last subject seen by the paid reviewer."""

    raw = build_revision_diff(baseline, current)
    changed_narratives = set(raw["changed_submodule_narratives"])
    if not changed_narratives.issubset(scope):
        raise ReviewLifecycleError(
            "module recheck revision changed narratives outside reviewer finding scope: "
            f"{sorted(changed_narratives - scope)}"
        )
    baseline_claims = {claim.id: claim for claim in baseline.claims}
    current_claims = {claim.id: claim for claim in current.claims}
    changed_claim_ids = set(raw["changed_claim_ids"])
    changed_claim_submodules = {
        claim.submodule_id
        for claim_id in changed_claim_ids
        for claim in (
            baseline_claims.get(claim_id),
            current_claims.get(claim_id),
        )
        if claim is not None
    }
    if not changed_claim_submodules.issubset(scope):
        raise ReviewLifecycleError(
            "module recheck revision changed Claims outside reviewer finding scope: "
            f"{sorted(changed_claim_submodules - scope)}"
        )
    current_statements = [
        _review_claim_statement(current_claims[claim_id])
        for claim_id in sorted(changed_claim_ids)
        if claim_id in current_claims
    ]
    prior_statements = [
        _review_claim_statement(baseline_claims[claim_id])
        for claim_id in sorted(changed_claim_ids)
        if claim_id in baseline_claims
    ]
    changed_statement_refs = sorted(
        {statement.statement_ref for statement in [*current_statements, *prior_statements]}
    )
    delta_submodule_ids = changed_narratives | {
        statement.submodule_id for statement in current_statements
    }
    subject_view = module_content_view(current, changed_narratives).model_copy(
        update={
            "evidence_ids_by_submodule": {
                submodule_id: sorted(
                    {
                        evidence_id
                        for statement in current_statements
                        if statement.submodule_id == submodule_id
                        for evidence_id in statement.evidence_ids
                        if evidence_id.startswith("E-")
                    }
                )
                for submodule_id in delta_submodule_ids
            }
        }
    )
    changed_statement_ids = set(changed_statement_refs)
    unchanged_statement_sha256 = {
        statement.statement_ref: _model_sha256(statement)
        for statement in (
            _review_claim_statement(claim)
            for claim in current.claims
            if claim.submodule_id in scope and claim.id not in changed_claim_ids
        )
        if statement.statement_ref not in changed_statement_ids
    }
    revision_diff = ModuleRevisionDiff(
        module_id=current.module_id,
        from_revision=baseline.revision,
        to_revision=current.revision,
        changed_submodule_narratives=sorted(changed_narratives),
        changed_statement_refs=changed_statement_refs,
        evidence_ids_added=raw["source_ids_added"],
        evidence_ids_removed=raw["source_ids_removed"],
    )
    relevant_evidence_ids = {
        evidence_id
        for statement in [*current_statements, *prior_statements]
        for evidence_id in statement.evidence_ids
        if evidence_id.startswith("E-")
    }
    return {
        "subject": subject_view,
        "claim_statements": current_statements,
        "prior_claim_statements": prior_statements,
        "unchanged_submodule_sha256": {
            submodule_id: _text_sha256(
                strip_runtime_claim_markers(current.submodule_narratives[submodule_id]).rstrip()
            )
            for submodule_id in sorted(scope - changed_narratives)
        },
        "unchanged_statement_sha256": unchanged_statement_sha256,
        "revision_diff": revision_diff,
        "relevant_evidence_ids": relevant_evidence_ids,
    }


def _apply_module_patch(
    baseline: ModuleSubmission,
    patch: ModuleRevisionSubmission,
    *,
    target_submodule_ids: set[str],
    required_finding_ids: set[str],
) -> ModuleSubmission:
    if patch.module_id != baseline.module_id:
        raise ReviewLifecycleError("module patch belongs to a different module")
    if patch.base_revision != baseline.revision:
        raise ReviewLifecycleError("module patch base_revision does not match the supplied subject")
    if set(patch.submodule_narratives) - target_submodule_ids:
        raise ReviewLifecycleError("module patch replaces an unassigned submodule")
    _validate_responses(
        patch.revision_responses,
        required_finding_ids,
        target_submodule_ids,
    )
    baseline_claims = {claim.id: claim for claim in baseline.claims}
    for claim_id in patch.claim_ids_remove:
        claim = baseline_claims.get(claim_id)
        if claim is None:
            raise ReviewLifecycleError(f"module patch removes unknown Claim id: {claim_id}")
        if claim.submodule_id not in target_submodule_ids:
            raise ReviewLifecycleError(f"module patch removes out-of-scope Claim id: {claim_id}")
        del baseline_claims[claim_id]
    for claim in patch.claims_upsert:
        if claim.module_id != baseline.module_id:
            raise ReviewLifecycleError(f"module patch Claim belongs to another module: {claim.id}")
        if claim.submodule_id not in target_submodule_ids:
            raise ReviewLifecycleError(f"module patch changes out-of-scope Claim id: {claim.id}")
        baseline_claims[claim.id] = claim
    claim_source_ids = {
        source_id for claim in baseline_claims.values() for source_id in claim.source_ids
    }
    if not claim_source_ids.issubset(set(patch.source_ids)):
        raise ReviewLifecycleError(
            "module patch source_ids do not cover every resulting Claim source"
        )
    narratives = dict(baseline.submodule_narratives)
    narratives.update(patch.submodule_narratives)
    return ModuleSubmission(
        module_id=baseline.module_id,
        submodule_narratives=narratives,
        claims=list(baseline_claims.values()),
        source_ids=patch.source_ids,
        unresolved_questions=patch.unresolved_questions,
        revision=patch.revision,
        revision_responses=patch.revision_responses,
    )


def _validate_module_findings(
    findings: list[ModuleReviewFinding],
    subject: ModuleSubmission,
    scope: set[str],
    *,
    id_prefix: str,
) -> None:
    _unique_ids((finding.id for finding in findings), label="module findings")
    for finding in findings:
        if not finding.id.startswith(id_prefix):
            raise ReviewLifecycleError(
                f"module finding id must start with {id_prefix}: {finding.id}"
            )
        if finding.target_submodule_id not in scope:
            raise ReviewLifecycleError(f"module finding targets unreviewed submodule: {finding.id}")


def _module_review_evidence_packet(
    runner: "ReportWorkflowRunner",
    subject: ModuleSubmission,
    run_id: str,
    submodule_ids: set[str],
    *,
    evidence_ids: set[str] | None = None,
) -> list[ReviewEvidenceExcerpt]:
    """Attach cited E-* evidence once so review does not become a retrieval loop."""

    ledger = SourceLedger(runner.service.workspace, run_id)
    records = {record.id: record for record in ledger.records}
    selected_evidence_ids = sorted(
        evidence_ids
        if evidence_ids is not None
        else {
            source_id
            for claim in subject.claims
            if claim.submodule_id in submodule_ids
            for source_id in claim.source_ids
            if source_id.startswith("E-")
        }
    )
    packet: list[ReviewEvidenceExcerpt] = []
    for evidence_id in selected_evidence_ids:
        record = records.get(evidence_id)
        content_ref = ledger.content_ref(evidence_id)
        if record is None or content_ref is None:
            raise ReviewLifecycleError(
                f"review subject cites unreadable current-run evidence: {evidence_id}"
            )
        content = (runner.service.workspace / content_ref).read_text(encoding="utf-8")
        packet.append(
            ReviewEvidenceExcerpt(
                evidence_id=evidence_id,
                title=record.title,
                locator=record.locator,
                content=content,
            )
        )
    return packet


def _module_review_knowledge_packet(
    runner: "ReportWorkflowRunner",
    state: dict,
    module_id: str,
    submodule_ids: set[str],
) -> tuple[str | None, str]:
    """Return only taxonomy sections relevant to the current module-review scope."""

    knowledge_ref = state.get("module_knowledge_refs", {}).get(module_id)
    if not knowledge_ref:
        return None, ""
    path = runner.service.workspace / knowledge_ref
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReviewLifecycleError(
            f"module review Knowledge is unreadable: {knowledge_ref}"
        ) from exc

    selected: list[str] = []
    current_selected = True
    for line in text.splitlines():
        match = re.match(r"^##\s+(2\.[1-5](?:\.\d+)+)\b", line)
        if match:
            current_selected = match.group(1) in submodule_ids
        if current_selected:
            selected.append(line)
    complete_selected_context = "\n".join(selected).strip()
    return knowledge_ref, complete_selected_context


def _build_module_recheck_input_and_envelope(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    module_id: str,
    current: ModuleSubmission,
    pending: Iterable[ModuleReviewFinding],
    scope: set[str],
    lifecycle_id: str,
    review_round: int,
    review_root: str,
    subject_ref: str,
    last_reviewed_subject_ref: str | None,
    responses: list[RevisionResponse],
    finding_refs: list[str],
    signal_ref: str,
    validation_report: ValidationReport,
) -> tuple[ModuleReviewInput, TaskEnvelope, str, str, str]:
    """Build the one shared module recheck input/envelope boundary.

    The delta projections, unchanged semantic metadata, prior-result delivery
    mode, and exact recheck TaskEnvelope are kept here so the declarative
    boundary and the legacy lifecycle consume the same contract.
    """

    pending_items = list(pending)
    baseline_subject_ref = last_reviewed_subject_ref
    if baseline_subject_ref is None:
        legacy_candidate = (
            f"Work/runs/{state['run_id']}/modules/{module_id}-r{current.revision - 1}.json"
        )
        if current.revision > 0 and (runner.service.workspace / legacy_candidate).is_file():
            baseline_subject_ref = legacy_candidate
        else:
            raise ReviewLifecycleError("module recheck lacks the last subject seen by its reviewer")
    try:
        baseline = ModuleSubmission.model_validate_json(
            (runner.service.workspace / baseline_subject_ref).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ReviewLifecycleError(
            f"module recheck last-reviewed baseline is unreadable: {baseline_subject_ref}"
        ) from exc
    delta = _module_recheck_delta(baseline, current, scope)
    relevant_evidence_ids = set(delta["relevant_evidence_ids"])
    relevant_evidence_ids.update(
        evidence_ref
        for finding in pending_items
        for evidence_ref in finding.evidence_refs
        if evidence_ref.startswith("E-")
    )
    revision_diff_ref = _write_model(
        runner,
        f"{review_root}/recheck-diff-r{review_round}.json",
        delta["revision_diff"],
    )
    knowledge_ref, _knowledge_context = _module_review_knowledge_packet(
        runner,
        state,
        module_id,
        scope,
    )
    review_input = ModuleReviewInput(
        phase="recheck",
        run_id=state["run_id"],
        module_id=module_id,
        lifecycle_id=lifecycle_id,
        review_round=review_round,
        subject_ref=subject_ref,
        subject_revision=current.revision,
        subject=delta["subject"],
        claim_statements=delta["claim_statements"],
        prior_claim_statements=delta["prior_claim_statements"],
        unchanged_submodule_sha256=delta["unchanged_submodule_sha256"],
        unchanged_statement_sha256=delta["unchanged_statement_sha256"],
        knowledge_ref=knowledge_ref,
        knowledge_context="",
        evidence=_module_review_evidence_packet(
            runner,
            current,
            state["run_id"],
            scope,
            evidence_ids=relevant_evidence_ids,
        ),
        required_submodule_ids=sorted(scope),
        required_findings=pending_items,
        revision_responses=responses,
        baseline_subject_ref=baseline_subject_ref,
        revision_diff_ref=revision_diff_ref,
        revision_diff=delta["revision_diff"],
        validation_report_ref=signal_ref,
        validation_report=validation_report,
    )
    input_ref = _write_model(
        runner,
        f"{review_root}/input-r{review_round}.json",
        review_input,
    )
    envelope = TaskEnvelope(
        task_id=f"module-{module_id}-{lifecycle_id}-review-r{review_round}",
        run_id=state["run_id"],
        agent_id="evidence-auditor",
        objective=(f"只对模块 {module_id} 的 required_findings 返回逐项 verdict，并检查修改回归。"),
        input_refs=[input_ref],
        constraints=[
            "coverage 记录实际检查范围，不是批准状态",
            "一次返回整个模块检查范围的 findings/verdicts；小节 id 只用于定位问题，"
            "不得拆成独立小节级审查任务或会话",
            "finding 首次提出后不可改写；复审不得复述旧 finding",
            "finding id 由运行时按 lifecycle 和 review round 分配，审查员不得提交或猜测 id",
            "advisory 与 blocking 都必须获得作者响应和 reviewer verdict",
            "verdicts 必须逐项且仅覆盖 required_findings；new_findings 只允许真实回归",
            *runner._user_supplement_constraints(
                state,
                stage="module_review",
                target_ids={
                    module_id,
                    *scope,
                    *(claim.id for claim in current.claims),
                },
            ),
        ],
        allowed_outputs=["module_review_verdict_submission"],
        revision=review_round,
        prior_result_ref=finding_refs[-1] if finding_refs else None,
        artifact_delivery_modes=_module_review_artifact_delivery_modes(
            input_ref,
            finding_refs,
        ),
        target_submodule_ids=sorted(scope),
        input_contract_kind="module_review_input",
        input_contract_ref=input_ref,
        inline_context=runner._template_skill_context(state, f"auditor-{module_id}"),
        allowed_tools=["submit_result"],
    )
    return (
        review_input,
        envelope,
        input_ref,
        baseline_subject_ref,
        revision_diff_ref,
    )


def _module_review_artifact_delivery_modes(
    input_ref: str,
    finding_refs: list[str],
) -> dict[str, str]:
    """Reuse the existing module-review artifact delivery projection."""

    return {
        input_ref: "inline",
        **({finding_refs[-1]: "hash_retained"} if finding_refs else {}),
    }


def _module_reviewer_session_key(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    module_id: str,
    lifecycle_id: str,
    progress: ModuleReviewProgress | None,
    regression_context: ModuleLocalRegressionContext | None,
) -> str:
    """Reuse exactly one stable reviewer identity per module."""

    stable = f"module-auditor-{module_id}"

    def require_module_identity(value: str) -> str:
        if value != stable:
            raise ReviewLifecycleError(
                f"module review progress belongs to another reviewer: {value}"
            )
        return value

    if regression_context is not None:
        return require_module_identity(
            regression_context.prior_review_completion.reviewer_session_key
        )
    if progress is not None and progress.reviewer_session_key:
        return require_module_identity(progress.reviewer_session_key)
    return stable


def _save_module_review_progress(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    progress_ref: str,
    module_id: str,
    next_action: str,
    current: ModuleSubmission,
    pending: Iterable[ModuleReviewFinding],
    responses: list[RevisionResponse],
    finding_refs: list[str],
    verdict_refs: list[str],
    resolved_ids: set[str],
    review_round: int,
    phase: str,
    scope: set[str],
    reviewer_session_key: str,
    last_reviewed_subject_ref: str | None,
) -> None:
    """Persist the existing module-review progress shape at one boundary."""

    _write_model(
        runner,
        progress_ref,
        ModuleReviewProgress(
            run_id=state["run_id"],
            module_id=module_id,
            next_action=next_action,
            current=current,
            pending=list(pending),
            responses=responses,
            finding_refs=finding_refs,
            verdict_refs=verdict_refs,
            resolved_ids=sorted(resolved_ids),
            review_round=review_round,
            phase=phase,
            scope=sorted(scope),
            reviewer_session_key=reviewer_session_key,
            last_reviewed_subject_ref=last_reviewed_subject_ref,
            review_protocol_version=2,
        ),
    )


async def prepare_module_initial_review(
    runner: "ReportWorkflowRunner",
    *,
    module_id: str,
    payload: ModuleSubmission,
    state: dict,
    workflow_id: str,
    initial_scope: set[str],
    lifecycle_id: str,
    regression_context: ModuleLocalRegressionContext | None = None,
) -> ModuleInitialReviewPreparation:
    """Prepare the current initial or local-regression Auditor boundary.

    The returned model is safe to pass through a generic runtime.  On a
    resumed run with a valid persisted lifecycle, it deliberately returns
    ``continue_existing`` so the caller can invoke ``run_module_review`` with
    ``resume=True`` instead of repeating the initial Auditor turn.
    """

    if payload.module_id != module_id:
        raise ReviewLifecycleError("module review payload belongs to a different module")
    if not re.fullmatch(r"[a-z0-9-]+", lifecycle_id):
        raise ReviewLifecycleError("module review lifecycle_id is not a safe component")

    review_root = f"Work/runs/{state['run_id']}/reviews/module/{lifecycle_id}/{module_id}"
    progress_ref = f"{review_root}/progress.json"
    phase = "local_regression" if regression_context is not None else "initial"
    progress = (
        _load_progress(runner, progress_ref, ModuleReviewProgress) if state.get("resume") else None
    )
    reviewer_session_key = _module_reviewer_session_key(
        runner,
        state=state,
        module_id=module_id,
        lifecycle_id=lifecycle_id,
        progress=progress,
        regression_context=regression_context,
    )
    if progress is not None:
        if progress.run_id != state["run_id"] or progress.module_id != module_id:
            raise ReviewLifecycleError("module review progress identity mismatch")
        if progress.next_action in {"completed", "revise", "review"}:
            return ModuleInitialReviewPreparation(
                mode="continue_existing",
                run_id=state["run_id"],
                module_id=module_id,
                lifecycle_id=lifecycle_id,
                workflow_id=workflow_id,
                reviewer_session_key=reviewer_session_key,
                review_root=review_root,
                progress_ref=progress_ref,
                review_round=progress.review_round,
                scope=list(progress.scope),
                current=progress.current,
                subject_ref=(
                    f"Work/runs/{state['run_id']}/modules/"
                    f"{module_id}-r{progress.current.revision}.json"
                ),
                progress=progress,
            )

    current = payload
    review_round = 0
    scope = set(initial_scope)
    preflight_attempts = 0
    preflight_failure_fingerprints: dict[tuple, int] = {}
    while True:
        subject_ref = f"Work/runs/{state['run_id']}/modules/{module_id}-r{current.revision}.json"
        if not (runner.service.workspace / subject_ref).is_file():
            _write_model(runner, subject_ref, current)
        structure_ref = runner._validate_module_structure(
            state,
            current,
            f"review-r{review_round}",
        )
        structure_report = ValidationReport.model_validate_json(
            (runner.service.workspace / structure_ref).read_text(encoding="utf-8")
        )
        _require_validation_binding(
            runner,
            structure_report,
            subject_ref=subject_ref,
            subject_revision=current.revision,
        )
        preflight = evaluate_module_review_preflight(
            runner.service.workspace,
            run_id=state["run_id"],
            subject=current,
            subject_ref=subject_ref,
            upstream_report=structure_report,
        )
        signal_ref = _write_model(
            runner,
            (f"{review_root}/preflight-subject-r{current.revision}-review-r{review_round}.json"),
            preflight.report,
        )
        validation_report = preflight.report
        if validation_report.passed:
            break
        preflight_attempts += 1
        fingerprint = tuple(
            sorted(
                (
                    failure.check_id,
                    failure.target_path,
                    failure.message,
                )
                for failure in validation_report.failures
            )
        )
        repeated = preflight_failure_fingerprints.get(fingerprint, 0) + 1
        preflight_failure_fingerprints[fingerprint] = repeated
        if repeated >= 2 or preflight_attempts >= 3:
            raise ReviewLifecycleError(
                "module preflight failed repeatedly before semantic review; "
                "no reviewer finding or verdict was created. "
                f"module={module_id}; attempts={preflight_attempts}; "
                f"validation_ref={signal_ref}"
            )
        current, _ = await request_module_revision(
            runner,
            state=state,
            workflow_id=workflow_id,
            subject=current,
            module_findings=[],
            cross_findings=(
                regression_context.trigger_cross_findings if regression_context is not None else []
            ),
            validation_ref=signal_ref,
            validation_target_submodule_ids=set(preflight.target_submodule_ids),
        )
        _save_module_review_progress(
            runner,
            state=state,
            progress_ref=progress_ref,
            module_id=module_id,
            next_action="review",
            current=current,
            pending=[],
            responses=[],
            finding_refs=[],
            verdict_refs=[],
            resolved_ids=set(),
            review_round=review_round,
            phase=phase,
            scope=scope,
            reviewer_session_key=reviewer_session_key,
            last_reviewed_subject_ref=None,
        )

    review_subject = module_content_view(current, scope)
    review_claim_statements = [
        _review_claim_statement(claim) for claim in current.claims if claim.submodule_id in scope
    ]
    knowledge_ref, knowledge_context = _module_review_knowledge_packet(
        runner,
        state,
        module_id,
        scope,
    )
    if phase != "initial":
        knowledge_context = ""
    review_input = ModuleReviewInput(
        phase=phase,
        run_id=state["run_id"],
        module_id=module_id,
        lifecycle_id=lifecycle_id,
        review_round=review_round,
        subject_ref=subject_ref,
        subject_revision=current.revision,
        subject=review_subject,
        claim_statements=review_claim_statements,
        prior_claim_statements=[],
        unchanged_submodule_sha256={},
        unchanged_statement_sha256={},
        knowledge_ref=knowledge_ref,
        knowledge_context=knowledge_context,
        evidence=_module_review_evidence_packet(
            runner,
            current,
            state["run_id"],
            scope,
            evidence_ids=None,
        ),
        required_submodule_ids=sorted(scope),
        required_findings=[],
        revision_responses=[],
        prior_review_completion_ref=(
            regression_context.prior_review_completion_ref
            if regression_context is not None
            else None
        ),
        prior_review_completion=(
            regression_context.prior_review_completion if regression_context is not None else None
        ),
        baseline_subject_ref=(
            regression_context.baseline_subject_ref if regression_context is not None else None
        ),
        trigger_cross_findings=(
            regression_context.trigger_cross_findings if regression_context is not None else []
        ),
        trigger_revision_responses=(
            regression_context.trigger_revision_responses if regression_context is not None else []
        ),
        revision_diff_ref=(
            regression_context.revision_diff_ref if regression_context is not None else None
        ),
        revision_diff=(
            regression_context.revision_diff if regression_context is not None else None
        ),
        validation_report_ref=signal_ref,
        validation_report=validation_report,
    )
    input_ref = _write_model(
        runner,
        f"{review_root}/input-r{review_round}.json",
        review_input,
    )
    envelope = TaskEnvelope(
        task_id=f"module-{module_id}-{lifecycle_id}-review-r{review_round}",
        run_id=state["run_id"],
        agent_id="evidence-auditor",
        objective=(
            f"审查模块 {module_id} 的当前正文、Claim 与证据边界。"
            if phase == "initial"
            else (f"由模块 {module_id} 的原审查者仅检查 Cross 回改范围、diff、Claim 与证据回归。")
        ),
        input_refs=[input_ref],
        constraints=[
            "coverage 记录实际检查范围，不是批准状态",
            "一次返回整个模块检查范围的 findings/verdicts；小节 id 只用于定位问题，"
            "不得拆成独立小节级审查任务或会话",
            "finding 首次提出后不可改写；复审不得复述旧 finding",
            "finding id 由运行时按 lifecycle 和 review round 分配，审查员不得提交或猜测 id",
            "advisory 与 blocking 都必须获得作者响应和 reviewer verdict",
            "首轮必须覆盖 input 中全部 required_submodule_ids",
            *(
                [
                    "这是原模块审查者的 local_regression，不得重新审查未修改小节",
                    "只根据 prior completion、Cross finding/作者响应、revision diff、目标 Claim/证据和当前 validation 提出真实回归 finding",
                ]
                if phase == "local_regression"
                else []
            ),
            *runner._user_supplement_constraints(
                state,
                stage="module_review",
                target_ids={
                    module_id,
                    *scope,
                    *(claim.id for claim in current.claims),
                },
            ),
        ],
        allowed_outputs=["module_review_finding_submission"],
        revision=review_round,
        prior_result_ref=None,
        artifact_delivery_modes={input_ref: "inline"},
        target_submodule_ids=sorted(scope),
        input_contract_kind="module_review_input",
        input_contract_ref=input_ref,
        inline_context=runner._template_skill_context(state, f"auditor-{module_id}"),
        allowed_tools=["submit_result"],
    )
    return ModuleInitialReviewPreparation(
        mode="invoke_agent",
        run_id=state["run_id"],
        module_id=module_id,
        lifecycle_id=lifecycle_id,
        workflow_id=workflow_id,
        reviewer_session_key=reviewer_session_key,
        review_root=review_root,
        progress_ref=progress_ref,
        review_round=review_round,
        scope=sorted(scope),
        current=current,
        subject_ref=subject_ref,
        review_input_ref=input_ref,
        review_input=review_input,
        envelope=envelope,
        progress=None,
    )


def _validate_verdicts(
    verdicts: list[ResolutionVerdict],
    required_ids: set[str],
) -> None:
    verdict_ids = _unique_ids((verdict.finding_id for verdict in verdicts), label="review verdicts")
    if verdict_ids != required_ids:
        raise ReviewLifecycleError(
            "review verdicts must cover exactly the required findings; "
            f"missing={sorted(required_ids - verdict_ids)}; "
            f"unexpected={sorted(verdict_ids - required_ids)}"
        )


def _validate_final_findings(
    findings: list[FinalReviewFinding],
    allowed_section_ids: set[str],
) -> None:
    _unique_ids((finding.id for finding in findings), label="final-review findings")
    for finding in findings:
        invalid = sorted(set(finding.target_section_ids) - allowed_section_ids)
        if invalid:
            raise ReviewLifecycleError(
                "final-review finding targets an inactive report section: "
                f"finding={finding.id}; sections={invalid}"
            )


def _final_cross_decision_view(
    runner: "ReportWorkflowRunner",
    state: dict,
) -> CrossDecisionPackView:
    """Load the current-run CrossDecisionPack view for normal Final review."""

    pack = state.get("cross_decision_pack")
    if pack is None and hasattr(runner, "_materialize_chief_cross_decision_pack"):
        try:
            pack = runner._materialize_chief_cross_decision_pack(state)
        except Exception as exc:
            raise ReviewLifecycleError(
                "Final review requires a materialized current-run CrossDecisionPack"
            ) from exc
    if pack is None or not hasattr(pack, "model_dump"):
        raise ReviewLifecycleError(
            "Final review requires a materialized current-run CrossDecisionPack"
        )
    payload = pack.model_dump(mode="python")
    payload["artifact_refs"] = [pack.cross_review_completion_ref]
    try:
        return CrossDecisionPackView.model_validate(payload)
    except ValueError as exc:
        raise ReviewLifecycleError("Final review CrossDecisionPack view is invalid") from exc


def _final_review_input(
    *,
    runner: "ReportWorkflowRunner",
    state: dict,
    aggregate_mode: bool,
    phase: str,
    subject_ref: str,
    subject_revision: int,
    subject_metadata,
    subject_metadata_sha256: str | None,
    canonical_markdown: str | None,
    changed_section_bodies: dict[str, str],
    unchanged_section_sha256: dict[str, str],
    required_section_ids: list[str],
    required_findings: list[FinalReviewFinding],
    revision_responses: list[RevisionResponse],
    validation_report_ref: str,
    validation_report: ValidationReport,
):
    """Construct the normal or explicit aggregate Final input adapter."""

    common = {
        "phase": phase,
        "run_id": state["run_id"],
        "subject_ref": subject_ref,
        "subject_revision": subject_revision,
        "subject_metadata": subject_metadata,
        "subject_metadata_sha256": subject_metadata_sha256,
        "canonical_markdown": canonical_markdown,
        "changed_section_bodies": changed_section_bodies,
        "unchanged_section_sha256": unchanged_section_sha256,
        "required_section_ids": required_section_ids,
        "required_findings": required_findings,
        "revision_responses": revision_responses,
        "validation_report_ref": validation_report_ref,
        "validation_report": validation_report,
    }
    if aggregate_mode:
        return AggregateFinalReviewInput(
            mode="aggregate_existing",
            cross_context=None,
            residual_risks=list(state.get("final_residual_risks", [])),
            **common,
        )
    cross_view = _final_cross_decision_view(runner, state)
    residual_risks = list(
        dict.fromkeys(
            [
                *getattr(cross_view, "residual_risks", []),
                *state.get("final_residual_risks", []),
            ]
        )
    )
    return FinalReviewInput(
        cross_decision=cross_view,
        cross_decision_pack_ref=state.get("cross_decision_pack_ref", ""),
        residual_risks=residual_risks,
        **common,
    )


async def _main_exception_decision(
    runner: "ReportWorkflowRunner",
    **kwargs,
) -> WorkflowDecisionSubmission:
    """Run Main only outside an active private-lane cohort, always serialized."""

    if kwargs.get("state", {}).get("_defer_main_exceptions"):
        raise DeferredMainDecision(
            "Main exception decision deferred until the active cohort drains"
        )

    lock = getattr(runner, "_main_exception_lock", None)
    if lock is None:
        return await _main_exception_decision_locked(runner, **kwargs)
    async with lock:
        return await _main_exception_decision_locked(runner, **kwargs)


async def _main_exception_decision_locked(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    workflow_id: str,
    scope: str,
    subject_refs: list[str],
    finding_refs: list[str],
    verdicts: list[ResolutionVerdict],
    responses: list[RevisionResponse],
    trigger: str = "reviewer_escalation",
) -> WorkflowDecisionSubmission:
    exception_ids = (
        {verdict.finding_id for verdict in verdicts if verdict.verdict == "escalate"}
        if trigger == "reviewer_escalation"
        else {
            response.finding_id
            for response in responses
            if response.action in {"disputed", "needs_input"}
        }
    )
    if not exception_ids:
        raise ReviewLifecycleError("Main exception decision requires explicit finding ids")
    exception_input = WorkflowExceptionInput(
        run_id=state["run_id"],
        scope=scope,
        trigger=trigger,
        finding_ids=sorted(exception_ids),
        subject_refs=subject_refs,
        finding_refs=finding_refs,
        verdicts=(
            [verdict for verdict in verdicts if verdict.verdict == "escalate"]
            if trigger == "reviewer_escalation"
            else []
        ),
        revision_responses=[
            response for response in responses if response.finding_id in exception_ids
        ],
    )
    input_ref = _write_model(
        runner,
        (
            f"Work/runs/{state['run_id']}/exceptions/"
            f"{scope}-{trigger}-{'-'.join(sorted(exception_ids))}.json"
        ),
        exception_input,
    )
    envelope = TaskEnvelope(
        task_id=f"{scope}-review-exception",
        run_id=state["run_id"],
        agent_id="main-agent",
        objective=(
            "只处理明确进入例外节点的 finding：核对当前正文、作者响应与可用审查理由，"
            "决定退回作者、接受争议、请求用户或停止不完整。"
        ),
        input_refs=[input_ref, *subject_refs, *finding_refs],
        constraints=[
            "不得重新执行整轮专业审查",
            "不得改写 finding、author response 或 reviewer verdict",
            "finding_ids 必须精确覆盖全部且仅覆盖本次 exception finding",
        ],
        allowed_outputs=["workflow_decision_submission"],
        input_contract_kind="workflow_exception_input",
        input_contract_ref=input_ref,
    )
    result = await runner._agent(
        "main-agent",
        envelope,
        envelope.input_refs,
        workflow_id,
        session_key=f"main-{scope}-exception",
    )
    if not isinstance(result, WorkflowDecisionSubmission):
        raise ReviewLifecycleError("Main returned the wrong exception-decision type")
    if set(result.finding_ids) != exception_ids:
        raise ReviewLifecycleError(
            "Main exception decision must cover exactly the exception findings"
        )
    decision_ref = _write_model(
        runner,
        (
            f"Work/runs/{state['run_id']}/exceptions/"
            f"{scope}-{trigger}-decision-{'-'.join(sorted(exception_ids))}.json"
        ),
        result,
    )
    state.setdefault("review_exception_refs", []).append(decision_ref)
    if result.decision == "request_user":
        from .workflow import ReportingNeedsDecisionError

        raise ReportingNeedsDecisionError(result.rationale)
    if result.decision == "stop_incomplete":
        from .workflow import ReportingNeedsDecisionError

        raise ReportingNeedsDecisionError(result.rationale, keep_agents_alive=False)
    return result


async def prepare_module_revision(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    workflow_id: str,
    subject: ModuleSubmission,
    module_findings: list[ModuleReviewFinding] | None = None,
    cross_findings: list[CrossReviewFinding] | None = None,
    requested_changes: list[RequestedModuleChange] | None = None,
    validation_ref: str | None = None,
    validation_target_submodule_ids: set[str] | None = None,
) -> ModuleRevisionPreparation:
    """Prepare one complete module revision for a generic Agent runtime.

    Module-level revision is the only supported revision protocol.  A single
    specialist receives the exact assigned submodule slice, writes one typed
    ``module_revision_submission``, and the runtime applies that patch once;
    there is no leaf fan-out or reducer merge to reconstruct the module.
    """

    module_findings = module_findings or []
    cross_findings = cross_findings or []
    requested_changes = requested_changes or []
    validation_target_submodule_ids = validation_target_submodule_ids or set()
    validation_report = (
        ValidationReport.model_validate_json(
            (runner.service.workspace / validation_ref).read_text(encoding="utf-8")
        )
        if validation_ref
        else None
    )
    required_ids = {
        *(finding.id for finding in module_findings),
        *(finding.id for finding in cross_findings),
        *(change.id for change in requested_changes),
    }
    targets = {
        *(finding.target_submodule_id for finding in module_findings),
        *(target_id for finding in cross_findings for target_id in finding.target_submodule_ids),
        *(target_id for change in requested_changes for target_id in change.target_submodule_ids),
        *validation_target_submodule_ids,
    }
    if validation_report is not None:
        validation_subject_ref = (
            f"Work/runs/{state['run_id']}/modules/{subject.module_id}-r{subject.revision}.json"
        )
        _require_validation_binding(
            runner,
            validation_report,
            subject_ref=validation_subject_ref,
            subject_revision=subject.revision,
        )
        if not targets:
            raise ReviewLifecycleError(
                "failed structural validation requires explicit module-local correction targets"
            )
    if not targets:
        raise ReviewLifecycleError(
            f"module revision requires at least one target submodule: {subject.module_id}"
        )
    revision_input = ModuleRevisionInput(
        run_id=state["run_id"],
        module_id=subject.module_id,
        subject_ref=(
            f"Work/runs/{state['run_id']}/modules/{subject.module_id}-r{subject.revision}.json"
        ),
        subject=module_content_view(subject, targets),
        target_submodule_ids=sorted(targets),
        module_findings=module_findings,
        cross_findings=cross_findings,
        requested_changes=requested_changes,
        validation_report_ref=validation_ref,
        validation_report=validation_report,
    )
    input_ref = _write_model(
        runner,
        (
            f"Work/runs/{state['run_id']}/reviews/module-revision-input-"
            f"{subject.module_id}-r{subject.revision + 1}.json"
        ),
        revision_input,
    )
    subject_path = runner.service.workspace / revision_input.subject_ref
    if not subject_path.is_file():
        _write_model(runner, revision_input.subject_ref, subject)
    specialist_id = f"module-{subject.module_id}-specialist"
    revision = subject.revision + 1
    envelope = TaskEnvelope(
        task_id=f"module-revision-r{revision}-{subject.module_id}",
        run_id=state["run_id"],
        agent_id=specialist_id,
        objective=(
            f"以完整模块 {subject.module_id} 的单一作者身份，一次完成所有明确分配的"
            "定向修订；只替换受影响小节，不重复提交未变正文。"
        ),
        input_refs=[input_ref],
        constraints=[
            f"唯一写作范围是模块 {subject.module_id}",
            f"本轮必须在一次 module_revision_submission 中覆盖目标 {sorted(targets)}",
            "submodule_narratives 只包含实际改变的已分配小节；不得修改其他模块或未分配小节",
            "revision_responses 必须逐项且仅覆盖全部分配的 finding ids",
            "disputed 或 needs_input 不得伪造 changed_target_ids",
            *(
                [f"上一版显式机器检查未通过；只修复 {validation_ref} 中列出的谓词失败"]
                if validation_report is not None
                else []
            ),
            *runner._user_supplement_constraints(
                state,
                stage="module_authoring",
                target_ids={
                    subject.module_id,
                    *targets,
                    *(claim.id for claim in subject.claims if claim.submodule_id in targets),
                },
            ),
        ],
        allowed_outputs=["module_revision_submission"],
        revision=revision,
        prior_result_ref=revision_input.subject_ref,
        artifact_delivery_modes={
            input_ref: "inline",
            revision_input.subject_ref: "hash_retained",
        },
        target_submodule_ids=sorted(targets),
        input_contract_kind="module_revision_input",
        input_contract_ref=input_ref,
        # The stable author Skill and domain Knowledge were supplied by the
        # first task of this persistent specialist identity.  This revision
        # carries only the changed business contract.
        inline_context="",
    )
    return ModuleRevisionPreparation(
        run_id=state["run_id"],
        module_id=subject.module_id,
        workflow_id=workflow_id,
        specialist_id=specialist_id,
        session_key=f"module-{subject.module_id}",
        subject=subject,
        revision_input=revision_input,
        input_ref=input_ref,
        subject_ref=revision_input.subject_ref,
        revision=revision,
        target_submodule_ids=sorted(targets),
        required_finding_ids=sorted(required_ids),
        envelope=envelope,
    )


def accept_module_revision(
    runner: "ReportWorkflowRunner",
    *,
    preparation: ModuleRevisionPreparation,
    result: ModuleRevisionSubmission,
) -> tuple[ModuleSubmission, str]:
    """Apply and persist one prepared module-author revision submission."""

    if not isinstance(result, ModuleRevisionSubmission):
        raise ReviewLifecycleError(
            f"module specialist returned the wrong revision type for {preparation.module_id}"
        )
    revised = _apply_module_patch(
        preparation.subject,
        result,
        target_submodule_ids=set(preparation.target_submodule_ids),
        required_finding_ids=set(preparation.required_finding_ids),
    )
    subject_ref = _write_model(
        runner,
        f"Work/runs/{preparation.run_id}/modules/"
        f"{preparation.module_id}-r{preparation.revision}.json",
        revised,
    )
    runner.service.store.write_json(
        (
            f"Work/runs/{preparation.run_id}/reviews/module-diff-"
            f"{preparation.module_id}-r{preparation.revision}.json"
        ),
        build_revision_diff(preparation.subject, revised),
    )
    runner.service.store.write_json(
        (
            f"Work/runs/{preparation.run_id}/reviews/module-revisions/"
            f"{preparation.module_id}/r{preparation.revision}/module-barrier.json"
        ),
        {
            "kind": "module_revision_barrier",
            "version": 1,
            "run_id": preparation.run_id,
            "module_id": preparation.module_id,
            "base_revision": preparation.subject.revision,
            "revision": preparation.revision,
            "target_submodule_ids": sorted(preparation.target_submodule_ids),
            "subject_ref": subject_ref,
            "subject_sha256": hashlib.sha256(
                (runner.service.workspace / subject_ref).read_bytes()
            ).hexdigest(),
        },
    )
    return revised, subject_ref


async def prepare_module_recheck(
    runner: "ReportWorkflowRunner",
    *,
    module_id: str,
    current: ModuleSubmission,
    state: dict,
    workflow_id: str,
    initial_scope: set[str],
    lifecycle_id: str = "initial",
) -> ModuleRecheckPreparation:
    """Prepare the first original-Auditor recheck from persisted revision state.

    The accepted initial finding and the author's revision are already durable at
    this boundary.  A valid candidate therefore only needs the reviewer preflight,
    the bounded delta input, and the original Auditor envelope.  Missing or
    exceptional state deliberately returns ``continue_existing`` so the legacy
    lifecycle remains the recovery path; this helper never invokes an author.
    """

    if current.module_id != module_id:
        raise ReviewLifecycleError("module recheck payload belongs to a different module")
    if not re.fullmatch(r"[a-z0-9-]+", lifecycle_id):
        raise ReviewLifecycleError("module review lifecycle_id is not a safe component")

    run_id = str(state["run_id"])
    review_root = f"Work/runs/{run_id}/reviews/module/{lifecycle_id}/{module_id}"
    progress_ref = f"{review_root}/progress.json"
    progress = _load_progress(runner, progress_ref, ModuleReviewProgress)
    reviewer_session_key = _module_reviewer_session_key(
        runner,
        state=state,
        module_id=module_id,
        lifecycle_id=lifecycle_id,
        progress=progress,
        regression_context=None,
    )

    def continuation(
        *,
        review_round: int = 0,
        scope: set[str] | None = None,
        pending: list[ModuleReviewFinding] | None = None,
        responses: list[RevisionResponse] | None = None,
        finding_refs: list[str] | None = None,
        verdict_refs: list[str] | None = None,
        resolved_ids: set[str] | None = None,
        subject_ref: str | None = None,
        preflight_ref: str | None = None,
    ) -> ModuleRecheckPreparation:
        return ModuleRecheckPreparation(
            mode="continue_existing",
            run_id=run_id,
            module_id=module_id,
            lifecycle_id=lifecycle_id,
            workflow_id=workflow_id,
            reviewer_session_key=reviewer_session_key,
            review_root=review_root,
            progress_ref=progress_ref,
            review_round=review_round,
            scope=sorted(scope or set(initial_scope)),
            current=current,
            pending=list(pending or []),
            responses=list(responses or []),
            finding_refs=list(finding_refs or []),
            verdict_refs=list(verdict_refs or []),
            resolved_ids=sorted(resolved_ids or set()),
            last_reviewed_subject_ref=(
                progress.last_reviewed_subject_ref if progress is not None else None
            ),
            subject_ref=subject_ref,
            progress=progress,
            preflight_ref=preflight_ref,
        )

    if progress is None:
        return continuation()
    if progress.run_id != run_id or progress.module_id != module_id:
        raise ReviewLifecycleError("module review progress identity mismatch")
    if progress.next_action != "revise":
        return continuation(
            review_round=progress.review_round,
            scope=set(progress.scope),
            pending=list(progress.pending),
            responses=list(progress.responses),
            finding_refs=list(progress.finding_refs),
            verdict_refs=list(progress.verdict_refs),
            resolved_ids=set(progress.resolved_ids),
        )

    pending = list(progress.pending)
    if not pending:
        return continuation(
            review_round=progress.review_round,
            scope=set(progress.scope),
            finding_refs=list(progress.finding_refs),
            verdict_refs=list(progress.verdict_refs),
            resolved_ids=set(progress.resolved_ids),
        )

    candidate_ref = f"Work/runs/{run_id}/modules/{module_id}-r{current.revision}.json"
    if not (runner.service.workspace / candidate_ref).is_file():
        return continuation(
            review_round=progress.review_round,
            scope={finding.target_submodule_id for finding in pending},
            pending=pending,
            responses=list(current.revision_responses),
            finding_refs=list(progress.finding_refs),
            verdict_refs=list(progress.verdict_refs),
            resolved_ids=set(progress.resolved_ids),
            subject_ref=candidate_ref,
        )

    scope = {finding.target_submodule_id for finding in pending}
    try:
        _validate_responses(
            current.revision_responses,
            {finding.id for finding in pending},
            scope,
        )
    except ReviewLifecycleError:
        return continuation(
            review_round=progress.review_round,
            scope=scope,
            pending=pending,
            responses=list(current.revision_responses),
            finding_refs=list(progress.finding_refs),
            verdict_refs=list(progress.verdict_refs),
            resolved_ids=set(progress.resolved_ids),
            subject_ref=candidate_ref,
        )
    if any(
        response.action in {"disputed", "needs_input"} for response in current.revision_responses
    ):
        return continuation(
            review_round=progress.review_round,
            scope=scope,
            pending=pending,
            responses=list(current.revision_responses),
            finding_refs=list(progress.finding_refs),
            verdict_refs=list(progress.verdict_refs),
            resolved_ids=set(progress.resolved_ids),
            subject_ref=candidate_ref,
        )

    review_round = progress.review_round + 1
    structure_ref: str
    try:
        structure_ref = runner._validate_module_structure(
            state,
            current,
            f"review-r{review_round}",
        )
        structure_report = ValidationReport.model_validate_json(
            (runner.service.workspace / structure_ref).read_text(encoding="utf-8")
        )
        _require_validation_binding(
            runner,
            structure_report,
            subject_ref=candidate_ref,
            subject_revision=current.revision,
        )
        preflight = evaluate_module_review_preflight(
            runner.service.workspace,
            run_id=run_id,
            subject=current,
            subject_ref=candidate_ref,
            upstream_report=structure_report,
        )
    except (OSError, ValueError, ReviewLifecycleError):
        return continuation(
            review_round=progress.review_round,
            scope=scope,
            pending=pending,
            responses=list(current.revision_responses),
            finding_refs=list(progress.finding_refs),
            verdict_refs=list(progress.verdict_refs),
            resolved_ids=set(progress.resolved_ids),
            subject_ref=candidate_ref,
        )

    signal_ref = _write_model(
        runner,
        f"{review_root}/preflight-subject-r{current.revision}-review-r{review_round}.json",
        preflight.report,
    )
    if not preflight.report.passed:
        return continuation(
            review_round=progress.review_round,
            scope=scope,
            pending=pending,
            responses=list(current.revision_responses),
            finding_refs=list(progress.finding_refs),
            verdict_refs=list(progress.verdict_refs),
            resolved_ids=set(progress.resolved_ids),
            subject_ref=candidate_ref,
            preflight_ref=signal_ref,
        )

    try:
        (
            review_input,
            envelope,
            input_ref,
            baseline_subject_ref,
            _review_diff_ref,
        ) = _build_module_recheck_input_and_envelope(
            runner,
            state=state,
            module_id=module_id,
            current=current,
            pending=pending,
            scope=scope,
            lifecycle_id=lifecycle_id,
            review_round=review_round,
            review_root=review_root,
            subject_ref=candidate_ref,
            last_reviewed_subject_ref=progress.last_reviewed_subject_ref,
            responses=list(current.revision_responses),
            finding_refs=list(progress.finding_refs),
            signal_ref=signal_ref,
            validation_report=preflight.report,
        )
    except (OSError, ValueError, ReviewLifecycleError):
        return continuation(
            review_round=progress.review_round,
            scope=scope,
            pending=pending,
            responses=list(current.revision_responses),
            finding_refs=list(progress.finding_refs),
            verdict_refs=list(progress.verdict_refs),
            resolved_ids=set(progress.resolved_ids),
            subject_ref=candidate_ref,
            preflight_ref=signal_ref,
        )
    _save_module_review_progress(
        runner,
        state=state,
        progress_ref=progress_ref,
        module_id=module_id,
        next_action="review",
        current=current,
        pending=pending,
        responses=list(current.revision_responses),
        finding_refs=list(progress.finding_refs),
        verdict_refs=list(progress.verdict_refs),
        resolved_ids=set(progress.resolved_ids),
        review_round=review_round,
        phase="recheck",
        scope=scope,
        reviewer_session_key=reviewer_session_key,
        last_reviewed_subject_ref=baseline_subject_ref,
    )
    return ModuleRecheckPreparation(
        mode="invoke_agent",
        run_id=run_id,
        module_id=module_id,
        lifecycle_id=lifecycle_id,
        workflow_id=workflow_id,
        reviewer_session_key=reviewer_session_key,
        review_root=review_root,
        progress_ref=progress_ref,
        review_round=review_round,
        scope=sorted(scope),
        current=current,
        pending=pending,
        responses=list(current.revision_responses),
        finding_refs=list(progress.finding_refs),
        verdict_refs=list(progress.verdict_refs),
        resolved_ids=sorted(progress.resolved_ids),
        last_reviewed_subject_ref=baseline_subject_ref,
        subject_ref=candidate_ref,
        review_input_ref=input_ref,
        review_input=review_input,
        envelope=envelope,
        progress=progress,
        preflight_ref=signal_ref,
    )


async def accept_module_recheck(
    runner: "ReportWorkflowRunner",
    *,
    preparation: ModuleRecheckPreparation,
    result: ModuleReviewVerdictSubmission,
    state: dict,
) -> ModuleRecheckAcceptance:
    """Accept one prepared recheck and persist verdicts or continuation state."""

    if preparation.mode != "invoke_agent":
        raise ReviewLifecycleError("cannot accept a module recheck without an Agent invocation")
    if preparation.subject_ref is None:
        raise ReviewLifecycleError("module recheck lacks a subject ref")
    if not isinstance(result, ModuleReviewVerdictSubmission):
        raise ReviewLifecycleError("module auditor returned the wrong recheck type")

    required_ids = {finding.id for finding in preparation.pending}
    _validate_verdicts(result.verdicts, required_ids)
    _validate_module_findings(
        result.new_findings,
        preparation.current,
        set(preparation.scope),
        id_prefix=(
            f"M-{preparation.module_id}-{preparation.lifecycle_id}-r{preparation.review_round}-"
        ),
    )
    verdict_ref = _write_immutable_model(
        runner,
        f"{preparation.review_root}/verdicts-r{preparation.review_round}.json",
        result,
    )
    verdict_refs = [*preparation.verdict_refs, verdict_ref]
    finding_refs = list(preparation.finding_refs)
    resolved_ids = set(preparation.resolved_ids)
    pending_by_id = {finding.id: finding for finding in preparation.pending}
    escalated = [verdict for verdict in result.verdicts if verdict.verdict == "escalate"]
    main_accepts: set[str] = set()
    if escalated:
        decision = await _main_exception_decision(
            runner,
            state=state,
            workflow_id=preparation.workflow_id,
            scope="module",
            subject_refs=[preparation.subject_ref],
            finding_refs=finding_refs,
            verdicts=escalated,
            responses=preparation.responses,
        )
        if decision.decision == "accept_dispute":
            main_accepts = set(decision.finding_ids)
    next_pending = {
        verdict.finding_id: pending_by_id[verdict.finding_id]
        for verdict in result.verdicts
        if verdict.verdict == "open"
        or (verdict.verdict == "escalate" and verdict.finding_id not in main_accepts)
    }
    resolved_ids.update(
        verdict.finding_id
        for verdict in result.verdicts
        if verdict.verdict == "resolved" or verdict.finding_id in main_accepts
    )
    for finding in result.new_findings:
        if finding.id in required_ids or finding.id in resolved_ids:
            raise ReviewLifecycleError(f"new module finding reuses an existing id: {finding.id}")
        next_pending[finding.id] = finding
    if result.new_findings:
        finding_ref = _write_immutable_model(
            runner,
            f"{preparation.review_root}/regression-findings-r{preparation.review_round}.json",
            ModuleReviewFindingSubmission(
                coverage=result.coverage,
                findings=result.new_findings,
            ),
        )
        finding_refs.append(finding_ref)

    if next_pending:
        _save_module_review_progress(
            runner,
            state=state,
            progress_ref=preparation.progress_ref,
            module_id=preparation.module_id,
            next_action="revise",
            current=preparation.current,
            pending=next_pending.values(),
            responses=preparation.responses,
            finding_refs=finding_refs,
            verdict_refs=verdict_refs,
            resolved_ids=resolved_ids,
            review_round=preparation.review_round,
            phase="recheck",
            scope=set(preparation.scope),
            reviewer_session_key=preparation.reviewer_session_key,
            last_reviewed_subject_ref=preparation.subject_ref,
        )
        next_action: Literal["continue_existing", "completed"] = "continue_existing"
        completion_ref = None
    else:
        completion_ref = _module_review_completion(
            runner,
            state=state,
            module=preparation.current,
            reviewer_session_key=preparation.reviewer_session_key,
            subject_ref=preparation.subject_ref,
            finding_refs=finding_refs,
            verdict_refs=verdict_refs,
            resolved_ids=resolved_ids,
            lifecycle_id=preparation.lifecycle_id,
        )
        _save_module_review_progress(
            runner,
            state=state,
            progress_ref=preparation.progress_ref,
            module_id=preparation.module_id,
            next_action="completed",
            current=preparation.current,
            pending=[],
            responses=preparation.responses,
            finding_refs=finding_refs,
            verdict_refs=verdict_refs,
            resolved_ids=resolved_ids,
            review_round=preparation.review_round,
            phase="recheck",
            scope=set(preparation.scope),
            reviewer_session_key=preparation.reviewer_session_key,
            last_reviewed_subject_ref=preparation.subject_ref,
        )
        next_action = "completed"
    return ModuleRecheckAcceptance(
        run_id=preparation.run_id,
        module_id=preparation.module_id,
        lifecycle_id=preparation.lifecycle_id,
        reviewer_session_key=preparation.reviewer_session_key,
        subject_ref=preparation.subject_ref,
        current=preparation.current,
        findings=list(next_pending.values()),
        finding_refs=finding_refs,
        verdict_refs=verdict_refs,
        resolved_ids=sorted(resolved_ids),
        next_action=next_action,
        progress_ref=preparation.progress_ref,
        completion_ref=completion_ref,
    )


async def request_module_revision(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    workflow_id: str,
    subject: ModuleSubmission,
    module_findings: list[ModuleReviewFinding] | None = None,
    cross_findings: list[CrossReviewFinding] | None = None,
    requested_changes: list[RequestedModuleChange] | None = None,
    validation_ref: str | None = None,
    validation_target_submodule_ids: set[str] | None = None,
) -> tuple[ModuleSubmission, str]:
    """Revise one complete module in one explicit author submission."""

    preparation = await prepare_module_revision(
        runner,
        state=state,
        workflow_id=workflow_id,
        subject=subject,
        module_findings=module_findings,
        cross_findings=cross_findings,
        requested_changes=requested_changes,
        validation_ref=validation_ref,
        validation_target_submodule_ids=validation_target_submodule_ids,
    )
    patch = await runner._agent(
        preparation.specialist_id,
        preparation.envelope,
        preparation.envelope.input_refs,
        workflow_id,
        session_key=preparation.session_key,
    )
    return accept_module_revision(
        runner,
        preparation=preparation,
        result=patch,
    )


def _module_review_completion(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    module: ModuleSubmission,
    reviewer_session_key: str,
    subject_ref: str,
    finding_refs: list[str],
    verdict_refs: list[str],
    resolved_ids: set[str],
    lifecycle_id: str,
) -> str:
    completion = ReviewCompletionRecord(
        review_protocol_version=2,
        lifecycle="module",
        run_id=state["run_id"],
        reviewer_agent_id="evidence-auditor",
        reviewer_session_key=reviewer_session_key,
        subject_refs=[subject_ref],
        finding_refs=finding_refs,
        verdict_refs=verdict_refs,
        resolved_finding_ids=sorted(resolved_ids),
    )
    ref = _write_immutable_model(
        runner,
        (
            f"Work/runs/{state['run_id']}/reviews/module/{lifecycle_id}/"
            f"{module.module_id}/completion-r{module.revision}.json"
        ),
        completion,
    )
    state.setdefault("module_review_completion_refs", {})[module.module_id] = ref
    runner.service.store.write_text(f"Outputs/Modules/{module.module_id}.md", module.markdown)
    return ref


def accept_module_initial_review(
    runner: "ReportWorkflowRunner",
    *,
    preparation: ModuleInitialReviewPreparation,
    result: ModuleReviewFindingSubmission,
    state: dict,
) -> ModuleInitialReviewAcceptance:
    """Accept and persist one prepared initial Auditor result."""

    if preparation.mode != "invoke_agent":
        raise ReviewLifecycleError(
            "cannot accept an initial module review without an Agent invocation"
        )
    if preparation.subject_ref is None:
        raise ReviewLifecycleError("initial module review lacks a subject ref")
    if not isinstance(result, ModuleReviewFindingSubmission):
        raise ReviewLifecycleError("module auditor returned the wrong initial type")
    phase = preparation.review_input.phase if preparation.review_input is not None else "initial"
    scope = set(preparation.scope)
    if not scope.issubset(set(result.coverage.submodule_ids)):
        raise ReviewLifecycleError("initial module review coverage omitted assigned submodules")
    _validate_module_findings(
        result.findings,
        preparation.current,
        scope,
        id_prefix=(
            f"M-{preparation.module_id}-{preparation.lifecycle_id}-r{preparation.review_round}-"
        ),
    )
    review_ref = _write_immutable_model(
        runner,
        f"{preparation.review_root}/findings-r{preparation.review_round}.json",
        result,
    )
    finding_refs = [review_ref]
    pending = {finding.id: finding for finding in result.findings}
    completion_ref: str | None = None
    if not pending:
        completion_ref = _module_review_completion(
            runner,
            state=state,
            module=preparation.current,
            reviewer_session_key=preparation.reviewer_session_key,
            subject_ref=preparation.subject_ref,
            finding_refs=finding_refs,
            verdict_refs=[],
            resolved_ids=set(),
            lifecycle_id=preparation.lifecycle_id,
        )
        _save_module_review_progress(
            runner,
            state=state,
            progress_ref=preparation.progress_ref,
            module_id=preparation.module_id,
            next_action="completed",
            current=preparation.current,
            pending=[],
            responses=[],
            finding_refs=finding_refs,
            verdict_refs=[],
            resolved_ids=set(),
            review_round=preparation.review_round,
            phase=phase,
            scope=scope,
            reviewer_session_key=preparation.reviewer_session_key,
            last_reviewed_subject_ref=preparation.subject_ref,
        )
        next_action: Literal["revise", "completed"] = "completed"
    else:
        _save_module_review_progress(
            runner,
            state=state,
            progress_ref=preparation.progress_ref,
            module_id=preparation.module_id,
            next_action="revise",
            current=preparation.current,
            pending=pending.values(),
            responses=[],
            finding_refs=finding_refs,
            verdict_refs=[],
            resolved_ids=set(),
            review_round=preparation.review_round,
            phase=phase,
            scope=scope,
            reviewer_session_key=preparation.reviewer_session_key,
            last_reviewed_subject_ref=preparation.subject_ref,
        )
        next_action = "revise"
    return ModuleInitialReviewAcceptance(
        run_id=preparation.run_id,
        module_id=preparation.module_id,
        lifecycle_id=preparation.lifecycle_id,
        reviewer_session_key=preparation.reviewer_session_key,
        subject_ref=preparation.subject_ref,
        current=preparation.current,
        findings=list(result.findings),
        finding_refs=finding_refs,
        verdict_refs=[],
        resolved_ids=[],
        next_action=next_action,
        progress_ref=preparation.progress_ref,
        completion_ref=completion_ref,
    )


async def _run_module_review_lifecycle(
    runner: "ReportWorkflowRunner",
    module_id: str,
    payload: ModuleSubmission,
    state: dict,
    workflow_id: str,
    *,
    initial_scope: set[str],
    lifecycle_id: str,
    regression_context: ModuleLocalRegressionContext | None = None,
) -> ModuleSubmission:
    """Run module-local finding/response/verdict closure with one reviewer session."""

    if payload.module_id != module_id:
        raise ReviewLifecycleError("module review payload belongs to a different module")
    if not re.fullmatch(r"[a-z0-9-]+", lifecycle_id):
        raise ReviewLifecycleError("module review lifecycle_id is not a safe component")
    current = payload
    pending: dict[str, ModuleReviewFinding] = {}
    responses: list[RevisionResponse] = []
    finding_refs: list[str] = []
    verdict_refs: list[str] = []
    resolved_ids: set[str] = set()
    review_round = 0
    phase = "local_regression" if regression_context is not None else "initial"
    scope = set(initial_scope)
    last_reviewed_subject_ref: str | None = None
    review_root = f"Work/runs/{state['run_id']}/reviews/module/{lifecycle_id}/{module_id}"
    progress_ref = f"{review_root}/progress.json"

    def save_progress(next_action: str) -> None:
        _save_module_review_progress(
            runner,
            state=state,
            progress_ref=progress_ref,
            module_id=module_id,
            next_action=next_action,
            current=current,
            pending=pending.values(),
            responses=responses,
            finding_refs=finding_refs,
            verdict_refs=verdict_refs,
            resolved_ids=resolved_ids,
            review_round=review_round,
            phase=phase,
            scope=scope,
            reviewer_session_key=reviewer_session_key,
            last_reviewed_subject_ref=last_reviewed_subject_ref,
        )

    progress = (
        _load_progress(runner, progress_ref, ModuleReviewProgress) if state.get("resume") else None
    )
    reviewer_session_key = _module_reviewer_session_key(
        runner,
        state=state,
        module_id=module_id,
        lifecycle_id=lifecycle_id,
        progress=progress,
        regression_context=regression_context,
    )
    if progress is not None and progress.next_action == "completed":
        # A verified completion is terminal.  Resume must return the persisted
        # typed subject without asking the Provider to repeat the auditor
        # finding/recheck sequence.  If the completion record is absent or
        # malformed, treat the progress marker as stale and continue through
        # the normal deterministic validation path.
        completion_ref = state.get("module_review_completion_refs", {}).get(module_id)
        if completion_ref is None:
            completion_ref = f"{review_root}/completion-r{progress.current.revision}.json"
        completion_path = runner.service.workspace / completion_ref
        if completion_path.is_file():
            try:
                completion = ReviewCompletionRecord.model_validate_json(
                    completion_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise ReviewLifecycleError(
                    f"completed module review marker is unreadable: {completion_ref}"
                ) from exc
            if (
                completion.lifecycle != "module"
                or completion.run_id != state["run_id"]
                or completion.reviewer_session_key != reviewer_session_key
                or completion.subject_refs
                != [
                    f"Work/runs/{state['run_id']}/modules/"
                    f"{module_id}-r{progress.current.revision}.json"
                ]
            ):
                raise ReviewLifecycleError(
                    "completed module review marker does not bind the current subject"
                )
            state.setdefault("module_review_completion_refs", {})[module_id] = str(completion_ref)
            return progress.current
    if progress is not None and progress.next_action != "completed":
        if progress.run_id != state["run_id"] or progress.module_id != module_id:
            raise ReviewLifecycleError("module review progress identity mismatch")
        current = progress.current
        pending = {finding.id: finding for finding in progress.pending}
        responses = progress.responses
        finding_refs = progress.finding_refs
        verdict_refs = progress.verdict_refs
        resolved_ids = set(progress.resolved_ids)
        review_round = progress.review_round
        phase = progress.phase
        scope = set(progress.scope)
        last_reviewed_subject_ref = progress.last_reviewed_subject_ref
        if progress.next_action == "revise":
            candidate_path = runner.service.workspace / (
                f"Work/runs/{state['run_id']}/modules/{module_id}-r{current.revision + 1}.json"
            )
            candidate = None
            if candidate_path.is_file():
                try:
                    candidate = ModuleSubmission.model_validate_json(
                        candidate_path.read_text(encoding="utf-8")
                    )
                    _validate_responses(
                        candidate.revision_responses,
                        set(pending),
                        {finding.target_submodule_id for finding in pending.values()},
                    )
                except (OSError, ValueError):
                    candidate = None
            if candidate is None:
                current, _ = await request_module_revision(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    subject=current,
                    module_findings=list(pending.values()),
                )
            else:
                current = candidate
            responses = current.revision_responses
            exceptional = [
                response for response in responses if response.action in {"disputed", "needs_input"}
            ]
            if exceptional:
                decision = await _main_exception_decision(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    scope="module",
                    subject_refs=[
                        f"Work/runs/{state['run_id']}/modules/{module_id}-r{current.revision}.json"
                    ],
                    finding_refs=finding_refs,
                    verdicts=[],
                    responses=exceptional,
                    trigger="author_response",
                )
                if decision.decision == "return_to_author":
                    current, _ = await request_module_revision(
                        runner,
                        state=state,
                        workflow_id=workflow_id,
                        subject=current,
                        module_findings=list(pending.values()),
                    )
                    responses = current.revision_responses
            scope = {finding.target_submodule_id for finding in pending.values()}
            phase = "recheck"
            review_round += 1
            save_progress("review")

    while True:
        preflight_attempts = 0
        preflight_failure_fingerprints: dict[tuple, int] = {}
        while True:
            subject_ref = (
                f"Work/runs/{state['run_id']}/modules/{module_id}-r{current.revision}.json"
            )
            if not (runner.service.workspace / subject_ref).is_file():
                _write_model(runner, subject_ref, current)
            structure_ref = runner._validate_module_structure(
                state,
                current,
                f"review-r{review_round}",
            )
            structure_report = ValidationReport.model_validate_json(
                (runner.service.workspace / structure_ref).read_text(encoding="utf-8")
            )
            _require_validation_binding(
                runner,
                structure_report,
                subject_ref=subject_ref,
                subject_revision=current.revision,
            )
            preflight = evaluate_module_review_preflight(
                runner.service.workspace,
                run_id=state["run_id"],
                subject=current,
                subject_ref=subject_ref,
                upstream_report=structure_report,
            )
            signal_ref = _write_model(
                runner,
                (
                    f"{review_root}/preflight-subject-r{current.revision}-"
                    f"review-r{review_round}.json"
                ),
                preflight.report,
            )
            validation_report = preflight.report
            if validation_report.passed:
                break
            preflight_attempts += 1
            fingerprint = tuple(
                sorted(
                    (
                        failure.check_id,
                        failure.target_path,
                        failure.message,
                    )
                    for failure in validation_report.failures
                )
            )
            repeated = preflight_failure_fingerprints.get(fingerprint, 0) + 1
            preflight_failure_fingerprints[fingerprint] = repeated
            if repeated >= 2 or preflight_attempts >= 3:
                raise ReviewLifecycleError(
                    "module preflight failed repeatedly before semantic review; "
                    "no reviewer finding or verdict was created. "
                    f"module={module_id}; attempts={preflight_attempts}; "
                    f"validation_ref={signal_ref}"
                )
            current, _ = await request_module_revision(
                runner,
                state=state,
                workflow_id=workflow_id,
                subject=current,
                module_findings=(list(pending.values()) if phase == "recheck" else []),
                cross_findings=(
                    regression_context.trigger_cross_findings
                    if phase == "local_regression" and regression_context is not None
                    else []
                ),
                validation_ref=signal_ref,
                validation_target_submodule_ids=set(preflight.target_submodule_ids),
            )
            if phase == "recheck":
                responses = current.revision_responses
            save_progress("review")

        if phase == "recheck":
            (
                review_input,
                envelope,
                input_ref,
                baseline_subject_ref,
                revision_diff_ref,
            ) = _build_module_recheck_input_and_envelope(
                runner,
                state=state,
                module_id=module_id,
                current=current,
                pending=pending.values(),
                scope=scope,
                lifecycle_id=lifecycle_id,
                review_round=review_round,
                review_root=review_root,
                subject_ref=subject_ref,
                last_reviewed_subject_ref=last_reviewed_subject_ref,
                responses=responses,
                finding_refs=finding_refs,
                signal_ref=signal_ref,
                validation_report=validation_report,
            )
        else:
            review_subject = module_content_view(current, scope)
            review_claim_statements = [
                _review_claim_statement(claim)
                for claim in current.claims
                if claim.submodule_id in scope
            ]
            knowledge_ref, knowledge_context = _module_review_knowledge_packet(
                runner,
                state,
                module_id,
                scope,
            )
            if phase != "initial":
                knowledge_context = ""
            review_input = ModuleReviewInput(
                phase=phase,
                run_id=state["run_id"],
                module_id=module_id,
                lifecycle_id=lifecycle_id,
                review_round=review_round,
                subject_ref=subject_ref,
                subject_revision=current.revision,
                subject=review_subject,
                claim_statements=review_claim_statements,
                prior_claim_statements=[],
                unchanged_submodule_sha256={},
                unchanged_statement_sha256={},
                knowledge_ref=knowledge_ref,
                knowledge_context=knowledge_context,
                evidence=_module_review_evidence_packet(
                    runner,
                    current,
                    state["run_id"],
                    scope,
                    evidence_ids=None,
                ),
                required_submodule_ids=sorted(scope),
                required_findings=[],
                revision_responses=[],
                prior_review_completion_ref=(
                    regression_context.prior_review_completion_ref
                    if phase == "local_regression" and regression_context is not None
                    else None
                ),
                prior_review_completion=(
                    regression_context.prior_review_completion
                    if phase == "local_regression" and regression_context is not None
                    else None
                ),
                baseline_subject_ref=(
                    regression_context.baseline_subject_ref
                    if phase == "local_regression" and regression_context is not None
                    else None
                ),
                trigger_cross_findings=(
                    regression_context.trigger_cross_findings
                    if phase == "local_regression" and regression_context is not None
                    else []
                ),
                trigger_revision_responses=(
                    regression_context.trigger_revision_responses
                    if phase == "local_regression" and regression_context is not None
                    else []
                ),
                revision_diff_ref=(
                    regression_context.revision_diff_ref
                    if phase == "local_regression" and regression_context is not None
                    else None
                ),
                revision_diff=(
                    regression_context.revision_diff
                    if phase == "local_regression" and regression_context is not None
                    else None
                ),
                validation_report_ref=signal_ref,
                validation_report=validation_report,
            )
            input_ref = _write_model(
                runner,
                f"{review_root}/input-r{review_round}.json",
                review_input,
            )
            output_kind = "module_review_finding_submission"
            envelope = TaskEnvelope(
                task_id=f"module-{module_id}-{lifecycle_id}-review-r{review_round}",
                run_id=state["run_id"],
                agent_id="evidence-auditor",
                objective=(
                    f"审查模块 {module_id} 的当前正文、Claim 与证据边界。"
                    if phase == "initial"
                    else (
                        f"由模块 {module_id} 的原审查者仅检查 Cross 回改范围、diff、"
                        "Claim 与证据回归。"
                    )
                ),
                input_refs=[input_ref],
                constraints=[
                    "coverage 记录实际检查范围，不是批准状态",
                    "一次返回整个模块检查范围的 findings/verdicts；小节 id 只用于定位问题，"
                    "不得拆成独立小节级审查任务或会话",
                    "finding 首次提出后不可改写；复审不得复述旧 finding",
                    "finding id 由运行时按 lifecycle 和 review round 分配，审查员不得提交或猜测 id",
                    "advisory 与 blocking 都必须获得作者响应和 reviewer verdict",
                    (
                        "首轮必须覆盖 input 中全部 required_submodule_ids"
                        if phase in {"initial", "local_regression"}
                        else "verdicts 必须逐项且仅覆盖 required_findings；new_findings 只允许真实回归"
                    ),
                    *(
                        [
                            "这是原模块审查者的 local_regression，不得重新审查未修改小节",
                            "只根据 prior completion、Cross finding/作者响应、revision diff、目标 Claim/证据和当前 validation 提出真实回归 finding",
                        ]
                        if phase == "local_regression"
                        else []
                    ),
                    *runner._user_supplement_constraints(
                        state,
                        stage="module_review",
                        target_ids={
                            module_id,
                            *scope,
                            *(claim.id for claim in current.claims),
                        },
                    ),
                ],
                allowed_outputs=[output_kind],
                revision=review_round,
                prior_result_ref=finding_refs[-1] if finding_refs else None,
                artifact_delivery_modes=_module_review_artifact_delivery_modes(
                    input_ref,
                    finding_refs,
                ),
                target_submodule_ids=sorted(scope),
                input_contract_kind="module_review_input",
                input_contract_ref=input_ref,
                inline_context=runner._template_skill_context(state, f"auditor-{module_id}"),
                allowed_tools=["submit_result"],
            )
        result = await runner._agent(
            "evidence-auditor",
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key=reviewer_session_key,
        )
        # This exact subject, not merely the immediately preceding revision, is
        # the semantic baseline for the next paid reviewer turn. Machine-only
        # corrections may introduce additional revisions between the two.
        last_reviewed_subject_ref = subject_ref
        if phase in {"initial", "local_regression"}:
            if not isinstance(result, ModuleReviewFindingSubmission):
                raise ReviewLifecycleError("module auditor returned the wrong initial type")
            if not scope.issubset(set(result.coverage.submodule_ids)):
                raise ReviewLifecycleError(
                    "initial module review coverage omitted assigned submodules"
                )
            _validate_module_findings(
                result.findings,
                current,
                scope,
                id_prefix=f"M-{module_id}-{lifecycle_id}-r{review_round}-",
            )
            review_ref = _write_immutable_model(
                runner,
                f"{review_root}/findings-r{review_round}.json",
                result,
            )
            finding_refs.append(review_ref)
            pending = {finding.id: finding for finding in result.findings}
        else:
            if not isinstance(result, ModuleReviewVerdictSubmission):
                raise ReviewLifecycleError("module auditor returned the wrong recheck type")
            required_ids = set(pending)
            _validate_verdicts(result.verdicts, required_ids)
            _validate_module_findings(
                result.new_findings,
                current,
                scope,
                id_prefix=f"M-{module_id}-{lifecycle_id}-r{review_round}-",
            )
            verdict_ref = _write_immutable_model(
                runner,
                f"{review_root}/verdicts-r{review_round}.json",
                result,
            )
            verdict_refs.append(verdict_ref)
            escalated = [verdict for verdict in result.verdicts if verdict.verdict == "escalate"]
            main_accepts: set[str] = set()
            if escalated:
                decision = await _main_exception_decision(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    scope="module",
                    subject_refs=[subject_ref],
                    finding_refs=finding_refs,
                    verdicts=escalated,
                    responses=responses,
                )
                if decision.decision == "accept_dispute":
                    main_accepts = set(decision.finding_ids)
            next_pending = {
                verdict.finding_id: pending[verdict.finding_id]
                for verdict in result.verdicts
                if verdict.verdict == "open"
                or (verdict.verdict == "escalate" and verdict.finding_id not in main_accepts)
            }
            resolved_ids.update(
                verdict.finding_id
                for verdict in result.verdicts
                if verdict.verdict == "resolved" or verdict.finding_id in main_accepts
            )
            for finding in result.new_findings:
                if finding.id in pending or finding.id in resolved_ids:
                    raise ReviewLifecycleError(
                        f"new module finding reuses an existing id: {finding.id}"
                    )
                next_pending[finding.id] = finding
            if result.new_findings:
                new_ref = _write_immutable_model(
                    runner,
                    f"{review_root}/regression-findings-r{review_round}.json",
                    ModuleReviewFindingSubmission(
                        coverage=result.coverage,
                        findings=result.new_findings,
                    ),
                )
                finding_refs.append(new_ref)
            pending = next_pending

        if not pending:
            _module_review_completion(
                runner,
                state=state,
                module=current,
                reviewer_session_key=reviewer_session_key,
                subject_ref=subject_ref,
                finding_refs=finding_refs,
                verdict_refs=verdict_refs,
                resolved_ids=resolved_ids,
                lifecycle_id=lifecycle_id,
            )
            save_progress("completed")
            return current

        save_progress("revise")
        while True:
            current, current_ref = await request_module_revision(
                runner,
                state=state,
                workflow_id=workflow_id,
                subject=current,
                module_findings=list(pending.values()),
            )
            responses = current.revision_responses
            exceptional = [
                response for response in responses if response.action in {"disputed", "needs_input"}
            ]
            if not exceptional:
                break
            decision = await _main_exception_decision(
                runner,
                state=state,
                workflow_id=workflow_id,
                scope="module",
                subject_refs=[current_ref],
                finding_refs=finding_refs,
                verdicts=[],
                responses=exceptional,
                trigger="author_response",
            )
            if decision.decision != "return_to_author":
                break
        scope = {finding.target_submodule_id for finding in pending.values()}
        phase = "recheck"
        review_round += 1
        save_progress("review")


async def run_module_review(
    runner: "ReportWorkflowRunner",
    module_id: str,
    payload: ModuleSubmission,
    state: dict,
    workflow_id: str,
    *,
    initial_scope: set[str],
    lifecycle_id: str,
    regression_context: ModuleLocalRegressionContext | None = None,
) -> ModuleSubmission:
    """Run the module review lifecycle through the reusable initial boundary."""

    if regression_context is not None:
        return await _run_module_review_lifecycle(
            runner,
            module_id,
            payload,
            state,
            workflow_id,
            initial_scope=initial_scope,
            lifecycle_id=lifecycle_id,
            regression_context=regression_context,
        )

    progress_ref = (
        f"Work/runs/{state['run_id']}/reviews/module/{lifecycle_id}/{module_id}/progress.json"
    )
    persisted_progress = (
        _load_progress(runner, progress_ref, ModuleReviewProgress) if state.get("resume") else None
    )
    if persisted_progress is not None:
        return await _run_module_review_lifecycle(
            runner,
            module_id,
            payload,
            state,
            workflow_id,
            initial_scope=initial_scope,
            lifecycle_id=lifecycle_id,
        )

    preparation = await prepare_module_initial_review(
        runner,
        module_id=module_id,
        payload=payload,
        state=state,
        workflow_id=workflow_id,
        initial_scope=initial_scope,
        lifecycle_id=lifecycle_id,
    )
    if preparation.mode == "continue_existing":
        return await _run_module_review_lifecycle(
            runner,
            module_id,
            payload,
            state,
            workflow_id,
            initial_scope=initial_scope,
            lifecycle_id=lifecycle_id,
        )
    if preparation.envelope is None:
        raise ReviewLifecycleError("initial module review lacks an Agent envelope")
    result = await runner._agent(
        "evidence-auditor",
        preparation.envelope,
        preparation.envelope.input_refs,
        workflow_id,
        session_key=preparation.reviewer_session_key,
    )
    acceptance = accept_module_initial_review(
        runner,
        preparation=preparation,
        result=result,
        state=state,
    )
    if acceptance.next_action == "completed":
        return acceptance.current

    # Continue the pre-existing author/recheck lifecycle from the persisted
    # finding boundary without changing the caller's original resume marker.
    had_resume_marker = "resume" in state
    original_resume_marker = state.get("resume")
    continuation_state = deepcopy(state)
    continuation_state["resume"] = True
    try:
        return await _run_module_review_lifecycle(
            runner,
            module_id,
            acceptance.current,
            continuation_state,
            workflow_id,
            initial_scope=initial_scope,
            lifecycle_id=lifecycle_id,
        )
    finally:
        state.clear()
        state.update(continuation_state)
        if had_resume_marker:
            state["resume"] = original_resume_marker
        else:
            state.pop("resume", None)


def _validate_cross_findings(
    findings: list[CrossReviewFinding],
    modules: dict[str, ModuleSubmission],
) -> None:
    _unique_ids((finding.id for finding in findings), label="cross findings")


def _validate_cross_owner_synthesis_namespace(
    owner_module_id: str,
    result: CrossOwnerFindingSubmission,
) -> None:
    invalid_synthesis_ids = [
        item.id
        for item in result.synthesis_inputs
        if not item.id.startswith(f"SI-{owner_module_id}-")
    ]
    if invalid_synthesis_ids:
        raise ReviewLifecycleError(
            f"Cross owner {owner_module_id} synthesis ids must use its owner namespace: "
            f"{invalid_synthesis_ids}"
        )


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
    findings: list[CrossReviewFinding]
    finding_refs: list[str]
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
    prior_completion_ref: str | None = None
    revised: ModuleSubmission
    candidate_ref: str


class CrossOwnerLocalReviewPreparation(StrictModel):
    """Typed boundary before the original module Auditor local regression."""

    mode: Literal["invoke_agent", "continue_existing"]
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
    review: ModuleInitialReviewAcceptance


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


_CROSS_OWNER_MODULE_IDS = tuple(REPORT_TAXONOMY)


def _cross_owner_related_view(
    runner: "ReportWorkflowRunner",
    *,
    module: ModuleSubmission,
    ref: str,
) -> CrossOwnerRelatedModuleView:
    digest = hashlib.sha256((runner.service.workspace / ref).read_bytes()).hexdigest()
    return CrossOwnerRelatedModuleView(
        module_id=module.module_id,
        revision=module.revision,
        subject_ref=ref,
        subject_sha256=digest,
        submodule_ids=sorted(module.submodule_narratives),
        claims=list(module.claims),
        evidence_ids_by_submodule={
            submodule_id: sorted(
                {
                    evidence_id
                    for claim in module.claims
                    if claim.submodule_id == submodule_id
                    for evidence_id in claim.source_ids
                    if evidence_id.startswith("E-")
                }
            )
            for submodule_id in sorted(module.submodule_narratives)
        },
        unresolved_questions=list(module.unresolved_questions),
    )


def _cross_owner_input(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    modules: dict[str, ModuleSubmission],
    owner_module_id: str,
    phase: Literal["initial", "recheck"],
    review_round: int,
    required_findings: list[CrossReviewFinding] | None = None,
    revision_responses: list[RevisionResponse] | None = None,
    local_review_ref: str | None = None,
    prior_synthesis_inputs: list[CrossSynthesisInput] | None = None,
) -> tuple[CrossOwnerInput, str]:
    """Build and persist one owner-complete/four-relation Cross input."""

    owner = modules[owner_module_id]
    owner_ref = f"Work/runs/{state['run_id']}/modules/{owner_module_id}-r{owner.revision}.json"
    related_refs: dict[str, str] = {}
    related_revisions: dict[str, int] = {}
    related_hashes: dict[str, str] = {}
    related_views: dict[str, CrossOwnerRelatedModuleView] = {}
    for module_id in _CROSS_OWNER_MODULE_IDS:
        if module_id == owner_module_id:
            continue
        related = modules[module_id]
        ref = f"Work/runs/{state['run_id']}/modules/{module_id}-r{related.revision}.json"
        view = _cross_owner_related_view(runner, module=related, ref=ref)
        related_refs[module_id] = ref
        related_revisions[module_id] = related.revision
        related_hashes[module_id] = view.subject_sha256
        related_views[module_id] = view
    contract = CrossOwnerInput(
        phase=phase,
        run_id=state["run_id"],
        review_round=review_round,
        owner_module_id=owner_module_id,
        review_focus=list(cross_lane_specialization(owner_module_id).review_focus),
        owner_subject_ref=owner_ref,
        owner_subject_revision=owner.revision,
        owner_subject=module_content_view(owner),
        related_module_refs=related_refs,
        related_module_revisions=related_revisions,
        related_module_sha256=related_hashes,
        related_module_views=related_views,
        required_findings=required_findings or [],
        revision_responses=revision_responses or [],
        prior_synthesis_inputs=prior_synthesis_inputs or [],
        local_regression_review_ref=local_review_ref,
    )
    ref = _write_model(
        runner,
        f"Work/runs/{state['run_id']}/reviews/cross-owner-input-r{review_round}-"
        f"{owner_module_id}.json",
        contract,
    )
    return contract, ref


def _cross_owner_artifact_ref(
    runner: "ReportWorkflowRunner",
    ref: str,
) -> ArtifactRef:
    path = (runner.service.workspace / ref).resolve()
    if not path.is_relative_to(runner.service.workspace) or not path.is_file():
        raise ReviewLifecycleError(f"Cross owner artifact is missing: {ref}")
    content = path.read_bytes()
    return ArtifactRef(
        ref=ref,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        media_type="application/json",
    )


def _cross_owner_artifact_path(
    runner: "ReportWorkflowRunner",
    ref: str,
    *,
    run_id: str,
    label: str,
    require_json: bool = True,
) -> Path:
    """Resolve one Cross artifact using business scope, not its digest.

    ArtifactRef hashes are retained as forensic metadata, but current-run
    recovery must decide from the typed business record and its path scope.
    This helper therefore checks only canonical workspace/run containment,
    readability, and (for active JSON artifacts) JSON syntax.
    """

    path = (runner.service.workspace / ref).resolve()
    run_root = (runner.service.workspace / f"Work/runs/{run_id}").resolve()
    if (
        not path.is_relative_to(runner.service.workspace)
        or not path.is_relative_to(run_root)
        or not path.is_file()
    ):
        raise ReviewLifecycleError(f"Cross owner {label} is unreadable or out of scope: {ref}")
    if require_json:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ReviewLifecycleError(f"Cross owner {label} is invalid JSON: {ref}") from exc
    return path


def _cross_owner_artifact_ref_value(value: ArtifactRef | str | None) -> str | None:
    """Return a legacy/current artifact's logical ref without inspecting hashes."""

    if value is None:
        return None
    if isinstance(value, ArtifactRef):
        return value.ref
    if isinstance(value, str) and value:
        return value
    return None


def _cross_owner_completion_business_key(
    completion: CrossOwnerCompletion,
) -> dict:
    """Project a completion to stable business identity.

    ``sha256``, ``size``, lease/task metadata, and schema-version migration are
    intentionally omitted.  The logical artifact refs, stage, owner, round,
    and subject revision remain part of semantic equality.
    """

    payload = completion.model_dump(mode="json")
    for field in (
        "owner_input",
        "initial_result",
        "verdict_result",
        "subject",
        "local_review_completion",
    ):
        payload[field] = _cross_owner_artifact_ref_value(getattr(completion, field))
    for field in (
        "semantic_key",
        "author_task_attempt_id",
        "reviewer_session_id",
        "lease_epoch",
        "schema_version",
    ):
        payload.pop(field, None)
    return payload


def _cross_owner_completion_business_equal(
    left: CrossOwnerCompletion,
    right: CrossOwnerCompletion,
) -> bool:
    return _cross_owner_completion_business_key(left) == _cross_owner_completion_business_key(right)


def _require_cross_owner_artifact(
    runner: "ReportWorkflowRunner",
    declared: ArtifactRef,
    *,
    label: str,
    run_id: str | None = None,
) -> None:
    # Hash/size fields are legacy metadata.  Recovery only requires a
    # canonical, current-run, readable JSON artifact.
    _cross_owner_artifact_path(
        runner,
        declared.ref,
        run_id=run_id or declared.ref.split("/runs/", 1)[-1].split("/", 1)[0],
        label=label,
    )


def _cross_owner_subject_ref(
    run_id: str,
    module_id: str,
    revision: int,
) -> str:
    return f"Work/runs/{run_id}/modules/{module_id}-r{revision}.json"


def _cross_owner_ref_revision(ref: str) -> int | None:
    match = re.search(r"-r([0-9]+)\.json$", ref)
    return int(match.group(1)) if match is not None else None


def _validate_cross_owner_lane_trigger(
    runner: "ReportWorkflowRunner",
    completion: CrossOwnerCompletion,
    *,
    run_id: str,
    owner_module_id: str,
) -> None:
    """Validate the typed artifact that triggered one owner revision lane.

    The first owner revision is triggered by the immutable ``CrossOwnerInput``.
    A later revision is triggered by the immediately preceding owner verdict
    that left findings open or introduced regressions.  Pipeline promotion
    keeps that final lane trigger while normalizing the outer barrier round to
    one, so recovery must validate the actual typed union written by the lane.
    """

    trigger_ref = _cross_owner_artifact_ref_value(completion.owner_input)
    if trigger_ref is None:
        raise ReviewLifecycleError(
            f"Cross owner completion has no typed lane trigger: {owner_module_id}"
        )
    trigger_path = _cross_owner_artifact_path(
        runner,
        trigger_ref,
        run_id=run_id,
        label="lane trigger",
    )
    try:
        raw = json.loads(trigger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ReviewLifecycleError(
            f"Cross owner completion lane trigger is invalid: {owner_module_id}"
        ) from exc
    kind = raw.get("kind") if isinstance(raw, dict) else None
    if kind == "cross_owner_input":
        try:
            contract = CrossOwnerInput.model_validate(raw)
        except ValueError as exc:
            raise ReviewLifecycleError(
                f"Cross owner completion lane trigger is not typed: {owner_module_id}"
            ) from exc
        expected_ref = (
            f"Work/runs/{run_id}/reviews/cross-owner-input-r"
            f"{contract.review_round}-{owner_module_id}.json"
        )
        if (
            trigger_ref != expected_ref
            or contract.run_id != run_id
            or contract.owner_module_id != owner_module_id
            or contract.review_round != 0
            or contract.phase != "initial"
        ):
            raise ReviewLifecycleError(
                f"Cross owner completion lane trigger identity mismatch: {owner_module_id}"
            )
        _load_cross_owner_input(
            runner,
            run_id=run_id,
            owner_module_id=owner_module_id,
            review_round=contract.review_round,
            phase=contract.phase,
        )
        return
    if kind == "cross_owner_verdict_submission":
        try:
            verdict = CrossOwnerVerdictSubmission.model_validate(raw)
        except ValueError as exc:
            raise ReviewLifecycleError(
                f"Cross owner completion lane trigger is not typed: {owner_module_id}"
            ) from exc
        match = re.fullmatch(
            rf"Work/runs/{re.escape(run_id)}/reviews/"
            rf"cross-owner-verdicts-r([1-9][0-9]*)-{re.escape(owner_module_id)}\.json",
            trigger_ref,
        )
        if match is None or verdict.owner_module_id != owner_module_id:
            raise ReviewLifecycleError(
                f"Cross owner completion lane trigger identity mismatch: {owner_module_id}"
            )
        trigger_round = int(match.group(1))
        if completion.schema_version == "3":
            final_verdict_ref = _cross_owner_artifact_ref_value(completion.verdict_result)
            final_match = (
                re.fullmatch(
                    rf"Work/runs/{re.escape(run_id)}/reviews/"
                    rf"cross-owner-verdicts-r([1-9][0-9]*)-"
                    rf"{re.escape(owner_module_id)}\.json",
                    final_verdict_ref,
                )
                if final_verdict_ref is not None
                else None
            )
            if final_match is None or int(final_match.group(1)) != trigger_round + 1:
                raise ReviewLifecycleError(
                    f"Cross owner promoted completion has inconsistent rounds: {owner_module_id}"
                )
        elif completion.review_round != trigger_round + 1:
            raise ReviewLifecycleError(
                f"Cross owner completion lane trigger round mismatch: {owner_module_id}"
            )
        return
    raise ReviewLifecycleError(
        f"Cross owner completion has unsupported lane trigger: {owner_module_id}"
    )


def _validate_cross_owner_completion_business_identity(
    runner: "ReportWorkflowRunner",
    completion: CrossOwnerCompletion,
    *,
    run_id: str,
    owner_module_id: str,
    review_round: int | None = None,
    owner_input_ref: str | None = None,
) -> ModuleSubmission:
    """Validate one completion's current-run identity and typed subject.

    This is deliberately digest-independent.  It raises for malformed or
    wrong-owner/run/revision records so callers cannot silently replay a paid
    Provider lane after a damaged recovery artifact.
    """

    if (
        completion.run_id != run_id
        or completion.stage != "cross"
        or completion.module_id != owner_module_id
        or (review_round is not None and completion.review_round != review_round)
        or completion.status != "completed"
        or completion.lane_id != f"cross-r{completion.review_round}-module-{owner_module_id}"
    ):
        raise ReviewLifecycleError(
            f"Cross owner completion business identity mismatch: {owner_module_id}"
        )
    subject_ref = _cross_owner_artifact_ref_value(completion.subject)
    if subject_ref is None:
        raise ReviewLifecycleError(
            f"Cross owner completion has no subject artifact: {owner_module_id}"
        )
    subject_path = _cross_owner_artifact_path(
        runner,
        subject_ref,
        run_id=run_id,
        label="subject",
    )
    try:
        module = ModuleSubmission.model_validate_json(subject_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewLifecycleError(
            f"Cross owner completion subject is invalid: {owner_module_id}"
        ) from exc
    if module.module_id != owner_module_id:
        raise ReviewLifecycleError(
            f"Cross owner completion subject ownership mismatch: {owner_module_id}"
        )
    ref_revision = _cross_owner_ref_revision(subject_ref)
    if ref_revision is None or ref_revision != module.revision:
        raise ReviewLifecycleError(
            f"Cross owner completion subject revision/ref mismatch: {owner_module_id}"
        )
    if completion.subject_revision is not None and completion.subject_revision != module.revision:
        raise ReviewLifecycleError(
            f"Cross owner completion subject revision mismatch: {owner_module_id}"
        )
    if owner_input_ref is not None:
        actual_owner_input_ref = _cross_owner_artifact_ref_value(completion.owner_input)
        if actual_owner_input_ref != owner_input_ref:
            raise ReviewLifecycleError(
                f"Cross owner completion input identity mismatch: {owner_module_id}"
            )
    _validate_cross_owner_lane_trigger(
        runner,
        completion,
        run_id=run_id,
        owner_module_id=owner_module_id,
    )
    for label, artifact in (
        ("initial result", completion.initial_result),
        ("verdict result", completion.verdict_result),
        ("local review", completion.local_review_completion),
    ):
        ref = _cross_owner_artifact_ref_value(artifact)
        if ref is not None:
            _cross_owner_artifact_path(
                runner,
                ref,
                run_id=run_id,
                label=label,
            )
            if label == "initial result":
                try:
                    initial_result = CrossOwnerFindingSubmission.model_validate_json(
                        (runner.service.workspace / ref).read_text(encoding="utf-8")
                    )
                except (OSError, ValueError) as exc:
                    raise ReviewLifecycleError(
                        f"Cross owner initial result is not typed: {owner_module_id}"
                    ) from exc
                if initial_result.owner_module_id != owner_module_id:
                    raise ReviewLifecycleError(
                        f"Cross owner initial result ownership mismatch: {owner_module_id}"
                    )
            elif label == "verdict result":
                try:
                    verdict_result = CrossOwnerVerdictSubmission.model_validate_json(
                        (runner.service.workspace / ref).read_text(encoding="utf-8")
                    )
                except (OSError, ValueError) as exc:
                    raise ReviewLifecycleError(
                        f"Cross owner verdict result is not typed: {owner_module_id}"
                    ) from exc
                if verdict_result.owner_module_id != owner_module_id:
                    raise ReviewLifecycleError(
                        f"Cross owner verdict ownership mismatch: {owner_module_id}"
                    )
            elif label == "local review":
                try:
                    local_completion = ReviewCompletionRecord.model_validate_json(
                        (runner.service.workspace / ref).read_text(encoding="utf-8")
                    )
                except (OSError, ValueError) as exc:
                    raise ReviewLifecycleError(
                        f"Cross owner local review completion is not typed: {owner_module_id}"
                    ) from exc
                if (
                    local_completion.lifecycle != "module"
                    or local_completion.run_id != run_id
                    or local_completion.reviewer_agent_id != "evidence-auditor"
                    or local_completion.reviewer_session_key != f"module-auditor-{owner_module_id}"
                    or local_completion.subject_refs != [subject_ref]
                ):
                    raise ReviewLifecycleError(
                        f"Cross owner local review completion does not bind subject: {owner_module_id}"
                    )
    if _cross_owner_artifact_ref_value(completion.local_review_completion) is None:
        raise ReviewLifecycleError(
            f"Cross owner completion has no local review completion: {owner_module_id}"
        )
    return module


def _load_cross_owner_input(
    runner: "ReportWorkflowRunner",
    *,
    run_id: str,
    owner_module_id: str,
    review_round: int,
    phase: Literal["initial", "recheck"],
) -> tuple[CrossOwnerInput, str] | None:
    ref = f"Work/runs/{run_id}/reviews/cross-owner-input-r{review_round}-{owner_module_id}.json"
    path = runner.service.workspace / ref
    if not path.is_file():
        return None
    try:
        contract = CrossOwnerInput.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewLifecycleError(
            f"Cross owner {phase} input is invalid: {owner_module_id}"
        ) from exc
    if (
        contract.run_id != run_id
        or contract.owner_module_id != owner_module_id
        or contract.review_round != review_round
        or contract.phase != phase
    ):
        raise ReviewLifecycleError(
            f"Cross owner {phase} input identity mismatch: {owner_module_id}"
        )
    expected_owner_ref = _cross_owner_subject_ref(
        run_id,
        owner_module_id,
        contract.owner_subject_revision,
    )
    if contract.owner_subject_ref != expected_owner_ref:
        raise ReviewLifecycleError(
            f"Cross owner {phase} owner subject ref mismatch: {owner_module_id}"
        )
    _cross_owner_artifact_path(
        runner,
        contract.owner_subject_ref,
        run_id=run_id,
        label="owner subject",
    )
    try:
        owner_module = ModuleSubmission.model_validate_json(
            (runner.service.workspace / contract.owner_subject_ref).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ReviewLifecycleError(
            f"Cross owner {phase} subject is invalid: {owner_module_id}"
        ) from exc
    if (
        contract.owner_subject_revision != owner_module.revision
        or contract.owner_subject != module_content_view(owner_module)
    ):
        raise ReviewLifecycleError(
            f"Cross owner {phase} input does not bind its subject: {owner_module_id}"
        )
    for related_id, related_ref in contract.related_module_refs.items():
        expected_related_ref = _cross_owner_subject_ref(
            run_id,
            related_id,
            contract.related_module_revisions[related_id],
        )
        if related_ref != expected_related_ref:
            raise ReviewLifecycleError(
                f"Cross owner {phase} related subject ref mismatch: {owner_module_id}/{related_id}"
            )
        _cross_owner_artifact_path(
            runner,
            related_ref,
            run_id=run_id,
            label=f"related subject {owner_module_id}/{related_id}",
        )
        try:
            related_module = ModuleSubmission.model_validate_json(
                (runner.service.workspace / related_ref).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ReviewLifecycleError(
                f"Cross owner {phase} related subject is invalid: {owner_module_id}/{related_id}"
            ) from exc
        expected_view = _cross_owner_related_view(
            runner,
            module=related_module,
            ref=related_ref,
        )
        expected_business_view = expected_view.model_dump(
            mode="json",
            exclude={"subject_sha256"},
        )
        actual_business_view = contract.related_module_views[related_id].model_dump(
            mode="json",
            exclude={"subject_sha256"},
        )
        if (
            contract.related_module_revisions[related_id] != related_module.revision
            or actual_business_view != expected_business_view
        ):
            raise ReviewLifecycleError(
                f"Cross owner {phase} related binding mismatch: {owner_module_id}/{related_id}"
            )
    if contract.local_regression_review_ref is not None:
        _cross_owner_artifact_path(
            runner,
            contract.local_regression_review_ref,
            run_id=run_id,
            label="local regression review",
        )
    return contract, ref


def _cross_owner_modules_from_input(
    runner: "ReportWorkflowRunner",
    contract: CrossOwnerInput,
) -> dict[str, ModuleSubmission]:
    refs = {
        contract.owner_module_id: contract.owner_subject_ref,
        **contract.related_module_refs,
    }
    modules: dict[str, ModuleSubmission] = {}
    for module_id, ref in refs.items():
        _cross_owner_artifact_path(
            runner,
            ref,
            run_id=contract.run_id,
            label=f"frozen module {module_id}",
        )
        try:
            module = ModuleSubmission.model_validate_json(
                (runner.service.workspace / ref).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ReviewLifecycleError(
                f"Cross owner frozen module is invalid: {module_id}"
            ) from exc
        if module.module_id != module_id:
            raise ReviewLifecycleError(f"Cross owner frozen module identity mismatch: {module_id}")
        expected_revision = (
            contract.owner_subject_revision
            if module_id == contract.owner_module_id
            else contract.related_module_revisions[module_id]
        )
        if module.revision != expected_revision or ref != _cross_owner_subject_ref(
            contract.run_id,
            module_id,
            expected_revision,
        ):
            raise ReviewLifecycleError(
                f"Cross owner frozen module revision/ref mismatch: {module_id}"
            )
        modules[module_id] = module
    if set(modules) != set(_CROSS_OWNER_MODULE_IDS):
        raise ReviewLifecycleError("Cross owner frozen input does not bind all five modules")
    return modules


def _load_cross_owner_initial_result(
    runner: "ReportWorkflowRunner",
    *,
    run_id: str,
    owner_module_id: str,
    require_task_binding: bool = False,
) -> tuple[CrossOwnerFindingSubmission, str] | None:
    ref = f"Work/runs/{run_id}/reviews/cross-owner-findings-r0-{owner_module_id}.json"
    path = runner.service.workspace / ref
    if not path.is_file():
        return None
    try:
        result = CrossOwnerFindingSubmission.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewLifecycleError(
            f"Cross owner initial result is invalid: {owner_module_id}"
        ) from exc
    if result.owner_module_id != owner_module_id:
        raise ReviewLifecycleError(
            f"Cross owner initial result ownership mismatch: {owner_module_id}"
        )
    if require_task_binding:
        _require_cross_owner_task_payload(
            runner,
            run_id=run_id,
            task_id=f"cross-owner-{owner_module_id}-r0-initial",
            expected_payload=result,
        )
    return result, ref


def _load_cross_owner_verdict(
    runner: "ReportWorkflowRunner",
    *,
    run_id: str,
    owner_module_id: str,
    review_round: int,
    required_findings: list[CrossReviewFinding],
    require_task_binding: bool = False,
) -> tuple[CrossOwnerVerdictSubmission, str] | None:
    ref = f"Work/runs/{run_id}/reviews/cross-owner-verdicts-r{review_round}-{owner_module_id}.json"
    path = runner.service.workspace / ref
    if not path.is_file():
        return None
    try:
        verdict = CrossOwnerVerdictSubmission.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewLifecycleError(f"Cross owner verdict is invalid: {owner_module_id}") from exc
    expected_ids = {finding.id for finding in required_findings}
    actual_ids = {item.finding_id for item in verdict.verdicts}
    if verdict.owner_module_id != owner_module_id or actual_ids != expected_ids:
        raise ReviewLifecycleError(
            f"Cross owner verdict does not cover its exact finding set: {owner_module_id}"
        )
    if require_task_binding:
        _require_cross_owner_task_payload(
            runner,
            run_id=run_id,
            task_id=f"cross-owner-{owner_module_id}-r{review_round}-recheck",
            expected_payload=verdict,
        )
    # Initial synthesis artifacts are runtime-owned and immutable; rechecks do
    # not echo or rewrite them.
    return verdict, ref


def _require_cross_owner_task_payload(
    runner: "ReportWorkflowRunner",
    *,
    run_id: str,
    task_id: str,
    expected_payload: StrictModel,
) -> None:
    """Bind a legacy owner artifact to its completed append-only task result."""

    attempt_store = TaskAttemptStore(runner.service.workspace, run_id)
    correlation = attempt_store.current(task_id)
    if correlation is None:
        raise ReviewLifecycleError(
            f"Cross owner legacy artifact has no task correlation: {task_id}"
        )
    try:
        recovered = attempt_store.load_verified_result(correlation)
    except RuntimeError as exc:
        raise ReviewLifecycleError(
            f"Cross owner legacy task result failed hash verification: {task_id}"
        ) from exc
    if recovered is None:
        raise ReviewLifecycleError(
            f"Cross owner legacy artifact has no terminal task result: {task_id}"
        )
    terminal, result_payload = recovered
    raw_payload = result_payload.get("payload")
    if (
        terminal.status != "completed"
        or result_payload.get("status") != "completed"
        or result_payload.get("task_id") != task_id
        or result_payload.get("run_id") != run_id
        or result_payload.get("agent_id") != "cross-module-reviewer"
        or not isinstance(raw_payload, dict)
        or raw_payload != expected_payload.model_dump(mode="json")
    ):
        raise ReviewLifecycleError(
            f"Cross owner legacy artifact does not match its completed task result: {task_id}"
        )


def _recover_cross_owner_lane(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    owner_module_id: str,
    review_round: int,
    owner_input_ref: str,
    required_findings: list[CrossReviewFinding],
) -> _CrossOwnerLaneResult | None:
    lane_root = (
        runner.service.workspace
        / f"Work/runs/{state['run_id']}/lanes/cross-r{review_round}/module-{owner_module_id}"
    )
    candidates = [
        path
        for path in sorted(lane_root.glob("completion-r*.json"))
        if path.name != "pipeline-completion.json"
    ]
    if not candidates:
        return None
    matching: list[tuple[str, CrossOwnerCompletion]] = []
    for path in candidates:
        try:
            completion = CrossOwnerCompletion.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReviewLifecycleError(
                f"Cross owner lane completion is invalid: {owner_module_id}"
            ) from exc
        _cross_owner_artifact_path(
            runner,
            path.relative_to(runner.service.workspace).as_posix(),
            run_id=state["run_id"],
            label="lane completion",
        )
        _validate_cross_owner_completion_business_identity(
            runner,
            completion,
            run_id=state["run_id"],
            owner_module_id=owner_module_id,
            review_round=review_round,
            owner_input_ref=owner_input_ref,
        )
        matching.append((path.relative_to(runner.service.workspace).as_posix(), completion))
    if not matching:
        return None
    if len(matching) != 1:
        raise ReviewLifecycleError(
            f"Cross owner has duplicate recoverable lane completions: {owner_module_id}"
        )
    completion_ref, completion = matching[0]
    module = _validate_cross_owner_completion_business_identity(
        runner,
        completion,
        run_id=state["run_id"],
        owner_module_id=owner_module_id,
        review_round=review_round,
        owner_input_ref=owner_input_ref,
    )
    finding_ids = {finding.id for finding in required_findings}
    responses: list[RevisionResponse] = []
    recheck_input = _load_cross_owner_input(
        runner,
        run_id=state["run_id"],
        owner_module_id=owner_module_id,
        review_round=review_round,
        phase="recheck",
    )
    if recheck_input is not None:
        responses = list(recheck_input[0].revision_responses)
    if {response.finding_id for response in responses} != finding_ids:
        module_root = runner.service.workspace / f"Work/runs/{state['run_id']}/modules"
        for path in sorted(module_root.glob(f"{owner_module_id}-r*.json")):
            try:
                candidate = ModuleSubmission.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            candidate_responses = [
                response
                for response in candidate.revision_responses
                if response.finding_id in finding_ids
            ]
            if {response.finding_id for response in candidate_responses} == finding_ids:
                responses = candidate_responses
                break
    if finding_ids and {response.finding_id for response in responses} != finding_ids:
        raise ReviewLifecycleError(
            f"Cross owner recovered lane lacks exact revision responses: {owner_module_id}"
        )
    return _CrossOwnerLaneResult(
        module=module,
        responses=responses,
        local_review_ref=_cross_owner_artifact_ref_value(completion.local_review_completion) or "",
        completion_ref=completion_ref,
        completion=completion,
    )


def _recover_cross_owner_lane_from_recovery(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    owner_module_id: str,
    completion_ref: str,
    required_findings: list[CrossReviewFinding],
    owner_input_ref: str | None = None,
    review_round: int | None = None,
) -> _CrossOwnerLaneResult | None:
    """Recover a completed owner lane from business state without hash gates."""

    path = _cross_owner_artifact_path(
        runner,
        completion_ref,
        run_id=state["run_id"],
        label="recovery lane completion",
    )
    try:
        completion = CrossOwnerCompletion.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewLifecycleError(
            f"Cross owner recovery lane completion is invalid: {owner_module_id}"
        ) from exc
    module = _validate_cross_owner_completion_business_identity(
        runner,
        completion,
        run_id=state["run_id"],
        owner_module_id=owner_module_id,
        review_round=review_round,
        owner_input_ref=owner_input_ref,
    )
    finding_ids = {finding.id for finding in required_findings}
    responses: list[RevisionResponse] = []
    module_root = runner.service.workspace / f"Work/runs/{state['run_id']}/modules"
    for candidate_path in sorted(module_root.glob(f"{owner_module_id}-r*.json")):
        try:
            candidate = ModuleSubmission.model_validate_json(
                candidate_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        candidate_responses = [
            response
            for response in candidate.revision_responses
            if response.finding_id in finding_ids
        ]
        if {response.finding_id for response in candidate_responses} == finding_ids:
            responses = candidate_responses
            break
    if finding_ids and {response.finding_id for response in responses} != finding_ids:
        raise ReviewLifecycleError(
            f"Cross owner recovered lane lacks exact revision responses: {owner_module_id}"
        )
    local_review_ref = _cross_owner_artifact_ref_value(completion.local_review_completion) or ""
    return _CrossOwnerLaneResult(
        module=module,
        responses=responses,
        local_review_ref=local_review_ref,
        completion_ref=completion_ref,
        completion=completion,
    )


def _promote_cross_owner_pipeline_completion(
    runner: "ReportWorkflowRunner",
    *,
    lane: _CrossOwnerLaneResult,
    initial_result_ref: str,
    verdict_ref: str | None,
) -> _CrossOwnerLaneResult:
    promoted = lane.completion.model_copy(
        update={
            # The exact-five barrier is the outer Cross lifecycle r1 barrier.
            # Owner-internal regression rounds remain visible in their lane refs
            # and verdict artifacts but do not split the outer barrier identity.
            "lane_id": f"cross-r1-module-{lane.completion.module_id}",
            "review_round": 1,
            "initial_result": _cross_owner_artifact_ref(runner, initial_result_ref),
            "verdict_result": (
                _cross_owner_artifact_ref(runner, verdict_ref) if verdict_ref is not None else None
            ),
            "schema_version": "3",
        }
    )
    ref = (
        f"Work/runs/{promoted.run_id}/lanes/cross-r1/"
        f"module-{promoted.module_id}/pipeline-completion.json"
    )
    path = runner.service.workspace / ref
    if path.is_file():
        try:
            existing = CrossOwnerCompletion.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReviewLifecycleError(
                f"Cross owner pipeline completion is invalid: {promoted.module_id}"
            ) from exc
        _validate_cross_owner_completion_business_identity(
            runner,
            existing,
            run_id=promoted.run_id,
            owner_module_id=promoted.module_id,
            review_round=1,
        )
        if not _cross_owner_completion_business_equal(existing, promoted):
            raise ReviewLifecycleError(
                f"Cross owner pipeline completion changed: {promoted.module_id}"
            )
        promoted = existing
    else:
        runner.service.store.write_json(ref, promoted.model_dump(mode="json"))
    return lane.model_copy(update={"completion_ref": ref, "completion": promoted})


def _build_cross_owner_review_envelope(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    owner_module_id: str,
    phase: Literal["initial", "recheck"],
    review_round: int,
    owner_input_ref: str,
) -> TaskEnvelope:
    """Build the shared typed TaskEnvelope for one Cross-owner turn."""

    input_kind = "cross_owner_input"
    output_kind = (
        "cross_owner_finding_submission" if phase == "initial" else "cross_owner_verdict_submission"
    )
    owner_input = CrossOwnerInput.model_validate_json(
        (runner.service.workspace / owner_input_ref).read_text(encoding="utf-8")
    )
    return TaskEnvelope(
        task_id=f"cross-owner-{owner_module_id}-r{review_round}-{phase}",
        run_id=state["run_id"],
        # One packaged reviewer definition is reused with five durable session
        # keys; agent-runner identity routing treats these keys as independent
        # Cross-owner identities.
        agent_id="cross-module-reviewer",
        objective=(
            f"以 Cross owner {owner_module_id} 身份审查本模块与其他四模块的关系；"
            "只创建归属本模块的 finding，并且 related 模块只读。"
            if phase == "initial"
            else f"由 Cross owner {owner_module_id} 原会话复审本轮 owner 修订，"
            "逐项给出原 finding verdict；related 模块仍只读。"
        ),
        input_refs=[owner_input_ref],
        constraints=[
            f"唯一 Cross owner 写作范围是模块 {owner_module_id}",
            "related_module_views 是紧凑只读关系视图，不得修改或创建其他模块 finding",
            (
                "initial 只提交 cross_owner_finding_submission"
                if phase == "initial"
                else (
                    "recheck 只提交 cross_owner_verdict_submission，逐项覆盖全部 "
                    "required_findings；new_findings 只允许当前修订引入的真实回归"
                )
            ),
            "coverage 仅证明当前 owner 检查过六个维度，不代表 approved",
        ],
        allowed_outputs=[output_kind],
        allowed_tools=["submit_result"],
        revision=review_round,
        prior_result_ref=(
            f"Work/runs/{state['run_id']}/reviews/cross-owner-findings-r0-{owner_module_id}.json"
            if phase == "recheck"
            else None
        ),
        artifact_delivery_modes={owner_input_ref: "inline"},
        target_submodule_ids=list(owner_input.owner_scope_submodule_ids),
        input_contract_kind=input_kind,
        input_contract_ref=owner_input_ref,
        # Owner-specific review questions already live in CrossOwnerInput.
        # Keeping one source avoids duplicating the focus in task context.
        inline_context="",
    )


def _accept_cross_owner_review_result(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    owner_module_id: str,
    phase: Literal["initial", "recheck"],
    review_round: int,
    result: CrossOwnerFindingSubmission | CrossOwnerVerdictSubmission,
    required_findings: list[CrossReviewFinding] | None = None,
) -> tuple[CrossOwnerFindingSubmission | CrossOwnerVerdictSubmission, str]:
    """Validate and persist one typed Cross-owner reviewer result."""

    if phase == "initial":
        if not isinstance(result, CrossOwnerFindingSubmission):
            raise ReviewLifecycleError(
                f"Cross owner {owner_module_id} returned the wrong initial type"
            )
        artifact_ref = _write_immutable_model(
            runner,
            (f"Work/runs/{state['run_id']}/reviews/cross-owner-findings-r0-{owner_module_id}.json"),
            result,
        )
    else:
        if not isinstance(result, CrossOwnerVerdictSubmission):
            raise ReviewLifecycleError(
                f"Cross owner {owner_module_id} returned the wrong recheck type"
            )
        expected_ids = {finding.id for finding in (required_findings or [])}
        actual_ids = {verdict.finding_id for verdict in result.verdicts}
        if actual_ids != expected_ids:
            raise ReviewLifecycleError(f"Cross owner {owner_module_id} verdict coverage mismatch")
        artifact_ref = _write_immutable_model(
            runner,
            (
                f"Work/runs/{state['run_id']}/reviews/"
                f"cross-owner-verdicts-r{review_round}-{owner_module_id}.json"
            ),
            result,
        )
    return result, artifact_ref


def _accept_cross_owner_initial_review(
    runner: "ReportWorkflowRunner",
    *,
    preparation: CrossOwnerInitialReviewPreparation,
    result: CrossOwnerFindingSubmission | None,
) -> CrossOwnerInitialReviewAcceptance:
    """Accept a fresh or already-persisted Cross-owner initial result."""

    if preparation.mode == "continue_existing":
        if result is not None:
            raise ReviewLifecycleError(
                "cannot accept a Cross owner initial result after persisted continuation"
            )
        if preparation.existing_result is None or preparation.existing_result_ref is None:
            raise ReviewLifecycleError(
                "Cross owner initial continuation lacks its persisted result"
            )
        accepted = preparation.existing_result
        result_ref = preparation.existing_result_ref
    else:
        if result is None:
            raise ReviewLifecycleError("Cross owner initial Agent result is missing")
        accepted, result_ref = _accept_cross_owner_review_result(
            runner,
            state={"run_id": preparation.run_id},
            owner_module_id=preparation.owner_module_id,
            phase="initial",
            review_round=preparation.review_round,
            result=result,
        )
        assert isinstance(accepted, CrossOwnerFindingSubmission)
    return CrossOwnerInitialReviewAcceptance(
        run_id=preparation.run_id,
        workflow_id=preparation.workflow_id,
        owner_module_id=preparation.owner_module_id,
        reviewer_session_key=preparation.reviewer_session_key,
        owner_input_ref=preparation.owner_input_ref,
        result_ref=result_ref,
        result=accepted,
    )


async def _run_cross_owner_review(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    workflow_id: str,
    modules: dict[str, ModuleSubmission],
    owner_module_id: str,
    phase: Literal["initial", "recheck"],
    review_round: int,
    owner_input_ref: str,
    required_findings: list[CrossReviewFinding] | None = None,
    revision_responses: list[RevisionResponse] | None = None,
    local_review_ref: str | None = None,
    prior_synthesis_inputs: list[CrossSynthesisInput] | None = None,
) -> tuple[CrossOwnerFindingSubmission | CrossOwnerVerdictSubmission, str]:
    """Dispatch one fixed Cross-owner reviewer and persist its typed result."""

    envelope = _build_cross_owner_review_envelope(
        runner,
        state=state,
        owner_module_id=owner_module_id,
        phase=phase,
        review_round=review_round,
        owner_input_ref=owner_input_ref,
    )
    result = await runner._agent(
        "cross-module-reviewer",
        envelope,
        envelope.input_refs,
        workflow_id,
        session_key=f"cross-owner-{owner_module_id}",
    )
    return _accept_cross_owner_review_result(
        runner,
        state=state,
        owner_module_id=owner_module_id,
        phase=phase,
        review_round=review_round,
        result=result,
        required_findings=required_findings,
    )


def _verified_cross_owner_noop(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    owner_module_id: str,
    module: ModuleSubmission,
    owner_input_ref: str,
    review_round: int,
) -> _CrossOwnerLaneResult:
    """Create a durable completion for an owner with no Cross finding."""

    subject_ref = f"Work/runs/{state['run_id']}/modules/{owner_module_id}-r{module.revision}.json"
    local_ref = state.get("module_review_completion_refs", {}).get(owner_module_id)
    if not local_ref:
        raise ReviewLifecycleError(
            f"Cross owner no-op requires prior module audit: {owner_module_id}"
        )
    semantic_key = hashlib.sha256(
        json.dumps(
            {
                "run_id": state["run_id"],
                "review_round": review_round,
                "module_id": owner_module_id,
                "owner_input": owner_input_ref,
                "subject_ref": subject_ref,
                "finding_ids": [],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    completion = CrossOwnerCompletion(
        lane_id=f"cross-r{review_round}-module-{owner_module_id}",
        run_id=state["run_id"],
        review_round=review_round,
        module_id=owner_module_id,
        semantic_key=semantic_key,
        subject_revision=module.revision,
        owner_input=_cross_owner_artifact_ref(runner, owner_input_ref),
        subject=_cross_owner_artifact_ref(runner, subject_ref),
        local_review_completion=_cross_owner_artifact_ref(runner, local_ref),
        author_task_attempt_id=f"cross-owner-noop-{owner_module_id}",
        reviewer_session_id=f"cross-owner-{owner_module_id}",
        lease_epoch=1,
    )
    completion_ref = (
        f"Work/runs/{state['run_id']}/lanes/cross-r{review_round}/"
        f"module-{owner_module_id}/completion-r{module.revision}.json"
    )
    path = runner.service.workspace / completion_ref
    if path.is_file():
        try:
            existing = CrossOwnerCompletion.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReviewLifecycleError(
                f"Cross owner no-op completion is invalid: {owner_module_id}"
            ) from exc
        _validate_cross_owner_completion_business_identity(
            runner,
            existing,
            run_id=state["run_id"],
            owner_module_id=owner_module_id,
            review_round=review_round,
            owner_input_ref=owner_input_ref,
        )
        if not _cross_owner_completion_business_equal(existing, completion):
            raise ReviewLifecycleError(f"Cross owner no-op completion changed: {owner_module_id}")
        completion = existing
    else:
        runner.service.store.write_json(
            completion_ref,
            completion.model_dump(mode="json"),
        )
    return _CrossOwnerLaneResult(
        module=module,
        responses=[],
        local_review_ref=local_ref,
        completion_ref=completion_ref,
        completion=completion,
    )


def _load_cross_synthesis_from_refs(
    runner: "ReportWorkflowRunner",
    refs: list[str],
) -> list[CrossSynthesisInput]:
    """Recover synthesis inputs from immutable owner/aggregate artifacts."""

    values: dict[str, CrossSynthesisInput] = {}
    for ref in refs:
        path = runner.service.workspace / ref
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ReviewLifecycleError(
                f"Cross synthesis artifact is unreadable during resume: {ref}"
            ) from exc
        for raw in payload.get("synthesis_inputs", []):
            item = CrossSynthesisInput.model_validate(raw)
            prior = values.get(item.id)
            if prior is not None and prior != item:
                raise ReviewLifecycleError(f"Cross synthesis id changed during resume: {item.id}")
            values[item.id] = item
    return list(values.values())


def _verify_cross_owner_barrier(
    runner: "ReportWorkflowRunner",
    barrier: CrossOwnerBarrier,
    *,
    barrier_path: Path,
    expected_run_id: str,
    expected_round: int,
    expected_modules: set[str],
) -> None:
    """Validate the exact Cross barrier from typed business records.

    ``completion_hashes`` and ``barrier_sha256`` remain readable legacy fields,
    but they are forensic metadata only.  Resume validity comes from current
    run/stage/owner/revision bindings, canonical refs, and readable typed JSON.
    """

    run_id = barrier.run_id
    run_root = (runner.service.workspace / f"Work/runs/{run_id}").resolve()
    if (
        barrier.run_id != expected_run_id
        or not barrier_path.resolve().is_relative_to(run_root)
        or barrier.review_round != expected_round
        or len(barrier.target_modules) != len(expected_modules)
        or set(barrier.target_modules) != expected_modules
        or len(barrier.completion_refs) != len(expected_modules)
        or set(barrier.completion_refs) != expected_modules
    ):
        raise ReviewLifecycleError("Cross owner barrier identity mismatch")
    if barrier.completion_revisions and (
        len(barrier.completion_revisions) != len(expected_modules)
        or set(barrier.completion_revisions) != expected_modules
    ):
        raise ReviewLifecycleError("Cross owner barrier revision set mismatch")
    revisions: dict[str, int] = {}
    for module_id in sorted(expected_modules, key=float):
        ref = barrier.completion_refs[module_id]
        completion_path = _cross_owner_artifact_path(
            runner,
            ref,
            run_id=run_id,
            label=f"barrier completion {module_id}",
        )
        try:
            completion = CrossOwnerCompletion.model_validate_json(
                completion_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ReviewLifecycleError(f"Cross owner barrier completion is invalid: {ref}") from exc
        module = _validate_cross_owner_completion_business_identity(
            runner,
            completion,
            run_id=run_id,
            owner_module_id=module_id,
            review_round=barrier.review_round,
        )
        revision = module.revision
        revisions[module_id] = revision
    if barrier.completion_revisions and revisions != barrier.completion_revisions:
        raise ReviewLifecycleError("Cross owner barrier revisions do not bind subjects")


def verify_cross_owner_barrier(
    runner: "ReportWorkflowRunner",
    barrier: CrossOwnerBarrier,
    *,
    barrier_path: Path,
    expected_run_id: str,
    expected_round: int,
    expected_modules: set[str],
) -> None:
    """Public resume-time verifier for the exact Cross-owner completion barrier."""

    _verify_cross_owner_barrier(
        runner,
        barrier,
        barrier_path=barrier_path,
        expected_run_id=expected_run_id,
        expected_round=expected_round,
        expected_modules=expected_modules,
    )


def _load_cross_owner_revision_candidate(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    module_id: str,
    current: ModuleSubmission,
    findings: list[CrossReviewFinding],
) -> tuple[ModuleSubmission, str] | None:
    """Load the same persisted owner Author candidate used by the Legacy lane."""

    modules_root = runner.service.workspace / f"Work/runs/{state['run_id']}/modules"
    candidates: list[ModuleSubmission] = []
    for path in modules_root.glob(f"{module_id}-r*.json"):
        try:
            candidate = ModuleSubmission.model_validate_json(path.read_text(encoding="utf-8"))
            if candidate.revision <= current.revision:
                continue
            _validate_responses(
                candidate.revision_responses,
                {finding.id for finding in findings},
                {target_id for finding in findings for target_id in finding.target_submodule_ids},
            )
            candidates.append(candidate)
        except (OSError, ValueError):
            continue
    if not candidates:
        return None
    candidate = max(candidates, key=lambda item: item.revision)
    candidate_ref = f"Work/runs/{state['run_id']}/modules/{module_id}-r{candidate.revision}.json"
    return candidate, candidate_ref


async def prepare_cross_owner_revision(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    workflow_id: str,
    owner_module_id: str,
    review_round: int,
    owner_input_ref: str,
    current: ModuleSubmission,
    findings: list[CrossReviewFinding],
    finding_refs: list[str],
    prior_completion_ref: str | None,
) -> CrossOwnerRevisionPreparation:
    """Prepare or recover the existing original-Author Cross revision."""

    existing = _load_cross_owner_revision_candidate(
        runner,
        state=state,
        module_id=owner_module_id,
        current=current,
        findings=findings,
    )
    if existing is not None:
        candidate, candidate_ref = existing
        return CrossOwnerRevisionPreparation(
            mode="continue_existing",
            run_id=state["run_id"],
            workflow_id=workflow_id,
            owner_module_id=owner_module_id,
            review_round=review_round,
            owner_input_ref=owner_input_ref,
            current=current,
            findings=findings,
            finding_refs=finding_refs,
            prior_completion_ref=prior_completion_ref,
            existing_candidate=candidate,
            existing_candidate_ref=candidate_ref,
        )
    prepared = await prepare_module_revision(
        runner,
        state=state,
        workflow_id=workflow_id,
        subject=current,
        cross_findings=findings,
    )
    return CrossOwnerRevisionPreparation(
        mode="invoke_agent",
        run_id=state["run_id"],
        workflow_id=workflow_id,
        owner_module_id=owner_module_id,
        review_round=review_round,
        owner_input_ref=owner_input_ref,
        current=current,
        findings=findings,
        finding_refs=finding_refs,
        prior_completion_ref=prior_completion_ref,
        prepared=prepared,
    )


def accept_cross_owner_revision(
    runner: "ReportWorkflowRunner",
    *,
    preparation: CrossOwnerRevisionPreparation,
    result: ModuleRevisionSubmission | None,
) -> CrossOwnerRevisionAcceptance:
    """Accept a fresh or existing original-Author candidate without copying apply logic."""

    if preparation.mode == "continue_existing":
        revised = cast(ModuleSubmission, preparation.existing_candidate)
        candidate_ref = cast(str, preparation.existing_candidate_ref)
    else:
        revised, candidate_ref = accept_module_revision(
            runner,
            preparation=cast(ModuleRevisionPreparation, preparation.prepared),
            result=cast(ModuleRevisionSubmission, result),
        )
    return CrossOwnerRevisionAcceptance(
        run_id=preparation.run_id,
        workflow_id=preparation.workflow_id,
        owner_module_id=preparation.owner_module_id,
        review_round=preparation.review_round,
        owner_input_ref=preparation.owner_input_ref,
        current=preparation.current,
        findings=preparation.findings,
        finding_refs=preparation.finding_refs,
        prior_completion_ref=preparation.prior_completion_ref,
        revised=revised,
        candidate_ref=candidate_ref,
    )


def _cross_owner_local_regression_context(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    owner_module_id: str,
    reviewed_baseline: ModuleSubmission,
    revised: ModuleSubmission,
    findings: list[CrossReviewFinding],
    review_round: int,
    prior_completion_ref: str | None,
) -> tuple[ModuleLocalRegressionContext, set[str]]:
    """Build the existing Cross-triggered module regression input once."""

    baseline_subject_ref = (
        f"Work/runs/{state['run_id']}/modules/{owner_module_id}-r{reviewed_baseline.revision}.json"
    )
    prior_completion_ref = prior_completion_ref or state.get(
        "module_review_completion_refs", {}
    ).get(owner_module_id)
    if not prior_completion_ref:
        raise ReviewLifecycleError(
            f"Cross local regression requires prior module review: {owner_module_id}"
        )
    try:
        prior_completion = ReviewCompletionRecord.model_validate_json(
            (runner.service.workspace / prior_completion_ref).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ReviewLifecycleError(
            f"Cross local regression prior completion is unreadable: {owner_module_id}"
        ) from exc
    if (
        prior_completion.lifecycle != "module"
        or prior_completion.run_id != state["run_id"]
        or baseline_subject_ref not in prior_completion.subject_refs
    ):
        raise ReviewLifecycleError(
            f"Cross local regression prior completion does not bind {owner_module_id}"
        )

    local_scope = {target_id for finding in findings for target_id in finding.target_submodule_ids}
    local_diff_ref = (
        f"Work/runs/{state['run_id']}/reviews/module/cross-r{review_round}/"
        f"{owner_module_id}/trigger-diff-r{revised.revision}.json"
    )
    raw_local_diff = build_revision_diff(reviewed_baseline, revised)
    local_diff = ModuleRevisionDiff(
        module_id=raw_local_diff["module_id"],
        from_revision=raw_local_diff["from_revision"],
        to_revision=raw_local_diff["to_revision"],
        changed_submodule_narratives=raw_local_diff["changed_submodule_narratives"],
        changed_statement_refs=[
            "statement-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
            for value in raw_local_diff["changed_claim_ids"]
        ],
        evidence_ids_added=raw_local_diff["source_ids_added"],
        evidence_ids_removed=raw_local_diff["source_ids_removed"],
    )
    runner.service.store.write_json(
        local_diff_ref,
        local_diff.model_dump(mode="json"),
    )
    return (
        ModuleLocalRegressionContext(
            prior_review_completion_ref=prior_completion_ref,
            prior_review_completion=prior_completion,
            baseline_subject_ref=baseline_subject_ref,
            trigger_cross_findings=findings,
            trigger_revision_responses=revised.revision_responses,
            revision_diff_ref=local_diff_ref,
            revision_diff=local_diff,
        ),
        local_scope,
    )


async def prepare_cross_owner_local_review(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    workflow_id: str,
    revision: CrossOwnerRevisionAcceptance,
) -> CrossOwnerLocalReviewPreparation:
    """Prepare the original module Auditor after an accepted Cross revision."""

    if any(
        response.action in {"disputed", "needs_input"}
        for response in revision.revised.revision_responses
    ):
        return CrossOwnerLocalReviewPreparation(
            mode="continue_existing",
            run_id=revision.run_id,
            workflow_id=workflow_id,
            owner_module_id=revision.owner_module_id,
            review_round=revision.review_round,
            owner_input_ref=revision.owner_input_ref,
            reviewed_baseline=revision.current,
            cross_responses=list(revision.revised.revision_responses),
        )
    regression_context, local_scope = _cross_owner_local_regression_context(
        runner,
        state=state,
        owner_module_id=revision.owner_module_id,
        reviewed_baseline=revision.current,
        revised=revision.revised,
        findings=revision.findings,
        review_round=revision.review_round,
        prior_completion_ref=revision.prior_completion_ref,
    )
    lane_state = deepcopy(state)
    lane_state["_defer_main_exceptions"] = True
    lane_state["resume"] = True
    lane_state.setdefault("module_submissions", {})[revision.owner_module_id] = revision.revised
    lane_state.setdefault("specialist_submissions", {})[revision.owner_module_id] = revision.revised
    prepared = await prepare_module_initial_review(
        runner,
        module_id=revision.owner_module_id,
        payload=revision.revised,
        state=lane_state,
        workflow_id=workflow_id,
        initial_scope=local_scope,
        lifecycle_id=f"cross-r{revision.review_round}",
        regression_context=regression_context,
    )
    existing_review: ModuleInitialReviewAcceptance | None = None
    if (
        prepared.mode == "continue_existing"
        and prepared.progress is not None
        and prepared.progress.next_action == "completed"
    ):
        cross_completion_ref = (
            f"{prepared.review_root}/completion-r{prepared.progress.current.revision}.json"
        )
        lane_state.setdefault("module_review_completion_refs", {})[revision.owner_module_id] = (
            cross_completion_ref
        )
        reviewed = await run_module_review(
            runner,
            revision.owner_module_id,
            prepared.current,
            lane_state,
            workflow_id,
            initial_scope=local_scope,
            lifecycle_id=f"cross-r{revision.review_round}",
            regression_context=regression_context,
        )
        progress = prepared.progress
        existing_review = ModuleInitialReviewAcceptance(
            run_id=revision.run_id,
            module_id=revision.owner_module_id,
            lifecycle_id=prepared.lifecycle_id,
            reviewer_session_key=prepared.reviewer_session_key,
            subject_ref=(
                f"Work/runs/{revision.run_id}/modules/"
                f"{revision.owner_module_id}-r{reviewed.revision}.json"
            ),
            current=reviewed,
            findings=list(progress.pending),
            finding_refs=list(progress.finding_refs),
            verdict_refs=list(progress.verdict_refs),
            resolved_ids=list(progress.resolved_ids),
            next_action="completed",
            progress_ref=prepared.progress_ref,
            completion_ref=cross_completion_ref,
        )
    return CrossOwnerLocalReviewPreparation(
        mode=prepared.mode,
        run_id=revision.run_id,
        workflow_id=workflow_id,
        owner_module_id=revision.owner_module_id,
        review_round=revision.review_round,
        owner_input_ref=revision.owner_input_ref,
        reviewed_baseline=revision.current,
        cross_responses=list(revision.revised.revision_responses),
        regression_context=regression_context,
        prepared=prepared,
        existing_review=existing_review,
    )


def accept_cross_owner_local_review(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    preparation: CrossOwnerLocalReviewPreparation,
    result: ModuleReviewFindingSubmission | None = None,
) -> CrossOwnerLocalReviewAcceptance:
    """Accept the declared original-Auditor local regression once."""

    if preparation.mode == "continue_existing":
        if preparation.existing_review is None:
            raise ReviewLifecycleError(
                "Cross owner local review has no completed result to recover"
            )
        review = preparation.existing_review
    else:
        if result is None:
            raise ReviewLifecycleError("Cross owner local review requires an Auditor result")
        lane_state = deepcopy(state)
        lane_state["_defer_main_exceptions"] = True
        review = accept_module_initial_review(
            runner,
            preparation=cast(ModuleInitialReviewPreparation, preparation.prepared),
            result=result,
            state=lane_state,
        )
    return CrossOwnerLocalReviewAcceptance(
        run_id=preparation.run_id,
        workflow_id=preparation.workflow_id,
        owner_module_id=preparation.owner_module_id,
        review_round=preparation.review_round,
        owner_input_ref=preparation.owner_input_ref,
        reviewed_baseline=preparation.reviewed_baseline,
        cross_responses=preparation.cross_responses,
        regression_context=cast(
            ModuleLocalRegressionContext,
            preparation.regression_context,
        ),
        review=review,
    )


def prepare_cross_owner_recheck(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    workflow_id: str,
    frozen_modules: dict[str, ModuleSubmission],
    initial: CrossOwnerInitialReviewAcceptance,
    lane: _CrossOwnerLaneResult,
    review_round: int,
    required_findings: list[CrossReviewFinding],
) -> CrossOwnerRecheckPreparation:
    """Prepare or recover the original Cross owner reviewer recheck."""

    owner_module_id = initial.owner_module_id
    modules_for_recheck = dict(frozen_modules)
    modules_for_recheck[owner_module_id] = lane.module
    recheck_loaded = _load_cross_owner_input(
        runner,
        run_id=state["run_id"],
        owner_module_id=owner_module_id,
        review_round=review_round,
        phase="recheck",
    )
    if recheck_loaded is None:
        _contract, owner_input_ref = _cross_owner_input(
            runner,
            state=state,
            modules=modules_for_recheck,
            owner_module_id=owner_module_id,
            phase="recheck",
            review_round=review_round,
            required_findings=required_findings,
            revision_responses=lane.responses,
            local_review_ref=lane.local_review_ref,
            prior_synthesis_inputs=list(initial.result.synthesis_inputs),
        )
    else:
        recheck_contract, owner_input_ref = recheck_loaded
        if {finding.id for finding in recheck_contract.required_findings} != {
            finding.id for finding in required_findings
        } or (recheck_contract.owner_subject_ref != lane.completion.subject.ref):
            raise ReviewLifecycleError(
                f"Cross owner recheck input does not bind recovered lane: "
                f"{owner_module_id}/r{review_round}"
            )

    reviewer_session_key = f"cross-owner-{owner_module_id}"
    verdict_loaded = _load_cross_owner_verdict(
        runner,
        run_id=state["run_id"],
        owner_module_id=owner_module_id,
        review_round=review_round,
        required_findings=required_findings,
        require_task_binding=False,
    )
    if verdict_loaded is not None:
        existing_result, existing_result_ref = verdict_loaded
        return CrossOwnerRecheckPreparation(
            mode="continue_existing",
            run_id=state["run_id"],
            workflow_id=workflow_id,
            owner_module_id=owner_module_id,
            review_round=review_round,
            reviewer_session_key=reviewer_session_key,
            initial_result_ref=initial.result_ref,
            initial_result=initial.result,
            lane=lane,
            owner_input_ref=owner_input_ref,
            required_findings=required_findings,
            existing_result_ref=existing_result_ref,
            existing_result=existing_result,
        )
    envelope = _build_cross_owner_review_envelope(
        runner,
        state=state,
        owner_module_id=owner_module_id,
        phase="recheck",
        review_round=review_round,
        owner_input_ref=owner_input_ref,
    )
    return CrossOwnerRecheckPreparation(
        mode="invoke_agent",
        run_id=state["run_id"],
        workflow_id=workflow_id,
        owner_module_id=owner_module_id,
        review_round=review_round,
        reviewer_session_key=reviewer_session_key,
        initial_result_ref=initial.result_ref,
        initial_result=initial.result,
        lane=lane,
        owner_input_ref=owner_input_ref,
        required_findings=required_findings,
        envelope=envelope,
    )


def accept_cross_owner_recheck(
    runner: "ReportWorkflowRunner",
    *,
    preparation: CrossOwnerRecheckPreparation,
    result: CrossOwnerVerdictSubmission | None,
) -> CrossOwnerRecheckAcceptance:
    """Accept a fresh or persisted original Cross owner recheck result."""

    if preparation.mode == "continue_existing":
        if result is not None:
            raise ReviewLifecycleError(
                "cannot accept a Cross owner recheck after persisted continuation"
            )
        accepted = cast(
            CrossOwnerVerdictSubmission,
            preparation.existing_result,
        )
        result_ref = cast(str, preparation.existing_result_ref)
    else:
        if result is None:
            raise ReviewLifecycleError("Cross owner recheck Agent result is missing")
        accepted, result_ref = _accept_cross_owner_review_result(
            runner,
            state={"run_id": preparation.run_id},
            owner_module_id=preparation.owner_module_id,
            phase="recheck",
            review_round=preparation.review_round,
            result=result,
            required_findings=preparation.required_findings,
        )
        assert isinstance(accepted, CrossOwnerVerdictSubmission)
    return CrossOwnerRecheckAcceptance(
        run_id=preparation.run_id,
        workflow_id=preparation.workflow_id,
        owner_module_id=preparation.owner_module_id,
        review_round=preparation.review_round,
        reviewer_session_key=preparation.reviewer_session_key,
        initial_result_ref=preparation.initial_result_ref,
        initial_result=preparation.initial_result,
        lane=preparation.lane,
        owner_input_ref=preparation.owner_input_ref,
        required_findings=preparation.required_findings,
        result_ref=result_ref,
        result=accepted,
    )


async def advance_cross_owner_round(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    workflow_id: str,
    initial_input_ref: str,
    initial_result_ref: str,
    initial_result: CrossOwnerFindingSubmission,
    acceptance: CrossOwnerRecheckAcceptance,
    previous: CrossOwnerRoundProgress | None = None,
) -> CrossOwnerRoundProgress:
    """Advance the existing owner finding/verdict state after one recheck."""

    owner_module_id = acceptance.owner_module_id
    verdict = acceptance.result
    lane = acceptance.lane
    _validate_cross_findings(
        verdict.new_findings,
        {owner_module_id: lane.module},
    )
    if previous is None:
        pending = {finding.id: finding for finding in acceptance.required_findings}
        resolved_ids: set[str] = set()
        finding_refs = [initial_result_ref]
        verdict_refs: list[str] = []
        all_findings = list(initial_result.findings)
        terminal_verdicts: dict[str, ResolutionVerdict] = {}
    else:
        pending = {finding.id: finding for finding in previous.pending}
        resolved_ids = set(previous.resolved_ids)
        finding_refs = list(previous.finding_refs)
        verdict_refs = list(previous.verdict_refs)
        all_findings = list(previous.findings)
        terminal_verdicts = {
            item.finding_id: item for item in previous.verdicts
        }

    verdict_refs.append(acceptance.result_ref)
    terminal_verdicts.update(
        {item.finding_id: item for item in verdict.verdicts}
    )
    escalated = [item for item in verdict.verdicts if item.verdict == "escalate"]
    main_accepts: set[str] = set()
    if escalated:
        decision = await _main_exception_decision(
            runner,
            state=state,
            workflow_id=workflow_id,
            scope="cross",
            subject_refs=[lane.completion.subject.ref],
            finding_refs=finding_refs,
            verdicts=escalated,
            responses=lane.responses,
        )
        if decision.decision == "accept_dispute":
            main_accepts = set(decision.finding_ids)

    next_pending = {
        item.finding_id: pending[item.finding_id]
        for item in verdict.verdicts
        if item.verdict == "open"
        or (
            item.verdict == "escalate"
            and item.finding_id not in main_accepts
        )
    }
    resolved_ids.update(
        item.finding_id
        for item in verdict.verdicts
        if item.verdict == "resolved" or item.finding_id in main_accepts
    )
    for finding in verdict.new_findings:
        if finding.id in pending or finding.id in resolved_ids:
            raise ReviewLifecycleError(
                f"new Cross owner finding reuses an existing id: {finding.id}"
            )
        next_pending[finding.id] = finding
        all_findings.append(finding)
    if verdict.new_findings:
        regression_ref = _write_immutable_model(
            runner,
            (
                f"Work/runs/{state['run_id']}/reviews/"
                f"cross-owner-regression-findings-r{acceptance.review_round}-"
                f"{owner_module_id}.json"
            ),
            CrossOwnerFindingSubmission(
                owner_module_id=owner_module_id,
                coverage=verdict.coverage,
                findings=verdict.new_findings,
                synthesis_inputs=[],
            ),
        )
        finding_refs.append(regression_ref)

    return CrossOwnerRoundProgress(
        run_id=acceptance.run_id,
        workflow_id=workflow_id,
        owner_module_id=owner_module_id,
        initial_input_ref=initial_input_ref,
        initial_result_ref=initial_result_ref,
        initial_result=initial_result,
        review_round=acceptance.review_round,
        next_review_round=acceptance.review_round + 1,
        next_owner_input_ref=acceptance.result_ref,
        next_action="revise" if next_pending else "completed",
        pending=list(next_pending.values()),
        resolved_ids=sorted(resolved_ids),
        finding_refs=finding_refs,
        verdict_refs=verdict_refs,
        findings=all_findings,
        verdicts=list(terminal_verdicts.values()),
        lane=lane,
        verdict_ref=acceptance.result_ref,
        verdict=verdict,
    )


def complete_cross_owner_round(
    runner: "ReportWorkflowRunner",
    *,
    progress: CrossOwnerRoundProgress,
) -> _CrossOwnerPipelineResult:
    """Promote an owner whose typed round state has no pending finding."""

    if progress.next_action != "completed":
        raise ReviewLifecycleError(
            f"Cross owner still requires revision: {progress.owner_module_id}"
        )
    lane = _promote_cross_owner_pipeline_completion(
        runner,
        lane=progress.lane,
        initial_result_ref=progress.initial_result_ref,
        verdict_ref=progress.verdict_ref,
    )
    return _CrossOwnerPipelineResult(
        owner_module_id=progress.owner_module_id,
        initial_input_ref=progress.initial_input_ref,
        initial_result_ref=progress.initial_result_ref,
        initial_result=progress.initial_result,
        lane=lane,
        verdict_ref=progress.verdict_ref,
        verdict=progress.verdict,
        finding_refs=progress.finding_refs,
        verdict_refs=progress.verdict_refs,
        findings=progress.findings,
        verdicts=progress.verdicts,
    )


def complete_cross_owner_without_findings(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    initial: CrossOwnerInitialReviewAcceptance,
    module: ModuleSubmission,
) -> _CrossOwnerPipelineResult:
    """Reuse the existing durable no-finding owner completion path."""

    owner_module_id = initial.owner_module_id
    _validate_cross_owner_synthesis_namespace(owner_module_id, initial.result)
    lane = _recover_cross_owner_lane(
        runner,
        state=state,
        owner_module_id=owner_module_id,
        review_round=1,
        owner_input_ref=initial.owner_input_ref,
        required_findings=[],
    )
    if lane is None:
        lane = _verified_cross_owner_noop(
            runner,
            state=state,
            owner_module_id=owner_module_id,
            module=module,
            owner_input_ref=initial.owner_input_ref,
            review_round=1,
        )
    lane = _promote_cross_owner_pipeline_completion(
        runner,
        lane=lane,
        initial_result_ref=initial.result_ref,
        verdict_ref=None,
    )
    return _CrossOwnerPipelineResult(
        owner_module_id=owner_module_id,
        initial_input_ref=initial.owner_input_ref,
        initial_result_ref=initial.result_ref,
        initial_result=initial.result,
        lane=lane,
        finding_refs=[initial.result_ref],
        findings=list(initial.result.findings),
    )


async def _run_cross_owner_lane(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    workflow_id: str,
    module_id: str,
    current: ModuleSubmission,
    findings: list[CrossReviewFinding],
    finding_refs: list[str],
    review_round: int,
    owner_input_ref: str | None = None,
    prior_completion_ref: str | None = None,
    defer_main_exceptions: bool = True,
    accepted_revision: CrossOwnerRevisionAcceptance | None = None,
    accepted_local_review: CrossOwnerLocalReviewAcceptance | None = None,
) -> _CrossOwnerLaneResult:
    """Run one owner-local revision and original-auditor regression in private state."""

    lane_state = deepcopy(state)
    lane_state["_defer_main_exceptions"] = defer_main_exceptions
    reviewed_baseline = current
    if accepted_local_review is None:
        persisted = (
            (accepted_revision.revised, accepted_revision.candidate_ref)
            if accepted_revision is not None
            else _load_cross_owner_revision_candidate(
                runner,
                state=state,
                module_id=module_id,
                current=current,
                findings=findings,
            )
        )

        while True:
            if persisted is not None:
                revised, revised_ref = persisted
                persisted = None
            else:
                revised, revised_ref = await request_module_revision(
                    runner,
                    state=lane_state,
                    workflow_id=workflow_id,
                    subject=current,
                    cross_findings=findings,
                )
            exceptional = [
                response
                for response in revised.revision_responses
                if response.action in {"disputed", "needs_input"}
            ]
            if exceptional:
                decision = await _main_exception_decision(
                    runner,
                    state=lane_state,
                    workflow_id=workflow_id,
                    scope="cross",
                    subject_refs=[revised_ref],
                    finding_refs=finding_refs,
                    verdicts=[],
                    responses=exceptional,
                    trigger="author_response",
                )
                if decision.decision == "return_to_author":
                    current = revised
                    continue
            break
        cross_responses = list(revised.revision_responses)
        regression_context, local_scope = _cross_owner_local_regression_context(
            runner,
            state=state,
            owner_module_id=module_id,
            reviewed_baseline=reviewed_baseline,
            revised=revised,
            findings=findings,
            review_round=review_round,
            prior_completion_ref=prior_completion_ref,
        )
        accepted_review = None
    else:
        revised = accepted_local_review.review.current
        cross_responses = list(accepted_local_review.cross_responses)
        regression_context = accepted_local_review.regression_context
        local_scope = {
            target_id for finding in findings for target_id in finding.target_submodule_ids
        }
        accepted_review = accepted_local_review.review

    lane_state.setdefault("module_submissions", {})[module_id] = revised
    lane_state.setdefault("specialist_submissions", {})[module_id] = revised
    if accepted_review is not None and accepted_review.next_action == "completed":
        local_reviewed = accepted_review.current
        lane_state.setdefault("module_review_completion_refs", {})[module_id] = cast(
            str, accepted_review.completion_ref
        )
    else:
        if accepted_review is not None:
            lane_state["resume"] = True
        local_reviewed = await run_module_review(
            runner,
            module_id,
            revised,
            lane_state,
            workflow_id,
            initial_scope=local_scope,
            lifecycle_id=f"cross-r{review_round}",
            regression_context=regression_context,
        )
    final_subject_ref = (
        f"Work/runs/{state['run_id']}/modules/{module_id}-r{local_reviewed.revision}.json"
    )
    local_review_ref = lane_state["module_review_completion_refs"][module_id]
    semantic_payload = {
        "run_id": state["run_id"],
        "review_round": review_round,
        "module_id": module_id,
        "owner_input_ref": owner_input_ref,
        "findings": [finding.model_dump(mode="json") for finding in findings],
        "schema_version": "1",
    }
    semantic_key = hashlib.sha256(
        json.dumps(
            semantic_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    completion = CrossOwnerCompletion(
        lane_id=f"cross-r{review_round}-module-{module_id}",
        run_id=state["run_id"],
        review_round=review_round,
        module_id=module_id,
        semantic_key=semantic_key,
        subject_revision=local_reviewed.revision,
        owner_input=(
            _cross_owner_artifact_ref(runner, owner_input_ref)
            if owner_input_ref is not None
            else None
        ),
        subject=_cross_owner_artifact_ref(runner, final_subject_ref),
        local_review_completion=_cross_owner_artifact_ref(runner, local_review_ref),
        author_task_attempt_id=f"cross-owner-attempt-{uuid4().hex}",
        reviewer_session_id=f"cross-owner-{module_id}",
        lease_epoch=1,
    )
    completion_ref = (
        f"Work/runs/{state['run_id']}/lanes/cross-r{review_round}/"
        f"module-{module_id}/completion-r{local_reviewed.revision}.json"
    )
    completion_path = runner.service.workspace / completion_ref
    if completion_path.is_file():
        try:
            existing = CrossOwnerCompletion.model_validate_json(
                completion_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ReviewLifecycleError(f"Cross owner completion is invalid: {module_id}") from exc
        _validate_cross_owner_completion_business_identity(
            runner,
            existing,
            run_id=state["run_id"],
            owner_module_id=module_id,
            review_round=review_round,
            owner_input_ref=owner_input_ref,
        )
        if not _cross_owner_completion_business_equal(existing, completion):
            raise ReviewLifecycleError(
                f"Cross owner completion changed for the same input: {module_id}"
            )
        completion = existing
    else:
        runner.service.store.write_json(
            completion_ref,
            completion.model_dump(mode="json"),
        )
    return _CrossOwnerLaneResult(
        module=local_reviewed,
        responses=cross_responses,
        local_review_ref=local_review_ref,
        completion_ref=completion_ref,
        completion=completion,
    )


class CrossReviewCoordinator:
    """Capability-owned coordinator for the five Cross owner pipelines.

    ``prepare`` materializes the shared frozen input snapshot, ``run_owner``
    executes one independent owner pipeline, and ``finalize`` reduces the
    typed owner results into the existing Cross aggregate.  This remains a
    package-local capability boundary; ``run_cross_review`` is the legacy
    compatibility entry point.
    """

    def __init__(
        self,
        runner: "ReportWorkflowRunner",
        state: dict,
        workflow_id: str,
    ) -> None:
        self.runner = runner
        self.state = state
        self.workflow_id = workflow_id
        self.owner_ids = tuple(REPORT_TAXONOMY)
        self.run_id = state["run_id"]
        self.modules: dict[str, ModuleSubmission] = dict(state["module_submissions"])
        self.completion_ref = f"Work/runs/{self.run_id}/reviews/cross-completion.json"
        self.completion_path = self.runner.service.workspace / self.completion_ref
        self.recovery = RecoveryStateStore(self.runner.service.workspace, self.run_id)
        self.recovered_owner_lanes = {}
        self.frozen_modules: dict[str, ModuleSubmission] | None = None
        self.initial_inputs: dict[str, tuple[CrossOwnerInput, str]] = {}
        self._resume_completed = False

    def _record_owner_terminal(
        self,
        owner_module_id: str,
        *,
        status: str,
        result_ref: str | None = None,
        revision: int = 0,
        error: str | None = None,
    ) -> None:
        # RecoveryStateStore validates the business lane id against the
        # referenced JSON payload.  Keep the typed Cross completion ref inside
        # a small owner-scoped projection so no hash/CAS field participates in
        # reuse decisions.
        projection_ref = f"Work/runs/{self.run_id}/recovery/cross-{owner_module_id}.json"
        if result_ref is not None:
            self.runner.service.store.write_json(
                projection_ref,
                {
                    "run_id": self.run_id,
                    "stage": "cross",
                    "lane_id": owner_module_id,
                    "status": status,
                    "revision": revision,
                    "result_ref": result_ref,
                },
            )
        current = self.recovery.load_lane_state("cross", owner_module_id)
        self.recovery.record_lane_attempt(
            {
                "run_id": self.run_id,
                "stage": "cross",
                "lane_id": owner_module_id,
                "task_id": owner_module_id,
                "attempt": (current.attempt + 1) if current is not None else 1,
                "revision": revision,
                "status": status,
                "result_ref": projection_ref if result_ref is not None else None,
                "error": error,
            }
        )

    def prepare(self) -> bool:
        """Prepare the shared frozen Cross snapshot and recovery state.

        Returns ``True`` when the existing aggregate was fully rehydrated and
        no owner should be dispatched.  Repeated calls are idempotent for the
        declarative runtime's reconstructed Action instances.
        """
        if self._resume_completed:
            return True
        self.recovered_owner_lanes = self.recovery.load_completed_lanes(
            "cross",
            list(self.owner_ids),
        )
        if (
            self.state.get("resume")
            and self.completion_path.is_file()
            and set(self.recovered_owner_lanes) == set(self.owner_ids)
        ):
            try:
                completion, _completion_artifacts = self.runner._load_current_review_completion(
                    run_id=self.run_id,
                    completion_ref=self.completion_ref,
                    lifecycle="cross",
                    reviewer_agent_id="cross-module-reviewer",
                    reviewer_session_key="cross-owner-wave",
                )
            except (AttributeError, OSError, ValueError) as exc:
                raise ReviewLifecycleError("Cross completion is unreadable during resume") from exc
            barrier_ref = f"Work/runs/{self.run_id}/lanes/cross-r1/owner-barrier.json"
            barrier_path = self.runner.service.workspace / barrier_ref
            try:
                barrier = CrossOwnerBarrier.model_validate_json(
                    barrier_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise ReviewLifecycleError(
                    "Cross owner barrier is unreadable during resume"
                ) from exc
            _verify_cross_owner_barrier(
                self.runner,
                barrier,
                barrier_path=barrier_path,
                expected_run_id=self.run_id,
                expected_round=1,
                expected_modules=set(self.owner_ids),
            )
            if len(completion.subject_refs) != len(self.owner_ids) or len(
                set(completion.subject_refs)
            ) != len(self.owner_ids):
                raise ReviewLifecycleError("Cross completion must bind exactly five owner subjects")
            # Rehydrate the business aggregate's module subjects and synthesis
            # portfolio before returning.  The status projection is intentionally
            # ignored; each typed subject is loaded from the aggregate's refs.
            for module_id, subject_ref in zip(
                self.owner_ids,
                completion.subject_refs[: len(self.owner_ids)],
                strict=True,
            ):
                try:
                    subject = ModuleSubmission.model_validate_json(
                        (self.runner.service.workspace / subject_ref).read_text(encoding="utf-8")
                    )
                except (OSError, ValueError) as exc:
                    raise ReviewLifecycleError(
                        f"Cross aggregate subject is unreadable: {module_id}"
                    ) from exc
                if subject.module_id != module_id:
                    raise ReviewLifecycleError(
                        f"Cross aggregate subject ownership mismatch: {module_id}"
                    )
                self.state.setdefault("module_submissions", {})[module_id] = subject
                self.state.setdefault("specialist_submissions", {})[module_id] = subject
                lane_state = self.recovered_owner_lanes.get(module_id)
                if lane_state is not None:
                    projection_ref = getattr(lane_state, "result_ref", None)
                    if not projection_ref:
                        raise ReviewLifecycleError(
                            f"Cross owner recovery projection is missing: {module_id}"
                        )
                    projection_path = _cross_owner_artifact_path(
                        self.runner,
                        projection_ref,
                        run_id=self.run_id,
                        label=f"recovery projection {module_id}",
                    )
                    try:
                        projection = json.loads(projection_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError, TypeError) as exc:
                        raise ReviewLifecycleError(
                            f"Cross owner recovery projection is invalid: {module_id}"
                        ) from exc
                    owner_completion_ref = projection.get("result_ref")
                    if owner_completion_ref != barrier.completion_refs[module_id]:
                        raise ReviewLifecycleError(
                            f"Cross owner recovery projection ref mismatch: {module_id}"
                        )
                    lane = _recover_cross_owner_lane_from_recovery(
                        self.runner,
                        state=self.state,
                        owner_module_id=module_id,
                        completion_ref=owner_completion_ref,
                        required_findings=[],
                        review_round=1,
                    )
                    if lane is None or not lane.local_review_ref:
                        raise ReviewLifecycleError(
                            f"Cross owner recovery completion is incomplete: {module_id}"
                        )
                    expected_subject_ref = completion.subject_refs[self.owner_ids.index(module_id)]
                    actual_subject_ref = _cross_owner_artifact_ref_value(lane.completion.subject)
                    if actual_subject_ref != expected_subject_ref or lane.module.model_dump(
                        mode="json"
                    ) != subject.model_dump(mode="json"):
                        raise ReviewLifecycleError(
                            f"Cross completion subject does not bind owner lane: {module_id}"
                        )
                    self.state.setdefault("module_review_completion_refs", {})[module_id] = (
                        lane.local_review_ref
                    )
            # RecoveryStateStore is authoritative for reusable owner lanes.  The
            # legacy barrier remains a status artifact, but its hashes/CAS fields
            # are not consulted for active recovery.
            self.state["cross_owner_barrier_ref"] = (
                f"Work/runs/{self.run_id}/lanes/cross-r1/owner-barrier.json"
            )
            self.state["cross_review_completion_ref"] = self.completion_ref
            self.state["cross_synthesis_inputs"] = _load_cross_synthesis_from_refs(
                self.runner, completion.finding_refs
            )
            self._resume_completed = True
            return True

        # All initial inputs are written before any owner dispatch.  On resume, any
        # one verified existing input reconstructs the common pre-Cross snapshot so
        # already revised owner modules cannot leak into a sibling's retry context.
        self.frozen_modules: dict[str, ModuleSubmission] | None = None
        for owner_module_id in self.owner_ids:
            loaded = _load_cross_owner_input(
                self.runner,
                run_id=self.run_id,
                owner_module_id=owner_module_id,
                review_round=0,
                phase="initial",
            )
            if loaded is not None:
                self.frozen_modules = _cross_owner_modules_from_input(self.runner, loaded[0])
                break
        if self.frozen_modules is None:
            self.frozen_modules = self.modules

        self.initial_inputs: dict[str, tuple[CrossOwnerInput, str]] = {}
        for owner_module_id in self.owner_ids:
            loaded = _load_cross_owner_input(
                self.runner,
                run_id=self.run_id,
                owner_module_id=owner_module_id,
                review_round=0,
                phase="initial",
            )
            if loaded is None:
                loaded = _cross_owner_input(
                    self.runner,
                    state=self.state,
                    modules=self.frozen_modules,
                    owner_module_id=owner_module_id,
                    phase="initial",
                    review_round=0,
                )
            loaded_modules = _cross_owner_modules_from_input(self.runner, loaded[0])
            for module_id in self.owner_ids:
                expected_ref = (
                    f"Work/runs/{self.run_id}/modules/"
                    f"{module_id}-r{self.frozen_modules[module_id].revision}.json"
                )
                actual_ref = (
                    loaded[0].owner_subject_ref
                    if module_id == owner_module_id
                    else loaded[0].related_module_refs[module_id]
                )
                if (
                    actual_ref != expected_ref
                    or loaded_modules[module_id] != self.frozen_modules[module_id]
                ):
                    raise ReviewLifecycleError(
                        f"Cross owner initial inputs do not share one frozen snapshot: "
                        f"{owner_module_id}/{module_id}"
                    )
            self.initial_inputs[owner_module_id] = loaded

        return False

    def ensure_prepared(self) -> bool:
        """Lazily prepare a reconstructed declarative coordinator instance."""
        if self._resume_completed:
            return True
        if self.initial_inputs:
            return False
        return self.prepare()

    def prepare_owner_initial(
        self,
        owner_module_id: str,
    ) -> CrossOwnerInitialReviewPreparation:
        """Prepare one owner initial reviewer turn for a generic runtime.

        The common frozen input is prepared first.  A durable typed initial
        result is returned as ``continue_existing``; otherwise the caller can
        invoke the returned Agent ``envelope`` and pass the result to
        :meth:`accept_owner_initial`.
        """

        aggregate_recovered = self.ensure_prepared()
        if aggregate_recovered:
            loaded_input = _load_cross_owner_input(
                self.runner,
                run_id=self.run_id,
                owner_module_id=owner_module_id,
                review_round=0,
                phase="initial",
            )
            if loaded_input is None:
                raise ReviewLifecycleError(
                    f"Cross owner initial input is missing after aggregate recovery: "
                    f"{owner_module_id}"
                )
            owner_input, owner_input_ref = loaded_input
        else:
            owner_input, owner_input_ref = self.initial_inputs[owner_module_id]

        pipeline_completion_path = (
            self.runner.service.workspace / f"Work/runs/{self.run_id}/lanes/cross-r1/"
            f"module-{owner_module_id}/pipeline-completion.json"
        )
        require_legacy_task_binding = (
            bool(self.state.get("resume")) and not pipeline_completion_path.is_file()
        )
        initial_loaded = _load_cross_owner_initial_result(
            self.runner,
            run_id=self.run_id,
            owner_module_id=owner_module_id,
            require_task_binding=require_legacy_task_binding,
        )
        reviewer_session_key = f"cross-owner-{owner_module_id}"
        if initial_loaded is not None:
            existing_result, existing_result_ref = initial_loaded
            return CrossOwnerInitialReviewPreparation(
                mode="continue_existing",
                run_id=self.run_id,
                workflow_id=self.workflow_id,
                owner_module_id=owner_module_id,
                review_round=0,
                reviewer_session_key=reviewer_session_key,
                owner_input_ref=owner_input_ref,
                owner_input=owner_input,
                existing_result_ref=existing_result_ref,
                existing_result=existing_result,
            )
        envelope = _build_cross_owner_review_envelope(
            self.runner,
            state=self.state,
            owner_module_id=owner_module_id,
            phase="initial",
            review_round=0,
            owner_input_ref=owner_input_ref,
        )
        return CrossOwnerInitialReviewPreparation(
            mode="invoke_agent",
            run_id=self.run_id,
            workflow_id=self.workflow_id,
            owner_module_id=owner_module_id,
            review_round=0,
            reviewer_session_key=reviewer_session_key,
            owner_input_ref=owner_input_ref,
            owner_input=owner_input,
            envelope=envelope,
        )

    def accept_owner_initial(
        self,
        preparation: CrossOwnerInitialReviewPreparation,
        result: CrossOwnerFindingSubmission | None = None,
    ) -> CrossOwnerInitialReviewAcceptance:
        """Accept one generic-runtime Cross-owner initial reviewer result."""

        return _accept_cross_owner_initial_review(
            self.runner,
            preparation=preparation,
            result=result,
        )

    async def prepare_owner_revision(
        self,
        initial: CrossOwnerInitialReviewAcceptance,
        progress: CrossOwnerRoundProgress | None = None,
    ) -> CrossOwnerRevisionPreparation:
        """Prepare or recover the current original-Author Cross revision."""

        owner_module_id = initial.owner_module_id
        if progress is not None:
            return await prepare_cross_owner_revision(
                self.runner,
                state=self.state,
                workflow_id=self.workflow_id,
                owner_module_id=owner_module_id,
                review_round=progress.next_review_round,
                owner_input_ref=progress.next_owner_input_ref,
                current=progress.lane.module,
                findings=list(progress.pending),
                finding_refs=list(progress.finding_refs),
                prior_completion_ref=progress.lane.local_review_ref,
            )
        return await prepare_cross_owner_revision(
            self.runner,
            state=self.state,
            workflow_id=self.workflow_id,
            owner_module_id=owner_module_id,
            review_round=1,
            owner_input_ref=initial.owner_input_ref,
            current=cast(dict[str, ModuleSubmission], self.frozen_modules)[owner_module_id],
            findings=list(initial.result.findings),
            finding_refs=[initial.result_ref],
            prior_completion_ref=self.state.get("module_review_completion_refs", {}).get(
                owner_module_id
            ),
        )

    def accept_owner_revision(
        self,
        preparation: CrossOwnerRevisionPreparation,
        result: ModuleRevisionSubmission | None = None,
    ) -> CrossOwnerRevisionAcceptance:
        """Accept one generic-runtime original-Author Cross revision."""

        return accept_cross_owner_revision(
            self.runner,
            preparation=preparation,
            result=result,
        )

    async def prepare_owner_local_review(
        self,
        revision: CrossOwnerRevisionAcceptance,
    ) -> CrossOwnerLocalReviewPreparation:
        """Prepare the first original-Auditor local regression."""

        return await prepare_cross_owner_local_review(
            self.runner,
            state=self.state,
            workflow_id=self.workflow_id,
            revision=revision,
        )

    def accept_owner_local_review(
        self,
        preparation: CrossOwnerLocalReviewPreparation,
        result: ModuleReviewFindingSubmission | None = None,
    ) -> CrossOwnerLocalReviewAcceptance:
        """Accept one declared original-Auditor local regression result."""

        return accept_cross_owner_local_review(
            self.runner,
            state=self.state,
            preparation=preparation,
            result=result,
        )

    async def prepare_owner_recheck(
        self,
        initial: CrossOwnerInitialReviewAcceptance,
        revision: CrossOwnerRevisionAcceptance,
        local_review: CrossOwnerLocalReviewAcceptance,
    ) -> CrossOwnerRecheckPreparation:
        """Prepare the first original Cross owner reviewer recheck."""

        self.ensure_prepared()
        owner_module_id = initial.owner_module_id
        _validate_cross_owner_synthesis_namespace(owner_module_id, initial.result)
        findings = list(revision.findings)
        review_round = revision.review_round
        lane = _recover_cross_owner_lane(
            self.runner,
            state=self.state,
            owner_module_id=owner_module_id,
            review_round=review_round,
            owner_input_ref=revision.owner_input_ref,
            required_findings=findings,
        )
        if lane is None:
            lane = await _run_cross_owner_lane(
                self.runner,
                state=self.state,
                workflow_id=self.workflow_id,
                module_id=owner_module_id,
                current=revision.current,
                findings=findings,
                finding_refs=list(revision.finding_refs),
                review_round=review_round,
                owner_input_ref=revision.owner_input_ref,
                prior_completion_ref=revision.prior_completion_ref,
                accepted_revision=revision,
                accepted_local_review=local_review,
            )
        return prepare_cross_owner_recheck(
            self.runner,
            state=self.state,
            workflow_id=self.workflow_id,
            frozen_modules=cast(dict[str, ModuleSubmission], self.frozen_modules),
            initial=initial,
            lane=lane,
            review_round=review_round,
            required_findings=findings,
        )

    def accept_owner_recheck(
        self,
        preparation: CrossOwnerRecheckPreparation,
        result: CrossOwnerVerdictSubmission | None = None,
    ) -> CrossOwnerRecheckAcceptance:
        """Accept one original Cross owner reviewer recheck."""

        return accept_cross_owner_recheck(
            self.runner,
            preparation=preparation,
            result=result,
        )

    async def advance_owner_round(
        self,
        initial: CrossOwnerInitialReviewAcceptance,
        recheck: CrossOwnerRecheckAcceptance,
        progress: CrossOwnerRoundProgress | None = None,
    ) -> CrossOwnerRoundProgress:
        """Advance the shared owner round state after an accepted verdict."""

        return await advance_cross_owner_round(
            self.runner,
            state=self.state,
            workflow_id=self.workflow_id,
            initial_input_ref=initial.owner_input_ref,
            initial_result_ref=initial.result_ref,
            initial_result=initial.result,
            acceptance=recheck,
            previous=progress,
        )

    def complete_owner_round(
        self,
        progress: CrossOwnerRoundProgress,
    ) -> _CrossOwnerPipelineResult:
        """Promote one completed typed owner round."""

        return complete_cross_owner_round(
            self.runner,
            progress=progress,
        )

    def complete_owner_without_findings(
        self,
        initial: CrossOwnerInitialReviewAcceptance,
    ) -> _CrossOwnerPipelineResult:
        """Complete one accepted owner whose initial result has no findings."""

        self.ensure_prepared()
        return complete_cross_owner_without_findings(
            self.runner,
            state=self.state,
            initial=initial,
            module=cast(dict[str, ModuleSubmission], self.frozen_modules)[
                initial.owner_module_id
            ],
        )

    async def run_owner(
        self,
        owner_module_id: str,
        *,
        initial_acceptance: CrossOwnerInitialReviewAcceptance | None = None,
        revision_acceptance: CrossOwnerRevisionAcceptance | None = None,
        local_review_acceptance: CrossOwnerLocalReviewAcceptance | None = None,
        recheck_acceptance: CrossOwnerRecheckAcceptance | None = None,
    ) -> _CrossOwnerPipelineResult:
        if self.ensure_prepared():
            raise ReviewLifecycleError(
                f"Cross aggregate is already complete; owner dispatch is not required: {owner_module_id}"
            )
        initial_contract, initial_input_ref = self.initial_inputs[owner_module_id]
        recovered_state = self.recovered_owner_lanes.get(owner_module_id)
        if recovered_state is not None:
            projection_ref = recovered_state.result_ref
            completion_ref_from_projection: str | None = None
            if projection_ref:
                try:
                    projection = json.loads(
                        (self.runner.service.workspace / projection_ref).read_text(encoding="utf-8")
                    )
                    completion_ref_from_projection = projection.get("result_ref")
                except (OSError, ValueError, TypeError):
                    completion_ref_from_projection = None
            if completion_ref_from_projection:
                try:
                    completion_path = self.runner.service.workspace / completion_ref_from_projection
                    completion = CrossOwnerCompletion.model_validate_json(
                        completion_path.read_text(encoding="utf-8")
                    )
                    initial_result_ref = (
                        completion.initial_result.ref
                        if completion.initial_result is not None
                        else f"Work/runs/{self.run_id}/reviews/cross-owner-findings-r0-{owner_module_id}.json"
                    )
                    initial_result = CrossOwnerFindingSubmission.model_validate_json(
                        (self.runner.service.workspace / initial_result_ref).read_text(
                            encoding="utf-8"
                        )
                    )
                    finding_refs = [initial_result_ref]
                    all_findings = list(initial_result.findings)
                    regression_root = self.runner.service.workspace / (
                        f"Work/runs/{self.run_id}/reviews"
                    )
                    for regression_path in sorted(
                        regression_root.glob(
                            f"cross-owner-regression-findings-r*-{owner_module_id}.json"
                        )
                    ):
                        try:
                            regression = CrossOwnerFindingSubmission.model_validate_json(
                                regression_path.read_text(encoding="utf-8")
                            )
                        except (OSError, ValueError):
                            continue
                        if regression.owner_module_id == owner_module_id:
                            finding_refs.append(
                                regression_path.relative_to(
                                    self.runner.service.workspace
                                ).as_posix()
                            )
                            all_findings.extend(regression.findings)
                    verdict = None
                    verdict_ref = (
                        completion.verdict_result.ref
                        if completion.verdict_result is not None
                        else None
                    )
                    verdict_refs: list[str] = []
                    verdicts: list[ResolutionVerdict] = []
                    if verdict_ref:
                        try:
                            verdict = CrossOwnerVerdictSubmission.model_validate_json(
                                (self.runner.service.workspace / verdict_ref).read_text(
                                    encoding="utf-8"
                                )
                            )
                            verdict_refs.append(verdict_ref)
                            verdicts = list(verdict.verdicts)
                        except (OSError, ValueError):
                            verdict = None
                    lane = _recover_cross_owner_lane_from_recovery(
                        self.runner,
                        state=self.state,
                        owner_module_id=owner_module_id,
                        completion_ref=completion_ref_from_projection,
                        required_findings=[],
                        review_round=1,
                    )
                    if lane is not None:
                        return _CrossOwnerPipelineResult(
                            owner_module_id=owner_module_id,
                            initial_input_ref=initial_input_ref,
                            initial_result_ref=initial_result_ref,
                            initial_result=initial_result,
                            lane=lane,
                            verdict_ref=(verdict_ref),
                            verdict=verdict,
                            finding_refs=finding_refs,
                            verdict_refs=verdict_refs,
                            findings=all_findings,
                            verdicts=verdicts,
                        )
                except (OSError, ValueError, TypeError, AttributeError):
                    # A stale/missing business result is not reusable; the
                    # owner is explicitly retried below.
                    pass
        pipeline_completion_path = (
            self.runner.service.workspace / f"Work/runs/{self.run_id}/lanes/cross-r1/"
            f"module-{owner_module_id}/pipeline-completion.json"
        )
        require_legacy_task_binding = (
            bool(self.state.get("resume")) and not pipeline_completion_path.is_file()
        )
        initial_loaded = (
            (initial_acceptance.result, initial_acceptance.result_ref)
            if initial_acceptance is not None
            else _load_cross_owner_initial_result(
                self.runner,
                run_id=self.run_id,
                owner_module_id=owner_module_id,
                require_task_binding=require_legacy_task_binding,
            )
        )
        if initial_loaded is None:
            initial_result, initial_result_ref = await _run_cross_owner_review(
                self.runner,
                state=self.state,
                workflow_id=self.workflow_id,
                modules=self.frozen_modules,
                owner_module_id=owner_module_id,
                phase="initial",
                review_round=0,
                owner_input_ref=initial_input_ref,
            )
            assert isinstance(initial_result, CrossOwnerFindingSubmission)
        else:
            initial_result, initial_result_ref = initial_loaded

        pending = {finding.id: finding for finding in initial_result.findings}
        finding_refs = [initial_result_ref]
        all_findings = list(initial_result.findings)
        round_progress: CrossOwnerRoundProgress | None = None
        current = self.frozen_modules[owner_module_id]
        current_review_completion_ref = self.state.get("module_review_completion_refs", {}).get(
            owner_module_id
        )
        review_round = 1
        owner_input_ref = initial_input_ref

        if not pending:
            return complete_cross_owner_without_findings(
                self.runner,
                state=self.state,
                initial=CrossOwnerInitialReviewAcceptance(
                    run_id=self.run_id,
                    workflow_id=self.workflow_id,
                    owner_module_id=owner_module_id,
                    reviewer_session_key=f"cross-owner-{owner_module_id}",
                    owner_input_ref=initial_input_ref,
                    result_ref=initial_result_ref,
                    result=initial_result,
                ),
                module=current,
            )

        _validate_cross_owner_synthesis_namespace(owner_module_id, initial_result)
        while pending:
            findings = list(pending.values())
            accepted_recheck = recheck_acceptance if review_round == 1 else None
            if accepted_recheck is not None:
                lane = accepted_recheck.lane
            else:
                lane = _recover_cross_owner_lane(
                    self.runner,
                    state=self.state,
                    owner_module_id=owner_module_id,
                    review_round=review_round,
                    owner_input_ref=owner_input_ref,
                    required_findings=findings,
                )
                if lane is None:
                    lane = await _run_cross_owner_lane(
                        self.runner,
                        state=self.state,
                        workflow_id=self.workflow_id,
                        module_id=owner_module_id,
                        current=current,
                        findings=findings,
                        finding_refs=finding_refs,
                        review_round=review_round,
                        owner_input_ref=owner_input_ref,
                        prior_completion_ref=current_review_completion_ref,
                        accepted_revision=(revision_acceptance if review_round == 1 else None),
                        accepted_local_review=(
                            local_review_acceptance if review_round == 1 else None
                        ),
                    )

            # A later Cross regression round reviews the module revision that
            # the immediately preceding owner-local audit completed.  Keep
            # that completion binding owner-local until the exact-five barrier
            # commits all lanes into shared workflow state.
            current_review_completion_ref = lane.local_review_ref

            verdict_loaded = (
                (accepted_recheck.result, accepted_recheck.result_ref)
                if accepted_recheck is not None
                else _load_cross_owner_verdict(
                    self.runner,
                    run_id=self.run_id,
                    owner_module_id=owner_module_id,
                    review_round=review_round,
                    required_findings=findings,
                    require_task_binding=require_legacy_task_binding,
                )
            )
            modules_for_recheck = dict(self.frozen_modules)
            modules_for_recheck[owner_module_id] = lane.module
            recheck_input_ref = (
                f"Work/runs/{self.run_id}/reviews/"
                f"cross-owner-input-r{review_round}-{owner_module_id}.json"
            )
            if verdict_loaded is None:
                recheck_loaded = _load_cross_owner_input(
                    self.runner,
                    run_id=self.run_id,
                    owner_module_id=owner_module_id,
                    review_round=review_round,
                    phase="recheck",
                )
                if recheck_loaded is None:
                    _contract, recheck_input_ref = _cross_owner_input(
                        self.runner,
                        state=self.state,
                        modules=modules_for_recheck,
                        owner_module_id=owner_module_id,
                        phase="recheck",
                        review_round=review_round,
                        required_findings=findings,
                        revision_responses=lane.responses,
                        local_review_ref=lane.local_review_ref,
                        prior_synthesis_inputs=list(initial_result.synthesis_inputs),
                    )
                else:
                    recheck_contract, recheck_input_ref = recheck_loaded
                    if {finding.id for finding in recheck_contract.required_findings} != set(
                        pending
                    ) or recheck_contract.owner_subject_ref != lane.completion.subject.ref:
                        raise ReviewLifecycleError(
                            f"Cross owner recheck input does not bind recovered lane: "
                            f"{owner_module_id}/r{review_round}"
                        )
                verdict, verdict_ref = await _run_cross_owner_review(
                    self.runner,
                    state=self.state,
                    workflow_id=self.workflow_id,
                    modules=modules_for_recheck,
                    owner_module_id=owner_module_id,
                    phase="recheck",
                    review_round=review_round,
                    owner_input_ref=recheck_input_ref,
                    required_findings=findings,
                    revision_responses=lane.responses,
                    local_review_ref=lane.local_review_ref,
                    prior_synthesis_inputs=list(initial_result.synthesis_inputs),
                )
                assert isinstance(verdict, CrossOwnerVerdictSubmission)
            else:
                verdict, verdict_ref = verdict_loaded

            accepted_round = (
                accepted_recheck
                if accepted_recheck is not None
                else CrossOwnerRecheckAcceptance(
                    run_id=self.run_id,
                    workflow_id=self.workflow_id,
                    owner_module_id=owner_module_id,
                    review_round=review_round,
                    reviewer_session_key=f"cross-owner-{owner_module_id}",
                    initial_result_ref=initial_result_ref,
                    initial_result=initial_result,
                    lane=lane,
                    owner_input_ref=recheck_input_ref,
                    required_findings=findings,
                    result_ref=verdict_ref,
                    result=verdict,
                )
            )
            round_progress = await advance_cross_owner_round(
                self.runner,
                state=self.state,
                workflow_id=self.workflow_id,
                initial_input_ref=initial_input_ref,
                initial_result_ref=initial_result_ref,
                initial_result=initial_result,
                acceptance=accepted_round,
                previous=round_progress,
            )
            if round_progress.next_action == "completed":
                return complete_cross_owner_round(
                    self.runner,
                    progress=round_progress,
                )

            pending = {finding.id: finding for finding in round_progress.pending}
            finding_refs = list(round_progress.finding_refs)
            all_findings = list(round_progress.findings)
            current = round_progress.lane.module
            owner_input_ref = round_progress.next_owner_input_ref
            review_round = round_progress.next_review_round

        raise ReviewLifecycleError(f"Cross owner loop ended without completion: {owner_module_id}")

    async def reduce(self, outcomes: dict[str, object]) -> None:
        """Reduce drained owner outcomes into the existing Cross aggregate."""
        self.ensure_prepared()
        if self._resume_completed:
            return
        normalized_outcomes: dict[str, object] = {}
        for owner_module_id, outcome in outcomes.items():
            if isinstance(outcome, dict):
                try:
                    outcome = _CrossOwnerPipelineResult.model_validate(outcome)
                except Exception as exc:
                    outcome = exc
            normalized_outcomes[owner_module_id] = outcome
        outcomes = normalized_outcomes
        errors = {
            owner_module_id: outcome
            for owner_module_id, outcome in outcomes.items()
            if isinstance(outcome, BaseException)
        }
        if errors:
            completion_refs = {
                owner_module_id: outcome.lane.completion_ref
                for owner_module_id, outcome in outcomes.items()
                if isinstance(outcome, _CrossOwnerPipelineResult)
            }
            terminal_ref = f"Work/runs/{self.run_id}/lanes/cross-r1/owner-terminal.json"
            self.runner.service.store.write_json(
                terminal_ref,
                {
                    "kind": "cross_owner_terminal_barrier",
                    "version": 2,
                    "run_id": self.run_id,
                    "review_round": 1,
                    "target_modules": list(self.owner_ids),
                    "status": "failed",
                    "terminal_statuses": {
                        owner_module_id: ("failed" if owner_module_id in errors else "completed")
                        for owner_module_id in self.owner_ids
                    },
                    "completion_refs": completion_refs,
                    "completion_hashes": {
                        owner_module_id: outcomes[
                            owner_module_id
                        ].lane.completion.completion_sha256()
                        for owner_module_id in completion_refs
                    },
                    "failure_messages": {
                        owner_module_id: str(error) for owner_module_id, error in errors.items()
                    },
                    "retry_scope": sorted(errors, key=float),
                    "retry_policy": (
                        "explicit resume reuses verified completed owners and "
                        "dispatches only retry_scope; failed Provider attempts do not block recovery"
                    ),
                },
            )
            for owner_module_id, outcome in outcomes.items():
                if isinstance(outcome, _CrossOwnerPipelineResult):
                    self._record_owner_terminal(
                        owner_module_id,
                        status="completed",
                        result_ref=outcome.lane.completion_ref,
                        revision=outcome.lane.module.revision,
                    )
                else:
                    self._record_owner_terminal(
                        owner_module_id,
                        status="failed",
                        error=str(outcome),
                    )
            raise sorted(errors.items(), key=lambda item: float(item[0]))[0][1]

        pipelines = {
            owner_module_id: outcome
            for owner_module_id, outcome in outcomes.items()
            if isinstance(outcome, _CrossOwnerPipelineResult)
        }
        if set(pipelines) != set(self.owner_ids):
            raise ReviewLifecycleError("Cross owner drain did not produce exactly five outcomes")

        finding_ids = [
            finding.id
            for owner_module_id in self.owner_ids
            for finding in pipelines[owner_module_id].findings
        ]
        if len(finding_ids) != len(set(finding_ids)):
            raise ReviewLifecycleError("Cross owner findings reused an id")
        synthesis_by_id: dict[str, CrossSynthesisInput] = {}
        all_verdicts: list[ResolutionVerdict] = []
        for owner_module_id in self.owner_ids:
            pipeline = pipelines[owner_module_id]
            for item in pipeline.initial_result.synthesis_inputs:
                if item.id in synthesis_by_id and synthesis_by_id[item.id] != item:
                    raise ReviewLifecycleError(
                        "Cross owner synthesis reused an id with different content"
                    )
                synthesis_by_id[item.id] = item
            all_verdicts.extend(pipeline.verdicts)

        final_modules = dict(self.frozen_modules)
        barrier_inputs: list[tuple[str, CrossOwnerCompletion]] = []
        for owner_module_id in self.owner_ids:
            pipeline = pipelines[owner_module_id]
            final_modules[owner_module_id] = pipeline.lane.module
            self.state.setdefault("module_submissions", {})[owner_module_id] = pipeline.lane.module
            self.state.setdefault("specialist_submissions", {})[owner_module_id] = (
                pipeline.lane.module
            )
            self.state.setdefault("module_review_completion_refs", {})[owner_module_id] = (
                pipeline.lane.local_review_ref
            )
            barrier_inputs.append((pipeline.lane.completion_ref, pipeline.lane.completion))

        aggregate_findings = CrossReviewFindingSubmission(
            coverage=[
                CrossReviewCoverageEntry(
                    module_id=owner_module_id,
                    checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
                )
                for owner_module_id in self.owner_ids
            ],
            findings=[
                finding
                for owner_module_id in self.owner_ids
                for finding in pipelines[owner_module_id].findings
            ],
            synthesis_inputs=list(synthesis_by_id.values()),
        )
        aggregate_finding_ref = _write_immutable_model(
            self.runner,
            f"Work/runs/{self.run_id}/reviews/cross-findings-r0.json",
            aggregate_findings,
        )
        aggregate_verdicts = CrossReviewVerdictSubmission(
            coverage=[
                CrossReviewCoverageEntry(
                    module_id=owner_module_id,
                    checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
                )
                for owner_module_id in self.owner_ids
            ],
            verdicts=all_verdicts,
            new_findings=[],
            # Recheck cannot rewrite synthesis.  The exact initial owner artifacts
            # are the sole canonical source.
            synthesis_inputs=list(synthesis_by_id.values()),
        )
        aggregate_verdict_ref = _write_immutable_model(
            self.runner,
            f"Work/runs/{self.run_id}/reviews/cross-verdicts-r1.json",
            aggregate_verdicts,
        )
        reducer = WorkflowReducer(self.runner.service.workspace, self.run_id)
        reducer.write_cross_owner_barrier(1, list(self.owner_ids), barrier_inputs)
        barrier_ref = f"Work/runs/{self.run_id}/lanes/cross-r1/owner-barrier.json"
        # ``CrossOwnerBarrier`` is a durable projection.  Recovery decisions use
        # the owner lane business records below; do not gate this stage on the
        # legacy completion/barrier hashes.
        self.state["cross_owner_barrier_ref"] = barrier_ref

        completion = ReviewCompletionRecord(
            review_protocol_version=2,
            lifecycle="cross",
            run_id=self.run_id,
            reviewer_agent_id="cross-module-reviewer",
            reviewer_session_key="cross-owner-wave",
            subject_refs=[
                f"Work/runs/{self.run_id}/modules/"
                f"{module_id}-r{final_modules[module_id].revision}.json"
                for module_id in self.owner_ids
            ],
            finding_refs=[aggregate_finding_ref],
            verdict_refs=[aggregate_verdict_ref],
            resolved_finding_ids=sorted(item.finding_id for item in all_verdicts),
        )
        _write_immutable_model(self.runner, self.completion_ref, completion)
        self.state["cross_review_completion_ref"] = self.completion_ref
        self.state["cross_synthesis_inputs"] = list(synthesis_by_id.values())
        for owner_module_id in self.owner_ids:
            pipeline = pipelines[owner_module_id]
            self._record_owner_terminal(
                owner_module_id,
                status="completed",
                result_ref=pipeline.lane.completion_ref,
                revision=pipeline.lane.module.revision,
            )
        try:
            self.recovery.record_aggregate(
                AggregateState(
                    run_id=self.run_id,
                    stage="cross",
                    lane_ids=list(self.owner_ids),
                    result_ref=self.completion_ref,
                    revision=1,
                    status="completed",
                )
            )
        except Exception as exc:
            self.recovery.recover_aggregate_failure(
                "cross",
                self.owner_ids,
                previous_stage="module",
                reason=str(exc),
            )
            raise

    async def finalize(self, outcomes: dict[str, object]) -> None:
        """Declarative-friendly alias for the aggregate reduction boundary."""
        await self.reduce(outcomes)


async def run_cross_review(
    runner: "ReportWorkflowRunner",
    state: dict,
    workflow_id: str,
) -> None:
    """Run five independently recoverable Cross-owner pipelines.

    This compatibility entry point delegates to ``CrossReviewCoordinator``
    while preserving the original gather, recovery, and aggregate semantics.
    """
    coordinator = CrossReviewCoordinator(runner, state, workflow_id)
    if coordinator.prepare():
        return
    outcomes = dict(
        zip(
            coordinator.owner_ids,
            await asyncio.gather(
                *(
                    coordinator.run_owner(owner_module_id)
                    for owner_module_id in coordinator.owner_ids
                ),
                return_exceptions=True,
            ),
            strict=True,
        )
    )
    await coordinator.finalize(outcomes)


async def _request_chief_revision(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    workflow_id: str,
    current: EditedReportSubmission,
    current_ref: str,
    pending: dict[str, FinalReviewFinding],
    approved_module_text: dict[str, str],
    claims: list,
    aggregate_mode: bool,
    chief_envelope: TaskEnvelope,
    chief_session_key: str,
    revision_number: int,
) -> tuple[EditedReportSubmission, str]:
    target_sections = {
        section_id for finding in pending.values() for section_id in finding.target_section_ids
    }
    consistency_section_ids = {"1.1", "1.2", "1.3"}
    if any(section_id.startswith(("3.", "4.")) for section_id in target_sections):
        consistency_section_ids.update({"3.1.1", "3.1.3", "3.2"})
    consistency_section_ids.difference_update(target_sections)
    revision_input = ChiefRevisionInput(
        run_id=state["run_id"],
        subject_ref=current_ref,
        target_section_bodies={
            section_id: getattr(
                current,
                CHIEF_SECTION_RESULT_PART_IDS[section_id],
            )
            for section_id in sorted(target_sections)
        },
        consistency_context={
            section_id: getattr(
                current,
                CHIEF_SECTION_RESULT_PART_IDS[section_id],
            )
            for section_id in sorted(consistency_section_ids)
        },
        revision=revision_number,
        target_section_ids=sorted(target_sections),
        findings=list(pending.values()),
    )
    revision_input_ref = _write_model(
        runner,
        (f"Work/runs/{state['run_id']}/reviews/chief-revision-input-r{revision_number}.json"),
        revision_input,
    )
    revision_chapters = tuple(
        chapter_id
        for chapter_id in CHAPTER_SECTION_IDS
        if chapter_id in {section_id.split(".", 1)[0] for section_id in target_sections}
    )
    revision_context = runner._chief_template_skill_context(state, revision_chapters)
    revision_envelope = TaskEnvelope.model_validate(
        chief_envelope.model_copy(
            update={
                "task_id": f"chief-edit-r{revision_number}",
                "objective": "按 final review findings 仅修订指定的实际总编小节。",
                "input_refs": [revision_input_ref],
                "allowed_outputs": ["chief_revision_submission"],
                "allowed_tools": [
                    "write_result_part",
                    "list_result_parts",
                    "submit_result",
                ],
                "revision": revision_number,
                "prior_result_ref": current_ref,
                "artifact_delivery_modes": {
                    revision_input_ref: "inline",
                    current_ref: "hash_retained",
                },
                "target_submodule_ids": [],
                "input_contract_kind": "chief_revision_input",
                "input_contract_ref": revision_input_ref,
                "constraints": [
                    "只为 target_section_ids 调用 write_result_part 逐项保存并提交 chief_revision_submission 小补丁；先用 list_result_parts 确认状态，ready 项不得重写",
                    "不得提交全文、第二章、表格、图片或其他元数据；运行时确定性继承",
                    "revision_responses 必须逐项且仅覆盖 assigned finding ids",
                    "不得让工作流替你补写响应、章节或引用",
                    (
                        "章节 ID 与 part_id 的固定映射为："
                        + "；".join(
                            f"{section_id} -> {CHIEF_SECTION_RESULT_PART_IDS[section_id]}"
                            for section_id in sorted(target_sections)
                        )
                    ),
                    *runner._user_supplement_constraints(
                        state,
                        stage="chief_edit",
                        target_ids=target_sections,
                    ),
                ],
                "context_summary_refs": [],
                "inline_context": revision_context,
            },
        ).model_dump(mode="python")
    )
    revised = await runner._agent(
        "chief-editor",
        revision_envelope,
        revision_envelope.input_refs,
        workflow_id,
        session_key=chief_session_key,
    )
    if not isinstance(revised, ChiefRevisionSubmission):
        raise ReviewLifecycleError("chief editor returned the wrong revision type")
    if revised.base_subject_ref != current_ref or revised.revision != revision_number:
        raise ReviewLifecycleError("chief editor patch targets the wrong subject revision")
    revised_report = _apply_chief_patch(
        current,
        revised,
        target_section_ids=target_sections,
        required_finding_ids=set(pending),
    )
    diff = runner._final_revision_diff(current, revised_report)
    unexpected_sections = sorted(set(diff["changed_section_ids"]) - target_sections)
    allowed_contract_fields = {"revision_responses"}
    unexpected_contract = sorted(set(diff["changed_contract_fields"]) - allowed_contract_fields)
    if unexpected_sections or unexpected_contract:
        raise ReviewLifecycleError(
            "chief revision changed content outside finding scope: "
            f"sections={unexpected_sections}; contract_fields={unexpected_contract}"
        )
    if aggregate_mode:
        validate_aggregate_retention(revised_report, approved_module_text)
        if claims:
            validate_editor_protection(revised_report, claims)
    else:
        validate_editor_protection(revised_report, claims)
        validate_editor_quality(revised_report, state["module_submissions"])
    revised_ref = _write_model(
        runner,
        (f"Work/runs/{state['run_id']}/edited-revisions/chief-author-r{revision_number}.json"),
        revised_report,
    )
    return revised_report, revised_ref


async def run_final_review(
    runner: "ReportWorkflowRunner",
    state: dict,
    workflow_id: str,
    *,
    chief_envelope: TaskEnvelope,
    chief_session_key: str,
    approved_module_text: dict[str, str],
    claims: list,
    aggregate_mode: bool = False,
) -> None:
    reviewer_session_key = "chief-editor-auditor"
    current: EditedReportSubmission = state["edited_report"]
    pending: dict[str, FinalReviewFinding] = {}
    responses: list[RevisionResponse] = []
    finding_refs: list[str] = []
    verdict_refs: list[str] = []
    resolved_ids: set[str] = set()
    residual_risks: list[str] = []
    phase = "initial"
    review_round = 0
    chief_revision_number = 0
    restart_round = state.get("final_review_restart_round")
    if restart_round is not None:
        review_round = int(restart_round)
        chief_revision_number = int(restart_round)
        progress_ref = f"Work/runs/{state['run_id']}/reviews/final-progress-r{review_round}.json"
    else:
        progress_ref = f"Work/runs/{state['run_id']}/reviews/final-progress.json"

    def save_progress(next_action: str) -> None:
        _write_model(
            runner,
            progress_ref,
            FinalReviewProgress(
                run_id=state["run_id"],
                next_action=next_action,
                current=current,
                pending=list(pending.values()),
                responses=responses,
                finding_refs=finding_refs,
                verdict_refs=verdict_refs,
                resolved_ids=sorted(resolved_ids),
                residual_risks=residual_risks,
                phase=phase,
                review_round=review_round,
                chief_revision_number=chief_revision_number,
            ),
        )

    progress = (
        _load_progress(runner, progress_ref, FinalReviewProgress) if state.get("resume") else None
    )
    if progress is not None and progress.next_action != "completed":
        if progress.run_id != state["run_id"]:
            raise ReviewLifecycleError("final review progress identity mismatch")
        current = progress.current
        pending = {finding.id: finding for finding in progress.pending}
        responses = progress.responses
        finding_refs = progress.finding_refs
        verdict_refs = progress.verdict_refs
        resolved_ids = set(progress.resolved_ids)
        residual_risks = progress.residual_risks
        phase = progress.phase
        review_round = progress.review_round
        chief_revision_number = progress.chief_revision_number
        if progress.next_action == "revise":
            next_revision = chief_revision_number + 1
            candidate_ref = (
                f"Work/runs/{state['run_id']}/edited-revisions/chief-author-r{next_revision}.json"
            )
            candidate_path = runner.service.workspace / candidate_ref
            revised = None
            if candidate_path.is_file():
                try:
                    revised = EditedReportSubmission.model_validate_json(
                        candidate_path.read_text(encoding="utf-8")
                    )
                    _validate_responses(
                        revised.revision_responses,
                        set(pending),
                        {
                            section_id
                            for finding in pending.values()
                            for section_id in finding.target_section_ids
                        },
                    )
                except (OSError, ValueError):
                    revised = None
            chief_revision_number = next_revision
            if revised is None:
                revised, candidate_ref = await _request_chief_revision(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    current=current,
                    current_ref=(
                        f"Work/runs/{state['run_id']}/edited-revisions/chief-r{review_round}.json"
                    ),
                    pending=pending,
                    approved_module_text=approved_module_text,
                    claims=claims,
                    aggregate_mode=aggregate_mode,
                    chief_envelope=chief_envelope,
                    chief_session_key=chief_session_key,
                    revision_number=chief_revision_number,
                )
            current = revised
            responses = revised.revision_responses
            exceptional = [
                response for response in responses if response.action in {"disputed", "needs_input"}
            ]
            if exceptional:
                decision = await _main_exception_decision(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    scope="final",
                    subject_refs=[candidate_ref],
                    finding_refs=finding_refs,
                    verdicts=[],
                    responses=exceptional,
                    trigger="author_response",
                )
                if decision.decision == "return_to_author":
                    chief_revision_number += 1
                    current, candidate_ref = await _request_chief_revision(
                        runner,
                        state=state,
                        workflow_id=workflow_id,
                        current=current,
                        current_ref=candidate_ref,
                        pending=pending,
                        approved_module_text=approved_module_text,
                        claims=claims,
                        aggregate_mode=aggregate_mode,
                        chief_envelope=chief_envelope,
                        chief_session_key=chief_session_key,
                        revision_number=chief_revision_number,
                    )
                    responses = current.revision_responses
            state["edited_report"] = current
            phase = "recheck"
            review_round += 1
            save_progress("review")

    while True:
        required_audit_sections = tuple(
            section_id for section_id in FINAL_SUMMARY_CONCLUSION_AUDIT_SECTION_IDS
        )
        audited_chapters = "第一、三章"
        subject_ref = _write_model(
            runner,
            (f"Work/runs/{state['run_id']}/edited-revisions/chief-r{review_round}.json"),
            current,
        )
        _, canonical = runner._delivery_projection(state, current, claims)
        runner._validate_final_report_structure(
            state, canonical, f"chief-candidate-r{review_round}"
        )
        integrity_ref = (
            f"Work/runs/{state['run_id']}/reviews/"
            f"report-integrity-chief-candidate-r{review_round}.json"
        )
        validation_report = ValidationReport.model_validate_json(
            (runner.service.workspace / integrity_ref).read_text(encoding="utf-8")
        )
        _require_validation_binding(
            runner,
            validation_report,
            subject_ref=(
                f"Work/runs/{state['run_id']}/validation/report-chief-candidate-r{review_round}.md"
            ),
            subject_revision=review_round,
        )
        metadata = final_audit_metadata_view(current)
        audit_bodies = _final_audit_section_bodies(current)
        recheck_targets = (
            {
                section_id
                for finding in pending.values()
                for section_id in finding.target_section_ids
            }
            if phase == "recheck"
            else set()
        )
        review_input = _final_review_input(
            runner=runner,
            state=state,
            aggregate_mode=aggregate_mode,
            phase=phase,
            subject_ref=subject_ref,
            subject_revision=review_round,
            subject_metadata=metadata if phase == "initial" else None,
            subject_metadata_sha256=(_model_sha256(metadata) if phase == "recheck" else None),
            canonical_markdown=(
                _final_audit_markdown(strip_runtime_claim_markers(canonical))
                if phase == "initial"
                else None
            ),
            changed_section_bodies=(
                {section_id: audit_bodies[section_id] for section_id in sorted(recheck_targets)}
                if phase == "recheck"
                else {}
            ),
            unchanged_section_sha256=(
                {
                    section_id: _text_sha256(body)
                    for section_id, body in audit_bodies.items()
                    if section_id not in recheck_targets
                }
                if phase == "recheck"
                else {}
            ),
            required_section_ids=list(required_audit_sections),
            required_findings=list(pending.values()) if phase == "recheck" else [],
            revision_responses=responses if phase == "recheck" else [],
            validation_report_ref=integrity_ref,
            validation_report=validation_report,
        )
        input_ref = _write_model(
            runner,
            f"Work/runs/{state['run_id']}/reviews/final-review-input-r{review_round}.json",
            review_input,
        )
        output_kind = (
            "final_review_finding_submission"
            if phase == "initial"
            else "final_review_verdict_submission"
        )
        envelope = TaskEnvelope(
            task_id=f"final-review-r{review_round}",
            run_id=state["run_id"],
            agent_id="chief-editor-auditor",
            objective=(
                f"独立审查当前成稿{audited_chapters}的保真、综合、可追溯、可执行和交付质量。"
                if phase == "initial"
                else (
                    "由原 final reviewer 逐项判断 required_findings 是否关闭并检查"
                    f"{audited_chapters}回归。"
                )
            ),
            input_refs=[input_ref],
            constraints=[
                f"只审查{audited_chapters}七个摘要/结论小节及最终交付质量，不重做模块或 Cross 专业审查",
                "第二章与第四章由其他结构/保真 gate 负责，不属于本阶段内容、覆盖范围或 finding target",
                "residual_risks 只记录无需内容修订的透明限制",
                "当前实际章节正文、必要表格或图片缺失属于 actionable finding，禁止塞入 residual_risks",
                (
                    f"首轮 checked_section_ids 必须精确覆盖{audited_chapters}的实际审计小节"
                    if phase == "initial"
                    else "verdicts 必须逐项且仅覆盖 required_findings；new_findings 只允许真实回归"
                ),
                *runner._user_supplement_constraints(
                    state,
                    stage="final_review",
                    target_ids={
                        *required_audit_sections,
                        *(claim.id for claim in claims),
                    },
                ),
            ],
            allowed_outputs=[output_kind],
            revision=review_round,
            prior_result_ref=finding_refs[-1] if finding_refs else None,
            artifact_delivery_modes={
                input_ref: "inline",
                **({finding_refs[-1]: "hash_retained"} if finding_refs else {}),
            },
            input_contract_kind=(
                "aggregate_final_review_input" if aggregate_mode else "final_review_input"
            ),
            input_contract_ref=input_ref,
            inline_context=runner._template_skill_context(state, "final-auditor"),
            allowed_tools=["submit_result"],
        )
        result = await runner._agent(
            "chief-editor-auditor",
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key=reviewer_session_key,
        )
        if phase == "initial":
            if not isinstance(result, FinalReviewFindingSubmission):
                raise ReviewLifecycleError("final reviewer returned the wrong initial type")
            if set(result.checked_section_ids) != set(required_audit_sections):
                raise ReviewLifecycleError(
                    "initial final review did not cover every chief-owned audit section"
                )
            _validate_final_findings(result.findings, set(required_audit_sections))
            finding_ref = _write_immutable_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/final-findings-r{review_round}.json",
                result,
            )
            finding_refs.append(finding_ref)
            pending = {finding.id: finding for finding in result.findings}
            residual_risks = result.residual_risks
        else:
            if not isinstance(result, FinalReviewVerdictSubmission):
                raise ReviewLifecycleError("final reviewer returned the wrong recheck type")
            if set(result.checked_section_ids) != set(required_audit_sections):
                raise ReviewLifecycleError(
                    "final recheck did not cover every active chief-owned audit section"
                )
            _validate_verdicts(result.verdicts, set(pending))
            _validate_final_findings(result.new_findings, set(required_audit_sections))
            verdict_ref = _write_immutable_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/final-verdicts-r{review_round}.json",
                result,
            )
            verdict_refs.append(verdict_ref)
            escalated = [verdict for verdict in result.verdicts if verdict.verdict == "escalate"]
            main_accepts: set[str] = set()
            if escalated:
                decision = await _main_exception_decision(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    scope="final",
                    subject_refs=[subject_ref],
                    finding_refs=finding_refs,
                    verdicts=escalated,
                    responses=responses,
                )
                if decision.decision == "accept_dispute":
                    main_accepts = set(decision.finding_ids)
            next_pending = {
                verdict.finding_id: pending[verdict.finding_id]
                for verdict in result.verdicts
                if verdict.verdict == "open"
                or (verdict.verdict == "escalate" and verdict.finding_id not in main_accepts)
            }
            resolved_ids.update(
                verdict.finding_id
                for verdict in result.verdicts
                if verdict.verdict == "resolved" or verdict.finding_id in main_accepts
            )
            for finding in result.new_findings:
                if finding.id in pending or finding.id in resolved_ids:
                    raise ReviewLifecycleError(
                        f"new final finding reuses an existing id: {finding.id}"
                    )
                next_pending[finding.id] = finding
            if result.new_findings:
                new_ref = _write_immutable_model(
                    runner,
                    (
                        f"Work/runs/{state['run_id']}/reviews/"
                        f"final-regression-findings-r{review_round}.json"
                    ),
                    FinalReviewFindingSubmission(
                        checked_section_ids=result.checked_section_ids,
                        findings=result.new_findings,
                        residual_risks=result.residual_risks,
                    ),
                )
                finding_refs.append(new_ref)
            pending = next_pending
            residual_risks = result.residual_risks

        if not pending:
            completion = ReviewCompletionRecord(
                lifecycle="final",
                run_id=state["run_id"],
                reviewer_agent_id="chief-editor-auditor",
                reviewer_session_key=reviewer_session_key,
                subject_refs=[subject_ref],
                finding_refs=finding_refs,
                verdict_refs=verdict_refs,
                resolved_finding_ids=sorted(resolved_ids),
            )
            completion_ref = _write_immutable_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/final-completion.json",
                completion,
            )
            canonical_ref = validation_report.subject_ref
            snapshot_ref = _write_immutable_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/final-audit-snapshot.json",
                FinalAuditSnapshot(
                    run_id=state["run_id"],
                    subject_ref=subject_ref,
                    subject_revision=review_round,
                    canonical_markdown_ref=canonical_ref,
                    validation_report_ref=integrity_ref,
                    completion_ref=completion_ref,
                ),
            )
            state["edited_report"] = current
            state["final_review_completion_ref"] = completion_ref
            state["final_audit_snapshot_ref"] = snapshot_ref
            state["final_residual_risks"] = residual_risks
            save_progress("completed")
            return

        save_progress("revise")
        while True:
            chief_revision_number += 1
            revised, revised_ref = await _request_chief_revision(
                runner,
                state=state,
                workflow_id=workflow_id,
                current=current,
                current_ref=subject_ref,
                pending=pending,
                approved_module_text=approved_module_text,
                claims=claims,
                aggregate_mode=aggregate_mode,
                chief_envelope=chief_envelope,
                chief_session_key=chief_session_key,
                revision_number=chief_revision_number,
            )
            current = revised
            subject_ref = revised_ref
            responses = revised.revision_responses
            exceptional = [
                response for response in responses if response.action in {"disputed", "needs_input"}
            ]
            if not exceptional:
                break
            decision = await _main_exception_decision(
                runner,
                state=state,
                workflow_id=workflow_id,
                scope="final",
                subject_refs=[subject_ref],
                finding_refs=finding_refs,
                verdicts=[],
                responses=exceptional,
                trigger="author_response",
            )
            if decision.decision != "return_to_author":
                break
        state["edited_report"] = current
        phase = "recheck"
        review_round += 1
        save_progress("review")
