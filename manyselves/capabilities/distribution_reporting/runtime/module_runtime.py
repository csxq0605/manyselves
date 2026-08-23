"""Capability-owned composition for the public module workflow runtime.

This module only assembles lifecycle functions that already belong to the
distribution-reporting Capability.  It intentionally does not recreate the
unmigrated lane start/author/revision/recheck/main-exception lifecycle from
the legacy Reporting runner; absent attributes remain an explicit boundary
for the next extraction slices.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import Any

from manyselves.kernel.ports import AgentInvoker
from manyselves.runtime.agent_execution import AgentExecutionService, AgentSessionLoop

from .models.module_cohort import DeclarativeModuleLaneOutcome
from .models.module_lane import DeclarativeModuleRuntimeLaneContext
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


def _prepare_module_lanes(value: Any) -> Any:
    """Keep the cohort's typed reporting state unchanged at its boundary."""

    return value


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
        agent_session_factory: SessionFactory,
        agent_invokers: Mapping[str, AgentInvoker],
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = store
        self.agent_execution = agent_execution
        self.agent_session_factory = agent_session_factory
        self.agent_invokers = agent_invokers

        self.prepare_lanes = _prepare_module_lanes
        self.start_lane = _start_module_lane
        self.author_requires_agent = _author_requires_agent
        self.lane_has_deferred_main_exception = _lane_has_deferred_main_exception
        self.lane_retries_preflight_revision = _lane_retries_preflight_revision
        self.preflight_revision_needs_recheck = _preflight_revision_needs_recheck
        self.accept_author_lane = partial(
            accept_current_module_authoring,
            store=store,
        )
        self.can_review_lane = module_lane_can_review
        self.prepare_review_lane = partial(
            prepare_current_module_review,
            store=store,
        )
        self.review_preflight_needs_revision = module_review_preflight_needs_revision
        self.review_requires_agent = module_review_requires_agent
        self.accept_review_lane = partial(
            accept_current_module_review,
            store=store,
        )
        self.review_needs_revision = module_review_needs_revision
        self.review_needs_recheck = module_review_needs_recheck
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
        self.complete_lane = complete_current_module_lane
        self.reduce_lanes = reduce_module_cohort


__all__ = ["CapabilityModuleRuntime", "SessionFactory"]
