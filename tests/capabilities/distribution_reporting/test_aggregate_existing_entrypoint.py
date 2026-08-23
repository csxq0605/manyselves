"""Focused characterization for the first aggregate-existing entrypoint slice."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
    ReportRequest,
    SpecialTopicPlan,
    SpecialTopicSectionRequirement,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.kernel.contracts import build_contract_catalog
from manyselves.kernel.definitions import DefinitionKind
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.workflow_host import InMemoryWorkflowEventSink, WorkflowRuntimeHost


class _FrozenSnapshot:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id

    def resolve(self, logical_ref: Path) -> Path:
        return (
            Path("Work")
            / "runs"
            / self.run_id
            / "frozen-project"
            / logical_ref
        )


def _request(refs: dict[str, Path]) -> ReportRequest:
    return ReportRequest(
        operation="aggregate_existing",
        instruction="汇总已完成的五个模块报告。",
        source_module_refs=refs,
    )


def _markdown(module_id: str) -> str:
    definition = REPORT_TAXONOMY[module_id]
    return "\n\n".join(
        f"# {section_id} {section.title}\n\n模块 {module_id} 的既有正文。"
        for section_id, section in definition.sections.items()
    ) + "\n"


def _structured(module_id: str) -> ModuleSubmission:
    definition = REPORT_TAXONOMY[module_id]
    return ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: f"模块 {module_id} 的既有小节正文。"
            for submodule_id in definition.submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )


def _edited_submission():
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        EditedReportSubmission,
    )

    return EditedReportSubmission(
        title="汇总报告",
        assessment_background="背景",
        findings_overview="发现",
        regional_executive_summary="区域摘要",
        module_narratives={
            module_id: f"[[APPROVED_MODULE:{module_id}]]"
            for module_id in REPORT_MODULE_IDS
        },
        risk_panorama="风险全景",
        dimension_risk_analysis="维度风险",
        data_gap_analysis="数据缺口",
        improvement_action_plan="改进行动",
    )


def _edited_submission_with_final_chapter_4():
    plan = SpecialTopicPlan(
        source_ref="Inputs/special-topic.md",
        source_sha256="0" * 64,
        sections=[
            SpecialTopicSectionRequirement(
                section_id="4.1",
                title="专项分析",
                requirement="说明专项事实、影响、方案条件与验证方法。",
            )
        ],
    )
    return _edited_submission().model_copy(
        update={
            "special_topic_plan": plan,
            "special_topic_analysis": (
                "### 4.1 专项分析\n\n"
                "专项正文包含项目事实、影响判断、方案条件与验证方法。"
            ),
        }
    )


def _write_frozen_modules(
    workspace: Path,
    run_id: str,
    *,
    suffixes: dict[str, str],
) -> dict[str, Path]:
    refs: dict[str, Path] = {}
    for module_id in REPORT_MODULE_IDS:
        suffix = suffixes[module_id]
        logical_ref = Path("Outputs") / "Modules" / f"{module_id}{suffix}"
        frozen = workspace / _FrozenSnapshot(run_id).resolve(logical_ref)
        frozen.parent.mkdir(parents=True, exist_ok=True)
        if suffix == ".json":
            frozen.write_text(
                json.dumps(_structured(module_id).model_dump(mode="json")),
                encoding="utf-8",
            )
        else:
            frozen.write_text(_markdown(module_id), encoding="utf-8")
        refs[module_id] = logical_ref
    return refs


def _tool_bundle(workspace: Path, run_id: str):
    from manyselves.capabilities.distribution_reporting.runtime.aggregate_existing import (
        build_aggregate_existing_tool_implementations,
    )

    return build_aggregate_existing_tool_implementations(
        workspace=workspace,
        input_snapshot=lambda _run_id: _FrozenSnapshot(run_id),
        store=ReportingStore(workspace),
    )


def test_prepare_aggregate_existing_projects_five_frozen_markdown_modules(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.aggregate_existing import (
        AggregateExistingPreparationInput,
    )
    run_id = "aggregate-markdown"
    refs = _write_frozen_modules(
        tmp_path,
        run_id,
        suffixes={module_id: ".md" for module_id in REPORT_MODULE_IDS},
    )

    result = _tool_bundle(tmp_path, run_id)["prepare-aggregate-existing"](
        AggregateExistingPreparationInput(
            run_id=run_id,
            request=_request(refs),
        )
    )

    assert result.source_format == "markdown"
    assert set(result.markdown_modules) == set(REPORT_MODULE_IDS)
    assert result.structured_modules == {}
    assert result.editor_input.source_format == "markdown"
    assert result.editor_input.cross_context is None
    assert set(result.editor_input.markdown_modules) == set(REPORT_MODULE_IDS)
    assert result.source_manifest_ref == (
        f"Work/runs/{run_id}/context/aggregate-source-manifest.json"
    )
    assert result.integrity_report_ref == (
        f"Work/runs/{run_id}/reviews/aggregate-module-integrity.json"
    )
    assert json.loads(
        (tmp_path / result.source_manifest_ref).read_text(encoding="utf-8")
    ) == {
        "source_format": "markdown",
        "module_refs": {
            module_id: f"Work/runs/{run_id}/frozen-project/{refs[module_id]}"
            for module_id in REPORT_MODULE_IDS
        },
    }
    integrity = json.loads(
        (tmp_path / result.integrity_report_ref).read_text(encoding="utf-8")
    )
    assert integrity["passed"] is True
    assert integrity["failures"] == []


def test_prepare_aggregate_existing_projects_five_frozen_structured_modules(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.aggregate_existing import (
        AggregateExistingPreparationInput,
    )

    run_id = "aggregate-structured"
    refs = _write_frozen_modules(
        tmp_path,
        run_id,
        suffixes={module_id: ".json" for module_id in REPORT_MODULE_IDS},
    )

    result = _tool_bundle(tmp_path, run_id)["prepare-aggregate-existing"](
        AggregateExistingPreparationInput(
            run_id=run_id,
            request=_request(refs),
        )
    )

    assert result.source_format == "structured_module"
    assert set(result.structured_modules) == set(REPORT_MODULE_IDS)
    assert result.markdown_modules == {}
    assert result.editor_input.source_format == "structured_module"
    assert set(result.editor_input.structured_modules) == set(REPORT_MODULE_IDS)
    assert result.editor_input.markdown_modules == {}
    assert json.loads(
        (tmp_path / result.source_manifest_ref).read_text(encoding="utf-8")
    )["source_format"] == "structured_module"
    assert json.loads(
        (tmp_path / result.integrity_report_ref).read_text(encoding="utf-8")
    )["passed"] is True


def test_prepare_aggregate_existing_rejects_mixed_module_formats(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.aggregate_existing import (
        AggregateExistingPreparationInput,
    )

    run_id = "aggregate-mixed"
    suffixes = {module_id: ".md" for module_id in REPORT_MODULE_IDS}
    suffixes["2.1"] = ".json"
    refs = _write_frozen_modules(tmp_path, run_id, suffixes=suffixes)

    with pytest.raises(ValueError, match="five structured module JSON refs together"):
        _tool_bundle(tmp_path, run_id)["prepare-aggregate-existing"](
            AggregateExistingPreparationInput(
                run_id=run_id,
                request=_request(refs),
            )
        )


def test_aggregate_existing_workflow_declares_only_the_prepare_and_agent_slice() -> None:
    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )

    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-aggregate-existing",
    )
    action_ids = [action["id"] for action in workflow.actions]
    referenced_workflows = {
        action["workflow"]
        for action in workflow.actions
        if "workflow" in action
    }

    assert action_ids == [
        "prepare-aggregate-existing",
        "project-aggregate-editor-input",
        "create-aggregate-existing-conversation",
        "invoke-aggregate-existing-agent",
        "project-aggregate-existing-handoff",
        "finish-aggregate-existing-agent",
    ]
    assert referenced_workflows == set()
    assert "distribution-cross-owner-cohort" not in referenced_workflows
    assert "distribution-chief-chapter-cohort" not in referenced_workflows

    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )
    assert plan.workflow_id == "distribution-aggregate-existing"
    assert workflow.output_contract == "aggregate_existing_handoff"


def test_aggregate_existing_tail_enters_final_and_delivery_without_other_cohorts() -> None:
    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )

    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-aggregate-existing-tail",
    )
    action_ids = [action["id"] for action in workflow.actions]
    referenced_workflows = {
        action["workflow"]
        for action in workflow.actions
        if "workflow" in action
    }

    assert action_ids == [
        "run-aggregate-existing",
        "project-aggregate-existing-tail",
        "run-final-review",
        "run-report-delivery",
        "finish-aggregate-existing-tail",
    ]
    assert referenced_workflows == {
        "distribution-aggregate-existing",
        "distribution-final-chapter-cohort",
        "distribution-report-delivery",
    }
    assert "distribution-cross-owner-cohort" not in referenced_workflows
    assert "distribution-chief-chapter-cohort" not in referenced_workflows

    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )
    assert plan.final_output_contract == "reporting_tail_state"
    assert "distribution-final-review-cycle" in plan.subworkflow_plans
    assert "distribution-report-delivery" in plan.subworkflow_plans


def test_aggregate_existing_agent_slice_does_not_claim_final_or_delivery_completion() -> None:
    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )

    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-aggregate-existing",
    )
    assert workflow.output_contract == "aggregate_existing_handoff"
    assert all(
        action.get("workflow") not in {
            "distribution-final-chapter-cohort",
            "distribution-final-review-cycle",
            "distribution-report-delivery",
        }
        for action in workflow.actions
    )


@pytest.mark.asyncio
async def test_aggregate_existing_host_executes_prepare_before_agent_boundary(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )
    from manyselves.capabilities.distribution_reporting.runtime.aggregate_existing import (
        AggregateExistingPreparationInput,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.aggregate_existing import (
        AggregateExistingContext,
    )

    run_id = "aggregate-host-preparation"
    refs = _write_frozen_modules(
        tmp_path,
        run_id,
        suffixes={module_id: ".md" for module_id in REPORT_MODULE_IDS},
    )
    request = _request(refs)
    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-aggregate-existing",
    )
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, registry)
    state = WorkflowState.for_plan(
        run_id,
        plan,
        initial_variables={
            "aggregate-input": AggregateExistingPreparationInput(
                run_id=run_id,
                request=request,
            )
        },
    )
    events = InMemoryWorkflowEventSink()
    store = FileWorkflowStateStore(tmp_path)
    host = WorkflowRuntimeHost(executors, store, events)

    with pytest.raises(RuntimeError, match="missing agent adapter: aggregate-editor"):
        await host.execute(
            plan,
            state,
            RuntimeContext(
                tools=_tool_bundle(tmp_path, run_id),
                contracts=build_contract_catalog(registry),
                definitions=registry,
            ),
        )

    persisted = store.load(run_id)
    assert persisted.actions["prepare-aggregate-existing"].status.value == "completed"
    assert persisted.actions["project-aggregate-editor-input"].status.value == "completed"
    assert persisted.actions["create-aggregate-existing-conversation"].status.value == "completed"
    assert persisted.actions["invoke-aggregate-existing-agent"].status.value == "failed"
    context = AggregateExistingContext.model_validate(
        persisted.variables["aggregate-context"]
    )
    assert (tmp_path / context.source_manifest_ref).is_file()
    assert (tmp_path / context.integrity_report_ref).is_file()
    assert (tmp_path / context.editor_input_ref).is_file()
    assert any(
        event.kind == "action.failed"
        and event.action_id == "invoke-aggregate-existing-agent"
        for event in events.events
    )


@pytest.mark.asyncio
async def test_aggregate_existing_runtime_binds_typed_agent_to_generic_host(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.aggregate_existing import (
        AggregateExistingPreparationInput,
        AggregateExistingWorkflowRuntime,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        EditedReportSubmission,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.aggregate_existing import (
        AggregateExistingHandoff,
    )
    from manyselves.kernel.ports import AgentInvocationOutcome

    class RecordingAggregateInvoker:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def invoke(
            self,
            agent,
            task,
            value,
            conversation,
            *,
            task_id: str,
        ) -> AgentInvocationOutcome:
            self.calls.append(
                {
                    "agent": agent.id,
                    "task": task.id,
                    "value": value,
                    "conversation_id": conversation.conversation_id,
                    "session_key": conversation.key.value,
                    "task_id": task_id,
                }
            )
            return AgentInvocationOutcome(
                status="ok",
                result=_edited_submission().model_dump(mode="json"),
                session_id="aggregate-editor-session",
            )

        async def invoke_with_recovery(
            self,
            agent,
            task,
            value,
            conversation,
            *,
            task_id: str,
            recovery_policy,
        ) -> AgentInvocationOutcome:
            del recovery_policy
            return await self.invoke(
                agent,
                task,
                value,
                conversation,
                task_id=task_id,
            )

    run_id = "aggregate-host-agent"
    refs = _write_frozen_modules(
        tmp_path,
        run_id,
        suffixes={module_id: ".md" for module_id in REPORT_MODULE_IDS},
    )
    invoker = RecordingAggregateInvoker()
    events = InMemoryWorkflowEventSink()
    runtime = AggregateExistingWorkflowRuntime(
        tmp_path,
        input_snapshot=lambda _run_id: _FrozenSnapshot(run_id),
        agent_invoker=invoker,
        events=events,
    )
    value = AggregateExistingPreparationInput(
        run_id=run_id,
        request=_request(refs),
    )

    completed = await runtime.execute(value)

    assert completed.status.value == "completed"
    assert isinstance(completed.outputs["result"], AggregateExistingHandoff)
    assert isinstance(completed.outputs["result"].edited_report, EditedReportSubmission)
    assert len(invoker.calls) == 1
    assert invoker.calls[0]["agent"] == "aggregate-editor"
    assert invoker.calls[0]["task"] == "aggregate-existing"
    assert invoker.calls[0]["session_key"] == "aggregate-existing"
    assert invoker.calls[0]["task_id"] == "invoke-aggregate-existing-agent"
    assert invoker.calls[0]["value"].run_id == run_id
    assert {
        call["agent"]
        for call in invoker.calls
    } == {"aggregate-editor"}
    assert sum(
        event.kind == "tool.invoked"
        and event.action_id == "prepare-aggregate-existing"
        for event in events.events
    ) == 1
    assert {
        event.kind
        for event in events.events
    } >= {
        "workflow.started",
        "conversation.created",
        "tool.invoked",
        "agent.invoked",
        "workflow.completed",
    }
    assert all(
        event.action_id not in {
            "distribution-cross-owner-cohort",
            "distribution-chief-chapter-cohort",
        }
        for event in events.events
    )

    repeated = await runtime.execute(value)

    assert repeated.status.value == "completed"
    assert (
        AggregateExistingHandoff.model_validate(repeated.outputs["result"])
        .edited_report.title
        == "汇总报告"
    )
    assert repeated.run_id == run_id
    assert len(invoker.calls) == 1
    assert sum(
        event.kind == "tool.invoked"
        and event.action_id == "prepare-aggregate-existing"
        for event in events.events
    ) == 1


@pytest.mark.asyncio
async def test_aggregate_existing_tail_reaches_final_boundary_without_claiming_delivery(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )
    from manyselves.capabilities.distribution_reporting.runtime.aggregate_existing import (
        AggregateExistingPreparationInput,
    )
    from manyselves.capabilities.distribution_reporting.runtime.final_agent_bridge import (
        FinalChapterAgentBridge,
    )
    from manyselves.capabilities.distribution_reporting.runtime.final_chief_agent_bridge import (
        FinalChiefAgentBridge,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        CHIEF_SECTION_RESULT_PART_IDS,
        ChapterScopedFinalReviewFinding,
        ChapterScopedFinalReviewTargetChange,
        ChiefChapterLaneRevisionSubmission,
        FinalChapterLaneFindingSubmission,
        RevisionResponse,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.final_review import (
        DeclarativeFinalReviewContext,
    )
    from manyselves.core.loops.bus import MessageBus
    from manyselves.interfaces.types import AgentResultMessage, UserMessage
    from manyselves.kernel.ports import AgentInvocationOutcome
    from manyselves.runtime.agent_execution import AgentExecutionService

    class RecordingAggregateInvoker:
        async def invoke(
            self,
            agent,
            task,
            value,
            conversation,
            *,
            task_id: str,
        ) -> AgentInvocationOutcome:
            del agent, task, value, conversation, task_id
            return AgentInvocationOutcome(
                status="ok",
                result=_edited_submission_with_final_chapter_4().model_dump(mode="json"),
                session_id="aggregate-editor-session",
            )

        async def invoke_with_recovery(
            self,
            agent,
            task,
            value,
            conversation,
            *,
            task_id: str,
            recovery_policy,
        ) -> AgentInvocationOutcome:
            del recovery_policy
            return await self.invoke(
                agent,
                task,
                value,
                conversation,
                task_id=task_id,
            )

    run_id = "aggregate-tail-final-boundary"
    refs = _write_frozen_modules(
        tmp_path,
        run_id,
        suffixes={module_id: ".md" for module_id in REPORT_MODULE_IDS},
    )
    chief_skill_path = (
        tmp_path
        / "Work/report-template-role-skills/chief-editor-chapter-1/SKILL.md"
    )
    chief_skill_path.parent.mkdir(parents=True, exist_ok=True)
    chief_skill_path.write_text(
        "# Chapter 1 Chief revision skill\n\nUse the assigned finding and preserve the typed lane contract.",
        encoding="utf-8",
    )
    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-aggregate-existing-tail",
    )
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, registry)
    state = WorkflowState.for_plan(
        run_id,
        plan,
        initial_variables={
            "aggregate-input": AggregateExistingPreparationInput(
                run_id=run_id,
                request=_request(refs),
            )
        },
    )
    events = InMemoryWorkflowEventSink()
    store = FileWorkflowStateStore(tmp_path)
    host = WorkflowRuntimeHost(executors, store, events)
    result_refs = {}
    for chapter_id, section_ids in {
        "1": ("1.1", "1.2", "1.3"),
        "3": ("3.1.1", "3.1.2", "3.1.3", "3.2"),
        "4": ("4.1",),
    }.items():
        result_ref = (
            f"Work/runs/{run_id}/reviews/final-chapter-{chapter_id}-agent-result.json"
        )
        result_path = tmp_path / result_ref
        result_path.parent.mkdir(parents=True, exist_ok=True)
        findings = []
        if chapter_id == "1":
            findings = [
                ChapterScopedFinalReviewFinding(
                    id="F-final-1",
                    target_section_ids=["1.1"],
                    target_changes=[
                        ChapterScopedFinalReviewTargetChange(
                            target_section_id="1.1",
                            required_change=(
                                "Add the missing explanation and verification detail."
                            ),
                            reviewer_checks=[
                                "The revised section states the check and outcome."
                            ],
                        )
                    ],
                    category="completeness",
                    impact="blocking",
                    observation=(
                        "The section does not explain the required verification detail."
                    ),
                    evidence_refs=[
                        f"Work/runs/{run_id}/reviews/final-initial-aggregate.json"
                    ],
                )
            ]
        result_path.write_text(
            json.dumps(
                FinalChapterLaneFindingSubmission(
                    run_id=run_id,
                    chapter_id=chapter_id,
                    checked_section_ids=list(section_ids),
                    findings=findings,
                ).model_dump(mode="json"),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result_refs[chapter_id] = result_ref

    chief_result_ref = (
        f"Work/runs/{run_id}/reviews/chief-chapter-1-agent-result.json"
    )
    chief_result_path = tmp_path / chief_result_ref
    chief_result_path.parent.mkdir(parents=True, exist_ok=True)
    chief_result_path.write_text(
        json.dumps(
            ChiefChapterLaneRevisionSubmission(
                run_id=run_id,
                base_subject_ref=(
                    f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
                ),
                chapter_id="1",
                revision=1,
                section_ids=["1.1", "1.2", "1.3"],
                part_refs={
                    CHIEF_SECTION_RESULT_PART_IDS[section_id]: (
                        f"Work/runs/{run_id}/drafts/chief-chapter-1-r1/"
                        f"{CHIEF_SECTION_RESULT_PART_IDS[section_id]}.md"
                    )
                    for section_id in ("1.1", "1.2", "1.3")
                },
                revision_responses=[
                    RevisionResponse(
                        finding_id="F-final-1",
                        action="implemented",
                        summary="The requested chapter-local change was implemented.",
                        changed_target_ids=["1.1"],
                    )
                ],
            ).model_dump(mode="json"),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    loops = {}

    class ScriptedLoop:
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
                result_path = (
                    chief_result_ref
                    if message.session_id.startswith("chief-chapter-")
                    else result_refs[message.session_id.removeprefix("final-chapter-")]
                )
                await bus.publish(
                    AgentResultMessage(
                        sender=self.runtime_id,
                        workflow_id=message.workflow_id,
                        task_id=message.task_id,
                        run_id=message.run_id,
                        result_path=result_path,
                        task_attempt_id=message.task_attempt_id,
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

    def session_factory(runtime_id: str) -> ScriptedLoop:
        loop = ScriptedLoop(runtime_id)
        loops[runtime_id] = loop
        return loop

    agent_service = AgentExecutionService(bus, timeout=1)
    final_bridge = FinalChapterAgentBridge(
        tmp_path,
        execution=agent_service,
        session_factory=session_factory,
    )
    chief_bridge = FinalChiefAgentBridge(
        tmp_path,
        execution=agent_service,
        session_factory=session_factory,
    )

    try:
        with pytest.raises(
            RuntimeError,
            match="missing tool adapter: accept-current-final-chief-revision",
        ):
            await host.execute(
                plan,
                state,
                RuntimeContext(
                    tools=_tool_bundle(tmp_path, run_id),
                    contracts=build_contract_catalog(registry),
                    definitions=registry,
                    agents={
                        "aggregate-editor": RecordingAggregateInvoker(),
                        "chief-editor": chief_bridge,
                        "chief-editor-auditor": final_bridge,
                    },
                ),
            )

        persisted = store.load(run_id)
        assert persisted.actions["run-aggregate-existing"].status.value == "completed"
        assert persisted.actions["project-aggregate-existing-tail"].status.value == "completed"
        assert persisted.actions["run-final-review"].status.value == "failed"
        assert persisted.actions["run-report-delivery"].status.value == "pending"
        final_state = WorkflowState.model_validate(
            persisted.subworkflow_states["run-final-review"]
        )
        assert final_state.actions["prepare-final-chapter-cohort"].status.value == "completed"
        assert final_state.actions["final-chapter-cohort"].status.value == "completed"
        assert final_state.actions["reduce-final-chapter-cohort"].status.value == "completed"
        assert final_state.actions["run-final-review-cycle"].status.value == "failed"
        review_state = WorkflowState.model_validate(
            final_state.subworkflow_states["run-final-review-cycle"]
        )
        assert review_state.actions["start-final-review-cycle"].status.value == "completed"
        assert review_state.actions["final-review-needs-round"].status.value == "completed"
        assert review_state.actions["advance-final-review-round"].status.value == "completed"
        assert review_state.actions["run-final-chief-revision-cohort"].status.value == "failed"
        review = DeclarativeFinalReviewContext.model_validate(
            review_state.variables["review"]
        )
        assert review.revision_number == 1
        assert set(review.pending_by_chapter) == {"1"}
        chief_input_ref = (
            tmp_path
            / f"Work/runs/{run_id}/context/chief-chapter-1-input-r1.json"
        )
        chief_input = json.loads(chief_input_ref.read_text(encoding="utf-8"))
        assert chief_input["phase"] == "revision"
        assert chief_input["chapter_id"] == "1"
        assert [finding["id"] for finding in chief_input["assigned_findings"]] == [
            "F-final-1"
        ]
        assert final_state.variables["prepared-final-state"]["run_id"] == run_id
        aggregate_ref = tmp_path / f"Work/runs/{run_id}/reviews/final-initial-aggregate.json"
        assert aggregate_ref.is_file()
        assert json.loads(aggregate_ref.read_text(encoding="utf-8"))["lane_ids"] == [
            "1",
            "3",
            "4",
        ]
        assert len(loops) == 4
        assert {loop.received[0].session_id for loop in loops.values()} == {
            "final-chapter-1",
            "final-chapter-3",
            "final-chapter-4",
            "chief-chapter-1",
        }
        assert all(
            "final_chapter_lane_input" in loop.received[0].content
            for runtime_id, loop in loops.items()
            if ":chief-editor-auditor:" in runtime_id
        )
        chief_loop = next(
            loop
            for runtime_id, loop in loops.items()
            if ":chief-editor:" in runtime_id
        )
        assert "chief_chapter_lane_input" in chief_loop.received[0].content
        assert (
            '<template_role_skill id="chief-editor-chapter-1"'
            in chief_loop.received[0].content
        )
        assert "Use the assigned finding and preserve the typed lane contract." in (
            chief_loop.received[0].content
        )
        assert all(
            '<final_lane_specialization chapter_id="' in loop.received[0].content
            for runtime_id, loop in loops.items()
            if ":chief-editor-auditor:" in runtime_id
        )
        assert len(agent_service.sessions) == 4
        assert persisted.outputs == {}
        assert not (tmp_path / "Outputs/Reports/配电安全专家咨询报告.md").exists()
        assert any(
            event.kind == "action.failed" and event.action_id == "run-final-review"
            for event in events.events
        )
    finally:
        await agent_service.close_workflow("distribution-aggregate-existing-tail")
        bus.shutdown()
        await bus_task
