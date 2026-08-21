from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from manyselves.core.reporting.agentic_models import (
    EditedReportSubmission,
    FinalChapterLaneFindingSubmission,
)
from manyselves.core.reporting.declarative_final_chapter_cohort import (
    DeclarativeFinalChapterOutcome,
    DeclarativeFinalChapterRuntime,
    compile_final_chapter_workflows,
    retry_failed_final_chapter_lanes,
)
from manyselves.core.reporting.models import REPORT_MODULE_IDS
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.workflow import ReportWorkflowRunner
from manyselves.kernel.definitions import DefinitionKind, WorkflowDefinition
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState, WorkflowStatus
from manyselves.runtime.state_store import InMemoryWorkflowStateStore
from manyselves.runtime.workflow_host import (
    InMemoryWorkflowEventSink,
    WorkflowRuntimeHost,
)


def _final_subject() -> EditedReportSubmission:
    return EditedReportSubmission(
        title="Report",
        assessment_background="background body",
        findings_overview="findings body",
        regional_executive_summary="regional body",
        module_narratives={module_id: f"module {module_id}" for module_id in REPORT_MODULE_IDS},
        risk_panorama="risk body",
        dimension_risk_analysis="dimension body",
        data_gap_analysis="gap body",
        improvement_action_plan="action body",
    )


def _final_runtime_fixture(
    tmp_path: Path,
    run_id: str,
) -> tuple[
    ReportWorkflowRunner,
    dict,
    WorkflowCompiler,
    object,
    dict,
    dict,
]:
    service = SimpleNamespace(workspace=tmp_path, store=ReportingStore(tmp_path))
    runner = ReportWorkflowRunner.__new__(ReportWorkflowRunner)
    runner.service = service
    runner._budget = None
    runner._final_template_skill_context = lambda _state, _chapter_id: "skill:final-auditor"
    subject = _final_subject()
    subject_ref = f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
    service.store.write_json(subject_ref, subject.model_dump(mode="json"))
    state = {
        "run_id": run_id,
        "edited_report": subject,
        "chief_candidate_ref": subject_ref,
        "evidence_items": [],
        "photo_assets": [],
        "preparation_refs": {},
    }
    from manyselves.core.reporting.declarative_reporting_tail import (
        build_reporting_tail_definition,
    )

    definitions, contracts, _tail = build_reporting_tail_definition()
    executors = build_builtin_executor_registry()
    cohort_plan, lane_plans = compile_final_chapter_workflows(definitions, executors)
    workflow = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-final-chapter-cohort",
    )
    assert isinstance(workflow, WorkflowDefinition)
    workflow = workflow.model_copy(deep=True)
    workflow.state["reporting-state"] = state
    compiler = WorkflowCompiler(executors)
    plan = compiler.compile(workflow, definitions)
    return (
        runner,
        state,
        compiler,
        plan,
        contracts,
        {
            "distribution-final-chapter-cohort": cohort_plan,
            **lane_plans,
        },
    )


def _runtime_context(
    runtime: DeclarativeFinalChapterRuntime,
    contracts: dict,
    definitions,
    subworkflows: dict,
) -> RuntimeContext:
    return RuntimeContext(
        tools={
            "prepare-final-chapter-cohort": runtime.prepare,
            "prepare-current-final-chapter": runtime.prepare_lane,
            "final-chapter-initial-requires-agent": runtime.requires_agent,
            "accept-current-final-chapter-initial": runtime.accept_lane,
            "complete-current-final-chapter": runtime.complete_lane,
            "reduce-final-chapter-cohort": runtime.reduce,
            "continue-current-final-review": runtime.continue_review,
        },
        agents=runtime.agent_invokers,
        contracts=contracts,
        definitions=definitions,
        subworkflows=subworkflows,
    )


