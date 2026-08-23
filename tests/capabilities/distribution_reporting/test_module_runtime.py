"""Characterization for the Capability-owned public module composition."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime import (
    module_cohort_tools,
    module_lane_tools,
    module_review_preparation,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TEMPLATE_ROLE_SKILL_IDS,
    TemplateSkillBoundaryManifest,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleRuntimeLaneContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    ReportRequest,
    UserSupplement,
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
    "prepare_author_lane",
    "author_requires_agent",
    "accept_author_lane",
    "resume_author_lane",
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
    assert "prepare-current-module-authoring" in implementations
    assert "prepare-current-module-authoring" in (
        PublicReportingWorkflowRuntime._plan_tool_ids(plan)
    )


def _author_request() -> ReportRequest:
    return ReportRequest(
        operation="module_report",
        instruction="完成模块 2.4 的完整正文和证据绑定",
        target_modules=["2.4"],
        execution_requirements=["保留当前项目证据边界"],
        user_supplements=[
            UserSupplement(
                id="US-author-1",
                content="当前 run 的模块正文必须明确标注待核实事实。",
                scope="module",
                target_ids=["2.4"],
                stages=["module_authoring"],
            )
        ],
        missing_evidence_policy="draft",
        preparation_mode="serial",
    )


def _write_author_skill_fixture(workspace: Path) -> None:
    root = workspace / "Work/report-template-role-skills"
    root.mkdir(parents=True, exist_ok=True)
    manifest = TemplateSkillBoundaryManifest(
        transferred_categories=[
            "analysis_method",
            "synthesis_method",
            "visual_method",
            "quality_check",
        ],
        excluded_categories=[
            "domain_knowledge",
            "domain_standard_or_threshold",
            "project_fact_or_number",
            "customer_identity",
            "project_finding_or_risk",
            "project_conclusion_or_recommendation",
            "evidence_or_claim_identifier",
        ],
        boundary_statement=(
            "仅保留可跨项目复用的方法与无事实样例；当前项目事实、具体数值、客户名称、"
            "风险结论和建议必须来自本 run 输入，禁止把模板内容当作项目证据。"
            "所有跨项目方法均不得替代当前项目的结构化输入、证据和用户补充。"
        ),
    )
    skill_text = "可复用的方法说明与无事实示例。" + (" 方法步骤。" * 80)
    hashes: dict[str, str] = {}
    for skill_id in TEMPLATE_ROLE_SKILL_IDS:
        path = root / skill_id / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(skill_text, encoding="utf-8")
        hashes[f"{skill_id}/SKILL.md"] = hashlib.sha256(path.read_bytes()).hexdigest()
    boundary_path = root / "boundary.json"
    boundary_path.write_text(
        json.dumps(manifest.model_dump(mode="json")),
        encoding="utf-8",
    )
    hashes["boundary.json"] = hashlib.sha256(boundary_path.read_bytes()).hexdigest()
    (root / "source.json").write_text(
        json.dumps(
            {
                "boundary_policy_version": manifest.policy_version,
                "boundary_ref": "Work/report-template-role-skills/boundary.json",
                "artifact_sha256": hashes,
            }
        ),
        encoding="utf-8",
    )


def _author_state(workspace: Path, run_id: str, *, resume: bool = False) -> dict[str, Any]:
    store = ReportingStore(workspace)
    refs = {
        "coverage": f"Work/runs/{run_id}/preparation/coverage.json",
        "evidence": f"Work/runs/{run_id}/preparation/evidence.jsonl",
        "manifest": f"Work/runs/{run_id}/preparation/manifest.json",
    }
    for name, ref in refs.items():
        store.write_text(ref, f"{name}-{run_id}\n")
    store.write_text(
        f"Work/runs/{run_id}/preparation/completion.json",
        "completion\n",
    )
    return {
        "run_id": run_id,
        "resume": resume,
        "request": _author_request(),
        "preparation_refs": refs,
        "preparation_completion_ref": f"Work/runs/{run_id}/preparation/completion.json",
        "evidence_index_ref": f"Work/runs/{run_id}/preparation/evidence-index.json",
    }


def _legacy_runner(workspace: Path) -> Any:
    from manyselves.core.reporting.workflow import ReportWorkflowRunner

    runner = object.__new__(ReportWorkflowRunner)
    runner.service = SimpleNamespace(
        workspace=workspace,
        store=ReportingStore(workspace),
        global_root=None,
    )
    return runner


def test_capability_module_authoring_matches_legacy_fresh_provider_contract(
    tmp_path: Path,
) -> None:
    """RED characterization for the complete fresh author preparation contract."""

    from manyselves.capabilities.distribution_reporting.runtime.module_authoring_preparation import (
        build_module_dispatch,
        prepare_current_module_authoring,
    )

    legacy_workspace = tmp_path / "legacy"
    capability_workspace = tmp_path / "capability"
    _write_author_skill_fixture(legacy_workspace)
    _write_author_skill_fixture(capability_workspace)
    legacy_state = _author_state(legacy_workspace, "run-author-fresh")
    capability_state = _author_state(capability_workspace, "run-author-fresh")

    legacy = _legacy_runner(legacy_workspace)
    legacy_dispatch = legacy._build_module_dispatch(legacy_state, ("2.4",))
    legacy_state["module_dispatch"] = legacy_dispatch
    capability_dispatch = build_module_dispatch(
        capability_state,
        workspace=capability_workspace,
        store=ReportingStore(capability_workspace),
    )

    legacy_task = legacy_dispatch.module_tasks[0].model_dump(
        mode="json", exclude={"task_attempt_id"}
    )
    capability_task = capability_dispatch.module_tasks[0].model_dump(
        mode="json", exclude={"task_attempt_id"}
    )
    assert capability_task == legacy_task
    assert capability_state["module_knowledge_refs"] == legacy_state["module_knowledge_refs"]
    assert capability_state["template_skill_refs"] == legacy_state["template_skill_refs"]

    legacy_preparation = legacy._prepare_module_authoring(
        "2.4",
        legacy_state,
        "public-reporting",
        review=False,
        checkpoint=False,
    )
    capability_context = prepare_current_module_authoring(
        DeclarativeModuleRuntimeLaneContext(
            module_id="2.4",
            workflow_id="public-reporting",
            reporting_state=capability_state,
            status="ready",
        ),
        workspace=capability_workspace,
        store=ReportingStore(capability_workspace),
    )
    assert capability_context.status == "author_ready"
    assert capability_context.authoring is not None
    capability_envelope = capability_context.authoring.envelope
    assert capability_envelope is not None
    assert capability_envelope.model_dump(
        mode="json", exclude={"task_attempt_id"}
    ) == legacy_preparation.envelope.model_dump(
        mode="json", exclude={"task_attempt_id"}
    )
    assert capability_context.authoring.revision == legacy_preparation.revision


def test_capability_module_authoring_preserves_resume_result_part_projection(
    tmp_path: Path,
) -> None:
    """RED characterization for saved/rewrite parts and same-run resume identity."""

    from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
    from manyselves.capabilities.distribution_reporting.runtime.module_authoring_preparation import (
        build_module_dispatch,
        prepare_current_module_authoring,
    )

    legacy_workspace = tmp_path / "legacy"
    capability_workspace = tmp_path / "capability"
    _write_author_skill_fixture(legacy_workspace)
    _write_author_skill_fixture(capability_workspace)
    legacy_state = _author_state(legacy_workspace, "run-author-resume", resume=True)
    capability_state = _author_state(capability_workspace, "run-author-resume", resume=True)
    legacy = _legacy_runner(legacy_workspace)
    legacy._build_module_dispatch(legacy_state, ("2.4",))
    capability_dispatch = build_module_dispatch(
        capability_state,
        workspace=capability_workspace,
        store=ReportingStore(capability_workspace),
    )
    legacy_state["module_dispatch"] = legacy._build_module_dispatch(
        legacy_state, ("2.4",)
    )
    capability_state["module_dispatch"] = capability_dispatch

    part_ids = list(REPORT_TAXONOMY["2.4"].submodules)
    for workspace in (legacy_workspace, capability_workspace):
        root = workspace / "Work/runs/run-author-resume/drafts/module-2.4/r0"
        (root / "_evidence").mkdir(parents=True, exist_ok=True)
        for index, part_id in enumerate(part_ids):
            (root / f"{part_id}.md").write_text(
                f"正文 {part_id}", encoding="utf-8"
            )
            if index == 0:
                (root / "_evidence" / f"{part_id}.json").write_text(
                    json.dumps({"evidence_ids": []}), encoding="utf-8"
                )
        marker = root / "_authoring-context.json"
        marker.write_text(
            json.dumps(
                {
                    "authoring_context_sha256": (
                        _legacy_runner(legacy_workspace)
                        ._module_authoring_context_sha256(legacy_state, "2.4")
                        if workspace == legacy_workspace
                        else "placeholder"
                    )
                }
            ),
            encoding="utf-8",
        )

    # Use the exact legacy marker for the capability workspace too, so both
    # paths take the same saved-part branch without changing the contract.
    capability_marker = capability_workspace / (
        "Work/runs/run-author-resume/drafts/module-2.4/r0/_authoring-context.json"
    )
    capability_marker.write_text(
        json.dumps(
            {
                "authoring_context_sha256": _legacy_runner(capability_workspace)
                ._module_authoring_context_sha256(capability_state, "2.4")
            }
        ),
        encoding="utf-8",
    )

    legacy_preparation = legacy._prepare_module_authoring(
        "2.4",
        legacy_state,
        "public-reporting",
        review=False,
        checkpoint=False,
    )
    capability_context = prepare_current_module_authoring(
        DeclarativeModuleRuntimeLaneContext(
            module_id="2.4",
            workflow_id="public-reporting",
            reporting_state=capability_state,
            status="ready",
        ),
        workspace=capability_workspace,
        store=ReportingStore(capability_workspace),
    )
    # Existing draft parts are a resume projection inside the Author envelope;
    # ``author_resumed`` remains reserved for a cached typed submission.
    assert capability_context.status == "author_ready"
    assert capability_context.authoring is not None
    assert capability_context.authoring.revision == legacy_preparation.revision
    assert capability_context.authoring.envelope is not None
    assert legacy_preparation.envelope is not None
    assert capability_context.authoring.envelope.model_dump(
        mode="json", exclude={"task_attempt_id"}
    ) == legacy_preparation.envelope.model_dump(
        mode="json", exclude={"task_attempt_id"}
    )
