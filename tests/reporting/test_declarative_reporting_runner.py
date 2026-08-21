import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.agentic_models import (
    ModuleReviewFinding,
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
    TaskEnvelope,
)
from manyselves.core.reporting.declarative_reporting_runner import (
    DeclarativeReportWorkflowRunner,
    _CurrentModuleStages,
    execute_declarative_module_stage,
)
from manyselves.core.reporting.input_contracts import (
    ModuleRevisionInput,
    module_content_view,
)
from manyselves.core.reporting.models import REPORT_MODULE_IDS, ReportRequest
from manyselves.core.reporting.parallel_runtime import LaneCompletion, LaneTaskSpec
from manyselves.core.reporting.review_lifecycle import (
    ModuleInitialReviewAcceptance,
    ModuleInitialReviewPreparation,
    ModuleRecheckAcceptance,
    ModuleRecheckPreparation,
    ModuleRevisionPreparation,
)
from manyselves.core.reporting.service import ReportingRunResult, ReportingService
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY
from manyselves.core.reporting.workflow import ReportWorkflowRunner
from manyselves.core.tools.task_board import TaskBoard
from manyselves.kernel.workflow import WorkflowStatus
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.workflow_host import FileWorkflowEventSink


@pytest.mark.asyncio
async def test_declarative_module_stage_runs_the_current_complete_cohort_as_an_adapter(
    tmp_path: Path,
) -> None:
    state = {"run_id": "report-declarative-module", "completed": []}
    calls: list[tuple[str, ...]] = []

    async def execute_current(requested_modules, current_state, workflow_id) -> None:
        calls.append(tuple(requested_modules))
        current_state["completed"] = list(requested_modules)
        current_state["workflow_id"] = workflow_id

    completed = await execute_declarative_module_stage(
        execute_current=execute_current,
        requested_modules=("2.1", "2.2"),
        state=state,
        workflow_id="full-power-distribution-report:report-declarative-module",
        state_store=FileWorkflowStateStore(tmp_path),
    )

    assert calls == [("2.1", "2.2")]
    assert state["completed"] == ["2.1", "2.2"]
    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.outputs["result"]["run_id"] == "report-declarative-module"
    assert completed.subworkflow_states["run-module-cohort"]["status"] == "completed"
    assert [path.name for path in (tmp_path / "Work" / "runs").iterdir()] == [
        "report-declarative-module"
    ]


@pytest.mark.asyncio
async def test_declarative_module_stage_resumes_the_same_failed_action(
    tmp_path: Path,
) -> None:
    state = {"run_id": "report-declarative-resume"}
    calls = 0

    async def execute_current(_modules, current_state, _workflow_id) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected module failure")
        current_state["module_stage"] = "completed"

    store = FileWorkflowStateStore(tmp_path)
    with pytest.raises(RuntimeError, match="injected module failure"):
        await execute_declarative_module_stage(
            execute_current=execute_current,
            requested_modules=("2.1",),
            state=state,
            workflow_id="workflow-resume",
            state_store=store,
        )

    completed = await execute_declarative_module_stage(
        execute_current=execute_current,
        requested_modules=("2.1",),
        state=state,
        workflow_id="workflow-resume",
        state_store=store,
    )

    assert calls == 2
    assert state["module_stage"] == "completed"
    assert completed.status is WorkflowStatus.COMPLETED


class _EmptyLaneRecovery:
    def load_completed_lanes(self, _stage, _module_ids):
        return {}


