from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from manyselves.core.reporting.agentic_models import (
    ChapterScopedFinalReviewFinding,
    ChapterScopedFinalReviewTargetChange,
    ChiefChapterLaneRevisionSubmission,
    ClaimRecord,
    EditedReportSubmission,
    FinalChapterLaneFindingSubmission,
    FinalChapterLaneVerdictSubmission,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
)
from manyselves.core.reporting.declarative_final_chapter_cohort import (
    DeclarativeFinalChapterOutcome,
    DeclarativeFinalChapterRuntime,
    compile_final_chapter_workflows,
    retry_failed_final_chapter_lanes,
)
from manyselves.core.reporting.declarative_final_review_cycle import (
    DeclarativeFinalReviewRuntime,
    compile_final_review_workflows,
    compose_final_review_agent_invokers,
)
from manyselves.core.reporting.input_contracts import ValidationReport
from manyselves.core.reporting.models import (
    CHIEF_SECTION_RESULT_PART_IDS,
    REPORT_MODULE_IDS,
)
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY, compose_module_markdown
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
    module_narratives = {
        module_id: compose_module_markdown(
            module_id,
            {
                section_id: f"module {module_id}"
                for section_id in REPORT_TAXONOMY[module_id].submodules
            },
        )
        for module_id in REPORT_MODULE_IDS
    }
    return EditedReportSubmission(
        title="Report",
        assessment_background="background body",
        findings_overview="findings body",
        regional_executive_summary="regional body",
        module_narratives=module_narratives,
        risk_panorama="risk body",
        dimension_risk_analysis="dimension body",
        data_gap_analysis="gap body",
        improvement_action_plan="action body",
    )


def _chapter_finding(
    finding_id: str,
    section_id: str,
) -> ChapterScopedFinalReviewFinding:
    return ChapterScopedFinalReviewFinding(
        id=finding_id,
        target_section_ids=[section_id],
        target_changes=[
            ChapterScopedFinalReviewTargetChange(
                target_section_id=section_id,
                required_change=(
                    "Revise the affected section with a concrete evidence-bound explanation "
                    "and an explicit verification step."
                ),
                reviewer_checks=[
                    "Confirm the revised section states the evidence boundary and verification step."
                ],
            )
        ],
        category="coverage",
        impact="blocking",
        observation=(
            "The current section omits the evidence boundary and does not explain how the "
            "reported conclusion can be verified."
        ),
        evidence_refs=["Work/runs/evidence/initial-final-review.json"],
    )


def _revision_response(finding: ChapterScopedFinalReviewFinding) -> RevisionResponse:
    return RevisionResponse(
        finding_id=finding.id,
        action="implemented",
        summary=(
            "Updated the assigned section with the requested evidence boundary and a "
            "deterministic verification explanation."
        ),
        changed_target_ids=list(finding.target_section_ids),
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
    runner._chief_template_skill_context = lambda _state, chapters: (
        f"skill:chief-editor:{','.join(chapters)}"
    )

    def validate_final(_state, _canonical, _phase):
        canonical_ref = f"Work/runs/{run_id}/validation/report-chief-candidate-r0.md"
        service.store.write_text(canonical_ref, _canonical)
        service.store.write_json(
            f"Work/runs/{run_id}/reviews/report-integrity-chief-candidate-r0.json",
            ValidationReport(
                run_id=run_id,
                subject_ref=canonical_ref,
                subject_revision=0,
                validator="test-final-structure",
                check_ids=["final_report.fixed_sections_and_markdown"],
                passed=True,
            ).model_dump(mode="json"),
        )

    runner._validate_final_report_structure = validate_final
    subject = _final_subject()
    subject_ref = f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
    service.store.write_json(subject_ref, subject.model_dump(mode="json"))
    state = {
        "run_id": run_id,
        "edited_report": subject,
        "chief_candidate_ref": subject_ref,
        "module_submissions": {
            module_id: ModuleSubmission(
                module_id=module_id,
                submodule_narratives={
                    section_id: f"module {module_id}"
                    for section_id in REPORT_TAXONOMY[module_id].submodules
                },
                claims=[
                    ClaimRecord(
                        id=f"C-{module_id}",
                        module_id=module_id,
                        submodule_id=next(iter(REPORT_TAXONOMY[module_id].submodules)),
                        text=f"module {module_id}",
                        claim_type="technical_interpretation",
                        footnote_required=False,
                    )
                ],
                source_ids=[],
                unresolved_questions=[],
                revision=0,
            )
            for module_id in REPORT_MODULE_IDS
        },
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
    review_plans = compile_final_review_workflows(definitions, executors)
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
            **review_plans,
        },
    )


