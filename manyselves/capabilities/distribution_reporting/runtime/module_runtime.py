"""Capability-owned composition for the public module workflow runtime.

This module only assembles lifecycle functions and typed state that already
belong to the distribution-reporting Capability.  It does not move provider,
Kernel, or workflow-compilation concerns into the Capability composition.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import Any

from manyselves.kernel.ports import AgentInvoker
from manyselves.runtime.agent_execution import AgentExecutionService, AgentSessionLoop

from .main_exception import accept_main_exception_decision, prepare_main_exception_decision
from .models.agentic import (
    AgentResult,
    AgentRunStatus,
    ModuleSubmission,
    WorkflowDecisionSubmission,
)
from .models.cross_owner import (
    DeclarativeMainExceptionAgentResult,
    DeclarativeMainExceptionUserInput,
)
from .models.module_cohort import DeclarativeModuleLaneOutcome
from .models.module_lane import (
    DeclarativeModuleRecheckAgentResult,
    DeclarativeModuleRuntimeLaneContext,
)
from .models.reporting import ReportRequest
from .models.review import ModuleInitialReviewAcceptance, ModuleRecheckAcceptance
from .module_authoring_preparation import (
    build_module_dispatch,
    prepare_current_module_authoring,
    resume_current_module_authoring,
)
from .module_cohort_tools import complete_current_module_lane, reduce_module_cohort
from .module_lane_tools import (
    accept_current_module_authoring,
    accept_current_module_review,
    module_lane_can_review,
    module_recheck_requires_agent,
    module_review_needs_recheck,
    module_review_needs_revision,
    module_review_preflight_needs_revision,
    module_review_requires_agent,
    prepare_current_module_author_exception,
)
from .module_preflight_revision import (
    accept_current_module_preflight_revision,
    prepare_current_module_preflight_revision,
)
from .module_recheck_tools import (
    accept_current_module_recheck,
    prepare_current_module_recheck,
)
from .module_review_preparation import prepare_current_module_review
from .module_revision_tools import (
    accept_current_module_revision,
    prepare_current_module_revision,
)
from .storage import ReportingStore

SessionFactory = Callable[[str], AgentSessionLoop]


def _context(value: Any) -> DeclarativeModuleRuntimeLaneContext:
    if isinstance(value, DeclarativeModuleRuntimeLaneContext):
        return value
    return DeclarativeModuleRuntimeLaneContext.model_validate(value)


def _prepare_module_lanes(
    value: Any,
    *,
    workspace: Path,
    store: ReportingStore,
    global_root: Path | None = None,
) -> Any:
    """Build the shared dispatch once before the module lanes fan out."""

    if not isinstance(value, Mapping):
        return value
    state = deepcopy(dict(value))
    if state.get("revision_targets"):
        return state
    if state.get("resume"):
        _restore_promoted_module_submissions(state, workspace=workspace)
    if "module_dispatch" not in state:
        build_module_dispatch(
            state,
            workspace=workspace,
            store=store,
            global_root=global_root,
        )
    return state


def _restore_promoted_module_submissions(
    state: dict[str, Any],
    *,
    workspace: Path,
) -> None:
    """Restore completed typed Author outputs before a same-Run lane replay."""

    request = state.get("request")
    if request is None:
        return
    run_id = str(state["run_id"])
    module_ids = ReportRequest.model_validate(request).target_modules
    submissions = state.setdefault("specialist_submissions", {})
    for module_id in module_ids:
        if module_id in submissions:
            continue
        result_path = (
            Path(workspace)
            / "Work"
            / "runs"
            / run_id
            / "results"
            / f"module-{module_id}.json"
        )
        if not result_path.is_file():
            continue
        result = AgentResult.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )
        if result.status is not AgentRunStatus.COMPLETED:
            continue
        submission = ModuleSubmission.model_validate(result.payload)
        if submission.module_id != module_id:
            raise ValueError("promoted module result belongs to another module")
        submissions[module_id] = submission


def _start_module_lane(
    module_id: str,
    reporting_state: Mapping[str, Any],
    workflow_id: str,
    lane_outcome: DeclarativeModuleLaneOutcome | None = None,
) -> DeclarativeModuleRuntimeLaneContext:
    """Project a lane input or joined outcome into Capability lane state."""

    if lane_outcome is not None:
        lane_state = lane_outcome.lane_state or dict(reporting_state)
        saved_context = lane_state.get("lane_context")
        if saved_context is not None and (
            lane_outcome.status == "deferred" or lane_outcome.retry_requested
        ):
            restored = _context(saved_context)
            resumed_state = deepcopy(restored.reporting_state)
            resumed_state["resume"] = True
            return restored.model_copy(
                deep=True,
                update={
                    "reporting_state": resumed_state,
                    "status": restored.resume_status or restored.status,
                    "resume_status": None,
                    "error": None,
                },
            )
        return DeclarativeModuleRuntimeLaneContext(
            module_id=module_id,
            workflow_id=workflow_id,
            reporting_state=deepcopy(dict(lane_state)),
            status=("completed" if lane_outcome.status == "completed" else "failed"),
            module=lane_outcome.module,
            completion_ref=lane_outcome.completion_ref,
            completion=lane_outcome.completion,
            error=lane_outcome.error,
        )

    return DeclarativeModuleRuntimeLaneContext(
        module_id=module_id,
        workflow_id=workflow_id,
        reporting_state=deepcopy(dict(reporting_state)),
        status="ready",
    )


def _author_requires_agent(value: Any) -> bool:
    return _context(value).status == "author_ready"


def _lane_has_deferred_main_exception(value: Any) -> bool:
    return _context(value).status in {
        "author_exception_deferred",
        "reviewer_exception_deferred",
    }


def _lane_retries_preflight_revision(value: Any) -> bool:
    return _context(value).status == "preflight_revision_ready"


def _preflight_revision_needs_recheck(value: Any) -> bool:
    return _context(value).status == "recheck_pending"


def _resume_review_lane(
    value: Any,
) -> DeclarativeModuleRuntimeLaneContext:
    """Re-expose a persisted initial-review turn through its typed envelope."""

    context = _context(value)
    if context.status != "review_resumed":
        return context
    if context.review is None:
        raise ValueError("module review resume requires its prepared review")
    prepared = context.review.prepared
    progress = prepared.progress
    if prepared.mode == "continue_existing" and progress is not None:
        if (
            progress.run_id != context.reporting_state.get("run_id")
            or progress.module_id != context.module_id
        ):
            raise ValueError("module review progress identity mismatch")
        if progress.next_action in {"revise", "completed"}:
            completion_ref = None
            if progress.next_action == "completed":
                completion_ref = context.reporting_state.get(
                    "module_review_completion_refs", {}
                ).get(context.module_id)
                if completion_ref is None:
                    raise ValueError(
                        "persisted module review completion was not restored"
                    )
            acceptance = ModuleInitialReviewAcceptance(
                run_id=prepared.run_id,
                module_id=prepared.module_id,
                lifecycle_id=prepared.lifecycle_id,
                reviewer_session_key=prepared.reviewer_session_key,
                subject_ref=(
                    prepared.subject_ref
                    or f"Work/runs/{progress.run_id}/modules/"
                    f"{context.module_id}-r{progress.current.revision}.json"
                ),
                current=progress.current,
                findings=list(progress.pending),
                finding_refs=list(progress.finding_refs),
                verdict_refs=list(progress.verdict_refs),
                resolved_ids=list(progress.resolved_ids),
                next_action=progress.next_action,
                progress_ref=prepared.progress_ref,
                completion_ref=completion_ref,
            )
            return context.model_copy(
                deep=True,
                update={
                    "status": (
                        "reviewed"
                        if acceptance.next_action == "completed"
                        else "revision_pending"
                    ),
                    "module": acceptance.current,
                    "review": context.review.model_copy(
                        update={"acceptance": acceptance}
                    ),
                    "error": None,
                },
            )
    if (
        progress is None
        or progress.next_action != "review"
        or progress.phase != "initial"
        or context.review.envelope is None
    ):
        raise ValueError("module review resume has no declared review progress")
    return context.model_copy(
        deep=True,
        update={
            "status": "review_ready",
            "module": progress.current,
            "review": context.review.model_copy(
                update={
                    "prepared": prepared.model_copy(
                        update={
                            "mode": "invoke_agent",
                            "current": progress.current,
                        }
                    )
                }
            ),
            "error": None,
        },
    )


def _resume_recheck_lane(
    value: Any,
) -> DeclarativeModuleRuntimeLaneContext:
    """Re-expose a persisted recheck turn through its typed envelope."""

    context = _context(value)
    if context.status != "review_resumed":
        return context
    if context.recheck is None:
        raise ValueError("module recheck resume requires its prepared recheck")
    prepared = context.recheck.prepared
    progress = prepared.progress
    if prepared.mode == "continue_existing" and progress is not None:
        if (
            progress.run_id != context.reporting_state.get("run_id")
            or progress.module_id != context.module_id
        ):
            raise ValueError("module recheck progress identity mismatch")
        if progress.next_action in {"revise", "completed"}:
            completion_ref = None
            if progress.next_action == "completed":
                completion_ref = context.reporting_state.get(
                    "module_review_completion_refs", {}
                ).get(context.module_id)
                if completion_ref is None:
                    raise ValueError(
                        "persisted module recheck completion was not restored"
                    )
            acceptance = ModuleRecheckAcceptance(
                run_id=prepared.run_id,
                module_id=prepared.module_id,
                lifecycle_id=prepared.lifecycle_id,
                reviewer_session_key=prepared.reviewer_session_key,
                subject_ref=(
                    prepared.subject_ref
                    or f"Work/runs/{progress.run_id}/modules/"
                    f"{context.module_id}-r{progress.current.revision}.json"
                ),
                current=progress.current,
                findings=list(progress.pending),
                finding_refs=list(progress.finding_refs),
                verdict_refs=list(progress.verdict_refs),
                resolved_ids=list(progress.resolved_ids),
                next_action=(
                    "completed" if progress.next_action == "completed" else "continue_existing"
                ),
                progress_ref=prepared.progress_ref,
                completion_ref=completion_ref,
            )
            review = context.review
            if review is None:
                raise ValueError("module recheck resume lacks initial review")
            return context.model_copy(
                deep=True,
                update={
                    "status": "reviewed",
                    "module": acceptance.current,
                    "review": review.model_copy(update={"acceptance": acceptance}),
                    "recheck": context.recheck.model_copy(
                        update={"acceptance": acceptance}
                    ),
                    "error": None,
                },
            )
    if (
        progress is None
        or progress.next_action != "review"
        or progress.phase != "recheck"
        or context.recheck.envelope is None
    ):
        raise ValueError("module recheck resume has no declared review progress")
    return context.model_copy(
        deep=True,
        update={
            "status": "recheck_ready",
            "module": progress.current,
            "recheck": context.recheck.model_copy(
                update={
                    "prepared": prepared.model_copy(
                        update={
                            "mode": "invoke_agent",
                            "current": progress.current,
                        }
                    )
                }
            ),
            "error": None,
        },
    )


def _exception_state(
    context: DeclarativeModuleRuntimeLaneContext,
    *,
    preparation: Any,
    acceptance: Any,
    status: str,
) -> DeclarativeModuleRuntimeLaneContext:
    return context.model_copy(
        deep=True,
        update={
            "status": status,
            "main_preparation": preparation,
            "main_acceptance": acceptance,
            "error": None,
        },
    )


def _prepare_main_exception_lane(
    value: Any,
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Prepare the existing typed Main decision for an exceptional lane."""

    context = _context(value)
    if context.status not in {
        "author_exception_deferred",
        "reviewer_exception_deferred",
    }:
        return context
    run_id = str(context.reporting_state["run_id"])
    if context.status == "author_exception_deferred":
        if context.review is None or context.module is None:
            raise ValueError("author exception lacks its review and module state")
        acceptance = context.review.acceptance
        if not isinstance(acceptance, (ModuleInitialReviewAcceptance, ModuleRecheckAcceptance)):
            raise ValueError("author exception lacks accepted review findings")
        responses = [
            response
            for response in context.module.revision_responses
            if response.action in {"disputed", "needs_input"}
        ]
        preparation = prepare_main_exception_decision(
            store=store,
            run_id=run_id,
            workflow_id=context.workflow_id,
            scope="module",
            subject_refs=[
                f"Work/runs/{run_id}/modules/"
                f"{context.module_id}-r{context.module.revision}.json"
            ],
            finding_refs=list(acceptance.finding_refs),
            verdicts=[],
            responses=responses,
            trigger="author_response",
        )
        ready_status = "author_exception_ready"
        resumed_status = "author_exception_resumed"
    else:
        if context.recheck is None or context.recheck.submission is None:
            raise ValueError("reviewer exception lacks its recheck submission")
        prepared_recheck = context.recheck.prepared
        submission = context.recheck.submission
        preparation = prepare_main_exception_decision(
            store=store,
            run_id=run_id,
            workflow_id=context.workflow_id,
            scope="module",
            subject_refs=[prepared_recheck.subject_ref or ""],
            finding_refs=list(prepared_recheck.finding_refs),
            verdicts=[
                verdict
                for verdict in submission.verdicts
                if verdict.verdict == "escalate"
            ],
            responses=list(prepared_recheck.responses),
            trigger="reviewer_escalation",
        )
        ready_status = "reviewer_exception_ready"
        resumed_status = "reviewer_exception_resumed"

    if preparation.mode == "continue_existing":
        decision_state = {
            "review_exception_refs": list(
                context.reporting_state.get("review_exception_refs", [])
            )
        }
        acceptance = accept_main_exception_decision(
            store=store,
            preparation=preparation,
            result=None,
            state=decision_state,
            raise_for_terminal_decisions=False,
        )
        context.reporting_state["review_exception_refs"] = decision_state[
            "review_exception_refs"
        ]
        if acceptance.result.decision == "stop_incomplete":
            return _exception_state(
                context,
                preparation=preparation,
                acceptance=acceptance,
                status="failed",
            ).model_copy(update={"error": acceptance.result.rationale})
        return _exception_state(
            context,
            preparation=preparation,
            acceptance=acceptance,
            status=resumed_status,
        )
    return _exception_state(
        context,
        preparation=preparation,
        acceptance=None,
        status=ready_status,
    )


