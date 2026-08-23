from __future__ import annotations

from collections import Counter
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CHIEF_SECTION_RESULT_PART_IDS,
    ChiefChapterLaneSubmission,
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.chief_chapter import (
    DeclarativeChiefChapterOutcome,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
)
from manyselves.core.reporting.declarative_chief_chapter_cohort import (
    DeclarativeChiefChapterRuntime,
    compile_chief_chapter_workflows,
    retry_failed_chief_chapter_lanes,
)
from manyselves.core.reporting.declarative_reporting_tail import (
    build_reporting_tail_definition,
)
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


@pytest.mark.asyncio
async def test_chief_cohort_retries_only_failed_chapter_after_join(
    tmp_path: Path,
) -> None:
    """The file-defined Join drains siblings and retains completed branches."""

    run_id = "run-declarative-chief-retry"
    service = SimpleNamespace(workspace=tmp_path, store=ReportingStore(tmp_path))
    runner = ReportWorkflowRunner.__new__(ReportWorkflowRunner)
    runner.service = service
    runner._budget = None
    runner._approved_module_text = lambda module: f"approved {module.module_id}"
    runner._chief_template_skill_context = (
        lambda _state, chapters: f"skill:chief-editor:{','.join(chapters)}"
    )
    calls: list[str] = []
    attempts: Counter[str] = Counter()

    async def fake_agent(
        self,
        _agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
        recovery_policy=None,
        definition_override=None,
    ):
        assert definition_override is not None
        chapter_id = envelope.task_id.rsplit("-", 1)[-1]
        calls.append(chapter_id)
        attempts[chapter_id] += 1
        assert session_key == f"chief-chapter-{chapter_id}"
        if chapter_id == "3" and attempts[chapter_id] == 1:
            raise RuntimeError("injected chief chapter failure")
        section_ids = {
            "1": ("1.1", "1.2", "1.3"),
            "3": ("3.1.1", "3.1.2", "3.1.3", "3.2"),
        }[chapter_id]
        draft_root = (
            tmp_path
            / "Work"
            / "runs"
            / run_id
            / "drafts"
            / envelope.task_id
            / "r0"
        )
        draft_root.mkdir(parents=True, exist_ok=True)
        refs: dict[str, str] = {}
        for section_id in section_ids:
            part_id = CHIEF_SECTION_RESULT_PART_IDS[section_id]
            part = draft_root / f"{part_id}.md"
            part.write_text(f"declarative body {section_id}", encoding="utf-8")
            refs[part_id] = part.relative_to(tmp_path).as_posix()
        return ChiefChapterLaneSubmission(
            run_id=run_id,
            chapter_id=chapter_id,
            section_ids=list(section_ids),
            part_refs=refs,
            revision=0,
        )

    runner._agent = MethodType(fake_agent, runner)
    state = {
        "run_id": run_id,
        "module_submissions": {
            module_id: ModuleSubmission(
                module_id=module_id,
                submodule_narratives={
                    section_id: "approved"
                    for section_id in REPORT_TAXONOMY[module_id].submodules
                },
                claims=[],
                source_ids=[],
                unresolved_questions=[],
                revision=0,
            )
            for module_id in REPORT_MODULE_IDS
        },
        "cross_review_completion_ref": (
            f"Work/runs/{run_id}/reviews/cross-completion.json"
        ),
        "evidence_items": [],
        "photo_assets": [],
        "preparation_refs": {},
    }

    definitions, contracts, _tail = build_reporting_tail_definition()
    executors = build_builtin_executor_registry()
    cohort_plan, lane_plans = compile_chief_chapter_workflows(
        definitions,
        executors,
    )
    workflow = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-chief-chapter-cohort",
    )
    assert isinstance(workflow, WorkflowDefinition)
    workflow = workflow.model_copy(deep=True)
    workflow.state["reporting-state"] = state
    plan = WorkflowCompiler(executors).compile(workflow, definitions)
    store = InMemoryWorkflowStateStore()
    runtime = DeclarativeChiefChapterRuntime(runner, state, "workflow")

    def context(current: DeclarativeChiefChapterRuntime) -> RuntimeContext:
        return RuntimeContext(
            tools={
                "prepare-chief-chapter-cohort": current.prepare,
                "prepare-current-chief-chapter": current.prepare_lane,
                "chief-chapter-requires-agent": current.requires_agent,
                "accept-current-chief-chapter": current.accept_lane,
                "complete-current-chief-chapter": current.complete_lane,
                "reduce-chief-chapter-cohort": current.reduce,
            },
            agents=current.agent_invokers,
            contracts=contracts,
            definitions=definitions,
            subworkflows={
                "distribution-chief-chapter-cohort": cohort_plan,
                **lane_plans,
            },
        )

    with pytest.raises(RuntimeError, match="injected chief chapter failure"):
        await WorkflowRuntimeHost(
            executors,
            store,
            InMemoryWorkflowEventSink(),
        ).execute(
            plan,
            WorkflowState.for_plan(run_id, plan),
            context(runtime),
        )

    failed = store.load(run_id)
    assert failed.status is WorkflowStatus.FAILED
    assert calls == ["1", "3"]
    outcomes = failed.parallel_results["chief-chapter-cohort"]
    assert DeclarativeChiefChapterOutcome.model_validate(
        outcomes["1"]["outcome-1"]
    ).status == "completed"
    assert DeclarativeChiefChapterOutcome.model_validate(
        outcomes["3"]["outcome-3"]
    ).status == "failed"
    assert DeclarativeChiefChapterOutcome.model_validate(
        outcomes["4"]["outcome-4"]
    ).status == "skipped"

    resumed = retry_failed_chief_chapter_lanes(plan, failed)
    resumed_runtime = DeclarativeChiefChapterRuntime(runner, state, "workflow")
    completed = await WorkflowRuntimeHost(
        executors,
        store,
        InMemoryWorkflowEventSink(),
    ).execute(plan, resumed, context(resumed_runtime))

    assert completed.status is WorkflowStatus.COMPLETED
    assert calls == ["1", "3", "3"]
    result = completed.outputs["result"]
    assert result["edited_report"].assessment_background == "declarative body 1.1"
    assert result["edited_report"].risk_panorama == "declarative body 3.1.1"
    assert set(result["chief_chapter_lane_refs"]) == {"1", "3"}
