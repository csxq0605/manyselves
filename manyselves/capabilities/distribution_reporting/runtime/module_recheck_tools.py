"""Capability-owned module Auditor recheck boundary.

This slice keeps the existing typed finding/verdict, bounded delta, and
progress artifacts.  It does not introduce provider recovery, locks, or a new
decision layer; the file Workflow remains responsible for routing the returned
acceptance.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleReviewFinding,
    ModuleReviewVerdictSubmission,
    ResolutionVerdict,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ModuleReviewInput,
    ReviewCompletionRecord,
    report_instruction_from_state,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleRecheckAgentResult,
    DeclarativeModuleRecheckPreparation,
    DeclarativeModuleRuntimeLaneContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    ModuleInitialReviewAcceptance,
    ModuleRecheckAcceptance,
    ModuleRecheckPreparation,
    ModuleReviewPreflightProgress,
    ModuleReviewProgress,
)
from manyselves.capabilities.distribution_reporting.runtime.module_preflight_revision import (
    advance_module_review_preflight_progress,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_acceptance import (
    _write_immutable_json,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_delta import (
    build_module_recheck_delta,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_preparation import (
    _review_evidence_by_ids,
    _structure_report,
)
from manyselves.capabilities.distribution_reporting.runtime.review_preflight import (
    evaluate_module_review_preflight,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def _context(value: Any) -> DeclarativeModuleRuntimeLaneContext:
    if isinstance(value, DeclarativeModuleRuntimeLaneContext):
        return value
    return DeclarativeModuleRuntimeLaneContext.model_validate(value)


def _acceptance(context: DeclarativeModuleRuntimeLaneContext):
    review = context.review
    return review.acceptance if review is not None else None


def _pending(context: DeclarativeModuleRuntimeLaneContext) -> list[ModuleReviewFinding]:
    acceptance = _acceptance(context)
    if isinstance(acceptance, (ModuleInitialReviewAcceptance, ModuleRecheckAcceptance)):
        return list(acceptance.findings)
    return []


def _load_progress(store: ReportingStore, progress_ref: str) -> ModuleReviewProgress:
    path = store.workspace / progress_ref
    if not path.is_file():
        raise ValueError(f"module recheck progress is missing: {progress_ref}")
    return ModuleReviewProgress.model_validate_json(path.read_text(encoding="utf-8"))


def prepare_current_module_recheck(
    value: DeclarativeModuleRuntimeLaneContext,
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Prepare the same Auditor identity for a typed recheck turn."""

    context = _context(value)
    current = context.module
    pending = _pending(context)
    if current is None or not pending:
        return context
    if context.review is None:
        raise ValueError("module recheck requires the existing module review identity")
    initial_prepared = context.review.prepared
    run_id = str(context.reporting_state["run_id"])
    progress_ref = initial_prepared.progress_ref
    progress = _load_progress(store, progress_ref)
    if progress.run_id != run_id or progress.module_id != context.module_id:
        raise ValueError("module recheck progress identity changed")
    if progress.next_action == "completed":
        return context
    baseline_ref = progress.last_reviewed_subject_ref
    if baseline_ref is None:
        baseline_ref = initial_prepared.subject_ref
    if baseline_ref is None:
        raise ValueError("module recheck baseline identity is missing")
    baseline_path = store.workspace / baseline_ref
    if not baseline_path.is_file():
        raise ValueError(f"module recheck baseline is missing: {baseline_ref}")
    baseline = type(current).model_validate_json(baseline_path.read_text(encoding="utf-8"))
    review_round = (
        progress.review_round
        if progress.next_action == "review" and progress.phase == "recheck"
        else progress.review_round + 1
    )
    review_root = initial_prepared.review_root
    lifecycle_id = initial_prepared.lifecycle_id
    subject_ref = f"Work/runs/{run_id}/modules/{context.module_id}-r{current.revision}.json"
    if not (store.workspace / subject_ref).is_file():
        store.write_json(subject_ref, current.model_dump(mode="json"))
    scope = {finding.target_submodule_id for finding in pending}
    delta = build_module_recheck_delta(baseline, current, scope)
    revision_diff = delta["revision_diff"]
    diff_ref = f"{review_root}/recheck-diff-r{review_round}.json"
    input_ref = f"{review_root}/input-r{review_round}.json"
    validation_ref, structure_report = _structure_report(
        store,
        run_id=run_id,
        subject=current,
        subject_ref=subject_ref,
        phase=f"review-r{review_round}",
    )
    preflight = evaluate_module_review_preflight(
        store.workspace,
        run_id=run_id,
        subject=current,
        subject_ref=subject_ref,
        upstream_report=structure_report,
    )
    preflight_ref = (
        f"{review_root}/preflight-subject-r{current.revision}-review-r{review_round}.json"
    )
    store.write_json(preflight_ref, preflight.report.model_dump(mode="json"))
    previous_preflight_progress = (
        context.recheck.prepared.preflight_progress
        if context.recheck is not None
        else None
    )
    current_preflight_progress = (
        ModuleReviewPreflightProgress(current=current)
        if previous_preflight_progress is None
        else previous_preflight_progress.model_copy(update={"current": current})
    )
    if not preflight.report.passed:
        current_preflight_progress = advance_module_review_preflight_progress(
            current=current,
            report=preflight.report,
            previous=previous_preflight_progress,
            validation_ref=preflight_ref,
        )
        reviewer_session_key = (
            progress.reviewer_session_key
            or f"module-auditor-{context.module_id}"
        )
        prepared = ModuleRecheckPreparation(
            mode="preflight_revision",
            run_id=run_id,
            module_id=context.module_id,
            lifecycle_id=lifecycle_id,
            workflow_id=context.workflow_id,
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
            resolved_ids=list(progress.resolved_ids),
            last_reviewed_subject_ref=baseline_ref,
            subject_ref=subject_ref,
            progress=progress,
            validation_ref=preflight_ref,
            validation_target_submodule_ids=sorted(
                preflight.target_submodule_ids
            ),
            preflight_progress=current_preflight_progress,
        )
        review = context.review
        if review is None:
            raise ValueError(
                "module recheck requires the existing module review identity"
            )
        return context.model_copy(
            deep=True,
            update={
                "status": "preflight_revision_pending",
                "recheck": DeclarativeModuleRecheckPreparation(prepared=prepared),
                "error": None,
            },
        )
    active_evidence_ids = set(delta["relevant_evidence_ids"])
    active_evidence_ids.update(
        evidence_ref
        for finding in pending
        for evidence_ref in finding.evidence_refs
        if evidence_ref.startswith("E-")
    )
    review_input = ModuleReviewInput(
        report_instruction=report_instruction_from_state(context.reporting_state),
        input_changes=context.reporting_state.get("revision_input_changes"),
        phase="recheck",
        run_id=run_id,
        module_id=context.module_id,
        lifecycle_id=lifecycle_id,
        review_round=review_round,
        subject_ref=subject_ref,
        subject_revision=current.revision,
        subject=delta["subject"],
        claim_statements=delta["claim_statements"],
        prior_claim_statements=delta["prior_claim_statements"],
        unchanged_submodule_sha256=delta["unchanged_submodule_sha256"],
        unchanged_statement_sha256=delta["unchanged_statement_sha256"],
        evidence=_review_evidence_by_ids(
            store.workspace,
            run_id,
            active_evidence_ids,
        ),
        required_submodule_ids=sorted(scope),
        required_findings=pending,
        revision_responses=list(current.revision_responses),
        baseline_subject_ref=baseline_ref,
        revision_diff_ref=diff_ref,
        revision_diff=revision_diff,
        validation_report_ref=preflight_ref,
        validation_report=preflight.report,
    )
    store.write_json(diff_ref, revision_diff.model_dump(mode="json"))
    store.write_json(input_ref, review_input.model_dump(mode="json"))
    reviewer_session_key = progress.reviewer_session_key or f"module-auditor-{context.module_id}"
    envelope = TaskEnvelope(
        task_id=(
            f"module-{context.module_id}-{lifecycle_id}-review-r{review_round}"
        ),
        run_id=run_id,
        agent_id="evidence-auditor",
        objective=f"只对模块 {context.module_id} 的 required_findings 返回逐项 verdict，并检查修改回归。",
        input_refs=[input_ref],
        constraints=[
            "coverage 记录实际检查范围，不是批准状态",
            "finding 首次提出后不可改写；复审不得复述旧 finding",
            "advisory 与 blocking 都必须获得作者响应和 reviewer verdict",
            "verdicts 必须逐项且仅覆盖 required_findings；new_findings 只允许真实回归",
            "finding id 由既有 review round 分配，审查员不得新造既有 finding id",
        ],
        allowed_outputs=["module_review_verdict_submission"],
        revision=review_round,
        prior_result_ref=(
            progress.finding_refs[-1] if progress.finding_refs else None
        ),
        artifact_delivery_modes={input_ref: "inline"},
        target_submodule_ids=sorted({finding.target_submodule_id for finding in pending}),
        input_contract_kind="module_review_input",
        input_contract_ref=input_ref,
        inline_context=None,
        allowed_tools=["submit_result"],
    )
    prepared = ModuleRecheckPreparation(
        mode="invoke_agent",
        run_id=run_id,
        module_id=context.module_id,
        lifecycle_id=lifecycle_id,
        workflow_id=context.workflow_id,
        reviewer_session_key=reviewer_session_key,
        review_root=review_root,
        progress_ref=progress_ref,
        review_round=review_round,
        scope=sorted({finding.target_submodule_id for finding in pending}),
        current=current,
        pending=pending,
        responses=list(current.revision_responses),
        finding_refs=list(progress.finding_refs),
        verdict_refs=list(progress.verdict_refs),
        resolved_ids=list(progress.resolved_ids),
        last_reviewed_subject_ref=baseline_ref,
        subject_ref=subject_ref,
        review_input_ref=input_ref,
        review_input=review_input,
        envelope=envelope,
        progress=progress,
        preflight_progress=current_preflight_progress,
    )
    _save_progress(
        store,
        preparation=prepared,
        current=current,
        pending=pending,
        finding_refs=list(progress.finding_refs),
        verdict_refs=list(progress.verdict_refs),
        resolved_ids=set(progress.resolved_ids),
        next_action="review",
    )
    review = context.review
    if review is None:
        raise ValueError("module recheck requires the existing module review identity")
    return context.model_copy(
        deep=True,
        update={
            "status": "recheck_ready",
            "recheck": DeclarativeModuleRecheckPreparation(prepared=prepared),
            "error": None,
        },
    )


