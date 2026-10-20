"""Capability-owned module Auditor recheck boundary.

This slice keeps the existing typed finding/verdict, bounded delta, and
progress artifacts.  It does not introduce provider recovery, locks, or a new
decision layer; the file Workflow remains responsible for routing the returned
acceptance.
"""

from __future__ import annotations

import json
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
from manyselves.capabilities.distribution_reporting.runtime.module_review_batches import (
    InputBudget,
    batch_index_from,
    batch_items,
    batch_ref,
    current_batch_needs_agent,
    mark_done,
    merge_verdict_results,
    plan_batches,
    restore_full,
    select_batch,
    write_batch_result,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_delta import (
    build_module_recheck_delta,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_preparation import (
    _review_evidence,
    _review_evidence_by_ids,
    _review_knowledge,
    _structure_report,
    _template_skill_context,
)
from manyselves.capabilities.distribution_reporting.runtime.review_preflight import (
    evaluate_module_review_preflight,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore

from .assessment_context import module_rule_source_refs


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
    input_budget: InputBudget = None,
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
    def build_input(part: set[str], index: int | None = None) -> ModuleReviewInput:
        part_delta = (
            delta if part == scope
            else build_module_recheck_delta(baseline, current, scope, view=part)
        )
        part_pending = [finding for finding in pending if finding.target_submodule_id in part]
        active_evidence_ids = set(part_delta["relevant_evidence_ids"])
        active_evidence_ids.update(
            evidence_ref
            for finding in part_pending
            for evidence_ref in finding.evidence_refs
            if evidence_ref.startswith("E-")
        )
        # Include uncited scope facts as well as old/new/finding references. A
        # persistent reviewer identity does not guarantee visible earlier messages.
        scope_evidence = _review_evidence(store.workspace, current, run_id, part)
        active_evidence_ids.difference_update(item.evidence_id for item in scope_evidence)
        knowledge_ref, knowledge_context = _review_knowledge(
            store.workspace, context.reporting_state, context.module_id, part,
        )
        return ModuleReviewInput(
            report_instruction=report_instruction_from_state(context.reporting_state),
            input_changes=context.reporting_state.get("revision_input_changes"),
            phase="recheck",
            run_id=run_id,
            module_id=context.module_id,
            lifecycle_id=lifecycle_id,
            review_round=review_round,
            subject_ref=subject_ref,
            subject_revision=current.revision,
            subject=part_delta["subject"],
            claim_statements=part_delta["claim_statements"],
            prior_claim_statements=part_delta["prior_claim_statements"],
            unchanged_submodule_sha256=part_delta["unchanged_submodule_sha256"],
            unchanged_statement_sha256=part_delta["unchanged_statement_sha256"],
            knowledge_ref=knowledge_ref,
            knowledge_context=knowledge_context,
            evidence=scope_evidence + _review_evidence_by_ids(
                store.workspace,
                run_id,
                active_evidence_ids,
                current_claims=[claim for claim in current.claims if claim.submodule_id in part],
            ),
            required_submodule_ids=sorted(part),
            required_findings=part_pending,
            revision_responses=[
                response for response in current.revision_responses
                if response.finding_id in {finding.id for finding in part_pending}
            ],
            baseline_subject_ref=baseline_ref,
            revision_diff_ref=diff_ref if index is None else batch_ref(diff_ref, index),
            revision_diff=part_delta["revision_diff"],
            validation_report_ref=preflight_ref,
            validation_report=preflight.report,
        )

    def write_batch_diff(index: int, part: set[str]) -> None:
        store.write_json(
            batch_ref(diff_ref, index),
            build_module_recheck_delta(
                baseline, current, scope, view=part,
            )["revision_diff"].model_dump(mode="json"),
        )

    review_input = build_input(scope)
    store.write_json(diff_ref, revision_diff.model_dump(mode="json"))
    store.write_json(input_ref, review_input.model_dump(mode="json"))
    batches = plan_batches(
        store,
        module_id=context.module_id,
        scope=scope,
        full_input=review_input,
        build_input=build_input,
        input_ref=input_ref,
        result_ref=f"{review_root}/verdicts-r{review_round}.json",
        budget=input_budget,
        before_write=write_batch_diff,
    )
    reviewer_session_key = progress.reviewer_session_key or f"module-auditor-{context.module_id}"
    rule_refs = sorted(set(module_rule_source_refs(current, scope)) | set(module_rule_source_refs(baseline, scope)))
    envelope = TaskEnvelope(
        task_id=(
            f"module-{context.module_id}-{lifecycle_id}-review-r{review_round}"
        ),
        run_id=run_id,
        agent_id="evidence-auditor",
        objective=f"只对模块 {context.module_id} 的 required_findings 返回逐项 verdict，并检查修改回归。",
        input_refs=[input_ref, *rule_refs],
        constraints=[
            "coverage 记录实际检查范围，不是批准状态",
            "finding 首次提出后不可改写；复审不得复述旧 finding",
            (
                "blocking 必须修订并复审；新增 advisory 为可选建议，留档但不自动返工或标记 resolved"
                if progress.review_protocol_version >= 3
                else "当前已持久化审计的 advisory 与 blocking 都必须获得作者响应和 reviewer verdict"
            ),
            "verdicts 必须逐项且仅覆盖 required_findings；new_findings 只允许真实回归",
            "finding id 由既有 review round 分配，审查员不得新造既有 finding id",
        ],
        allowed_outputs=["module_review_verdict_submission"],
        revision=review_round,
        prior_result_ref=(
            progress.finding_refs[-1] if progress.finding_refs else None
        ),
        artifact_delivery_modes={input_ref: "inline", **{ref: "reference" for ref in rule_refs}},
        target_submodule_ids=sorted({finding.target_submodule_id for finding in pending}),
        input_contract_kind="module_review_input",
        input_contract_ref=input_ref,
        inline_context=_template_skill_context(
            store.workspace, context.reporting_state, context.module_id,
        ),
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
        batches=batches,
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
            review_protocol_version=preparation.review_protocol_version,
        ).model_dump(mode="json"),
    )


def _completion(
    store: ReportingStore,
    *,
    preparation: ModuleRecheckPreparation,
    finding_refs: list[str],
    verdict_refs: list[str],
    resolved_ids: set[str],
    open_ids: set[str] = frozenset(),
) -> str:
    completion = ReviewCompletionRecord(
        review_protocol_version=preparation.review_protocol_version,
        lifecycle="module",
        run_id=preparation.run_id,
        reviewer_agent_id="evidence-auditor",
        reviewer_session_key=preparation.reviewer_session_key,
        subject_refs=[preparation.subject_ref or ""],
        finding_refs=finding_refs,
        verdict_refs=verdict_refs,
        resolved_finding_ids=sorted(resolved_ids),
        deferred_advisory_ids=(
            sorted(
                finding["id"]
                for ref in finding_refs
                for finding in json.loads(
                    (store.workspace / ref).read_text(encoding="utf-8")
                )["findings"]
                if finding["impact"] == "advisory" and finding["id"] not in resolved_ids
                and finding["id"] not in open_ids
            )
            if preparation.review_protocol_version >= 3
            else []
        ),
        open_at_round_limit_ids=sorted(open_ids),
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


DEFAULT_MAX_MODULE_RECHECK_ROUNDS = 2


def _max_recheck_rounds(context: DeclarativeModuleRuntimeLaneContext) -> int:
    value = context.reporting_state.get(
        "max_module_recheck_rounds", DEFAULT_MAX_MODULE_RECHECK_ROUNDS,
    )
    return max(1, int(value))


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
    if preparation.batch_index is not None:
        return _accept_recheck_batch(context, submission, store=store)
    return _record_recheck(context, submission, store=store)


def _accept_recheck_batch(
    context: DeclarativeModuleRuntimeLaneContext,
    submission: ModuleReviewVerdictSubmission,
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Persist one submodule batch of the recheck."""

    recheck = context.recheck
    preparation = recheck.prepared
    index = int(preparation.batch_index)
    batch = preparation.batches[index]
    scope = set(batch.submodule_ids)
    required_ids = {
        finding.id for finding in preparation.pending if finding.target_submodule_id in scope
    }
    _validate_verdicts(submission.verdicts, required_ids)
    _validate_new_findings(
        submission.new_findings,
        module_id=preparation.module_id,
        lifecycle_id=preparation.lifecycle_id,
        review_round=preparation.review_round,
        scope=scope,
        existing_ids={*(finding.id for finding in preparation.pending), *preparation.resolved_ids},
    )
    write_batch_result(store, batch, submission.model_dump(mode="json"))
    return context.model_copy(
        deep=True,
        update={
            "recheck": recheck.model_copy(update={"prepared": mark_done(preparation, index)}),
            "error": None,
        },
    )


def list_current_module_recheck_batches(value: Any) -> list[dict[str, int]]:
    """Positions the recheck loop visits, in submodule order."""

    context = _context(value)
    return batch_items(context.recheck.prepared if context.recheck is not None else None)


def select_current_module_recheck_batch(
    value: Mapping[str, Any],
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Point the recheck turn at one submodule batch."""

    context = _context(value["context"])
    recheck = context.recheck
    if (
        recheck is None
        or not recheck.prepared.batches
        or recheck.acceptance is not None
        or context.status == "failed"
    ):
        return context
    prepared = select_batch(
        store,
        recheck.prepared,
        batch_index_from(value),
        target_submodule_ids=lambda batch: list(batch.submodule_ids),
    )
    return context.model_copy(
        deep=True,
        update={"recheck": recheck.model_copy(update={"prepared": prepared})},
    )


def merge_current_module_recheck_batches(
    value: Any,
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Record the recheck once every submodule batch has a result."""

    context = _context(value)
    recheck = context.recheck
    if (
        recheck is None
        or not recheck.prepared.batches
        or recheck.acceptance is not None
        or context.status == "failed"
    ):
        return context
    missing = [batch.index + 1 for batch in recheck.prepared.batches if not batch.done]
    if missing:
        raise ValueError(f"module recheck batches without a result: {missing}")
    preparation = restore_full(store, recheck.prepared)
    merged = merge_verdict_results(
        store,
        preparation.batches,
        prefix=(
            f"M-{preparation.module_id}-{preparation.lifecycle_id}-r{preparation.review_round}-"
        ),
        scope=list(preparation.scope),
    )
    restored = context.model_copy(
        deep=True,
        update={"recheck": recheck.model_copy(update={"prepared": preparation})},
    )
    return _record_recheck(restored, merged, store=store)


def _record_recheck(
    context: DeclarativeModuleRuntimeLaneContext,
    submission: ModuleReviewVerdictSubmission,
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    preparation = context.recheck.prepared
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
        next_pending.update({
            finding.id: finding for finding in submission.new_findings
            if preparation.review_protocol_version < 3 or finding.impact == "blocking"
        })
    open_at_limit: set[str] = set()
    if next_pending and preparation.review_round >= _max_recheck_rounds(context):
        # Bounded rework: stop revising, close the lane with the findings
        # recorded as open instead of looping until a model gives in.
        open_at_limit = set(next_pending)
        next_pending = {}
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
        open_ids=open_at_limit,
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
        and context.status != "failed"
        and context.recheck.prepared.mode == "invoke_agent"
        and context.recheck.acceptance is None
        and current_batch_needs_agent(context.recheck.prepared)
    )


__all__ = [
    "accept_current_module_recheck",
    "list_current_module_recheck_batches",
    "merge_current_module_recheck_batches",
    "select_current_module_recheck_batch",
    "module_recheck_requires_agent",
    "prepare_current_module_recheck",
]
