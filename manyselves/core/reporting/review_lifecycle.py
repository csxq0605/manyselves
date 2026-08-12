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
from typing import TYPE_CHECKING, Iterable, Literal
from uuid import uuid4

from pydantic import Field

from .agentic_models import (
    CROSS_REVIEW_DIMENSIONS,
    ChiefRevisionSubmission,
    CrossReviewFinding,
    CrossReviewFindingSubmission,
    CrossReviewVerdictSubmission,
    CrossSynthesisInput,
    EditedReportSubmission,
    FinalReviewFinding,
    FinalReviewFindingSubmission,
    FinalReviewVerdictSubmission,
    ClaimRecord,
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
from .input_contracts import (
    AggregateFinalReviewInput,
    ChiefRevisionInput,
    CrossReviewInput,
    FinalAuditSnapshot,
    FinalReviewInput,
    CrossDecisionPackView,
    ModuleReviewInput,
    ModuleRevisionDiff,
    ModuleRevisionInput,
    RequestedModuleChange,
    ReviewClaimStatement,
    ReviewCompletionRecord,
    ReviewEvidenceExcerpt,
    ValidationFailure,
    ValidationReport,
    WorkflowExceptionInput,
    final_audit_metadata_view,
    module_content_view,
    strip_runtime_claim_markers,
)
from .module_collaboration import (
    InterfaceCrossClosure,
    InterfaceResolutionClosureBatch,
    InterfaceResolutionRegistry,
    apply_interface_cross_closure,
    interface_owner_finding_id,
)
from .models import (
    CHIEF_SECTION_RESULT_PART_IDS,
    FINAL_SUMMARY_CONCLUSION_AUDIT_SECTION_IDS,
)
from .parallel_runtime import (
    ArtifactRef,
    CrossOwnerBarrier,
    CrossOwnerCompletion,
    LaneExceptionCandidate,
    WorkflowReducer,
)
from .revision_diff import build_revision_diff
from .scheduling import (
    AdaptiveTaskScheduler,
    SchedulingCandidate,
    TaskTimingHistory,
)
from .review_preflight import evaluate_module_review_preflight
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
    """Require one v2 validation report to bind the exact persisted subject bytes."""

    subject_path = runner.service.workspace / subject_ref
    try:
        content_sha256 = hashlib.sha256(subject_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReviewLifecycleError(
            f"validated subject is unreadable: {subject_ref}"
        ) from exc
    if (
        report.validation_protocol_version < 2
        or report.subject_ref != subject_ref
        or report.subject_revision != subject_revision
        or report.content_sha256 != content_sha256
    ):
        raise ReviewLifecycleError(
            "validation report does not bind the exact final subject; "
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
    interface_registry_ref: str | None = None
    interface_registry_sha256: str | None = None
    interface_closure_refs: list[str] = Field(default_factory=list)
    interface_closure_sha256: dict[str, str] = Field(default_factory=dict)
    interface_residual_risks: dict[str, str] = Field(default_factory=dict)


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


def _artifact_sha256(
    runner: "ReportWorkflowRunner",
    refs: Iterable[str],
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for ref in refs:
        path = (runner.service.workspace / ref).resolve()
        if not path.is_relative_to(runner.service.workspace) or not path.is_file():
            raise ReviewLifecycleError(f"cannot complete review with unreadable artifact: {ref}")
        hashes[ref] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


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
        statement_ref=(
            "statement-" + hashlib.sha256(claim.id.encode("utf-8")).hexdigest()[:12]
        ),
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
        {
            statement.statement_ref
            for statement in [*current_statements, *prior_statements]
        }
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
                strip_runtime_claim_markers(
                    current.submodule_narratives[submodule_id]
                ).rstrip()
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


def _module_reviewer_session_key(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    module_id: str,
    lifecycle_id: str,
    progress: ModuleReviewProgress | None,
    regression_context: ModuleLocalRegressionContext | None,
) -> str:
    """Reuse the module's original reviewer while preserving legacy resumes."""

    stable = f"module-auditor-{module_id}"
    def require_module_identity(value: str) -> str:
        if value != stable and not value.startswith(f"{stable}-"):
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
    registry_path = (
        runner.service.workspace
        / f"Work/runs/{state['run_id']}/agent-identities.json"
    )
    if not registry_path.is_file():
        return stable
    try:
        identities = json.loads(
            registry_path.read_text(encoding="utf-8")
        ).get("identities", {})
    except (OSError, ValueError, AttributeError):
        return stable
    if stable in identities:
        return stable
    current_legacy = f"{stable}-{lifecycle_id}"
    if progress is not None and current_legacy in identities:
        return current_legacy
    initial_legacy = f"{stable}-initial"
    if initial_legacy in identities:
        return initial_legacy
    return stable


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
    payload.pop("artifact_sha256", None)
    payload.pop("pack_sha256", None)
    payload["artifact_refs"] = sorted(pack.artifact_sha256)
    try:
        return CrossDecisionPackView.model_validate(payload)
    except ValueError as exc:
        raise ReviewLifecycleError(
            "Final review CrossDecisionPack view is invalid"
        ) from exc


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
        cross_decision_pack_sha256=state.get("cross_decision_pack_sha256", ""),
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
    """Fan one module correction out to its original fixed leaf identities."""

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
        _require_validation_binding(
            runner,
            validation_report,
            subject_ref=(
                f"Work/runs/{state['run_id']}/modules/"
                f"{subject.module_id}-r{subject.revision}.json"
            ),
            subject_revision=subject.revision,
        )
        if not targets:
            raise ReviewLifecycleError(
                "failed machine validation requires explicit module-local correction targets"
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

    def finding_ids_for(target_id: str) -> set[str]:
        return {
            *(finding.id for finding in module_findings if finding.target_submodule_id == target_id),
            *(
                finding.id
                for finding in cross_findings
                if target_id in finding.target_submodule_ids
            ),
            *(
                change.id
                for change in requested_changes
                if target_id in change.target_submodule_ids
            ),
        }

    leaf_inputs: dict[str, tuple[ModuleRevisionInput, str]] = {}
    patch_refs: dict[str, str] = {}
    patch_contexts: dict[str, str] = {}
    for target_id in sorted(targets):
        leaf_input = ModuleRevisionInput(
            run_id=state["run_id"],
            module_id=subject.module_id,
            subject_ref=revision_input.subject_ref,
            subject=module_content_view(subject, {target_id}),
            target_submodule_ids=[target_id],
            module_findings=[
                finding
                for finding in module_findings
                if finding.target_submodule_id == target_id
            ],
            cross_findings=[
                finding
                for finding in cross_findings
                if target_id in finding.target_submodule_ids
            ],
            requested_changes=[
                change
                for change in requested_changes
                if target_id in change.target_submodule_ids
            ],
            validation_report_ref=(
                validation_ref if target_id in validation_target_submodule_ids else None
            ),
            validation_report=(
                validation_report
                if target_id in validation_target_submodule_ids
                else None
            ),
        )
        leaf_ref = _write_model(
            runner,
            (
                f"Work/runs/{state['run_id']}/reviews/module-revisions/"
                f"{subject.module_id}/r{revision}/input-{target_id}.json"
            ),
            leaf_input,
        )
        leaf_inputs[target_id] = (leaf_input, leaf_ref)
        patch_refs[target_id] = (
            f"Work/runs/{state['run_id']}/reviews/module-revisions/"
            f"{subject.module_id}/r{revision}/patch-{target_id}.json"
        )
        if hasattr(runner, "_submodule_task_context_sha256"):
            patch_contexts[target_id] = runner._submodule_task_context_sha256(
                state,
                task_kind=f"submodule_revision_r{revision}",
                submodule_id=target_id,
                input_refs=[leaf_ref, revision_input.subject_ref],
            )

    async def execute_leaf(target_id: str) -> ModuleRevisionSubmission:
        leaf_input, leaf_ref = leaf_inputs[target_id]
        leaf_required_ids = finding_ids_for(target_id)
        envelope = TaskEnvelope(
            task_id=f"submodule-revision-r{revision}-{target_id}",
            run_id=state["run_id"],
            agent_id=specialist_id,
            objective=(
                f"只修复固定叶子 {target_id} 的确定性机器谓词失败。"
                if leaf_input.validation_report is not None and not leaf_required_ids
                else f"由原叶子身份对固定叶子 {target_id} 执行显式、定向补丁修订。"
            ),
            input_refs=[leaf_ref],
            constraints=[
                f"唯一写作和身份范围是固定叶子 {target_id}",
                "只提交小型 module_revision_submission commit；不得在其中重复正文",
                "只用 write_result_part 保存本叶子的完整替换正文和 evidence_ids；先用 list_result_parts 确认状态，ready 项不得重写",
                "同一 finding 可能分配给多个叶子；只实现并申报当前叶子的 changed_target_ids",
                "最终提交只使用 schema 声明的简短字段；运行时从保存的小节自动生成补丁",
                (
                    "本次只有机器 preflight 触发；revision_responses 必须为空，"
                    "不得伪造 reviewer finding 或 verdict"
                    if not leaf_required_ids
                    else "revision_responses 必须逐项且仅覆盖本叶子分配的 finding ids"
                ),
                "disputed 或 needs_input 不得伪造 changed_target_ids",
                *(
                    [f"上一版显式机器检查未通过；只修复 {validation_ref} 中列出的谓词失败"]
                    if leaf_input.validation_report is not None
                    else []
                ),
                *runner._user_supplement_constraints(
                    state,
                    stage="module_authoring",
                    target_ids={
                        subject.module_id,
                        target_id,
                        *(
                            claim.id
                            for claim in subject.claims
                            if claim.submodule_id == target_id
                        ),
                    },
                ),
            ],
            allowed_outputs=["module_revision_submission"],
            revision=revision,
            prior_result_ref=revision_input.subject_ref,
            artifact_delivery_modes={
                leaf_ref: "inline",
                revision_input.subject_ref: "hash_retained",
            },
            target_submodule_ids=[target_id],
            input_contract_kind="module_revision_input",
            input_contract_ref=leaf_ref,
            inline_context=runner._role_skill_context(state, "module-author"),
        )
        patch = await runner._agent(
            specialist_id,
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key=f"submodule-{target_id}",
        )
        if not isinstance(patch, ModuleRevisionSubmission):
            raise ReviewLifecycleError(
                f"module specialist returned the wrong revision type for {target_id}"
            )
        return patch

    patches: dict[str, ModuleRevisionSubmission] = {}
    pending_targets: list[str] = []
    can_recover = all(
        hasattr(runner, name)
        for name in (
            "_load_collaboration_submission",
            "_persist_submodule_task_completion",
            "_run_scheduled_submodule_stage",
        )
    )
    for target_id in sorted(targets):
        patch = None
        if can_recover:
            patch = runner._load_collaboration_submission(
                run_id=state["run_id"],
                artifact_ref=patch_refs[target_id],
                task_id=f"submodule-revision-r{revision}-{target_id}",
                module_id=subject.module_id,
                submodule_id=target_id,
                expected_type=ModuleRevisionSubmission,
                wave=f"revision-r{revision}",
                expected_context_sha256=patch_contexts[target_id],
                require_payload_submodule_identity=False,
            )
        if patch is None:
            pending_targets.append(target_id)
        else:
            patches[target_id] = patch
    if pending_targets and can_recover:
        task_kind = f"submodule_revision_r{revision}"
        patches.update(
            await runner._run_scheduled_submodule_stage(
                tuple(pending_targets),
                run_id=state["run_id"],
                workflow_id=workflow_id,
                task_kind=task_kind,
                concurrency=int(
                    getattr(state.get("request"), "submodule_task_concurrency", 8)
                ),
                # A finding round is a business cohort: every affected leaf
                # is ready once the reviewer emits its typed finding.  The
                # helper still groups multiple findings for one leaf into a
                # single task, while all distinct leaves run concurrently;
                # Provider backpressure is enforced below this scheduler.
                all_ready=True,
                execute=execute_leaf,
                persist=lambda target_id, patch: (
                    runner._persist_submodule_task_completion(
                        state=state,
                        task_kind=task_kind,
                        wave=f"revision-r{revision}",
                        submodule_id=target_id,
                        artifact_ref=patch_refs[target_id],
                        context_sha256=patch_contexts[target_id],
                        payload=patch,
                    )
                ),
            )
        )
    elif pending_targets:
        for target_id in pending_targets:
            patches[target_id] = await execute_leaf(target_id)

    narratives = dict(subject.submodule_narratives)
    claims_by_id = {claim.id: claim for claim in subject.claims}
    baseline_questions = list(subject.unresolved_questions)
    removed_questions: set[str] = set()
    added_questions: list[str] = []
    responses_by_id: dict[str, list[tuple[str, RevisionResponse]]] = defaultdict(list)
    for target_id in sorted(targets):
        patch = patches[target_id]
        interim = _apply_module_patch(
            subject,
            patch,
            target_submodule_ids={target_id},
            required_finding_ids=finding_ids_for(target_id),
        )
        narratives.update(patch.submodule_narratives)
        for claim_id in patch.claim_ids_remove:
            claims_by_id.pop(claim_id, None)
        for claim in patch.claims_upsert:
            claims_by_id[claim.id] = claim
        removed_questions.update(set(baseline_questions) - set(interim.unresolved_questions))
        for question in interim.unresolved_questions:
            if question not in baseline_questions and question not in added_questions:
                added_questions.append(question)
        for response in patch.revision_responses:
            responses_by_id[response.finding_id].append((target_id, response))

    merged_responses: list[RevisionResponse] = []
    for finding_id in sorted(required_ids):
        leaf_responses = responses_by_id.get(finding_id, [])
        expected_targets = {
            target_id for target_id in targets if finding_id in finding_ids_for(target_id)
        }
        if {target_id for target_id, _ in leaf_responses} != expected_targets:
            raise ReviewLifecycleError(
                f"leaf revisions did not cover every assigned target for {finding_id}"
            )
        actions = {response.action for _, response in leaf_responses}
        if actions == {"implemented"}:
            action = "implemented"
            changed_target_ids = sorted(
                {
                    target_id
                    for _, response in leaf_responses
                    for target_id in response.changed_target_ids
                }
            )
        elif "needs_input" in actions:
            action = "needs_input"
            changed_target_ids = []
        else:
            action = "disputed"
            changed_target_ids = []
        merged_responses.append(
            RevisionResponse(
                finding_id=finding_id,
                action=action,
                summary="；".join(
                    f"[{target_id}] {response.summary}"
                    for target_id, response in leaf_responses
                ),
                changed_target_ids=changed_target_ids,
            )
        )

    final_questions = [
        question for question in baseline_questions if question not in removed_questions
    ]
    final_questions.extend(
        question for question in added_questions if question not in final_questions
    )
    revised = ModuleSubmission(
        module_id=subject.module_id,
        submodule_narratives=narratives,
        claims=list(claims_by_id.values()),
        source_ids=sorted(
            {
                source_id
                for claim in claims_by_id.values()
                for source_id in claim.source_ids
            }
            | set(subject.source_ids)
            | {
                source_id
                for patch in patches.values()
                for source_id in patch.source_ids
            }
        ),
        unresolved_questions=final_questions,
        revision=revision,
        revision_responses=merged_responses,
    )
    subject_ref = _write_model(
        runner,
        (f"Work/runs/{state['run_id']}/modules/{subject.module_id}-r{revised.revision}.json"),
        revised,
    )
    diff = build_revision_diff(subject, revised)
    runner.service.store.write_json(
        (
            f"Work/runs/{state['run_id']}/reviews/module-diff-"
            f"{subject.module_id}-r{revised.revision}.json"
        ),
        diff,
    )
    runner.service.store.write_json(
        (
            f"Work/runs/{state['run_id']}/reviews/module-revisions/"
            f"{subject.module_id}/r{revision}/reducer-barrier.json"
        ),
        {
            "kind": "module_leaf_revision_reducer_barrier",
            "version": 1,
            "run_id": state["run_id"],
            "module_id": subject.module_id,
            "base_revision": subject.revision,
            "revision": revision,
            "target_submodule_ids": sorted(targets),
            "patch_refs": patch_refs,
            "patch_sha256": {
                target_id: hashlib.sha256(
                    (runner.service.workspace / patch_ref).read_bytes()
                ).hexdigest()
                for target_id, patch_ref in patch_refs.items()
                if (runner.service.workspace / patch_ref).is_file()
            },
            "subject_ref": subject_ref,
            "subject_sha256": hashlib.sha256(
                (runner.service.workspace / subject_ref).read_bytes()
            ).hexdigest(),
        },
    )
    return revised, subject_ref


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
    refs = [subject_ref, *finding_refs, *verdict_refs]
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
        artifact_sha256=_artifact_sha256(runner, refs),
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
        _write_model(
            runner,
            progress_ref,
            ModuleReviewProgress(
                run_id=state["run_id"],
                module_id=module_id,
                next_action=next_action,
                current=current,
                pending=list(pending.values()),
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
            completion_ref = (
                f"{review_root}/completion-r{progress.current.revision}.json"
            )
        completion_path = runner.service.workspace / completion_ref
        if completion_path.is_file():
            try:
                completion = ReviewCompletionRecord.model_validate_json(
                    completion_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise ReviewLifecycleError(
                    "completed module review marker is unreadable: "
                    f"{completion_ref}"
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
            state.setdefault("module_review_completion_refs", {})[module_id] = (
                str(completion_ref)
            )
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
        machine_attempts = 0
        machine_failure_fingerprints: dict[tuple, int] = {}
        while True:
            subject_ref = (
                f"Work/runs/{state['run_id']}/modules/"
                f"{module_id}-r{current.revision}.json"
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
            machine_attempts += 1
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
            repeated = machine_failure_fingerprints.get(fingerprint, 0) + 1
            machine_failure_fingerprints[fingerprint] = repeated
            if repeated >= 2 or machine_attempts >= 3:
                raise ReviewLifecycleError(
                    "module preflight failed repeatedly before semantic review; "
                    "no reviewer finding or verdict was created. "
                    f"module={module_id}; attempts={machine_attempts}; "
                    f"validation_ref={signal_ref}"
                )
            current, _ = await request_module_revision(
                runner,
                state=state,
                workflow_id=workflow_id,
                subject=current,
                module_findings=(
                    list(pending.values()) if phase == "recheck" else []
                ),
                cross_findings=(
                    regression_context.trigger_cross_findings
                    if phase == "local_regression"
                    and regression_context is not None
                    else []
                ),
                validation_ref=signal_ref,
                validation_target_submodule_ids=set(
                    preflight.target_submodule_ids
                ),
            )
            if phase == "recheck":
                responses = current.revision_responses
            save_progress("review")

        review_subject = module_content_view(current, scope)
        review_claim_statements = [
            _review_claim_statement(claim)
            for claim in current.claims
            if claim.submodule_id in scope
        ]
        prior_claim_statements: list[ReviewClaimStatement] = []
        unchanged_submodule_sha256: dict[str, str] = {}
        unchanged_statement_sha256: dict[str, str] = {}
        baseline_subject_ref: str | None = None
        revision_diff_ref: str | None = None
        revision_diff: ModuleRevisionDiff | None = None
        relevant_evidence_ids: set[str] | None = None
        if phase == "recheck":
            baseline_subject_ref = last_reviewed_subject_ref
            if baseline_subject_ref is None:
                legacy_candidate = (
                    f"Work/runs/{state['run_id']}/modules/"
                    f"{module_id}-r{current.revision - 1}.json"
                )
                if current.revision > 0 and (
                    runner.service.workspace / legacy_candidate
                ).is_file():
                    baseline_subject_ref = legacy_candidate
                else:
                    raise ReviewLifecycleError(
                        "module recheck lacks the last subject seen by its reviewer"
                    )
            try:
                baseline = ModuleSubmission.model_validate_json(
                    (
                        runner.service.workspace / baseline_subject_ref
                    ).read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise ReviewLifecycleError(
                    "module recheck last-reviewed baseline is unreadable: "
                    f"{baseline_subject_ref}"
                ) from exc
            delta = _module_recheck_delta(baseline, current, scope)
            review_subject = delta["subject"]
            review_claim_statements = delta["claim_statements"]
            prior_claim_statements = delta["prior_claim_statements"]
            unchanged_submodule_sha256 = delta["unchanged_submodule_sha256"]
            unchanged_statement_sha256 = delta["unchanged_statement_sha256"]
            revision_diff = delta["revision_diff"]
            relevant_evidence_ids = set(delta["relevant_evidence_ids"])
            relevant_evidence_ids.update(
                evidence_ref
                for finding in pending.values()
                for evidence_ref in finding.evidence_refs
                if evidence_ref.startswith("E-")
            )
            revision_diff_ref = _write_model(
                runner,
                f"{review_root}/recheck-diff-r{review_round}.json",
                revision_diff,
            )
        # ReportingAgentRunner deliberately resets provider working memory at
        # every typed task boundary.  A stable reviewer identity therefore does
        # not retain the initial Knowledge slice.  Re-send the same bounded,
        # immutable taxonomy packet alongside the delta; unchanged subject
        # prose and evidence remain hash-only/delta-only.
        knowledge_ref, knowledge_context = _module_review_knowledge_packet(
            runner,
            state,
            module_id,
            scope,
        )
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
            prior_claim_statements=prior_claim_statements,
            unchanged_submodule_sha256=unchanged_submodule_sha256,
            unchanged_statement_sha256=unchanged_statement_sha256,
            knowledge_ref=knowledge_ref,
            knowledge_context=knowledge_context,
            evidence=_module_review_evidence_packet(
                runner,
                current,
                state["run_id"],
                scope,
                evidence_ids=relevant_evidence_ids,
            ),
            required_submodule_ids=sorted(scope),
            required_findings=list(pending.values()) if phase == "recheck" else [],
            revision_responses=responses if phase == "recheck" else [],
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
                else baseline_subject_ref
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
                else revision_diff_ref
            ),
            revision_diff=(
                regression_context.revision_diff
                if phase == "local_regression" and regression_context is not None
                else revision_diff
            ),
            validation_report_ref=signal_ref,
            validation_report=validation_report,
        )
        input_ref = _write_model(
            runner,
            f"{review_root}/input-r{review_round}.json",
            review_input,
        )
        output_kind = (
            "module_review_finding_submission"
            if phase in {"initial", "local_regression"}
            else "module_review_verdict_submission"
        )
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
                    if phase == "local_regression"
                    else (
                        f"只对模块 {module_id} 的 required_findings 返回逐项 verdict，"
                        "并检查修改回归。"
                    )
                )
            ),
            input_refs=[input_ref],
            constraints=[
                "coverage 记录实际检查范围，不是批准状态",
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
            artifact_delivery_modes={
                input_ref: "inline",
                **(
                    {finding_refs[-1]: "hash_retained"}
                    if finding_refs
                    else {}
                ),
            },
            target_submodule_ids=sorted(scope),
            input_contract_kind="module_review_input",
            input_contract_ref=input_ref,
            inline_context=runner._role_skill_context(state, "module-auditor"),
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


def _resolve_subject_path(subject: ModuleSubmission, path: str) -> str:
    if path.startswith("submodule_narratives."):
        key = path.removeprefix("submodule_narratives.")
        try:
            return subject.submodule_narratives[key]
        except KeyError as exc:
            raise ReviewLifecycleError(f"machine check target does not exist: {path}") from exc
    if path.startswith("claims."):
        remainder = path.removeprefix("claims.")
        claim_id, separator, field = remainder.rpartition(".")
        if not separator:
            raise ReviewLifecycleError(f"machine Claim path requires a field: {path}")
        claim = next((value for value in subject.claims if value.id == claim_id), None)
        if claim is None or field not in claim.model_fields:
            raise ReviewLifecycleError(f"machine check target does not exist: {path}")
        return str(getattr(claim, field))
    raise ReviewLifecycleError(f"unsupported explicit machine-check path: {path}")


def _run_cross_machine_checks(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    subject: ModuleSubmission,
    subject_ref: str,
    findings: list[CrossReviewFinding],
) -> str:
    subject_path = runner.service.workspace / subject_ref
    try:
        content_sha256 = hashlib.sha256(subject_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReviewLifecycleError(
            f"Cross machine-check subject is unreadable: {subject_ref}"
        ) from exc
    failures: list[ValidationFailure] = []
    check_ids: list[str] = []
    for finding in findings:
        for index, check in enumerate(finding.machine_checks, start=1):
            check_id = f"{finding.id}:machine:{index}"
            check_ids.append(check_id)
            values = [_resolve_subject_path(subject, path) for path in check.target_paths]
            if check.kind == "forbidden_terms_absent":
                for path, value in zip(check.target_paths, values, strict=True):
                    present = [term for term in check.expected_values if term in value]
                    if present:
                        failures.append(
                            ValidationFailure(
                                check_id=check_id,
                                finding_id=finding.id,
                                target_path=path,
                                message=f"forbidden terms still present: {present}",
                            )
                        )
            elif check.kind == "required_terms_present":
                combined = "\n".join(values)
                missing = [term for term in check.expected_values if term not in combined]
                if missing:
                    failures.append(
                        ValidationFailure(
                            check_id=check_id,
                            finding_id=finding.id,
                            target_path=", ".join(check.target_paths),
                            message=f"required terms missing: {missing}",
                        )
                    )
            elif check.kind == "field_equals":
                if len(check.target_paths) != len(check.expected_values):
                    raise ReviewLifecycleError(
                        f"{check_id} field_equals requires one expected value per path"
                    )
                for path, value, expected in zip(
                    check.target_paths,
                    values,
                    check.expected_values,
                    strict=True,
                ):
                    if value != expected:
                        failures.append(
                            ValidationFailure(
                                check_id=check_id,
                                finding_id=finding.id,
                                target_path=path,
                                message="field value does not equal the declared expected value",
                            )
                        )
    report = ValidationReport(
        validation_protocol_version=2,
        run_id=state["run_id"],
        subject_ref=subject_ref,
        subject_revision=subject.revision,
        content_sha256=content_sha256,
        validator="explicit-cross-predicates/v2",
        check_ids=check_ids,
        failures=failures,
        passed=not failures,
    )
    return _write_model(
        runner,
        (
            f"Work/runs/{state['run_id']}/validations/cross-"
            f"{subject.module_id}-r{subject.revision}.json"
        ),
        report,
    )


def _validate_cross_findings(
    findings: list[CrossReviewFinding],
    modules: dict[str, ModuleSubmission],
) -> None:
    _unique_ids((finding.id for finding in findings), label="cross findings")


def _validate_cross_synthesis_portfolio(
    synthesis_inputs: list[CrossSynthesisInput],
    modules: dict[str, ModuleSubmission],
) -> None:
    """Require system synthesis rather than treating six-dimension coverage as evidence."""

    if len(synthesis_inputs) < 2:
        raise ReviewLifecycleError(
            "Cross completion requires at least two supported system relationships"
        )
    kinds = {item.cluster_type for item in synthesis_inputs}
    required_kinds = {"risk_cluster", "global_propagation"}
    if not required_kinds.issubset(kinds):
        raise ReviewLifecycleError(
            "Cross synthesis portfolio requires a risk cluster and a global propagation "
            f"chain; missing={sorted(required_kinds - kinds)}"
        )


class _CrossOwnerLaneResult(StrictModel):
    module: ModuleSubmission
    responses: list[RevisionResponse]
    local_review_ref: str
    machine_validation_ref: str
    completion_ref: str
    completion: CrossOwnerCompletion


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


def _verify_cross_owner_barrier(
    runner: "ReportWorkflowRunner",
    barrier: CrossOwnerBarrier,
    *,
    barrier_path: Path,
    expected_run_id: str,
    expected_round: int,
    expected_modules: set[str],
) -> None:
    """Validate the exact refs, hashes, revisions and owner set at the barrier."""

    run_id = barrier.run_id
    run_root = (runner.service.workspace / f"Work/runs/{run_id}").resolve()
    if (
        barrier.run_id != expected_run_id
        or not barrier_path.resolve().is_relative_to(run_root)
        or barrier.review_round != expected_round
        or set(barrier.target_modules) != expected_modules
        or set(barrier.completion_refs) != expected_modules
        or set(barrier.completion_hashes) != expected_modules
    ):
        raise ReviewLifecycleError("Cross owner barrier identity mismatch")
    if barrier.completion_revisions and set(barrier.completion_revisions) != expected_modules:
        raise ReviewLifecycleError("Cross owner barrier revision set mismatch")
    payload = {
        "run_id": barrier.run_id,
        "review_round": barrier.review_round,
        "target_modules": sorted(barrier.target_modules, key=float),
        "completion_refs": {
            module_id: barrier.completion_refs[module_id]
            for module_id in sorted(expected_modules, key=float)
        },
        "completion_hashes": {
            module_id: barrier.completion_hashes[module_id]
            for module_id in sorted(expected_modules, key=float)
        },
        **(
            {
                "completion_revisions": {
                    module_id: barrier.completion_revisions[module_id]
                    for module_id in sorted(expected_modules, key=float)
                }
            }
            if barrier.completion_revisions
            else {}
        ),
    }
    expected_barrier_sha = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if barrier.barrier_sha256 != expected_barrier_sha:
        raise ReviewLifecycleError("Cross owner barrier hash mismatch")
    revisions: dict[str, int] = {}
    for module_id in sorted(expected_modules, key=float):
        ref = barrier.completion_refs[module_id]
        completion_path = (runner.service.workspace / ref).resolve()
        if not completion_path.is_relative_to(run_root) or not completion_path.is_file():
            raise ReviewLifecycleError(
                f"Cross owner barrier completion is unreadable: {ref}"
            )
        try:
            completion = CrossOwnerCompletion.model_validate_json(
                completion_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ReviewLifecycleError(
                f"Cross owner barrier completion is invalid: {ref}"
            ) from exc
        if (
            completion.run_id != barrier.run_id
            or completion.review_round != barrier.review_round
            or completion.module_id != module_id
            or completion.completion_sha256() != barrier.completion_hashes[module_id]
        ):
            raise ReviewLifecycleError(
                f"Cross owner barrier completion identity/hash mismatch: {module_id}"
            )
        for artifact in (
            completion.subject,
            completion.local_review_completion,
            completion.machine_validation,
        ):
            if not (
                runner.service.workspace / artifact.ref
            ).resolve().is_relative_to(run_root):
                raise ReviewLifecycleError(
                    f"Cross owner barrier artifact leaves current run: {artifact.ref}"
                )
        subject_actual = _cross_owner_artifact_ref(runner, completion.subject.ref)
        review_actual = _cross_owner_artifact_ref(
            runner,
            completion.local_review_completion.ref,
        )
        machine_actual = _cross_owner_artifact_ref(
            runner,
            completion.machine_validation.ref,
        )
        for label, declared, actual in (
            ("subject", completion.subject, subject_actual),
            ("local review", completion.local_review_completion, review_actual),
            ("machine validation", completion.machine_validation, machine_actual),
        ):
            if declared.sha256 != actual.sha256 or declared.size != actual.size:
                raise ReviewLifecycleError(
                    f"Cross owner barrier {label} hash mismatch: {module_id}"
                )
        subject_match = re.search(r"-r([0-9]+)\.json$", completion.subject.ref)
        if completion.subject_revision is not None:
            revision = completion.subject_revision
        elif subject_match is not None:
            revision = int(subject_match.group(1))
        else:
            raise ReviewLifecycleError(
                f"Cross owner barrier subject has no revision: {module_id}"
            )
        revisions[module_id] = revision
    if barrier.completion_revisions and revisions != barrier.completion_revisions:
        raise ReviewLifecycleError("Cross owner barrier revisions do not bind subjects")


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
    defer_main_exceptions: bool = True,
) -> _CrossOwnerLaneResult:
    """Run one owner-local revision and original-auditor regression in private state."""

    lane_state = deepcopy(state)
    lane_state["_defer_main_exceptions"] = defer_main_exceptions
    reviewed_baseline = current
    baseline_subject_ref = (
        f"Work/runs/{state['run_id']}/modules/"
        f"{module_id}-r{reviewed_baseline.revision}.json"
    )
    prior_completion_ref = state.get("module_review_completion_refs", {}).get(
        module_id
    )
    if not prior_completion_ref:
        raise ReviewLifecycleError(
            f"Cross local regression requires prior module review: {module_id}"
        )
    try:
        prior_completion = ReviewCompletionRecord.model_validate_json(
            (runner.service.workspace / prior_completion_ref).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError) as exc:
        raise ReviewLifecycleError(
            f"Cross local regression prior completion is unreadable: {module_id}"
        ) from exc
    if (
        prior_completion.lifecycle != "module"
        or prior_completion.run_id != state["run_id"]
        or baseline_subject_ref not in prior_completion.subject_refs
    ):
        raise ReviewLifecycleError(
            f"Cross local regression prior completion does not bind {module_id}"
        )

    validation_ref: str | None = None
    machine_attempts = 0
    machine_failure_fingerprints: dict[tuple, int] = {}
    persisted_candidate: ModuleSubmission | None = None
    modules_root = runner.service.workspace / f"Work/runs/{state['run_id']}/modules"
    candidates: list[ModuleSubmission] = []
    for path in modules_root.glob(f"{module_id}-r*.json"):
        try:
            candidate = ModuleSubmission.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if candidate.revision <= current.revision:
                continue
            _validate_responses(
                candidate.revision_responses,
                {finding.id for finding in findings},
                {
                    target_id
                    for finding in findings
                    for target_id in finding.target_submodule_ids
                },
            )
            candidates.append(candidate)
        except (OSError, ValueError):
            continue
    if candidates:
        persisted_candidate = max(candidates, key=lambda item: item.revision)

    while True:
        machine_attempts += 1
        if persisted_candidate is not None:
            revised = persisted_candidate
            revised_ref = (
                f"Work/runs/{state['run_id']}/modules/"
                f"{module_id}-r{revised.revision}.json"
            )
            persisted_candidate = None
        else:
            revised, revised_ref = await request_module_revision(
                runner,
                state=lane_state,
                workflow_id=workflow_id,
                subject=current,
                cross_findings=findings,
                validation_ref=validation_ref,
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
                validation_ref = None
                continue
        validation_ref = _run_cross_machine_checks(
            runner,
            state=lane_state,
            subject=revised,
            subject_ref=revised_ref,
            findings=findings,
        )
        report = ValidationReport.model_validate_json(
            (runner.service.workspace / validation_ref).read_text(encoding="utf-8")
        )
        if report.passed:
            break
        fingerprint = tuple(
            sorted(
                (
                    failure.check_id,
                    failure.finding_id or "",
                    failure.target_path,
                    failure.message,
                )
                for failure in report.failures
            )
        )
        repeated = machine_failure_fingerprints.get(fingerprint, 0) + 1
        machine_failure_fingerprints[fingerprint] = repeated
        if repeated >= 2 or machine_attempts >= 3:
            raise ReviewLifecycleError(
                "Cross machine validation failed repeatedly; the workflow stopped "
                "without rewriting the module or reviewer verdict. "
                f"module={module_id}; attempts={machine_attempts}; "
                f"validation_ref={validation_ref}"
            )
        current = revised

    lane_state.setdefault("module_submissions", {})[module_id] = revised
    lane_state.setdefault("specialist_submissions", {})[module_id] = revised
    local_scope = {
        target_id for finding in findings for target_id in finding.target_submodule_ids
    }
    local_diff_ref = (
        f"Work/runs/{state['run_id']}/reviews/module/cross-r{review_round}/"
        f"{module_id}/trigger-diff-r{revised.revision}.json"
    )
    raw_local_diff = build_revision_diff(reviewed_baseline, revised)
    local_diff = ModuleRevisionDiff(
        module_id=raw_local_diff["module_id"],
        from_revision=raw_local_diff["from_revision"],
        to_revision=raw_local_diff["to_revision"],
        changed_submodule_narratives=raw_local_diff[
            "changed_submodule_narratives"
        ],
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
    cross_responses = list(revised.revision_responses)
    local_reviewed = await run_module_review(
        runner,
        module_id,
        revised,
        lane_state,
        workflow_id,
        initial_scope=local_scope,
        lifecycle_id=f"cross-r{review_round}",
        regression_context=ModuleLocalRegressionContext(
            prior_review_completion_ref=prior_completion_ref,
            prior_review_completion=prior_completion,
            baseline_subject_ref=baseline_subject_ref,
            trigger_cross_findings=findings,
            trigger_revision_responses=revised.revision_responses,
            revision_diff_ref=local_diff_ref,
            revision_diff=local_diff,
        ),
    )
    final_subject_ref = (
        f"Work/runs/{state['run_id']}/modules/"
        f"{module_id}-r{local_reviewed.revision}.json"
    )
    final_validation_ref = _run_cross_machine_checks(
        runner,
        state=lane_state,
        subject=local_reviewed,
        subject_ref=final_subject_ref,
        findings=findings,
    )
    final_validation = ValidationReport.model_validate_json(
        (runner.service.workspace / final_validation_ref).read_text(encoding="utf-8")
    )
    _require_validation_binding(
        runner,
        final_validation,
        subject_ref=final_subject_ref,
        subject_revision=local_reviewed.revision,
    )
    if not final_validation.passed:
        raise ReviewLifecycleError(
            "Cross machine validation failed after the module-local regression "
            "review; the workflow stopped before Cross recheck without starting "
            "another author revision. "
            f"module={module_id}; validation_ref={final_validation_ref}"
        )
    local_review_ref = lane_state["module_review_completion_refs"][module_id]
    semantic_payload = {
        "run_id": state["run_id"],
        "review_round": review_round,
        "module_id": module_id,
        "baseline_subject_sha256": _cross_owner_artifact_ref(
            runner, baseline_subject_ref
        ).sha256,
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
        subject=_cross_owner_artifact_ref(runner, final_subject_ref),
        local_review_completion=_cross_owner_artifact_ref(
            runner, local_review_ref
        ),
        machine_validation=_cross_owner_artifact_ref(
            runner, final_validation_ref
        ),
        author_task_attempt_id=f"cross-owner-attempt-{uuid4().hex}",
        reviewer_session_id=ReviewCompletionRecord.model_validate_json(
            (runner.service.workspace / local_review_ref).read_text(
                encoding="utf-8"
            )
        ).reviewer_session_key,
        lease_epoch=1,
    )
    completion_ref = (
        f"Work/runs/{state['run_id']}/lanes/cross-r{review_round}/"
        f"module-{module_id}/completion-r{local_reviewed.revision}.json"
    )
    completion_path = runner.service.workspace / completion_ref
    if completion_path.is_file():
        existing = CrossOwnerCompletion.model_validate_json(
            completion_path.read_text(encoding="utf-8")
        )
        if existing.completion_sha256() != completion.completion_sha256():
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
        machine_validation_ref=final_validation_ref,
        completion_ref=completion_ref,
        completion=completion,
    )


async def run_cross_review(
    runner: "ReportWorkflowRunner",
    state: dict,
    workflow_id: str,
) -> None:
    reviewer_session_key = "cross-module-reviewer"
    modules: dict[str, ModuleSubmission] = dict(state["module_submissions"])
    pending: dict[str, CrossReviewFinding] = {}
    responses_by_module: dict[str, list[RevisionResponse]] = {}
    local_review_refs: dict[str, str] = {}
    machine_refs: list[str] = []
    finding_refs: list[str] = []
    verdict_refs: list[str] = []
    resolved_ids: set[str] = set()
    prior_synthesis = []
    phase = "initial"
    review_round = 0
    revised_owner_ids: set[str] = set()
    cross_owner_barrier_ref: str | None = None
    interface_registry: InterfaceResolutionRegistry | None = state.get(
        "interface_resolution_registry"
    )
    interface_registry_ref: str | None = state.get(
        "interface_resolution_registry_ref"
    )
    interface_registry_sha256: str | None = state.get(
        "interface_resolution_registry_sha256"
    )
    interface_closure_refs: list[str] = list(state.get("interface_closure_refs", []))
    interface_closure_sha256: dict[str, str] = dict(
        state.get("interface_closure_sha256", {})
    )
    interface_residual_risks: dict[str, str] = dict(
        state.get("interface_residual_risks", {})
    )
    progress_ref = f"Work/runs/{state['run_id']}/reviews/cross-progress.json"

    if interface_registry_ref is not None:
        run_prefix = f"Work/runs/{state['run_id']}/"
        if not interface_registry_ref.startswith(run_prefix):
            raise ReviewLifecycleError(
                "Cross interface registry reference is outside the current run"
            )

    if interface_registry is None and interface_registry_ref is not None:
        registry_path = runner.service.workspace / interface_registry_ref
        if not registry_path.is_file():
            raise ReviewLifecycleError(
                f"Cross interface registry is missing: {interface_registry_ref}"
            )
        actual_hash = hashlib.sha256(registry_path.read_bytes()).hexdigest()
        if interface_registry_sha256 != actual_hash:
            raise ReviewLifecycleError(
                "Cross interface registry hash does not match its checkpoint binding"
            )
        try:
            interface_registry = InterfaceResolutionRegistry.model_validate_json(
                registry_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ReviewLifecycleError("Cross interface registry is invalid") from exc
        if interface_registry.run_id != state["run_id"]:
            raise ReviewLifecycleError("Cross interface registry belongs to another run")
        state["interface_resolution_registry"] = interface_registry

    def save_progress(next_action: str) -> None:
        _write_model(
            runner,
            progress_ref,
            CrossReviewProgress(
                run_id=state["run_id"],
                next_action=next_action,
                modules=modules,
                pending=list(pending.values()),
                responses_by_module=responses_by_module,
                local_review_refs=local_review_refs,
                machine_refs=machine_refs,
                finding_refs=finding_refs,
                verdict_refs=verdict_refs,
                resolved_ids=sorted(resolved_ids),
                prior_synthesis=prior_synthesis,
                phase=phase,
                review_round=review_round,
                revised_owner_ids=sorted(revised_owner_ids),
                cross_owner_barrier_ref=cross_owner_barrier_ref,
                interface_registry_ref=interface_registry_ref,
                interface_registry_sha256=interface_registry_sha256,
                interface_closure_refs=interface_closure_refs,
                interface_closure_sha256=interface_closure_sha256,
                interface_residual_risks=interface_residual_risks,
            ),
        )

    progress = (
        _load_progress(runner, progress_ref, CrossReviewProgress) if state.get("resume") else None
    )
    # Crash recovery can leave the immutable Cross r0 finding artifact and an
    # owner completion on disk before the tiny progress checkpoint write.  Do
    # not charge Cross r0 again in that window; reconstruct the minimal
    # revision-wave state from the immutable finding submission below.
    resume_from_finding_only = False
    if progress is None and state.get("resume"):
        finding_ref = f"Work/runs/{state['run_id']}/reviews/cross-findings-r0.json"
        finding_path = runner.service.workspace / finding_ref
        if finding_path.is_file():
            try:
                finding_submission = CrossReviewFindingSubmission.model_validate_json(
                    finding_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise ReviewLifecycleError(
                    "Cross r0 finding artifact is unreadable during resume"
                ) from exc
            finding_refs = [finding_ref]
            pending = {
                finding.id: finding for finding in finding_submission.findings
            }
            prior_synthesis = finding_submission.synthesis_inputs
            phase = "initial"
            review_round = 0
            resume_from_finding_only = True
    if progress is not None and progress.next_action != "completed":
        if progress.run_id != state["run_id"]:
            raise ReviewLifecycleError("Cross review progress identity mismatch")
        modules = progress.modules
        state["module_submissions"].update(modules)
        pending = {finding.id: finding for finding in progress.pending}
        responses_by_module = progress.responses_by_module
        local_review_refs = progress.local_review_refs
        machine_refs = progress.machine_refs
        finding_refs = progress.finding_refs
        verdict_refs = progress.verdict_refs
        resolved_ids = set(progress.resolved_ids)
        prior_synthesis = progress.prior_synthesis
        phase = progress.phase
        review_round = progress.review_round
        revised_owner_ids = set(progress.revised_owner_ids)
        cross_owner_barrier_ref = progress.cross_owner_barrier_ref
        interface_registry_ref = progress.interface_registry_ref
        interface_registry_sha256 = progress.interface_registry_sha256
        interface_closure_refs = list(progress.interface_closure_refs)
        interface_closure_sha256 = dict(progress.interface_closure_sha256)
        interface_residual_risks = dict(progress.interface_residual_risks)
        if cross_owner_barrier_ref is not None:
            state["cross_owner_barrier_ref"] = cross_owner_barrier_ref

    async def complete_revision_wave() -> None:
        nonlocal responses_by_module, local_review_refs, machine_refs
        nonlocal cross_owner_barrier_ref
        grouped: dict[str, list[CrossReviewFinding]] = defaultdict(list)
        for finding in pending.values():
            grouped[finding.owner_module_id].append(finding)
        if not revised_owner_ids:
            responses_by_module = {}
            local_review_refs = {}
            machine_refs = []
        owner_targets = [
            module_id
            for module_id in sorted(grouped, key=float)
            if module_id not in revised_owner_ids
        ]
        if not owner_targets:
            if grouped:
                if cross_owner_barrier_ref is None:
                    raise ReviewLifecycleError(
                        "Cross owner resume lacks its exact owner barrier"
                    )
                barrier_path = runner.service.workspace / cross_owner_barrier_ref
                try:
                    barrier = CrossOwnerBarrier.model_validate_json(
                        barrier_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError) as exc:
                    raise ReviewLifecycleError(
                        "Cross owner resume barrier is unreadable or invalid"
                    ) from exc
                _verify_cross_owner_barrier(
                    runner,
                    barrier,
                    barrier_path=barrier_path,
                    expected_run_id=state["run_id"],
                    expected_round=review_round,
                    expected_modules=set(grouped),
                )
            return

        execution_mode = getattr(
            state.get("request"),
            "execution_mode",
            "current_serial_review",
        )
        # ``all_ready`` is the business path: every distinct owner is admitted
        # immediately and the Provider router, rather than this helper, owns
        # physical backpressure.  ``bounded_module_lanes`` remains a legacy
        # compatibility mode for checkpoints/tests that explicitly request its
        # fixed worker cap.  Cross r0/r1 themselves are always serial.
        parallel_owner_lanes = execution_mode in {
            "all_ready",
            "bounded_module_lanes",
        }
        all_ready_owner_lanes = execution_mode == "all_ready"
        results: dict[str, _CrossOwnerLaneResult] = {}

        def _promote_result(module_id: str, result: _CrossOwnerLaneResult) -> None:
            """Single-writer promotion of one private owner-lane result."""

            modules[module_id] = result.module
            state["module_submissions"][module_id] = result.module
            state.setdefault("specialist_submissions", {})[module_id] = result.module
            state.setdefault("module_review_completion_refs", {})[module_id] = (
                result.local_review_ref
            )
            responses_by_module[module_id] = result.responses
            if result.machine_validation_ref not in machine_refs:
                machine_refs.append(result.machine_validation_ref)
            local_review_refs[module_id] = result.local_review_ref
            revised_owner_ids.add(module_id)

        def _validate_artifact(artifact: ArtifactRef, *, label: str) -> None:
            run_root = (runner.service.workspace / f"Work/runs/{state['run_id']}").resolve()
            path = (runner.service.workspace / artifact.ref).resolve()
            if not path.is_relative_to(run_root) or not path.is_file():
                raise ReviewLifecycleError(
                    f"Cross owner {label} is missing or outside the current run: {artifact.ref}"
                )
            content = path.read_bytes()
            if (
                hashlib.sha256(content).hexdigest() != artifact.sha256
                or len(content) != artifact.size
            ):
                raise ReviewLifecycleError(
                    f"Cross owner {label} hash/size mismatch: {artifact.ref}"
                )

        def _recover_completed_lane(
            module_id: str,
        ) -> _CrossOwnerLaneResult | None:
            """Recover a verified completion without invoking any Agent."""

            lane_root = (
                runner.service.workspace
                / f"Work/runs/{state['run_id']}/lanes/cross-r{review_round}/"
                f"module-{module_id}"
            )
            candidates: list[tuple[int, Path, CrossOwnerCompletion]] = []
            for path in lane_root.glob("completion-r*.json"):
                try:
                    completion = CrossOwnerCompletion.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    continue
                if (
                    completion.run_id != state["run_id"]
                    or completion.review_round != review_round
                    or completion.module_id != module_id
                ):
                    continue
                revision = completion.subject_revision
                if revision is None:
                    match = re.search(r"completion-r([0-9]+)\.json$", path.name)
                    if match is None:
                        continue
                    revision = int(match.group(1))
                candidates.append((revision, path, completion))
            for _revision, completion_path, completion in sorted(
                candidates,
                key=lambda item: (item[0], item[1].as_posix()),
                reverse=True,
            ):
                _validate_artifact(completion.subject, label="subject")
                _validate_artifact(
                    completion.local_review_completion,
                    label="local review completion",
                )
                _validate_artifact(
                    completion.machine_validation,
                    label="machine validation",
                )
                subject_path = runner.service.workspace / completion.subject.ref
                try:
                    subject = ModuleSubmission.model_validate_json(
                        subject_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError) as exc:
                    raise ReviewLifecycleError(
                        f"Cross owner completion subject is invalid: {completion.subject.ref}"
                    ) from exc
                if subject.module_id != module_id:
                    raise ReviewLifecycleError(
                        "Cross owner completion subject owner mismatch: "
                        f"expected={module_id}; actual={subject.module_id}"
                    )
                if (
                    completion.subject_revision is not None
                    and completion.subject_revision != subject.revision
                ):
                    raise ReviewLifecycleError(
                        f"Cross owner completion revision mismatch: {module_id}"
                    )
                _validate_responses(
                    subject.revision_responses,
                    {finding.id for finding in grouped[module_id]},
                    {
                        target_id
                        for finding in grouped[module_id]
                        for target_id in finding.target_submodule_ids
                    },
                )
                machine_ref = completion.machine_validation.ref
                try:
                    machine_report = ValidationReport.model_validate_json(
                        (runner.service.workspace / machine_ref).read_text(
                            encoding="utf-8"
                        )
                    )
                except (OSError, ValueError) as exc:
                    raise ReviewLifecycleError(
                        f"Cross owner machine validation is invalid: {machine_ref}"
                    ) from exc
                _require_validation_binding(
                    runner,
                    machine_report,
                    subject_ref=completion.subject.ref,
                    subject_revision=subject.revision,
                )
                if not machine_report.passed:
                    raise ReviewLifecycleError(
                        f"Cross owner completion binds a failed machine validation: {module_id}"
                    )
                # The completion file itself is immutable evidence.  Hashing
                # its canonical payload catches accidental replacement before
                # it can be promoted into the next Cross input.
                expected_completion_ref = completion_path.relative_to(
                    runner.service.workspace
                ).as_posix()
                if expected_completion_ref != (
                    f"Work/runs/{state['run_id']}/lanes/cross-r{review_round}/"
                    f"module-{module_id}/completion-r{subject.revision}.json"
                ):
                    raise ReviewLifecycleError(
                        f"Cross owner completion path/revision mismatch: {module_id}"
                    )
                return _CrossOwnerLaneResult(
                    module=subject,
                    responses=list(subject.revision_responses),
                    local_review_ref=completion.local_review_completion.ref,
                    machine_validation_ref=completion.machine_validation.ref,
                    completion_ref=expected_completion_ref,
                    completion=completion,
                )
            return None

        def _owner_has_accepted_unknown(module_id: str) -> bool:
            """Fail closed when a Provider attempt may already have been accepted."""

            roots = [
                runner.service.workspace
                / f"Work/runs/{state['run_id']}/lanes/cross-r{review_round}/module-{module_id}/exceptions",
                runner.service.workspace
                / f"Work/runs/{state['run_id']}/collaboration/ambiguities",
                runner.service.workspace
                / f"Work/runs/{state['run_id']}/context-manifests/provider-calls",
            ]
            for root in roots:
                if not root.is_dir():
                    continue
                for path in root.rglob("*.json"):
                    try:
                        payload = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError, TypeError):
                        continue
                    disposition = str(
                        payload.get("attempt_disposition")
                        or payload.get("disposition")
                        or ""
                    )
                    task_id = str(payload.get("task_id") or "")
                    payload_module_id = str(payload.get("module_id") or "")
                    if (
                        disposition == "accepted_or_unknown"
                        and (
                            payload_module_id == module_id
                            or f"module-{module_id}" in task_id
                            or f"-{module_id}." in task_id
                        )
                    ):
                        return True
            return False

        # A crash can occur after a private lane completion is durable but
        # before the coordinator writes progress.  Promote such completions
        # first; only owners with no verified completion are runnable.
        runnable_targets: list[str] = []
        for module_id in sorted(set(grouped).intersection(revised_owner_ids), key=float):
            recovered = _recover_completed_lane(module_id)
            if recovered is None:
                raise ReviewLifecycleError(
                    "Cross owner progress marks an owner complete but its "
                    f"verified completion is missing: {module_id}"
                )
            results[module_id] = recovered
        for module_id in owner_targets:
            recovered = _recover_completed_lane(module_id)
            if recovered is not None:
                results[module_id] = recovered
                continue
            if _owner_has_accepted_unknown(module_id):
                raise ReviewLifecycleError(
                    "Cross owner Provider attempt is accepted_or_unknown; "
                    f"resume will not replay module {module_id}"
                )
            runnable_targets.append(module_id)

        if not parallel_owner_lanes:
            for module_id in runnable_targets:
                results[module_id] = await _run_cross_owner_lane(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    module_id=module_id,
                    current=modules[module_id],
                    findings=grouped[module_id],
                    finding_refs=finding_refs,
                    review_round=review_round,
                    defer_main_exceptions=False,
                )

        parallel_targets = runnable_targets if parallel_owner_lanes else []
        timing_history = TaskTimingHistory(runner.service.workspace)
        candidates = []
        for index, module_id in enumerate(parallel_targets):
            provider_router = getattr(runner.service, "provider_router", None)
            if provider_router is None:
                priority, critical_path_weight, configured_duration_ms = (
                    50,
                    1.0,
                    60_000,
                )
            else:
                priority, critical_path_weight, configured_duration_ms = (
                    provider_router.scheduling_hints(
                        task_id=f"cross-owner-lane:{module_id}",
                        task_kind="cross_owner_lane",
                    )
                )
            candidates.append(
                SchedulingCandidate(
                    task_id=module_id,
                    owner_key=module_id,
                    task_kind="cross_owner_lane",
                    ordinal=index,
                    priority=priority,
                    critical_path_weight=critical_path_weight,
                    expected_duration_ms=timing_history.estimate_ms(
                        "cross_owner_lane",
                        module_id,
                        default=configured_duration_ms,
                    ),
                )
            )
        scheduler = AdaptiveTaskScheduler(candidates)
        failures_by_module: dict[str, BaseException] = {}
        deferred_main_modules: set[str] = set()
        freeze_admission = asyncio.Event()

        def persist_exception_candidate(
            module_id: str,
            disposition: Literal["escalate", "failed", "accepted_or_unknown"],
            reason: str,
            *,
            attempt_disposition: str | None = None,
        ) -> None:
            attempt_id = f"cross-owner-attempt-{uuid4().hex}"
            runner.service.store.write_json(
                (
                    f"Work/runs/{state['run_id']}/lanes/cross-r{review_round}/"
                    f"module-{module_id}/exceptions/{attempt_id}.json"
                ),
                LaneExceptionCandidate(
                    lane_id=f"cross-r{review_round}-module-{module_id}",
                    run_id=state["run_id"],
                    module_id=module_id,
                    disposition=disposition,
                    reason=reason,
                    task_attempt_id=attempt_id,
                    attempt_disposition=attempt_disposition,
                ).model_dump(mode="json"),
            )

        async def run_admitted_lane(module_id: str) -> None:
            started = time.perf_counter()
            try:
                results[module_id] = await _run_cross_owner_lane(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    module_id=module_id,
                    current=modules[module_id],
                    findings=grouped[module_id],
                    finding_refs=finding_refs,
                    review_round=review_round,
                )
                timing_history.record(
                    run_id=state["run_id"],
                    task_id=module_id,
                    task_kind="cross_owner_lane",
                    owner_key=module_id,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    status="completed",
                )
            except DeferredMainDecision as exc:
                persist_exception_candidate(module_id, "escalate", str(exc))
                deferred_main_modules.add(module_id)
                timing_history.record(
                    run_id=state["run_id"],
                    task_id=module_id,
                    task_kind="cross_owner_lane",
                    owner_key=module_id,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    status="deferred",
                )
            except BaseException as exc:
                accepted_unknown = (
                    getattr(exc, "attempt_disposition", None)
                    == "accepted_or_unknown"
                    or exc.__class__.__name__ == "ProviderAttemptRecoveryRequired"
                )
                persist_exception_candidate(
                    module_id,
                    "accepted_or_unknown" if accepted_unknown else "failed",
                    str(exc),
                    attempt_disposition=(
                        "accepted_or_unknown" if accepted_unknown else None
                    ),
                )
                failures_by_module[module_id] = exc
                # all_ready has already admitted every ready owner.  Do not
                # stop siblings when one ordinary owner fails; they must drain
                # to terminal state before the failure is surfaced.  The
                # explicit bounded compatibility mode retains its historical
                # freeze-on-failure behavior.
                if not all_ready_owner_lanes:
                    freeze_admission.set()
                timing_history.record(
                    run_id=state["run_id"],
                    task_id=module_id,
                    task_kind="cross_owner_lane",
                    owner_key=module_id,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    status="failed",
                )

        async def worker() -> None:
            while True:
                if freeze_admission.is_set():
                    return
                candidate = await scheduler.next()
                if candidate is None:
                    return
                if freeze_admission.is_set():
                    return
                await run_admitted_lane(candidate.task_id)

        configured = int(
            getattr(state.get("request"), "module_lane_concurrency", 5)
        )
        worker_count = (
            len(parallel_targets)
            if all_ready_owner_lanes
            else min(max(1, configured), len(parallel_targets))
        )
        if all_ready_owner_lanes:
            # Claim every ready owner before running any lane.  This removes a
            # scheduler/event-loop race where an immediately failing first
            # lane could otherwise consume the queue before sibling owners
            # were admitted.
            admitted: list[str] = []
            while True:
                candidate = await scheduler.next()
                if candidate is None:
                    break
                admitted.append(candidate.task_id)
            await asyncio.gather(
                *(run_admitted_lane(module_id) for module_id in admitted)
            )
        else:
            workers = [
                asyncio.create_task(
                    worker(),
                    name=f"cross-owner-worker-{index + 1}",
                )
                for index in range(worker_count)
            ]
            if workers:
                await asyncio.gather(*workers)
        if parallel_owner_lanes:
            runner.service.store.write_json(
                (
                    f"Work/runs/{state['run_id']}/scheduling/"
                    f"cross-owner-r{review_round}.json"
                ),
                {
                    "policy": "longest_critical_path_first_v1",
                    "decisions": [
                        decision.model_dump(mode="json")
                        for decision in scheduler.decisions
                    ],
                },
            )
        for module_id in sorted(deferred_main_modules, key=float):
            try:
                results[module_id] = await _run_cross_owner_lane(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    module_id=module_id,
                    current=modules[module_id],
                    findings=grouped[module_id],
                    finding_refs=finding_refs,
                    review_round=review_round,
                    defer_main_exceptions=False,
                )
            except BaseException as exc:
                failures_by_module[module_id] = exc

        for module_id in sorted(results, key=float):
            _promote_result(module_id, results[module_id])

        if failures_by_module:
            # Every started owner has reached a terminal result.  Keep all
            # successful owner artifacts and progress, but emit a failed
            # terminal manifest instead of the success barrier; Cross r1 is
            # therefore impossible until a later resume repairs the missing
            # owners.
            failure_refs: dict[str, list[str]] = {}
            for module_id in sorted(failures_by_module, key=float):
                exception_root = (
                    runner.service.workspace
                    / f"Work/runs/{state['run_id']}/lanes/cross-r{review_round}/"
                    f"module-{module_id}/exceptions"
                )
                failure_refs[module_id] = [
                    path.relative_to(runner.service.workspace).as_posix()
                    for path in sorted(exception_root.glob("*.json"))
                    if path.is_file()
                ]
            terminal_ref = (
                f"Work/runs/{state['run_id']}/lanes/cross-r{review_round}/"
                "owner-terminal.json"
            )
            runner.service.store.write_json(
                terminal_ref,
                {
                    "kind": "cross_owner_terminal_barrier",
                    "version": 1,
                    "run_id": state["run_id"],
                    "review_round": review_round,
                    "target_modules": sorted(grouped, key=float),
                    "status": "failed",
                    "completion_refs": {
                        module_id: result.completion_ref
                        for module_id, result in sorted(results.items(), key=lambda item: float(item[0]))
                    },
                    "completion_hashes": {
                        module_id: result.completion.completion_sha256()
                        for module_id, result in sorted(results.items(), key=lambda item: float(item[0]))
                    },
                    "completion_revisions": {
                        module_id: result.module.revision
                        for module_id, result in sorted(results.items(), key=lambda item: float(item[0]))
                    },
                    "failure_refs": failure_refs,
                    "terminal_statuses": {
                        module_id: (
                            "completed" if module_id in results else "failed"
                        )
                        for module_id in sorted(grouped, key=float)
                    },
                },
            )
            save_progress("revise")
            first_module = sorted(failures_by_module, key=float)[0]
            raise failures_by_module[first_module]

        # Verified recovered and newly completed lanes are reduced together in
        # deterministic owner order.  Only this single writer may publish the
        # exact barrier that unlocks Cross r1.
        barrier_inputs: list[tuple[str, CrossOwnerCompletion]] = []
        for module_id in sorted(grouped, key=float):
            result = results.get(module_id)
            if result is None:
                raise ReviewLifecycleError(
                    f"Cross owner wave missing terminal result: {module_id}"
                )
            barrier_inputs.append((result.completion_ref, result.completion))

        barrier = WorkflowReducer(
            runner.service.workspace,
            state["run_id"],
        ).write_cross_owner_barrier(
            review_round,
            sorted(grouped, key=float),
            barrier_inputs,
        )
        cross_owner_barrier_ref = (
            f"Work/runs/{state['run_id']}/lanes/cross-r{review_round}/"
            "owner-barrier.json"
        )
        _verify_cross_owner_barrier(
            runner,
            barrier,
            barrier_path=runner.service.workspace / cross_owner_barrier_ref,
            expected_run_id=state["run_id"],
            expected_round=review_round,
            expected_modules=set(grouped),
        )
        state["cross_owner_barrier_ref"] = cross_owner_barrier_ref
        save_progress("revise")

    def persist_interface_closures(
        closures: list[InterfaceCrossClosure],
        *,
        review_round: int,
    ) -> None:
        """Persist an immutable closure batch and its hash-bound registry view."""

        nonlocal interface_registry, interface_registry_ref, interface_registry_sha256
        if interface_registry is None:
            if closures:
                raise ReviewLifecycleError(
                    "Cross returned interface closures without a canonical registry"
                )
            return
        try:
            interface_registry = apply_interface_cross_closure(
                interface_registry,
                closures,
                review_round=review_round,
            )
        except ValueError as exc:
            raise ReviewLifecycleError(f"invalid Cross interface closure set: {exc}") from exc
        batch = InterfaceResolutionClosureBatch(
            run_id=state["run_id"],
            review_round=review_round,
            closures=[closure.model_copy(deep=True) for closure in closures],
        )
        batch_ref = _write_immutable_model(
            runner,
            f"Work/runs/{state['run_id']}/reviews/interface-closures-r{review_round}.json",
            batch,
        )
        registry_ref = _write_immutable_model(
            runner,
            f"Work/runs/{state['run_id']}/collaboration/interface-resolution-registry-r{review_round}.json",
            interface_registry,
        )
        interface_closure_refs.append(batch_ref)
        interface_closure_sha256[batch_ref] = hashlib.sha256(
            (runner.service.workspace / batch_ref).read_bytes()
        ).hexdigest()
        interface_registry_ref = registry_ref
        interface_registry_sha256 = hashlib.sha256(
            (runner.service.workspace / registry_ref).read_bytes()
        ).hexdigest()
        state["interface_resolution_registry"] = interface_registry
        state["interface_resolution_registry_ref"] = interface_registry_ref
        state["interface_resolution_registry_sha256"] = interface_registry_sha256
        state["interface_closure_refs"] = list(interface_closure_refs)
        state["interface_closure_sha256"] = dict(interface_closure_sha256)
        state["interface_residual_risks"] = dict(interface_residual_risks)

    def interface_reroute_finding(
        closure: InterfaceCrossClosure,
    ) -> CrossReviewFinding:
        if interface_registry is None:
            raise ReviewLifecycleError("cannot materialize XMR-IF without interface registry")
        try:
            resolution = interface_registry.resolutions[closure.request_id]
        except KeyError as exc:
            raise ReviewLifecycleError(
                f"Cross reroute references unknown IF: {closure.request_id}"
            ) from exc
        request = resolution.request
        owner_module_id = closure.owner_module_id or request.requester_module_id
        owner_submodule_id = closure.owner_submodule_id or request.requester_submodule_id
        if owner_submodule_id is None:
            # Legacy module-level requests have no fixed leaf target.  Their
            # requester module's first leaf is the only deterministic owner
            # lane target, while the request id remains the stable XMR key.
            owner_submodule_id = REPORT_TAXONOMY[owner_module_id].submodules[0]
        related = sorted(
            {
                request.requester_module_id,
                request.target_module_id,
            }
            - {owner_module_id},
            key=float,
        )
        source_refs = list(
            dict.fromkeys(
                [
                    *request.evidence_ids,
                    *resolution.disposition.evidence_ids,
                    *resolution.disposition.checked_evidence_ids,
                ]
            )
        ) or [request.request_id]
        return CrossReviewFinding(
            id=interface_owner_finding_id(closure.request_id),
            owner_module_id=owner_module_id,
            target_submodule_ids=[owner_submodule_id],
            related_module_ids=related,
            category="dependencies",
            impact="blocking" if request.blocking else "advisory",
            observation=(
                f"Wave 2 request {request.request_id} remains unresolved after the "
                "target checks; Cross requires the requester-owned interface boundary "
                "to be written into the module subject before closure."
            ),
            evidence_refs=source_refs,
            required_change=(
                f"Write the interface question and its unresolved boundary into "
                f"{owner_submodule_id}; preserve the target response limit and state "
                f"the reroute reason: {closure.reason}."
            ),
            reviewer_checks=[
                "the owner submodule names the exact interface request and boundary",
                "the final text preserves the residual risk and does not invent an answer",
            ],
        )

    if resume_from_finding_only and not pending:
        _validate_cross_synthesis_portfolio(prior_synthesis, modules)
        module_refs = {
            module_id: (
                f"Work/runs/{state['run_id']}/modules/"
                f"{module_id}-r{modules[module_id].revision}.json"
            )
            for module_id in REPORT_TAXONOMY
        }
        completion_refs = [*module_refs.values(), *finding_refs]
        completion = ReviewCompletionRecord(
            lifecycle="cross",
            run_id=state["run_id"],
            reviewer_agent_id="cross-module-reviewer",
            reviewer_session_key=reviewer_session_key,
            subject_refs=list(module_refs.values()),
            finding_refs=finding_refs,
            verdict_refs=[],
            resolved_finding_ids=[],
            artifact_sha256=_artifact_sha256(runner, completion_refs),
        )
        completion_ref = _write_immutable_model(
            runner,
            f"Work/runs/{state['run_id']}/reviews/cross-completion.json",
            completion,
        )
        state["cross_review_completion_ref"] = completion_ref
        state["cross_synthesis_inputs"] = prior_synthesis
        save_progress("completed")
        return

    if (
        progress is not None and progress.next_action == "revise"
    ) or resume_from_finding_only:
        await complete_revision_wave()
        phase = "recheck"
        review_round += 1
        save_progress("review")

    while True:
        module_refs = {
            module_id: (
                f"Work/runs/{state['run_id']}/modules/"
                f"{module_id}-r{modules[module_id].revision}.json"
            )
            for module_id in REPORT_TAXONOMY
        }
        changed_module_ids = set(REPORT_TAXONOMY) if phase == "initial" else set(revised_owner_ids)
        if phase == "recheck" and not changed_module_ids:
            raise ReviewLifecycleError(
                "Cross recheck requires the current revision wave module ids"
            )
        unchanged_module_sha256 = (
            {}
            if phase == "initial"
            else {
                module_id: hashlib.sha256(
                    (runner.service.workspace / module_refs[module_id]).read_bytes()
                ).hexdigest()
                for module_id in set(REPORT_TAXONOMY) - changed_module_ids
            }
        )
        machine_validation_reports: list[ValidationReport] = []
        if phase == "recheck":
            for validation_ref in machine_refs:
                report = ValidationReport.model_validate_json(
                    (runner.service.workspace / validation_ref).read_text(encoding="utf-8")
                )
                _require_validation_binding(
                    runner,
                    report,
                    subject_ref=report.subject_ref,
                    subject_revision=report.subject_revision
                    if report.subject_revision is not None
                    else -1,
                )
                machine_validation_reports.append(report)
        cross_input = CrossReviewInput(
            phase=phase,
            run_id=state["run_id"],
            module_refs=module_refs,
            module_revisions={
                module_id: modules[module_id].revision for module_id in REPORT_TAXONOMY
            },
            modules={
                module_id: module_content_view(modules[module_id])
                for module_id in changed_module_ids
            },
            changed_module_ids=sorted(changed_module_ids),
            unchanged_module_sha256=unchanged_module_sha256,
            required_findings=list(pending.values()) if phase == "recheck" else [],
            revision_responses_by_module=responses_by_module if phase == "recheck" else {},
            local_regression_review_refs=local_review_refs if phase == "recheck" else {},
            prior_synthesis_inputs=prior_synthesis if phase == "recheck" else [],
            machine_validation_refs=machine_refs if phase == "recheck" else [],
            machine_validation_reports=machine_validation_reports,
            interface_registry_ref=interface_registry_ref,
            interface_registry_sha256=interface_registry_sha256,
            pending_interface_request_ids=(
                list(interface_registry.pending_request_ids)
                if interface_registry is not None
                else []
            ),
            interface_closure_refs=list(interface_closure_refs),
            interface_residual_risks=dict(interface_residual_risks),
        )
        input_ref = _write_model(
            runner,
            f"Work/runs/{state['run_id']}/reviews/cross-review-input-r{review_round}.json",
            cross_input,
        )
        output_kind = (
            "cross_review_finding_submission"
            if phase == "initial"
            else "cross_review_verdict_submission"
        )
        envelope = TaskEnvelope(
            task_id=f"cross-review-r{review_round}",
            run_id=state["run_id"],
            agent_id="cross-module-reviewer",
            objective=(
                "审查五个已完成模块之间的术语、事实、风险、依赖、传播和联合验证接口。"
                if phase == "initial"
                else "由原 Cross reviewer 逐项判断 required_findings 是否关闭并检查接口回归。"
            ),
            input_refs=[input_ref],
            constraints=[
                "coverage 只记录审查范围，不含 integrated 或 approved 状态",
                "findings 只包含必须写回责任模块的问题",
                "synthesis_inputs 只包含无需模块返工、可供总编综合的已支持关系",
                (
                    "recheck 只接收 changed_module_ids 的当前正文；未修改模块由首轮"
                    "不可变 finding/synthesis 输入及当前 SHA-256 证明保持不变，"
                    "不得依赖或要求进程内对话记忆"
                ),
                "不得审查单模块局部写作质量",
                (
                    "首轮 coverage 必须对五个模块逐项覆盖全部六个维度"
                    if phase == "initial"
                    else "verdicts 必须逐项且仅覆盖 required_findings；模块 auditor 的结果不能代替 Cross verdict"
                ),
                *runner._user_supplement_constraints(
                    state,
                    stage="cross_review",
                    target_ids={
                        *REPORT_TAXONOMY,
                        *(claim.id for module in modules.values() for claim in module.claims),
                    },
                ),
            ],
            allowed_outputs=[output_kind],
            revision=review_round,
            prior_result_ref=finding_refs[-1] if finding_refs else None,
            artifact_delivery_modes={
                input_ref: "inline",
                **(
                    {finding_refs[-1]: "hash_retained"}
                    if finding_refs
                    else {}
                ),
            },
            input_contract_kind="cross_review_input",
            input_contract_ref=input_ref,
            inline_context=runner._role_skill_context(state, "cross-reviewer"),
            allowed_tools=["submit_result"],
        )
        result = await runner._agent(
            "cross-module-reviewer",
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key=reviewer_session_key,
        )
        if phase == "initial":
            if not isinstance(result, CrossReviewFindingSubmission):
                raise ReviewLifecycleError("Cross reviewer returned the wrong initial type")
            for entry in result.coverage:
                missing = set(CROSS_REVIEW_DIMENSIONS) - set(entry.checked_dimensions)
                if missing:
                    raise ReviewLifecycleError(
                        f"Cross coverage for {entry.module_id} omitted dimensions: {sorted(missing)}"
                    )
            # Every unresolved Wave 2 request must be closed by Cross r0 in
            # one exact typed batch.  Wave 2 answered requests are terminal
            # and are intentionally absent from this set.
            if interface_registry is not None:
                expected_interface_ids = set(interface_registry.pending_request_ids)
                actual_interface_ids = {
                    closure.request_id for closure in result.interface_closures
                }
                if actual_interface_ids != expected_interface_ids:
                    raise ReviewLifecycleError(
                        "Cross r0 interface closures must cover the exact unresolved IF set; "
                        f"missing={sorted(expected_interface_ids - actual_interface_ids)}; "
                        f"extra={sorted(actual_interface_ids - expected_interface_ids)}"
                    )
                persist_interface_closures(
                    list(result.interface_closures),
                    review_round=0,
                )
                for closure in result.interface_closures:
                    if closure.outcome == "confirmed_missing":
                        interface_residual_risks[closure.request_id] = (
                            closure.residual_risk or ""
                        )
                    elif closure.outcome == "reroute_to_owner":
                        result.findings.append(interface_reroute_finding(closure))
                state["interface_residual_risks"] = dict(interface_residual_risks)
            _validate_cross_findings(result.findings, modules)
            finding_ref = _write_immutable_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/cross-findings-r{review_round}.json",
                result,
            )
            finding_refs.append(finding_ref)
            pending = {finding.id: finding for finding in result.findings}
            prior_synthesis = result.synthesis_inputs
        else:
            if not isinstance(result, CrossReviewVerdictSubmission):
                raise ReviewLifecycleError("Cross reviewer returned the wrong recheck type")
            _validate_verdicts(result.verdicts, set(pending))
            _validate_cross_findings(result.new_findings, modules)
            if interface_registry is not None:
                expected_interface_ids = set(interface_registry.pending_request_ids)
                actual_interface_ids = {
                    closure.request_id for closure in result.interface_closures
                }
                if actual_interface_ids != expected_interface_ids:
                    raise ReviewLifecycleError(
                        "Cross r1 interface closures must cover the exact pending IF/XMR set; "
                        f"missing={sorted(expected_interface_ids - actual_interface_ids)}; "
                        f"extra={sorted(actual_interface_ids - expected_interface_ids)}"
                    )
                verdict_by_id = {verdict.finding_id: verdict for verdict in result.verdicts}
                for closure in result.interface_closures:
                    xmr_id = interface_owner_finding_id(closure.request_id)
                    verdict = verdict_by_id.get(xmr_id)
                    if verdict is None or verdict.verdict != "resolved":
                        raise ReviewLifecycleError(
                            f"Cross r1 cannot close {closure.request_id} without a resolved {xmr_id} verdict"
                        )
                persist_interface_closures(
                    list(result.interface_closures),
                    review_round=1,
                )
                state["interface_residual_risks"] = dict(interface_residual_risks)
            verdict_ref = _write_immutable_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/cross-verdicts-r{review_round}.json",
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
                    scope="cross",
                    subject_refs=list(module_refs.values()),
                    finding_refs=finding_refs,
                    verdicts=escalated,
                    responses=[
                        response for values in responses_by_module.values() for response in values
                    ],
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
                        f"new Cross finding reuses an existing id: {finding.id}"
                    )
                next_pending[finding.id] = finding
            if result.new_findings:
                new_ref = _write_immutable_model(
                    runner,
                    (
                        f"Work/runs/{state['run_id']}/reviews/"
                        f"cross-regression-findings-r{review_round}.json"
                    ),
                    CrossReviewFindingSubmission(
                        coverage=result.coverage,
                        findings=result.new_findings,
                        synthesis_inputs=result.synthesis_inputs,
                    ),
                )
                finding_refs.append(new_ref)
            pending = next_pending
            prior_synthesis = result.synthesis_inputs

        if not pending:
            if interface_registry is not None and interface_registry.pending_request_ids:
                raise ReviewLifecycleError(
                    "Cross completion is forbidden while interface IF/XMR records remain pending"
                )
            _validate_cross_synthesis_portfolio(prior_synthesis, modules)
            completion_refs = [
                *module_refs.values(),
                *finding_refs,
                *verdict_refs,
            ]
            completion = ReviewCompletionRecord(
                lifecycle="cross",
                run_id=state["run_id"],
                reviewer_agent_id="cross-module-reviewer",
                reviewer_session_key=reviewer_session_key,
                subject_refs=list(module_refs.values()),
                finding_refs=finding_refs,
                verdict_refs=verdict_refs,
                resolved_finding_ids=sorted(resolved_ids),
                artifact_sha256=_artifact_sha256(runner, completion_refs),
            )
            completion_ref = _write_immutable_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/cross-completion.json",
                completion,
            )
            state["cross_review_completion_ref"] = completion_ref
            state["cross_synthesis_inputs"] = prior_synthesis
            save_progress("completed")
            return

        revised_owner_ids = set()
        save_progress("revise")
        await complete_revision_wave()
        phase = "recheck"
        review_round += 1
        save_progress("review")


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
    revision_context = runner._role_skill_context(
        state,
        "chief-revision",
        target_section_ids=target_sections,
    )
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
        state["editor_quality_observations"] = validate_aggregate_retention(
            revised_report, approved_module_text
        )
        if claims:
            validate_editor_protection(revised_report, claims)
    else:
        validate_editor_protection(revised_report, claims)
        state["editor_quality_observations"] = validate_editor_quality(
            revised_report, state["module_submissions"]
        )
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
            section_id
            for section_id in FINAL_SUMMARY_CONCLUSION_AUDIT_SECTION_IDS
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
                f"Work/runs/{state['run_id']}/validation/"
                f"report-chief-candidate-r{review_round}.md"
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
            subject_metadata_sha256=(
                _model_sha256(metadata) if phase == "recheck" else None
            ),
            canonical_markdown=(
                _final_audit_markdown(strip_runtime_claim_markers(canonical))
                if phase == "initial"
                else None
            ),
            changed_section_bodies=(
                {
                    section_id: audit_bodies[section_id]
                    for section_id in sorted(recheck_targets)
                }
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
                **(
                    {finding_refs[-1]: "hash_retained"}
                    if finding_refs
                    else {}
                ),
            },
            input_contract_kind=(
                "aggregate_final_review_input"
                if aggregate_mode
                else "final_review_input"
            ),
            input_contract_ref=input_ref,
            inline_context=runner._role_skill_context(state, "final-auditor"),
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
            completion_refs = [subject_ref, *finding_refs, *verdict_refs]
            completion = ReviewCompletionRecord(
                lifecycle="final",
                run_id=state["run_id"],
                reviewer_agent_id="chief-editor-auditor",
                reviewer_session_key=reviewer_session_key,
                subject_refs=[subject_ref],
                finding_refs=finding_refs,
                verdict_refs=verdict_refs,
                resolved_finding_ids=sorted(resolved_ids),
                artifact_sha256=_artifact_sha256(runner, completion_refs),
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
                    subject_sha256=hashlib.sha256(
                        (runner.service.workspace / subject_ref).read_bytes()
                    ).hexdigest(),
                    canonical_markdown_ref=canonical_ref,
                    canonical_markdown_sha256=hashlib.sha256(
                        (runner.service.workspace / canonical_ref).read_bytes()
                    ).hexdigest(),
                    validation_report_ref=integrity_ref,
                    validation_report_sha256=hashlib.sha256(
                        (runner.service.workspace / integrity_ref).read_bytes()
                    ).hexdigest(),
                    completion_ref=completion_ref,
                    completion_sha256=hashlib.sha256(
                        (runner.service.workspace / completion_ref).read_bytes()
                    ).hexdigest(),
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
