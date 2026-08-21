"""Reporting-owned bindings for the file-defined initial Final chapter wave."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from inspect import isawaitable
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from manyselves.kernel.conversations import ConversationRecord
from manyselves.kernel.definitions import (
    AgentDefinition,
    DefinitionKind,
    DefinitionRegistry,
    TaskDefinition,
    WorkflowDefinition,
    specialize_workflow,
)
from manyselves.kernel.executors import ExecutorRegistry
from manyselves.kernel.ports import AgentInvocationOutcome, AgentInvoker
from manyselves.kernel.workflow import (
    ResolvedPlan,
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
from .final_specialization import final_lane_specialization
from .input_contracts import FinalChapterLaneInput
from .models import (
    CHAPTER1_SECTION_IDS,
    CHAPTER3_SECTION_IDS,
    chapter_section_ids,
)

FINAL_CHAPTER_IDS = ("1", "3", "4")


class DeclarativeFinalChapterAgentResult(BaseModel):
    """Typed adapter result that keeps one failed Auditor inside its branch."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: FinalChapterLaneFindingSubmission | None = None
    error: str | None = None


class DeclarativeFinalChapterContext(BaseModel):
    """Serializable initial Final chapter preparation and result."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal["ready", "resumed", "accepted", "skipped", "failed"]
    contract: FinalChapterLaneInput | None = None
    input_ref: str | None = None
    envelope: TaskEnvelope | None = None
    submission: FinalChapterLaneFindingSubmission | None = None
    output_ref: str | None = None
    error: str | None = None


class DeclarativeFinalChapterOutcome(BaseModel):
    """Serializable terminal result joined after the initial Final wave."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal["completed", "skipped", "failed"]
    submission: FinalChapterLaneFindingSubmission | None = None
    output_ref: str | None = None
    error: str | None = None


class _FinalChapterInvoker:
    def __init__(self, runtime: "DeclarativeFinalChapterRuntime") -> None:
        self._runtime = runtime

    async def invoke(
        self,
        _agent: AgentDefinition,
        _task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        del task_id
        context = DeclarativeFinalChapterContext.model_validate(value)
        envelope = cast(TaskEnvelope, context.envelope)
        try:
            payload = await self._runtime._current_runner._agent(
                "chief-editor-auditor",
                envelope,
                envelope.input_refs,
                self._runtime._workflow_id,
                session_key=conversation.key.value,
            )
            result = DeclarativeFinalChapterAgentResult(
                status="completed",
                submission=FinalChapterLaneFindingSubmission.model_validate(payload),
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            result = DeclarativeFinalChapterAgentResult(
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
        *,
        continue_final: Any,
    ) -> None:
        self.current_state = state
        self._current_runner = getattr(runner, "_runner", runner)
        self._workflow_id = workflow_id
        self._continue_final = continue_final
        self._production = hasattr(self._current_runner, "service") and callable(
            getattr(self._current_runner, "_agent", None)
        )
        self.agent_invokers: Mapping[str, AgentInvoker] = (
            {"chief-editor-auditor": _FinalChapterInvoker(self)} if self._production else {}
        )

    def prepare(self, state: dict[str, Any]) -> dict[str, Any]:
        _restore_state(state)
        self.current_state = state
        return deepcopy(state)

    def prepare_lane(self, values: Mapping[str, Any]) -> DeclarativeFinalChapterContext:
        chapter_id = cast(Literal["1", "3", "4"], str(values["chapter_id"]))
        if not self._production or "final_review_completion_ref" in self.current_state:
            return DeclarativeFinalChapterContext(
                chapter_id=chapter_id,
                status="skipped",
            )
        state = self.current_state
        active_chapters = self._current_runner._chapter_lane_ids(state)
        if chapter_id not in active_chapters:
            return DeclarativeFinalChapterContext(
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
            return DeclarativeFinalChapterContext(
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
        return DeclarativeFinalChapterContext(
            chapter_id=chapter_id,
            status="ready",
            contract=contract,
            input_ref=input_ref,
            envelope=envelope,
        )

    @staticmethod
    def requires_agent(context: DeclarativeFinalChapterContext) -> bool:
        return context.status == "ready"

    def accept_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeFinalChapterContext:
        context = DeclarativeFinalChapterContext.model_validate(values["context"])
        result = DeclarativeFinalChapterAgentResult.model_validate(values["result"])
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
        context: DeclarativeFinalChapterContext,
    ) -> DeclarativeFinalChapterOutcome:
        context = DeclarativeFinalChapterContext.model_validate(context)
        if context.status == "failed":
            return DeclarativeFinalChapterOutcome(
                chapter_id=context.chapter_id,
                status="failed",
                error=context.error,
            )
        if context.status == "skipped":
            return DeclarativeFinalChapterOutcome(
                chapter_id=context.chapter_id,
                status="skipped",
            )
        return DeclarativeFinalChapterOutcome(
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
            chapter_id: DeclarativeFinalChapterOutcome.model_validate(outcome)
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

    async def continue_review(self, state: dict[str, Any]) -> dict[str, Any]:
        """Continue current revision/recheck behavior after the declared initial wave."""

        _restore_state(state)
        self.current_state = state
        result = self._continue_final(state)
        if isawaitable(result):
            await result
        return state

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
) -> WorkflowState:
    """Retry only initial Final branches whose typed outcome failed."""

    if state.status is not WorkflowStatus.FAILED:
        return state
    branches = state.parallel_results.get("final-chapter-cohort", {})
    failed = {
        chapter_id
        for chapter_id in FINAL_CHAPTER_IDS
        if chapter_id in branches
        and DeclarativeFinalChapterOutcome.model_validate(
            branches[chapter_id][f"outcome-{chapter_id}"]
        ).status
        == "failed"
    }
    if not failed:
        return state
    return retry_parallel_branches(
        plan,
        state,
        parallel_action_id="final-chapter-cohort",
        branch_ids=failed,
    )


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
    "DeclarativeFinalChapterAgentResult",
    "DeclarativeFinalChapterContext",
    "DeclarativeFinalChapterOutcome",
    "DeclarativeFinalChapterRuntime",
    "compile_final_chapter_workflows",
    "register_final_chapter_lane_specializations",
    "retry_failed_final_chapter_lanes",
]
