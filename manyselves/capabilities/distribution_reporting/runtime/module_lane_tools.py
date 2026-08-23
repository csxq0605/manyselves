"""Capability-owned typed projections for the public module runtime Lane.

These projections carry the result of the selected module Author turn into the
file-defined review branch.  They deliberately do not perform review
preflight, persistence, recovery, or Agent orchestration; those are separate
Capability slices and the generic Workflow Host remains the boundary between
each action.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any

from .models.agentic import ModuleSubmission
from .models.module_lane import (
    DeclarativeModuleAuthoringAgentResult,
    DeclarativeModuleRuntimeLaneContext,
)
from .models.review import ModuleInitialReviewAcceptance, ModuleRecheckAcceptance
from .module_recheck_tools import (
    accept_current_module_recheck,
    module_recheck_requires_agent,
    prepare_current_module_recheck,
)
from .module_review_acceptance import accept_current_module_review
from .module_review_preparation import prepare_current_module_review
from .module_revision_tools import (
    accept_current_module_revision,
    prepare_current_module_revision,
)
from .storage import ReportingStore


def _context(value: Any) -> DeclarativeModuleRuntimeLaneContext:
    if isinstance(value, DeclarativeModuleRuntimeLaneContext):
        return value
    return DeclarativeModuleRuntimeLaneContext.model_validate(value)


def accept_current_module_authoring(
    value: Mapping[str, Any],
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Project one completed Author result into Lane and reporting state."""

    context = _context(value["context"])
    result = DeclarativeModuleAuthoringAgentResult.model_validate(value["result"])
    if result.status == "failed":
        return context.model_copy(
            deep=True,
            update={
                "status": "failed",
                "authoring": None,
                "error": result.error or "module author failed",
            },
        )

    submission = ModuleSubmission.model_validate(result.module)
    if submission.module_id != context.module_id:
        raise ValueError("module author returned the wrong module payload")
    revision = context.authoring.revision if context.authoring is not None else submission.revision
    run_id = str(context.reporting_state["run_id"])
    store.write_json(
        f"Work/runs/{run_id}/modules/{context.module_id}-r{revision}.json",
        submission.model_dump(mode="json"),
    )
    accepted = context.model_copy(
        deep=True,
        update={
            "status": "authored",
            "authoring": None,
            "module": submission,
            "error": None,
        },
    )
    accepted.reporting_state.setdefault("specialist_submissions", {})[
        context.module_id
    ] = submission
    return accepted


def module_lane_can_review(
    value: DeclarativeModuleRuntimeLaneContext,
) -> bool:
    """Preserve the existing authored-to-review route predicate."""

    return _context(value).status == "authored"


def module_review_preflight_needs_revision(
    value: DeclarativeModuleRuntimeLaneContext,
) -> bool:
    """Route only a prepared machine-preflight failure to author correction."""

    context = _context(value)
    initial_revision = (
        context.review is not None
        and context.review.prepared.mode == "preflight_revision"
    )
    recheck_revision = (
        context.recheck is not None
        and context.recheck.prepared.mode == "preflight_revision"
    )
    return initial_revision or recheck_revision


def module_review_requires_agent(
    value: DeclarativeModuleRuntimeLaneContext,
) -> bool:
    """Route a passed initial preflight to the declared Auditor Agent."""

    context = _context(value)
    return (
        context.review is not None
        and context.review.acceptance is None
        and context.review.prepared.mode == "invoke_agent"
    )


def module_review_needs_recheck(
    value: DeclarativeModuleRuntimeLaneContext,
) -> bool:
    """Route only a future recheck continuation to the Auditor recheck branch."""

    context = _context(value)
    acceptance = context.review.acceptance if context.review is not None else None
    return (
        isinstance(acceptance, ModuleRecheckAcceptance)
        and acceptance.next_action == "continue_existing"
        and not acceptance.findings
    )


def module_review_needs_revision(
    value: DeclarativeModuleRuntimeLaneContext,
) -> bool:
    """Route initial findings to the next Author revision boundary."""

    context = _context(value)
    acceptance = context.review.acceptance if context.review is not None else None
    if isinstance(acceptance, ModuleInitialReviewAcceptance):
        return acceptance.next_action == "revise"
    return (
        isinstance(acceptance, ModuleRecheckAcceptance)
        and acceptance.next_action == "continue_existing"
        and bool(acceptance.findings)
    )


def prepare_current_module_author_exception(
    value: DeclarativeModuleRuntimeLaneContext,
) -> DeclarativeModuleRuntimeLaneContext:
    """Keep the existing author-exception decision boundary explicit.

    A normal typed revision has no deferred Main decision.  The file workflow
    still visits this boundary so a future exception implementation can be
    attached without changing the Kernel graph.
    """

    return _context(value)


def build_module_lane_tool_implementations(
    *,
    store: ReportingStore,
) -> dict[str, Any]:
    """Return the migrated Author accept and adjacent route predicate."""

    return {
        "accept-current-module-authoring": partial(
            accept_current_module_authoring,
            store=store,
        ),
        "module-lane-can-review": module_lane_can_review,
        "module-review-preflight-needs-revision": module_review_preflight_needs_revision,
        "module-review-requires-agent": module_review_requires_agent,
        "accept-current-module-review": partial(
            accept_current_module_review,
            store=store,
        ),
        "module-review-needs-recheck": module_review_needs_recheck,
        "module-review-needs-revision": module_review_needs_revision,
        "prepare-current-module-review": partial(
            prepare_current_module_review,
            store=store,
        ),
        "prepare-current-module-revision": partial(
            prepare_current_module_revision,
            store=store,
        ),
        "accept-current-module-revision": partial(
            accept_current_module_revision,
            store=store,
        ),
        "prepare-current-module-recheck": partial(
            prepare_current_module_recheck,
            store=store,
        ),
        "accept-current-module-recheck": partial(
            accept_current_module_recheck,
            store=store,
        ),
        "module-recheck-requires-agent": module_recheck_requires_agent,
        "prepare-current-module-author-exception": prepare_current_module_author_exception,
    }


__all__ = [
    "accept_current_module_authoring",
    "accept_current_module_review",
    "build_module_lane_tool_implementations",
    "module_lane_can_review",
    "module_review_preflight_needs_revision",
    "module_review_requires_agent",
    "module_review_needs_recheck",
    "module_review_needs_revision",
    "prepare_current_module_review",
    "accept_current_module_revision",
    "prepare_current_module_revision",
    "accept_current_module_recheck",
    "module_recheck_requires_agent",
    "prepare_current_module_recheck",
    "prepare_current_module_author_exception",
]
