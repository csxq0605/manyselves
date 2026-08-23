"""Capability-owned preparation and acceptance of a module Author revision.

The functions in this module are the typed boundary between the file-defined
module Lane and the neutral Agent bridge.  They apply one explicit
``ModuleRevisionSubmission`` to the current subject; they do not invoke an
Agent and do not own recovery or provider policy.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.domain.revision_diff import (
    build_revision_diff,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CrossReviewFinding,
    ModuleReviewFinding,
    ModuleRevisionSubmission,
    ModuleSubmission,
    RevisionResponse,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ModuleRevisionInput,
    RequestedModuleChange,
    ValidationReport,
    module_content_view,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleRevisionAgentResult,
    DeclarativeModuleRevisionPreparation,
    DeclarativeModuleRuntimeLaneContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    UserSupplement,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    ModuleInitialReviewAcceptance,
    ModuleRecheckAcceptance,
    ModuleReviewProgress,
    ModuleRevisionPreparation,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.capabilities.distribution_reporting.runtime.user_supplements import (
    user_supplement_constraints,
)


class ModuleRevisionPreparationError(ValueError):
    """Expected preparation failure translated by the Core compatibility layer."""


class ModuleRevisionAcceptanceError(ValueError):
    """Expected acceptance failure translated by the Core compatibility layer."""


def _context(value: Any) -> DeclarativeModuleRuntimeLaneContext:
    if isinstance(value, DeclarativeModuleRuntimeLaneContext):
        return value
    return DeclarativeModuleRuntimeLaneContext.model_validate(value)


def _review_findings(context: DeclarativeModuleRuntimeLaneContext):
    review = context.review
    acceptance = review.acceptance if review is not None else None
    if isinstance(acceptance, (ModuleInitialReviewAcceptance, ModuleRecheckAcceptance)):
        return list(acceptance.findings)
    return []


async def prepare_module_revision(
    *,
    workspace: Path,
    store: ReportingStore,
    state: dict[str, Any],
    workflow_id: str,
    subject: ModuleSubmission,
    module_findings: list[ModuleReviewFinding] | None = None,
    cross_findings: list[CrossReviewFinding] | None = None,
    requested_changes: list[RequestedModuleChange] | None = None,
    validation_ref: str | None = None,
    validation_target_submodule_ids: set[str] | None = None,
    validate_validation_binding: Callable[[ValidationReport, str, int], None] | None = None,
    user_supplements: Iterable[UserSupplement | Mapping[str, Any]] | None = None,
) -> ModuleRevisionPreparation:
    """Prepare the existing generic module revision contract.

    The Core compatibility layer supplies only the existing validation
    identity callback.  Typed user supplements are projected here so Cross
    and ordinary module revisions use one implementation.
    """

    module_findings = module_findings or []
    cross_findings = cross_findings or []
    requested_changes = requested_changes or []
    validation_target_submodule_ids = validation_target_submodule_ids or set()
    validation_report = (
        ValidationReport.model_validate_json(
            (Path(workspace) / validation_ref).read_text(encoding="utf-8")
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
        if validate_validation_binding is not None:
            validate_validation_binding(
                validation_report,
                validation_subject_ref,
                subject.revision,
            )
        if not targets:
            raise ModuleRevisionPreparationError(
                "failed structural validation requires explicit module-local correction targets"
            )
    if not targets:
        raise ModuleRevisionPreparationError(
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
    input_ref = (
        f"Work/runs/{state['run_id']}/reviews/module-revision-input-"
        f"{subject.module_id}-r{subject.revision + 1}.json"
    )
    store.write_json(input_ref, revision_input.model_dump(mode="json"))
    subject_path = Path(workspace) / revision_input.subject_ref
    if not subject_path.is_file():
        store.write_json(revision_input.subject_ref, subject.model_dump(mode="json"))
    specialist_id = f"module-{subject.module_id}-specialist"
    revision = subject.revision + 1
    constraints = [
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
    ]
    constraints.extend(
        user_supplement_constraints(
            user_supplements or (),
            stage="module_authoring",
            target_ids={
                subject.module_id,
                *targets,
                *(claim.id for claim in subject.claims if claim.submodule_id in targets),
            },
        )
    )
    envelope = TaskEnvelope(
        task_id=f"module-revision-r{revision}-{subject.module_id}",
        run_id=state["run_id"],
        agent_id=specialist_id,
        objective=(
            f"以完整模块 {subject.module_id} 的单一作者身份，一次完成所有明确分配的"
            "定向修订；只替换受影响小节，不重复提交未变正文。"
        ),
        input_refs=[input_ref],
        constraints=constraints,
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


def prepare_current_module_revision(
    value: DeclarativeModuleRuntimeLaneContext,
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Build the exact typed Author revision input for current findings."""

    context = _context(value)
    subject = context.module
    if subject is None:
        raise ValueError("module revision requires the current module subject")
    findings = _review_findings(context)
    if not findings:
        return context

    run_id = str(context.reporting_state["run_id"])
    targets = {finding.target_submodule_id for finding in findings}
    subject_ref = f"Work/runs/{run_id}/modules/{context.module_id}-r{subject.revision}.json"
    if not (store.workspace / subject_ref).is_file():
        store.write_json(subject_ref, subject.model_dump(mode="json"))
    revision = subject.revision + 1
    revision_input = ModuleRevisionInput(
        run_id=run_id,
        module_id=context.module_id,
        subject_ref=subject_ref,
        subject=module_content_view(subject, targets),
        target_submodule_ids=sorted(targets),
        module_findings=findings,
    )
    input_ref = (
        f"Work/runs/{run_id}/reviews/module-revision-input-{context.module_id}-r{revision}.json"
    )
    store.write_json(input_ref, revision_input.model_dump(mode="json"))
    specialist_id = f"module-{context.module_id}-specialist"
    envelope = TaskEnvelope(
        task_id=f"module-revision-r{revision}-{context.module_id}",
        run_id=run_id,
        agent_id=specialist_id,
        objective=(
            f"以完整模块 {context.module_id} 的单一作者身份，一次完成所有明确分配的"
            "定向修订；只替换受影响小节，不重复提交未变正文。"
        ),
        input_refs=[input_ref],
        constraints=[
            f"唯一写作范围是模块 {context.module_id}",
            f"本轮必须在一次 module_revision_submission 中覆盖目标 {sorted(targets)}",
            "submodule_narratives 只包含实际改变的已分配小节；不得修改其他模块或未分配小节",
            "revision_responses 必须逐项且仅覆盖全部分配的 finding ids",
            "disputed 或 needs_input 不得伪造 changed_target_ids",
        ],
        allowed_outputs=["module_revision_submission"],
        revision=revision,
        prior_result_ref=subject_ref,
        artifact_delivery_modes={input_ref: "inline", subject_ref: "reference"},
        target_submodule_ids=sorted(targets),
        input_contract_kind="module_revision_input",
        input_contract_ref=input_ref,
        inline_context="",
        allowed_tools=["submit_result"],
    )
    prepared = ModuleRevisionPreparation(
        run_id=run_id,
        module_id=context.module_id,
        workflow_id=context.workflow_id,
        specialist_id=specialist_id,
        session_key=f"module-{context.module_id}",
        subject=subject,
        revision_input=revision_input,
        input_ref=input_ref,
        subject_ref=subject_ref,
        revision=revision,
        target_submodule_ids=sorted(targets),
        required_finding_ids=sorted(finding.id for finding in findings),
        envelope=envelope,
    )
    return context.model_copy(
        deep=True,
        update={
            "status": "revision_ready",
            "revision": DeclarativeModuleRevisionPreparation(prepared=prepared),
            "error": None,
        },
    )


