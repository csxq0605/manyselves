"""Characterization for the Capability-owned public module composition."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime import (
    module_cohort_tools,
    module_lane_tools,
    module_review_preparation,
)
from manyselves.capabilities.distribution_reporting.runtime.module_cohort_tools import (
    complete_current_module_lane,
    reduce_module_cohort,
)
from manyselves.capabilities.distribution_reporting.runtime.module_lane_tools import (
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
from manyselves.capabilities.distribution_reporting.runtime.module_recheck_tools import (
    accept_current_module_recheck,
    prepare_current_module_recheck,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_preparation import (
    prepare_current_module_review,
)
from manyselves.capabilities.distribution_reporting.runtime.module_revision_tools import (
    accept_current_module_revision,
    prepare_current_module_revision,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore

_PUBLIC_MODULE_RUNTIME_METHODS = (
    "start_lane",
    "prepare_author_lane",
    "author_requires_agent",
    "accept_author_lane",
    "resume_author_lane",
    "can_review_lane",
    "prepare_review_lane",
    "review_preflight_needs_revision",
    "prepare_preflight_revision_lane",
    "accept_preflight_revision_lane",
    "review_requires_agent",
    "accept_review_lane",
    "review_needs_revision",
    "review_needs_recheck",
    "prepare_revision_lane",
    "accept_revision_lane",
    "prepare_author_exception_lane",
    "prepare_recheck_lane",
    "recheck_requires_agent",
    "accept_recheck_lane",
    "resume_review_lane",
    "resume_recheck_lane",
    "lane_has_deferred_main_exception",
    "lane_retries_preflight_revision",
    "preflight_revision_needs_recheck",
    "prepare_main_exception_lane",
    "main_exception_requires_agent",
    "accept_main_exception_lane",
    "main_exception_requests_user",
    "apply_main_exception_user_input",
    "route_after_main_exception",
    "complete_lane",
    "prepare_lanes",
    "reduce_lanes",
)

_CAPABILITY_AVAILABLE_METHODS = (
    "start_lane",
    "author_requires_agent",
    "accept_author_lane",
    "can_review_lane",
    "prepare_review_lane",
    "review_preflight_needs_revision",
    "review_requires_agent",
    "accept_review_lane",
    "review_needs_revision",
    "review_needs_recheck",
    "prepare_revision_lane",
    "accept_revision_lane",
    "prepare_author_exception_lane",
    "prepare_recheck_lane",
    "recheck_requires_agent",
    "accept_recheck_lane",
    "lane_has_deferred_main_exception",
    "lane_retries_preflight_revision",
    "preflight_revision_needs_recheck",
    "complete_lane",
    "prepare_lanes",
    "reduce_lanes",
)


def _build_runtime(tmp_path: Path) -> tuple[Any, object, object, dict[str, object]]:
    from manyselves.capabilities.distribution_reporting.runtime.module_runtime import (
        CapabilityModuleRuntime,
    )

    execution = object()
    session_factory = object()
    agent_invokers = {"module-2.4-specialist": object()}
    runtime = CapabilityModuleRuntime(
        tmp_path,
        store=ReportingStore(tmp_path),
        agent_execution=execution,
        agent_session_factory=session_factory,
        agent_invokers=agent_invokers,
    )
    return runtime, execution, session_factory, agent_invokers


def test_module_runtime_delegates_capability_owned_happy_path_ports(
    tmp_path: Path,
) -> None:
    runtime, execution, session_factory, agent_invokers = _build_runtime(tmp_path)

    assert runtime.agent_execution is execution
    assert runtime.agent_session_factory is session_factory
    assert runtime.agent_invokers is agent_invokers

    assert runtime.accept_author_lane.func is module_lane_tools.accept_current_module_authoring
    assert runtime.can_review_lane is module_lane_tools.module_lane_can_review
    assert runtime.prepare_review_lane.func is module_review_preparation.prepare_current_module_review
    assert (
        runtime.review_preflight_needs_revision
        is module_lane_tools.module_review_preflight_needs_revision
    )
    assert runtime.review_requires_agent is module_lane_tools.module_review_requires_agent
    assert runtime.accept_review_lane.func is module_lane_tools.accept_current_module_review
    assert runtime.review_needs_revision is module_lane_tools.module_review_needs_revision
    assert runtime.review_needs_recheck is module_lane_tools.module_review_needs_recheck
    assert runtime.prepare_revision_lane.func is prepare_current_module_revision
    assert runtime.accept_revision_lane.func is accept_current_module_revision
    assert runtime.prepare_recheck_lane.func is prepare_current_module_recheck
    assert runtime.recheck_requires_agent is module_recheck_requires_agent
    assert runtime.accept_recheck_lane.func is accept_current_module_recheck
    assert runtime.prepare_author_exception_lane is prepare_current_module_author_exception
    assert runtime.complete_lane is module_cohort_tools.complete_current_module_lane
    assert runtime.reduce_lanes is module_cohort_tools.reduce_module_cohort

    # Keep direct imports in the characterization so a future refactor cannot
    # accidentally satisfy this test with an identically named local function.
    assert runtime.accept_author_lane.func is accept_current_module_authoring
    assert runtime.prepare_review_lane.func is prepare_current_module_review
    assert runtime.accept_review_lane.func is accept_current_module_review
    assert runtime.complete_lane is complete_current_module_lane
    assert runtime.reduce_lanes is reduce_module_cohort
    assert runtime.review_needs_revision is module_review_needs_revision
    assert runtime.review_needs_recheck is module_review_needs_recheck
    assert runtime.prepare_revision_lane.func is prepare_current_module_revision
    assert runtime.accept_revision_lane.func is accept_current_module_revision
    assert runtime.prepare_recheck_lane.func is prepare_current_module_recheck
    assert runtime.recheck_requires_agent is module_recheck_requires_agent
    assert runtime.accept_recheck_lane.func is accept_current_module_recheck
    assert runtime.prepare_author_exception_lane is prepare_current_module_author_exception
    assert runtime.review_requires_agent is module_review_requires_agent
    assert runtime.review_preflight_needs_revision is module_review_preflight_needs_revision
    assert runtime.can_review_lane is module_lane_can_review


def test_module_runtime_marks_unmigrated_lifecycle_ports_explicitly(
    tmp_path: Path,
) -> None:
    runtime, _execution, _session_factory, _agent_invokers = _build_runtime(tmp_path)

    available = [
        name for name in _PUBLIC_MODULE_RUNTIME_METHODS if callable(getattr(runtime, name, None))
    ]
    missing = [
        name for name in _PUBLIC_MODULE_RUNTIME_METHODS if not callable(getattr(runtime, name, None))
    ]

    assert available == list(_CAPABILITY_AVAILABLE_METHODS)
    assert missing == [
        "prepare_author_lane",
        "resume_author_lane",
        "prepare_preflight_revision_lane",
        "accept_preflight_revision_lane",
        "resume_review_lane",
        "resume_recheck_lane",
        "prepare_main_exception_lane",
        "main_exception_requires_agent",
        "accept_main_exception_lane",
        "main_exception_requests_user",
        "apply_main_exception_user_input",
        "route_after_main_exception",
    ]


def test_public_runtime_exposes_migrated_lane_ports_without_boundary_runtime(
    tmp_path: Path,
) -> None:
    """Characterize the real Public Runtime binding and the remaining author gap."""

    from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
        ReportRequest,
    )
    from manyselves.capabilities.distribution_reporting.runtime.public_reporting import (
        PublicReportingWorkflowRuntime,
    )

    module_runtime, _execution, _session_factory, _agent_invokers = _build_runtime(tmp_path)
    public_runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot=lambda _run_id: {},
        snapshot_content=lambda source, target: (target, "a" * 64, "blob"),
        runtime_photo_ids=lambda _evidence, _photos: None,
        module_runtime=module_runtime,
    )
    request = ReportRequest(
        operation="module_report",
        instruction="run module 2.4",
        target_modules=["2.4"],
        missing_evidence_policy="draft",
        preparation_mode="serial",
    )
    _definitions, _contracts, plan = public_runtime.compile_plan(request)
    implementations = public_runtime._module_tool_implementations()

    assert "start-current-module-lane" in implementations
    assert "prepare-module-cohort" in implementations
    assert "prepare-current-module-recheck" in implementations
    assert "module-recheck-requires-agent" in implementations
    assert "accept-current-module-recheck" in implementations
    assert "prepare-current-module-author-exception" in implementations
    assert "module-lane-has-deferred-main-exception" in implementations
    assert "prepare-current-module-authoring" not in implementations
    assert "prepare-current-module-authoring" in (
        PublicReportingWorkflowRuntime._plan_tool_ids(plan)
    )