class _CurrentLaneRunner:
    def __init__(self, workspace: Path) -> None:
        self.service = SimpleNamespace(workspace=workspace)
        self.calls = {module_id: 0 for module_id in REPORT_MODULE_IDS}
        self.lifecycle = {module_id: [] for module_id in REPORT_MODULE_IDS}
        self.fail_once = "2.2"
        self.fail_review_once = ""
        self.review_findings: set[str] = set()
        self.recheck_open_once = False
        self.recheck_rounds = {module_id: 0 for module_id in REPORT_MODULE_IDS}
        self.revision_numbers = {module_id: 0 for module_id in REPORT_MODULE_IDS}
        self.recheck_verdicts: dict[str, list[str]] = {
            module_id: [] for module_id in REPORT_MODULE_IDS
        }

    def _raise_if_cancel_requested(self, _run_id: str) -> None:
        return None

    def _recovery_store(self, _state: dict) -> _EmptyLaneRecovery:
        return _EmptyLaneRecovery()

    def _recovery_stage_name(self, stage: str) -> str:
        return stage

    def _load_recovery_module_lane(self, *_args):
        return None

    def _start_module_lane_attempt(
        self,
        module_id: str,
        lane_state: dict,
        workflow_id: str,
        _defer_main_exceptions: bool,
        _lane_state_override,
    ) -> SimpleNamespace:
        self.lifecycle[module_id].append("start")
        return SimpleNamespace(
            lane_state=lane_state,
            spec=LaneTaskSpec(
                lane_id=f"module-{module_id}",
                run_id=lane_state["run_id"],
                stage="module",
                module_id=module_id,
            ),
            spec_ref=f"lanes/{module_id}/spec.json",
            lane_attempt_id=f"attempt-{module_id}",
            started_at_ns=1,
            attempt_ref=f"lanes/{module_id}/attempt.json",
        )

    def _prepare_module_authoring(
        self,
        module_id: str,
        lane_state: dict,
        workflow_id: str,
        *,
        review: bool,
        checkpoint: bool,
    ) -> SimpleNamespace:
        self.lifecycle[module_id].append("prepare")
        return SimpleNamespace(
            module_id=module_id,
            state=lane_state,
            workflow_id=workflow_id,
            specialist_id=f"module-{module_id}-specialist",
            envelope=TaskEnvelope(
                task_id=f"module-{module_id}",
                run_id=lane_state["run_id"],
                agent_id=f"module-{module_id}-specialist",
                objective=f"author module {module_id}",
                allowed_outputs=["module_submission"],
            ),
            resumed_payload=lane_state.get("specialist_submissions", {}).get(
                module_id
            ),
            revision=0,
            review=review,
            checkpoint=checkpoint,
        )

    async def _resume_module_authoring(self, context) -> ModuleSubmission | None:
        if context.resumed_payload is not None:
            self.lifecycle[context.module_id].append("resume")
        return context.resumed_payload

    async def _agent(
        self,
        agent_id: str,
        _envelope: TaskEnvelope,
        _artifacts,
        _workflow_id: str,
        *,
        session_key: str | None = None,
    ) -> ModuleSubmission | ModuleReviewFindingSubmission | ModuleRevisionSubmission:
        if agent_id == "evidence-auditor":
            module_id = str(session_key).removeprefix("module-auditor-")
            if "module_review_verdict_submission" in _envelope.allowed_outputs:
                self.lifecycle[module_id].append(f"rechecker:{session_key}")
                verdict = (
                    "open"
                    if self.recheck_open_once and _envelope.revision == 1
                    else "resolved"
                )
                self.recheck_verdicts[module_id].append(verdict)
                return ModuleReviewVerdictSubmission(
                    coverage={
                        "submodule_ids": [
                            next(iter(REPORT_TAXONOMY[module_id].submodules))
                        ]
                    },
                    verdicts=[
                        ResolutionVerdict(
                            finding_id=f"M-{module_id}-initial-r0-1",
                            verdict=verdict,
                            reason=(
                                "The revised narrative now states the requested operational "
                                "consequence within the assigned scope."
                            ),
                            evidence_refs=[f"modules/{module_id}-r1.json"],
                        )
                    ],
                    new_findings=[],
                )
            self.lifecycle[module_id].append(f"reviewer:{session_key}")
            if module_id == self.fail_review_once:
                self.fail_review_once = ""
                raise RuntimeError("injected declarative review failure")
            findings = []
            if module_id in self.review_findings:
                target = next(iter(REPORT_TAXONOMY[module_id].submodules))
                findings = [
                    ModuleReviewFinding(
                        id=f"M-{module_id}-initial-r0-1",
                        target_submodule_id=target,
                        category="analysis_depth",
                        impact="advisory",
                        observation=(
                            "The current narrative does not explain the operational "
                            "consequence of the observed condition."
                        ),
                        evidence_refs=[f"modules/{module_id}.json"],
                        required_change=(
                            "Add a concise operational consequence within the assigned "
                            "submodule and keep the evidence boundary explicit."
                        ),
                        reviewer_checks=[
                            "The revised submodule states the operational consequence."
                        ],
                    )
                ]
            return ModuleReviewFindingSubmission(
                coverage={
                    "submodule_ids": list(REPORT_TAXONOMY[module_id].submodules)
                },
                findings=findings,
            )
        if "module_revision_submission" in _envelope.allowed_outputs:
            module_id = str(session_key).removeprefix("module-")
            target = next(iter(REPORT_TAXONOMY[module_id].submodules))
            finding_id = f"M-{module_id}-initial-r0-1"
            revision = self.revision_numbers[module_id]
            self.lifecycle[module_id].append(f"revision:{session_key}")
            return ModuleRevisionSubmission(
                module_id=module_id,
                base_revision=revision - 1,
                revision=revision,
                submodule_narratives={
                    target: f"{target} revised body with operational consequence"
                },
                claims_upsert=[],
                claim_ids_remove=[],
                source_ids=[],
                unresolved_questions=[],
                revision_responses=[
                    RevisionResponse(
                        finding_id=finding_id,
                        action="implemented",
                        summary=(
                            "Added the requested operational consequence while preserving "
                            "the existing evidence boundary."
                        ),
                        changed_target_ids=[target],
                    )
                ],
            )
        module_id = agent_id.removeprefix("module-").removesuffix("-specialist")
        self.calls[module_id] += 1
        self.lifecycle[module_id].append(f"author:{session_key}")
        if module_id == self.fail_once:
            self.fail_once = ""
            raise RuntimeError("injected declarative lane failure")
        return ModuleSubmission(
            module_id=module_id,
            submodule_narratives={
                submodule_id: f"{submodule_id} body"
                for submodule_id in REPORT_TAXONOMY[module_id].submodules
            },
            claims=[],
            source_ids=[],
            unresolved_questions=[],
            revision=0,
        )

    def _accept_module_authoring(
        self,
        context,
        submission: ModuleSubmission,
    ) -> ModuleSubmission:
        self.lifecycle[context.module_id].append("accept")
        context.state.setdefault("specialist_submissions", {})[
            context.module_id
        ] = submission
        return submission

    async def _module_pipeline(
        self,
        module_id: str,
        lane_state: dict,
        _workflow_id: str,
        **_kwargs,
    ) -> ModuleSubmission:
        self.calls[module_id] += 1
        self.lifecycle[module_id].append("author")
        if module_id == self.fail_once:
            self.fail_once = ""
            raise RuntimeError("injected declarative lane failure")
        submission = ModuleSubmission(
            module_id=module_id,
            submodule_narratives={
                submodule_id: f"{submodule_id} body"
                for submodule_id in REPORT_TAXONOMY[module_id].submodules
            },
            claims=[],
            source_ids=[],
            unresolved_questions=[],
            revision=0,
        )
        lane_state.setdefault("specialist_submissions", {})[module_id] = submission
        return submission

    async def _module_review_loop(
        self,
        module_id: str,
        submission: ModuleSubmission,
        lane_state: dict,
        _workflow_id: str,
        **_kwargs,
    ) -> ModuleSubmission:
        self.lifecycle[module_id].append("review")
        lane_state.setdefault("module_submissions", {})[module_id] = submission
        lane_state.setdefault("specialist_submissions", {})[module_id] = submission
        lane_state.setdefault("module_review_completion_refs", {})[module_id] = (
            f"reviews/{module_id}.json"
        )
        return submission

    async def _prepare_module_initial_review(
        self,
        module_id: str,
        submission: ModuleSubmission,
        lane_state: dict,
        workflow_id: str,
        *,
        initial_scope: set[str],
    ) -> ModuleInitialReviewPreparation:
        self.lifecycle[module_id].append("review-prepare")
        subject_ref = f"modules/{module_id}.json"
        return ModuleInitialReviewPreparation(
            mode="invoke_agent",
            run_id=lane_state["run_id"],
            module_id=module_id,
            lifecycle_id="initial",
            workflow_id=workflow_id,
            reviewer_session_key=f"module-auditor-{module_id}",
            review_root=f"reviews/{module_id}",
            progress_ref=f"reviews/{module_id}/progress.json",
            review_round=0,
            scope=sorted(initial_scope),
            current=submission,
            subject_ref=subject_ref,
            review_input_ref=f"reviews/{module_id}/input-r0.json",
            envelope=TaskEnvelope(
                task_id=f"module-{module_id}-initial-review-r0",
                run_id=lane_state["run_id"],
                agent_id="evidence-auditor",
                objective=f"review module {module_id}",
                allowed_outputs=["module_review_finding_submission"],
            ),
        )

    def _accept_module_initial_review(
        self,
        preparation: ModuleInitialReviewPreparation,
        result: ModuleReviewFindingSubmission,
        lane_state: dict,
    ) -> ModuleInitialReviewAcceptance:
        module_id = preparation.module_id
        self.lifecycle[module_id].append("review-accept")
        submission = preparation.current
        if result.findings:
            return ModuleInitialReviewAcceptance(
                run_id=preparation.run_id,
                module_id=module_id,
                lifecycle_id="initial",
                reviewer_session_key=preparation.reviewer_session_key,
                subject_ref=str(preparation.subject_ref),
                current=submission,
                findings=list(result.findings),
                finding_refs=[f"reviews/{module_id}/findings-r0.json"],
                next_action="revise",
                progress_ref=preparation.progress_ref,
            )
        lane_state.setdefault("module_submissions", {})[module_id] = submission
        lane_state.setdefault("specialist_submissions", {})[module_id] = submission
        lane_state.setdefault("module_review_completion_refs", {})[module_id] = (
            f"reviews/{module_id}.json"
        )
        return ModuleInitialReviewAcceptance(
            run_id=preparation.run_id,
            module_id=module_id,
            lifecycle_id="initial",
            reviewer_session_key=preparation.reviewer_session_key,
            subject_ref=str(preparation.subject_ref),
            current=submission,
            findings=[],
            finding_refs=[f"reviews/{module_id}/findings-r0.json"],
            next_action="completed",
            progress_ref=preparation.progress_ref,
            completion_ref=f"reviews/{module_id}.json",
        )

    async def _prepare_module_revision(
        self,
        subject: ModuleSubmission,
        state: dict,
        workflow_id: str,
        findings: list[ModuleReviewFinding],
    ) -> ModuleRevisionPreparation:
        module_id = subject.module_id
        targets = sorted({finding.target_submodule_id for finding in findings})
        revision = subject.revision + 1
        self.revision_numbers[module_id] = revision
        revision_input = ModuleRevisionInput(
            run_id=state["run_id"],
            module_id=module_id,
            subject_ref=f"modules/{module_id}-r{subject.revision}.json",
            subject=module_content_view(subject, set(targets)),
            target_submodule_ids=targets,
            module_findings=findings,
        )
        return ModuleRevisionPreparation(
            run_id=state["run_id"],
            module_id=module_id,
            workflow_id=workflow_id,
            specialist_id=f"module-{module_id}-specialist",
            session_key=f"module-{module_id}",
            subject=subject,
            revision_input=revision_input,
            input_ref=f"reviews/{module_id}/revision-input.json",
            subject_ref=f"modules/{module_id}-r{subject.revision}.json",
            revision=revision,
            target_submodule_ids=targets,
            required_finding_ids=[finding.id for finding in findings],
            envelope=TaskEnvelope(
                task_id=f"module-revision-r{revision}-{module_id}",
                run_id=state["run_id"],
                agent_id=f"module-{module_id}-specialist",
                objective=f"revise module {module_id}",
                allowed_outputs=["module_revision_submission"],
            ),
        )

    def _accept_module_revision(
        self,
        preparation: ModuleRevisionPreparation,
        result: ModuleRevisionSubmission,
    ) -> tuple[ModuleSubmission, str]:
        module_id = preparation.module_id
        self.lifecycle[module_id].append("revision-accept")
        narratives = dict(preparation.subject.submodule_narratives)
        narratives.update(result.submodule_narratives)
        revised = preparation.subject.model_copy(
            update={
                "submodule_narratives": narratives,
                "revision": result.revision,
                "revision_responses": result.revision_responses,
            }
        )
        return revised, f"modules/{module_id}-r{result.revision}.json"

    async def _prepare_module_recheck(
        self,
        module_id: str,
        current: ModuleSubmission,
        state: dict,
        workflow_id: str,
        *,
        initial_scope: set[str],
        lifecycle_id: str = "initial",
    ) -> ModuleRecheckPreparation:
        self.lifecycle[module_id].append("recheck-prepare")
        review_round = self.recheck_rounds[module_id] + 1
        self.recheck_rounds[module_id] = review_round
        target = next(iter(REPORT_TAXONOMY[module_id].submodules))
        finding = ModuleReviewFinding(
            id=f"M-{module_id}-initial-r0-1",
            target_submodule_id=target,
            category="analysis_depth",
            impact="advisory",
            observation=(
                "The current narrative does not explain the operational "
                "consequence of the observed condition."
            ),
            evidence_refs=[f"modules/{module_id}.json"],
            required_change=(
                "Add a concise operational consequence within the assigned "
                "submodule and keep the evidence boundary explicit."
            ),
            reviewer_checks=[
                "The revised submodule states the operational consequence."
            ],
        )
        return ModuleRecheckPreparation(
            mode="invoke_agent",
            run_id=state["run_id"],
            module_id=module_id,
            lifecycle_id=lifecycle_id,
            workflow_id=workflow_id,
            reviewer_session_key=f"module-auditor-{module_id}",
            review_root=f"reviews/{module_id}",
            progress_ref=f"reviews/{module_id}/progress.json",
            review_round=review_round,
            scope=[target],
            current=current,
            pending=[finding],
            responses=current.revision_responses,
            finding_refs=[f"reviews/{module_id}/findings-r0.json"],
            subject_ref=f"modules/{module_id}-r{current.revision}.json",
            envelope=TaskEnvelope(
                task_id=f"module-{module_id}-initial-review-r{review_round}",
                run_id=state["run_id"],
                agent_id="evidence-auditor",
                objective=f"recheck module {module_id}",
                allowed_outputs=["module_review_verdict_submission"],
                revision=review_round,
            ),
        )

    async def _accept_module_recheck(
        self,
        preparation: ModuleRecheckPreparation,
        _result: ModuleReviewVerdictSubmission,
        _state: dict,
    ) -> ModuleRecheckAcceptance:
        module_id = preparation.module_id
        self.lifecycle[module_id].append("recheck-accept")
        if self.recheck_open_once and preparation.review_round == 1:
            return ModuleRecheckAcceptance(
                run_id=preparation.run_id,
                module_id=module_id,
                lifecycle_id=preparation.lifecycle_id,
                reviewer_session_key=preparation.reviewer_session_key,
                subject_ref=str(preparation.subject_ref),
                current=preparation.current,
                findings=list(preparation.pending),
                finding_refs=preparation.finding_refs,
                verdict_refs=[f"reviews/{module_id}/verdicts-r1.json"],
                resolved_ids=[],
                next_action="continue_existing",
                progress_ref=preparation.progress_ref,
            )
        return ModuleRecheckAcceptance(
            run_id=preparation.run_id,
            module_id=module_id,
            lifecycle_id=preparation.lifecycle_id,
            reviewer_session_key=preparation.reviewer_session_key,
            subject_ref=str(preparation.subject_ref),
            current=preparation.current,
            findings=[],
            finding_refs=preparation.finding_refs,
            verdict_refs=[
                f"reviews/{module_id}/verdicts-r{preparation.review_round}.json"
            ],
            resolved_ids=[f"M-{module_id}-initial-r0-1"],
            next_action="completed",
            progress_ref=preparation.progress_ref,
            completion_ref=f"reviews/{module_id}/completion-r1.json",
        )

    def _complete_module_lane_attempt(
        self,
        context,
        submission: ModuleSubmission,
    ):
        self.lifecycle[context.module_id].append("complete")
        completion = LaneCompletion(
            lane_id=f"module-{context.module_id}",
            run_id=context.lane_state["run_id"],
            stage="module",
            module_id=context.module_id,
            result_ref=f"modules/{context.module_id}.json",
        )
        return (
            submission,
            f"lanes/{context.module_id}.json",
            completion,
            context.lane_state,
        )

    def _fail_module_lane_attempt(self, context, _exc: BaseException) -> None:
        self.lifecycle[context.module_id].append("fail")
        return None

    def _finalize_module_lanes(
        self,
        requested_modules,
        state,
        _workflow_id,
        _results,
        failures,
        _failures_by_module,
    ) -> None:
        if failures:
            raise failures[0]
        state["cohort_finalized"] = list(requested_modules)