@pytest.mark.asyncio
async def test_final_cohort_retries_only_failed_chapter_after_join(
    tmp_path: Path,
) -> None:
    """The typed Join keeps completed siblings while retrying one failed lane."""

    run_id = "run-declarative-final-retry"
    runner, state, _compiler, plan, contracts, subworkflows = _final_runtime_fixture(
        tmp_path,
        run_id,
    )
    calls: list[tuple[str, str | None]] = []
    attempts: Counter[str] = Counter()
    continuation_calls: list[str] = []

    async def fake_agent(
        self,
        _agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
    ):
        input_payload = json.loads(
            (tmp_path / envelope.input_contract_ref).read_text(encoding="utf-8")
        )
        chapter_id = input_payload["chapter_id"]
        calls.append((chapter_id, session_key))
        attempts[chapter_id] += 1
        assert session_key == f"final-chapter-{chapter_id}"
        if chapter_id == "3" and attempts[chapter_id] == 1:
            raise RuntimeError("injected final chapter failure")
        return FinalChapterLaneFindingSubmission(
            run_id=run_id,
            chapter_id=chapter_id,
            checked_section_ids=list(input_payload["section_ids"]),
            findings=[],
        )

    runner._agent = MethodType(fake_agent, runner)

    def continue_final(current: dict) -> None:
        continuation_calls.append(str(current["run_id"]))

    executors = build_builtin_executor_registry()
    from manyselves.core.reporting.declarative_reporting_tail import (
        build_reporting_tail_definition,
    )

    definitions, _contracts, _tail = build_reporting_tail_definition()
    runtime = DeclarativeFinalChapterRuntime(
        runner,
        state,
        "workflow",
        continue_final=continue_final,
    )
    store = InMemoryWorkflowStateStore()
    with pytest.raises(RuntimeError, match="injected final chapter failure"):
        await WorkflowRuntimeHost(
            executors,
            store,
            InMemoryWorkflowEventSink(),
        ).execute(
            plan,
            WorkflowState.for_plan(run_id, plan),
            _runtime_context(runtime, contracts, definitions, subworkflows),
        )

    failed = store.load(run_id)
    assert failed.status is WorkflowStatus.FAILED
    assert {chapter_id for chapter_id, _session in calls} == {"1", "3"}
    assert len(calls) == 2
    outcomes = failed.parallel_results["final-chapter-cohort"]
    assert (
        DeclarativeFinalChapterOutcome.model_validate(outcomes["1"]["outcome-1"]).status
        == "completed"
    )
    assert (
        DeclarativeFinalChapterOutcome.model_validate(outcomes["3"]["outcome-3"]).status == "failed"
    )
    assert (
        DeclarativeFinalChapterOutcome.model_validate(outcomes["4"]["outcome-4"]).status
        == "skipped"
    )

    resumed = retry_failed_final_chapter_lanes(plan, failed)
    resumed_runtime = DeclarativeFinalChapterRuntime(
        runner,
        state,
        "workflow",
        continue_final=continue_final,
    )
    completed = await WorkflowRuntimeHost(
        executors,
        store,
        InMemoryWorkflowEventSink(),
    ).execute(
        plan,
        resumed,
        _runtime_context(resumed_runtime, contracts, definitions, subworkflows),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert calls.count(("1", "final-chapter-1")) == 1
    assert calls.count(("3", "final-chapter-3")) == 2
    assert continuation_calls == [run_id]
    assert completed.outputs["result"]["run_id"] == run_id


@pytest.mark.asyncio
async def test_final_cohort_reuses_initial_artifacts_before_continuation(
    tmp_path: Path,
) -> None:
    """A fresh host run consumes valid initial lane artifacts without re-invoking Agents."""

    run_id = "run-declarative-final-recovery"
    runner, state, _compiler, plan, contracts, subworkflows = _final_runtime_fixture(
        tmp_path,
        run_id,
    )
    calls: list[str] = []
    continuation_calls: list[str] = []

    async def fake_agent(
        self,
        _agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
    ):
        input_payload = json.loads(
            (tmp_path / envelope.input_contract_ref).read_text(encoding="utf-8")
        )
        chapter_id = input_payload["chapter_id"]
        calls.append(chapter_id)
        assert session_key == f"final-chapter-{chapter_id}"
        return FinalChapterLaneFindingSubmission(
            run_id=run_id,
            chapter_id=chapter_id,
            checked_section_ids=list(input_payload["section_ids"]),
            findings=[],
        )

    runner._agent = MethodType(fake_agent, runner)

    def continue_final(current: dict) -> None:
        continuation_calls.append(str(current["run_id"]))

    executors = build_builtin_executor_registry()
    from manyselves.core.reporting.declarative_reporting_tail import (
        build_reporting_tail_definition,
    )

    definitions, _contracts, _tail = build_reporting_tail_definition()
    runtime = DeclarativeFinalChapterRuntime(
        runner,
        state,
        "workflow",
        continue_final=continue_final,
    )
    store = InMemoryWorkflowStateStore()
    first = await WorkflowRuntimeHost(
        executors,
        store,
        InMemoryWorkflowEventSink(),
    ).execute(
        plan,
        WorkflowState.for_plan(run_id, plan),
        _runtime_context(runtime, contracts, definitions, subworkflows),
    )
    assert first.status is WorkflowStatus.COMPLETED
    assert sorted(calls) == ["1", "3"]
    assert continuation_calls == [run_id]

    second_runtime = DeclarativeFinalChapterRuntime(
        runner,
        state,
        "workflow",
        continue_final=continue_final,
    )
    second = await WorkflowRuntimeHost(
        executors,
        InMemoryWorkflowStateStore(),
        InMemoryWorkflowEventSink(),
    ).execute(
        plan,
        WorkflowState.for_plan(run_id, plan),
        _runtime_context(second_runtime, contracts, definitions, subworkflows),
    )

    assert second.status is WorkflowStatus.COMPLETED
    assert sorted(calls) == ["1", "3"]
    assert continuation_calls == [run_id, run_id]
    for chapter_id in ("1", "3"):
        input_ref = f"Work/runs/{run_id}/context/final-chapter-{chapter_id}-input-r0.json"
        output_ref = f"Work/runs/{run_id}/reviews/final-chapter-lane-{chapter_id}-r0.json"
        assert (tmp_path / input_ref).is_file()
        assert (tmp_path / output_ref).is_file()
