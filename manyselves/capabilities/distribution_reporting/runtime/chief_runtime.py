"""Capability-owned initial Chief chapter lane runtime.

This module owns the file-defined Chief lane's preparation, typed initial
Agent turn, acceptance boundary, and pure initial-candidate reducer.  It
deliberately stops before the revision/recheck lifecycle and delivery.  Those
phases remain separate Capability composition points.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal, cast

from manyselves.capabilities.distribution_reporting.domain.photo_bindings import (
    runtime_photo_ids,
)
from manyselves.capabilities.distribution_reporting.runtime.final_review_tools import (
    _chief_template_skill_context,
    _render_special_topic_analysis,
    _split_special_topic_analysis,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CHIEF_SECTION_RESULT_PART_IDS,
    ChiefChapterLaneSubmission,
    EditedReportSubmission,
    ModuleSubmission,
    TaskEnvelope,
    numbered_markdown_headings,
)
from manyselves.capabilities.distribution_reporting.runtime.models.chief_chapter import (
    DeclarativeChiefChapterAgentResult,
    DeclarativeChiefChapterContext,
    DeclarativeChiefChapterOutcome,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ChiefChapterLaneInput,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    CHAPTER1_SECTION_IDS,
    CHAPTER3_SECTION_IDS,
    REPORT_MODULE_IDS,
    SpecialTopicPlan,
    chapter_section_ids,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.kernel.conversations import ConversationRecord
from manyselves.kernel.definitions import (
    AgentDefinition,
    RecoveryPolicyDefinition,
    TaskDefinition,
)
from manyselves.kernel.ports import AgentInvocationOutcome
from manyselves.runtime.agent_execution import (
    AgentExecutionService,
    AgentSessionLoop,
    AgentTurnRequest,
)
from manyselves.runtime.typed_agent_turn import TypedAgentTurn

SessionFactory = Callable[[str], AgentSessionLoop]
ChapterId = Literal["1", "3", "4"]


def _model(value: Any, model_type: type[Any]) -> Any:
    if isinstance(value, model_type):
        return value
    return model_type.model_validate(value)


def _state_from(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        for key in ("state", "reporting_state"):
            nested = value.get(key)
            if isinstance(nested, Mapping):
                return deepcopy(dict(nested))
        return deepcopy(dict(value))
    raise TypeError("Chief chapter runtime requires a state object")


def _restore_modules(state: dict[str, Any]) -> None:
    modules = state.get("module_submissions")
    if not isinstance(modules, Mapping):
        return
    state["module_submissions"] = {
        str(module_id): _model(module, ModuleSubmission)
        for module_id, module in modules.items()
    }


def _active_chapters(state: Mapping[str, Any]) -> tuple[ChapterId, ...]:
    return ("1", "3", "4") if state.get("special_topic_plan") is not None else ("1", "3")


def _chapter_sections(
    chapter_id: ChapterId,
    special_topic_plan: SpecialTopicPlan | None,
) -> tuple[str, ...]:
    if chapter_id == "1":
        return tuple(CHAPTER1_SECTION_IDS)
    if chapter_id == "3":
        return tuple(CHAPTER3_SECTION_IDS)
    return chapter_section_ids("4", special_topic_plan)


def _source_projection(
    state: Mapping[str, Any],
    chapter_id: ChapterId,
) -> tuple[dict[str, str], list[str]]:
    """Project only the source context assigned to one Chief chapter lane."""

    cross_ref = state.get("cross_review_completion_ref")
    source_refs = [str(cross_ref)] if cross_ref else []
    raw_modules = state.get("module_submissions", {})
    modules = {
        str(module_id): _model(module, ModuleSubmission)
        for module_id, module in raw_modules.items()
    } if isinstance(raw_modules, Mapping) else {}

    if chapter_id == "1":
        source_context = {
            f"module-{module_id}": json.dumps(
                {
                    "module_id": module_id,
                    "revision": module.revision,
                    "submodule_ids": sorted(module.submodule_narratives),
                    "unresolved_questions": list(module.unresolved_questions),
                    "claim_ids": [claim.id for claim in module.claims],
                },
                ensure_ascii=False,
                sort_keys=True,
            )[:2400]
            for module_id, module in sorted(
                modules.items(), key=lambda item: float(item[0])
            )
        }
        source_context["approved_markers"] = ",".join(
            f"[[APPROVED_MODULE:{module_id}]]" for module_id in REPORT_MODULE_IDS
        )
    elif chapter_id == "3":
        source_context = {
            "cross_synthesis": json.dumps(
                [
                    item.model_dump(mode="json")
                    if hasattr(item, "model_dump")
                    else item
                    for item in state.get("cross_synthesis_inputs", [])
                ],
                ensure_ascii=False,
                sort_keys=True,
            )[:6000],
            "module_boundaries": ",".join(
                f"{module_id}:{','.join(sorted(module.submodule_narratives))}"
                for module_id, module in sorted(
                    modules.items(), key=lambda item: float(item[0])
                )
            ),
        }
    else:
        plan = state.get("special_topic_plan")
        if plan is not None and not isinstance(plan, SpecialTopicPlan):
            plan = SpecialTopicPlan.model_validate(plan)
        source_context = {
            "special_topic_plan": json.dumps(
                plan.model_dump(mode="json") if hasattr(plan, "model_dump") else plan,
                ensure_ascii=False,
                sort_keys=True,
            )[:12_000],
        }
        knowledge_ref = state.get("special_topic_knowledge_ref")
        if knowledge_ref:
            source_refs.append(str(knowledge_ref))

    evidence_ref = state.get("preparation_refs", {}).get("evidence")
    if evidence_ref:
        source_refs.append(str(evidence_ref))
    source_refs = list(dict.fromkeys(ref for ref in source_refs if ref))
    if not source_context and not source_refs:
        source_context = {"scope": f"chapter-{chapter_id}"}
    return source_context, source_refs


def _build_envelope(
    contract: ChiefChapterLaneInput,
    *,
    input_ref: str,
    inline_context: str,
) -> TaskEnvelope:
    chapter_id = contract.chapter_id
    return TaskEnvelope(
        task_id=f"chief-chapter-{chapter_id}",
        run_id=contract.run_id,
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
        inline_context=inline_context,
    )


class ChiefChapterAgentInvoker:
    """Invoke one typed Chief initial turn through the neutral Agent runtime."""

    def __init__(
        self,
        workspace: Path,
        *,
        execution: AgentExecutionService,
        session_factory: SessionFactory,
        workflow_id: str = "public-reporting",
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.execution = execution
        self.session_factory = session_factory
        self.workflow_id = workflow_id

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        return await self._invoke_once(agent, task, value, conversation, task_id=task_id)

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
        """Retain the declared port while this slice is initial-only."""

        del recovery_policy
        return await self._invoke_once(agent, task, value, conversation, task_id=task_id)

    async def _invoke_once(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        context = _model(value, DeclarativeChiefChapterContext)
        if context.contract is None or context.envelope is None:
            return AgentInvocationOutcome(
                status="failed",
                session_id=conversation.external_session_id,
                error="Chief chapter initial preparation has no contract or TaskEnvelope",
            )
        workflow_id = self.workflow_id
        run_id = context.contract.run_id
        runtime_id = f"{workflow_id}:{agent.id}:{conversation.key.value}"
        session_id = conversation.external_session_id or (
            f"{workflow_id}:{conversation.key.value}"
        )
        typed_turn = TypedAgentTurn(
            execution=self.execution,
            workflow_id=workflow_id,
            conversation_key=conversation.key.value,
            runtime_id=runtime_id,
            session_id=session_id,
            session_factory=lambda: self.session_factory(runtime_id),
        )
        try:
            session = await typed_turn.start_or_restore()
        except Exception as exc:
            return AgentInvocationOutcome(
                status="failed",
                session_id=conversation.external_session_id,
                error=str(exc),
            )

        conversation.external_session_id = session.session_id
        begin_typed_task = getattr(session.loop, "begin_typed_task", None)
        if callable(begin_typed_task):
            begin_typed_task(
                {
                    "task_id": task.id,
                    "run_id": run_id,
                    "input_contract": task.input_contract,
                    "output_contract": task.output_contract,
                }
            )
        request = AgentTurnRequest(
            content=self._prompt(agent, task, context.contract, context.envelope),
            message_id=f"{task_id}:{run_id}:chapter-{context.chapter_id}:initial",
            workflow_id=workflow_id,
            run_id=run_id,
            task_id=task_id,
            task_attempt_id=task_id,
            turn_kind="task_initial",
        )
        terminal = typed_turn.result_terminal(
            run_id=run_id,
            task_id=task_id,
            task_attempt_id=task_id,
            session_id=session.session_id,
        )
        outcome = await typed_turn.dispatch(session, request, terminals=(terminal,))
        return TypedAgentTurn.map_outcome(
            outcome,
            session_id=session.session_id,
            decode_result=self._decode_result,
        )

    def _prompt(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        contract: ChiefChapterLaneInput,
        envelope: TaskEnvelope,
    ) -> str:
        return "\n\n".join(
            (
                agent.instructions,
                f"Task: {task.objective}",
                json.dumps(contract.model_dump(mode="json"), ensure_ascii=False, indent=2),
                f"Allowed tools: {json.dumps(task.tools, ensure_ascii=False)}",
                f"Output contract: {task.output_contract}",
                *( [f"Inline context:\n{envelope.inline_context}"]
                   if envelope.inline_context else [] ),
            )
        )

    def _decode_result(self, result_ref: str) -> dict[str, Any]:
        path = Path(result_ref)
        path = path if path.is_absolute() else self.workspace / path
        submission = ChiefChapterLaneSubmission.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        return DeclarativeChiefChapterAgentResult(
            status="completed",
            submission=submission,
        ).model_dump(mode="json")


class ChiefChapterRuntime:
    """Compose the Capability-owned initial Chief chapter lifecycle."""

    def __init__(
        self,
        workspace: Path,
        *,
        state: Mapping[str, Any] | None = None,
        store: ReportingStore | None = None,
        agent_execution: AgentExecutionService | None = None,
        agent_session_factory: SessionFactory | None = None,
        workflow_id: str = "public-reporting",
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = store or ReportingStore(self.workspace)
        self.current_state = _state_from(state or {})
        _restore_modules(self.current_state)
        self.workflow_id = workflow_id
        self.agent_execution = agent_execution
        self.agent_session_factory = agent_session_factory
        if agent_execution is not None and callable(agent_session_factory):
            self.agent_invokers: Mapping[str, Any] = {
                "chief-editor": ChiefChapterAgentInvoker(
                    self.workspace,
                    execution=agent_execution,
                    session_factory=agent_session_factory,
                    workflow_id=workflow_id,
                )
            }
        else:
            self.agent_invokers = {}

    def prepare(self, value: Any) -> dict[str, Any]:
        state = _state_from(value)
        _restore_modules(state)
        self.current_state = state
        return deepcopy(state)

    def prepare_lane(self, value: Any) -> DeclarativeChiefChapterContext:
        raw = value if isinstance(value, Mapping) else {}
        state = _state_from(raw.get("state", raw))
        _restore_modules(state)
        self.current_state = state
        chapter_id = cast(ChapterId, str(raw.get("chapter_id", "")))
        if chapter_id not in ("1", "3", "4"):
            raise ValueError(f"unsupported Chief chapter lane: {chapter_id}")
        if "chief_candidate_ref" in state:
            return DeclarativeChiefChapterContext(
                chapter_id=chapter_id,
                status="resumed",
            )
        if chapter_id not in _active_chapters(state):
            return DeclarativeChiefChapterContext(
                chapter_id=chapter_id,
                status="skipped",
            )
        plan = state.get("special_topic_plan")
        if plan is not None and not isinstance(plan, SpecialTopicPlan):
            plan = SpecialTopicPlan.model_validate(plan)
        run_id = str(state["run_id"])
        section_ids = _chapter_sections(chapter_id, plan)
        baseline_ref = str(
            state.get("cross_review_completion_ref")
            or f"Work/runs/{run_id}/reviews/cross-completion.json"
        )
        source_context, source_refs = _source_projection(state, chapter_id)
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
        self.store.write_json(input_ref, contract.model_dump(mode="json"))
        envelope = _build_envelope(
            contract,
            input_ref=input_ref,
            inline_context=_chief_template_skill_context(
                state,
                self.workspace,
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
    def requires_agent(value: Any) -> bool:
        return _model(value, DeclarativeChiefChapterContext).status == "ready"

    def accept_lane(self, value: Any) -> DeclarativeChiefChapterContext:
        if not isinstance(value, Mapping):
            raise TypeError("Chief chapter acceptance requires context and result")
        context = _model(value.get("context"), DeclarativeChiefChapterContext)
        result = _model(value.get("result"), DeclarativeChiefChapterAgentResult)
        if result.status == "failed" or result.submission is None:
            return context.model_copy(
                update={
                    "status": "failed",
                    "error": result.error or "Chief chapter Agent failed",
                }
            )
        contract = context.contract
        if contract is None:
            raise ValueError("Chief chapter acceptance has no contract")
        submission = result.submission
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
        self.store.write_json(output_ref, submission.model_dump(mode="json"))
        return context.model_copy(
            update={
                "status": "accepted",
                "submission": submission,
                "output_ref": output_ref,
                "error": None,
            }
        )

    def _read_chief_chapter_parts(
        self,
        state: Mapping[str, Any],
        chapter_id: ChapterId,
        submission: ChiefChapterLaneSubmission,
        task_id: str,
    ) -> dict[str, str]:
        """Materialize the submitted parts inside this lane's existing task scope."""

        run_root = (self.workspace / f"Work/runs/{state['run_id']}").resolve()
        task_root = (run_root / "drafts" / task_id / f"r{submission.revision}").resolve()
        if not task_root.is_relative_to(run_root):
            raise ValueError("Chief chapter task root escapes the current run")
        part_to_sections: dict[str, list[str]] = {}
        if chapter_id == "4":
            part_to_sections["special_topic_analysis"] = list(submission.section_ids)
        else:
            for section_id in submission.section_ids:
                part_to_sections.setdefault(
                    CHIEF_SECTION_RESULT_PART_IDS[section_id], []
                ).append(section_id)
        bodies: dict[str, str] = {}
        for part_id, ref in submission.part_refs.items():
            path = (self.workspace / ref).resolve()
            if not path.is_relative_to(task_root) or path.suffix != ".md" or not path.is_file():
                raise ValueError(
                    f"Chief chapter {chapter_id} returned a part outside its lane task: {ref}"
                )
            content = path.read_text(encoding="utf-8")
            if not content.strip():
                raise ValueError(f"Chief chapter {chapter_id} returned a blank part: {part_id}")
            if chapter_id == "4" and part_id == "special_topic_analysis":
                plan = state.get("special_topic_plan")
                if not isinstance(plan, SpecialTopicPlan):
                    plan = SpecialTopicPlan.model_validate(plan)
                bodies.update(
                    _split_special_topic_analysis(
                        content,
                        plan,
                        allow_single_body=len(submission.section_ids) == 1,
                    )
                )
            else:
                headings = numbered_markdown_headings(content)
                if headings:
                    raise ValueError(
                        f"Chief chapter {chapter_id} part {part_id} must contain section body only"
                    )
                for section_id in part_to_sections.get(part_id, []):
                    bodies[section_id] = content
        if set(bodies) != set(submission.section_ids):
            raise ValueError(
                f"Chief chapter {chapter_id} did not return every assigned section body"
            )
        return bodies

    def reduce(self, value: Any) -> dict[str, Any]:
        """Assemble the initial Chief candidate from accepted lane outcomes."""

        if not isinstance(value, Mapping):
            raise TypeError("Chief chapter reduction requires state and outcomes")
        state = _state_from(value.get("state", value))
        _restore_modules(state)
        if "chief_candidate_ref" in state:
            self.current_state = state
            return deepcopy(state)
        raw_outcomes = value.get("outcomes")
        if not isinstance(raw_outcomes, Mapping):
            raise ValueError("Chief chapter reduction requires lane outcomes")
        outcomes = {
            str(chapter_id): _model(outcome, DeclarativeChiefChapterOutcome)
            for chapter_id, outcome in raw_outcomes.items()
        }
        active_chapters = _active_chapters(state)
        failures = {
            chapter_id: outcome.error or "Chief chapter lane failed"
            for chapter_id, outcome in outcomes.items()
            if outcome.status == "failed"
        }
        if failures:
            first = min(failures, key=lambda item: (item not in active_chapters, item))
            raise RuntimeError(failures[first])
        section_bodies: dict[str, str] = {}
        lane_refs: dict[str, str] = {}
        for chapter_id in active_chapters:
            outcome = outcomes.get(chapter_id)
            if outcome is None or outcome.status != "completed" or outcome.submission is None:
                raise ValueError(f"Chief chapter lane {chapter_id} did not complete")
            section_bodies.update(
                self._read_chief_chapter_parts(
                    state,
                    chapter_id,
                    outcome.submission,
                    f"chief-chapter-{chapter_id}",
                )
            )
            lane_refs[chapter_id] = outcome.output_ref or ""
        plan = state.get("special_topic_plan")
        if plan is not None and not isinstance(plan, SpecialTopicPlan):
            plan = SpecialTopicPlan.model_validate(plan)
        approved_module_text = {
            module_id: _model(state["module_submissions"][module_id], ModuleSubmission).markdown
            for module_id in REPORT_MODULE_IDS
        }
        claims = [
            claim
            for module_id in REPORT_MODULE_IDS
            for claim in _model(
                state["module_submissions"][module_id], ModuleSubmission
            ).claims
        ]
        special_topic_body = _render_special_topic_analysis(
            {
                section_id: section_bodies[section_id]
                for section_id in chapter_section_ids("4", plan)
            }
            if plan is not None
            else {},
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
            photo_ids=runtime_photo_ids(
                state.get("evidence_items", []), state.get("photo_assets", [])
            ),
            unresolved_editorial_issues=[],
            revision_responses=[],
        )
        run_id = str(state["run_id"])
        candidate_ref = f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
        self.store.write_json(candidate_ref, edited.model_dump(mode="json"))
        state.update(
            {
                "edited_report": edited,
                "approved_module_text": approved_module_text,
                "chief_candidate_ref": candidate_ref,
                "chief_chapter_lane_refs": lane_refs,
                "chief_editor_session_key": "chief-editor",
                "chief_editor_completion_ref": candidate_ref,
                "aggregate_refs": {
                    **dict(state.get("aggregate_refs", {})),
                    "chief": candidate_ref,
                },
            }
        )
        self.current_state = state
        return deepcopy(state)

    @staticmethod
    def complete_lane(
        value: DeclarativeChiefChapterContext,
    ) -> DeclarativeChiefChapterOutcome:
        context = _model(value, DeclarativeChiefChapterContext)
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


def build_chief_chapter_tool_implementations(
    runtime: ChiefChapterRuntime,
) -> dict[str, Any]:
    """Bind the declared Chief lifecycle Tool ids to one Capability runtime."""

    return {
        "prepare-chief-chapter-cohort": runtime.prepare,
        "prepare-current-chief-chapter": runtime.prepare_lane,
        "chief-chapter-requires-agent": runtime.requires_agent,
        "accept-current-chief-chapter": runtime.accept_lane,
        "complete-current-chief-chapter": runtime.complete_lane,
        "reduce-chief-chapter-cohort": runtime.reduce,
    }


CapabilityChiefChapterRuntime = ChiefChapterRuntime

__all__ = [
    "build_chief_chapter_tool_implementations",
    "CapabilityChiefChapterRuntime",
    "ChiefChapterAgentInvoker",
    "ChiefChapterRuntime",
]
