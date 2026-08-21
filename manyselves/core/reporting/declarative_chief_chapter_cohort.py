"""Reporting-owned runtime bindings for the file-defined Chief chapter wave."""

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
    RecoveryPolicyDefinition,
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
    ChiefChapterLaneSubmission,
    EditedReportSubmission,
    ModuleSubmission,
    TaskEnvelope,
)
from .assets import ReportAssetAssembler
from .input_contracts import ChiefChapterLaneInput
from .models import (
    CHAPTER1_SECTION_IDS,
    CHAPTER3_SECTION_IDS,
    REPORT_MODULE_IDS,
    chapter_section_ids,
)

CHIEF_CHAPTER_IDS = ("1", "3", "4")


class DeclarativeChiefChapterAgentResult(BaseModel):
    """Typed adapter result that keeps one failed Agent inside its branch."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: ChiefChapterLaneSubmission | None = None
    error: str | None = None


class DeclarativeChiefChapterContext(BaseModel):
    """Serializable preparation and result for one declared Chief branch."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal[
        "ready",
        "resumed",
        "accepted",
        "skipped",
        "compatibility",
        "failed",
    ]
    contract: ChiefChapterLaneInput | None = None
    input_ref: str | None = None
    envelope: TaskEnvelope | None = None
    submission: ChiefChapterLaneSubmission | None = None
    output_ref: str | None = None
    error: str | None = None


