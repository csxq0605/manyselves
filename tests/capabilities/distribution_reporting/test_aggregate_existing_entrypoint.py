"""Focused characterization for the first aggregate-existing entrypoint slice."""

from __future__ import annotations

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
    assert isinstance(completed.outputs["result"], EditedReportSubmission)
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
    assert EditedReportSubmission.model_validate(repeated.outputs["result"]).title == (
        "汇总报告"
    )
    assert repeated.run_id == run_id
    assert len(invoker.calls) == 1
    assert sum(
        event.kind == "tool.invoked"
        and event.action_id == "prepare-aggregate-existing"
        for event in events.events
    ) == 1
