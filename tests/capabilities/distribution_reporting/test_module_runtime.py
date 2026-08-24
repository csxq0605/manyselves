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


def test_module_runtime_exposes_all_declared_lifecycle_ports(
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
    assert missing == []


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


@pytest.mark.parametrize(
    ("operation", "target_modules"),
    (
        ("module_report", ["2.4"]),
        ("full_report", ["2.1", "2.2", "2.3", "2.4", "2.5"]),
    ),
)
def test_public_runtime_binds_all_declared_module_lane_ports(
    tmp_path: Path,
    operation: str,
    target_modules: list[str],
) -> None:
    """Characterize the eight module adapters required by both public roots."""

    from manyselves.capabilities.distribution_reporting.runtime.public_reporting import (
        PublicReportingWorkflowRuntime,
    )

    module_runtime, _execution, _session_factory, _agent_invokers = _build_runtime(tmp_path)
    public_runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot=lambda _run_id: {},
        snapshot_content=lambda source, target: (target, "a" * 64, source),
        runtime_photo_ids=lambda _evidence, _photos: None,
        module_runtime=module_runtime,
    )
    request = ReportRequest(
        operation=operation,
        instruction="验证 public module lane ports",
        target_modules=target_modules,
        missing_evidence_policy="draft",
        preparation_mode="serial",
    )
    implementations = public_runtime._module_tool_implementations()
    expected = {
        "resume-current-module-review",
        "resume-current-module-recheck",
        "prepare-current-module-main-exception",
        "module-main-exception-requires-agent",
        "accept-current-module-main-exception",
        "module-main-exception-requests-user",
        "apply-current-module-main-exception-user-input",
        "route-current-module-after-main-exception",
    }

    assert public_runtime._workflow_id(request, None) == (
        "full-report" if operation == "full_report" else "module-report"
    )
    assert expected.issubset(set(implementations))


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


def test_module_provider_composition_exposes_real_runtime_ports_and_injected_dependencies(
    tmp_path: Path,
) -> None:
    """Characterize the missing Provider composition before implementation."""

    from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
        ModuleProviderDependencies,
        build_module_provider_composition,
    )
    from manyselves.config.schema import AgentDefaults
    from manyselves.core.loops.bus import MessageBus
    from manyselves.runtime.agent_execution import AgentExecutionService
    from manyselves.runtime.services import RuntimeServicesView

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

    from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
        build_module_provider_composition,
    )
    from manyselves.capabilities.distribution_reporting.runtime.public_reporting import (
        PublicReportingWorkflowRuntime,
    )
    from manyselves.config.schema import AgentDefaults
    from manyselves.core.loops.bus import MessageBus
    from manyselves.runtime.services import RuntimeServicesView

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


def test_module_provider_selects_envelope_for_current_declared_task() -> None:
    """Accumulated lane state must not bind a revision to the prior review."""

    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        TaskEnvelope,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleAuthoringPreparation,
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
    from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
        ModuleProviderRuntime,
    )
    from manyselves.kernel.definitions import DefinitionKind

    _capability, registry = load_distribution_reporting_capability()
    author_task = registry.require(DefinitionKind.TASK, "module-2.4-authoring")
    review_task = registry.require(DefinitionKind.TASK, "module-runtime-initial-review")
    revision_task = registry.require(
        DefinitionKind.TASK,
        "module-2.4-runtime-revision",
    )
    recheck_task = registry.require(DefinitionKind.TASK, "module-runtime-recheck")
    run_id = "module-provider-current-envelope"

    def envelope(task_id: str, agent_id: str, output: str) -> TaskEnvelope:
        return TaskEnvelope(
            task_id=task_id,
            run_id=run_id,
            agent_id=agent_id,
            objective=task_id,
            allowed_outputs=[output],
        )

    author_envelope = envelope(
        "module-2.4-authoring-r0",
        "module-2.4-specialist",
        "module_submission",
    )
    review_envelope = envelope(
        "module-2.4-initial-review-r0",
        "evidence-auditor",
        "module_review_finding_submission",
    )
    revision_envelope = envelope(
        "module-revision-r1-2.4",
        "module-2.4-specialist",
        "module_revision_submission",
    )
    recheck_envelope = envelope(
        "module-2.4-initial-review-r1",
        "evidence-auditor",
        "module_review_verdict_submission",
    )
    context = DeclarativeModuleRuntimeLaneContext.model_construct(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": run_id},
        status="recheck_ready",
        authoring=DeclarativeModuleAuthoringPreparation.model_construct(
            specialist_id="module-2.4-specialist",
            envelope=author_envelope,
            revision=0,
            review=False,
            checkpoint=False,
        ),
        review=DeclarativeModuleReviewPreparation.model_construct(
            envelope=review_envelope,
            prepared=ModuleInitialReviewPreparation.model_construct(
                envelope=review_envelope,
            ),
        ),
        revision=DeclarativeModuleRevisionPreparation.model_construct(
            prepared=ModuleRevisionPreparation.model_construct(
                envelope=revision_envelope,
            ),
        ),
        recheck=DeclarativeModuleRecheckPreparation.model_construct(
            prepared=ModuleRecheckPreparation.model_construct(
                envelope=recheck_envelope,
            ),
        ),
    )

    assert ModuleProviderRuntime._envelope(context, author_task) is author_envelope
    assert ModuleProviderRuntime._envelope(context, review_task) is review_envelope
    assert ModuleProviderRuntime._envelope(context, revision_task) is revision_envelope
    assert ModuleProviderRuntime._envelope(context, recheck_task) is recheck_envelope
    assert ModuleAuthoringAgentBridge._envelope(context, author_task) is author_envelope
    assert ModuleAuthoringAgentBridge._envelope(context, revision_task) is revision_envelope