class DeclarativeChiefChapterOutcome(BaseModel):
    """Serializable terminal outcome joined after all Chief branches drain."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal["completed", "skipped", "failed"]
    submission: ChiefChapterLaneSubmission | None = None
    output_ref: str | None = None
    error: str | None = None


class _ChiefChapterInvoker:
    """Invoke a prepared Chief chapter through the generic Agent port."""

    def __init__(self, runtime: "DeclarativeChiefChapterRuntime") -> None:
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
        _agent: AgentDefinition,
        _task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition | None,
    ) -> AgentInvocationOutcome:
        del task_id
        context = DeclarativeChiefChapterContext.model_validate(value)
        envelope = cast(TaskEnvelope, context.envelope)
        try:
            runner_kwargs: dict[str, Any] = {
                "session_key": conversation.key.value,
            }
            if recovery_policy is not None:
                runner_kwargs["recovery_policy"] = recovery_policy
            payload = await self._runtime._current_runner._agent(
                "chief-editor",
                envelope,
                envelope.input_refs,
                self._runtime._workflow_id,
                **runner_kwargs,
            )
            submission = ChiefChapterLaneSubmission.model_validate(payload)
            result = DeclarativeChiefChapterAgentResult(
                status="completed",
                submission=submission,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            result = DeclarativeChiefChapterAgentResult(
                status="failed",
                error=str(exc),
            )
        return AgentInvocationOutcome(status="ok", result=result)


def register_chief_chapter_lane_specializations(
    definitions: DefinitionRegistry,
) -> dict[str, WorkflowDefinition]:
    """Register the three statically bound Chief chapter definitions."""

    template = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-chief-chapter-lane",
    )
    if not isinstance(template, WorkflowDefinition):
        raise TypeError("distribution-chief-chapter-lane is not a workflow")
    workflows: dict[str, WorkflowDefinition] = {}
    for chapter_id in CHIEF_CHAPTER_IDS:
        workflow_id = f"distribution-chief-chapter-{chapter_id}-lane"
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
                "conversation_key": f"chief-chapter-{chapter_id}",
            },
            workflow_id=workflow_id,
        )
        definitions.register(workflow)
        workflows[workflow_id] = workflow
    return workflows


def compile_chief_chapter_workflows(
    definitions: DefinitionRegistry,
    executors: ExecutorRegistry,
) -> tuple[ResolvedPlan, dict[str, ResolvedPlan]]:
    """Compile the packaged Chief cohort and its three chapter lanes."""

    cohort = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-chief-chapter-cohort",
    )
    if not isinstance(cohort, WorkflowDefinition):
        raise TypeError("distribution-chief-chapter-cohort is not a workflow")
    compiler = WorkflowCompiler(executors)
    lanes = {
        workflow_id: compiler.compile(workflow, definitions)
        for workflow_id, workflow in register_chief_chapter_lane_specializations(
            definitions
        ).items()
    }
    return compiler.compile(cohort, definitions), lanes


class DeclarativeChiefChapterRuntime:
    """Bind file-defined Chief actions to existing Reporting-owned semantics."""

    def __init__(
        self,
        runner: Any,
        state: dict[str, Any],
        workflow_id: str,
        *,
        compatibility_chief: Any | None = None,
    ) -> None:
        self.current_state = state
        self._runner = runner
        self._current_runner = getattr(runner, "_runner", runner)
        self._workflow_id = workflow_id
        self._compatibility_chief = compatibility_chief
        self._compatibility_invoked = False
        self._production = hasattr(self._current_runner, "service") and callable(
            getattr(self._current_runner, "_agent", None)
        )
        self.agent_invokers: Mapping[str, AgentInvoker] = (
            {"chief-editor": _ChiefChapterInvoker(self)} if self._production else {}
        )

    def prepare(self, state: dict[str, Any]) -> dict[str, Any]:
        """Restore typed modules before the declared Parallel starts."""

        _restore_modules(state)
        self.current_state = state
        return deepcopy(state)

    def prepare_lane(self, values: Mapping[str, Any]) -> DeclarativeChiefChapterContext:
        """Prepare or recover one exact initial Chief chapter Agent turn."""

        chapter_id = cast(Literal["1", "3", "4"], str(values["chapter_id"]))
        if not self._production:
            return DeclarativeChiefChapterContext(
                chapter_id=chapter_id,
                status=(
                    "compatibility"
                    if chapter_id == "1" and "chief_candidate_ref" not in self.current_state
                    else "skipped"
                ),
            )
        if "chief_candidate_ref" in self.current_state:
            return DeclarativeChiefChapterContext(
                chapter_id=chapter_id,
                status="resumed",
            )

        state = self.current_state
        run_id = str(state["run_id"])
        active_chapters = self._current_runner._chapter_lane_ids(state)
        if chapter_id not in active_chapters:
            return DeclarativeChiefChapterContext(
                chapter_id=chapter_id,
                status="skipped",
            )
        plan = state.get("special_topic_plan")
        section_ids = (
            tuple(CHAPTER1_SECTION_IDS)
            if chapter_id == "1"
            else (
                tuple(CHAPTER3_SECTION_IDS) if chapter_id == "3" else chapter_section_ids("4", plan)
            )
        )
        baseline_ref = str(
            state.get("cross_review_completion_ref")
            or f"Work/runs/{run_id}/reviews/cross-completion.json"
        )
        source_context, source_refs = self._current_runner._chief_chapter_source_projection(
            state,
            chapter_id,
        )
        contract = ChiefChapterLaneInput(
            phase="initial",
            run_id=run_id,
            subject_ref=baseline_ref,
            chapter_id=chapter_id,
            section_ids=list(section_ids),
            section_bodies={},
            source_context=source_context,
            source_refs=source_refs,
            assigned_findings=[],
            special_topic_plan=plan,
            revision=0,
        )
        input_ref = f"Work/runs/{run_id}/context/chief-chapter-{chapter_id}-input.json"
        existing_input_matches = False
        input_path = self._current_runner.service.workspace / input_ref
        if input_path.is_file():
            try:
                existing = ChiefChapterLaneInput.model_validate_json(
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
            section_ids=section_ids,
        )
        if recovered is not None and existing_input_matches:
            submission, output_ref = recovered
            return DeclarativeChiefChapterContext(
                chapter_id=chapter_id,
                status="resumed",
                contract=contract,
                input_ref=input_ref,
                submission=submission,
                output_ref=output_ref,
            )

        envelope = TaskEnvelope(
            task_id=f"chief-chapter-{chapter_id}",
            run_id=run_id,
            agent_id="chief-editor",
            objective=f"仅完成报告第{chapter_id}章的总编正文分段；不得输出其他章节。",
            input_refs=[input_ref],
            constraints=[
                f"只处理 Chapter {chapter_id} 的 section_ids={','.join(contract.section_ids)}",
                "source_context/source_refs 是本 lane 唯一事实边界；不得内联或复述其他章节正文",
                "每个分段必须先用 write_result_part 持久化，再提交 part_refs",
                (
                    "Chapter 1/3 的每个 part 只含对应 section body；禁止任何编号 Markdown 标题，运行时负责装配标题"
                    if chapter_id != "4"
                    else "Chapter 4 必须按计划保留全部且仅保留 ### 4.n 顶层小节；允许在匹配父节内使用 #### 4.n.m 等从属小标题"
                ),
                "submit_result 只提交 chief_chapter_lane_submission，不得提交完整 EditedReportSubmission",
            ],
            allowed_outputs=["chief_chapter_lane_submission"],
            allowed_tools=["write_result_part", "list_result_parts", "submit_result"],
            revision=0,
            target_submodule_ids=[],
            input_contract_kind="chief_chapter_lane_input",
            input_contract_ref=input_ref,
            artifact_delivery_modes={input_ref: "inline"},
            inline_context=self._current_runner._chief_template_skill_context(
                state,
                (chapter_id,),
            ),
        )
        return DeclarativeChiefChapterContext(
            chapter_id=chapter_id,
            status="ready",
            contract=contract,
            input_ref=input_ref,
            envelope=envelope,
        )

    @staticmethod
    def requires_agent(context: DeclarativeChiefChapterContext) -> bool:
        """Return the explicit Agent branch decision."""

        return context.status == "ready"

    def accept_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeChiefChapterContext:
        """Validate and persist one typed Chief chapter submission."""

        context = DeclarativeChiefChapterContext.model_validate(values["context"])
        result = DeclarativeChiefChapterAgentResult.model_validate(values["result"])
        if result.status == "failed":
            self._record_lane_failure(context.chapter_id, result.error)
            return context.model_copy(update={"status": "failed", "error": result.error})
        submission = cast(ChiefChapterLaneSubmission, result.submission)
        contract = cast(ChiefChapterLaneInput, context.contract)
        try:
            if (
                submission.run_id != contract.run_id
                or submission.chapter_id != context.chapter_id
                or set(submission.section_ids) != set(contract.section_ids)
                or submission.revision != 0
            ):
                raise ValueError(
                    f"chief chapter {context.chapter_id} returned an out-of-scope submission"
                )
            output_ref = (
                f"Work/runs/{contract.run_id}/reviews/"
                f"chief-chapter-lane-{context.chapter_id}-r0.json"
            )
            self._current_runner.service.store.write_json(
                output_ref,
                submission.model_dump(mode="json"),
            )
            self._current_runner._record_recovery_lane(
                self.current_state,
                stage="chief",
                lane_id=context.chapter_id,
                status="completed",
                result_ref=output_ref,
                revision=0,
            )
        except BaseException as exc:
            self._record_lane_failure(context.chapter_id, str(exc))
            return context.model_copy(update={"status": "failed", "error": str(exc)})
        return context.model_copy(
            update={
                "status": "accepted",
                "submission": submission,
                "output_ref": output_ref,
            }
        )

    async def complete_lane(
        self,
        context: DeclarativeChiefChapterContext,
    ) -> DeclarativeChiefChapterOutcome:
        """Promote one terminal branch without mutating sibling branch state."""

        context = DeclarativeChiefChapterContext.model_validate(context)
        if context.status == "compatibility":
            try:
                if not self._compatibility_invoked:
                    if self._compatibility_chief is None:
                        result = self._runner._chief_edit(
                            self.current_state,
                            self._workflow_id,
                        )
                    else:
                        result = self._compatibility_chief(self.current_state)
                    if isawaitable(result):
                        await result
                    self._compatibility_invoked = True
            except BaseException as exc:
                return DeclarativeChiefChapterOutcome(
                    chapter_id=context.chapter_id,
                    status="failed",
                    error=str(exc),
                )
            return DeclarativeChiefChapterOutcome(
                chapter_id=context.chapter_id,
                status="completed",
            )
        if context.status == "failed":
            return DeclarativeChiefChapterOutcome(
                chapter_id=context.chapter_id,
                status="failed",
                error=context.error,
            )
        if context.status == "skipped":
            return DeclarativeChiefChapterOutcome(
                chapter_id=context.chapter_id,
                status="skipped",
            )
        return DeclarativeChiefChapterOutcome(
            chapter_id=context.chapter_id,
            status="completed",
            submission=context.submission,
            output_ref=context.output_ref,
        )

    def reduce(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Deterministically assemble the drained chapter outcomes."""

        if not self._production:
            outcomes = {
                chapter_id: DeclarativeChiefChapterOutcome.model_validate(outcome)
                for chapter_id, outcome in dict(values["outcomes"]).items()
            }
            failures = {
                chapter_id: outcome.error or "Chief chapter lane failed"
                for chapter_id, outcome in outcomes.items()
                if outcome.status == "failed"
            }
            if failures:
                first = min(failures, key=int)
                raise RuntimeError(failures[first])
            return deepcopy(self.current_state)
        state = deepcopy(dict(values["state"]))
        _restore_modules(state)
        self.current_state = state
        if "chief_candidate_ref" in state:
            return state
        outcomes = {
            chapter_id: DeclarativeChiefChapterOutcome.model_validate(outcome)
            for chapter_id, outcome in dict(values["outcomes"]).items()
        }
        failures = {
            chapter_id: outcome.error or "Chief chapter lane failed"
            for chapter_id, outcome in outcomes.items()
            if outcome.status == "failed"
        }
        if failures:
            first = min(failures, key=int)
            raise RuntimeError(failures[first])

        active_chapters = self._current_runner._chapter_lane_ids(state)
        run_id = str(state["run_id"])
        plan = state.get("special_topic_plan")
        chapter_sections = {
            "1": tuple(CHAPTER1_SECTION_IDS),
            "3": tuple(CHAPTER3_SECTION_IDS),
            **({"4": chapter_section_ids("4", plan)} if plan is not None else {}),
        }
        section_bodies: dict[str, str] = {}
        lane_refs: dict[str, str] = {}
        for chapter_id in active_chapters:
            outcome = outcomes[chapter_id]
            submission = cast(ChiefChapterLaneSubmission, outcome.submission)
            output_ref = cast(str, outcome.output_ref)
            section_bodies.update(
                self._current_runner._read_chief_chapter_parts(
                    state,
                    chapter_id,
                    submission,
                    f"chief-chapter-{chapter_id}",
                )
            )
            lane_refs[chapter_id] = output_ref

        approved_module_text = {
            module_id: self._current_runner._approved_module_text(
                state["module_submissions"][module_id]
            )
            for module_id in REPORT_MODULE_IDS
        }
        claims = [
            claim
            for module_id in REPORT_MODULE_IDS
            for claim in state["module_submissions"][module_id].claims
        ]
        special_topic_body = self._current_runner._render_special_topic_analysis(
            {
                section_id: section_bodies[section_id]
                for section_id in chapter_sections.get("4", ())
            },
            plan,
        )
        edited = EditedReportSubmission(
            title="配电安全专家咨询报告",
            assessment_background=section_bodies["1.1"],
            findings_overview=section_bodies["1.2"],
            regional_executive_summary=section_bodies["1.3"],
            module_narratives=approved_module_text,
            risk_panorama=section_bodies["3.1.1"],
            dimension_risk_analysis=section_bodies["3.1.2"],
            data_gap_analysis=section_bodies["3.1.3"],
            improvement_action_plan=section_bodies["3.2"],
            special_topic_plan=plan,
            special_topic_analysis=special_topic_body,
            protected_claim_ids=sorted(claim.id for claim in claims),
            tables=[],
            photo_ids=ReportAssetAssembler.runtime_photo_ids(
                state.get("evidence_items", []),
                state.get("photo_assets", []),
            ),
            unresolved_editorial_issues=[],
            revision_responses=[],
        )
        candidate_ref = f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
        self._current_runner.service.store.write_json(
            candidate_ref,
            edited.model_dump(mode="json"),
        )
        state["edited_report"] = edited
        state["approved_module_text"] = approved_module_text
        state["chief_candidate_ref"] = candidate_ref
        state["chief_chapter_lane_refs"] = lane_refs
        state["chief_editor_session_key"] = "chief-editor"
        state["chief_editor_completion_ref"] = candidate_ref
        state["aggregate_refs"] = {
            **dict(state.get("aggregate_refs", {})),
            "chief": candidate_ref,
        }
        self._current_runner._record_recovery_aggregate(
            state,
            stage="chief",
            lane_ids=list(active_chapters),
            result_ref=candidate_ref,
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
    ) -> tuple[ChiefChapterLaneSubmission, str] | None:
        def reusable(payload: object, _lane_state: object) -> bool:
            if not isinstance(payload, dict):
                return False
            try:
                recovered = ChiefChapterLaneSubmission.model_validate(payload)
                if (
                    recovered.run_id != run_id
                    or recovered.chapter_id != chapter_id
                    or set(recovered.section_ids) != set(section_ids)
                ):
                    return False
                self._current_runner._read_chief_chapter_parts(
                    self.current_state,
                    chapter_id,
                    recovered,
                    f"chief-chapter-{chapter_id}",
                )
            except (OSError, ValueError, RuntimeError):
                return False
            return True

        recovered = self._current_runner._recovery_store(self.current_state).load_completed_lanes(
            "chief",
            [chapter_id],
            business_gate=reusable,
        )
        lane_state = recovered.get(chapter_id)
        result_ref = getattr(lane_state, "result_ref", None)
        expected_ref = f"Work/runs/{run_id}/reviews/chief-chapter-lane-{chapter_id}-r0.json"
        if result_ref != expected_ref:
            return None
        try:
            payload = ChiefChapterLaneSubmission.model_validate_json(
                (self._current_runner.service.workspace / result_ref).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        if payload.revision != 0:
            return None
        return payload, result_ref

    def _record_lane_failure(self, chapter_id: str, error: str | None) -> None:
        self._current_runner._record_recovery_lane(
            self.current_state,
            stage="chief",
            lane_id=chapter_id,
            status="failed",
            error=error or "Chief chapter lane failed",
        )


def retry_failed_chief_chapter_lanes(
    plan: ResolvedPlan,
    state: WorkflowState,
) -> WorkflowState:
    """Retry only Chief branches whose joined typed outcome failed."""

    if state.status is not WorkflowStatus.FAILED:
        return state
    branches = state.parallel_results.get("chief-chapter-cohort", {})
    failed = {
        chapter_id
        for chapter_id in CHIEF_CHAPTER_IDS
        if chapter_id in branches
        and DeclarativeChiefChapterOutcome.model_validate(
            branches[chapter_id][f"outcome-{chapter_id}"]
        ).status
        == "failed"
    }
    if not failed:
        return state
    return retry_parallel_branches(
        plan,
        state,
        parallel_action_id="chief-chapter-cohort",
        branch_ids=failed,
    )


def _restore_modules(state: dict[str, Any]) -> None:
    modules = state.get("module_submissions")
    if not isinstance(modules, Mapping):
        return
    state["module_submissions"] = {
        module_id: ModuleSubmission.model_validate(value) for module_id, value in modules.items()
    }


__all__ = [
    "DeclarativeChiefChapterAgentResult",
    "DeclarativeChiefChapterContext",
    "DeclarativeChiefChapterOutcome",
    "DeclarativeChiefChapterRuntime",
    "compile_chief_chapter_workflows",
    "register_chief_chapter_lane_specializations",
    "retry_failed_chief_chapter_lanes",
]
