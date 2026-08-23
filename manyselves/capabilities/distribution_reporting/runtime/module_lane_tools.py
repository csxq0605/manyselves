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
from .module_review_preparation import prepare_current_module_review
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
    return (
        context.review is not None
        and context.review.prepared.mode == "preflight_revision"
    )


def module_review_requires_agent(
    value: DeclarativeModuleRuntimeLaneContext,
) -> bool:
    """Route a passed initial preflight to the declared Auditor Agent."""

    context = _context(value)
    return (
        context.review is not None
        and context.review.prepared.mode == "invoke_agent"
    )


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
        "prepare-current-module-review": partial(
            prepare_current_module_review,
            store=store,
        ),
    }


__all__ = [
    "accept_current_module_authoring",
    "build_module_lane_tool_implementations",
    "module_lane_can_review",
    "module_review_preflight_needs_revision",
    "module_review_requires_agent",
    "prepare_current_module_review",
]