def _runtime_context(
    runtime: DeclarativeFinalChapterRuntime,
    review_runtime: DeclarativeFinalReviewRuntime,
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
            "start-final-review-cycle": review_runtime.start_cycle,
            "final-review-needs-round": review_runtime.needs_round,
            "advance-final-review-round": review_runtime.advance_round,
            "prepare-current-final-chief-revision": (review_runtime.prepare_chief_revision),
            "final-chief-revision-requires-agent": (review_runtime.chief_revision_requires_agent),
            "accept-current-final-chief-revision": (review_runtime.accept_chief_revision),
            "complete-current-final-chief-revision": (review_runtime.complete_chief_revision),
            "reduce-final-chief-revision-cohort": (review_runtime.reduce_chief_revisions),
            "prepare-current-final-recheck": review_runtime.prepare_recheck,
            "final-recheck-requires-agent": review_runtime.recheck_requires_agent,
            "accept-current-final-recheck": review_runtime.accept_recheck,
            "complete-current-final-recheck": review_runtime.complete_recheck,
            "reduce-final-recheck-cohort": review_runtime.reduce_rechecks,
            "complete-final-review": review_runtime.complete_review,
        },
        agents=compose_final_review_agent_invokers(
            runtime.agent_invokers,
            review_runtime,
        ),
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

    executors = build_builtin_executor_registry()
    from manyselves.core.reporting.declarative_reporting_tail import (
        build_reporting_tail_definition,
    )

    definitions, _contracts, _tail = build_reporting_tail_definition()
    runtime = DeclarativeFinalChapterRuntime(runner, state, "workflow")
    review_runtime = DeclarativeFinalReviewRuntime(runner, state, "workflow")
    store = InMemoryWorkflowStateStore()
    with pytest.raises(RuntimeError, match="injected final chapter failure"):
        await WorkflowRuntimeHost(
            executors,
            store,
            InMemoryWorkflowEventSink(),
        ).execute(
            plan,
            WorkflowState.for_plan(run_id, plan),
            _runtime_context(
                runtime,
                review_runtime,
                contracts,
                definitions,
                subworkflows,
            ),
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
    resumed_runtime = DeclarativeFinalChapterRuntime(runner, state, "workflow")
    resumed_review_runtime = DeclarativeFinalReviewRuntime(runner, state, "workflow")
    completed = await WorkflowRuntimeHost(
        executors,
        store,
        InMemoryWorkflowEventSink(),
    ).execute(
        plan,
        resumed,
        _runtime_context(
            resumed_runtime,
            resumed_review_runtime,
            contracts,
            definitions,
            subworkflows,
        ),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert calls.count(("1", "final-chapter-1")) == 1
    assert calls.count(("3", "final-chapter-3")) == 2
    assert completed.outputs["result"]["final_review_completion_ref"].endswith(
        "/final-completion.json"
    )
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

    executors = build_builtin_executor_registry()
    from manyselves.core.reporting.declarative_reporting_tail import (
        build_reporting_tail_definition,
    )

    definitions, _contracts, _tail = build_reporting_tail_definition()
    runtime = DeclarativeFinalChapterRuntime(runner, state, "workflow")
    review_runtime = DeclarativeFinalReviewRuntime(runner, state, "workflow")
    store = InMemoryWorkflowStateStore()
    first = await WorkflowRuntimeHost(
        executors,
        store,
        InMemoryWorkflowEventSink(),
    ).execute(
        plan,
        WorkflowState.for_plan(run_id, plan),
        _runtime_context(
            runtime,
            review_runtime,
            contracts,
            definitions,
            subworkflows,
        ),
    )
    assert first.status is WorkflowStatus.COMPLETED
    assert sorted(calls) == ["1", "3"]

    second_runtime = DeclarativeFinalChapterRuntime(runner, state, "workflow")
    second_review_runtime = DeclarativeFinalReviewRuntime(runner, state, "workflow")
    second = await WorkflowRuntimeHost(
        executors,
        InMemoryWorkflowStateStore(),
        InMemoryWorkflowEventSink(),
    ).execute(
        plan,
        WorkflowState.for_plan(run_id, plan),
        _runtime_context(
            second_runtime,
            second_review_runtime,
            contracts,
            definitions,
            subworkflows,
        ),
    )

    assert second.status is WorkflowStatus.COMPLETED
    assert sorted(calls) == ["1", "3"]
    assert second.outputs["result"]["final_review_completion_ref"].endswith(
        "/final-completion.json"
    )
    for chapter_id in ("1", "3"):
        input_ref = f"Work/runs/{run_id}/context/final-chapter-{chapter_id}-input-r0.json"
        output_ref = f"Work/runs/{run_id}/reviews/final-chapter-lane-{chapter_id}-r0.json"
        assert (tmp_path / input_ref).is_file()
        assert (tmp_path / output_ref).is_file()


@pytest.mark.asyncio
async def test_final_review_cycle_revises_only_affected_chapter_and_rechecks_new_finding(
    tmp_path: Path,
) -> None:
    """Final findings drive affected-only Chief/recheck rounds on one public subject."""

    run_id = "run-declarative-final-cycle"
    runner, state, _compiler, _initial_plan, contracts, _initial_subworkflows = (
        _final_runtime_fixture(tmp_path, run_id)
    )
    finding_one = _chapter_finding("final-f1", "1.1")
    finding_two = _chapter_finding("final-f2", "1.2")
    initial_sections = {
        "1": ["1.1", "1.2", "1.3"],
        "3": ["3.1.1", "3.1.2", "3.1.3", "3.2"],
    }
    initial_outcomes: dict[str, dict] = {}
    for chapter_id, section_ids in initial_sections.items():
        submission = FinalChapterLaneFindingSubmission(
            run_id=run_id,
            chapter_id=chapter_id,
            checked_section_ids=section_ids,
            findings=[finding_one] if chapter_id == "1" else [],
        )
        output_ref = f"Work/runs/{run_id}/reviews/final-chapter-lane-{chapter_id}-r0.json"
        runner.service.store.write_json(output_ref, submission.model_dump(mode="json"))
        initial_outcomes[chapter_id] = DeclarativeFinalChapterOutcome(
            chapter_id=chapter_id,
            status="completed",
            submission=submission,
            output_ref=output_ref,
        ).model_dump(mode="json")

    calls: list[tuple[str, str, str, str, int]] = []

    async def fake_agent(
        self,
        agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
        recovery_policy=None,
        definition_override=None,
    ):
        assert definition_override is not None
        input_payload = json.loads(
            (tmp_path / envelope.input_contract_ref).read_text(encoding="utf-8")
        )
        chapter_id = input_payload["chapter_id"]
        revision = int(input_payload["revision"])
        calls.append(
            (
                agent_id,
                envelope.task_id,
                str(session_key),
                input_payload["phase"],
                revision,
            )
        )
        assert chapter_id == "1"

        if agent_id == "chief-editor":
            assert input_payload["phase"] == "revision"
            assert session_key == "chief-chapter-1"
            assigned = finding_one if revision == 1 else finding_two
            draft_root = (
                tmp_path / "Work" / "runs" / run_id / "drafts" / envelope.task_id / f"r{revision}"
            )
            draft_root.mkdir(parents=True, exist_ok=True)
            part_refs: dict[str, str] = {}
            for section_id in input_payload["section_ids"]:
                body = input_payload["section_bodies"][section_id]
                if revision == 1 and section_id == "1.1":
                    body = "round one revised evidence-bound background body"
                if revision == 2 and section_id == "1.2":
                    body = "round two revised findings-overview body"
                part_id = CHIEF_SECTION_RESULT_PART_IDS[section_id]
                part = draft_root / f"{part_id}.md"
                part.write_text(body, encoding="utf-8")
                part_refs[part_id] = part.relative_to(tmp_path).as_posix()
            return ChiefChapterLaneRevisionSubmission(
                run_id=run_id,
                base_subject_ref=input_payload["subject_ref"],
                chapter_id=chapter_id,
                revision=revision,
                section_ids=list(input_payload["section_ids"]),
                part_refs=part_refs,
                revision_responses=[_revision_response(assigned)],
            )

        assert agent_id == "chief-editor-auditor"
        assert input_payload["phase"] == "recheck"
        assert session_key == "final-chapter-1"
        required = finding_one if revision == 1 else finding_two
        verdict = ResolutionVerdict(
            finding_id=required.id,
            verdict="resolved",
            reason="The revised section now includes the requested evidence boundary and verification detail.",
            evidence_refs=[f"Work/runs/{run_id}/edited-revisions/chief-r{revision}.json"],
        )
        return FinalChapterLaneVerdictSubmission(
            run_id=run_id,
            chapter_id=chapter_id,
            checked_section_ids=list(input_payload["section_ids"]),
            verdicts=[verdict],
            new_findings=[finding_two] if revision == 1 else [],
        )

    runner._agent = MethodType(fake_agent, runner)
    from manyselves.core.reporting.declarative_reporting_tail import (
        build_reporting_tail_definition,
    )

    definitions, _contracts, _tail = build_reporting_tail_definition()
    executors = build_builtin_executor_registry()
    review_plans = compile_final_review_workflows(definitions, executors)
    cycle_workflow = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-final-review-cycle",
    )
    assert isinstance(cycle_workflow, WorkflowDefinition)
    cycle_workflow = cycle_workflow.model_copy(deep=True)
    cycle_workflow.state["reporting-state"] = state
    cycle_workflow.state["initial-outcomes"] = initial_outcomes
    cycle_plan = WorkflowCompiler(executors).compile(cycle_workflow, definitions)
    runtime = DeclarativeFinalChapterRuntime(runner, state, "workflow")
    review_runtime = DeclarativeFinalReviewRuntime(runner, state, "workflow")
    store = InMemoryWorkflowStateStore()
    completed = await WorkflowRuntimeHost(
        executors,
        store,
        InMemoryWorkflowEventSink(),
    ).execute(
        cycle_plan,
        WorkflowState.for_plan(run_id, cycle_plan),
        _runtime_context(
            runtime,
            review_runtime,
            contracts,
            definitions,
            review_plans,
        ),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert calls == [
        (
            "chief-editor",
            "chief-chapter-1-r1",
            "chief-chapter-1",
            "revision",
            1,
        ),
        (
            "chief-editor-auditor",
            "final-chapter-1-r1",
            "final-chapter-1",
            "recheck",
            1,
        ),
        (
            "chief-editor",
            "chief-chapter-1-r2",
            "chief-chapter-1",
            "revision",
            2,
        ),
        (
            "chief-editor-auditor",
            "final-chapter-1-r2",
            "final-chapter-1",
            "recheck",
            2,
        ),
    ]
    result = completed.outputs["result"]
    subject = EditedReportSubmission.model_validate(result["edited_report"])
    assert subject.assessment_background == "round one revised evidence-bound background body"
    assert subject.findings_overview == "round two revised findings-overview body"
    assert subject.module_narratives == state["edited_report"].module_narratives
    assert result["chief_candidate_ref"].endswith("/edited-revisions/chief-r2.json")
    assert result["final_review_completion_ref"].endswith("/final-completion.json")

    completion = json.loads(
        (tmp_path / "Work" / "runs" / run_id / "reviews" / "final-completion.json").read_text(
            encoding="utf-8"
        )
    )
    assert completion["resolved_finding_ids"] == ["final-f1", "final-f2"]
    assert json.loads(
        (
            tmp_path / "Work" / "runs" / run_id / "context" / "chief-chapter-1-input-r1.json"
        ).read_text(encoding="utf-8")
    )["subject_ref"].endswith("/edited-revisions/chief-r0.json")
    assert json.loads(
        (
            tmp_path / "Work" / "runs" / run_id / "context" / "final-chapter-1-input-r1.json"
        ).read_text(encoding="utf-8")
    )["subject_ref"].endswith("/edited-revisions/chief-r1.json")
    assert json.loads(
        (
            tmp_path / "Work" / "runs" / run_id / "context" / "final-chapter-1-input-r2.json"
        ).read_text(encoding="utf-8")
    )["subject_ref"].endswith("/edited-revisions/chief-r2.json")