@pytest.mark.asyncio
async def test_module_provider_runtime_builds_declared_tools_and_reuses_conversation_session(
    tmp_path: Path,
) -> None:
    """Provider composition uses the generic service for both Author turns."""

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
    from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
        TaskCorrelation,
    )
    from manyselves.config.schema import AgentDefaults
    from manyselves.core.loops.bus import MessageBus
    from manyselves.core.tools.registry import Tool
    from manyselves.interfaces.types import AgentResultMessage, UserMessage
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import DefinitionKind
    from manyselves.runtime.agent_execution import AgentExecutionService
    from manyselves.runtime.services import RuntimeServicesView

    _capability, registry = load_distribution_reporting_capability()
    agent = registry.require(DefinitionKind.AGENT, "module-2.4-specialist")
    task = registry.require(DefinitionKind.TASK, "module-2.4-authoring")
    run_id = "module-provider-runtime"
    part_ids = list(REPORT_TAXONOMY["2.4"].submodules)
    envelope = TaskEnvelope(
        task_id="module-2.4",
        task_attempt_id="attempt-module-provider",
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
    task_correlation = TaskCorrelation(
        workflow_id="public-reporting",
        run_id=run_id,
        task_id=envelope.task_id,
        task_attempt_id=envelope.task_attempt_id,
        agent_id=agent.id,
        identity_key=agent.id,
        session_id="public-reporting:module-2.4",
        lease_owner_id="focused-module-provider",
        lease_epoch=1,
    )
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
                        task_attempt_id=envelope.task_attempt_id,
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
        assert received[0].task_attempt_id == envelope.task_attempt_id
        assert first.session_id == second.session_id
        assert conversation.external_session_id == first.session_id
        assert len(service.sessions) == 1
    finally:
        await runtime.close()
        bus.shutdown()
        await bus_task


@pytest.mark.asyncio
async def test_module_provider_schema_correction_uses_declared_recovery_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Capability-owned SubmitResultTool reports schema recovery in-session."""

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
        DeclarativeModuleAuthoringPreparation,
        DeclarativeModuleRuntimeLaneContext,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
        ModuleProviderRuntime,
    )
    from manyselves.config.schema import AgentDefaults
    from manyselves.core.loops.bus import MessageBus
    from manyselves.interfaces.types import UserMessage
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import (
        DefinitionKind,
        RecoveryPolicyDefinition,
    )
    from manyselves.kernel.recovery import RecoveryActionKind, RecoveryController
    from manyselves.runtime.agent_execution import AgentExecutionService
    from manyselves.runtime.services import RuntimeServicesView

    _capability, registry = load_distribution_reporting_capability()
    agent = registry.require(DefinitionKind.AGENT, "module-2.4-specialist")
    task = registry.require(DefinitionKind.TASK, "module-2.4-authoring")
    policy = registry.require(DefinitionKind.RECOVERY, task.recovery)
    assert isinstance(policy, RecoveryPolicyDefinition)
    run_id = "module-provider-schema-recovery"
    part_ids = list(REPORT_TAXONOMY["2.4"].submodules)
    envelope = TaskEnvelope(
        task_id="module-2.4",
        run_id=run_id,
        agent_id=agent.id,
        objective=task.objective,
        allowed_outputs=["module_submission"],
        allowed_tools=[],
        target_submodule_ids=part_ids,
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
    decisions: list[tuple[str, str]] = []
    original_decide = RecoveryController.decide

    def record_decision(self, event, current_policy, state):
        decision = original_decide(self, event, current_policy, state)
        decisions.append((event.kind.value, decision.action.value))
        return decision

    monkeypatch.setattr(RecoveryController, "decide", record_decision)
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    service = AgentExecutionService(bus, timeout=1)
    corrections: list[dict[str, object]] = []
    loops: list[object] = []

    class SchemaCorrectionLoop:
        def __init__(self, **kwargs: Any) -> None:
            self.runtime_id = str(kwargs["agent_type"])
            self.tools = kwargs["tools"]
            self.callback = None
            loops.append(self)

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
                write_part = self.tools.get("write_result_part")
                submit = self.tools.get("submit_result")
                for part_id in part_ids:
                    await write_part(
                        part_id=part_id,
                        content=f"{part_id} reader-visible analysis",
                        evidence_ids=[],
                    )
                corrections.append(
                    await submit(payload={"kind": "module_submission"})
                )
                await submit(
                    kind="module_submission",
                    module_id="2.4",
                    unresolved_questions=[],
                    revision=0,
                )

            self.callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self.callback is not None:
                bus.unsubscribe(UserMessage, self.callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    runtime = ModuleProviderRuntime(
        RuntimeServicesView(
            workspace=tmp_path,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        execution=service,
        loop_builder=SchemaCorrectionLoop,
    )
    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(agent_id=agent.id, value="module-2.4", mode="run"),
        run_id=run_id,
    )
    try:
        outcome = await runtime.invoke_with_recovery(
            agent,
            task,
            context,
            conversation,
            task_id="dispatch-module-2.4",
            recovery_policy=policy,
        )
    finally:
        await runtime.close()
        bus.shutdown()
        await bus_task

    assert outcome.status == "ok"
    assert corrections[0]["status"] == "correction_required"
    assert decisions == [
        ("invalid_structured_output", RecoveryActionKind.CORRECT.value)
    ]
    assert len(loops) == 1


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


@pytest.mark.asyncio
async def test_module_reviewer_bridge_matches_nonempty_provider_task_attempt_id(
    tmp_path: Path,
) -> None:
    """Reviewer terminals use the Provider attempt identity, not an empty sentinel."""

    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        AgentResult,
        AgentRunStatus,
        ModuleReviewFindingSubmission,
        TaskEnvelope,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        ModuleReviewInput,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleReviewPreparation,
        DeclarativeModuleRuntimeLaneContext,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.review import (
        ModuleInitialReviewPreparation,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_reviewer_bridge import (
        ModuleReviewerAgentBridge,
    )
    from manyselves.core.loops.bus import MessageBus
    from manyselves.interfaces.types import AgentResultMessage, UserMessage
    from manyselves.kernel.conversations import (
        ConversationKey,
        ConversationMode,
        ConversationRegistry,
    )
    from manyselves.kernel.definitions import DefinitionKind
    from manyselves.runtime.agent_execution import AgentExecutionService

    _capability, registry = load_distribution_reporting_capability()
    reviewer = registry.require(DefinitionKind.AGENT, "evidence-auditor")
    review_task = registry.require(DefinitionKind.TASK, "module-runtime-initial-review")
    run_id = "module-reviewer-provider-attempt"
    provider_task_attempt_id = "attempt-module-reviewer-provider"
    envelope = TaskEnvelope(
        task_id="module-2.4-initial-review-r0",
        task_attempt_id=provider_task_attempt_id,
        run_id=run_id,
        agent_id=reviewer.id,
        objective=review_task.objective,
        allowed_outputs=["module_review_finding_submission"],
    )
    context = DeclarativeModuleRuntimeLaneContext.model_construct(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": run_id},
        status="review_ready",
        review=DeclarativeModuleReviewPreparation.model_construct(
            envelope=envelope,
            prepared=ModuleInitialReviewPreparation.model_construct(
                envelope=envelope,
                review_input=ModuleReviewInput.model_construct(),
            ),
        ),
    )
    result_ref = f"Work/runs/{run_id}/results/reviewer.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True)
    payload = ModuleReviewFindingSubmission(
        coverage={"submodule_ids": ["2.4.1.1"]},
        findings=[],
    )

    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    service = AgentExecutionService(bus, timeout=1)
    received: list[UserMessage] = []

    class ProviderLoop:
        def __init__(self, **kwargs: Any) -> None:
            self.runtime_id = str(kwargs["agent_type"])
            self.callback = None

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
                received.append(message)
                result_path.write_text(
                    AgentResult(
                        task_id=envelope.task_id,
                        run_id=run_id,
                        agent_id=envelope.agent_id,
                        session_id=message.session_id,
                        status=AgentRunStatus.COMPLETED,
                        payload=payload,
                    ).model_dump_json(),
                    encoding="utf-8",
                )
                await bus.publish(
                    AgentResultMessage(
                        sender=envelope.agent_id,
                        workflow_id=message.workflow_id,
                        task_id=envelope.task_id,
                        run_id=run_id,
                        result_path=result_ref,
                        task_attempt_id=provider_task_attempt_id,
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

    try:
        bridge = ModuleReviewerAgentBridge(
            tmp_path,
            execution=service,
            session_factory=lambda _runtime_id: ProviderLoop(
                agent_type=_runtime_id,
            ),
            terminal_task_attempt_id=provider_task_attempt_id,
        )
        conversation = ConversationRegistry().create_or_resolve(
            ConversationKey(
                agent_id=reviewer.id,
                value="module-reviewer-provider-attempt",
                mode=ConversationMode.RUN,
            ),
            run_id=run_id,
        )
        outcome = await bridge.invoke(
            reviewer,
            review_task,
            context,
            conversation,
            task_id="host-action-reviewer",
        )
    finally:
        await service.close_workflow("public-reporting")
        bus.shutdown()
        await bus_task

    assert outcome.status == "ok"
    assert received[0].task_attempt_id == provider_task_attempt_id


@pytest.mark.asyncio
async def test_module_reviewer_bridge_reuses_completed_result_before_provider_session(
    tmp_path: Path,
) -> None:
    """A completed Reviewer result is returned without creating a Provider session."""

    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        AgentResult,
        AgentRunStatus,
        ModuleReviewFindingSubmission,
        TaskEnvelope,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        ModuleReviewInput,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleReviewPreparation,
        DeclarativeModuleRuntimeLaneContext,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.review import (
        ModuleInitialReviewPreparation,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_reviewer_bridge import (
        ModuleReviewerAgentBridge,
    )
    from manyselves.core.loops.bus import MessageBus
    from manyselves.kernel.conversations import (
        ConversationKey,
        ConversationMode,
        ConversationRegistry,
    )
    from manyselves.kernel.definitions import DefinitionKind
    from manyselves.runtime.agent_execution import AgentExecutionService

    _capability, registry = load_distribution_reporting_capability()
    reviewer = registry.require(DefinitionKind.AGENT, "evidence-auditor")
    review_task = registry.require(DefinitionKind.TASK, "module-runtime-initial-review")
    run_id = "module-reviewer-completed-reuse"
    envelope = TaskEnvelope(
        task_id="module-2.4-initial-review-r0",
        task_attempt_id="attempt-module-reviewer-completed",
        run_id=run_id,
        agent_id=reviewer.id,
        objective=review_task.objective,
        allowed_outputs=["module_review_finding_submission"],
    )
    context = DeclarativeModuleRuntimeLaneContext.model_construct(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": run_id},
        status="review_ready",
        review=DeclarativeModuleReviewPreparation.model_construct(
            envelope=envelope,
            prepared=ModuleInitialReviewPreparation.model_construct(
                envelope=envelope,
                review_input=ModuleReviewInput.model_construct(),
            ),
        ),
    )
    session_id = "persisted-module-review-session"
    persisted = AgentResult(
        task_id=envelope.task_id,
        run_id=run_id,
        agent_id=reviewer.id,
        session_id=session_id,
        status=AgentRunStatus.COMPLETED,
        payload=ModuleReviewFindingSubmission(
            coverage={"submodule_ids": ["2.4.1.1"]},
            findings=[],
        ),
    )
    bus = MessageBus()
    service = AgentExecutionService(bus, timeout=1)
    provider_calls = 0

    def forbidden_session(_runtime_id: str) -> Any:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("completed Reviewer result must not start a Provider session")

    try:
        bridge = ModuleReviewerAgentBridge(
            tmp_path,
            execution=service,
            session_factory=forbidden_session,
            completed_result_loader=lambda: persisted,
        )
        conversation = ConversationRegistry().create_or_resolve(
            ConversationKey(
                agent_id=reviewer.id,
                value="module-reviewer-completed-reuse",
                mode=ConversationMode.RUN,
            ),
            run_id=run_id,
        )
        outcome = await bridge.invoke(
            reviewer,
            review_task,
            context,
            conversation,
            task_id="host-action-reviewer-completed",
        )
    finally:
        await service.close_workflow("public-reporting")
        bus.shutdown()

    assert outcome.status == "ok"
    assert outcome.session_id == session_id
    assert outcome.result["status"] == "completed"
    assert outcome.result["submission"]["kind"] == "module_review_finding_submission"
    assert provider_calls == 0
    assert service.sessions == {}


def test_module_provider_runtime_selects_reviewer_bridge_for_declared_review_task(
    tmp_path: Path,
) -> None:
    """Reviewer composition keeps its typed bridge and full declared tool set."""

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
    from manyselves.runtime.services import RuntimeServicesView

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
    from manyselves.core.loops.bus import MessageBus
    from manyselves.core.tools.result_memory import RunToolResultIndex

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


@pytest.mark.asyncio
async def test_module_revision_result_part_persists_existing_evidence_binding(
    tmp_path: Path,
) -> None:
    """Revision prose uses the same evidence-bound result-part contract as authoring."""

    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        TaskEnvelope,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
        ModuleProviderDependencies,
        build_module_provider_tools,
    )
    from manyselves.capabilities.distribution_reporting.runtime.source_ledger import (
        SourceLedger,
    )
    from manyselves.core.loops.bus import MessageBus

    run_id = "module-provider-revision-result-part"
    task_id = "module-revision-r1-2.4"
    part_id = "2.4.1.1"
    SourceLedger(tmp_path, run_id).register_project(
        "E-0001",
        "现场证据",
        "Knowledge/evidence.txt",
        "设备状态记录",
    )
    envelope = TaskEnvelope(
        task_id=task_id,
        run_id=run_id,
        agent_id="module-2.4-specialist",
        objective="revise one assigned module part",
        allowed_outputs=["module_revision_submission"],
        revision=1,
        target_submodule_ids=[part_id],
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
        tool_names=("write_result_part",),
        expected_part_ids=(part_id,),
        dependencies=ModuleProviderDependencies(),
    )

    write_result_part = registry.get("write_result_part")
    assert write_result_part is not None
    result = await write_result_part(
        part_id=part_id,
        content="修订后的完整小节正文。",
        evidence_ids=["E-0001"],
    )

    assert result["persisted"] is True
    binding_path = (
        tmp_path
        / "Work/runs"
        / run_id
        / "drafts"
        / task_id
        / "r1/_evidence"
        / f"{part_id}.json"
    )
    assert json.loads(binding_path.read_text(encoding="utf-8"))["evidence_ids"] == [
        "E-0001"
    ]


def test_module_provider_composes_scoped_artifact_access_per_prepared_task(
    tmp_path: Path,
) -> None:
    """The Provider bridge derives per-task artifact resources from the envelope."""

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
    from manyselves.runtime.services import RuntimeServicesView

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


def test_module_revision_provider_uses_file_declared_task_tools(
    tmp_path: Path,
) -> None:
    """A persisted revision envelope cannot hide tools declared by its YAML task."""

    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        TaskEnvelope,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleRevisionPreparation,
        DeclarativeModuleRuntimeLaneContext,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.review import (
        ModuleRevisionPreparation,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
        ModuleProviderRuntime,
    )
    from manyselves.config.schema import AgentDefaults
    from manyselves.core.loops.bus import MessageBus
    from manyselves.core.tools.registry import ToolRegistry
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import DefinitionKind
    from manyselves.runtime.agent_execution import AgentExecutionService
    from manyselves.runtime.services import RuntimeServicesView

    _capability, registry = load_distribution_reporting_capability()
    agent = registry.require(DefinitionKind.AGENT, "module-2.4-specialist")
    task = registry.require(DefinitionKind.TASK, "module-2.4-runtime-revision")
    run_id = "module-provider-revision-task-tools"
    subject_ref = f"Work/runs/{run_id}/modules/2.4-r0.json"
    subject_path = tmp_path / subject_ref
    subject_path.parent.mkdir(parents=True, exist_ok=True)
    subject_path.write_text("{}", encoding="utf-8")
    envelope = TaskEnvelope(
        task_id="module-revision-r1-2.4",
        run_id=run_id,
        agent_id=agent.id,
        objective=task.objective,
        allowed_outputs=["module_revision_submission"],
        allowed_tools=["submit_result"],
        revision=1,
        input_refs=[subject_ref],
        prior_result_ref=subject_ref,
        artifact_delivery_modes={subject_ref: "reference"},
        target_submodule_ids=["2.4.1.1"],
    )
    context = DeclarativeModuleRuntimeLaneContext.model_construct(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": run_id},
        status="revision_ready",
        revision=DeclarativeModuleRevisionPreparation.model_construct(
            prepared=ModuleRevisionPreparation.model_construct(
                envelope=envelope,
                run_id=run_id,
                module_id="2.4",
                workflow_id="public-reporting",
            )
        ),
    )
    captured: dict[str, Any] = {}

    def tool_builder(*args: Any, **kwargs: Any) -> ToolRegistry:
        del args
        captured.update(kwargs)
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
    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(agent_id=agent.id, value="module-2.4", mode="run"),
        run_id=run_id,
    )

    runtime._bridge(
        agent,
        task,
        context,
        conversation,
        task_id="invoke-current-module-revision",
    )

    compiled_tools = set(captured["dependencies"].artifact_access.tool_names)
    assert {"write_result_part", "list_result_parts", "submit_result"}.issubset(
        compiled_tools
    )
    assert captured["tool_names"] == list(
        captured["dependencies"].artifact_access.tool_names
    )


@pytest.mark.parametrize(
    (
        "case_name",
        "marker",
        "continuation_kind",
        "event_name",
        "prompt_fragment",
    ),
    (
        (
            "natural-language",
            "已完成分析，但尚未提交结构化结果。",
            "submission_correction",
            "natural_language_without_submission",
            "submission_correction",
        ),
        (
            "max-tokens",
            "AGENT_MAX_TOKENS_CONTINUATION_REQUIRED",
            "max_tokens_continuation",
            "max_tokens",
            "max_tokens",
        ),
        (
            "tool-slice",
            "AGENT_TURN_CONTINUATION_REQUIRED",
            "tool_slice_continuation",
            "tool_slice_boundary",
            "tool_slice",
        ),
    ),
)
@pytest.mark.asyncio
async def test_module_author_bridge_recovers_markers_in_same_session(
    tmp_path: Path,
    case_name: str,
    marker: str,
    continuation_kind: str,
    event_name: str,
    prompt_fragment: str,
) -> None:
    """Author marker recovery keeps one session and reaches typed completion."""

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
    from manyselves.capabilities.distribution_reporting.runtime.module_agent_bridge import (
        ModuleAuthoringAgentBridge,
    )
    from manyselves.core.loops.agent_loop import (
        AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
        AGENT_TURN_CONTINUATION_REQUIRED,
    )
    from manyselves.core.loops.bus import MessageBus
    from manyselves.interfaces.types import AgentResponse, AgentResultMessage, UserMessage
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import (
        AgentDefinition,
        RecoveryPolicyDefinition,
        RecoveryRule,
        TaskDefinition,
    )
    from manyselves.runtime.agent_execution import AgentExecutionService

    run_id = f"module-author-recovery-{case_name}"
    agent = AgentDefinition(
        id="module-2.4-specialist",
        version="1.0.0",
        description="module author",
        instructions="author the module",
        accepts=["declarative_module_runtime_lane_context"],
        produces=["declarative_module_authoring_agent_result"],
    )
    task = TaskDefinition(
        id="module-2.4-authoring",
        version="1.0.0",
        description="author task",
        agent=agent.id,
        objective="Author module 2.4",
        input_contract="declarative_module_runtime_lane_context",
        output_contract="declarative_module_authoring_agent_result",
    )
    part_ids = list(REPORT_TAXONOMY["2.4"].submodules)
    envelope = TaskEnvelope(
        task_id=f"{run_id}-task",
        run_id=run_id,
        agent_id=agent.id,
        objective=task.objective,
        allowed_outputs=["module_submission"],
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
    result_path.parent.mkdir(parents=True, exist_ok=True)
    submission = ModuleSubmission(
        module_id="2.4",
        submodule_narratives={part_id: f"正文 {part_id}" for part_id in part_ids},
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )

    marker_content = {
        "AGENT_MAX_TOKENS_CONTINUATION_REQUIRED": AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
        "AGENT_TURN_CONTINUATION_REQUIRED": AGENT_TURN_CONTINUATION_REQUIRED,
    }.get(marker, marker)

    class ScriptedAuthorLoop:
        def __init__(self, runtime_id: str) -> None:
            self.runtime_id = runtime_id
            self.received: list[UserMessage] = []
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
                if message.turn_kind == "task_initial":
                    await bus.publish(
                        AgentResponse(
                            agent_type=self.runtime_id,
                            message_id=message.message_id,
                            content=marker_content,
                            internal=marker.startswith("AGENT_"),
                            workflow_id=message.workflow_id,
                            run_id=message.run_id,
                            task_id=message.task_id,
                            task_attempt_id=message.task_attempt_id,
                            session_id=message.session_id,
                        )
                    )
                    return
                if message.turn_kind != continuation_kind:
                    await bus.publish(
                        AgentResponse(
                            agent_type=self.runtime_id,
                            message_id=message.message_id,
                            content="unexpected continuation",
                            workflow_id=message.workflow_id,
                            run_id=message.run_id,
                            task_id=message.task_id,
                            task_attempt_id=message.task_attempt_id,
                            session_id=message.session_id,
                        )
                    )
                    return
                result_path.write_text(
                    AgentResult(
                        task_id=envelope.task_id,
                        run_id=envelope.run_id,
                        agent_id=envelope.agent_id,
                        session_id=message.session_id,
                        status=AgentRunStatus.COMPLETED,
                        payload=submission,
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

            self._callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self._callback is not None:
                bus.unsubscribe(UserMessage, self._callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(
            agent_id=agent.id,
            value=f"module-author-{case_name}",
            mode="run",
        ),
        run_id=run_id,
    )
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    service = AgentExecutionService(bus, timeout=1)
    loops: list[ScriptedAuthorLoop] = []

    def session_factory(runtime_id: str) -> ScriptedAuthorLoop:
        loop = ScriptedAuthorLoop(runtime_id)
        loops.append(loop)
        return loop

    bridge = ModuleAuthoringAgentBridge(
        tmp_path,
        execution=service,
        session_factory=session_factory,
    )
    try:
        outcome = await bridge.invoke_with_recovery(
            agent,
            task,
            context,
            conversation,
            task_id=f"dispatch-{case_name}",
            recovery_policy=RecoveryPolicyDefinition(
                id=f"module-{case_name}-recovery",
                version="1.0.0",
                description="recover declared AgentLoop boundary",
                rules={
                    event_name: RecoveryRule(
                        action=(
                            "correct"
                            if event_name == "natural_language_without_submission"
                            else "continue"
                        )
                    )
                },
            ),
        )
    finally:
        await service.close_workflow("public-reporting")
        bus.shutdown()
        await bus_task

    assert outcome.status == "ok"
    assert outcome.result["status"] == "completed"
    assert ModuleSubmission.model_validate(outcome.result["module"]).module_id == "2.4"
    assert len(loops) == 1
    assert [message.turn_kind for message in loops[0].received] == [
        "task_initial",
        continuation_kind,
    ]
    assert loops[0].received[0].session_id == loops[0].received[1].session_id
    assert loops[0].received[1].internal is True
    assert prompt_fragment in loops[0].received[1].content
    assert outcome.session_id == conversation.external_session_id


@pytest.mark.asyncio
async def test_module_provider_reuses_persisted_completed_result_before_provider(
    tmp_path: Path,
) -> None:
    """The real Module Provider reuses a durable typed result before a session."""

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
    from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
        IdentityLeaseManager,
        TaskAttemptStore,
        TaskCorrelation,
    )
    from manyselves.config.schema import AgentDefaults
    from manyselves.core.loops.bus import MessageBus
    from manyselves.core.tools.registry import ToolRegistry
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import (
        DefinitionKind,
        RecoveryPolicyDefinition,
        RecoveryRule,
    )
    from manyselves.runtime.agent_execution import AgentExecutionService
    from manyselves.runtime.services import RuntimeServicesView

    _capability, registry = load_distribution_reporting_capability()
    agent = registry.require(DefinitionKind.AGENT, "module-2.4-specialist")
    task = registry.require(DefinitionKind.TASK, "module-2.4-authoring")
    workflow_id = "public-reporting"
    run_id = "module-provider-persisted-reuse"
    session_id = "session-module-persisted"
    part_ids = list(REPORT_TAXONOMY["2.4"].submodules)
    envelope = TaskEnvelope(
        task_id=task.id,
        task_attempt_id="attempt-module-persisted",
        run_id=run_id,
        agent_id=agent.id,
        objective=task.objective,
        allowed_outputs=["module_submission"],
        allowed_tools=list(task.tools),
        target_submodule_ids=part_ids,
    )
    context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.4",
        workflow_id=workflow_id,
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
    submission = ModuleSubmission(
        module_id="2.4",
        submodule_narratives={part_id: f"正文 {part_id}" for part_id in part_ids},
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )
    lease_manager = IdentityLeaseManager(tmp_path, run_id)
    lease_handle = lease_manager.acquire(workflow_id, agent.id)
    correlation = TaskCorrelation(
        workflow_id=workflow_id,
        run_id=run_id,
        task_id=envelope.task_id,
        task_attempt_id=envelope.task_attempt_id,
        agent_id=agent.id,
        identity_key=agent.id,
        session_id=session_id,
        lease_owner_id=lease_handle.lease.owner_id,
        lease_epoch=lease_handle.lease.lease_epoch,
    )
    try:
        store = TaskAttemptStore(tmp_path, run_id)
        store.activate(correlation)
        store.persist_result(
            correlation,
            AgentResult(
                task_id=envelope.task_id,
                run_id=run_id,
                agent_id=agent.id,
                session_id=session_id,
                status=AgentRunStatus.COMPLETED,
                payload=submission,
            ).model_dump(mode="json"),
            status="completed",
        )
    finally:
        lease_handle.release()

    bus = MessageBus()
    service = AgentExecutionService(bus)
    provider_calls = 0

    def forbidden_loop_builder(**_kwargs: Any) -> Any:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("completed result must be reused before Provider session")

    runtime = ModuleProviderRuntime(
        RuntimeServicesView(
            workspace=tmp_path,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        execution=service,
        loop_builder=forbidden_loop_builder,
        dependencies=ModuleProviderDependencies(
            artifact_gateway=object(),
            artifact_access=object(),
            result_index=object(),
            task_correlation=correlation,
        ),
        tool_builder=lambda *_args, **_kwargs: ToolRegistry(),
    )
    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(agent_id=agent.id, value="module-2.4", mode="run"),
        run_id=run_id,
    )
    try:
        outcome = await runtime.invoke_with_recovery(
            agent,
            task,
            context,
            conversation,
            task_id=task.id,
            recovery_policy=RecoveryPolicyDefinition(
                id="module-completed-reuse",
                version="1.0.0",
                description="reuse persisted completed result",
                rules={
                    "completed_tool_result": RecoveryRule(action="reuse_result"),
                },
            ),
        )
    finally:
        await runtime.close()

    assert outcome.status == "ok"
    assert outcome.session_id == session_id
    assert outcome.result["status"] == "completed"
    assert ModuleSubmission.model_validate(outcome.result["module"]).module_id == "2.4"
    assert provider_calls == 0
    assert service.sessions == {}


def _runtime_module_submission(
    module_id: str = "2.4",
    *,
    revision: int = 1,
    revision_responses: list[Any] | None = None,
):
    from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
        REPORT_TAXONOMY,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        ModuleSubmission,
    )

    return ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            part_id: f"当前模块正文 {part_id}"
            for part_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=revision,
        revision_responses=list(revision_responses or []),
    )


def test_module_runtime_restores_promoted_author_result_on_same_run_resume(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        AgentResult,
        AgentRunStatus,
    )

    runtime, _execution, _session_factory, _agent_invokers = _build_runtime(tmp_path)
    run_id = "module-author-promoted-resume"
    submission = _runtime_module_submission(revision=0)
    result_path = tmp_path / f"Work/runs/{run_id}/results/module-2.4.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        AgentResult(
            task_id="module-2.4",
            run_id=run_id,
            agent_id="module-2.4-specialist",
            session_id="public-reporting:specialist-2.4",
            status=AgentRunStatus.COMPLETED,
            payload=submission,
        ).model_dump_json(),
        encoding="utf-8",
    )
    reporting_state = {
        "run_id": run_id,
        "resume": True,
        "request": ReportRequest(
            operation="module_report",
            instruction="resume module 2.4",
            target_modules=["2.4"],
            missing_evidence_policy="draft",
            preparation_mode="serial",
        ),
        "module_dispatch": {"already": "prepared"},
    }

    restored = runtime.prepare_lanes(reporting_state)

    assert restored["specialist_submissions"] == {"2.4": submission}


@pytest.mark.parametrize(
    ("next_action", "expected_status"),
    (("completed", "reviewed"), ("revise", "revision_pending")),
)
def test_module_runtime_resumes_persisted_initial_review_completed_and_revise(
    tmp_path: Path,
    next_action: str,
    expected_status: str,
) -> None:
    """Resume preserves the old completed/revise continuation projection."""

    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleReviewPreparation,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.review import (
        ModuleInitialReviewPreparation,
        ModuleReviewProgress,
    )

    runtime, _execution, _session_factory, _agent_invokers = _build_runtime(tmp_path)
    run_id = f"module-review-resume-{next_action}"
    current = _runtime_module_submission(revision=1)
    subject_ref = f"Work/runs/{run_id}/modules/2.4-r1.json"
    progress = ModuleReviewProgress(
        run_id=run_id,
        module_id="2.4",
        next_action=next_action,
        current=current,
        finding_refs=[f"Work/runs/{run_id}/reviews/findings.json"],
        review_round=0,
        phase="initial",
        scope=["2.4.1.1"],
        reviewer_session_key="module-auditor-2.4",
        last_reviewed_subject_ref=subject_ref,
    )
    prepared = ModuleInitialReviewPreparation(
        mode="continue_existing",
        run_id=run_id,
        module_id="2.4",
        lifecycle_id="initial",
        workflow_id="public-reporting",
        reviewer_session_key="module-auditor-2.4",
        review_root=f"Work/runs/{run_id}/reviews/module/initial/2.4",
        progress_ref=f"Work/runs/{run_id}/reviews/module/initial/2.4/progress.json",
        review_round=0,
        scope=["2.4.1.1"],
        current=current,
        subject_ref=subject_ref,
        progress=progress,
    )
    completion_ref = (
        f"Work/runs/{run_id}/reviews/module/initial/2.4/completion-r1.json"
    )
    context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={
            "run_id": run_id,
            **(
                {"module_review_completion_refs": {"2.4": completion_ref}}
                if next_action == "completed"
                else {}
            ),
        },
        status="review_resumed",
        module=current,
        review=DeclarativeModuleReviewPreparation(
            envelope=None,
            reviewer_session_key="module-auditor-2.4",
            prepared=prepared,
        ),
    )

    resumed = runtime.resume_review_lane(context)

    assert resumed.status == expected_status
    assert resumed.module == current
    assert resumed.review is not None
    assert resumed.review.acceptance is not None
    assert resumed.review.acceptance.next_action == next_action
    assert resumed.review.acceptance.completion_ref == (
        completion_ref if next_action == "completed" else None
    )


@pytest.mark.parametrize(
    ("next_action", "expected_acceptance_action"),
    (("completed", "completed"), ("revise", "continue_existing")),
)
def test_module_runtime_resumes_persisted_recheck_completed_and_revise(
    tmp_path: Path,
    next_action: str,
    expected_acceptance_action: str,
) -> None:
    """Recheck resume keeps completion and pending-revision state typed."""

    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleRecheckPreparation,
        DeclarativeModuleReviewPreparation,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.review import (
        ModuleInitialReviewPreparation,
        ModuleRecheckPreparation,
        ModuleReviewProgress,
    )

    runtime, _execution, _session_factory, _agent_invokers = _build_runtime(tmp_path)
    run_id = f"module-recheck-resume-{next_action}"
    current = _runtime_module_submission(revision=1)
    subject_ref = f"Work/runs/{run_id}/modules/2.4-r1.json"
    progress = ModuleReviewProgress(
        run_id=run_id,
        module_id="2.4",
        next_action=next_action,
        current=current,
        finding_refs=[f"Work/runs/{run_id}/reviews/findings.json"],
        review_round=1,
        phase="recheck",
        scope=["2.4.1.1"],
        reviewer_session_key="module-auditor-2.4",
        last_reviewed_subject_ref=subject_ref,
    )
    initial_prepared = ModuleInitialReviewPreparation(
        mode="continue_existing",
        run_id=run_id,
        module_id="2.4",
        lifecycle_id="initial",
        workflow_id="public-reporting",
        reviewer_session_key="module-auditor-2.4",
        review_root=f"Work/runs/{run_id}/reviews/module/initial/2.4",
        progress_ref=f"Work/runs/{run_id}/reviews/module/initial/2.4/progress.json",
        review_round=0,
        scope=["2.4.1.1"],
        current=current,
        subject_ref=subject_ref,
    )
    recheck_prepared = ModuleRecheckPreparation(
        mode="continue_existing",
        run_id=run_id,
        module_id="2.4",
        lifecycle_id="initial",
        workflow_id="public-reporting",
        reviewer_session_key="module-auditor-2.4",
        review_root=f"Work/runs/{run_id}/reviews/module/initial/2.4",
        progress_ref=f"Work/runs/{run_id}/reviews/module/initial/2.4/progress.json",
        review_round=1,
        scope=["2.4.1.1"],
        current=current,
        subject_ref=subject_ref,
        progress=progress,
    )
    completion_ref = (
        f"Work/runs/{run_id}/reviews/module/initial/2.4/completion-r1.json"
    )
    context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={
            "run_id": run_id,
            **(
                {"module_review_completion_refs": {"2.4": completion_ref}}
                if next_action == "completed"
                else {}
            ),
        },
        status="review_resumed",
        module=current,
        review=DeclarativeModuleReviewPreparation(
            envelope=None,
            reviewer_session_key="module-auditor-2.4",
            prepared=initial_prepared,
        ),
        recheck=DeclarativeModuleRecheckPreparation(prepared=recheck_prepared),
    )

    resumed = runtime.resume_recheck_lane(context)

    assert resumed.status == "reviewed"
    assert resumed.module == current
    assert resumed.review is not None
    assert resumed.review.acceptance is not None
    assert resumed.review.acceptance.next_action == expected_acceptance_action
    assert resumed.recheck is not None
    assert resumed.recheck.acceptance is not None
    assert resumed.recheck.acceptance.next_action == expected_acceptance_action
    assert resumed.recheck.acceptance.completion_ref == (
        completion_ref if next_action == "completed" else None
    )


def _author_exception_context(run_id: str) -> Any:
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        RevisionResponse,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleReviewPreparation,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.review import (
        ModuleInitialReviewAcceptance,
        ModuleInitialReviewPreparation,
    )

    response = RevisionResponse(
        finding_id="F-author",
        action="disputed",
        summary="作者明确说明该 finding 不应改变当前正文范围。",
    )
    current = _runtime_module_submission(revision=1, revision_responses=[response])
    subject_ref = f"Work/runs/{run_id}/modules/2.4-r1.json"
    prepared = ModuleInitialReviewPreparation(
        mode="invoke_agent",
        run_id=run_id,
        module_id="2.4",
        lifecycle_id="initial",
        workflow_id="public-reporting",
        reviewer_session_key="module-auditor-2.4",
        review_root=f"Work/runs/{run_id}/reviews/module/initial/2.4",
        progress_ref=f"Work/runs/{run_id}/reviews/module/initial/2.4/progress.json",
        review_round=0,
        scope=["2.4.1.1"],
        current=current,
        subject_ref=subject_ref,
    )
    acceptance = ModuleInitialReviewAcceptance(
        run_id=run_id,
        module_id="2.4",
        lifecycle_id="initial",
        reviewer_session_key="module-auditor-2.4",
        subject_ref=subject_ref,
        current=current,
        finding_refs=[f"Work/runs/{run_id}/reviews/findings.json"],
        next_action="revise",
        progress_ref=prepared.progress_ref,
    )
    return DeclarativeModuleRuntimeLaneContext(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": run_id},
        status="author_exception_deferred",
        module=current,
        review=DeclarativeModuleReviewPreparation(
            envelope=None,
            reviewer_session_key=prepared.reviewer_session_key,
            prepared=prepared,
            acceptance=acceptance,
        ),
    )


def _reviewer_exception_context(run_id: str) -> Any:
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        ModuleReviewFinding,
        ModuleReviewVerdictSubmission,
        ResolutionVerdict,
        RevisionResponse,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleRecheckPreparation,
        DeclarativeModuleReviewPreparation,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.review import (
        ModuleInitialReviewAcceptance,
        ModuleInitialReviewPreparation,
        ModuleRecheckPreparation,
    )

    response = RevisionResponse(
        finding_id="F-reviewer",
        action="implemented",
        summary="作者已经完成目标小节的修订并补充了对应的验证说明。",
        changed_target_ids=["2.4.1.1"],
    )
    current = _runtime_module_submission(revision=1, revision_responses=[response])
    subject_ref = f"Work/runs/{run_id}/modules/2.4-r1.json"
    finding = ModuleReviewFinding(
        id="F-reviewer",
        target_submodule_id="2.4.1.1",
        category="analysis_depth",
        impact="blocking",
        observation="当前修订仍未给出可复核的分析边界和对应证据链。",
        evidence_refs=["E-1"],
        required_change="补充可复核的分析边界、证据链和对应的验证步骤。",
        reviewer_checks=["确认分析边界和证据链已经补充并可复核。"],
    )
    initial_prepared = ModuleInitialReviewPreparation(
        mode="invoke_agent",
        run_id=run_id,
        module_id="2.4",
        lifecycle_id="initial",
        workflow_id="public-reporting",
        reviewer_session_key="module-auditor-2.4",
        review_root=f"Work/runs/{run_id}/reviews/module/initial/2.4",
        progress_ref=f"Work/runs/{run_id}/reviews/module/initial/2.4/progress.json",
        review_round=0,
        scope=["2.4.1.1"],
        current=current,
        subject_ref=subject_ref,
    )
    initial_acceptance = ModuleInitialReviewAcceptance(
        run_id=run_id,
        module_id="2.4",
        lifecycle_id="initial",
        reviewer_session_key="module-auditor-2.4",
        subject_ref=subject_ref,
        current=current,
        findings=[finding],
        finding_refs=[f"Work/runs/{run_id}/reviews/findings.json"],
        next_action="revise",
        progress_ref=initial_prepared.progress_ref,
    )
    recheck_prepared = ModuleRecheckPreparation(
        mode="invoke_agent",
        run_id=run_id,
        module_id="2.4",
        lifecycle_id="initial",
        workflow_id="public-reporting",
        reviewer_session_key="module-auditor-2.4",
        review_root=initial_prepared.review_root,
        progress_ref=initial_prepared.progress_ref,
        review_round=1,
        scope=["2.4.1.1"],
        current=current,
        pending=[finding],
        responses=[response],
        finding_refs=initial_acceptance.finding_refs,
        subject_ref=subject_ref,
    )
    submission = ModuleReviewVerdictSubmission(
        coverage={"submodule_ids": ["2.4.1.1"]},
        verdicts=[
            ResolutionVerdict(
                finding_id=finding.id,
                verdict="escalate",
                reason="作者与审查者对该 finding 仍存在无法在本轮消解的分歧。",
                evidence_refs=[subject_ref],
            )
        ],
    )
    return DeclarativeModuleRuntimeLaneContext(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": run_id},
        status="reviewer_exception_deferred",
        module=current,
        review=DeclarativeModuleReviewPreparation(
            envelope=None,
            reviewer_session_key=initial_prepared.reviewer_session_key,
            prepared=initial_prepared,
            acceptance=initial_acceptance,
        ),
        recheck=DeclarativeModuleRecheckPreparation(
            prepared=recheck_prepared,
            submission=submission,
        ),
    )


def test_module_runtime_prepares_author_and_reviewer_main_exception_triggers(
    tmp_path: Path,
) -> None:
    """Both exception sources use the existing typed Main decision boundary."""

    runtime, _execution, _session_factory, _agent_invokers = _build_runtime(tmp_path)
    author = runtime.prepare_main_exception_lane(
        _author_exception_context("module-main-author")
    )
    reviewer = runtime.prepare_main_exception_lane(
        _reviewer_exception_context("module-main-reviewer")
    )

    assert author.status == "author_exception_ready"
    assert author.main_preparation is not None
    assert author.main_preparation.trigger == "author_response"
    assert author.main_preparation.exception_ids == ["F-author"]
    assert runtime.main_exception_requires_agent(author)

    assert reviewer.status == "reviewer_exception_ready"
    assert reviewer.main_preparation is not None
    assert reviewer.main_preparation.trigger == "reviewer_escalation"
    assert reviewer.main_preparation.exception_ids == ["F-reviewer"]
    assert runtime.main_exception_requires_agent(reviewer)


def test_module_runtime_resumes_existing_author_main_decision(
    tmp_path: Path,
) -> None:
    """A persisted decision is accepted without creating another decision turn."""

    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        WorkflowDecisionSubmission,
    )

    runtime, _execution, _session_factory, _agent_invokers = _build_runtime(tmp_path)
    context = _author_exception_context("module-main-author-resume")
    ready = runtime.prepare_main_exception_lane(context)
    assert ready.main_preparation is not None
    persisted = WorkflowDecisionSubmission(
        decision="accept_dispute",
        rationale="Main 接受作者对该 finding 的明确争议。",
        finding_ids=["F-author"],
    )
    ReportingStore(tmp_path).write_json(
        ready.main_preparation.decision_ref,
        persisted.model_dump(mode="json"),
    )

    resumed = runtime.prepare_main_exception_lane(context)

    assert resumed.status == "author_exception_resumed"
    assert resumed.main_preparation is not None
    assert resumed.main_preparation.mode == "continue_existing"
    assert resumed.main_acceptance is not None
    assert resumed.main_acceptance.result == persisted


@pytest.mark.parametrize(
    ("user_decision", "expected_status", "route_status"),
    (
        ("return_to_author", "author_exception_accepted", "revision_pending"),
        ("stop_incomplete", "failed", None),
    ),
)
def test_module_runtime_routes_main_user_decisions_and_stop_incomplete(
    tmp_path: Path,
    user_decision: str,
    expected_status: str,
    route_status: str | None,
) -> None:
    """Request-user interaction preserves author routing and terminal stop semantics."""

    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        WorkflowDecisionSubmission,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import (
        DeclarativeMainExceptionAgentResult,
    )

    runtime, _execution, _session_factory, _agent_invokers = _build_runtime(tmp_path)
    ready = runtime.prepare_main_exception_lane(
        _author_exception_context(f"module-main-user-{user_decision}")
    )
    requested = runtime.accept_main_exception_lane(
        {
            "context": ready,
            "result": DeclarativeMainExceptionAgentResult(
                status="completed",
                submission=WorkflowDecisionSubmission(
                    decision="request_user",
                    rationale="该争议需要用户在同一个 Run 中明确下一步。",
                    finding_ids=["F-author"],
                ),
            ),
        }
    )
    assert requested.status == "author_exception_accepted"
    assert runtime.main_exception_requests_user(requested)

    applied = runtime.apply_main_exception_user_input(
        {
            "context": requested,
            "input": {
                "decision": user_decision,
                "rationale": "用户明确给出该例外节点的下一步处理意见。",
            },
        }
    )
    assert applied.status == expected_status
    if user_decision == "stop_incomplete":
        assert applied.error
    else:
        routed = runtime.route_after_main_exception(applied)
        assert routed.status == route_status


def test_module_runtime_routes_reviewer_escalation_through_recheck_acceptance(
    tmp_path: Path,
) -> None:
    """Reviewer escalation reuses the Capability recheck acceptance before routing."""

    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        WorkflowDecisionSubmission,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import (
        DeclarativeMainExceptionAgentResult,
    )

    runtime, _execution, _session_factory, _agent_invokers = _build_runtime(tmp_path)
    ready = runtime.prepare_main_exception_lane(
        _reviewer_exception_context("module-main-reviewer-route")
    )
    accepted = runtime.accept_main_exception_lane(
        {
            "context": ready,
            "result": DeclarativeMainExceptionAgentResult(
                status="completed",
                submission=WorkflowDecisionSubmission(
                    decision="accept_dispute",
                    rationale="Main 接受审查者与作者之间的明确争议。",
                    finding_ids=["F-reviewer"],
                ),
            ),
        }
    )

    routed = runtime.route_after_main_exception(accepted)

    assert routed.status == "revision_pending"
    assert routed.review is not None
    assert routed.review.acceptance is not None
    assert routed.review.acceptance.next_action == "continue_existing"
    assert routed.recheck is None
