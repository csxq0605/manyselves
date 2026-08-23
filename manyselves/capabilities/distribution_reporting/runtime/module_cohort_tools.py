"""Capability-owned completion and reduction for selected module lanes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_cohort import (
    DeclarativeModuleLaneOutcome,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleRuntimeLaneContext,
)


def complete_current_module_lane(
    value: DeclarativeModuleRuntimeLaneContext,
) -> DeclarativeModuleLaneOutcome:
    """Project one reviewed lane into the typed cohort outcome."""

    context = (
        value
        if isinstance(value, DeclarativeModuleRuntimeLaneContext)
        else DeclarativeModuleRuntimeLaneContext.model_validate(value)
    )
    completion_refs = context.reporting_state.get("module_review_completion_refs", {})
    completion_ref = (
        completion_refs.get(context.module_id)
        if isinstance(completion_refs, Mapping)
        else None
    )
    completed = (
        context.status in {"reviewed", "completed"}
        and completion_ref is not None
    )
    return DeclarativeModuleLaneOutcome(
        module_id=context.module_id,
        status="completed" if completed else "failed",
        module=context.module if completed else None,
        error=context.error,
        lane_state=context.reporting_state if completed else None,
        completion_ref=completion_ref if completed else None,
    )


def reduce_module_cohort(
    values: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Reduce selected lanes while carrying their accepted state forward.

    The public root attaches the cohort result back to the reporting state.  A
    lane outcome therefore carries both its typed submission and the small
    reporting-state projection produced by review acceptance; dropping that
    projection would lose specialist submissions and completion references at
    the root boundary.
    """

    submissions: dict[str, dict[str, Any]] = {}
    for module_id, value in values.items():
        outcome = DeclarativeModuleLaneOutcome.model_validate(value)
        if outcome.status != "completed" or outcome.module is None:
            raise ValueError(outcome.error or f"module lane {module_id} did not complete")
        submission = ModuleSubmission.model_validate(outcome.module)
        submissions[submission.module_id] = {
            "module": submission,
            "lane_state": outcome.lane_state,
            "completion_ref": outcome.completion_ref,
        }
    return submissions


def build_module_cohort_tool_implementations() -> dict[str, Any]:
    """Bind selected-lane completion and reduction to the generic Host."""

    return {
        "complete-current-module-lane": complete_current_module_lane,
        "reduce-module-cohort": reduce_module_cohort,
    }


__all__ = [
    "build_module_cohort_tool_implementations",
    "complete_current_module_lane",
    "reduce_module_cohort",
]