def _validate_responses(
    responses: list[RevisionResponse],
    required_ids: set[str],
    targets: set[str],
) -> None:
    ids = [response.finding_id for response in responses]
    if len(ids) != len(set(ids)) or set(ids) != required_ids:
        raise ValueError("module revision responses must cover each assigned finding exactly once")
    invalid_targets = sorted(
        {
            target
            for response in responses
            for target in response.changed_target_ids
            if target not in targets
        }
    )
    if invalid_targets:
        raise ValueError(f"module revision response targets are out of scope: {invalid_targets}")


def load_module_revision_candidate(
    *,
    workspace: Path,
    run_id: str,
    module_id: str,
    current: ModuleSubmission,
    findings: Iterable[CrossReviewFinding],
) -> tuple[ModuleSubmission, str] | None:
    """Load the existing same-run Author candidate without changing its rules."""

    findings = list(findings)
    required_finding_ids = {finding.id for finding in findings}
    target_submodule_ids = {
        target_id
        for finding in findings
        for target_id in finding.target_submodule_ids
    }
    modules_root = Path(workspace) / f"Work/runs/{run_id}/modules"
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
                required_finding_ids,
                target_submodule_ids,
            )
            candidates.append(candidate)
        except (OSError, ValueError):
            continue
    if not candidates:
        return None
    candidate = max(candidates, key=lambda item: item.revision)
    candidate_ref = (
        f"Work/runs/{run_id}/modules/{module_id}-r{candidate.revision}.json"
    )
    return candidate, candidate_ref


