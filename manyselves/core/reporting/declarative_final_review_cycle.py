"""Reporting-owned bindings for file-declared Final revision/recheck rounds."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from manyselves.capabilities.distribution_reporting.adapters import (
    project_reporting_agent,
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
from manyselves.kernel.workflow import ResolvedPlan, WorkflowCompiler

from .agentic_models import (
    ChapterScopedFinalReviewFinding,
    ChiefChapterLaneRevisionSubmission,
    EditedReportSubmission,
    FinalChapterLaneVerdictSubmission,
    ModuleSubmission,
    RevisionResponse,
    TaskEnvelope,
)
from .declarative_final_chapter_cohort import (
    FINAL_CHAPTER_IDS,
    DeclarativeFinalChapterOutcome,
)
from .declarative_task_binding import bind_declared_task
from .final_specialization import final_lane_specialization
from .input_contracts import (
    ChiefChapterLaneInput,
    FinalAuditSnapshot,
    FinalChapterLaneInput,
    ReviewCompletionRecord,
)
from .models import (
    CHAPTER1_SECTION_IDS,
    CHAPTER3_SECTION_IDS,
    REPORT_MODULE_IDS,
    SpecialTopicPlan,
    chapter_section_ids,
)


class DeclarativeFinalVerdictRecord(BaseModel):
    """One persisted Final recheck verdict retained across declared rounds."""

    model_config = ConfigDict(extra="forbid")

    submission: FinalChapterLaneVerdictSubmission
    output_ref: str


class DeclarativeFinalReviewContext(BaseModel):
    """Serializable business state threaded through Final review rounds."""

    model_config = ConfigDict(extra="forbid")

    state: dict[str, Any]
    current: EditedReportSubmission
    subject_ref: str
    findings_by_chapter: dict[str, list[ChapterScopedFinalReviewFinding]]
    pending_by_chapter: dict[str, list[ChapterScopedFinalReviewFinding]]
    initial_lane_refs: dict[str, str]
    initial_residual_risks: list[str] = Field(default_factory=list)
    revision_responses: dict[str, list[RevisionResponse]] = Field(default_factory=dict)
    verdict_history: list[DeclarativeFinalVerdictRecord] = Field(default_factory=list)
    latest_verdict_refs: dict[str, str] = Field(default_factory=dict)
    revision_number: int = 0
    already_completed: bool = False


class DeclarativeFinalChiefRevisionAgentResult(BaseModel):
    """Typed result from one affected Chief revision Agent invocation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: ChiefChapterLaneRevisionSubmission | None = None
    error: str | None = None


class DeclarativeFinalChiefRevisionContext(BaseModel):
    """Prepared or accepted state for one affected Chief revision branch."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal["ready", "resumed", "accepted", "skipped", "failed"]
    contract: ChiefChapterLaneInput | None = None
    input_ref: str | None = None
    envelope: TaskEnvelope | None = None
    submission: ChiefChapterLaneRevisionSubmission | None = None
    output_ref: str | None = None
    parts: dict[str, str] = Field(default_factory=dict)
    error: str | None = None


class DeclarativeFinalChiefRevisionOutcome(BaseModel):
    """Terminal result from one drained Chief revision branch."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal["completed", "skipped", "failed"]
    submission: ChiefChapterLaneRevisionSubmission | None = None
    output_ref: str | None = None
    parts: dict[str, str] = Field(default_factory=dict)
    error: str | None = None


class DeclarativeFinalRecheckAgentResult(BaseModel):
    """Typed result from one affected Final recheck Agent invocation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: FinalChapterLaneVerdictSubmission | None = None
    error: str | None = None


class DeclarativeFinalRecheckContext(BaseModel):
    """Prepared or accepted state for one affected Final recheck branch."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal["ready", "resumed", "accepted", "skipped", "failed"]
    contract: FinalChapterLaneInput | None = None
    input_ref: str | None = None
    envelope: TaskEnvelope | None = None
    submission: FinalChapterLaneVerdictSubmission | None = None
    output_ref: str | None = None
    error: str | None = None