@pytest.mark.asyncio
async def test_top_level_runtime_retries_only_the_failed_file_defined_module_branch(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-cohort-retry"
    workflow_id = f"full-power-distribution-report:{run_id}"
    requested = ("2.1", "2.2")
    state = {"run_id": run_id}
    runner = _CurrentLaneRunner(tmp_path)
    store = FileWorkflowStateStore(tmp_path)

    with pytest.raises(RuntimeError, match="injected declarative lane failure"):
        await execute_declarative_module_stage(
            requested_modules=requested,
            state=state,
            workflow_id=workflow_id,
            state_store=store,
            module_runtime=_CurrentModuleStages(
                runner,
                requested,
                state,
                workflow_id,
            ),
        )

    completed = await execute_declarative_module_stage(
        requested_modules=requested,
        state=state,
        workflow_id=workflow_id,
        state_store=store,
        module_runtime=_CurrentModuleStages(
            runner,
            requested,
            state,
            workflow_id,
        ),
    )

    assert runner.calls["2.1"] == 1
    assert runner.calls["2.2"] == 2
    assert runner.lifecycle["2.1"] == [
        "start",
        "prepare",
        "author:specialist-2.1",
        "accept",
        "review-prepare",
        "reviewer:module-auditor-2.1",
        "review-accept",
        "complete",
    ]
    assert runner.lifecycle["2.2"] == [
        "start",
        "prepare",
        "author:specialist-2.2",
        "fail",
        "start",
        "prepare",
        "author:specialist-2.2",
        "accept",
        "review-prepare",
        "reviewer:module-auditor-2.2",
        "review-accept",
        "complete",
    ]
    assert all(runner.calls[module_id] == 0 for module_id in REPORT_MODULE_IDS[2:])
    assert state["cohort_finalized"] == ["2.1", "2.2"]
    assert completed.subworkflow_states["run-module-cohort"]["status"] == "completed"


@pytest.mark.asyncio
async def test_file_defined_module_author_reuses_same_run_submission_without_agent(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-author-resume"
    module_id = "2.1"
    submission = ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: f"{submodule_id} body"
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )
    state = {
        "run_id": run_id,
        "resume": True,
        "specialist_submissions": {module_id: submission},
    }
    runner = _CurrentLaneRunner(tmp_path)

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=f"full-power-distribution-report:{run_id}",
        state_store=FileWorkflowStateStore(tmp_path),
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            f"full-power-distribution-report:{run_id}",
        ),
    )

    assert runner.calls[module_id] == 0
    assert runner.lifecycle[module_id] == [
        "start",
        "prepare",
        "resume",
        "review-prepare",
        "reviewer:module-auditor-2.1",
        "review-accept",
        "complete",
    ]
    assert state["module_submissions"][module_id] == submission
    assert completed.status is WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_file_defined_initial_reviewer_failure_drains_and_retries_its_lane(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-review-retry"
    requested = ("2.1", "2.2")
    workflow_id = f"full-power-distribution-report:{run_id}"
    state = {"run_id": run_id}
    runner = _CurrentLaneRunner(tmp_path)
    runner.fail_once = ""
    runner.fail_review_once = "2.2"
    store = FileWorkflowStateStore(tmp_path)

    with pytest.raises(RuntimeError, match="injected declarative review failure"):
        await execute_declarative_module_stage(
            requested_modules=requested,
            state=state,
            workflow_id=workflow_id,
            state_store=store,
            module_runtime=_CurrentModuleStages(
                runner,
                requested,
                state,
                workflow_id,
            ),
        )

    completed = await execute_declarative_module_stage(
        requested_modules=requested,
        state=state,
        workflow_id=workflow_id,
        state_store=store,
        module_runtime=_CurrentModuleStages(
            runner,
            requested,
            state,
            workflow_id,
        ),
    )

    assert runner.lifecycle["2.1"].count("complete") == 1
    assert runner.lifecycle["2.1"].count("reviewer:module-auditor-2.1") == 1
    assert runner.lifecycle["2.2"].count("fail") == 1
    assert runner.lifecycle["2.2"].count("reviewer:module-auditor-2.2") == 2
    assert completed.status is WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_file_defined_initial_finding_invokes_original_author_revision_once(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-first-revision"
    module_id = "2.1"
    workflow_id = f"full-power-distribution-report:{run_id}"
    state = {"run_id": run_id}
    runner = _CurrentLaneRunner(tmp_path)
    runner.fail_once = ""
    runner.review_findings.add(module_id)

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=workflow_id,
        state_store=FileWorkflowStateStore(tmp_path),
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            workflow_id,
        ),
    )

    assert runner.calls[module_id] == 1
    assert runner.lifecycle[module_id] == [
        "start",
        "prepare",
        "author:specialist-2.1",
        "accept",
        "review-prepare",
        "reviewer:module-auditor-2.1",
        "review-accept",
        "revision:module-2.1",
        "revision-accept",
        "recheck-prepare",
        "rechecker:module-auditor-2.1",
        "recheck-accept",
        "complete",
    ]
    assert runner.revision_numbers[module_id] == 1
    assert completed.status is WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_file_defined_module_recheck_revises_again_before_completion(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-recheck-revision"
    module_id = "2.1"
    workflow_id = f"full-power-distribution-report:{run_id}"
    state = {"run_id": run_id}
    runner = _CurrentLaneRunner(tmp_path)
    runner.fail_once = ""
    runner.review_findings.add(module_id)
    runner.recheck_open_once = True

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=workflow_id,
        state_store=FileWorkflowStateStore(tmp_path),
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            workflow_id,
        ),
    )

    assert runner.lifecycle[module_id] == [
        "start",
        "prepare",
        "author:specialist-2.1",
        "accept",
        "review-prepare",
        "reviewer:module-auditor-2.1",
        "review-accept",
        "revision:module-2.1",
        "revision-accept",
        "recheck-prepare",
        "rechecker:module-auditor-2.1",
        "recheck-accept",
        "revision:module-2.1",
        "revision-accept",
        "recheck-prepare",
        "rechecker:module-auditor-2.1",
        "recheck-accept",
        "complete",
    ]
    assert runner.lifecycle[module_id].count("revision:module-2.1") == 2
    assert runner.lifecycle[module_id].count("rechecker:module-auditor-2.1") == 2
    assert runner.lifecycle[module_id].count("reviewer:module-auditor-2.1") == 1
    assert runner.recheck_verdicts[module_id] == ["open", "resolved"]
    assert "review" not in runner.lifecycle[module_id]
    assert runner.revision_numbers[module_id] == 2
    assert completed.status is WorkflowStatus.COMPLETED