def apply_module_revision(
    baseline: ModuleSubmission,
    patch: ModuleRevisionSubmission,
    *,
    target_submodule_ids: set[str],
    required_finding_ids: set[str],
) -> ModuleSubmission:
    """Apply one bounded revision patch while retaining unassigned content."""

    if patch.module_id != baseline.module_id:
        raise ValueError("module revision belongs to a different module")
    if patch.base_revision != baseline.revision:
        raise ValueError("module revision base_revision does not match the current subject")
    if set(patch.submodule_narratives) - target_submodule_ids:
        raise ValueError("module revision changes an unassigned submodule")
    _validate_responses(patch.revision_responses, required_finding_ids, target_submodule_ids)

    claims = {claim.id: claim for claim in baseline.claims}
    for claim_id in patch.claim_ids_remove:
        claim = claims.get(claim_id)
        if claim is None:
            raise ValueError(f"module revision removes unknown Claim id: {claim_id}")
        if claim.submodule_id not in target_submodule_ids:
            raise ValueError(f"module revision removes an out-of-scope Claim id: {claim_id}")
        del claims[claim_id]
    for claim in patch.claims_upsert:
        if claim.module_id != baseline.module_id:
            raise ValueError(f"module revision Claim belongs to another module: {claim.id}")
        if claim.submodule_id not in target_submodule_ids:
            raise ValueError(f"module revision changes an out-of-scope Claim id: {claim.id}")
        claims[claim.id] = claim
    resulting_sources = {source_id for claim in claims.values() for source_id in claim.source_ids}
    if not resulting_sources.issubset(set(patch.source_ids)):
        raise ValueError("module revision source_ids omit a resulting Claim source")
    narratives = dict(baseline.submodule_narratives)
    narratives.update(patch.submodule_narratives)
    return ModuleSubmission(
        module_id=baseline.module_id,
        submodule_narratives=narratives,
        claims=list(claims.values()),
        source_ids=patch.source_ids,
        unresolved_questions=patch.unresolved_questions,
        revision=patch.revision,
        revision_responses=patch.revision_responses,
    )


def accept_module_revision(
    *,
    workspace: Path,
    store: ReportingStore,
    preparation: ModuleRevisionPreparation,
    result: ModuleRevisionSubmission,
) -> tuple[ModuleSubmission, str]:
    """Apply and persist one generic module revision.

    The barrier and its existing ``subject_sha256`` field are retained
    mechanically from the Reporting lifecycle.  This function does not add a
    new comparison or recovery decision around that metadata.
    """

    if not isinstance(result, ModuleRevisionSubmission):
        raise ModuleRevisionAcceptanceError(
            f"module specialist returned the wrong revision type for {preparation.module_id}"
        )
    try:
        revised = apply_module_revision(
            preparation.subject,
            result,
            target_submodule_ids=set(preparation.target_submodule_ids),
            required_finding_ids=set(preparation.required_finding_ids),
        )
    except ValueError as exc:
        raise ModuleRevisionAcceptanceError(str(exc)) from exc
    subject_ref = (
        f"Work/runs/{preparation.run_id}/modules/"
        f"{preparation.module_id}-r{preparation.revision}.json"
    )
    store.write_json(subject_ref, revised.model_dump(mode="json"))
    store.write_json(
        (
            f"Work/runs/{preparation.run_id}/reviews/module-diff-"
            f"{preparation.module_id}-r{preparation.revision}.json"
        ),
        build_revision_diff(preparation.subject, revised),
    )
    store.write_json(
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
                (Path(workspace) / subject_ref).read_bytes()
            ).hexdigest(),
        },
    )
    return revised, subject_ref