class DeclarativeFinalRecheckOutcome(BaseModel):
    """Terminal result from one drained Final recheck branch."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Literal["1", "3", "4"]
    status: Literal["completed", "skipped", "failed"]
    submission: FinalChapterLaneVerdictSubmission | None = None
    output_ref: str | None = None
    error: str | None = None


class _FinalChiefRevisionInvoker:
    def __init__(self, runtime: "DeclarativeFinalReviewRuntime") -> None:
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
        context = DeclarativeFinalChiefRevisionContext.model_validate(value)
        envelope = bind_declared_task(cast(TaskEnvelope, context.envelope), task)
        try:
            runner_kwargs: dict[str, Any] = {
                "session_key": conversation.key.value,
                "definition_override": project_reporting_agent(agent),
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
            result = DeclarativeFinalChiefRevisionAgentResult(
                status="completed",
                submission=ChiefChapterLaneRevisionSubmission.model_validate(payload),
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            result = DeclarativeFinalChiefRevisionAgentResult(
                status="failed",
                error=str(exc),
            )
        return AgentInvocationOutcome(status="ok", result=result)


class _FinalRecheckInvoker:
    def __init__(self, runtime: "DeclarativeFinalReviewRuntime") -> None:
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
        context = DeclarativeFinalRecheckContext.model_validate(value)
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
            result = DeclarativeFinalRecheckAgentResult(
                status="completed",
                submission=FinalChapterLaneVerdictSubmission.model_validate(payload),
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            result = DeclarativeFinalRecheckAgentResult(
                status="failed",
                error=str(exc),
            )
        return AgentInvocationOutcome(status="ok", result=result)


class _TaskRoutedAgentInvoker:
    """Keep a shared Agent identity while routing its declared Final Task."""

    def __init__(
        self,
        default: AgentInvoker,
        task_id: str,
        routed: AgentInvoker,
    ) -> None:
        self._default = default
        self._task_id = task_id
        self._routed = routed

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
        invoker = self._routed if task.id == self._task_id else self._default
        if recovery_policy is None:
            return await invoker.invoke(
                agent,
                task,
                value,
                conversation,
                task_id=task_id,
            )
        invoke_with_recovery = getattr(invoker, "invoke_with_recovery", None)
        if not callable(invoke_with_recovery):
            raise RuntimeError(
                f"routed agent adapter does not support declared recovery: {agent.id}"
            )
        return await invoke_with_recovery(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=recovery_policy,
        )


def compose_final_review_agent_invokers(
    existing: Mapping[str, AgentInvoker],
    runtime: "DeclarativeFinalReviewRuntime",
) -> dict[str, AgentInvoker]:
    """Add Final round Task routes without replacing existing Agent identities."""

    combined = dict(existing)
    routes = {
        "chief-editor": ("final-chief-chapter-revision", runtime.chief_invoker),
        "chief-editor-auditor": ("final-chapter-recheck", runtime.recheck_invoker),
    }
    for agent_id, (task_id, routed) in routes.items():
        default = combined.get(agent_id)
        combined[agent_id] = (
            routed if default is None else _TaskRoutedAgentInvoker(default, task_id, routed)
        )
    return combined


def register_final_review_lane_specializations(
    definitions: DefinitionRegistry,
) -> dict[str, WorkflowDefinition]:
    """Register static Chapter 1/3/4 specializations for both round phases."""

    workflows: dict[str, WorkflowDefinition] = {}
    for phase in ("chief-revision", "recheck"):
        template_id = f"distribution-final-{phase}-lane"
        template = definitions.require(DefinitionKind.WORKFLOW, template_id)
        if not isinstance(template, WorkflowDefinition):
            raise TypeError(f"{template_id} is not a workflow")
        for chapter_id in FINAL_CHAPTER_IDS:
            workflow_id = f"distribution-final-{phase}-{chapter_id}-lane"
            registered = definitions.get(DefinitionKind.WORKFLOW, workflow_id)
            if registered is None:
                registered = specialize_workflow(
                    template,
                    {
                        "chapter_id": chapter_id,
                        "conversation_key": (
                            f"chief-chapter-{chapter_id}"
                            if phase == "chief-revision"
                            else f"final-chapter-{chapter_id}"
                        ),
                    },
                    workflow_id=workflow_id,
                )
                definitions.register(registered)
            if not isinstance(registered, WorkflowDefinition):
                raise TypeError(f"{workflow_id} is not a workflow")
            workflows[workflow_id] = registered
    return workflows


def compile_final_review_workflows(
    definitions: DefinitionRegistry,
    executors: ExecutorRegistry,
) -> dict[str, ResolvedPlan]:
    """Compile the two round cohorts and every statically bound phase lane."""

    compiler = WorkflowCompiler(executors)
    workflows = register_final_review_lane_specializations(definitions)
    for workflow_id in (
        "distribution-final-review-cycle",
        "distribution-final-chief-revision-cohort",
        "distribution-final-recheck-cohort",
    ):
        workflow = definitions.require(DefinitionKind.WORKFLOW, workflow_id)
        if not isinstance(workflow, WorkflowDefinition):
            raise TypeError(f"{workflow_id} is not a workflow")
        workflows[workflow_id] = workflow
    return {
        workflow_id: compiler.compile(workflow, definitions)
        for workflow_id, workflow in workflows.items()
    }


class DeclarativeFinalReviewRuntime:
    """Bind affected-only Chief revisions and Final rechecks to current semantics."""

    def __init__(self, runner: Any, state: dict[str, Any], workflow_id: str) -> None:
        self.current_state = state
        self._current_runner = getattr(runner, "_runner", runner)
        self._workflow_id = workflow_id
        self._production = hasattr(self._current_runner, "service") and callable(
            getattr(self._current_runner, "_agent", None)
        )
        self._active_revision = 0
        self.chief_invoker: AgentInvoker = _FinalChiefRevisionInvoker(self)
        self.recheck_invoker: AgentInvoker = _FinalRecheckInvoker(self)

    def start_cycle(self, values: Mapping[str, Any]) -> DeclarativeFinalReviewContext:
        state = deepcopy(dict(values["state"]))
        _restore_state(state)
        self.current_state = state
        current = EditedReportSubmission.model_validate(state["edited_report"])
        subject_ref = str(
            state.get("chief_candidate_ref")
            or f"Work/runs/{state['run_id']}/edited-revisions/chief-r0.json"
        )
        outcomes = {
            chapter_id: DeclarativeFinalChapterOutcome.model_validate(outcome)
            for chapter_id, outcome in dict(values["outcomes"]).items()
        }
        if not self._production or "final_review_completion_ref" in state:
            return DeclarativeFinalReviewContext(
                state=state,
                current=current,
                subject_ref=subject_ref,
                findings_by_chapter={},
                pending_by_chapter={},
                initial_lane_refs={},
                already_completed="final_review_completion_ref" in state,
            )
        active = self._current_runner._chapter_lane_ids(state)
        submissions = {
            chapter_id: cast(
                Any,
                outcomes[chapter_id].submission,
            )
            for chapter_id in active
        }
        findings = {chapter_id: list(submissions[chapter_id].findings) for chapter_id in active}
        return DeclarativeFinalReviewContext(
            state=state,
            current=current,
            subject_ref=subject_ref,
            findings_by_chapter=findings,
            pending_by_chapter={
                chapter_id: list(chapter_findings)
                for chapter_id, chapter_findings in findings.items()
                if chapter_findings
            },
            initial_lane_refs={
                chapter_id: cast(str, outcomes[chapter_id].output_ref) for chapter_id in active
            },
            initial_residual_risks=[
                risk for chapter_id in active for risk in submissions[chapter_id].residual_risks
            ],
        )

    @staticmethod
    def needs_round(review: DeclarativeFinalReviewContext) -> bool:
        review = DeclarativeFinalReviewContext.model_validate(review)
        return bool(review.pending_by_chapter) and not review.already_completed

    @staticmethod
    def advance_round(
        review: DeclarativeFinalReviewContext,
    ) -> DeclarativeFinalReviewContext:
        review = DeclarativeFinalReviewContext.model_validate(review)
        if not review.pending_by_chapter:
            return review
        maximum = max(1, int(review.state.get("max_final_review_rounds", 3)))
        next_round = review.revision_number + 1
        if next_round > maximum:
            raise RuntimeError("final chapter review exceeded the maximum revision rounds")
        return review.model_copy(
            update={
                "revision_number": next_round,
                "revision_responses": {},
            }
        )

    def prepare_chief_revision(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeFinalChiefRevisionContext:
        review = DeclarativeFinalReviewContext.model_validate(values["review"])
        chapter_id = cast(Literal["1", "3", "4"], str(values["chapter_id"]))
        self._set_current(review)
        findings = review.pending_by_chapter.get(chapter_id, [])
        if not self._production or not findings:
            return DeclarativeFinalChiefRevisionContext(
                chapter_id=chapter_id,
                status="skipped",
            )
        state = review.state
        run_id = str(state["run_id"])
        revision = review.revision_number
        recovered = self._recover_chief_revision(review, chapter_id)
        if recovered is not None:
            submission, output_ref, parts = recovered
            return DeclarativeFinalChiefRevisionContext(
                chapter_id=chapter_id,
                status="resumed",
                submission=submission,
                output_ref=output_ref,
                parts=parts,
            )
        section_ids = self._chapter_sections(state, chapter_id)
        section_bodies = self._current_runner._final_chapter_section_bodies(
            review.current,
            chapter_id,
        )
        source_context, source_refs = self._current_runner._chief_chapter_source_projection(
            state,
            chapter_id,
        )
        contract = ChiefChapterLaneInput(
            phase="revision",
            run_id=run_id,
            subject_ref=review.subject_ref,
            chapter_id=chapter_id,
            section_ids=list(section_ids),
            section_bodies=section_bodies,
            source_context=source_context,
            source_refs=source_refs,
            assigned_findings=findings,
            special_topic_plan=state.get("special_topic_plan"),
            revision=revision,
        )
        input_ref = f"Work/runs/{run_id}/context/chief-chapter-{chapter_id}-input-r{revision}.json"
        self._current_runner.service.store.write_json(
            input_ref,
            contract.model_dump(mode="json"),
        )
        envelope = TaskEnvelope(
            task_id=f"chief-chapter-{chapter_id}-r{revision}",
            run_id=run_id,
            agent_id="chief-editor",
            objective=f"只修订 Chapter {chapter_id} 被 Final 指定的 finding 小节。",
            input_refs=[input_ref],
            constraints=[
                "只提交 chief_chapter_lane_revision_submission，禁止提交完整报告",
                "part_refs 只能覆盖本章；revision_responses 必须对应本章 findings",
                (
                    "Chapter 1/3 的每个 part 只含对应 section body；禁止任何编号 Markdown 标题，运行时负责装配标题"
                    if chapter_id != "4"
                    else "Chapter 4 必须按计划保留全部且仅保留 ### 4.n 顶层小节；允许在匹配父节内使用 #### 4.n.m 等从属小标题"
                ),
            ],
            allowed_outputs=["chief_chapter_lane_revision_submission"],
            allowed_tools=["write_result_part", "list_result_parts", "submit_result"],
            revision=revision,
            prior_result_ref=review.subject_ref,
            input_contract_kind="chief_chapter_lane_input",
            input_contract_ref=input_ref,
            artifact_delivery_modes={input_ref: "inline"},
            inline_context=self._current_runner._chief_template_skill_context(
                state,
                (chapter_id,),
            ),
        )
        return DeclarativeFinalChiefRevisionContext(
            chapter_id=chapter_id,
            status="ready",
            contract=contract,
            input_ref=input_ref,
            envelope=envelope,
        )

    @staticmethod
    def chief_revision_requires_agent(
        context: DeclarativeFinalChiefRevisionContext,
    ) -> bool:
        return context.status == "ready"

    def accept_chief_revision(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeFinalChiefRevisionContext:
        context = DeclarativeFinalChiefRevisionContext.model_validate(values["context"])
        result = DeclarativeFinalChiefRevisionAgentResult.model_validate(values["result"])
        if result.status == "failed":
            self._record_failure("chief-revision", context.chapter_id, result.error)
            return context.model_copy(update={"status": "failed", "error": result.error})
        submission = cast(ChiefChapterLaneRevisionSubmission, result.submission)
        contract = cast(ChiefChapterLaneInput, context.contract)
        try:
            matches_identity = (
                submission.run_id == contract.run_id
                and submission.base_subject_ref == contract.subject_ref
                and submission.chapter_id == context.chapter_id
                and submission.revision == contract.revision
            )
            if contract.revision == 1:
                matches_identity = matches_identity and {
                    response.finding_id for response in submission.revision_responses
                } == {finding.id for finding in contract.assigned_findings}
            if not matches_identity:
                raise ValueError(f"chief chapter {context.chapter_id} revision identity mismatch")
            task_id = f"chief-chapter-{context.chapter_id}-r{contract.revision}"
            parts = self._current_runner._read_chief_chapter_parts(
                self.current_state,
                context.chapter_id,
                submission,
                task_id,
            )
            output_ref = (
                f"Work/runs/{contract.run_id}/reviews/chief-chapter-lane-"
                f"{context.chapter_id}-r{contract.revision}.json"
            )
            self._current_runner.service.store.write_json(
                output_ref,
                submission.model_dump(mode="json"),
            )
            self._current_runner._record_recovery_lane(
                self.current_state,
                stage=f"chief-revision-r{contract.revision}",
                lane_id=context.chapter_id,
                status="completed",
                result_ref=output_ref,
                revision=contract.revision,
            )
        except BaseException as exc:
            self._record_failure("chief-revision", context.chapter_id, str(exc))
            return context.model_copy(update={"status": "failed", "error": str(exc)})
        return context.model_copy(
            update={
                "status": "accepted",
                "submission": submission,
                "output_ref": output_ref,
                "parts": parts,
            }
        )

    @staticmethod
    def complete_chief_revision(
        context: DeclarativeFinalChiefRevisionContext,
    ) -> DeclarativeFinalChiefRevisionOutcome:
        context = DeclarativeFinalChiefRevisionContext.model_validate(context)
        if context.status == "failed":
            return DeclarativeFinalChiefRevisionOutcome(
                chapter_id=context.chapter_id,
                status="failed",
                error=context.error,
            )
        if context.status == "skipped":
            return DeclarativeFinalChiefRevisionOutcome(
                chapter_id=context.chapter_id,
                status="skipped",
            )
        return DeclarativeFinalChiefRevisionOutcome(
            chapter_id=context.chapter_id,
            status="completed",
            submission=context.submission,
            output_ref=context.output_ref,
            parts=context.parts,
        )

    def reduce_chief_revisions(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeFinalReviewContext:
        review = DeclarativeFinalReviewContext.model_validate(values["review"])
        self._set_current(review)
        outcomes = {
            chapter_id: DeclarativeFinalChiefRevisionOutcome.model_validate(outcome)
            for chapter_id, outcome in dict(values["outcomes"]).items()
        }
        self._raise_failures(outcomes)
        active = tuple(review.pending_by_chapter)
        parts_by_chapter = {chapter_id: outcomes[chapter_id].parts for chapter_id in active}
        updates = {
            section_id: body
            for chapter_parts in parts_by_chapter.values()
            for section_id, body in chapter_parts.items()
        }
        field_for_section = {
            "1.1": "assessment_background",
            "1.2": "findings_overview",
            "1.3": "regional_executive_summary",
            "3.1.1": "risk_panorama",
            "3.1.2": "dimension_risk_analysis",
            "3.1.3": "data_gap_analysis",
            "3.2": "improvement_action_plan",
        }
        current = review.current.model_copy(
            update={
                **{
                    field_for_section[section_id]: body
                    for section_id, body in updates.items()
                    if section_id in field_for_section
                },
                **(
                    {
                        "special_topic_analysis": self._current_runner._render_special_topic_analysis(
                            parts_by_chapter["4"],
                            review.state.get("special_topic_plan"),
                        )
                    }
                    if "4" in parts_by_chapter
                    else {}
                ),
            }
        )
        run_id = str(review.state["run_id"])
        revision = review.revision_number
        subject_ref = f"Work/runs/{run_id}/edited-revisions/chief-r{revision}.json"
        self._current_runner.service.store.write_json(
            subject_ref,
            current.model_dump(mode="json"),
        )
        state = deepcopy(review.state)
        _restore_state(state)
        state["edited_report"] = current
        state["chief_candidate_ref"] = subject_ref
        if revision >= 2:
            aggregate_ref = f"Work/runs/{run_id}/reviews/chief-revision-r{revision}-aggregate.json"
            self._current_runner.service.store.write_json(
                aggregate_ref,
                {
                    "run_id": run_id,
                    "stage": f"chief-revision-r{revision}",
                    "status": "completed",
                    "lane_ids": list(active),
                    "result_ref": subject_ref,
                },
            )
        self._current_runner._record_recovery_aggregate(
            state,
            stage=f"chief-revision-r{revision}",
            lane_ids=list(active),
            result_ref=subject_ref,
            revision=revision,
        )
        self.current_state = state
        return review.model_copy(
            update={
                "state": state,
                "current": current,
                "subject_ref": subject_ref,
                "revision_responses": {
                    chapter_id: list(
                        cast(
                            ChiefChapterLaneRevisionSubmission,
                            outcomes[chapter_id].submission,
                        ).revision_responses
                    )
                    for chapter_id in active
                },
            }
        )

    def prepare_recheck(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeFinalRecheckContext:
        review = DeclarativeFinalReviewContext.model_validate(values["review"])
        chapter_id = cast(Literal["1", "3", "4"], str(values["chapter_id"]))
        self._set_current(review)
        findings = review.pending_by_chapter.get(chapter_id, [])
        if not self._production or not findings:
            return DeclarativeFinalRecheckContext(
                chapter_id=chapter_id,
                status="skipped",
            )
        state = review.state
        run_id = str(state["run_id"])
        revision = review.revision_number
        section_ids = self._chapter_sections(state, chapter_id)
        current_bodies = self._current_runner._final_chapter_section_bodies(
            review.current,
            chapter_id,
        )
        changed_bodies, unchanged_digests = self._current_runner._final_recheck_section_projection(
            current_bodies,
            findings,
        )
        contract = FinalChapterLaneInput(
            phase="recheck",
            run_id=run_id,
            subject_ref=review.subject_ref,
            chapter_id=chapter_id,
            review_focus=list(final_lane_specialization(chapter_id).review_focus),
            section_ids=list(section_ids),
            section_bodies=changed_bodies,
            unchanged_section_sha256=unchanged_digests,
            required_findings=findings,
            revision_responses=review.revision_responses[chapter_id],
            special_topic_plan=state.get("special_topic_plan"),
            revision=revision,
        )
        input_ref = f"Work/runs/{run_id}/context/final-chapter-{chapter_id}-input-r{revision}.json"
        recovered = self._recover_recheck(review, chapter_id, contract, input_ref)
        if recovered is not None:
            submission, output_ref = recovered
            return DeclarativeFinalRecheckContext(
                chapter_id=chapter_id,
                status="resumed",
                contract=contract,
                input_ref=input_ref,
                submission=submission,
                output_ref=output_ref,
            )
        self._current_runner.service.store.write_json(
            input_ref,
            contract.model_dump(mode="json"),
        )
        envelope = TaskEnvelope(
            task_id=f"final-chapter-{chapter_id}-r{revision}",
            run_id=run_id,
            agent_id="chief-editor-auditor",
            objective=f"只复核 Chapter {chapter_id} 的 assigned findings 并提交 verdicts。",
            input_refs=[input_ref],
            constraints=[
                "只提交 final_chapter_lane_verdict_submission",
                "verdicts 必须覆盖该章全部 required_findings，new_findings 只能留在该章",
            ],
            allowed_outputs=["final_chapter_lane_verdict_submission"],
            allowed_tools=["submit_result"],
            revision=revision,
            prior_result_ref=review.subject_ref,
            input_contract_kind="final_chapter_lane_input",
            input_contract_ref=input_ref,
            artifact_delivery_modes={input_ref: "inline"},
            inline_context=self._current_runner._final_template_skill_context(
                state,
                chapter_id,
            ),
        )
        return DeclarativeFinalRecheckContext(
            chapter_id=chapter_id,
            status="ready",
            contract=contract,
            input_ref=input_ref,
            envelope=envelope,
        )

    @staticmethod
    def recheck_requires_agent(context: DeclarativeFinalRecheckContext) -> bool:
        return context.status == "ready"

    def accept_recheck(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeFinalRecheckContext:
        context = DeclarativeFinalRecheckContext.model_validate(values["context"])
        result = DeclarativeFinalRecheckAgentResult.model_validate(values["result"])
        if result.status == "failed":
            self._record_failure("final-recheck", context.chapter_id, result.error)
            return context.model_copy(update={"status": "failed", "error": result.error})
        submission = cast(FinalChapterLaneVerdictSubmission, result.submission)
        contract = cast(FinalChapterLaneInput, context.contract)
        try:
            expected_ids = {finding.id for finding in contract.required_findings}
            actual_ids = {verdict.finding_id for verdict in submission.verdicts}
            if (
                submission.run_id != contract.run_id
                or submission.chapter_id != context.chapter_id
                or set(submission.checked_section_ids) != set(contract.section_ids)
                or actual_ids != expected_ids
            ):
                raise ValueError(
                    f"final chapter {context.chapter_id} verdict does not close its lane findings"
                )
            output_ref = (
                f"Work/runs/{contract.run_id}/reviews/final-chapter-lane-"
                f"{context.chapter_id}-r{contract.revision}.json"
            )
            self._current_runner.service.store.write_json(
                output_ref,
                submission.model_dump(mode="json"),
            )
            self._current_runner._record_recovery_lane(
                self.current_state,
                stage=f"final-recheck-r{contract.revision}",
                lane_id=context.chapter_id,
                status="completed",
                result_ref=output_ref,
                revision=contract.revision,
            )
        except BaseException as exc:
            self._record_failure("final-recheck", context.chapter_id, str(exc))
            return context.model_copy(update={"status": "failed", "error": str(exc)})
        return context.model_copy(
            update={
                "status": "accepted",
                "submission": submission,
                "output_ref": output_ref,
            }
        )

    @staticmethod
    def complete_recheck(
        context: DeclarativeFinalRecheckContext,
    ) -> DeclarativeFinalRecheckOutcome:
        context = DeclarativeFinalRecheckContext.model_validate(context)
        if context.status == "failed":
            return DeclarativeFinalRecheckOutcome(
                chapter_id=context.chapter_id,
                status="failed",
                error=context.error,
            )
        if context.status == "skipped":
            return DeclarativeFinalRecheckOutcome(
                chapter_id=context.chapter_id,
                status="skipped",
            )
        return DeclarativeFinalRecheckOutcome(
            chapter_id=context.chapter_id,
            status="completed",
            submission=context.submission,
            output_ref=context.output_ref,
        )

    def reduce_rechecks(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeFinalReviewContext:
        review = DeclarativeFinalReviewContext.model_validate(values["review"])
        self._set_current(review)
        outcomes = {
            chapter_id: DeclarativeFinalRecheckOutcome.model_validate(outcome)
            for chapter_id, outcome in dict(values["outcomes"]).items()
        }
        self._raise_failures(outcomes)
        active = tuple(review.pending_by_chapter)
        next_pending: dict[str, list[ChapterScopedFinalReviewFinding]] = {}
        history = list(review.verdict_history)
        latest_refs = dict(review.latest_verdict_refs)
        for chapter_id in active:
            outcome = outcomes[chapter_id]
            payload = cast(FinalChapterLaneVerdictSubmission, outcome.submission)
            output_ref = cast(str, outcome.output_ref)
            history.append(
                DeclarativeFinalVerdictRecord(
                    submission=payload,
                    output_ref=output_ref,
                )
            )
            latest_refs[chapter_id] = output_ref
            verdict_by_id = {verdict.finding_id: verdict for verdict in payload.verdicts}
            pending = [
                finding
                for finding in review.pending_by_chapter[chapter_id]
                if verdict_by_id[finding.id].verdict != "resolved"
            ]
            pending.extend(payload.new_findings)
            if pending:
                next_pending[chapter_id] = pending
        run_id = str(review.state["run_id"])
        revision = review.revision_number
        aggregate_ref = f"Work/runs/{run_id}/reviews/final-recheck-r{revision}-aggregate.json"
        self._current_runner.service.store.write_json(
            aggregate_ref,
            {
                "run_id": run_id,
                "stage": f"final-recheck-r{revision}",
                "status": "completed",
                "lane_ids": list(active),
                "result_ref": review.subject_ref,
            },
        )
        self._current_runner._record_recovery_aggregate(
            review.state,
            stage=f"final-recheck-r{revision}",
            lane_ids=list(active),
            result_ref=aggregate_ref,
            revision=revision,
        )
        return review.model_copy(
            update={
                "pending_by_chapter": next_pending,
                "verdict_history": history,
                "latest_verdict_refs": latest_refs,
            }
        )

    def complete_review(
        self,
        review: DeclarativeFinalReviewContext,
    ) -> dict[str, Any]:
        review = DeclarativeFinalReviewContext.model_validate(review)
        state = deepcopy(review.state)
        _restore_state(state)
        self.current_state = state
        if review.already_completed or not self._production:
            return state
        run_id = str(state["run_id"])
        revision = review.revision_number
        state["edited_report"] = review.current
        state["chief_candidate_ref"] = review.subject_ref
        state["final_review_restart_round"] = revision or 1
        completion = ReviewCompletionRecord(
            lifecycle="final",
            run_id=run_id,
            reviewer_agent_id="chief-editor-auditor",
            reviewer_session_key="final-chapter-wave",
            subject_refs=[review.subject_ref],
            finding_refs=list(review.initial_lane_refs.values()),
            verdict_refs=[record.output_ref for record in review.verdict_history],
            resolved_finding_ids=sorted(
                {
                    finding.id
                    for findings in review.findings_by_chapter.values()
                    for finding in findings
                }
                | {
                    finding.id
                    for record in review.verdict_history
                    for finding in record.submission.new_findings
                }
            ),
        )
        completion_ref = f"Work/runs/{run_id}/reviews/final-completion.json"
        self._current_runner.service.store.write_json(
            completion_ref,
            completion.model_dump(mode="json"),
        )
        claims = [
            claim
            for module_id in REPORT_MODULE_IDS
            for claim in getattr(
                state.get("module_submissions", {}).get(module_id),
                "claims",
                [],
            )
        ]
        canonical_ref = f"Work/runs/{run_id}/validation/report-chief-candidate-r{revision}.md"
        _, canonical = self._current_runner._delivery_projection(
            state,
            review.current,
            claims,
        )
        self._current_runner._validate_final_report_structure(
            state,
            canonical,
            f"chief-candidate-r{revision}",
        )
        validation_ref = (
            f"Work/runs/{run_id}/reviews/report-integrity-chief-candidate-r{revision}.json"
        )
        snapshot_ref = f"Work/runs/{run_id}/reviews/final-audit-snapshot.json"
        snapshot = FinalAuditSnapshot(
            run_id=run_id,
            subject_ref=review.subject_ref,
            subject_revision=revision,
            canonical_markdown_ref=canonical_ref,
            validation_report_ref=validation_ref,
            completion_ref=completion_ref,
        )
        self._current_runner.service.store.write_json(
            snapshot_ref,
            snapshot.model_dump(mode="json"),
        )
        state["final_review_completion_ref"] = completion_ref
        state["final_audit_snapshot_ref"] = snapshot_ref
        state["final_residual_risks"] = list(review.initial_residual_risks)
        state["final_chapter_lane_refs"] = dict(review.initial_lane_refs)
        state["aggregate_refs"] = {
            **dict(state.get("aggregate_refs", {})),
            "final": completion_ref,
        }
        for chapter_id in self._current_runner._chapter_lane_ids(state):
            terminal_ref = review.latest_verdict_refs.get(
                chapter_id,
                review.initial_lane_refs.get(chapter_id),
            )
            if terminal_ref:
                self._current_runner._record_recovery_lane(
                    state,
                    stage="final",
                    lane_id=chapter_id,
                    status="completed",
                    result_ref=terminal_ref,
                    revision=revision,
                )
        self._current_runner._record_recovery_aggregate(
            state,
            stage="final",
            lane_ids=list(self._current_runner._chapter_lane_ids(state)),
            result_ref=completion_ref,
            revision=revision,
        )
        self.current_state = state
        return state

    def _set_current(self, review: DeclarativeFinalReviewContext) -> None:
        state = deepcopy(review.state)
        _restore_state(state)
        self.current_state = state
        self._active_revision = review.revision_number

    def _recover_chief_revision(
        self,
        review: DeclarativeFinalReviewContext,
        chapter_id: str,
    ) -> tuple[ChiefChapterLaneRevisionSubmission, str, dict[str, str]] | None:
        if review.revision_number != 1:
            return None
        recovered = self._current_runner._recovery_store(review.state).load_completed_lanes(
            "chief-revision-r1", [chapter_id]
        )
        lane = recovered.get(chapter_id)
        result_ref = getattr(lane, "result_ref", None)
        expected_ref = (
            f"Work/runs/{review.state['run_id']}/reviews/chief-chapter-lane-{chapter_id}-r1.json"
        )
        if result_ref != expected_ref:
            return None
        try:
            submission = ChiefChapterLaneRevisionSubmission.model_validate_json(
                (self._current_runner.service.workspace / result_ref).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        findings = review.pending_by_chapter[chapter_id]
        if (
            submission.run_id != review.state["run_id"]
            or submission.base_subject_ref != review.subject_ref
            or submission.chapter_id != chapter_id
            or submission.revision != 1
            or {response.finding_id for response in submission.revision_responses}
            != {finding.id for finding in findings}
        ):
            return None
        parts = self._current_runner._read_chief_chapter_parts(
            review.state,
            chapter_id,
            submission,
            f"chief-chapter-{chapter_id}-r1",
        )
        return submission, result_ref, parts

    def _recover_recheck(
        self,
        review: DeclarativeFinalReviewContext,
        chapter_id: str,
        contract: FinalChapterLaneInput,
        input_ref: str,
    ) -> tuple[FinalChapterLaneVerdictSubmission, str] | None:
        if review.revision_number != 1:
            return None
        recovered = self._current_runner._recovery_store(review.state).load_completed_lanes(
            "final-recheck-r1", [chapter_id]
        )
        lane = recovered.get(chapter_id)
        result_ref = getattr(lane, "result_ref", None)
        expected_ref = (
            f"Work/runs/{review.state['run_id']}/reviews/final-chapter-lane-{chapter_id}-r1.json"
        )
        if result_ref != expected_ref:
            return None
        try:
            submission = FinalChapterLaneVerdictSubmission.model_validate_json(
                (self._current_runner.service.workspace / result_ref).read_text(encoding="utf-8")
            )
            persisted = FinalChapterLaneInput.model_validate_json(
                (self._current_runner.service.workspace / input_ref).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        if (
            submission.run_id != review.state["run_id"]
            or submission.chapter_id != chapter_id
            or set(submission.checked_section_ids) != set(contract.section_ids)
            or {verdict.finding_id for verdict in submission.verdicts}
            != {finding.id for finding in contract.required_findings}
            or persisted.model_dump(mode="json") != contract.model_dump(mode="json")
        ):
            return None
        return submission, result_ref

    def _record_failure(
        self,
        stage_prefix: str,
        chapter_id: str,
        error: str | None,
    ) -> None:
        revision = self._current_revision()
        self._current_runner._record_recovery_lane(
            self.current_state,
            stage=f"{stage_prefix}-r{revision}",
            lane_id=chapter_id,
            status="failed",
            error=error or f"{stage_prefix} lane failed",
            revision=revision,
        )

    def _current_revision(self) -> int:
        return self._active_revision

    @staticmethod
    def _chapter_sections(
        state: Mapping[str, Any],
        chapter_id: str,
    ) -> tuple[str, ...]:
        if chapter_id == "1":
            return tuple(CHAPTER1_SECTION_IDS)
        if chapter_id == "3":
            return tuple(CHAPTER3_SECTION_IDS)
        return chapter_section_ids("4", state.get("special_topic_plan"))

    @staticmethod
    def _raise_failures(outcomes: Mapping[str, Any]) -> None:
        failures = {
            chapter_id: outcome.error or "Final round lane failed"
            for chapter_id, outcome in outcomes.items()
            if outcome.status == "failed"
        }
        if failures:
            first = min(failures, key=int)
            raise RuntimeError(failures[first])


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
    plan = state.get("special_topic_plan")
    if isinstance(plan, Mapping):
        state["special_topic_plan"] = SpecialTopicPlan.model_validate(plan)


__all__ = [
    "DeclarativeFinalChiefRevisionAgentResult",
    "DeclarativeFinalChiefRevisionContext",
    "DeclarativeFinalChiefRevisionOutcome",
    "DeclarativeFinalRecheckAgentResult",
    "DeclarativeFinalRecheckContext",
    "DeclarativeFinalRecheckOutcome",
    "DeclarativeFinalReviewContext",
    "DeclarativeFinalReviewRuntime",
    "DeclarativeFinalVerdictRecord",
    "compile_final_review_workflows",
    "compose_final_review_agent_invokers",
    "register_final_review_lane_specializations",
]