def _validate_verdicts(
    verdicts: list[ResolutionVerdict],
    required_ids: set[str],
) -> None:
    ids = [verdict.finding_id for verdict in verdicts]
    if len(ids) != len(set(ids)) or set(ids) != required_ids:
        raise ValueError("module recheck verdicts must cover each required finding exactly once")


def _validate_new_findings(
    findings: list[ModuleReviewFinding],
    *,
    module_id: str,
    lifecycle_id: str,
    review_round: int,
    scope: set[str],
    existing_ids: set[str],
) -> None:
    prefix = f"M-{module_id}-{lifecycle_id}-r{review_round}-"
    ids = [finding.id for finding in findings]
    if len(ids) != len(set(ids)):
        raise ValueError("new module review findings contain duplicate ids")
    for finding in findings:
        if finding.id in existing_ids:
            raise ValueError(f"new module finding reuses an existing id: {finding.id}")
        if not finding.id.startswith(prefix):
            raise ValueError(f"new module finding id must start with {prefix}: {finding.id}")
        if finding.target_submodule_id not in scope:
            raise ValueError(f"new module finding is outside the recheck scope: {finding.id}")


def _save_progress(
    store: ReportingStore,
    *,
    preparation: ModuleRecheckPreparation,
    current,
    pending: list[ModuleReviewFinding],
    finding_refs: list[str],
    verdict_refs: list[str],
    resolved_ids: set[str],
    next_action: str,
) -> None:
    store.write_json(
        preparation.progress_ref,
        ModuleReviewProgress(
            run_id=preparation.run_id,
            module_id=preparation.module_id,
            next_action=next_action,
            current=current,
            pending=pending,
            responses=list(current.revision_responses),
            finding_refs=finding_refs,
            verdict_refs=verdict_refs,
            resolved_ids=sorted(resolved_ids),
            review_round=preparation.review_round,
            phase="recheck",
            scope=preparation.scope,
            reviewer_session_key=preparation.reviewer_session_key,
            last_reviewed_subject_ref=preparation.subject_ref,
            review_protocol_version=2,
        ).model_dump(mode="json"),
    )


