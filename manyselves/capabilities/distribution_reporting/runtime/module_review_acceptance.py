"""Capability-owned acceptance of one initial module review result."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleReviewFindingSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ReviewCompletionRecord,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleReviewAgentResult,
    DeclarativeModuleRuntimeLaneContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    ModuleInitialReviewAcceptance,
    ModuleInitialReviewPreparation,
    ModuleReviewProgress,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_batches import (
    batch_index_from,
    batch_items,
    mark_done,
    merge_finding_results,
    restore_full,
    select_batch,
    write_batch_result,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def _context(value: Any) -> DeclarativeModuleRuntimeLaneContext:
    if isinstance(value, DeclarativeModuleRuntimeLaneContext):
        return value
    return DeclarativeModuleRuntimeLaneContext.model_validate(value)


def _validate_findings(
    submission: ModuleReviewFindingSubmission,
    *,
    module_id: str,
    lifecycle_id: str,
    review_round: int,
    scope: set[str],
) -> None:
    finding_ids = [finding.id for finding in submission.findings]
    if len(finding_ids) != len(set(finding_ids)):
        raise ValueError("module findings contains duplicate ids")
    prefix = f"M-{module_id}-{lifecycle_id}-r{review_round}-"
    for finding in submission.findings:
        if not finding.id.startswith(prefix):
            raise ValueError(f"module finding id must start with {prefix}: {finding.id}")
        if finding.target_submodule_id not in scope:
            raise ValueError(
                f"module finding targets unreviewed submodule: {finding.id}"
            )


def _write_immutable_json(
    store: ReportingStore,
    relative: str,
    payload: dict[str, Any],
) -> None:
    """Reuse an identical findings artifact and reject a changed replacement."""

    path = store.workspace / relative
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"immutable audit artifact is unreadable: {relative}") from exc
        if existing != payload:
            raise ValueError(f"refusing to overwrite immutable audit artifact: {relative}")
        return
    store.write_json(relative, payload)


def _save_progress(
    store: ReportingStore,
    *,
    progress_ref: str,
    run_id: str,
    module_id: str,
    next_action: str,
    current: Any,
    pending: list[Any],
    finding_refs: list[str],
    reviewer_session_key: str,
    review_round: int,
    phase: str,
    scope: set[str],
    subject_ref: str,
    review_protocol_version: int,
) -> None:
    progress = ModuleReviewProgress(
        run_id=run_id,
        module_id=module_id,
        next_action=next_action,
        current=current,
        pending=pending,
        finding_refs=finding_refs,
        verdict_refs=[],
        resolved_ids=[],
        review_round=review_round,
        phase=phase,
        scope=sorted(scope),
        reviewer_session_key=reviewer_session_key,
        last_reviewed_subject_ref=subject_ref,
        review_protocol_version=review_protocol_version,
    )
    store.write_json(progress_ref, progress.model_dump(mode="json"))


def _save_completion(
    store: ReportingStore,
    *,
    run_id: str,
    module_id: str,
    lifecycle_id: str,
    revision: int,
    reviewer_session_key: str,
    subject_ref: str,
    finding_refs: list[str],
    review_protocol_version: int,
    deferred_advisory_ids: list[str],
) -> str:
    completion = ReviewCompletionRecord(
        review_protocol_version=review_protocol_version,
        lifecycle="module",
        run_id=run_id,
        reviewer_agent_id="evidence-auditor",
        reviewer_session_key=reviewer_session_key,
        subject_refs=[subject_ref],
        finding_refs=finding_refs,
        verdict_refs=[],
        resolved_finding_ids=[],
        deferred_advisory_ids=deferred_advisory_ids,
    )
    completion_ref = (
        f"Work/runs/{run_id}/reviews/module/{lifecycle_id}/{module_id}/"
        f"completion-r{revision}.json"
    )
    store.write_json(completion_ref, completion.model_dump(mode="json"))
    return completion_ref


def accept_module_initial_review(
    *,
    preparation: ModuleInitialReviewPreparation,
    submission: ModuleReviewFindingSubmission,
    store: ReportingStore,
) -> ModuleInitialReviewAcceptance:
    """Accept one typed initial or local-regression Auditor submission."""

    prepared = preparation
    if prepared.mode != "invoke_agent":
        raise ValueError("cannot accept an initial module review without an Agent invocation")
    if prepared.subject_ref is None:
        raise ValueError("initial module review lacks a subject ref")
    scope = set(prepared.scope)
    if not scope.issubset(set(submission.coverage.submodule_ids)):
        raise ValueError("initial module review coverage omitted assigned submodules")
    _validate_findings(
        submission,
        module_id=prepared.module_id,
        lifecycle_id=prepared.lifecycle_id,
        review_round=prepared.review_round,
        scope=scope,
    )

    finding_ref = f"{prepared.review_root}/findings-r{prepared.review_round}.json"
    _write_immutable_json(
        store,
        finding_ref,
        submission.model_dump(mode="json"),
    )
    finding_refs = [finding_ref]
    pending = [
        finding for finding in submission.findings
        if prepared.review_protocol_version < 3 or finding.impact == "blocking"
    ]
    phase = (
        prepared.review_input.phase
        if prepared.review_input is not None
        else prepared.phase
    )
    completion_ref: str | None = None
    if pending:
        next_action = "revise"
    else:
        completion_ref = _save_completion(
            store,
            run_id=prepared.run_id,
            module_id=prepared.module_id,
            lifecycle_id=prepared.lifecycle_id,
            revision=prepared.current.revision,
            reviewer_session_key=prepared.reviewer_session_key,
            subject_ref=prepared.subject_ref,
            finding_refs=finding_refs,
            review_protocol_version=prepared.review_protocol_version,
            deferred_advisory_ids=[finding.id for finding in submission.findings],
        )
        store.write_text(
            f"Outputs/Modules/{prepared.module_id}.md",
            prepared.current.markdown,
        )
        next_action = "completed"
    _save_progress(
        store,
        progress_ref=prepared.progress_ref,
        run_id=prepared.run_id,
        module_id=prepared.module_id,
        next_action=next_action,
        current=prepared.current,
        pending=pending,
        finding_refs=finding_refs,
        reviewer_session_key=prepared.reviewer_session_key,
        review_round=prepared.review_round,
        phase=phase,
        scope=scope,
        subject_ref=prepared.subject_ref,
        review_protocol_version=prepared.review_protocol_version,
    )
    return ModuleInitialReviewAcceptance(
        run_id=prepared.run_id,
        module_id=prepared.module_id,
        lifecycle_id=prepared.lifecycle_id,
        reviewer_session_key=prepared.reviewer_session_key,
        subject_ref=prepared.subject_ref,
        current=prepared.current,
        findings=pending,
        finding_refs=finding_refs,
        verdict_refs=[],
        resolved_ids=[],
        next_action=next_action,
        progress_ref=prepared.progress_ref,
        completion_ref=completion_ref,
    )


def accept_current_module_review(
    value: Mapping[str, Any],
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Persist one typed initial review and project its next route."""

    context = _context(value["context"])
    result = DeclarativeModuleReviewAgentResult.model_validate(value["result"])
    if result.status == "failed":
        return context.model_copy(
            deep=True,
            update={
                "status": "failed",
                "error": result.error or "module reviewer failed",
            },
        )
    submission = ModuleReviewFindingSubmission.model_validate(result.submission)
    review = context.review
    prepared = review.prepared
    if prepared.module_id != context.module_id or prepared.current.module_id != context.module_id:
        raise ValueError("module review result does not belong to the current module")
    if prepared.reviewer_session_key != review.reviewer_session_key:
        raise ValueError("module review reviewer session identity changed")
    if prepared.batch_index is not None:
        return _accept_batch(context, submission, store=store)
    return _record_review(context, submission, store=store)


