"""Characterization for the Capability-owned public module composition."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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


def test_module_provider_composition_exposes_real_runtime_ports_and_injected_dependencies(
    tmp_path: Path,
) -> None:
    """Characterize the missing Provider composition before implementation."""

    from manyselves.application.runtime_services import RuntimeServicesView
    from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
        ModuleProviderDependencies,
        build_module_provider_composition,
    )
    from manyselves.config.schema import AgentDefaults
    from manyselves.core.loops.bus import MessageBus
    from manyselves.runtime.agent_execution import AgentExecutionService

    bus = MessageBus()
    execution = AgentExecutionService(bus)
    session_factory = object()
    gateway = object()
    task_correlation = object()
    recovery_callback = object()
    dependencies = ModuleProviderDependencies(
        artifact_gateway=gateway,
        task_correlation=task_correlation,
        recovery_event_callback=recovery_callback,
    )
    services = RuntimeServicesView(
        workspace=tmp_path,
        bus=bus,
        active_provider=object(),
        agent_defaults=AgentDefaults(),
        global_knowledge_root=None,
    )

    composition = build_module_provider_composition(
        services,
        execution=execution,
        agent_session_factory=session_factory,
        dependencies=dependencies,
    )

    assert composition.module_runtime.agent_execution is execution
    assert composition.module_runtime.agent_session_factory is session_factory
    assert composition.module_runtime.agent_invokers is composition.agent_invokers
    assert composition.dependencies is dependencies
    assert composition.dependencies.artifact_gateway is gateway
    assert composition.dependencies.task_correlation is task_correlation
    assert composition.dependencies.recovery_event_callback is recovery_callback


def test_public_runtime_keeps_capability_provider_invokers(tmp_path: Path) -> None:
    """The Host must not replace the Provider runtime with a tool-less bridge."""

    from manyselves.application.runtime_services import RuntimeServicesView
    from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
        build_module_provider_composition,
    )
    from manyselves.capabilities.distribution_reporting.runtime.public_reporting import (
        PublicReportingWorkflowRuntime,
    )
    from manyselves.config.schema import AgentDefaults
    from manyselves.core.loops.bus import MessageBus

    composition = build_module_provider_composition(
        RuntimeServicesView(
            workspace=tmp_path,
            bus=MessageBus(),
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
    )
    runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot=object(),
        snapshot_content=lambda source, target: (target, "", source),
        runtime_photo_ids=lambda _evidence, _photos: [],
        module_runtime=composition.module_runtime,
    )

    invokers = runtime._agent_invokers()

    assert composition.module_runtime.agent_session_factory is None
    assert invokers["module-2.4-specialist"] is composition.provider
    assert invokers["evidence-auditor"] is composition.provider


@pytest.mark.asyncio
async def test_module_provider_runtime_builds_declared_tools_and_reuses_conversation_session(
    tmp_path: Path,
) -> None:
    """Provider composition uses the generic service for both Author turns."""

    from manyselves.application.runtime_services import RuntimeServicesView
    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )
    from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
        REPORT_TAXONOMY,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        AgentResult,
        AgentRunStatus,
        ModuleSubmission,
        TaskEnvelope,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleAuthoringPreparation,
        DeclarativeModuleRuntimeLaneContext,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
        ModuleProviderDependencies,
        ModuleProviderRuntime,
    )
    from manyselves.config.schema import AgentDefaults
    from manyselves.core.loops.bus import MessageBus
    from manyselves.core.tools.registry import Tool
    from manyselves.interfaces.types import AgentResultMessage, UserMessage
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import DefinitionKind
    from manyselves.runtime.agent_execution import AgentExecutionService

    _capability, registry = load_distribution_reporting_capability()
    agent = registry.require(DefinitionKind.AGENT, "module-2.4-specialist")
    task = registry.require(DefinitionKind.TASK, "module-2.4-authoring")
    run_id = "module-provider-runtime"
    part_ids = list(REPORT_TAXONOMY["2.4"].submodules)
    envelope = TaskEnvelope(
        task_id="module-2.4",
        run_id=run_id,
        agent_id=agent.id,
        objective=task.objective,
        allowed_outputs=["module_submission"],
        allowed_tools=list(task.tools),
        input_refs=[f"Work/runs/{run_id}/context/module-input.json"],
        target_submodule_ids=part_ids,
        input_contract_kind="module_authoring_input",
        input_contract_ref=f"Work/runs/{run_id}/context/module-input.json",
    )
    context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": run_id},
        status="author_ready",
        authoring=DeclarativeModuleAuthoringPreparation(
            specialist_id=agent.id,
            envelope=envelope,
            revision=0,
            review=False,
            checkpoint=False,
        ),
    )
    result_ref = f"Work/runs/{run_id}/results/module-2.4.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        AgentResult(
            task_id=envelope.task_id,
            run_id=envelope.run_id,
            agent_id=envelope.agent_id,
            session_id="persisted-module-session",
            status=AgentRunStatus.COMPLETED,
            payload=ModuleSubmission(
                module_id="2.4",
                submodule_narratives={part_id: f"正文 {part_id}" for part_id in part_ids},
                claims=[],
                source_ids=[],
                unresolved_questions=[],
                revision=0,
            ),
        ).model_dump_json(),
        encoding="utf-8",
    )

    class InjectedTool(Tool):
        def __init__(self, name: str) -> None:
            self.name = name

        async def __call__(self, **kwargs: Any) -> dict[str, Any]:
            return {"name": self.name, "kwargs": kwargs}

    injected = {
        name: InjectedTool(name)
        for name in {"inspect_image", "open_artifact", "search_text", "calculate"}
    }
    gateway = object()
    task_correlation = object()
    recovery_callback = object()
    dependencies = ModuleProviderDependencies(
        artifact_gateway=gateway,
        task_correlation=task_correlation,
        recovery_event_callback=recovery_callback,
        tool_implementations=injected,
    )
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    service = AgentExecutionService(bus, timeout=1)
    built_kwargs: list[dict[str, Any]] = []
    received: list[UserMessage] = []

    class ScriptedLoop:
        def __init__(self, **kwargs: Any) -> None:
            built_kwargs.append(kwargs)
            self.runtime_id = str(kwargs["agent_type"])
            self.callback = None

        def restore_conversation(self, messages, *, task_boundaries=(), handoff_summary=None):
            del messages, task_boundaries, handoff_summary

        async def start(self) -> None:
            async def respond(message: UserMessage) -> None:
                if message.agent_type != self.runtime_id:
                    return
                received.append(message)
                result_path.write_text(
                    AgentResult(
                        task_id=envelope.task_id,
                        run_id=envelope.run_id,
                        agent_id=envelope.agent_id,
                        session_id=message.session_id,
                        status=AgentRunStatus.COMPLETED,
                        payload=ModuleSubmission(
                            module_id="2.4",
                            submodule_narratives={
                                part_id: f"正文 {part_id}" for part_id in part_ids
                            },
                            claims=[],
                            source_ids=[],
                            unresolved_questions=[],
                            revision=0,
                        ),
                    ).model_dump_json(),
                    encoding="utf-8",
                )
                await bus.publish(
                    AgentResultMessage(
                        sender=envelope.agent_id,
                        workflow_id=message.workflow_id,
                        task_id=envelope.task_id,
                        run_id=envelope.run_id,
                        result_path=result_ref,
                        task_attempt_id="",
                        session_id=message.session_id,
                    )
                )

            self.callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self.callback is not None:
                bus.unsubscribe(UserMessage, self.callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    def loop_builder(**kwargs: Any) -> ScriptedLoop:
        return ScriptedLoop(**kwargs)

    services = RuntimeServicesView(
        workspace=tmp_path,
        bus=bus,
        active_provider=object(),
        agent_defaults=AgentDefaults(),
        global_knowledge_root=None,
    )
    runtime = ModuleProviderRuntime(
        services,
        execution=service,
        loop_builder=loop_builder,
        dependencies=dependencies,
    )
    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(agent_id=agent.id, value="module-2.4", mode="run"),
        run_id=run_id,
    )
    try:
        first = await runtime.invoke(agent, task, context, conversation, task_id="dispatch-2.4")
        second = await runtime.invoke(agent, task, context, conversation, task_id="dispatch-2.4")

        assert first.status == "ok"
        assert second.status == "ok"
        assert len(built_kwargs) == 1
        assert built_kwargs[0]["bus"] is bus
        assert built_kwargs[0]["llm_provider"] is services.active_provider
        assert built_kwargs[0]["config"] is services.agent_defaults
        assert built_kwargs[0]["system_prompt"] == agent.instructions
        assert built_kwargs[0]["artifact_gateway"] is gateway
        assert set(built_kwargs[0]["tools"].get_all()) == set(task.tools)
        assert len(received) == 2
        assert first.session_id == second.session_id
        assert conversation.external_session_id == first.session_id
        assert len(service.sessions) == 1
    finally:
        await runtime.close()
        bus.shutdown()
        await bus_task


@pytest.mark.asyncio
async def test_module_bridges_use_prepared_envelope_identity_and_agent_result_payload(
    tmp_path: Path,
) -> None:
    """Author revision, Review, and Recheck use the SubmitResultTool wire shape."""

    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        AgentResult,
        AgentRunStatus,
        ModuleReviewFindingSubmission,
        ModuleReviewVerdictSubmission,
        ModuleRevisionSubmission,
        TaskEnvelope,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        ModuleReviewInput,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleRecheckPreparation,
        DeclarativeModuleReviewPreparation,
        DeclarativeModuleRevisionPreparation,
        DeclarativeModuleRuntimeLaneContext,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.review import (
        ModuleInitialReviewPreparation,
        ModuleRecheckPreparation,
        ModuleRevisionPreparation,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_agent_bridge import (
        ModuleAuthoringAgentBridge,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_reviewer_bridge import (
        ModuleReviewerAgentBridge,
    )
    from manyselves.core.loops.bus import MessageBus
    from manyselves.interfaces.types import AgentResultMessage, UserMessage
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import DefinitionKind
    from manyselves.runtime.agent_execution import AgentExecutionService

    _capability, registry = load_distribution_reporting_capability()
    author = registry.require(DefinitionKind.AGENT, "module-2.4-specialist")
    reviewer = registry.require(DefinitionKind.AGENT, "evidence-auditor")
    revision_task = registry.require(DefinitionKind.TASK, "module-2.4-runtime-revision")
    review_task = registry.require(DefinitionKind.TASK, "module-runtime-initial-review")
    recheck_task = registry.require(DefinitionKind.TASK, "module-runtime-recheck")
    run_id = "module-bridge-terminal-contract"

    revision_envelope = TaskEnvelope(
        task_id="module-revision-r1-2.4",
        run_id=run_id,
        agent_id=author.id,
        objective=revision_task.objective,
        allowed_outputs=["module_revision_submission"],
    )
    revision_context = DeclarativeModuleRuntimeLaneContext.model_construct(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": run_id},
        status="revision_ready",
        revision=DeclarativeModuleRevisionPreparation.model_construct(
            prepared=ModuleRevisionPreparation.model_construct(
                envelope=revision_envelope,
                run_id=run_id,
                module_id="2.4",
                workflow_id="public-reporting",
            )
        ),
    )

    review_envelope = TaskEnvelope(
        task_id="module-2.4-initial-review-r0",
        run_id=run_id,
        agent_id=reviewer.id,
        objective=review_task.objective,
        allowed_outputs=["module_review_finding_submission"],
    )
    review_context = DeclarativeModuleRuntimeLaneContext.model_construct(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": run_id},
        status="review_ready",
        review=DeclarativeModuleReviewPreparation.model_construct(
            envelope=review_envelope,
            prepared=ModuleInitialReviewPreparation.model_construct(
                envelope=review_envelope,
                review_input=ModuleReviewInput.model_construct(),
            ),
        ),
    )

    recheck_envelope = TaskEnvelope(
        task_id="module-2.4-initial-review-r1",
        run_id=run_id,
        agent_id=reviewer.id,
        objective=recheck_task.objective,
        allowed_outputs=["module_review_verdict_submission"],
    )
    recheck_context = DeclarativeModuleRuntimeLaneContext.model_construct(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": run_id},
        status="recheck_ready",
        recheck=DeclarativeModuleRecheckPreparation.model_construct(
            prepared=ModuleRecheckPreparation.model_construct(
                envelope=recheck_envelope,
                review_input=ModuleReviewInput.model_construct(phase="recheck"),
            )
        ),
    )

    class EnvelopeLoop:
        def __init__(self, **kwargs: Any) -> None:
            self.runtime_id = str(kwargs["agent_type"])
            self.envelope = kwargs.pop("_test_envelope")
            self.payload = kwargs.pop("_test_payload")
            self.result_path = kwargs.pop("_test_result_path")
            self.bus = kwargs["bus"]
            self.received: list[UserMessage] = []
            self.published: list[AgentResultMessage] = []
            self._callback = None

        def restore_conversation(
            self,
            messages,
            *,
            task_boundaries=(),
            handoff_summary=None,
        ) -> None:
            del messages, task_boundaries, handoff_summary

        async def start(self) -> None:
            async def respond(message: UserMessage) -> None:
                if message.agent_type != self.runtime_id:
                    return
                self.received.append(message)
                result = AgentResult(
                    task_id=self.envelope.task_id,
                    run_id=self.envelope.run_id,
                    agent_id=self.envelope.agent_id,
                    session_id=message.session_id,
                    status=AgentRunStatus.COMPLETED,
                    payload=self.payload,
                )
                self.result_path.write_text(result.model_dump_json(), encoding="utf-8")
                terminal = AgentResultMessage(
                    sender=self.envelope.agent_id,
                    workflow_id=message.workflow_id,
                    task_id=self.envelope.task_id,
                    run_id=self.envelope.run_id,
                    result_path=self.result_path.relative_to(tmp_path).as_posix(),
                    task_attempt_id="",
                    session_id=message.session_id,
                )
                self.published.append(terminal)
                await self.bus.publish(terminal)

            self._callback = respond
            self.bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self._callback is not None:
                self.bus.unsubscribe(UserMessage, self._callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    execution = AgentExecutionService(bus, timeout=1)
    conversations = ConversationRegistry()
    cases = (
        (
            "revision",
            ModuleAuthoringAgentBridge,
            author,
            revision_task,
            revision_context,
            revision_envelope,
            ModuleRevisionSubmission(
                module_id="2.4",
                base_revision=0,
                revision=1,
                submodule_narratives={"2.4.1.1": "修订后的正文"},
                claims_upsert=[],
                claim_ids_remove=[],
                source_ids=[],
                unresolved_questions=[],
                revision_responses=[],
            ),
        ),
        (
            "review",
            ModuleReviewerAgentBridge,
            reviewer,
            review_task,
            review_context,
            review_envelope,
            ModuleReviewFindingSubmission(
                coverage={"submodule_ids": ["2.4.1.1"]},
                findings=[],
            ),
        ),
        (
            "recheck",
            ModuleReviewerAgentBridge,
            reviewer,
            recheck_task,
            recheck_context,
            recheck_envelope,
            ModuleReviewVerdictSubmission(
                coverage={"submodule_ids": ["2.4.1.1"]},
                verdicts=[],
                new_findings=[],
            ),
        ),
    )
    loops: list[EnvelopeLoop] = []
    try:
        for label, bridge_type, agent, task, context, envelope, payload in cases:
            result_path = tmp_path / f"Work/results/{label}.json"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text("{}", encoding="utf-8")

            def session_factory(
                _runtime_id: str,
                *,
                _envelope=envelope,
                _payload=payload,
                _result_path=result_path,
            ) -> EnvelopeLoop:
                loop = EnvelopeLoop(
                    agent_type=_runtime_id,
                    bus=bus,
                    _test_envelope=_envelope,
                    _test_payload=_payload,
                    _test_result_path=_result_path,
                )
                loops.append(loop)
                return loop

            bridge = bridge_type(
                tmp_path,
                execution=execution,
                session_factory=session_factory,
            )
            conversation = conversations.create_or_resolve(
                ConversationKey(
                    agent_id=agent.id,
                    value=f"module-bridge-{label}",
                    mode="run",
                ),
                run_id=run_id,
            )
            outcome = await bridge.invoke(
                agent,
                task,
                context,
                conversation,
                task_id=f"host-action-{label}",
            )

            assert outcome.status == "ok"
            assert outcome.result["status"] == "completed"
            assert outcome.result["submission"]["kind"] == payload.kind
            loop = loops[-1]
            assert loop.received[0].task_id == f"host-action-{label}"
            assert loop.published[0].sender == envelope.agent_id
            assert loop.published[0].task_id == envelope.task_id
            assert loop.published[0].task_attempt_id == ""
    finally:
        await execution.close_workflow("public-reporting")
        bus.shutdown()
        await bus_task


def test_module_provider_runtime_selects_reviewer_bridge_for_declared_review_task(
    tmp_path: Path,
) -> None:
    """Reviewer composition keeps its typed bridge and full declared tool set."""

    from manyselves.application.runtime_services import RuntimeServicesView
    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )
    from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
        REPORT_TAXONOMY,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        TaskEnvelope,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleReviewPreparation,
        DeclarativeModuleRuntimeLaneContext,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
        ModuleProviderDependencies,
        ModuleProviderRuntime,
        build_module_provider_tools,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_reviewer_bridge import (
        ModuleReviewerAgentBridge,
    )
    from manyselves.config.schema import AgentDefaults
    from manyselves.core.loops.bus import MessageBus
    from manyselves.core.tools.registry import Tool
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import DefinitionKind
    from manyselves.runtime.agent_execution import AgentExecutionService

    _capability, registry = load_distribution_reporting_capability()
    agent = registry.require(DefinitionKind.AGENT, "evidence-auditor")
    task = registry.require(DefinitionKind.TASK, "module-runtime-initial-review")
    run_id = "module-provider-review"
    envelope = TaskEnvelope(
        task_id="module-2.4-initial-review-r0",
        run_id=run_id,
        agent_id=agent.id,
        objective=task.objective,
        allowed_outputs=["module_review_finding_submission"],
        allowed_tools=list(task.tools),
        input_refs=[f"Work/runs/{run_id}/reviews/input.json"],
        target_submodule_ids=list(REPORT_TAXONOMY["2.4"].submodules),
        input_contract_kind="module_review_input",
        input_contract_ref=f"Work/runs/{run_id}/reviews/input.json",
    )
    review = DeclarativeModuleReviewPreparation.model_construct(
        envelope=envelope,
        reviewer_session_key="module-auditor-2.4",
        prepared=object(),
    )
    context = DeclarativeModuleRuntimeLaneContext.model_construct(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": run_id},
        status="review_ready",
        review=review,
    )

    class InjectedTool(Tool):
        def __init__(self, name: str) -> None:
            self.name = name

        async def __call__(self, **kwargs: Any) -> dict[str, Any]:
            return kwargs

    injected = {
        name: InjectedTool(name)
        for name in ("inspect_image", "calculate")
    }
    dependencies = ModuleProviderDependencies(
        artifact_gateway=object(),
        task_correlation=object(),
        recovery_event_callback=object(),
        tool_implementations=injected,
    )
    captured: dict[str, Any] = {}
    built_registry: dict[str, Any] = {}

    def tool_builder(*args: Any, **kwargs: Any):
        captured.update(kwargs)
        built_registry["value"] = build_module_provider_tools(*args, **kwargs)
        return built_registry["value"]

    bus = MessageBus()
    runtime = ModuleProviderRuntime(
        RuntimeServicesView(
            workspace=tmp_path,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        execution=AgentExecutionService(bus),
        dependencies=dependencies,
        tool_builder=tool_builder,
    )
    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(agent_id=agent.id, value="module-review", mode="run"),
        run_id=run_id,
    )

    bridge = runtime._bridge(agent, task, context, conversation, task_id="review-r0")

    assert isinstance(bridge, ModuleReviewerAgentBridge)
    assert captured["tool_names"] == list(task.tools)
    assert captured["dependencies"] is dependencies
    assert set(built_registry["value"].get_all()) == set(task.tools)


@pytest.mark.asyncio
async def test_module_provider_tools_assemble_artifact_tools_and_reuse_completed_results(
    tmp_path: Path,
) -> None:
    """Capability composition owns the declared artifact/calculation tools."""

    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        TaskEnvelope,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
        ModuleProviderDependencies,
        build_module_provider_tools,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_provider_tools import (
        CalculateTool as CapabilityCalculateTool,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_provider_tools import (
        InspectImageTool as CapabilityInspectImageTool,
    )
    from manyselves.core.loops.bus import MessageBus
    from manyselves.core.reporting.agent_runner import (
        CalculateTool as LegacyCalculateTool,
    )
    from manyselves.core.reporting.agent_runner import (
        InspectImageTool as LegacyInspectImageTool,
    )
    from manyselves.core.tools.result_memory import RunToolResultIndex

    assert LegacyCalculateTool is CapabilityCalculateTool
    assert LegacyInspectImageTool is CapabilityInspectImageTool

    image_path = tmp_path / "image-ref"
    image_path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001"
            "08060000001f15c4890000000d49444154789c6360f8cf000000"
            "03000100c9fe92ef0000000049454e44ae426082"
        )
    )

    class Gateway:
        def __init__(self) -> None:
            self.open_calls: list[tuple[str, int, int]] = []
            self.search_calls: list[tuple[str, str]] = []

        def describe(self, ref: str) -> SimpleNamespace:
            del ref
            return SimpleNamespace(
                kind="image",
                allowed_operations=("inspect_image",),
            )

        def _resolve(self, ref: str) -> Path:
            del ref
            return image_path

        def open(self, ref: str, *, offset: int, limit: int) -> SimpleNamespace:
            self.open_calls.append((ref, offset, limit))
            return SimpleNamespace(
                as_dict=lambda: {
                    "ref": ref,
                    "offset": offset,
                    "limit": limit,
                    "content": "artifact content",
                }
            )

        def search(
            self,
            ref: str,
            query: str,
            *,
            max_matches: int,
            context_lines: int,
        ) -> dict[str, Any]:
            del max_matches, context_lines
            self.search_calls.append((ref, query))
            return {"ref": ref, "query": query, "matches": []}

    gateway = Gateway()
    result_index = RunToolResultIndex(tmp_path, "module-provider-tools")
    artifact_access = SimpleNamespace(
        capabilities=(),
        readable_refs=("artifact-ref", "image-ref"),
        photo_map=lambda: {},
    )
    envelope = TaskEnvelope(
        task_id="module-2.4",
        run_id="module-provider-tools",
        agent_id="module-2.4-specialist",
        objective="assemble declared tools",
        allowed_outputs=["module_submission"],
    )
    registry = build_module_provider_tools(
        tmp_path,
        envelope=envelope,
        module_id="2.4",
        session_id="module-session",
        workflow_id="public-reporting",
        bus=MessageBus(),
        store=ReportingStore(tmp_path),
        global_knowledge_root=None,
        tool_names=("inspect_image", "calculate", "open_artifact", "search_text"),
        expected_part_ids=(),
        dependencies=ModuleProviderDependencies(
            artifact_gateway=gateway,
            artifact_access=artifact_access,
            result_index=result_index,
        ),
    )

    assert set(registry.get_all()) == {
        "inspect_image",
        "calculate",
        "open_artifact",
        "search_text",
    }
    calculate = registry.get("calculate")
    inspect_image = registry.get("inspect_image")
    open_artifact = registry.get("open_artifact")
    search_text = registry.get("search_text")
    assert calculate is not None
    assert inspect_image is not None
    assert open_artifact is not None
    assert search_text is not None
    assert (await calculate(expression="2 + 3"))["result"] == 5
    inspected = await inspect_image(ref="image-ref")
    assert inspected["path"] == "image-ref"

    first_open = await open_artifact("artifact-ref", limit=20)
    second_open = await open_artifact("artifact-ref", limit=20)
    assert first_open == second_open
    assert gateway.open_calls == [("artifact-ref", 0, 20)]

    first_search = await search_text("artifact-ref", "evidence")
    second_search = await search_text("artifact-ref", "evidence")
    assert first_search == second_search
    assert gateway.search_calls == [("artifact-ref", "evidence")]


def test_module_provider_composes_scoped_artifact_access_per_prepared_task(
    tmp_path: Path,
) -> None:
    """The Provider bridge derives per-task artifact resources from the envelope."""

    from manyselves.application.runtime_services import RuntimeServicesView
    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )
    from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
        REPORT_TAXONOMY,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        TaskEnvelope,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        ModuleAuthoringInput,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleAuthoringPreparation,
        DeclarativeModuleRuntimeLaneContext,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
        ModuleProviderRuntime,
    )
    from manyselves.config.schema import AgentDefaults
    from manyselves.core.artifacts.gateway import ArtifactGateway, ArtifactGrant
    from manyselves.core.loops.bus import MessageBus
    from manyselves.core.tools.registry import ToolRegistry
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import DefinitionKind
    from manyselves.runtime.agent_execution import AgentExecutionService

    _capability, registry = load_distribution_reporting_capability()
    agent = registry.require(DefinitionKind.AGENT, "module-2.4-specialist")
    task = registry.require(DefinitionKind.TASK, "module-2.4-authoring")
    part_ids = list(REPORT_TAXONOMY["2.4"].submodules)

    def make_context(run_id: str, task_id: str) -> DeclarativeModuleRuntimeLaneContext:
        refs = {
            "coverage": f"Work/runs/{run_id}/preparation/coverage.json",
            "evidence": f"Work/runs/{run_id}/preparation/evidence.jsonl",
            "manifest": f"Work/runs/{run_id}/preparation/manifest.json",
            "knowledge": f"Work/runs/{run_id}/knowledge/module-2.4.md",
        }
        for name, ref in refs.items():
            path = tmp_path / ref
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{name}-{run_id}\n", encoding="utf-8")
        contract_ref = f"Work/runs/{run_id}/context/module-input.json"
        contract = ModuleAuthoringInput(
            run_id=run_id,
            module_id="2.4",
            revision=0,
            required_submodule_ids=part_ids,
            coverage_ref=refs["coverage"],
            evidence_ref=refs["evidence"],
            manifest_ref=refs["manifest"],
            knowledge_ref=refs["knowledge"],
        )
        contract_path = tmp_path / contract_ref
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(contract.model_dump_json(), encoding="utf-8")
        envelope = TaskEnvelope(
            task_id=task_id,
            run_id=run_id,
            agent_id=agent.id,
            objective=task.objective,
            allowed_outputs=["module_submission"],
            allowed_tools=list(task.tools),
            input_refs=[contract_ref, *refs.values()],
            target_submodule_ids=part_ids,
            input_contract_kind="module_authoring_input",
            input_contract_ref=contract_ref,
        )
        return DeclarativeModuleRuntimeLaneContext(
            module_id="2.4",
            workflow_id="public-reporting",
            reporting_state={"run_id": run_id},
            status="author_ready",
            authoring=DeclarativeModuleAuthoringPreparation(
                specialist_id=agent.id,
                envelope=envelope,
                revision=0,
                review=False,
                checkpoint=False,
            ),
        )

    captured: list[dict[str, Any]] = []

    def tool_builder(*args: Any, **kwargs: Any) -> ToolRegistry:
        del args
        captured.append(kwargs)
        return ToolRegistry()

    bus = MessageBus()
    runtime = ModuleProviderRuntime(
        RuntimeServicesView(
            workspace=tmp_path,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        execution=AgentExecutionService(bus),
        tool_builder=tool_builder,
    )
    conversations = ConversationRegistry()
    contexts = (
        make_context("module-provider-run-a", "module-2.4-task-a"),
        make_context("module-provider-run-b", "module-2.4-task-b"),
    )
    for context, run_id in zip(contexts, ("module-provider-run-a", "module-provider-run-b")):
        conversation = conversations.create_or_resolve(
            ConversationKey(agent_id=agent.id, value=run_id, mode="run"),
            run_id=run_id,
        )
        runtime._bridge(
            agent,
            task,
            context,
            conversation,
            task_id=f"dispatch-{run_id}",
        )

    assert len(captured) == 2
    first, second = (item["dependencies"] for item in captured)
    assert isinstance(first.artifact_gateway, ArtifactGateway)
    assert isinstance(second.artifact_gateway, ArtifactGateway)
    assert first.artifact_gateway.grant == ArtifactGrant(
        "public-reporting",
        "module-2.4-task-a",
        agent.id,
        "public-reporting:module-provider-run-a",
    )
    assert second.artifact_gateway.grant == ArtifactGrant(
        "public-reporting",
        "module-2.4-task-b",
        agent.id,
        "public-reporting:module-provider-run-b",
    )
    assert first.artifact_access.gateway is first.artifact_gateway
    assert second.artifact_access.gateway is second.artifact_gateway
    assert first.result_index is not second.result_index
    assert first.result_index.run_id == "module-provider-run-a"
    assert second.result_index.run_id == "module-provider-run-b"
    assert first.result_index.path != second.result_index.path
    assert set(task.tools).issubset(set(captured[0]["tool_names"]))
    assert first.artifact_access.readable_refs
    assert second.artifact_access.readable_refs