def _completion(
    store: ReportingStore,
    *,
    preparation: ModuleRecheckPreparation,
    finding_refs: list[str],
    verdict_refs: list[str],
    resolved_ids: set[str],
) -> str:
    completion = ReviewCompletionRecord(
        review_protocol_version=2,
        lifecycle="module",
        run_id=preparation.run_id,
        reviewer_agent_id="evidence-auditor",
        reviewer_session_key=preparation.reviewer_session_key,
        subject_refs=[preparation.subject_ref or ""],
        finding_refs=finding_refs,
        verdict_refs=verdict_refs,
        resolved_finding_ids=sorted(resolved_ids),
    )
    ref = (
        f"{preparation.review_root}/completion-r{preparation.current.revision}.json"
    )
    _write_immutable_json(store, ref, completion.model_dump(mode="json"))
    store.write_text(
        f"Outputs/Modules/{preparation.module_id}.md",
        preparation.current.markdown,
    )
    return ref


def accept_current_module_recheck(
    value: Mapping[str, Any],
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Persist verdicts and expose either the next revision or completion."""

    context = _context(value["context"])
    result = DeclarativeModuleRecheckAgentResult.model_validate(value["result"])
    if result.status == "failed":
        return context.model_copy(
            deep=True,
            update={"status": "failed", "error": result.error or "module recheck failed"},
        )
    if context.recheck is None or result.submission is None:
        raise ValueError("module recheck result lacks its prepared boundary")
    preparation = context.recheck.prepared
    submission = ModuleReviewVerdictSubmission.model_validate(result.submission)
    required_ids = {finding.id for finding in preparation.pending}
    _validate_verdicts(submission.verdicts, required_ids)
    _validate_new_findings(
        submission.new_findings,
        module_id=preparation.module_id,
        lifecycle_id=preparation.lifecycle_id,
        review_round=preparation.review_round,
        scope=set(preparation.scope),
        existing_ids={*required_ids, *preparation.resolved_ids},
    )
    verdict_ref = f"{preparation.review_root}/verdicts-r{preparation.review_round}.json"
    _write_immutable_json(store, verdict_ref, submission.model_dump(mode="json"))
    verdict_refs = [*preparation.verdict_refs, verdict_ref]
    pending_by_id = {finding.id: finding for finding in preparation.pending}
    next_pending = {
        verdict.finding_id: pending_by_id[verdict.finding_id]
        for verdict in submission.verdicts
        if verdict.verdict != "resolved"
    }
    resolved_ids = set(preparation.resolved_ids)
    resolved_ids.update(
        verdict.finding_id
        for verdict in submission.verdicts
        if verdict.verdict == "resolved"
    )
    finding_refs = list(preparation.finding_refs)
    if submission.new_findings:
        finding_ref = f"{preparation.review_root}/regression-findings-r{preparation.review_round}.json"
        _write_immutable_json(
            store,
            finding_ref,
            {
                "kind": "module_review_finding_submission",
                "coverage": submission.coverage.model_dump(mode="json"),
                "findings": [finding.model_dump(mode="json") for finding in submission.new_findings],
            },
        )
        finding_refs.append(finding_ref)
        next_pending.update({finding.id: finding for finding in submission.new_findings})
    if next_pending:
        _save_progress(
            store,
            preparation=preparation,
            current=preparation.current,
            pending=list(next_pending.values()),
            finding_refs=finding_refs,
            verdict_refs=verdict_refs,
            resolved_ids=resolved_ids,
            next_action="revise",
        )
        acceptance = ModuleRecheckAcceptance(
            run_id=preparation.run_id,
            module_id=preparation.module_id,
            lifecycle_id=preparation.lifecycle_id,
            reviewer_session_key=preparation.reviewer_session_key,
            subject_ref=preparation.subject_ref or "",
            current=preparation.current,
            findings=list(next_pending.values()),
            finding_refs=finding_refs,
            verdict_refs=verdict_refs,
            resolved_ids=sorted(resolved_ids),
            next_action="continue_existing",
            progress_ref=preparation.progress_ref,
            completion_ref=None,
        )
        review = context.review
        if review is None:
            raise ValueError("module recheck lacks the existing review")
        return context.model_copy(
            deep=True,
            update={
                "status": "reviewed",
                "review": review.model_copy(update={"acceptance": acceptance}),
                "recheck": context.recheck.model_copy(update={"acceptance": acceptance}),
                "error": None,
            },
        )

    completion_ref = _completion(
        store,
        preparation=preparation,
        finding_refs=finding_refs,
        verdict_refs=verdict_refs,
        resolved_ids=resolved_ids,
    )
    _save_progress(
        store,
        preparation=preparation,
        current=preparation.current,
        pending=[],
        finding_refs=finding_refs,
        verdict_refs=verdict_refs,
        resolved_ids=resolved_ids,
        next_action="completed",
    )
    context.reporting_state.setdefault("module_review_completion_refs", {})[
        preparation.module_id
    ] = completion_ref
    acceptance = ModuleRecheckAcceptance(
        run_id=preparation.run_id,
        module_id=preparation.module_id,
        lifecycle_id=preparation.lifecycle_id,
        reviewer_session_key=preparation.reviewer_session_key,
        subject_ref=preparation.subject_ref or "",
        current=preparation.current,
        findings=[],
        finding_refs=finding_refs,
        verdict_refs=verdict_refs,
        resolved_ids=sorted(resolved_ids),
        next_action="completed",
        progress_ref=preparation.progress_ref,
        completion_ref=completion_ref,
    )
    review = context.review
    if review is None:
        raise ValueError("module recheck lacks the existing review")
    return context.model_copy(
        deep=True,
        update={
            "status": "reviewed",
            "review": review.model_copy(update={"acceptance": acceptance}),
            "recheck": context.recheck.model_copy(update={"acceptance": acceptance}),
            "error": None,
        },
    )


def module_recheck_requires_agent(
    value: DeclarativeModuleRuntimeLaneContext,
) -> bool:
    context = _context(value)
    return bool(
        context.recheck is not None
        and context.recheck.prepared.mode == "invoke_agent"
        and context.recheck.acceptance is None
    )


__all__ = [
    "accept_current_module_recheck",
    "module_recheck_requires_agent",
    "prepare_current_module_recheck",
]