def _accept_batch(
    context: DeclarativeModuleRuntimeLaneContext,
    submission: ModuleReviewFindingSubmission,
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Persist one submodule batch of the initial review."""

    review = context.review
    prepared = review.prepared
    index = int(prepared.batch_index)
    batch = prepared.batches[index]
    scope = set(batch.submodule_ids)
    if not scope.issubset(set(submission.coverage.submodule_ids)):
        raise ValueError("module review batch coverage omitted assigned submodules")
    _validate_findings(
        submission,
        module_id=prepared.module_id,
        lifecycle_id=prepared.lifecycle_id,
        review_round=prepared.review_round,
        scope=scope,
    )
    write_batch_result(store, batch, submission.model_dump(mode="json"))
    return context.model_copy(
        deep=True,
        update={
            "review": review.model_copy(update={"prepared": mark_done(prepared, index)}),
            "error": None,
        },
    )


def list_current_module_review_batches(value: Any) -> list[dict[str, int]]:
    """Positions the initial review loop visits, in submodule order."""

    context = _context(value)
    return batch_items(context.review.prepared if context.review is not None else None)


def select_current_module_review_batch(
    value: Mapping[str, Any],
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Point the initial review turn at one submodule batch."""

    context = _context(value["context"])
    review = context.review
    if (
        review is None
        or not review.prepared.batches
        or review.acceptance is not None
        or context.status == "failed"
    ):
        return context
    prepared = select_batch(
        store,
        review.prepared,
        batch_index_from(value),
        target_submodule_ids=lambda batch: list(batch.submodule_ids),
    )
    return context.model_copy(
        deep=True,
        update={"review": review.model_copy(update={"prepared": prepared, "envelope": prepared.envelope})},
    )


def merge_current_module_review_batches(
    value: Any,
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Record the initial review once every submodule batch has a result."""

    context = _context(value)
    review = context.review
    if review is None or not review.prepared.batches or review.acceptance is not None:
        return context
    if context.status == "failed":
        return context
    missing = [batch.index + 1 for batch in review.prepared.batches if not batch.done]
    if missing:
        raise ValueError(f"module review batches without a result: {missing}")
    prepared = restore_full(store, review.prepared)
    merged = merge_finding_results(
        store,
        prepared.batches,
        prefix=f"M-{prepared.module_id}-{prepared.lifecycle_id}-r{prepared.review_round}-",
        scope=list(prepared.scope),
    )
    restored = context.model_copy(
        deep=True,
        update={"review": review.model_copy(update={"prepared": prepared, "envelope": prepared.envelope})},
    )
    return _record_review(restored, merged, store=store)


def _record_review(
    context: DeclarativeModuleRuntimeLaneContext,
    submission: ModuleReviewFindingSubmission,
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    review = context.review
    prepared = review.prepared
    acceptance = accept_module_initial_review(
        preparation=prepared,
        submission=submission,
        store=store,
    )
    reporting_state = context.reporting_state
    if acceptance.completion_ref is not None:
        reporting_state.setdefault("module_review_completion_refs", {})[
            prepared.module_id
        ] = acceptance.completion_ref
    accepted_review = review.model_copy(update={"acceptance": acceptance})
    return context.model_copy(
        deep=True,
        update={
            "status": "reviewed",
            "review": accepted_review,
            "error": None,
        },
    )


__all__ = [
    "accept_current_module_review",
    "accept_module_initial_review",
    "list_current_module_review_batches",
    "merge_current_module_review_batches",
    "select_current_module_review_batch",
]