class _TopLevelTailRunner:
    def __init__(self, *, fail_stage: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_stage = fail_stage

    def _fail(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise RuntimeError(f"injected {stage} failure")

    async def _cross_review(self, state: dict, workflow_id: str) -> None:
        self.calls.append("cross")
        self._fail("cross")
        state["cross_review_completion_ref"] = f"{workflow_id}/cross.json"

    async def _chief_edit(self, state: dict, workflow_id: str) -> None:
        self.calls.append("chief")
        self._fail("chief")
        state["chief_candidate_ref"] = f"{workflow_id}/chief.json"
        state["chief_editor_session_key"] = "chief-editor"
        state["approved_module_text"] = {"2.1": "approved"}

    async def _final_review_loop(self, state: dict, workflow_id: str, **kwargs) -> None:
        self.calls.append("final")
        self._fail("final")
        state["final_review_completion_ref"] = f"{workflow_id}/final.json"

    def _deliver(self, state: dict) -> None:
        self.calls.append("delivery")
        self._fail("delivery")
        state["delivery_completion_ref"] = "delivery.json"


@pytest.mark.asyncio
async def test_top_level_runtime_nests_the_file_defined_tail_in_one_run(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-one-state"
    state = {"run_id": run_id}
    tail = _TopLevelTailRunner()

    async def execute_current(_modules, current_state, _workflow_id) -> None:
        current_state["module_submissions"] = {
            module_id: ModuleSubmission(
                module_id=module_id,
                submodule_narratives={
                    submodule_id: f"{submodule_id} body"
                    for submodule_id in REPORT_TAXONOMY[module_id].submodules
                },
                claims=[],
                source_ids=[],
                unresolved_questions=[],
                revision=0,
            )
            for module_id in REPORT_MODULE_IDS
        }

    completed = await execute_declarative_module_stage(
        execute_current=execute_current,
        requested_modules=tuple(REPORT_MODULE_IDS),
        state=state,
        workflow_id=f"full-power-distribution-report:{run_id}",
        state_store=FileWorkflowStateStore(tmp_path),
        tail_runner=tail,
        event_sink=FileWorkflowEventSink(tmp_path),
    )

    assert tail.calls == ["cross", "chief", "final", "delivery"]
    assert state["delivery_completion_ref"] == "delivery.json"
    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.subworkflow_states["run-reporting-tail"]["status"] == "completed"
    assert [path.name for path in (tmp_path / "Work" / "runs").iterdir()] == [run_id]
    events = [
        json.loads(line)
        for line in (
            tmp_path / "Work" / "runs" / run_id / "workflow-events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert [
        (event["kind"], event["action_id"])
        for event in events
        if event["workflow_id"] == "distribution-reporting-tail"
    ] == [
        ("workflow.started", None),
        ("action.started", "run-cross"),
        ("action.completed", "run-cross"),
        ("action.started", "run-chief"),
        ("action.completed", "run-chief"),
        ("action.started", "run-final"),
        ("action.completed", "run-final"),
        ("action.started", "run-delivery"),
        ("action.completed", "run-delivery"),
        ("action.started", "finish-reporting-tail"),
        ("action.completed", "finish-reporting-tail"),
        ("workflow.completed", None),
    ]


@pytest.mark.asyncio
async def test_top_level_runtime_resumes_inside_the_failed_tail_subworkflow(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-tail-resume"
    state = {"run_id": run_id}
    module_calls = 0

    async def execute_current(_modules, current_state, _workflow_id) -> None:
        nonlocal module_calls
        module_calls += 1
        current_state["module_submissions"] = {
            module_id: ModuleSubmission(
                module_id=module_id,
                submodule_narratives={
                    submodule_id: f"{submodule_id} body"
                    for submodule_id in REPORT_TAXONOMY[module_id].submodules
                },
                claims=[],
                source_ids=[],
                unresolved_questions=[],
                revision=0,
            )
            for module_id in REPORT_MODULE_IDS
        }

    store = FileWorkflowStateStore(tmp_path)
    failing_tail = _TopLevelTailRunner(fail_stage="chief")
    with pytest.raises(RuntimeError, match="injected chief failure"):
        await execute_declarative_module_stage(
            execute_current=execute_current,
            requested_modules=tuple(REPORT_MODULE_IDS),
            state=state,
            workflow_id=f"full-power-distribution-report:{run_id}",
            state_store=store,
            tail_runner=failing_tail,
        )

    failed = store.load(run_id)
    child = failed.subworkflow_states["run-reporting-tail"]
    assert child["actions"]["run-cross"]["status"] == "completed"
    assert child["actions"]["run-chief"]["status"] == "failed"

    resumed_tail = _TopLevelTailRunner()
    completed = await execute_declarative_module_stage(
        execute_current=execute_current,
        requested_modules=tuple(REPORT_MODULE_IDS),
        state=state,
        workflow_id=f"full-power-distribution-report:{run_id}",
        state_store=store,
        tail_runner=resumed_tail,
    )

    assert module_calls == 1
    assert failing_tail.calls == ["cross", "chief"]
    assert resumed_tail.calls == ["chief", "final", "delivery"]
    assert completed.status is WorkflowStatus.COMPLETED


class _NoCallProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__("test", model="no-call")

    async def chat(self, *args, **kwargs):
        raise AssertionError("runner selection must not call the Provider")


@pytest.mark.asyncio
async def test_reporting_service_selects_runner_from_run_identity_and_keeps_legacy_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected: list[str] = []

    async def run_legacy(_runner, _state) -> None:
        selected.append("legacy")

    async def run_declarative(_runner, _state) -> None:
        selected.append("declarative")

    monkeypatch.setattr(ReportWorkflowRunner, "run", run_legacy)
    monkeypatch.setattr(DeclarativeReportWorkflowRunner, "run", run_declarative)
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=_NoCallProvider(),
    )
    monkeypatch.setattr(
        service,
        "_finalize_completed_run",
        lambda result: result,
    )
    request = ReportRequest(
        operation="module_report",
        instruction="Select the requested runtime",
        target_modules=["2.1"],
    )

    legacy = await service._execute_locked(request, "report-legacy-selection")
    declarative = await service._execute_locked(
        request,
        "report-declarative-selection",
    )

    assert legacy == ReportingRunResult(
        run_id="report-legacy-selection",
        status="completed",
    )
    assert declarative == ReportingRunResult(
        run_id="report-declarative-selection",
        status="completed",
    )
    assert selected == ["legacy", "declarative"]
