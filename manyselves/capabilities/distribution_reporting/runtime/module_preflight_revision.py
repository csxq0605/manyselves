"""Capability-owned module machine-preflight revision boundary.

This module mechanically preserves the existing Reporting preflight correction
flow.  It prepares one typed Author revision, accepts the result, and restores
the existing module-review progress for the next file-defined action.  Provider
execution and workflow routing remain owned by the generic runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models.agentic import ModuleRevisionSubmission, ModuleSubmission
from .models.inputs import ValidationReport
from .models.module_lane import (
    DeclarativeModuleRevisionAgentResult,
    DeclarativeModuleRevisionPreparation,
    DeclarativeModuleRuntimeLaneContext,
)
from .models.review import (
    ModuleInitialReviewPreparation,
    ModuleRecheckPreparation,
    ModuleReviewPreflightProgress,
    ModuleReviewProgress,
)
from .module_revision_tools import accept_module_revision, prepare_module_revision
from .storage import ReportingStore
from .user_supplements import request_user_supplements


def _context(value: Any) -> DeclarativeModuleRuntimeLaneContext:
    if isinstance(value, DeclarativeModuleRuntimeLaneContext):
        return value
    return DeclarativeModuleRuntimeLaneContext.model_validate(value)


def require_validation_binding(
    report: ValidationReport,
    subject_ref: str,
    subject_revision: int,
) -> None:
    """Preserve the existing typed validation-subject identity check."""

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
        raise ValueError(
            "validation report does not match the final subject identity; "
            f"subject_ref={subject_ref}; subject_revision={subject_revision}"
        )


def advance_module_review_preflight_progress(
    *,
    current: ModuleSubmission,
    report: ValidationReport,
    previous: ModuleReviewPreflightProgress | None,
    validation_ref: str,
) -> ModuleReviewPreflightProgress:
    """Carry forward the established repeated/total preflight stop semantics."""

    progress = (
        ModuleReviewPreflightProgress(current=current)
        if previous is None
        else previous.model_copy(update={"current": current})
    )
    failure_signature = tuple(
        sorted(
            (failure.check_id, failure.target_path, failure.message)
            for failure in report.failures
        )
    )
    failure_signatures = [*progress.failure_signatures, failure_signature]
    attempts = progress.attempts + 1
    progressed = progress.model_copy(
        update={
            "attempts": attempts,
            "failure_signatures": failure_signatures,
        }
    )
    if failure_signatures.count(failure_signature) >= 2 or attempts >= 3:
        raise ValueError(
            "module preflight failed repeatedly before semantic review; "
            "no reviewer finding or verdict was created. "
            f"module={current.module_id}; attempts={attempts}; "
            f"validation_ref={validation_ref}"
        )
    return progressed


def _prepared_boundary(
    context: DeclarativeModuleRuntimeLaneContext,
) -> tuple[ModuleInitialReviewPreparation | ModuleRecheckPreparation, bool]:
    if context.recheck is not None and context.recheck.prepared.mode == "preflight_revision":
        return context.recheck.prepared, True
    if context.review is None or context.review.prepared.mode != "preflight_revision":
        raise ValueError("module preflight revision has no prepared review boundary")
    return context.review.prepared, False


async def prepare_current_module_preflight_revision(
    value: Any,
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Prepare the current Author correction from a typed preflight failure."""

    context = _context(value)
    if context.status != "preflight_revision_pending":
        return context
    prepared, is_recheck = _prepared_boundary(context)
    revision = await prepare_module_revision(
        workspace=store.workspace,
        store=store,
        state=context.reporting_state,
        workflow_id=prepared.workflow_id,
        subject=prepared.current,
        module_findings=list(prepared.pending) if is_recheck else [],
        validation_ref=prepared.validation_ref,
        validation_target_submodule_ids=set(
            prepared.validation_target_submodule_ids
        ),
        user_supplements=request_user_supplements(
            context.reporting_state.get("request")
        ),
        validate_validation_binding=require_validation_binding,
    )
    return context.model_copy(
        deep=True,
        update={
            "status": "preflight_revision_ready",
            "revision": DeclarativeModuleRevisionPreparation(prepared=revision),
            "error": None,
        },
    )


def _record_initial_progress(
    store: ReportingStore,
    *,
    prepared: ModuleInitialReviewPreparation,
    current: ModuleSubmission,
) -> None:
    store.write_json(
        prepared.progress_ref,
        ModuleReviewProgress(
            run_id=prepared.run_id,
            module_id=prepared.module_id,
            next_action="review",
            current=current,
            pending=[],
            responses=[],
            finding_refs=[],
            verdict_refs=[],
            resolved_ids=[],
            review_round=prepared.review_round,
            phase=prepared.phase,
            scope=list(prepared.scope),
            reviewer_session_key=prepared.reviewer_session_key,
            last_reviewed_subject_ref=None,
            review_protocol_version=2,
        ).model_dump(mode="json"),
    )


def _record_recheck_progress(
    store: ReportingStore,
    *,
    prepared: ModuleRecheckPreparation,
    current: ModuleSubmission,
) -> None:
    store.write_json(
        prepared.progress_ref,
        ModuleReviewProgress(
            run_id=prepared.run_id,
            module_id=prepared.module_id,
            next_action="review",
            current=current,
            pending=list(prepared.pending),
            responses=list(current.revision_responses),
            finding_refs=list(prepared.finding_refs),
            verdict_refs=list(prepared.verdict_refs),
            resolved_ids=list(prepared.resolved_ids),
            review_round=prepared.review_round,
            phase="recheck",
            scope=list(prepared.scope),
            reviewer_session_key=prepared.reviewer_session_key,
            last_reviewed_subject_ref=prepared.last_reviewed_subject_ref,
            review_protocol_version=2,
        ).model_dump(mode="json"),
    )


def accept_current_module_preflight_revision(
    value: Mapping[str, Any],
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Accept one typed preflight correction and resume review preparation."""

    context = _context(value["context"])
    result = DeclarativeModuleRevisionAgentResult.model_validate(value["result"])
    if result.status == "failed":
        return context.model_copy(
            deep=True,
            update={
                "status": "failed",
                "error": result.error or "module preflight revision failed",
            },
        )
    if context.revision is None or result.submission is None:
        raise ValueError("module preflight revision result lacks its prepared boundary")
    prepared, is_recheck = _prepared_boundary(context)
    revised, _subject_ref = accept_module_revision(
        workspace=store.workspace,
        store=store,
        preparation=context.revision.prepared,
        result=ModuleRevisionSubmission.model_validate(result.submission),
    )
    if is_recheck:
        _record_recheck_progress(store, prepared=prepared, current=revised)
        next_status = "recheck_pending"
    else:
        _record_initial_progress(store, prepared=prepared, current=revised)
        next_status = "authored"
    return context.model_copy(
        deep=True,
        update={
            "status": next_status,
            "module": revised,
            "revision": None,
            "error": None,
        },
    )


__all__ = [
    "accept_current_module_preflight_revision",
    "advance_module_review_preflight_progress",
    "prepare_current_module_preflight_revision",
    "require_validation_binding",
]
