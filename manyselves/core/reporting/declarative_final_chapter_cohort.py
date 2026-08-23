"""Reporting-owned bindings for the file-defined initial Final chapter wave."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal, cast

from manyselves.capabilities.distribution_reporting.adapters import (
    project_reporting_agent,
)
from manyselves.capabilities.distribution_reporting.runtime.models import (
    final_chapter as final_chapter_models,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    CHAPTER1_SECTION_IDS,
    CHAPTER3_SECTION_IDS,
    chapter_section_ids,
)
from manyselves.kernel.conversations import ConversationRecord
from manyselves.kernel.definitions import (
    AgentDefinition,
    DefinitionKind,
    DefinitionRegistry,
    RecoveryPolicyDefinition,
    TaskDefinition,
    WorkflowDefinition,
    specialize_workflow,
)
from manyselves.kernel.executors import ExecutorRegistry
from manyselves.kernel.ports import AgentInvocationOutcome, AgentInvoker
from manyselves.kernel.workflow import (
    ActionExecutionStatus,
    ParallelAction,
    ResolvedPlan,
    SubworkflowAction,
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
    retry_parallel_branches,
)

from .agentic_models import (
    EditedReportSubmission,
    FinalChapterLaneFindingSubmission,
    ModuleSubmission,
    TaskEnvelope,
)
from .declarative_task_binding import bind_declared_task
from .final_specialization import final_lane_specialization
from .input_contracts import FinalChapterLaneInput

FINAL_CHAPTER_IDS = ("1", "3", "4")


class _FinalChapterInvoker:
    def __init__(self, runtime: "DeclarativeFinalChapterRuntime") -> None:
        self._runtime = runtime

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        return await self._invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=None,
        )

    async def invoke_with_recovery(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition,
    ) -> AgentInvocationOutcome:
        return await self._invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=recovery_policy,
        )

    async def _invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition | None,
    ) -> AgentInvocationOutcome:
        del task_id
        context = final_chapter_models.DeclarativeFinalChapterContext.model_validate(
            value
        )
        envelope = bind_declared_task(cast(TaskEnvelope, context.envelope), task)
        try:
            runner_kwargs: dict[str, Any] = {
                "session_key": conversation.key.value,
                "definition_override": project_reporting_agent(agent),
            }
            if recovery_policy is not None:
                runner_kwargs["recovery_policy"] = recovery_policy
            payload = await self._runtime._current_runner._agent(
                "chief-editor-auditor",
                envelope,
                envelope.input_refs,
                self._runtime._workflow_id,
                **runner_kwargs,
            )
            result = final_chapter_models.DeclarativeFinalChapterAgentResult(
                status="completed",
                submission=FinalChapterLaneFindingSubmission.model_validate(payload),
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            result = final_chapter_models.DeclarativeFinalChapterAgentResult(
                status="failed",
                error=str(exc),
            )
        return AgentInvocationOutcome(status="ok", result=result)


def register_final_chapter_lane_specializations(
    definitions: DefinitionRegistry,
) -> dict[str, WorkflowDefinition]:
    """Register the three statically bound initial Final chapter definitions."""

    template = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-final-chapter-lane",
    )
    if not isinstance(template, WorkflowDefinition):
        raise TypeError("distribution-final-chapter-lane is not a workflow")
    workflows: dict[str, WorkflowDefinition] = {}
    for chapter_id in FINAL_CHAPTER_IDS:
        workflow_id = f"distribution-final-chapter-{chapter_id}-lane"
        registered = definitions.get(DefinitionKind.WORKFLOW, workflow_id)
        if registered is not None:
            if not isinstance(registered, WorkflowDefinition):
                raise TypeError(f"{workflow_id} is not a workflow")
            workflows[workflow_id] = registered
            continue
        workflow = specialize_workflow(
            template,
            {
                "chapter_id": chapter_id,
                "conversation_key": f"final-chapter-{chapter_id}",
            },
            workflow_id=workflow_id,
        )
        definitions.register(workflow)
        workflows[workflow_id] = workflow
    return workflows


def compile_final_chapter_workflows(
    definitions: DefinitionRegistry,
    executors: ExecutorRegistry,
) -> tuple[ResolvedPlan, dict[str, ResolvedPlan]]:
    """Compile the packaged initial Final cohort and chapter lanes."""

    cohort = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-final-chapter-cohort",
    )
    if not isinstance(cohort, WorkflowDefinition):
        raise TypeError("distribution-final-chapter-cohort is not a workflow")
    compiler = WorkflowCompiler(executors)
    lanes = {
        workflow_id: compiler.compile(workflow, definitions)
        for workflow_id, workflow in register_final_chapter_lane_specializations(
            definitions
        ).items()
    }
    return compiler.compile(cohort, definitions), lanes


class DeclarativeFinalChapterRuntime:
    """Bind initial Final file actions to current Reporting-owned semantics."""

    def __init__(
        self,
        runner: Any,
        state: dict[str, Any],
        workflow_id: str,
    ) -> None:
        self.current_state = state
        self._current_runner = getattr(runner, "_runner", runner)
        self._workflow_id = workflow_id
        self._production = hasattr(self._current_runner, "service") and callable(
            getattr(self._current_runner, "_agent", None)
        )
        self.agent_invokers: Mapping[str, AgentInvoker] = (
            {"chief-editor-auditor": _FinalChapterInvoker(self)} if self._production else {}
        )

    def prepare(self, state: dict[str, Any]) -> dict[str, Any]:
        _restore_state(state)
        if self._production and "final_review_completion_ref" not in state:
            self._current_runner._restore_final_review_completion(state)
        self.current_state = state
        return deepcopy(state)

    def prepare_lane(
        self,
        values: Mapping[str, Any],
    ) -> final_chapter_models.DeclarativeFinalChapterContext:
        chapter_id = cast(Literal["1", "3", "4"], str(values["chapter_id"]))
        if not self._production or "final_review_completion_ref" in self.current_state:
            return final_chapter_models.DeclarativeFinalChapterContext(
                chapter_id=chapter_id,
                status="skipped",
            )
        state = self.current_state
        active_chapters = self._current_runner._chapter_lane_ids(state)
        if chapter_id not in active_chapters:
            return final_chapter_models.DeclarativeFinalChapterContext(
                chapter_id=chapter_id,
                status="skipped",
            )
        run_id = str(state["run_id"])
        plan = state.get("special_topic_plan")
        chapter_sections = (
            tuple(CHAPTER1_SECTION_IDS)
            if chapter_id == "1"
            else (
                tuple(CHAPTER3_SECTION_IDS) if chapter_id == "3" else chapter_section_ids("4", plan)
            )
        )
        current = EditedReportSubmission.model_validate(state["edited_report"])
        subject_ref = str(
            state.get("chief_candidate_ref") or f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
        )
        contract = FinalChapterLaneInput(
            phase="initial",
            run_id=run_id,
            subject_ref=subject_ref,
            chapter_id=chapter_id,
            review_focus=list(final_lane_specialization(chapter_id).review_focus),
            section_ids=list(chapter_sections),
            section_bodies=self._current_runner._final_chapter_section_bodies(
                current,
                chapter_id,
            ),
            required_findings=[],
            revision_responses=[],
            special_topic_plan=plan,
            revision=0,
        )
        input_ref = f"Work/runs/{run_id}/context/final-chapter-{chapter_id}-input-r0.json"
        input_path = self._current_runner.service.workspace / input_ref
        existing_input_matches = False
        if input_path.is_file():
            try:
                existing = FinalChapterLaneInput.model_validate_json(
                    input_path.read_text(encoding="utf-8")
                )
                existing_input_matches = existing.model_dump(mode="json") == contract.model_dump(
                    mode="json"
                )
            except (OSError, ValueError):
                existing_input_matches = False
        self._current_runner.service.store.write_json(
            input_ref,
            contract.model_dump(mode="json"),
        )
        recovered = self._recover_lane(
            chapter_id=chapter_id,
            run_id=run_id,
            section_ids=chapter_sections,
        )
        if recovered is not None and existing_input_matches:
            submission, output_ref = recovered
            return final_chapter_models.DeclarativeFinalChapterContext(
                chapter_id=chapter_id,
                status="resumed",
                contract=contract,
                input_ref=input_ref,
                submission=submission,
                output_ref=output_ref,
            )
        envelope = TaskEnvelope(
            task_id=f"final-chapter-{chapter_id}-r0",
            run_id=run_id,
            agent_id="chief-editor-auditor",
            objective=f"只审查报告第{chapter_id}章指定小节并提交 lane-local findings。",
            input_refs=[input_ref],
            constraints=[
                f"只覆盖 Chapter {chapter_id} section_ids={','.join(contract.section_ids)}",
                "不得复制其他章节正文、全局 EditedReport 或跨章节 finding",
                "提交 final_chapter_lane_finding_submission，findings target_section_ids 必须留在本 lane",
            ],
            allowed_outputs=["final_chapter_lane_finding_submission"],
            allowed_tools=["submit_result"],
            revision=0,
            input_contract_kind="final_chapter_lane_input",
            input_contract_ref=input_ref,
            artifact_delivery_modes={input_ref: "inline"},
            inline_context=self._current_runner._final_template_skill_context(
                state,
                chapter_id,
            ),
        )
        return final_chapter_models.DeclarativeFinalChapterContext(
            chapter_id=chapter_id,
            status="ready",
            contract=contract,
            input_ref=input_ref,
            envelope=envelope,
        )

    @staticmethod
    def requires_agent(
        context: final_chapter_models.DeclarativeFinalChapterContext,
    ) -> bool:
        return context.status == "ready"

    def accept_lane(
        self,
        values: Mapping[str, Any],
    ) -> final_chapter_models.DeclarativeFinalChapterContext:
        context = final_chapter_models.DeclarativeFinalChapterContext.model_validate(
            values["context"]
        )
        result = final_chapter_models.DeclarativeFinalChapterAgentResult.model_validate(
            values["result"]
        )
        if result.status == "failed":
            self._record_failure(context.chapter_id, result.error)
            return context.model_copy(update={"status": "failed", "error": result.error})
        submission = cast(FinalChapterLaneFindingSubmission, result.submission)
        contract = cast(FinalChapterLaneInput, context.contract)
        try:
            if (
                submission.run_id != contract.run_id
                or submission.chapter_id != context.chapter_id
                or set(submission.checked_section_ids) != set(contract.section_ids)
            ):
                raise ValueError(
                    f"final chapter {context.chapter_id} returned an out-of-scope finding lane"
                )
            output_ref = (
                f"Work/runs/{contract.run_id}/reviews/"
                f"final-chapter-lane-{context.chapter_id}-r0.json"
            )
            self._current_runner.service.store.write_json(
                output_ref,
                submission.model_dump(mode="json"),
            )
            self._current_runner._record_recovery_lane(
                self.current_state,
                stage="final-initial",
                lane_id=context.chapter_id,
                status="completed",
                result_ref=output_ref,
                revision=0,
            )
        except BaseException as exc:
            self._record_failure(context.chapter_id, str(exc))
            return context.model_copy(update={"status": "failed", "error": str(exc)})
        return context.model_copy(
            update={
                "status": "accepted",
                "submission": submission,
                "output_ref": output_ref,
            }
        )

    @staticmethod
    def complete_lane(
        context: final_chapter_models.DeclarativeFinalChapterContext,
    ) -> final_chapter_models.DeclarativeFinalChapterOutcome:
        context = final_chapter_models.DeclarativeFinalChapterContext.model_validate(
            context
        )
        if context.status == "failed":
            return final_chapter_models.DeclarativeFinalChapterOutcome(
                chapter_id=context.chapter_id,
                status="failed",
                error=context.error,
            )
        if context.status == "skipped":
            return final_chapter_models.DeclarativeFinalChapterOutcome(
                chapter_id=context.chapter_id,
                status="skipped",
            )
        return final_chapter_models.DeclarativeFinalChapterOutcome(
            chapter_id=context.chapter_id,
            status="completed",
            submission=context.submission,
            output_ref=context.output_ref,
        )

    def reduce(self, values: Mapping[str, Any]) -> dict[str, Any]:
        state = deepcopy(dict(values["state"]))
        _restore_state(state)
        self.current_state = state
        outcomes = {
            chapter_id: final_chapter_models.DeclarativeFinalChapterOutcome.model_validate(
                outcome
            )
            for chapter_id, outcome in dict(values["outcomes"]).items()
        }
        failures = {
            chapter_id: outcome.error or "Final chapter lane failed"
            for chapter_id, outcome in outcomes.items()
            if outcome.status == "failed"
        }
        if failures:
            first = min(failures, key=int)
            raise RuntimeError(failures[first])
        if not self._production or "final_review_completion_ref" in state:
            return state
        active_chapters = self._current_runner._chapter_lane_ids(state)
        run_id = str(state["run_id"])
        initial_projection_ref = f"Work/runs/{run_id}/reviews/final-initial-aggregate.json"
        self._current_runner.service.store.write_json(
            initial_projection_ref,
            {
                "run_id": run_id,
                "stage": "final-initial",
                "status": "completed",
                "lane_ids": list(active_chapters),
                "result_refs": {
                    chapter_id: outcomes[chapter_id].output_ref for chapter_id in active_chapters
                },
            },
        )
        self._current_runner._record_recovery_aggregate(
            state,
            stage="final-initial",
            lane_ids=list(active_chapters),
            result_ref=initial_projection_ref,
            revision=0,
        )
        self.current_state = state
        return deepcopy(state)

    def _recover_lane(
        self,
        *,
        chapter_id: str,
        run_id: str,
        section_ids: tuple[str, ...],
    ) -> tuple[FinalChapterLaneFindingSubmission, str] | None:
        recovered = self._current_runner._recovery_store(self.current_state).load_completed_lanes(
            "final-initial", [chapter_id]
        )
        lane_state = recovered.get(chapter_id)
        result_ref = getattr(lane_state, "result_ref", None)
        expected_ref = f"Work/runs/{run_id}/reviews/final-chapter-lane-{chapter_id}-r0.json"
        if result_ref != expected_ref:
            return None
        try:
            payload = FinalChapterLaneFindingSubmission.model_validate_json(
                (self._current_runner.service.workspace / result_ref).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        if (
            payload.run_id != run_id
            or payload.chapter_id != chapter_id
            or set(payload.checked_section_ids) != set(section_ids)
        ):
            return None
        return payload, result_ref

    def _record_failure(self, chapter_id: str, error: str | None) -> None:
        self._current_runner._record_recovery_lane(
            self.current_state,
            stage="final-initial",
            lane_id=chapter_id,
            status="failed",
            error=error or "Final chapter lane failed",
        )


def retry_failed_final_chapter_lanes(
    plan: ResolvedPlan,
    state: WorkflowState,
    *,
    subworkflows: Mapping[str, ResolvedPlan] | None = None,
) -> WorkflowState:
    """Retry failed initial Final lanes or a failed nested Final review lane.

    The outer Final cohort owns the initial chapter parallel action and the
    review-cycle subworkflow.  A review-cycle failure is persisted recursively
    through the cycle and its Chief/Recheck cohort.  Retry the failed inner
    branches with the Kernel's existing branch helper, then retain that
    runnable child state while resetting each containing subworkflow action.
    """

    if state.status is not WorkflowStatus.FAILED:
        return state
    branches = state.parallel_results.get("final-chapter-cohort", {})
    failed = {
        chapter_id
        for chapter_id in FINAL_CHAPTER_IDS
        if chapter_id in branches
        and final_chapter_models.DeclarativeFinalChapterOutcome.model_validate(
            branches[chapter_id][f"outcome-{chapter_id}"]
        ).status
        == "failed"
    }
    if not failed:
        return _retry_failed_nested_final_review_lane(
            plan,
            state,
            subworkflows=subworkflows,
        )
    return retry_parallel_branches(
        plan,
        state,
        parallel_action_id="final-chapter-cohort",
        branch_ids=failed,
    )


_NESTED_FINAL_REVIEW_RETRY_TARGETS = (
    (
        "run-final-chief-revision-cohort",
        "distribution-final-chief-revision-cohort",
        "final-chief-revision-cohort",
    ),
    (
        "run-final-recheck-cohort",
        "distribution-final-recheck-cohort",
        "final-recheck-cohort",
    ),
)


def _retry_failed_nested_final_review_lane(
    plan: ResolvedPlan,
    state: WorkflowState,
    *,
    subworkflows: Mapping[str, ResolvedPlan] | None,
) -> WorkflowState:
    """Resume one failed Chief/Recheck branch below the Final review cycle."""

    cycle_action = next(
        (action for action in plan.actions if action.id == "run-final-review-cycle"),
        None,
    )
    if not isinstance(cycle_action, SubworkflowAction):
        return state
    cycle_payload = state.subworkflow_states.get(cycle_action.id)
    if cycle_payload is None:
        return state
    cycle_state = WorkflowState.model_validate(cycle_payload)
    cycle_plan = _final_review_subworkflow_plan(
        "distribution-final-review-cycle",
        subworkflows,
    )
    if cycle_plan is None:
        return state

    for (
        action_id,
        workflow_id,
        parallel_action_id,
    ) in _NESTED_FINAL_REVIEW_RETRY_TARGETS:
        nested_payload = cycle_state.subworkflow_states.get(action_id)
        if nested_payload is None:
            continue
        nested_state = WorkflowState.model_validate(nested_payload)
        nested_action = cycle_state.actions.get(action_id)
        if (
            nested_action is not None
            and nested_action.status is not ActionExecutionStatus.FAILED
            and nested_state.status is not WorkflowStatus.FAILED
        ):
            continue
        nested_plan = _final_review_subworkflow_plan(workflow_id, subworkflows)
        if nested_plan is None:
            continue
        parallel = next(
            (action for action in nested_plan.actions if action.id == parallel_action_id),
            None,
        )
        if not isinstance(parallel, ParallelAction):
            continue
        failed_branches = _failed_parallel_branch_ids(
            nested_state,
            parallel,
        )
        if not failed_branches:
            continue
        resumed_nested = retry_parallel_branches(
            nested_plan,
            nested_state,
            parallel_action_id=parallel_action_id,
            branch_ids=failed_branches,
        )
        resumed_cycle = _reset_subworkflow_action(
            cycle_plan,
            cycle_state,
            action_id=action_id,
            child_state=resumed_nested,
        )
        return _reset_subworkflow_action(
            plan,
            state,
            action_id=cycle_action.id,
            child_state=resumed_cycle,
        )
    return state


def _final_review_subworkflow_plan(
    workflow_id: str,
    subworkflows: Mapping[str, ResolvedPlan] | None,
) -> ResolvedPlan | None:
    if subworkflows is not None:
        return subworkflows.get(workflow_id)
    return _compile_final_review_subworkflow(workflow_id)


def _compile_final_review_subworkflow(workflow_id: str) -> ResolvedPlan | None:
    """Compile the packaged Final review child plan when nested recovery needs it."""

    from manyselves.core.reporting.declarative_final_review_cycle import (
        compile_final_review_workflows,
    )
    from manyselves.core.reporting.declarative_reporting_tail import (
        build_reporting_tail_definition,
    )
    from manyselves.kernel.executors import build_builtin_executor_registry

    definitions, _contracts, _workflow = build_reporting_tail_definition()
    plans = compile_final_review_workflows(
        definitions,
        build_builtin_executor_registry(),
    )
    return plans.get(workflow_id)


def _failed_parallel_branch_ids(
    state: WorkflowState,
    parallel: ParallelAction,
) -> set[str]:
    """Find typed failures and raw failed branch states without new validation."""

    failed: set[str] = set()
    results = state.parallel_results.get(parallel.id, {})
    branch_states = state.parallel_states.get(parallel.id, {})
    for branch_id in parallel.branches:
        if _contains_failed_status(results.get(branch_id)):
            failed.add(branch_id)
        branch_payload = branch_states.get(branch_id)
        if branch_payload is None:
            continue
        branch_state = WorkflowState.model_validate(branch_payload)
        if branch_state.status is WorkflowStatus.FAILED:
            failed.add(branch_id)
    return failed


def _contains_failed_status(value: Any) -> bool:
    if isinstance(value, Mapping):
        if value.get("status") == "failed":
            return True
        return any(_contains_failed_status(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_failed_status(item) for item in value)
    return False


def _reset_subworkflow_action(
    plan: ResolvedPlan,
    state: WorkflowState,
    *,
    action_id: str,
    child_state: WorkflowState,
) -> WorkflowState:
    """Make a failed subworkflow action runnable while retaining child progress."""

    resumed = state.model_copy(deep=True)
    action_index = next(
        index for index, action in enumerate(plan.actions) if action.id == action_id
    )
    action_state = resumed.actions[action_id]
    action_state.status = ActionExecutionStatus.PENDING
    action_state.output = None
    action_state.error = None
    resumed.control_frames.pop(action_id, None)
    resumed.subworkflow_states[action_id] = child_state.model_dump(mode="json")
    resumed.status = WorkflowStatus.PENDING
    resumed.waiting_input = None
    resumed.next_action_index = action_index
    resumed.next_action_id = action_id
    return resumed


def _restore_state(state: dict[str, Any]) -> None:
    modules = state.get("module_submissions")
    if isinstance(modules, Mapping):
        state["module_submissions"] = {
            module_id: ModuleSubmission.model_validate(value)
            for module_id, value in modules.items()
        }
    edited = state.get("edited_report")
    if edited is not None:
        state["edited_report"] = EditedReportSubmission.model_validate(edited)


__all__ = [
    "DeclarativeFinalChapterRuntime",
    "compile_final_chapter_workflows",
    "register_final_chapter_lane_specializations",
    "retry_failed_final_chapter_lanes",
]
