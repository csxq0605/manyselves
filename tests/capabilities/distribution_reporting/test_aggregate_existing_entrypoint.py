"""Focused characterization for the first aggregate-existing entrypoint slice."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ClaimRecord,
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
    first_submodule_id = next(iter(definition.submodules))
    return ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: f"模块 {module_id} 的既有小节正文。"
            for submodule_id in definition.submodules
        },
        claims=[
            ClaimRecord(
                id=f"C-{module_id.replace('.', '')}-existing",
                module_id=module_id,
                submodule_id=first_submodule_id,
                text=f"模块 {module_id} 的既有汇总说明。",
                claim_type="recommendation",
                footnote_required=False,
                unresolved=True,
            )
        ],
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
            module_id: _structured(module_id).markdown
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
            module_id: (
                f"Work/runs/{run_id}/frozen-project/"
                f"{refs[module_id].as_posix()}"
            )
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
    chief_revision_task = registry.require(
        DefinitionKind.TASK,
        "final-chief-chapter-revision",
    )
    assert chief_revision_task.tools == [
        "open_artifact",
        "search_text",
        "write_result_part",
        "list_result_parts",
        "submit_result",
    ]
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


def test_final_recheck_open_and_new_finding_returns_to_next_round_same_run(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )
    from manyselves.capabilities.distribution_reporting.runtime.final_review_tools import (
        FinalReviewTools,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        ChapterScopedFinalReviewFinding,
        ChapterScopedFinalReviewTargetChange,
        FinalChapterLaneVerdictSubmission,
        ResolutionVerdict,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.final_review import (
        DeclarativeFinalRecheckOutcome,
        DeclarativeFinalReviewContext,
    )

    run_id = "aggregate-open-recheck"
    finding = ChapterScopedFinalReviewFinding(
        id="F-final-open",
        target_section_ids=["1.1"],
        target_changes=[
            ChapterScopedFinalReviewTargetChange(
                target_section_id="1.1",
                required_change="保留原 finding 并补充下一轮需要核对的证据说明。",
                reviewer_checks=["下一轮应重新检查该 section 的证据说明。"],
            )
        ],
        category="completeness",
        impact="blocking",
        observation="当前修订仍缺少可复核的证据说明，需要继续修订。",
        evidence_refs=[f"Work/runs/{run_id}/reviews/final-initial.json"],
    )
    new_finding = finding.model_copy(update={"id": "F-final-new"})
    review = DeclarativeFinalReviewContext(
        state={
            "run_id": run_id,
            "max_final_review_rounds": 3,
            "edited_report": _edited_submission_with_final_chapter_4(),
        },
        current=_edited_submission_with_final_chapter_4(),
        subject_ref=f"Work/runs/{run_id}/edited-revisions/chief-r1.json",
        findings_by_chapter={"1": [finding]},
        pending_by_chapter={"1": [finding]},
        initial_lane_refs={"1": f"Work/runs/{run_id}/reviews/final-initial.json"},
        revision_number=1,
    )
    submission = FinalChapterLaneVerdictSubmission(
        run_id=run_id,
        chapter_id="1",
        checked_section_ids=["1.1", "1.2", "1.3"],
        verdicts=[
            ResolutionVerdict(
                finding_id=finding.id,
                verdict="open",
                reason="当前 section 仍未满足 reviewer_checks，需要下一轮修订。",
            )
        ],
        new_findings=[new_finding],
    )
    outcome = DeclarativeFinalRecheckOutcome(
        chapter_id="1",
        status="completed",
        submission=submission,
        output_ref=f"Work/runs/{run_id}/reviews/final-chapter-lane-1-r1.json",
    )

    reduced = FinalReviewTools(
        workspace=tmp_path,
        store=ReportingStore(tmp_path),
    ).reduce_rechecks(
        {"review": review, "outcomes": {"1": outcome}}
    )
    assert [item.id for item in reduced.pending_by_chapter["1"]] == [
        "F-final-open",
        "F-final-new",
    ]
    assert reduced.latest_verdict_refs["1"] == outcome.output_ref
    assert reduced.verdict_history[0].submission.new_findings[0].id == "F-final-new"
    assert FinalReviewTools.needs_round(reduced) is True

    next_round = FinalReviewTools.advance_round(reduced)
    assert next_round.revision_number == 2
    assert next_round.state["run_id"] == run_id
    assert next_round.revision_responses == {}

    _capability, registry = load_distribution_reporting_capability()
    cycle = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-final-review-cycle",
    )
    choose_next = next(
        action for action in cycle.actions if action["id"] == "choose-final-review-next-step"
    )
    assert choose_next["then"] == "advance-final-review-round"


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
async def test_aggregate_editor_bridge_uses_typed_input_and_result(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )
    from manyselves.capabilities.distribution_reporting.runtime.aggregate_agent_bridge import (
        AggregateEditorAgentBridge,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        AgentResult,
        EditedReportSubmission,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        AggregateEditorInput,
    )
    from manyselves.interfaces.types import AgentResultMessage, UserMessage
    from manyselves.kernel.conversations import (
        ConversationKey,
        ConversationMode,
        ConversationRecord,
    )
    from manyselves.kernel.definitions import DefinitionKind
    from manyselves.runtime.agent_execution import AgentExecutionService
    from manyselves.runtime.loops.bus import MessageBus

    run_id = "aggregate-bridge-typed"
    result_ref = f"Work/runs/{run_id}/reviews/aggregate-editor-result.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        AgentResult(
            task_id="aggregate-existing",
            run_id=run_id,
            agent_id="aggregate-editor",
            session_id="aggregate-existing",
            status="completed",
            payload=_edited_submission(),
        ).model_dump_json(),
        encoding="utf-8",
    )
    editor_input = AggregateEditorInput(
        run_id=run_id,
        source_format="markdown",
        approved_module_markers={
            module_id: f"[[APPROVED_MODULE:{module_id}]]"
            for module_id in REPORT_MODULE_IDS
        },
        markdown_modules={
            module_id: _markdown(module_id) for module_id in REPORT_MODULE_IDS
        },
    )
    _capability, registry = load_distribution_reporting_capability()
    agent = registry.require(DefinitionKind.AGENT, "aggregate-editor")
    task = registry.require(DefinitionKind.TASK, "aggregate-existing")

    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    loops: list[object] = []

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
                await bus.publish(
                    AgentResultMessage(
                        sender="aggregate-editor",
                        workflow_id=message.workflow_id,
                        task_id="aggregate-existing",
                        run_id=message.run_id,
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

    def session_factory(_runtime_id: str) -> ScriptedLoop:
        loop = ScriptedLoop(_runtime_id)
        loops.append(loop)
        return loop

    execution = AgentExecutionService(bus, timeout=1)
    bridge = AggregateEditorAgentBridge(
        tmp_path,
        execution=execution,
        session_factory=session_factory,
        terminal_sender="aggregate-editor",
        terminal_task_id="aggregate-existing",
        terminal_task_attempt_id="",
    )
    conversation = ConversationRecord(
        conversation_id=(
            f"run:{run_id}:aggregate-editor:aggregate-existing"
        ),
        key=ConversationKey(
            agent_id="aggregate-editor",
            value="aggregate-existing",
            mode=ConversationMode.RUN,
        ),
        run_id=run_id,
    )

    try:
        first = await bridge.invoke(
            agent,
            task,
            editor_input,
            conversation,
            task_id="invoke-aggregate-existing-agent",
        )
        second = await bridge.invoke(
            agent,
            task,
            editor_input,
            conversation,
            task_id="invoke-aggregate-existing-agent",
        )

        assert first.status == "ok"
        assert second.status == "ok"
        assert first.session_id == "aggregate-existing"
        assert second.session_id == first.session_id
        assert isinstance(
            EditedReportSubmission.model_validate(first.result),
            EditedReportSubmission,
        )
        assert len(loops) == 1
        assert len(loops[0].received) == 2
        assert all(
            message.session_id == "aggregate-existing"
            for message in loops[0].received
        )
        assert all(
            '"kind": "aggregate_editor_input"' in message.content
            for message in loops[0].received
        )
        assert "模块 2.1 的既有正文" in loops[0].received[0].content
        assert conversation.external_session_id == "aggregate-existing"
    finally:
        await execution.close_workflow("distribution-aggregate-existing-tail")
        bus.shutdown()
        await bus_task


@pytest.mark.asyncio
async def test_aggregate_existing_runtime_executes_tail_with_agent_map(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.aggregate_agent_bridge import (
        AggregateEditorAgentBridge,
    )
    from manyselves.capabilities.distribution_reporting.runtime.aggregate_existing import (
        AggregateExistingPreparationInput,
        AggregateExistingWorkflowRuntime,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        AgentResult,
        ChapterScopedFinalReviewFinding,
        ChapterScopedFinalReviewTargetChange,
        ChiefChapterLaneRevisionSubmission,
        FinalChapterLaneFindingSubmission,
        FinalChapterLaneVerdictSubmission,
        ResolutionVerdict,
        RevisionResponse,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.final_chapter import (
        DeclarativeFinalChapterAgentResult,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.final_review import (
        DeclarativeFinalChiefRevisionAgentResult,
        DeclarativeFinalRecheckAgentResult,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        ChiefChapterLaneInput,
        FinalChapterLaneInput,
    )
    from manyselves.interfaces.types import AgentResultMessage, UserMessage
    from manyselves.kernel.ports import AgentInvocationOutcome
    from manyselves.runtime.agent_execution import AgentExecutionService
    from manyselves.runtime.loops.bus import MessageBus

    class RecordingAgentInvoker:
        def __init__(self, role: str) -> None:
            self.role = role
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
                    "task_id": task_id,
                    "conversation": conversation.key.value,
                    "conversation_id": conversation.conversation_id,
                    "session_id": conversation.external_session_id,
                }
            )
            call = self.calls[-1]
            contract = value.contract
            if self.role == "auditor":
                typed = FinalChapterLaneInput.model_validate(contract)
                if typed.phase == "initial":
                    findings = []
                    if typed.chapter_id == "1":
                        findings = [
                            ChapterScopedFinalReviewFinding(
                                id="F-runtime-tail-1",
                                target_section_ids=["1.1"],
                                target_changes=[
                                    ChapterScopedFinalReviewTargetChange(
                                            target_section_id="1.1",
                                            required_change=(
                                                "补充该章节缺失的核验说明、结论依据以及对应的复核结果。"
                                            ),
                                        reviewer_checks=[
                                            "修订正文应明确写出核验说明和结论依据。"
                                        ],
                                    )
                                ],
                                category="completeness",
                                impact="blocking",
                                observation=(
                                    "该章节未明确写出核验说明和结论依据，需进行一次修订。"
                                ),
                                evidence_refs=[
                                    f"Work/runs/{typed.run_id}/reviews/final-initial-aggregate.json"
                                ],
                            )
                        ]
                    submission = FinalChapterLaneFindingSubmission(
                        run_id=typed.run_id,
                        chapter_id=typed.chapter_id,
                        checked_section_ids=list(typed.section_ids),
                        findings=findings,
                    )
                    result = DeclarativeFinalChapterAgentResult(
                        status="completed",
                        submission=submission,
                    ).model_dump(mode="json")
                else:
                    required = [
                        item
                        for item in typed.required_findings
                    ]
                    submission = FinalChapterLaneVerdictSubmission(
                        run_id=typed.run_id,
                        chapter_id=typed.chapter_id,
                        checked_section_ids=list(typed.section_ids),
                        verdicts=[
                            ResolutionVerdict(
                                finding_id=finding.id,
                                verdict="resolved",
                                reason=(
                                    "修订正文已补充核验说明和结论依据，满足本次 reviewer_checks。"
                                ),
                                evidence_refs=[typed.subject_ref],
                            )
                            for finding in required
                        ],
                    )
                    result = DeclarativeFinalRecheckAgentResult(
                        status="completed",
                        submission=submission,
                    ).model_dump(mode="json")
                session_id = f"final-auditor-session-{conversation.key.value}"
                call["returned_session_id"] = session_id
                return AgentInvocationOutcome(
                    result=result,
                    session_id=session_id,
                )

            typed = ChiefChapterLaneInput.model_validate(contract)
            bodies = {
                "1.1": "修订后的背景正文，补充核验说明和结论依据。",
                "1.2": "保留原有发现概述正文。",
                "1.3": "保留原有区域摘要正文。",
            }
            assigned = [
                ChapterScopedFinalReviewFinding.model_validate(item)
                for item in typed.assigned_findings
            ]
            changed_ids = sorted(
                {
                    section_id
                    for finding in assigned
                    for section_id in finding.target_section_ids
                }
            )
            task_root = (
                tmp_path
                / "Work/runs"
                / typed.run_id
                / "drafts"
                / f"chief-chapter-{typed.chapter_id}-r{typed.revision}"
                / f"r{typed.revision}"
            )
            task_root.mkdir(parents=True, exist_ok=True)
            part_refs = {}
            for section_id in changed_ids:
                part_id = {
                    "1.1": "assessment_background",
                    "1.2": "findings_overview",
                    "1.3": "regional_executive_summary",
                }[section_id]
                path = task_root / f"{part_id}.md"
                path.write_text(bodies[section_id], encoding="utf-8")
                part_refs[part_id] = path.relative_to(tmp_path).as_posix()
            submission = ChiefChapterLaneRevisionSubmission(
                run_id=typed.run_id,
                base_subject_ref=typed.subject_ref,
                chapter_id=typed.chapter_id,
                revision=typed.revision,
                section_ids=changed_ids,
                part_refs=part_refs,
                revision_responses=[
                    RevisionResponse(
                        finding_id=finding.id,
                        action="implemented",
                        summary="已完成指定章节修改并补充核验说明和结论依据。",
                        changed_target_ids=list(finding.target_section_ids),
                    )
                    for finding in assigned
                ],
            )
            session_id = f"final-chief-session-{conversation.key.value}"
            call["returned_session_id"] = session_id
            return AgentInvocationOutcome(
                result=DeclarativeFinalChiefRevisionAgentResult(
                    status="completed",
                    submission=submission,
                ).model_dump(mode="json"),
                session_id=session_id,
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

    run_id = "aggregate-runtime-tail"
    refs = _write_frozen_modules(
        tmp_path,
        run_id,
        suffixes={module_id: ".json" for module_id in REPORT_MODULE_IDS},
    )
    aggregate_result_ref = (
        f"Work/runs/{run_id}/reviews/aggregate-editor-result.json"
    )
    aggregate_result_path = tmp_path / aggregate_result_ref
    aggregate_result_path.parent.mkdir(parents=True, exist_ok=True)
    aggregate_result_path.write_text(
        AgentResult(
            task_id="aggregate-existing",
            run_id=run_id,
            agent_id="aggregate-editor",
            session_id="aggregate-existing",
            status="completed",
            payload=_edited_submission_with_final_chapter_4(),
        ).model_dump_json(),
        encoding="utf-8",
    )
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    aggregate_loops: list[object] = []

    class AggregateLoop:
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
                await bus.publish(
                    AgentResultMessage(
                        sender="aggregate-editor",
                        workflow_id=message.workflow_id,
                        task_id="aggregate-existing",
                        run_id=message.run_id,
                        result_path=aggregate_result_ref,
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

    def aggregate_session_factory(runtime_id: str) -> AggregateLoop:
        loop = AggregateLoop(runtime_id)
        aggregate_loops.append(loop)
        return loop

    aggregate_execution = AgentExecutionService(bus, timeout=1)
    aggregate_bridge = AggregateEditorAgentBridge(
        tmp_path,
        execution=aggregate_execution,
        session_factory=aggregate_session_factory,
        terminal_sender="aggregate-editor",
        terminal_task_id="aggregate-existing",
        terminal_task_attempt_id="",
    )
    events = InMemoryWorkflowEventSink()
    chief = RecordingAgentInvoker("chief")
    auditor = RecordingAgentInvoker("auditor")
    runtime = AggregateExistingWorkflowRuntime(
        tmp_path,
        input_snapshot=lambda _run_id: _FrozenSnapshot(run_id),
        agent_invokers={
            "aggregate-editor": aggregate_bridge,
            "chief-editor": chief,
            "chief-editor-auditor": auditor,
        },
        workflow_id="distribution-aggregate-existing-tail",
        events=events,
    )

    try:
        completed = await runtime.execute(
            AggregateExistingPreparationInput(
                run_id=run_id,
                request=_request(refs),
            )
        )
    finally:
        await aggregate_execution.close_workflow(
            "distribution-aggregate-existing-tail"
        )
        bus.shutdown()
        await bus_task

    assert completed.status.value == "completed"
    result = completed.outputs["result"]
    assert result["delivery_status"] == "delivered"
    assert result["delivery_completion_ref"] == (
        f"Work/runs/{run_id}/delivery-completion.json"
    )
    assert {
        artifact["path"] for artifact in result["output_artifacts"]
    } >= {
        "Outputs/Reports/配电安全专家咨询报告.md",
        "Outputs/Reports/配电安全专家咨询报告.docx",
        f"Work/runs/{run_id}/reviews/final-completion.json",
    }
    run_root = tmp_path / "Work" / "runs" / run_id
    assert (run_root / "report/配电安全专家咨询报告.md").is_file()
    assert (run_root / "report/配电安全专家咨询报告.docx").is_file()
    assert (run_root / "delivery-receipt.json").is_file()
    assert (run_root / "delivery-completion.json").is_file()
    assert (tmp_path / "Outputs/Reports/配电安全专家咨询报告.md").is_file()
    assert (tmp_path / "Outputs/Reports/配电安全专家咨询报告.docx").is_file()
    assert len(aggregate_loops) == 1
    assert len(aggregate_loops[0].received) == 1
    assert aggregate_loops[0].received[0].session_id == "aggregate-existing"
    assert '"kind": "aggregate_editor_input"' in (
        aggregate_loops[0].received[0].content
    )
    assert len(chief.calls) == 1
    assert chief.calls[0]["agent"] == "chief-editor"
    assert chief.calls[0]["conversation"] == "chief-chapter-1"
    assert chief.calls[0]["session_id"] is None
    assert chief.calls[0]["returned_session_id"] == "final-chief-session-chief-chapter-1"
    assert len(auditor.calls) == 4
    assert {call["agent"] for call in auditor.calls} == {"chief-editor-auditor"}
    assert {call["conversation"] for call in auditor.calls} == {
        "final-chapter-1",
        "final-chapter-3",
        "final-chapter-4",
    }
    assert sum(call["conversation"] == "final-chapter-1" for call in auditor.calls) == 2
    final_chapter_1_calls = [
        call for call in auditor.calls if call["conversation"] == "final-chapter-1"
    ]
    assert final_chapter_1_calls[0]["session_id"] is None
    assert final_chapter_1_calls[1]["session_id"] == (
        "final-auditor-session-final-chapter-1"
    )
    assert final_chapter_1_calls[1]["session_id"] == (
        final_chapter_1_calls[0]["returned_session_id"]
    )
    assert final_chapter_1_calls[0]["conversation_id"] == (
        final_chapter_1_calls[1]["conversation_id"]
    )
    assert all(
        call["session_id"] is None
        for call in (*chief.calls, *auditor.calls)
        if call not in final_chapter_1_calls[1:]
    )
    assert all(
        call["returned_session_id"]
        for call in (*chief.calls, *auditor.calls)
    )
    assert not any(
        event.workflow_id.startswith(
            ("distribution-cross-owner", "distribution-chief-chapter")
        )
        for event in events.events
    )


@pytest.mark.asyncio
async def test_aggregate_existing_tail_completes_delivery_without_other_cohorts(
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
        AgentResult,
        AgentRunStatus,
        ChapterScopedFinalReviewFinding,
        ChapterScopedFinalReviewTargetChange,
        ChiefChapterLaneRevisionSubmission,
        FinalChapterLaneFindingSubmission,
        FinalChapterLaneVerdictSubmission,
        ResolutionVerdict,
        RevisionResponse,
    )
    from manyselves.interfaces.types import AgentResultMessage, UserMessage
    from manyselves.kernel.ports import AgentInvocationOutcome
    from manyselves.runtime.agent_execution import AgentExecutionService
    from manyselves.runtime.loops.bus import MessageBus

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
        suffixes={module_id: ".json" for module_id in REPORT_MODULE_IDS},
    )
    chief_skill_path = (
        tmp_path
        / "Inputs/report-template-role-skills/chief-editor-chapter-1/SKILL.md"
    )
    chief_skill_path.parent.mkdir(parents=True, exist_ok=True)
    chief_skill_path.write_text(
        "# Chapter 1 Chief revision skill\n\nUse the assigned finding and preserve the typed lane contract.",
        encoding="utf-8",
    )
    final_skill_path = (
        tmp_path
        / "Inputs/report-template-role-skills/final-auditor/SKILL.md"
    )
    final_skill_path.parent.mkdir(parents=True, exist_ok=True)
    final_skill_path.write_text(
        "# Final auditor skill\n\nRecheck only the assigned findings against the current subject.",
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
                AgentResult(
                    task_id=f"final-chapter-{chapter_id}-r0",
                    run_id=run_id,
                    agent_id="chief-editor-auditor",
                    session_id=f"final-chapter-{chapter_id}",
                    status=AgentRunStatus.COMPLETED,
                    payload=FinalChapterLaneFindingSubmission(
                        run_id=run_id,
                        chapter_id=chapter_id,
                        checked_section_ids=list(section_ids),
                        findings=findings,
                    ),
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
    chief_part_ref = (
        f"Work/runs/{run_id}/drafts/chief-chapter-1-r1/r1/"
        "assessment_background.md"
    )
    chief_part_path = tmp_path / chief_part_ref
    chief_part_path.parent.mkdir(parents=True, exist_ok=True)
    chief_part_path.write_text(
        "Updated background text with the requested verification detail.",
        encoding="utf-8",
    )
    chief_result_path.write_text(
        json.dumps(
            AgentResult(
                task_id="chief-chapter-1-r1",
                run_id=run_id,
                agent_id="chief-editor",
                session_id="chief-chapter-1",
                status=AgentRunStatus.COMPLETED,
                payload=ChiefChapterLaneRevisionSubmission(
                    run_id=run_id,
                    base_subject_ref=(
                        f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
                    ),
                    chapter_id="1",
                    revision=1,
                    section_ids=["1.1"],
                    part_refs={"assessment_background": chief_part_ref},
                    revision_responses=[
                        RevisionResponse(
                            finding_id="F-final-1",
                            action="implemented",
                            summary="The requested chapter-local change was implemented.",
                            changed_target_ids=["1.1"],
                        )
                    ],
                ),
            ).model_dump(mode="json"),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    recheck_result_ref = (
        f"Work/runs/{run_id}/reviews/final-chapter-1-recheck-agent-result.json"
    )
    recheck_result_path = tmp_path / recheck_result_ref
    recheck_result_path.parent.mkdir(parents=True, exist_ok=True)
    recheck_result_path.write_text(
        json.dumps(
            AgentResult(
                task_id="final-chapter-1-r1",
                run_id=run_id,
                agent_id="chief-editor-auditor",
                session_id="final-chapter-1",
                status=AgentRunStatus.COMPLETED,
                payload=FinalChapterLaneVerdictSubmission(
                    run_id=run_id,
                    chapter_id="1",
                    checked_section_ids=["1.1", "1.2", "1.3"],
                    verdicts=[
                        ResolutionVerdict(
                            finding_id="F-final-1",
                            verdict="resolved",
                            reason="The revised section now states the requested verification detail.",
                            evidence_refs=[
                                f"Work/runs/{run_id}/edited-revisions/chief-r1.json"
                            ],
                        )
                    ],
                ),
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
                if message.task_id == "invoke-final-recheck-agent":
                    result_path = recheck_result_ref
                    terminal_task_id = "final-chapter-1-r1"
                    terminal_sender = "chief-editor-auditor"
                else:
                    result_path = (
                        chief_result_ref
                        if message.session_id.startswith("chief-chapter-")
                        else result_refs[message.session_id.removeprefix("final-chapter-")]
                    )
                    terminal_task_id = (
                        "chief-chapter-1-r1"
                        if message.session_id.startswith("chief-chapter-")
                        else f"final-chapter-{message.session_id.removeprefix('final-chapter-')}-r0"
                    )
                    terminal_sender = (
                        "chief-editor"
                        if message.session_id.startswith("chief-chapter-")
                        else "chief-editor-auditor"
                    )
                await bus.publish(
                    AgentResultMessage(
                        sender=terminal_sender,
                        workflow_id=message.workflow_id,
                        task_id=terminal_task_id,
                        run_id=message.run_id,
                        result_path=result_path,
                        task_attempt_id=(
                            ""
                        ),
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
        completed = await host.execute(
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
        assert completed.status.value == "completed"
        assert persisted.status.value == "completed"
        assert persisted.actions["run-aggregate-existing"].status.value == "completed"
        assert persisted.actions["project-aggregate-existing-tail"].status.value == "completed"
        assert persisted.actions["run-final-review"].status.value == "completed"
        assert persisted.actions["run-report-delivery"].status.value == "completed"
        final_state = WorkflowState.model_validate(
            persisted.subworkflow_states["run-final-review"]
        )
        assert final_state.status.value == "completed"
        completed_reporting_state = final_state.outputs["result"]
        assert completed_reporting_state["final_review_completion_ref"] == (
            f"Work/runs/{run_id}/reviews/final-completion.json"
        )
        assert completed_reporting_state["final_audit_snapshot_ref"] == (
            f"Work/runs/{run_id}/reviews/final-audit-snapshot.json"
        )
        delivery_state = completed.outputs["result"]
        assert delivery_state["run_id"] == run_id
        assert delivery_state["delivery_status"] == "delivered"
        assert delivery_state["delivery_completion_ref"] == (
            f"Work/runs/{run_id}/delivery-completion.json"
        )
        assert delivery_state["final_review_completion_ref"] == (
            completed_reporting_state["final_review_completion_ref"]
        )
        final_cohort_completed = {
            event.action_id
            for event in events.events
            if event.kind == "action.completed"
            and event.workflow_id == "distribution-final-chapter-cohort"
        }
        assert {
            "prepare-final-chapter-cohort",
            "final-chapter-cohort",
            "reduce-final-chapter-cohort",
            "run-final-review-cycle",
        }.issubset(final_cohort_completed)
        review_completed = {
            event.action_id
            for event in events.events
            if event.kind == "action.completed"
            and event.workflow_id == "distribution-final-review-cycle"
        }
        assert {
            "start-final-review-cycle",
            "final-review-needs-round",
            "advance-final-review-round",
            "run-final-chief-revision-cohort",
            "run-final-recheck-cohort",
            "final-review-needs-round-after-recheck",
            "complete-final-review",
            "finish-final-review-cycle",
        }.issubset(review_completed)
        recheck_lane_completed = {
            event.action_id
            for event in events.events
            if event.kind == "action.completed"
            and event.workflow_id == "distribution-final-recheck-1-lane"
        }
        assert {
            "prepare-current-final-recheck",
            "final-recheck-requires-agent",
            "create-final-recheck-conversation",
            "invoke-final-recheck-agent",
        }.issubset(recheck_lane_completed)
        chief_cohort_completed = {
            event.action_id
            for event in events.events
            if event.kind == "action.completed"
            and event.workflow_id == "distribution-final-chief-revision-cohort"
        }
        assert {
            "final-chief-revision-cohort",
            "join-final-chief-revision-cohort",
            "reduce-final-chief-revision-cohort",
        }.issubset(chief_cohort_completed)
        assert {
            event.action_id
            for event in events.events
            if event.kind == "action.completed"
            and event.workflow_id == "distribution-final-chief-revision-1-lane"
        } >= {
            "accept-current-final-chief-revision",
            "complete-current-final-chief-revision",
        }
        assert completed_reporting_state["chief_candidate_ref"] == (
            f"Work/runs/{run_id}/edited-revisions/chief-r1.json"
        )
        assert completed_reporting_state["edited_report"]["assessment_background"] == (
            "Updated background text with the requested verification detail."
        )
        assert completed_reporting_state["edited_report"]["findings_overview"] == "发现"
        assert completed_reporting_state["edited_report"]["regional_executive_summary"] == (
            "区域摘要"
        )
        chief_output_ref = (
            tmp_path
            / f"Work/runs/{run_id}/reviews/chief-chapter-lane-1-r1.json"
        )
        assert chief_output_ref.is_file()
        assert json.loads(chief_output_ref.read_text(encoding="utf-8"))["revision"] == 1
        chief_input_ref = (
            tmp_path
            / f"Work/runs/{run_id}/context/chief-chapter-1-input-r1.json"
        )
        chief_input = json.loads(chief_input_ref.read_text(encoding="utf-8"))
        assert chief_input["phase"] == "revision"
        assert chief_input["chapter_id"] == "1"
        assert chief_input["section_ids"] == ["1.1", "1.2", "1.3"]
        assert set(chief_input["section_bodies"]) == {"1.1", "1.2", "1.3"}
        assert [finding["id"] for finding in chief_input["assigned_findings"]] == [
            "F-final-1"
        ]
        assert chief_input["source_refs"] == [
            f"Work/runs/{run_id}/context/chief-source-modules/{module_id}.md"
            for module_id in REPORT_MODULE_IDS
        ]
        recheck_input_ref = (
            tmp_path / f"Work/runs/{run_id}/context/final-chapter-1-input-r1.json"
        )
        recheck_input = json.loads(recheck_input_ref.read_text(encoding="utf-8"))
        assert recheck_input["phase"] == "recheck"
        assert recheck_input["chapter_id"] == "1"
        assert set(recheck_input["section_bodies"]) == {"1.1"}
        assert set(recheck_input["unchanged_section_sha256"]) == {"1.2", "1.3"}
        assert recheck_input["unchanged_section_sha256"] == {
            "1.2": hashlib.sha256("发现".encode("utf-8")).hexdigest(),
            "1.3": hashlib.sha256("区域摘要".encode("utf-8")).hexdigest(),
        }
        assert completed_reporting_state["run_id"] == run_id
        aggregate_ref = tmp_path / f"Work/runs/{run_id}/reviews/final-initial-aggregate.json"
        assert aggregate_ref.is_file()
        assert json.loads(aggregate_ref.read_text(encoding="utf-8"))["lane_ids"] == [
            "1",
            "3",
            "4",
        ]
        recheck_aggregate_ref = (
            tmp_path / f"Work/runs/{run_id}/reviews/final-recheck-r1-aggregate.json"
        )
        assert recheck_aggregate_ref.is_file()
        completion_ref = tmp_path / completed_reporting_state["final_review_completion_ref"]
        completion = json.loads(completion_ref.read_text(encoding="utf-8"))
        assert completion["subject_refs"] == [
            f"Work/runs/{run_id}/edited-revisions/chief-r1.json"
        ]
        assert completion["resolved_finding_ids"] == ["F-final-1"]
        assert completion["verdict_refs"] == [
            f"Work/runs/{run_id}/reviews/final-chapter-lane-1-r1.json"
        ]
        snapshot_ref = tmp_path / completed_reporting_state["final_audit_snapshot_ref"]
        snapshot = json.loads(snapshot_ref.read_text(encoding="utf-8"))
        assert snapshot["completion_ref"] == completed_reporting_state[
            "final_review_completion_ref"
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
        assert all(
            '<template_role_skill id="final-auditor"' in loop.received[0].content
            for runtime_id, loop in loops.items()
            if ":chief-editor-auditor:" in runtime_id
        )
        assert all(
            "Recheck only the assigned findings against the current subject." in loop.received[0].content
            for runtime_id, loop in loops.items()
            if ":chief-editor-auditor:" in runtime_id
        )
        initial_messages = [
            message
            for loop in loops.values()
            for message in loop.received
            if message.task_id == "invoke-final-chapter-auditor"
        ]
        assert len(initial_messages) == 3
        assert all(
            message.task_attempt_id == message.task_id
            for message in initial_messages
        )
        recheck_messages = [
            message
            for loop in loops.values()
            for message in loop.received
            if message.task_id == "invoke-final-recheck-agent"
        ]
        assert len(recheck_messages) == 1
        assert recheck_messages[0].task_id == "invoke-final-recheck-agent"
        assert recheck_messages[0].task_attempt_id == recheck_messages[0].task_id
        assert recheck_messages[0].session_id == "final-chapter-1"
        assert '"phase": "recheck"' in recheck_messages[0].content
        assert '<final_lane_specialization chapter_id="1"' in recheck_messages[0].content
        assert len(agent_service.sessions) == 4
        assert persisted.outputs["result"]["delivery_status"] == "delivered"
        run_root = tmp_path / "Work" / "runs" / run_id
        expected_current_run_files = (
            "report/配电安全专家咨询报告.md",
            "report/配电安全专家咨询报告.docx",
            "delivery-receipt.json",
            "delivery-completion.json",
        )
        assert all((run_root / relative).is_file() for relative in expected_current_run_files)
        current_markdown = (
            run_root / "report/配电安全专家咨询报告.md"
        ).read_text(encoding="utf-8")
        public_markdown = (
            tmp_path / "Outputs/Reports/配电安全专家咨询报告.md"
        ).read_text(encoding="utf-8")
        assert current_markdown == public_markdown
        assert "汇总报告" in current_markdown
        assert (tmp_path / "Outputs/Reports/配电安全专家咨询报告.docx").is_file()
        receipt = json.loads(
            (run_root / "delivery-receipt.json").read_text(encoding="utf-8")
        )
        assert receipt["success"] is True
        delivery_dir = run_root / "delivery" / f"{run_id}-{run_id}"
        assert Path(receipt["delivery_dir"]) == delivery_dir
        assert Path(receipt["final_docx"]) == delivery_dir / "配电安全专家咨询报告.docx"
        assert Path(receipt["manifest_path"]) == delivery_dir / "delivery-manifest.json"
        assert Path(receipt["manifest_path"]).is_file()
        completion = json.loads(
            (run_root / "delivery-completion.json").read_text(encoding="utf-8")
        )
        assert completion["status"] == "delivered"
        assert completion["delivery_receipt_ref"] == (
            f"Work/runs/{run_id}/delivery-receipt.json"
        )
        assert completion["final_review_completion_ref"] == (
            completed_reporting_state["final_review_completion_ref"]
        )
        assert completion["output_artifacts"] == delivery_state["output_artifacts"]
        assert {
            artifact["path"] for artifact in delivery_state["output_artifacts"]
        } >= {
            "Outputs/Reports/配电安全专家咨询报告.md",
            "Outputs/Reports/配电安全专家咨询报告.docx",
            f"Work/runs/{run_id}/reviews/final-completion.json",
        }
        assert not any(
            event.kind == "action.failed" and event.action_id == "run-report-delivery"
            for event in events.events
        )
    finally:
        await agent_service.close_workflow("distribution-aggregate-existing-tail")
        bus.shutdown()
        await bus_task


@pytest.mark.asyncio
async def test_public_aggregate_existing_runtime_projects_root_and_freezes_modules(
    tmp_path: Path,
) -> None:
    """Characterize the application-facing root before adding its runtime."""

    from manyselves.capabilities.distribution_reporting.runtime.aggregate_existing import (
        PublicAggregateExistingWorkflowRuntime,
    )

    for module_id in REPORT_MODULE_IDS:
        target = tmp_path / "Outputs" / "Modules" / f"{module_id}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_markdown(module_id), encoding="utf-8")

    runtime = PublicAggregateExistingWorkflowRuntime(tmp_path)
    command_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    with pytest.raises(RuntimeError, match="missing agent adapter"):
        await runtime.start(
            command_id,
            "aggregate-existing",
            {
                "instruction": "汇总五个已完成模块。",
                "target_modules": list(REPORT_MODULE_IDS),
            },
        )

    run_id = f"aggregate-existing-{command_id.hex}"
    from manyselves.capabilities.distribution_reporting.runtime.input_snapshot import (
        RunInputSnapshotStore,
    )
    from manyselves.runtime.capability_binding import CapabilityRunInputError

    snapshot = RunInputSnapshotStore(tmp_path).load(run_id)
    assert {
        item.logical_ref.as_posix()
        for item in snapshot.files
    } >= {
        f"Outputs/Modules/{module_id}.md"
        for module_id in REPORT_MODULE_IDS
    }
    projected = runtime.get_run(run_id)
    assert projected["run"] == {
        "run_id": run_id,
        "capability_id": "distribution-reporting",
        "workflow_id": "aggregate-existing",
        "status": "failed",
        "active": False,
        "task_id": None,
    }
    assert projected["state"]["variables"]["run-id"] == run_id
    assert projected["state"]["variables"]["request"]["instruction"] == (
        "汇总五个已完成模块。"
    )
    assert projected["waiting_input"] == []
    assert runtime.get_outputs(run_id) == {"run_id": run_id, "outputs": []}
    with pytest.raises(CapabilityRunInputError, match="no waiting input"):
        await runtime.provide_input(
            command_id,
            run_id,
            input_id=None,
            values={},
        )
    assert runtime.get_cost(run_id)["run_id"] == run_id