def _main_exception_requires_agent(value: Any) -> bool:
    return _context(value).status in {
        "author_exception_ready",
        "reviewer_exception_ready",
    }


def _accept_main_exception_lane(
    value: Mapping[str, Any],
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    context = _context(value["context"])
    result = DeclarativeMainExceptionAgentResult.model_validate(value["result"])
    if result.status == "failed":
        return context.model_copy(
            deep=True,
            update={
                "status": "failed",
                "error": result.error or "module Main exception failed",
            },
        )
    if result.submission is None or context.main_preparation is None:
        raise ValueError("module Main exception result lacks its preparation")
    decision_state = {
        "review_exception_refs": list(
            context.reporting_state.get("review_exception_refs", [])
        )
    }
    acceptance = accept_main_exception_decision(
        store=store,
        preparation=context.main_preparation,
        result=result.submission,
        state=decision_state,
        raise_for_terminal_decisions=False,
    )
    context.reporting_state["review_exception_refs"] = decision_state[
        "review_exception_refs"
    ]
    if acceptance.result.decision == "stop_incomplete":
        return context.model_copy(
            deep=True,
            update={
                "status": "failed",
                "main_acceptance": acceptance,
                "error": acceptance.result.rationale,
            },
        )
    return context.model_copy(
        deep=True,
        update={
            "status": (
                "author_exception_accepted"
                if acceptance.trigger == "author_response"
                else "reviewer_exception_accepted"
            ),
            "main_acceptance": acceptance,
            "error": None,
        },
    )


def _main_exception_requests_user(value: Any) -> bool:
    context = _context(value)
    return bool(
        context.status != "failed"
        and context.main_acceptance is not None
        and context.main_acceptance.result.decision == "request_user"
    )


def _apply_main_exception_user_input(
    value: Mapping[str, Any],
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    context = _context(value["context"])
    if context.main_preparation is None:
        raise ValueError("module Main exception user input lacks its preparation")
    supplied = DeclarativeMainExceptionUserInput.model_validate(value["input"])
    decision_state = {
        "review_exception_refs": list(
            context.reporting_state.get("review_exception_refs", [])
        )
    }
    acceptance = accept_main_exception_decision(
        store=store,
        preparation=context.main_preparation,
        result=WorkflowDecisionSubmission(
            decision=supplied.decision,
            rationale=supplied.rationale,
            finding_ids=list(context.main_preparation.exception_ids),
        ),
        state=decision_state,
        raise_for_terminal_decisions=False,
    )
    context.reporting_state["review_exception_refs"] = decision_state[
        "review_exception_refs"
    ]
    if acceptance.result.decision == "stop_incomplete":
        return context.model_copy(
            deep=True,
            update={
                "status": "failed",
                "main_acceptance": acceptance,
                "error": acceptance.result.rationale,
            },
        )
    return context.model_copy(
        deep=True,
        update={
            "status": (
                "author_exception_accepted"
                if context.main_preparation.trigger == "author_response"
                else "reviewer_exception_accepted"
            ),
            "main_acceptance": acceptance,
            "error": None,
        },
    )


def _route_after_main_exception(
    value: Any,
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    context = _context(value)
    acceptance = context.main_acceptance
    if context.status == "failed" or acceptance is None:
        return context
    if acceptance.trigger == "author_response":
        return context.model_copy(
            deep=True,
            update={
                "status": (
                    "revision_pending"
                    if acceptance.result.decision == "return_to_author"
                    else "recheck_pending"
                )
            },
        )
    if context.recheck is None:
        raise ValueError("reviewer exception route lacks its recheck")
    accepted = context.recheck.acceptance
    if accepted is None:
        if context.recheck.submission is None:
            raise ValueError("reviewer exception route lacks its verdict submission")
        context = accept_current_module_recheck(
            {
                "context": context,
                "result": DeclarativeModuleRecheckAgentResult(
                    status="completed",
                    submission=context.recheck.submission,
                ),
            },
            store=store,
        )
        if context.status == "failed" or context.recheck is None:
            return context
        accepted = context.recheck.acceptance
        if accepted is None:
            raise ValueError("reviewer exception acceptance was not projected")
    if context.review is None:
        raise ValueError("reviewer exception route lacks its initial review")
    return context.model_copy(
        deep=True,
        update={
            "status": (
                "reviewed" if accepted.next_action == "completed" else "revision_pending"
            ),
            "module": accepted.current,
            "review": context.review.model_copy(update={"acceptance": accepted}),
            "recheck": None,
            "error": None,
        },
    )


class CapabilityModuleRuntime:
    """Compose existing Capability module lifecycle ports for Public Runtime.

    The Public Reporting runtime discovers lifecycle callables by attribute.
    This composition binds only the Capability-owned functions that are
    already available, including the shared ``ReportingStore`` where their
    signatures require it.  Missing lifecycle attributes are deliberate: the
    corresponding implementations still belong to a later migration slice.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        store: ReportingStore,
        agent_execution: AgentExecutionService,
        agent_invokers: Mapping[str, AgentInvoker],
        agent_session_factory: SessionFactory | None = None,
        global_root: Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = store
        self.global_root = (
            Path(global_root).resolve() if global_root is not None else None
        )
        self.agent_execution = agent_execution
        self.agent_session_factory = agent_session_factory
        self.agent_invokers = agent_invokers

        self.prepare_lanes = partial(
            _prepare_module_lanes,
            workspace=self.workspace,
            store=store,
            global_root=self.global_root,
        )
        self.start_lane = _start_module_lane
        self.prepare_author_lane = partial(
            prepare_current_module_authoring,
            workspace=self.workspace,
            store=store,
            global_root=self.global_root,
        )
        self.author_requires_agent = _author_requires_agent
        self.lane_has_deferred_main_exception = _lane_has_deferred_main_exception
        self.lane_retries_preflight_revision = _lane_retries_preflight_revision
        self.preflight_revision_needs_recheck = _preflight_revision_needs_recheck
        self.accept_author_lane = partial(
            accept_current_module_authoring,
            store=store,
        )
        self.resume_author_lane = resume_current_module_authoring
        self.can_review_lane = module_lane_can_review
        self.prepare_review_lane = partial(
            prepare_current_module_review,
            store=store,
        )
        self.review_preflight_needs_revision = module_review_preflight_needs_revision
        self.prepare_preflight_revision_lane = partial(
            prepare_current_module_preflight_revision,
            store=store,
        )
        self.accept_preflight_revision_lane = partial(
            accept_current_module_preflight_revision,
            store=store,
        )
        self.review_requires_agent = module_review_requires_agent
        self.accept_review_lane = partial(
            accept_current_module_review,
            store=store,
        )
        self.review_needs_revision = module_review_needs_revision
        self.review_needs_recheck = module_review_needs_recheck
        self.resume_review_lane = _resume_review_lane
        self.resume_recheck_lane = _resume_recheck_lane
        self.prepare_revision_lane = partial(
            prepare_current_module_revision,
            store=store,
        )
        self.accept_revision_lane = partial(
            accept_current_module_revision,
            store=store,
        )
        self.prepare_recheck_lane = partial(
            prepare_current_module_recheck,
            store=store,
        )
        self.recheck_requires_agent = module_recheck_requires_agent
        self.accept_recheck_lane = partial(
            accept_current_module_recheck,
            store=store,
        )
        self.prepare_author_exception_lane = prepare_current_module_author_exception
        self.prepare_main_exception_lane = partial(
            _prepare_main_exception_lane,
            store=store,
        )
        self.main_exception_requires_agent = _main_exception_requires_agent
        self.accept_main_exception_lane = partial(
            _accept_main_exception_lane,
            store=store,
        )
        self.main_exception_requests_user = _main_exception_requests_user
        self.apply_main_exception_user_input = partial(
            _apply_main_exception_user_input,
            store=store,
        )
        self.route_after_main_exception = partial(
            _route_after_main_exception,
            store=store,
        )
        self.complete_lane = complete_current_module_lane
        self.reduce_lanes = reduce_module_cohort


__all__ = ["CapabilityModuleRuntime", "SessionFactory"]