def _save_progress(
    store: ReportingStore,
    *,
    progress_ref: str,
    context: DeclarativeModuleRuntimeLaneContext,
    current: ModuleSubmission,
    findings: list[Any],
    responses: list[RevisionResponse],
) -> None:
    existing_path = store.workspace / progress_ref
    existing = (
        ModuleReviewProgress.model_validate_json(existing_path.read_text(encoding="utf-8"))
        if existing_path.is_file()
        else None
    )
    store.write_json(
        progress_ref,
        ModuleReviewProgress(
            run_id=str(context.reporting_state["run_id"]),
            module_id=context.module_id,
            next_action="revise",
            current=current,
            pending=findings,
            responses=responses,
            finding_refs=list(existing.finding_refs) if existing else [],
            verdict_refs=list(existing.verdict_refs) if existing else [],
            resolved_ids=list(existing.resolved_ids) if existing else [],
            review_round=existing.review_round if existing else 0,
            phase=existing.phase if existing else "initial",
            scope=list(existing.scope)
            if existing
            else sorted(finding.target_submodule_id for finding in findings),
            reviewer_session_key=(
                existing.reviewer_session_key if existing else f"module-auditor-{context.module_id}"
            ),
            last_reviewed_subject_ref=(existing.last_reviewed_subject_ref if existing else None),
            review_protocol_version=2,
        ).model_dump(mode="json"),
    )


def accept_current_module_revision(
    value: Mapping[str, Any],
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Persist one typed Author revision and retain the pending findings."""

    context = _context(value["context"])
    result = DeclarativeModuleRevisionAgentResult.model_validate(value["result"])
    if result.status == "failed":
        return context.model_copy(
            deep=True,
            update={"status": "failed", "error": result.error or "module revision failed"},
        )
    if result.submission is None or context.revision is None:
        raise ValueError("module revision result lacks its prepared submission boundary")
    prepared = context.revision.prepared
    revised = apply_module_revision(
        prepared.subject,
        ModuleRevisionSubmission.model_validate(result.submission),
        target_submodule_ids=set(prepared.target_submodule_ids),
        required_finding_ids=set(prepared.required_finding_ids),
    )
    run_id = str(context.reporting_state["run_id"])
    subject_ref = f"Work/runs/{run_id}/modules/{context.module_id}-r{revised.revision}.json"
    store.write_json(subject_ref, revised.model_dump(mode="json"))
    store.write_json(
        f"Work/runs/{run_id}/reviews/module-diff-{context.module_id}-r{revised.revision}.json",
        build_revision_diff(prepared.subject, revised),
    )
    findings = _review_findings(context)
    progress_ref = (
        context.review.acceptance.progress_ref
        if context.review is not None
        and isinstance(
            context.review.acceptance,
            (ModuleInitialReviewAcceptance, ModuleRecheckAcceptance),
        )
        else f"Work/runs/{run_id}/reviews/module/initial/{context.module_id}/progress.json"
    )
    _save_progress(
        store,
        progress_ref=progress_ref,
        context=context,
        current=revised,
        findings=findings,
        responses=list(revised.revision_responses),
    )
    state = context.reporting_state
    state.setdefault("specialist_submissions", {})[context.module_id] = revised
    review = context.review
    if review is not None and review.acceptance is not None:
        acceptance = review.acceptance.model_copy(update={"current": revised})
        review = review.model_copy(update={"acceptance": acceptance})
    return context.model_copy(
        deep=True,
        update={
            "status": "reviewed",
            "module": revised,
            "revision": None,
            "review": review,
            "error": None,
        },
    )


__all__ = [
    "accept_module_revision",
    "accept_current_module_revision",
    "apply_module_revision",
    "load_module_revision_candidate",
    "prepare_module_revision",
    "prepare_current_module_revision",
]
