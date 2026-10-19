"""Capability-owned initial Cross-owner review runtime.

The Cross tail has five independent owner lanes.  This module owns the typed
boundary that prepares those lanes, invokes the shared Cross reviewer through
the neutral Agent runtime, accepts a typed initial submission, and reduces the
no-finding path into the current-run Cross completion and decision pack.

The runtime consumes module refs supplied by the upstream module cohort when
available.  For persisted same-run module submissions it can reconstruct the
existing current-run ref from the run id, module id, and revision.  Cross input
does not calculate or require digests.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from manyselves.capabilities.distribution_reporting.domain.cross_specialization import (
    cross_lane_specialization,
)
from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.agent_recovery_turn import (
    ProgressObserver,
    execute_reporting_recovery,
)
from manyselves.capabilities.distribution_reporting.runtime.agent_result_payload import (
    load_agent_result_payload,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_completion import (
    build_cross_owner_lane_completion,
    promote_cross_owner_pipeline_completion,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_local_regression import (
    build_cross_owner_local_regression_context,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_module_revision_input import (
    project_cross_owner_local_module_agent_input,
    project_cross_owner_module_revision_agent_input,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_recheck import (
    accept_cross_owner_recheck,
    prepare_cross_owner_recheck,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_round import (
    advance_cross_owner_round,
)
from manyselves.capabilities.distribution_reporting.runtime.main_exception import (
    accept_main_exception_decision,
    prepare_main_exception_decision,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CROSS_REVIEW_DIMENSIONS,
    AgentResult,
    CrossDecisionPack,
    CrossOwnerFindingSubmission,
    CrossOwnerVerdictSubmission,
    CrossReviewCoverageEntry,
    CrossReviewFindingSubmission,
    CrossReviewVerdictSubmission,
    CrossSynthesisInput,
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
    TaskEnvelope,
    WorkflowDecisionSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import (
    DeclarativeCrossOwnerInitialAgentResult,
    DeclarativeCrossOwnerPipelineOutcome,
    DeclarativeCrossOwnerRecheckAgentResult,
    DeclarativeCrossOwnerRuntimeContext,
    DeclarativeMainExceptionAgentResult,
    DeclarativeMainExceptionUserInput,
    cross_runtime_context,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    CrossOwnerInput,
    CrossOwnerRelatedModuleView,
    ModuleContentView,
    ReviewCompletionRecord,
    module_content_view,
    report_instruction_from_state,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleRecheckAgentResult,
    DeclarativeModuleReviewAgentResult,
    DeclarativeModuleReviewPreparation,
    DeclarativeModuleRevisionAgentResult,
    DeclarativeModuleRevisionPreparation,
    DeclarativeModuleRuntimeLaneContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    UserSupplement,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    CrossOwnerInitialReviewAcceptance,
    CrossOwnerInitialReviewPreparation,
    CrossOwnerLocalReviewAcceptance,
    CrossOwnerLocalReviewPreparation,
    CrossOwnerRevisionAcceptance,
    CrossOwnerRevisionPreparation,
    MainExceptionDecisionPreparation,
    ModuleLocalRegressionContext,
    ModuleRevisionPreparation,
    _CrossOwnerPipelineResult,
)
from manyselves.capabilities.distribution_reporting.runtime.module_lane_tools import (
    module_review_needs_revision,
    module_review_preflight_needs_revision,
    module_review_requires_agent,
)
from manyselves.capabilities.distribution_reporting.runtime.module_preflight_revision import (
    accept_current_module_preflight_revision,
    prepare_current_module_preflight_revision,
)
from manyselves.capabilities.distribution_reporting.runtime.module_recheck_tools import (
    accept_current_module_recheck,
    module_recheck_requires_agent,
    prepare_current_module_recheck,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_acceptance import (
    accept_current_module_review,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_preparation import (
    prepare_module_local_regression_review,
)
from manyselves.capabilities.distribution_reporting.runtime.module_revision_tools import (
    accept_current_module_revision,
    accept_module_revision,
    load_module_revision_candidate,
    prepare_module_revision,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.capabilities.distribution_reporting.runtime.user_supplements import (
    request_user_supplements,
)
from manyselves.config.schema import AgentDefaults
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
from manyselves.runtime.agent_recovery import AgentRecoveryDriver
from manyselves.runtime.context_budget import estimate_text_tokens, retained_input_budget_for
from manyselves.runtime.typed_agent_turn import TypedAgentTurn

from .task_context import bind_task_context

REPORT_MODULE_IDS = tuple(REPORT_TAXONOMY)
SessionFactory = Callable[[str], AgentSessionLoop]
CompletedResultLoader = Callable[
    [], AgentResult | None | Awaitable[AgentResult | None]
]


def _model(value: Any, model_type: type[Any]) -> Any:
    if isinstance(value, model_type):
        return value
    return model_type.model_validate(value)


def _state_from(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        if isinstance(value.get("state"), Mapping):
            return deepcopy(dict(value["state"]))
        if isinstance(value.get("reporting_state"), Mapping):
            return deepcopy(dict(value["reporting_state"]))
        return deepcopy(dict(value))
    raise TypeError("Cross owner runtime requires a state object")


def _state_user_supplements(state: Mapping[str, Any]) -> list[UserSupplement]:
    return [
        item
        if isinstance(item, UserSupplement)
        else UserSupplement.model_validate(item)
        for item in request_user_supplements(state.get("request"))
    ]


def _module_submissions(state: Mapping[str, Any]) -> dict[str, ModuleSubmission]:
    raw = state.get("module_submissions")
    if not isinstance(raw, Mapping):
        raw = state.get("specialist_submissions")
    modules = {
        str(module_id): _model(payload, ModuleSubmission)
        for module_id, payload in raw.items()
    }
    return modules


def _metadata_map(state: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in (
        "module_subject_refs",
        "module_artifact_refs",
        "module_artifacts",
        "module_refs",
    ):
        candidate = state.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    return {}


def _metadata_ref(metadata: Mapping[str, Any], module_id: str) -> str | None:
    entry = metadata.get(module_id, {})
    if isinstance(entry, Mapping):
        ref = entry.get("ref") or entry.get("subject_ref") or entry.get("path")
    else:
        ref = getattr(entry, "ref", None) or getattr(entry, "subject_ref", None)
        if ref is None and isinstance(entry, str):
            ref = entry
    return str(ref) if ref else None


def _module_subject_ref(
    state: Mapping[str, Any],
    metadata: Mapping[str, Any],
    module: ModuleSubmission,
) -> str:
    return _metadata_ref(metadata, module.module_id) or (
        f"Work/runs/{state['run_id']}/modules/"
        f"{module.module_id}-r{module.revision}.json"
    )


def _related_view(
    module: ModuleSubmission,
    *,
    subject_ref: str,
    evidence_ids: set[str] | None = None,
) -> CrossOwnerRelatedModuleView:
    """Compact related view; with ``evidence_ids`` only claims on those objects."""

    content = module_content_view(module)
    claims = module.claims
    by_submodule = content.evidence_ids_by_submodule
    if evidence_ids is not None:
        claims = [claim for claim in claims if evidence_ids & set(claim.source_ids)]
        by_submodule = {
            key: [item for item in values if item in evidence_ids]
            for key, values in by_submodule.items()
        }
    return CrossOwnerRelatedModuleView(
        module_id=module.module_id,
        revision=module.revision,
        subject_ref=subject_ref,
        submodule_ids=list(content.submodule_narratives),
        claims=claims,
        evidence_ids_by_submodule=by_submodule,
        unresolved_questions=content.unresolved_questions,
    )


def _owner_dossiers(state: Mapping[str, Any], workspace, owner_module_id: str) -> list[dict] | None:
    """Dossiers for this owner; None when the run has no joint dossiers (old path)."""

    ref = state.get("joint_dossiers_ref")
    if not ref:
        return None
    payload = json.loads((Path(workspace) / str(ref)).read_text(encoding="utf-8"))
    return [
        dossier for dossier in payload.get("dossiers", [])
        if owner_module_id in dossier.get("module_ids", [])
    ]


def _input_tokens(contract: CrossOwnerInput) -> int:
    """Estimated tokens of the inline owner input as the reviewer receives it."""

    return estimate_text_tokens(
        json.dumps(contract.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    )


def _dossier_batches(
    dossiers: list[dict],
    measure: Callable[[list[dict]], int],
    budget: int,
) -> list[list[dict]]:
    """Consecutive dossier groups whose owner input each fits ``budget``.

    Dossiers are independent objects, so splitting them never separates the
    facts and judgments of one object.  A single dossier above the budget is
    still sent alone; the Agent runtime then stops with its context-budget
    reason instead of sending a request the Provider would reject.
    """

    if len(dossiers) <= 1 or measure(dossiers) <= budget:
        return [dossiers]
    batches: list[list[dict]] = []
    current: list[dict] = []
    for dossier in dossiers:
        candidate = [*current, dossier]
        if current and measure(candidate) > budget:
            batches.append(current)
            current = [dossier]
        else:
            current = candidate
    batches.append(current)
    return batches


def _merge_owner_inputs(parts: list[CrossOwnerInput]) -> CrossOwnerInput:
    """One owner input covering several dossier batches of the same owner."""

    if len(parts) == 1:
        return parts[0]
    first = parts[0]
    narratives: dict[str, str] = {}
    assessments: dict[str, dict] = {}
    evidence_by_submodule: dict[str, list[str]] = {}
    dossiers: list[dict] = []
    related: dict[str, Any] = {}
    for part in parts:
        narratives.update(part.owner_subject.submodule_narratives)
        assessments.update(part.owner_subject.assessments)
        for key, values in part.owner_subject.evidence_ids_by_submodule.items():
            merged = evidence_by_submodule.setdefault(key, [])
            merged.extend(value for value in values if value not in merged)
        dossiers.extend(part.joint_dossiers)
        for module_id, view in part.related_module_views.items():
            current = related.get(module_id)
            if current is None:
                related[module_id] = view
                continue
            claims = {claim.id: claim for claim in current.claims}
            claims.update({claim.id: claim for claim in view.claims})
            by_submodule = {key: list(values) for key, values in current.evidence_ids_by_submodule.items()}
            for key, values in view.evidence_ids_by_submodule.items():
                merged = by_submodule.setdefault(key, [])
                merged.extend(value for value in values if value not in merged)
            related[module_id] = current.model_copy(update={
                "claims": list(claims.values()),
                "evidence_ids_by_submodule": by_submodule,
            })
    order = list(first.owner_subject.submodule_narratives)
    for part in parts[1:]:
        order.extend(key for key in part.owner_subject.submodule_narratives if key not in order)
    owner_subject = first.owner_subject.model_copy(update={
        "submodule_narratives": {key: narratives[key] for key in order},
        "evidence_ids_by_submodule": evidence_by_submodule,
        "assessments": assessments,
    })
    return first.model_copy(update={
        "owner_subject": owner_subject,
        "owner_scope_submodule_ids": order,
        "joint_dossiers": dossiers,
        "related_module_views": related,
    })


def _merge_batch_submissions(
    owner_module_id: str,
    results: list[CrossOwnerFindingSubmission],
) -> tuple[CrossOwnerFindingSubmission, dict[str, int]]:
    """Combine batch reviews; ids repeated across batches get a batch suffix."""

    dimensions: list[str] = []
    findings = []
    synthesis = []
    finding_batches: dict[str, int] = {}
    seen_synthesis: set[str] = set()
    for index, result in enumerate(results):
        dimensions.extend(
            item for item in result.coverage.checked_dimensions if item not in dimensions
        )
        for finding in result.findings:
            finding_id = finding.id
            if finding_id in finding_batches:
                finding_id = f"{finding.id}-b{index + 1}"
                finding = finding.model_copy(update={"id": finding_id})
            finding_batches[finding_id] = index
            findings.append(finding)
        for item in result.synthesis_inputs:
            if item.id in seen_synthesis:
                item = item.model_copy(update={"id": f"{item.id}-b{index + 1}"})
            seen_synthesis.add(item.id)
            synthesis.append(item)
    merged = CrossOwnerFindingSubmission(
        owner_module_id=owner_module_id,
        coverage=CrossReviewCoverageEntry(
            module_id=owner_module_id,
            checked_dimensions=dimensions,
        ),
        findings=findings,
        synthesis_inputs=synthesis,
    )
    return merged, finding_batches


def _build_owner_input(
    *,
    state: Mapping[str, Any],
    modules: Mapping[str, ModuleSubmission],
    metadata: Mapping[str, Any],
    owner_module_id: str,
    review_round: int,
    phase: str = "initial",
    dossiers: list[dict] | None = None,
) -> tuple[CrossOwnerInput, str]:
    owner = modules[owner_module_id]
    dossier_evidence = (
        {fact["evidence_id"] for dossier in dossiers for fact in dossier["facts"]}
        | {
            evidence_id for dossier in dossiers for judgment in dossier["judgments"]
            for evidence_id in judgment["evidence_ids"]
        }
        if dossiers else None
    )
    owner_scope = (
        {
            item["submodule_id"] for dossier in dossiers for item in dossier["submodules"]
            if item["module_id"] == owner_module_id
        } & set(owner.submodule_narratives)
        if dossiers else None
    ) or None
    owner_ref = _module_subject_ref(state, metadata, owner)
    related_refs: dict[str, str] = {}
    related_revisions: dict[str, int] = {}
    related_views: dict[str, CrossOwnerRelatedModuleView] = {}
    for module_id in REPORT_MODULE_IDS:
        if module_id == owner_module_id:
            continue
        related = modules[module_id]
        related_ref = _module_subject_ref(state, metadata, related)
        related_refs[module_id] = related_ref
        related_revisions[module_id] = related.revision
        related_views[module_id] = _related_view(
            related,
            subject_ref=related_ref,
            evidence_ids=dossier_evidence,
        )

    owner_view: ModuleContentView = module_content_view(owner, owner_scope)
    contract = CrossOwnerInput(
        phase=phase,
        run_id=str(state["run_id"]),
        review_round=review_round,
        owner_module_id=owner_module_id,
        review_focus=list(cross_lane_specialization(owner_module_id).review_focus),
        owner_subject_ref=owner_ref,
        owner_subject_revision=owner.revision,
        owner_subject=owner_view,
        owner_scope_submodule_ids=list(owner_view.submodule_narratives),
        joint_dossiers=list(dossiers or []),
        related_module_refs=related_refs,
        related_module_revisions=related_revisions,
        related_module_views=related_views,
    )
    ref = (
        f"Work/runs/{state['run_id']}/reviews/"
        f"cross-owner-input-r{review_round}-{owner_module_id}.json"
    )
    return contract, ref


def _build_initial_envelope(
    *,
    state: Mapping[str, Any],
    contract: CrossOwnerInput,
    owner_module_id: str,
    review_round: int,
    workflow_id: str,
    input_ref: str | None = None,
    batch_index: int | None = None,
) -> TaskEnvelope:
    specialization = cross_lane_specialization(owner_module_id)
    input_ref = input_ref or (
        f"Work/runs/{state['run_id']}/reviews/"
        f"cross-owner-input-r{review_round}-{owner_module_id}.json"
    )
    batch_suffix = "" if batch_index is None else f"-b{batch_index + 1}"
    return TaskEnvelope(
        task_id=f"cross-owner-{owner_module_id}-r{review_round}-initial{batch_suffix}",
        run_id=str(state["run_id"]),
        agent_id="cross-module-reviewer",
        objective=(
            f"以 Cross owner {owner_module_id} 身份审查本模块与其他四模块的关系；"
            "只创建归属本模块的 finding，并且 related 模块只读。"
        ),
        input_refs=[input_ref, *contract.related_module_refs.values()],
        constraints=[
            f"唯一 Cross owner 写作范围是模块 {owner_module_id}",
            "related_module_views 是紧凑只读关系视图，不得修改或创建其他模块 finding",
            "initial 只提交 cross_owner_finding_submission",
            "coverage 仅证明当前 owner 检查过六个维度，不代表 approved",
            *([
                "本轮只审查 joint_dossiers 中的对象：逐个对象核对各模块的事实、判断和正文摘录是否说得通；"
                "不同模块按各自方面判断时允许结论不同，同一方面以负责小节为准；"
                "只为本模块需要改的地方创建 finding，其他模块的问题由它自己的 owner 审查提出",
            ] if contract.joint_dossiers else []),
        ],
        allowed_outputs=["cross_owner_finding_submission"],
        allowed_tools=["search_text", "open_artifact", "submit_result"],
        revision=review_round,
        artifact_delivery_modes={
            input_ref: "inline",
            **{ref: "reference" for ref in contract.related_module_refs.values()},
        },
        target_submodule_ids=list(contract.owner_scope_submodule_ids),
        input_contract_kind="cross_owner_input",
        input_contract_ref=input_ref,
        inline_context=specialization.prompt_context(),
    )


class CrossOwnerAgentInvoker:
    """Bridge one typed Cross owner turn to the neutral Agent runtime."""

    def __init__(
        self,
        workspace: Path,
        *,
        execution: AgentExecutionService,
        session_factory: SessionFactory,
        workflow_id: str = "public-reporting",
        terminal_task_attempt_id: str = "",
        completed_result_loader: CompletedResultLoader | None = None,
        recovery_driver: AgentRecoveryDriver | None = None,
        progress_observer: ProgressObserver | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.execution = execution
        self.session_factory = session_factory
        self.workflow_id = workflow_id
        self.terminal_task_attempt_id = terminal_task_attempt_id
        self.completed_result_loader = completed_result_loader
        self.recovery_driver = recovery_driver
        self.progress_observer = progress_observer

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
        return await self._invoke_once(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=recovery_policy,
        )

    async def _invoke_once(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition | None = None,
    ) -> AgentInvocationOutcome:
        context = cross_runtime_context(value)
        preparation = self.preparation_for_output(context, task.output_contract)
        if preparation is None or preparation.envelope is None:
            return AgentInvocationOutcome(
                status="failed",
                session_id=conversation.external_session_id,
                error=f"Cross owner {task.output_contract} preparation has no TaskEnvelope",
            )
        workflow_id = preparation.workflow_id or self.workflow_id
        run_id = preparation.run_id
        batch_index = getattr(preparation, "batch_index", None)
        if batch_index is not None:
            # One loop action reviews several batches; each turn keeps its own id.
            task_id = f"{task_id}:b{batch_index + 1}"
        runtime_id = f"{workflow_id}:{agent.id}:{conversation.key.value}"
        session_id = conversation.external_session_id or (
            f"{workflow_id}:{conversation.key.value}"
        )
        if self.completed_result_loader is not None:
            persisted = self.completed_result_loader()
            if inspect.isawaitable(persisted):
                persisted = await persisted
            if persisted is not None:
                if not isinstance(persisted, AgentResult):
                    persisted = AgentResult.model_validate(persisted)
                conversation.external_session_id = persisted.session_id

                async def reuse_completed(_directive: Any) -> AgentInvocationOutcome:
                    return AgentInvocationOutcome(
                        status="ok",
                        result=self._decode_payload(
                            persisted.payload,
                            output_contract=task.output_contract,
                        ),
                        session_id=persisted.session_id,
                    )

                async def stop_completed(directive: Any) -> AgentInvocationOutcome:
                    return AgentInvocationOutcome(
                        status="incomplete",
                        session_id=persisted.session_id,
                        error=(
                            getattr(directive, "reason", None)
                            or "completed result recovery stopped"
                        ),
                    )

                recovered = await self.execution.recover_completed_result(
                    recovery=(
                        self.recovery_driver
                        or AgentRecoveryDriver(recovery_policy)
                    ),
                    detail={
                        "task_id": task.id,
                        "source": "persisted_result",
                    },
                    reuse_result=reuse_completed,
                    stop=stop_completed,
                )
                if isinstance(recovered, AgentInvocationOutcome):
                    return recovered
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
        def render_prompt() -> str:
            return self._prompt(agent, task, preparation)

        projection = bind_task_context(
            session.loop, self.workspace, preparation.envelope,
            output_contract=task.output_contract, render_prompt=render_prompt,
        )
        request = AgentTurnRequest(
            content=projection.initial_message() if projection is not None else render_prompt(),
            message_id=f"{task_id}:{run_id}:initial",
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
            sender=preparation.envelope.agent_id,
            terminal_task_id=preparation.envelope.task_id,
            terminal_task_attempt_id=self.terminal_task_attempt_id,
        )
        def decode_result(result_ref: str) -> dict[str, Any]:
            return self._decode_result(
                result_ref,
                output_contract=task.output_contract,
            )
        if recovery_policy is None:
            outcome = await typed_turn.dispatch(session, request, terminals=(terminal,))
            return TypedAgentTurn.map_outcome(
                outcome,
                session_id=session.session_id,
                decode_result=decode_result,
            )
        recovered = await execute_reporting_recovery(
            self.execution,
            session,
            request,
            recovery_policy=recovery_policy,
            terminals=(terminal,),
            prompt_builder=lambda event_kind: self._recovery_prompt(
                agent,
                task,
                preparation,
                event_kind,
            ),
            result_decoder=decode_result,
            recovery=self.recovery_driver,
            progress_observer=self.progress_observer,
        )
        if isinstance(recovered, AgentInvocationOutcome):
            return recovered
        return AgentInvocationOutcome(
            status="ok",
            result=recovered,
            session_id=session.session_id,
        )

    @staticmethod
    def _recovery_prompt(
        agent: AgentDefinition,
        task: TaskDefinition,
        preparation: Any,
        event_kind: Any,
    ) -> str:
        event_name = getattr(event_kind, "value", str(event_kind))
        if event_name == "max_tokens":
            instruction = (
                "继续当前 Cross owner 会话；上轮达到 max_tokens，"
                "不要重做已经完成的分析或工具调用。"
            )
        elif event_name == "tool_slice_boundary":
            instruction = (
                "继续当前 Cross owner 会话；复用已有 tool slice 结果，"
                "不要重放已经完成的工具。"
            )
        else:
            instruction = (
                "上一轮没有提交结构化结果；在当前会话中立即完成纠正，"
                "不要重复已经完成的分析。"
            )
        return "\n\n".join(
            (
                f"<{event_name}>",
                instruction,
                f"你仍是 {agent.id}，当前任务是 {task.id}。",
                "立即调用 submit_result，提交符合 output contract 的类型化结果。",
                json.dumps(
                    {
                        "run_id": preparation.run_id,
                        "owner_module_id": getattr(preparation, "owner_module_id", "joint"),
                        "output_contract": task.output_contract,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                f"</{event_name}>",
            )
        )

    @staticmethod
    def preparation_for_output(
        context: DeclarativeCrossOwnerRuntimeContext,
        output_contract: str,
    ) -> Any:
        local = getattr(context, "local_module_context", None)
        if output_contract == "declarative_module_review_agent_result":
            return local.review.prepared if local is not None and local.review else None
        if output_contract == "declarative_module_recheck_agent_result":
            return local.recheck.prepared if local is not None and local.recheck else None
        if output_contract == "declarative_module_revision_agent_result":
            if local is not None and local.revision is not None:
                return local.revision.prepared
            return context.revision_preparation
        if output_contract == "declarative_cross_owner_recheck_agent_result":
            return context.recheck_preparation
        if output_contract == "declarative_main_exception_agent_result":
            return context.main_preparation
        return context.preparation

    def _prompt(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        preparation: Any,
    ) -> str:
        envelope = preparation.envelope
        input_path = self.workspace / (
            envelope.input_contract_ref or envelope.input_refs[0]
        )
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        sections = [
            agent.instructions,
            f"Task: {task.objective}",
            # Compact: indentation of the five-module snapshot cost ~11% tokens.
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            f"Allowed tools: {json.dumps(task.tools, ensure_ascii=False)}",
            f"Output contract: {task.output_contract}",
        ]
        if envelope is not None and envelope.inline_context:
            sections.append(f"Cross owner specialization:\n{envelope.inline_context}")
        return "\n\n".join(sections)

    def _decode_result(self, result_ref: str, *, output_contract: str) -> dict[str, Any]:
        loaded = load_agent_result_payload(self.workspace, result_ref)
        return self._decode_payload(loaded.payload, output_contract=output_contract)

    @staticmethod
    def _decode_payload(payload: Any, *, output_contract: str) -> dict[str, Any]:
        if output_contract == "declarative_module_review_agent_result":
            return DeclarativeModuleReviewAgentResult(
                status="completed",
                submission=ModuleReviewFindingSubmission.model_validate(payload),
            ).model_dump(mode="json")
        if output_contract == "declarative_module_revision_agent_result":
            return DeclarativeModuleRevisionAgentResult(
                status="completed",
                submission=ModuleRevisionSubmission.model_validate(payload),
            ).model_dump(mode="json")
        if output_contract == "declarative_module_recheck_agent_result":
            return DeclarativeModuleRecheckAgentResult(
                status="completed",
                submission=ModuleReviewVerdictSubmission.model_validate(payload),
            ).model_dump(mode="json")
        if output_contract == "declarative_cross_owner_recheck_agent_result":
            return DeclarativeCrossOwnerRecheckAgentResult(
                status="completed",
                submission=CrossOwnerVerdictSubmission.model_validate(payload),
            ).model_dump(mode="json")
        if output_contract == "declarative_main_exception_agent_result":
            return DeclarativeMainExceptionAgentResult(
                status="completed",
                submission=WorkflowDecisionSubmission.model_validate(payload),
            ).model_dump(mode="json")
        submission = CrossOwnerFindingSubmission.model_validate(payload)
        return DeclarativeCrossOwnerInitialAgentResult(
            status="completed",
            submission=submission,
        ).model_dump(mode="json")


class CrossOwnerRuntime:
    """Compose Cross owner preparation, initial acceptance, and reduction."""

    @property
    def joint_tools(self):
        # The full-text joint Cross pass was replaced by program consolidation.
        from .observation_consolidation import build_observation_consolidation_tools
        return build_observation_consolidation_tools(self)

    def __init__(
        self,
        workspace: Path,
        *,
        agent_execution: AgentExecutionService | None = None,
        agent_session_factory: SessionFactory | None = None,
        workflow_id: str = "public-reporting",
        store: ReportingStore | None = None,
        input_budget: Callable[[], int] | int | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = store or ReportingStore(self.workspace)
        self.workflow_id = workflow_id
        # Estimated tokens one owner turn may carry as its retained input.
        self.input_budget = (
            input_budget
            if input_budget is not None
            else retained_input_budget_for(AgentDefaults())
        )
        self.agent_execution = agent_execution
        self.agent_session_factory = agent_session_factory
        if agent_execution is not None and callable(agent_session_factory):
            invoker = CrossOwnerAgentInvoker(
                self.workspace,
                execution=agent_execution,
                session_factory=agent_session_factory,
                workflow_id=workflow_id,
            )
            self.agent_invokers: Mapping[str, Any] = {
                "cross-module-reviewer": invoker,
                "main-agent": invoker,
            }
        else:
            self.agent_invokers = {}

    def _input_budget(self) -> int:
        budget = self.input_budget() if callable(self.input_budget) else self.input_budget
        return int(budget)

    def prepare(self, value: Any) -> dict[str, Any]:
        state = _state_from(value)
        modules = _module_submissions(state)
        metadata = _metadata_map(state)
        review_round = int(state.get("cross_review_round", 0))
        refs: dict[str, str] = {}
        inputs: dict[str, CrossOwnerInput] = {}
        batch_refs: dict[str, list[dict[str, Any]]] = {}
        skipped: list[str] = []
        budget = self._input_budget()

        def owner_contract(owner_module_id: str, dossiers: list[dict] | None):
            contract, ref = _build_owner_input(
                state=state,
                modules=modules,
                metadata=metadata,
                owner_module_id=owner_module_id,
                review_round=review_round,
                dossiers=dossiers,
            )
            contract = contract.model_copy(update={"report_instruction": report_instruction_from_state(state)})
            if state.get("revision_input_changes"):
                contract = CrossOwnerInput.model_validate({
                    **contract.model_dump(mode="json"),
                    "input_changes": state["revision_input_changes"],
                })
            return contract, ref

        for owner_module_id in REPORT_MODULE_IDS:
            dossiers = _owner_dossiers(state, self.workspace, owner_module_id)
            contract, ref = owner_contract(owner_module_id, dossiers)
            if dossiers is not None and not dossiers:
                skipped.append(owner_module_id)
            self.store.write_json(ref, contract.model_dump(mode="json"))
            refs[owner_module_id] = ref
            inputs[owner_module_id] = contract
            batches = _dossier_batches(
                dossiers or [],
                lambda part, owner=owner_module_id: _input_tokens(owner_contract(owner, part)[0]),
                budget,
            )
            if len(batches) > 1:
                owner_batches = []
                for index, part in enumerate(batches):
                    batch_ref = ref.removesuffix(".json") + f"-b{index + 1}.json"
                    batch_contract, _ = owner_contract(owner_module_id, part)
                    self.store.write_json(batch_ref, batch_contract.model_dump(mode="json"))
                    owner_batches.append({
                        "ref": batch_ref,
                        "dossier_ids": [dossier["dossier_id"] for dossier in part],
                    })
                batch_refs[owner_module_id] = owner_batches
        state["cross_owner_input_refs"] = refs
        state["cross_owner_inputs"] = inputs
        state["cross_owner_input_batches"] = batch_refs
        state["cross_owner_input_budget_tokens"] = budget
        state["cross_owner_without_dossiers"] = skipped
        state["cross_review_round"] = review_round
        state["cross_owner_prepared"] = True
        return state

    def prepare_initial(self, value: Any) -> DeclarativeCrossOwnerRuntimeContext:
        raw = value if isinstance(value, Mapping) else {}
        state = _state_from(raw.get("state", raw))
        owner_module_id = str(raw.get("owner_module_id") or raw.get("owner-module-id") or "")
        if not isinstance(state.get("cross_owner_inputs"), Mapping):
            state = self.prepare(state)
        contract = _model(state["cross_owner_inputs"][owner_module_id], CrossOwnerInput)
        review_round = contract.review_round
        input_ref = str(state["cross_owner_input_refs"][owner_module_id])
        workflow_id = str(state.get("workflow_id") or self.workflow_id)
        session_key = f"cross-owner-{owner_module_id}"
        user_supplements = _state_user_supplements(state)
        completion_refs = state.get("module_review_completion_refs")
        prior_completion_ref = (
            str(completion_refs[owner_module_id])
            if isinstance(completion_refs, Mapping)
            and owner_module_id in completion_refs
            else None
        )
        result_ref = (
            f"Work/runs/{contract.run_id}/reviews/"
            f"cross-owner-findings-r{review_round}-{owner_module_id}.json"
        )
        existing_path = self.workspace / result_ref
        if not existing_path.is_file() and owner_module_id in state.get(
            "cross_owner_without_dossiers", []
        ):
            # No object of this module needs a joint look: record an empty review.
            self.store.write_json(result_ref, CrossOwnerFindingSubmission(
                owner_module_id=owner_module_id,
                coverage=CrossReviewCoverageEntry(
                    module_id=owner_module_id, checked_dimensions=["facts"],
                ),
            ).model_dump(mode="json"))
        if existing_path.is_file():
            existing = CrossOwnerFindingSubmission.model_validate_json(
                existing_path.read_text(encoding="utf-8")
            )
            if existing.owner_module_id != owner_module_id:
                raise ValueError(
                    f"Persisted Cross owner result belongs to {existing.owner_module_id}"
                )
            preparation = CrossOwnerInitialReviewPreparation(
                mode="continue_existing",
                run_id=contract.run_id,
                workflow_id=workflow_id,
                owner_module_id=owner_module_id,
                review_round=review_round,
                reviewer_session_key=session_key,
                owner_input_ref=input_ref,
                owner_input=contract,
                user_supplements=user_supplements,
                prior_module_review_completion_ref=prior_completion_ref,
                existing_result_ref=result_ref,
                existing_result=existing,
            )
            acceptance = CrossOwnerInitialReviewAcceptance(
                run_id=contract.run_id,
                workflow_id=workflow_id,
                owner_module_id=owner_module_id,
                reviewer_session_key=session_key,
                owner_input_ref=input_ref,
                user_supplements=user_supplements,
                prior_module_review_completion_ref=prior_completion_ref,
                result_ref=result_ref,
                result=existing,
            )
            return DeclarativeCrossOwnerRuntimeContext(
                owner_module_id=owner_module_id,
                status="initial_resumed",
                preparation=preparation,
                acceptance=acceptance,
            )
        envelope = _build_initial_envelope(
            state=state,
            contract=contract,
            owner_module_id=owner_module_id,
            review_round=review_round,
            workflow_id=workflow_id,
        )
        preparation = CrossOwnerInitialReviewPreparation(
            mode="invoke_agent",
            run_id=contract.run_id,
            workflow_id=workflow_id,
            owner_module_id=owner_module_id,
            review_round=review_round,
            reviewer_session_key=session_key,
            owner_input_ref=input_ref,
            owner_input=contract,
            user_supplements=user_supplements,
            prior_module_review_completion_ref=prior_completion_ref,
            envelope=envelope,
        )
        batch_specs = (state.get("cross_owner_input_batches") or {}).get(owner_module_id) or []
        if len(batch_specs) <= 1:
            return DeclarativeCrossOwnerRuntimeContext(
                owner_module_id=owner_module_id,
                status="initial_ready",
                preparation=preparation,
            )
        batches: list[CrossOwnerInitialReviewPreparation] = []
        results: list[CrossOwnerFindingSubmission | None] = []
        for index, spec in enumerate(batch_specs):
            batch_ref = str(spec["ref"])
            batch_contract = CrossOwnerInput.model_validate_json(
                (self.workspace / batch_ref).read_text(encoding="utf-8")
            )
            batch_result_ref = result_ref.removesuffix(".json") + f"-b{index + 1}.json"
            batch_result_path = self.workspace / batch_result_ref
            persisted = (
                CrossOwnerFindingSubmission.model_validate_json(
                    batch_result_path.read_text(encoding="utf-8")
                )
                if batch_result_path.is_file()
                else None
            )
            results.append(persisted)
            batches.append(preparation.model_copy(update={
                "mode": "continue_existing" if persisted is not None else "invoke_agent",
                "owner_input_ref": batch_ref,
                "owner_input": batch_contract,
                "envelope": _build_initial_envelope(
                    state=state,
                    contract=batch_contract,
                    owner_module_id=owner_module_id,
                    review_round=review_round,
                    workflow_id=workflow_id,
                    input_ref=batch_ref,
                    batch_index=index,
                ),
                "existing_result_ref": batch_result_ref if persisted is not None else None,
                "existing_result": persisted,
                "batch_index": index,
                "batch_dossier_ids": list(spec.get("dossier_ids") or []),
            }))
        context = DeclarativeCrossOwnerRuntimeContext(
            owner_module_id=owner_module_id,
            status="initial_ready",
            preparation=next(
                (batch for batch in batches if batch.mode == "invoke_agent"), batches[0],
            ),
            initial_full_preparation=preparation,
            initial_batches=batches,
            initial_batch_results=results,
        )
        if all(result is not None for result in results):
            return self.merge_initial_batches(context)
        return context

    @staticmethod
    def initial_batch_items(value: Any) -> list[dict[str, int]]:
        """Batch positions the initial review loop visits, in order."""

        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        count = len(context.initial_batches) or 1
        return [{"batch_index": index} for index in range(count)]

    def select_initial_batch(self, value: Any) -> DeclarativeCrossOwnerRuntimeContext:
        """Point the initial turn at one dossier batch (or the single input)."""

        if not isinstance(value, Mapping):
            raise TypeError("Cross owner batch selection requires context and batch")
        context = _model(value.get("context"), DeclarativeCrossOwnerRuntimeContext)
        if not context.initial_batches:
            return context
        batch = value.get("batch") or {}
        index = int(batch.get("batch_index", 0) if isinstance(batch, Mapping) else batch)
        return context.model_copy(update={"preparation": context.initial_batches[index]})

    def merge_initial_batches(self, value: Any) -> DeclarativeCrossOwnerRuntimeContext:
        """Record the owner's initial review once every batch has a result."""

        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        if not context.initial_batches or context.status in {"initial_accepted", "initial_resumed"}:
            return context
        full = context.initial_full_preparation
        if full is None:
            raise ValueError("Cross owner batches have no full-scope preparation")
        missing = [
            index + 1 for index, result in enumerate(context.initial_batch_results)
            if result is None
        ]
        if missing:
            raise ValueError(f"Cross owner initial batches without a result: {missing}")
        merged, finding_batches = _merge_batch_submissions(
            context.owner_module_id,
            [cast(CrossOwnerFindingSubmission, result) for result in context.initial_batch_results],
        )
        result_ref = (
            f"Work/runs/{full.run_id}/reviews/"
            f"cross-owner-findings-r{full.review_round}-{context.owner_module_id}.json"
        )
        self.store.write_json(result_ref, merged.model_dump(mode="json"))
        acceptance = CrossOwnerInitialReviewAcceptance(
            run_id=full.run_id,
            workflow_id=full.workflow_id,
            owner_module_id=full.owner_module_id,
            reviewer_session_key=full.reviewer_session_key,
            owner_input_ref=full.owner_input_ref,
            user_supplements=full.user_supplements,
            prior_module_review_completion_ref=full.prior_module_review_completion_ref,
            result_ref=result_ref,
            result=merged,
        )
        return context.model_copy(update={
            "status": "initial_accepted",
            "preparation": full,
            "acceptance": acceptance,
            "initial_finding_batches": finding_batches,
            "error": None,
        })

    @staticmethod
    def initial_requires_agent(value: Any) -> bool:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        return bool(
            context.status == "initial_ready"
            and context.preparation is not None
            and context.preparation.mode == "invoke_agent"
        )

    def accept_initial(self, value: Any) -> DeclarativeCrossOwnerRuntimeContext:
        if not isinstance(value, Mapping):
            raise TypeError("Cross owner initial acceptance requires context and result")
        context = _model(value.get("context"), DeclarativeCrossOwnerRuntimeContext)
        preparation = context.preparation
        if preparation is None:
            raise ValueError("Cross owner initial acceptance has no preparation")
        if context.initial_batches and preparation.batch_index is not None:
            return self._accept_initial_batch(context, preparation, value.get("result"))
        if preparation.mode == "continue_existing":
            if value.get("result") is not None:
                raise ValueError("persisted Cross owner result cannot be accepted twice")
            if preparation.existing_result is None or preparation.existing_result_ref is None:
                raise ValueError("Cross owner continuation has no persisted result")
            submission = preparation.existing_result
            result_ref = preparation.existing_result_ref
        else:
            result = _model(value.get("result"), DeclarativeCrossOwnerInitialAgentResult)
            if result.status != "completed" or result.submission is None:
                return context.model_copy(
                    update={
                        "status": "failed",
                        "error": result.error or "Cross owner initial Agent failed",
                    }
                )
            submission = result.submission
            if submission.owner_module_id != context.owner_module_id:
                raise ValueError("Cross owner initial result belongs to another owner")
            result_ref = (
                f"Work/runs/{preparation.run_id}/reviews/"
                f"cross-owner-findings-r{preparation.review_round}-{context.owner_module_id}.json"
            )
            self.store.write_json(result_ref, submission.model_dump(mode="json"))
        acceptance = CrossOwnerInitialReviewAcceptance(
            run_id=preparation.run_id,
            workflow_id=preparation.workflow_id,
            owner_module_id=preparation.owner_module_id,
            reviewer_session_key=preparation.reviewer_session_key,
            owner_input_ref=preparation.owner_input_ref,
            user_supplements=preparation.user_supplements,
            prior_module_review_completion_ref=(
                preparation.prior_module_review_completion_ref
            ),
            result_ref=result_ref,
            result=submission,
        )
        return context.model_copy(
            update={
                "status": "initial_accepted",
                "preparation": preparation,
                "acceptance": acceptance,
                "error": None,
            }
        )

    def _accept_initial_batch(
        self,
        context: DeclarativeCrossOwnerRuntimeContext,
        preparation: CrossOwnerInitialReviewPreparation,
        value: Any,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        result = _model(value, DeclarativeCrossOwnerInitialAgentResult)
        if result.status != "completed" or result.submission is None:
            return context.model_copy(update={
                "status": "failed",
                "error": result.error or "Cross owner initial Agent failed",
            })
        submission = result.submission
        if submission.owner_module_id != context.owner_module_id:
            raise ValueError("Cross owner initial result belongs to another owner")
        index = cast(int, preparation.batch_index)
        batch_ref = (
            f"Work/runs/{preparation.run_id}/reviews/"
            f"cross-owner-findings-r{preparation.review_round}-{context.owner_module_id}"
            f"-b{index + 1}.json"
        )
        self.store.write_json(batch_ref, submission.model_dump(mode="json"))
        batches = list(context.initial_batches)
        batches[index] = preparation.model_copy(update={
            "mode": "continue_existing",
            "existing_result_ref": batch_ref,
            "existing_result": submission,
        })
        results = list(context.initial_batch_results)
        results[index] = submission
        return context.model_copy(update={
            "preparation": batches[index],
            "initial_batches": batches,
            "initial_batch_results": results,
        })

    @staticmethod
    def initial_has_findings(value: Any) -> bool:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        return bool(context.acceptance and any(
            finding.impact == "blocking" for finding in context.acceptance.result.findings
        ))

    async def prepare_revision(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Prepare or reuse the original owner Author's bounded revision."""

        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        acceptance = context.acceptance
        initial_preparation = context.preparation
        if acceptance is None or initial_preparation is None:
            raise ValueError("Cross owner revision requires accepted initial findings")

        if context.round_progress is not None:
            progress = context.round_progress
            findings = list(progress.pending)
            current = progress.lane.module
            reviewed_baseline = current
            finding_refs = list(progress.finding_refs)
            review_round = progress.next_review_round
            owner_input_ref = progress.next_owner_input_ref
            prior_completion_ref = progress.lane.local_review_ref
        else:
            findings = [f for f in acceptance.result.findings if f.impact == "blocking"]
            owner_subject_path = self.workspace / initial_preparation.owner_input.owner_subject_ref
            current = ModuleSubmission.model_validate_json(
                owner_subject_path.read_text(encoding="utf-8")
            )
            reviewed_baseline = current
            finding_refs = [acceptance.result_ref]
            review_round = initial_preparation.review_round + 1
            owner_input_ref = acceptance.owner_input_ref
            prior_completion_ref = acceptance.prior_module_review_completion_ref
        if not findings:
            raise ValueError("Cross owner revision requires at least one pending finding")

        existing = load_module_revision_candidate(
            workspace=self.workspace,
            run_id=acceptance.run_id,
            module_id=context.owner_module_id,
            current=current,
            findings=findings,
        )
        if existing is not None:
            candidate, candidate_ref = existing
            preparation = CrossOwnerRevisionPreparation(
                mode="continue_existing",
                run_id=acceptance.run_id,
                workflow_id=acceptance.workflow_id,
                owner_module_id=context.owner_module_id,
                review_round=review_round,
                owner_input_ref=owner_input_ref,
                current=current,
                reviewed_baseline=reviewed_baseline,
                findings=findings,
                finding_refs=finding_refs,
                user_supplements=acceptance.user_supplements,
                prior_completion_ref=prior_completion_ref,
                existing_candidate=candidate,
                existing_candidate_ref=candidate_ref,
            )
            revision_acceptance = CrossOwnerRevisionAcceptance(
                run_id=preparation.run_id,
                workflow_id=preparation.workflow_id,
                owner_module_id=preparation.owner_module_id,
                review_round=preparation.review_round,
                owner_input_ref=preparation.owner_input_ref,
                current=reviewed_baseline,
                findings=findings,
                finding_refs=finding_refs,
                user_supplements=acceptance.user_supplements,
                prior_completion_ref=preparation.prior_completion_ref,
                revised=candidate,
                candidate_ref=candidate_ref,
            )
            return context.model_copy(
                update={
                    "status": "revision_resumed",
                    "revision_preparation": preparation,
                    "revision_acceptance": revision_acceptance,
                    "error": None,
                }
            )

        prepared = await prepare_module_revision(
            workspace=self.workspace,
            store=self.store,
            state=self._owner_business_state(context, acceptance.run_id),
            workflow_id=acceptance.workflow_id,
            subject=current,
            cross_findings=findings,
            user_supplements=acceptance.user_supplements,
        )
        preparation = CrossOwnerRevisionPreparation(
            mode="invoke_agent",
            run_id=acceptance.run_id,
            workflow_id=acceptance.workflow_id,
            owner_module_id=context.owner_module_id,
            review_round=review_round,
            owner_input_ref=owner_input_ref,
            current=current,
            reviewed_baseline=reviewed_baseline,
            findings=findings,
            finding_refs=finding_refs,
            user_supplements=acceptance.user_supplements,
            prior_completion_ref=prior_completion_ref,
            prepared=prepared,
        )
        return context.model_copy(
            update={
                "status": "revision_ready",
                "revision_preparation": preparation,
                "revision_acceptance": None,
                "error": None,
            }
        )

    @staticmethod
    def revision_requires_agent(value: Any) -> bool:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        preparation = context.revision_preparation
        return bool(
            context.status == "revision_ready"
            and preparation is not None
            and preparation.mode == "invoke_agent"
        )

    @staticmethod
    def module_revision_agent_input(
        value: Any,
    ) -> DeclarativeModuleRuntimeLaneContext:
        """Project a Cross-owned revision into the original Author boundary."""

        return project_cross_owner_module_revision_agent_input(value)

    @staticmethod
    def local_module_agent_input(
        value: Any,
    ) -> DeclarativeModuleRuntimeLaneContext:
        """Project the prepared Cross-local Auditor lane into its Agent boundary."""

        return project_cross_owner_local_module_agent_input(value)

    def accept_revision(self, value: Any) -> DeclarativeCrossOwnerRuntimeContext:
        """Accept one typed Author revision or its same-run candidate."""

        if not isinstance(value, Mapping):
            raise TypeError("Cross owner revision acceptance requires context and result")
        context = _model(value.get("context"), DeclarativeCrossOwnerRuntimeContext)
        preparation = context.revision_preparation
        if preparation is None:
            raise ValueError("Cross owner revision acceptance has no preparation")
        if preparation.mode == "continue_existing":
            if (
                preparation.existing_candidate is None
                or preparation.existing_candidate_ref is None
            ):
                raise ValueError("Cross owner revision continuation has no candidate")
            revised = preparation.existing_candidate
            candidate_ref = preparation.existing_candidate_ref
        else:
            result = _model(value.get("result"), DeclarativeModuleRevisionAgentResult)
            if result.status != "completed" or result.submission is None:
                return context.model_copy(
                    update={
                        "status": "failed",
                        "error": result.error or "Cross owner revision Agent failed",
                    }
                )
            revised, candidate_ref = accept_module_revision(
                workspace=self.workspace,
                store=self.store,
                preparation=cast(ModuleRevisionPreparation, preparation.prepared),
                result=result.submission,
            )
        revision_acceptance = CrossOwnerRevisionAcceptance(
            run_id=preparation.run_id,
            workflow_id=preparation.workflow_id,
            owner_module_id=preparation.owner_module_id,
            review_round=preparation.review_round,
            owner_input_ref=preparation.owner_input_ref,
            current=preparation.reviewed_baseline or preparation.current,
            findings=preparation.findings,
            finding_refs=preparation.finding_refs,
            user_supplements=preparation.user_supplements,
            prior_completion_ref=preparation.prior_completion_ref,
            revised=revised,
            candidate_ref=candidate_ref,
        )
        return context.model_copy(
            update={
                "status": "revision_accepted",
                "revision_acceptance": revision_acceptance,
                "error": None,
            }
        )

    def prepare_author_exception(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Prepare Main only for explicit Author dispute/input responses."""

        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        revision = context.revision_acceptance
        if context.status == "failed" or revision is None:
            return context
        exceptional = [
            response
            for response in revision.revised.revision_responses
            if response.action in {"disputed", "needs_input"}
        ]
        if not exceptional:
            return context.model_copy(
                update={
                    "status": "author_exception_not_required",
                    "main_preparation": None,
                    "main_acceptance": None,
                }
            )
        preparation = prepare_main_exception_decision(
            store=self.store,
            run_id=revision.run_id,
            workflow_id=revision.workflow_id,
            scope="cross",
            subject_refs=[revision.candidate_ref],
            finding_refs=revision.finding_refs,
            verdicts=[],
            responses=exceptional,
            trigger="author_response",
        )
        if preparation.mode == "continue_existing":
            return self._accept_prepared_main_exception(
                context,
                preparation=preparation,
                result=None,
                resumed=True,
            )
        return context.model_copy(
            update={
                "status": "author_exception_ready",
                "main_preparation": preparation,
                "main_acceptance": None,
            }
        )

    def prepare_reviewer_exception(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Prepare Main only for explicit Cross reviewer escalations."""

        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        recheck = context.recheck_acceptance
        if context.status == "failed" or recheck is None:
            return context
        escalated = [
            verdict for verdict in recheck.result.verdicts if verdict.verdict == "escalate"
        ]
        if not escalated:
            return context.model_copy(
                update={
                    "status": "reviewer_exception_not_required",
                    "main_preparation": None,
                    "main_acceptance": None,
                }
            )
        subject = recheck.lane.completion.subject
        subject_ref = subject.ref if hasattr(subject, "ref") else str(subject)
        preparation = prepare_main_exception_decision(
            store=self.store,
            run_id=recheck.run_id,
            workflow_id=recheck.workflow_id,
            scope="cross",
            subject_refs=[subject_ref],
            finding_refs=(
                list(context.round_progress.finding_refs)
                if context.round_progress is not None
                else [recheck.initial_result_ref]
            ),
            verdicts=escalated,
            responses=recheck.lane.responses,
            trigger="reviewer_escalation",
        )
        if preparation.mode == "continue_existing":
            return self._accept_prepared_main_exception(
                context,
                preparation=preparation,
                result=None,
                resumed=True,
            )
        return context.model_copy(
            update={
                "status": "reviewer_exception_ready",
                "main_preparation": preparation,
                "main_acceptance": None,
            }
        )

    @staticmethod
    def main_exception_requires_agent(value: Any) -> bool:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        return context.status in {"author_exception_ready", "reviewer_exception_ready"}

    def accept_main_exception(self, value: Any) -> DeclarativeCrossOwnerRuntimeContext:
        """Accept a typed Main result while retaining the decision artifact ref."""

        if not isinstance(value, Mapping):
            raise TypeError("Main exception acceptance requires context and result")
        context = _model(value.get("context"), DeclarativeCrossOwnerRuntimeContext)
        preparation = context.main_preparation
        if preparation is None:
            raise ValueError("Main exception acceptance has no preparation")
        raw_result = value.get("result")
        if isinstance(raw_result, DeclarativeMainExceptionAgentResult) or (
            isinstance(raw_result, Mapping) and "status" in raw_result
        ):
            result = _model(raw_result, DeclarativeMainExceptionAgentResult)
            if result.status != "completed" or result.submission is None:
                return context.model_copy(
                    update={
                        "status": "failed",
                        "error": result.error or "Main exception Agent failed",
                    }
                )
            submission = result.submission
        else:
            submission = _model(raw_result, WorkflowDecisionSubmission)
        return self._accept_prepared_main_exception(
            context,
            preparation=preparation,
            result=submission,
            resumed=False,
        )

    def _accept_prepared_main_exception(
        self,
        context: DeclarativeCrossOwnerRuntimeContext,
        *,
        preparation: MainExceptionDecisionPreparation,
        result: WorkflowDecisionSubmission | None,
        resumed: bool,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        decision_state: dict[str, Any] = {
            "review_exception_refs": list(context.review_exception_refs)
        }
        acceptance = accept_main_exception_decision(
            store=self.store,
            preparation=preparation,
            result=result,
            state=decision_state,
            raise_for_terminal_decisions=False,
        )
        if acceptance.result.decision == "stop_incomplete":
            return context.model_copy(
                update={
                    "status": "failed",
                    "main_preparation": preparation,
                    "main_acceptance": acceptance,
                    "review_exception_refs": decision_state["review_exception_refs"],
                    "error": acceptance.result.rationale,
                }
            )
        prefix = "author" if preparation.trigger == "author_response" else "reviewer"
        return context.model_copy(
            update={
                "status": f"{prefix}_exception_{'resumed' if resumed else 'accepted'}",
                "main_preparation": preparation,
                "main_acceptance": acceptance,
                "review_exception_refs": decision_state["review_exception_refs"],
                "error": None,
            }
        )

    @staticmethod
    def main_exception_requests_user(value: Any) -> bool:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        return bool(
            context.status != "failed"
            and context.main_acceptance is not None
            and context.main_acceptance.result.decision == "request_user"
        )

    def apply_main_exception_user_input(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Apply one generic Interaction response to the prepared exception."""

        if not isinstance(value, Mapping):
            raise TypeError("Main exception user input requires context and input")
        context = _model(value.get("context"), DeclarativeCrossOwnerRuntimeContext)
        preparation = context.main_preparation
        if preparation is None:
            raise ValueError("Main exception user input has no preparation")
        supplied = _model(value.get("input"), DeclarativeMainExceptionUserInput)
        return self._accept_prepared_main_exception(
            context,
            preparation=preparation,
            result=WorkflowDecisionSubmission(
                decision=supplied.decision,
                rationale=supplied.rationale,
                finding_ids=list(preparation.exception_ids),
            ),
            resumed=False,
        )

    @staticmethod
    def author_exception_returns_to_author(value: Any) -> bool:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        return bool(
            context.status != "failed"
            and context.revision_acceptance is not None
            and context.main_acceptance is not None
            and context.main_acceptance.trigger == "author_response"
            and context.main_acceptance.result.decision == "return_to_author"
        )

    def _owner_business_state(self, context: DeclarativeCrossOwnerRuntimeContext, run_id: str) -> dict[str, Any]:
        from .research.project_evidence import ProjectEvidenceIndex

        owner_input = context.preparation.owner_input if context.preparation is not None else None
        return {
            "run_id": run_id,
            "report_instruction": owner_input.report_instruction if owner_input else "",
            "revision_input_changes": owner_input.input_changes if owner_input else None,
            "evidence_items": [item for item in ProjectEvidenceIndex(self.workspace, run_id=run_id).items()
                               if item.module_id in {None, context.owner_module_id}],
        }

    async def prepare_local_review(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Prepare the original module Auditor's scoped local regression."""

        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        revision = context.revision_acceptance
        if revision is None:
            raise ValueError("Cross owner local review requires an accepted revision")
        previous_lane = context.local_module_context
        current = (
            previous_lane.module
            if previous_lane is not None and previous_lane.module is not None
            else revision.revised
        )
        previous_preparation = context.local_review_preparation
        if (
            previous_preparation is not None
            and previous_preparation.regression_context is not None
        ):
            regression_context = previous_preparation.regression_context
            local_scope = set(previous_preparation.prepared.scope)
        else:
            regression_context, local_scope = build_cross_owner_local_regression_context(
                workspace=self.workspace,
                store=self.store,
                run_id=revision.run_id,
                owner_module_id=revision.owner_module_id,
                reviewed_baseline=revision.current,
                revised=current,
                findings=list(revision.findings),
                review_round=revision.review_round,
                prior_completion_ref=revision.prior_completion_ref,
            )
        previous_preflight_progress = (
            previous_lane.review.prepared.preflight_progress
            if previous_lane is not None and previous_lane.review is not None
            else None
        )
        business_state = self._owner_business_state(context, revision.run_id)
        prepared = prepare_module_local_regression_review(
            store=self.store,
            workflow_id=revision.workflow_id,
            run_id=revision.run_id,
            current=current,
            scope=local_scope,
            review_round=revision.review_round,
            regression_context=regression_context,
            user_supplements=revision.user_supplements,
            report_instruction=business_state["report_instruction"],
            input_changes=business_state["revision_input_changes"],
            previous_preflight_progress=previous_preflight_progress,
        )
        preparation = CrossOwnerLocalReviewPreparation(
            mode=prepared.mode,
            run_id=revision.run_id,
            workflow_id=revision.workflow_id,
            owner_module_id=revision.owner_module_id,
            review_round=revision.review_round,
            owner_input_ref=revision.owner_input_ref,
            reviewed_baseline=revision.current,
            cross_responses=list(revision.revised.revision_responses),
            regression_context=regression_context,
            prepared=prepared,
        )
        reporting_state = (
            previous_lane.reporting_state
            if previous_lane is not None
            else {
                "run_id": revision.run_id,
                "request": {
                    "user_supplements": [
                        item.model_dump(mode="json")
                        for item in revision.user_supplements
                    ]
                },
            }
        )
        reporting_state = {**reporting_state, **business_state}
        lane = DeclarativeModuleRuntimeLaneContext(
            module_id=revision.owner_module_id,
            workflow_id=revision.workflow_id,
            reporting_state=reporting_state,
            status=(
                "preflight_revision_pending"
                if prepared.mode == "preflight_revision"
                else "review_ready"
            ),
            review=DeclarativeModuleReviewPreparation(
                envelope=prepared.envelope,
                reviewer_session_key=prepared.reviewer_session_key,
                prepared=prepared,
            ),
            module=prepared.current,
        )
        return context.model_copy(
            deep=True,
            update={
                "status": "local_review_ready",
                "local_review_preparation": preparation,
                "local_review_acceptance": None,
                "local_module_context": lane,
                "error": None,
            }
        )

    @staticmethod
    def local_review_requires_agent(value: Any) -> bool:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        return bool(
            context.status == "local_review_ready"
            and context.local_module_context is not None
            and module_review_requires_agent(context.local_module_context)
        )

    @staticmethod
    def local_review_preflight_needs_revision(value: Any) -> bool:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        return bool(
            context.local_module_context is not None
            and module_review_preflight_needs_revision(
                context.local_module_context
            )
        )

    async def prepare_local_preflight_revision(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        lane = self._require_local_module_context(context)
        prepared = await prepare_current_module_preflight_revision(
            lane,
            store=self.store,
        )
        return context.model_copy(
            deep=True,
            update={"local_module_context": prepared, "error": None},
        )

    def accept_local_preflight_revision(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        if not isinstance(value, Mapping):
            raise TypeError("Cross local preflight acceptance requires context and result")
        context = _model(value.get("context"), DeclarativeCrossOwnerRuntimeContext)
        lane = self._require_local_module_context(context)
        accepted = accept_current_module_preflight_revision(
            {"context": lane, "result": value.get("result")},
            store=self.store,
        )
        return context.model_copy(
            deep=True,
            update={"local_module_context": accepted, "error": accepted.error},
        )

    def accept_local_review(self, value: Any) -> DeclarativeCrossOwnerRuntimeContext:
        """Accept one typed original-Auditor local-regression result."""

        if not isinstance(value, Mapping):
            raise TypeError("Cross owner local review acceptance requires context and result")
        context = _model(value.get("context"), DeclarativeCrossOwnerRuntimeContext)
        preparation = context.local_review_preparation
        lane = self._require_local_module_context(context)
        if preparation is None or preparation.prepared is None or lane.review is None:
            raise ValueError("Cross owner local review acceptance has no preparation")
        raw_result = value.get("result")
        if isinstance(raw_result, DeclarativeModuleReviewAgentResult) or (
            isinstance(raw_result, Mapping) and "status" in raw_result
        ):
            result = _model(raw_result, DeclarativeModuleReviewAgentResult)
            if result.status != "completed" or result.submission is None:
                return context.model_copy(
                    update={
                        "status": "failed",
                        "error": result.error or "Cross owner local Auditor failed",
                    }
                )
            typed_result = result
        else:
            typed_result = DeclarativeModuleReviewAgentResult(
                status="completed",
                submission=_model(raw_result, ModuleReviewFindingSubmission),
            )
        accepted_lane = accept_current_module_review(
            {"context": lane, "result": typed_result},
            store=self.store,
        )
        review = accepted_lane.review.acceptance
        if review is None:
            raise ValueError("Cross owner local Auditor result was not accepted")
        acceptance = CrossOwnerLocalReviewAcceptance(
            run_id=preparation.run_id,
            workflow_id=preparation.workflow_id,
            owner_module_id=preparation.owner_module_id,
            review_round=preparation.review_round,
            owner_input_ref=preparation.owner_input_ref,
            reviewed_baseline=preparation.reviewed_baseline,
            cross_responses=preparation.cross_responses,
            regression_context=cast(
                ModuleLocalRegressionContext,
                preparation.regression_context,
            ),
            review=review,
        )
        return context.model_copy(
            deep=True,
            update={
                "status": "local_review_accepted",
                "local_review_acceptance": acceptance,
                "local_module_context": accepted_lane,
                "error": None,
            }
        )

    @staticmethod
    def local_review_needs_revision(value: Any) -> bool:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        return bool(
            context.local_module_context is not None
            and module_review_needs_revision(context.local_module_context)
        )

    async def prepare_local_module_revision(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        lane = self._require_local_module_context(context)
        review = lane.review
        acceptance = review.acceptance if review is not None else None
        if acceptance is None or lane.module is None:
            raise ValueError("Cross local module revision requires accepted findings")
        prepared = await prepare_module_revision(
            workspace=self.workspace,
            store=self.store,
            state=lane.reporting_state,
            workflow_id=lane.workflow_id,
            subject=lane.module,
            module_findings=list(acceptance.findings),
            user_supplements=request_user_supplements(
                lane.reporting_state.get("request")
            ),
        )
        prepared_lane = lane.model_copy(
            deep=True,
            update={
                "status": "revision_ready",
                "revision": DeclarativeModuleRevisionPreparation(prepared=prepared),
                "error": None,
            },
        )
        return context.model_copy(
            deep=True,
            update={"local_module_context": prepared_lane, "error": None},
        )

    @staticmethod
    def local_revision_requires_agent(value: Any) -> bool:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        lane = context.local_module_context
        return bool(lane is not None and lane.status == "revision_ready")

    def accept_local_module_revision(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        if not isinstance(value, Mapping):
            raise TypeError("Cross local revision acceptance requires context and result")
        context = _model(value.get("context"), DeclarativeCrossOwnerRuntimeContext)
        lane = self._require_local_module_context(context)
        accepted = accept_current_module_revision(
            {"context": lane, "result": value.get("result")},
            store=self.store,
        )
        return context.model_copy(
            deep=True,
            update={"local_module_context": accepted, "error": accepted.error},
        )

    def prepare_local_module_recheck(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        lane = self._require_local_module_context(context)
        prepared = prepare_current_module_recheck(lane, store=self.store)
        return context.model_copy(
            deep=True,
            update={"local_module_context": prepared, "error": prepared.error},
        )

    @staticmethod
    def local_recheck_requires_agent(value: Any) -> bool:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        return bool(
            context.local_module_context is not None
            and module_recheck_requires_agent(context.local_module_context)
        )

    def accept_local_module_recheck(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        if not isinstance(value, Mapping):
            raise TypeError("Cross local recheck acceptance requires context and result")
        context = _model(value.get("context"), DeclarativeCrossOwnerRuntimeContext)
        lane = self._require_local_module_context(context)
        accepted = accept_current_module_recheck(
            {"context": lane, "result": value.get("result")},
            store=self.store,
        )
        review = accepted.review
        local_review = review.acceptance if review is not None else None
        preparation = context.local_review_preparation
        if local_review is None or preparation is None:
            raise ValueError("Cross local recheck result was not accepted")
        acceptance = CrossOwnerLocalReviewAcceptance(
            run_id=preparation.run_id,
            workflow_id=preparation.workflow_id,
            owner_module_id=preparation.owner_module_id,
            review_round=preparation.review_round,
            owner_input_ref=preparation.owner_input_ref,
            reviewed_baseline=preparation.reviewed_baseline,
            cross_responses=preparation.cross_responses,
            regression_context=cast(
                ModuleLocalRegressionContext,
                preparation.regression_context,
            ),
            review=local_review,
        )
        return context.model_copy(
            deep=True,
            update={
                "status": "local_review_accepted",
                "local_review_acceptance": acceptance,
                "local_module_context": accepted,
                "error": accepted.error,
            },
        )

    @staticmethod
    def _require_local_module_context(
        context: DeclarativeCrossOwnerRuntimeContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if context.local_module_context is None:
            raise ValueError("Cross owner local module lifecycle is not prepared")
        return context.local_module_context

    async def prepare_recheck(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Prepare or recover the original Cross owner reviewer recheck."""

        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        initial = context.acceptance
        initial_preparation = context.preparation
        revision = context.revision_acceptance
        local = context.local_review_acceptance
        if (
            initial is None
            or initial_preparation is None
            or revision is None
            or local is None
        ):
            raise ValueError("Cross owner recheck requires accepted revision and local review")
        if local.review.next_action != "completed" or local.review.completion_ref is None:
            raise ValueError("Cross owner local review still requires its module revision loop")

        required_findings = (
            list(context.round_progress.pending)
            if context.round_progress is not None
            else list(revision.findings)
        )
        lane = build_cross_owner_lane_completion(
            workspace=self.workspace,
            store=self.store,
            run_id=revision.run_id,
            review_round=revision.review_round,
            owner_module_id=revision.owner_module_id,
            owner_input_ref=revision.owner_input_ref,
            revised=local.review.current,
            cross_responses=local.cross_responses,
            local_review_completion_ref=local.review.completion_ref,
            findings=required_findings,
        )
        subject_ref = lane.completion.subject
        owner_subject_ref = (
            subject_ref.ref if hasattr(subject_ref, "ref") else str(subject_ref)
        )
        initial_input = initial_preparation.owner_input
        if context.initial_batches:
            # Recheck only the dossier batches whose findings are being checked.
            needed = {
                context.initial_finding_batches.get(str(getattr(finding, "id", "")))
                for finding in required_findings
            }
            indices = (
                range(len(context.initial_batches)) if None in needed or not needed
                else sorted(cast(set[int], needed))
            )
            initial_input = _merge_owner_inputs(
                [context.initial_batches[index].owner_input for index in indices]
            ).model_copy(update={"input_changes": initial_input.input_changes})
        recheck_scope = (
            set(initial_input.owner_scope_submodule_ids) if initial_input.joint_dossiers else None
        )
        recheck_view = module_content_view(lane.module, recheck_scope)
        frozen_input = initial_input.model_copy(
            deep=True,
            update={
                "phase": "recheck",
                "review_round": revision.review_round,
                "owner_subject_ref": owner_subject_ref,
                "owner_subject_revision": lane.module.revision,
                "owner_subject": recheck_view,
                "owner_scope_submodule_ids": list(recheck_view.submodule_narratives),
                "required_findings": required_findings,
                "revision_responses": list(lane.responses),
                "prior_synthesis_inputs": list(initial.result.synthesis_inputs),
                "local_regression_review_ref": lane.local_review_ref,
            },
        )
        owner_input_ref = (
            f"Work/runs/{revision.run_id}/reviews/"
            f"cross-owner-input-r{revision.review_round}-{revision.owner_module_id}.json"
        )
        self.store.write_json(owner_input_ref, frozen_input.model_dump(mode="json"))

        def read_result(ref: str) -> CrossOwnerVerdictSubmission | None:
            path = self.workspace / ref
            return (
                CrossOwnerVerdictSubmission.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                if path.is_file()
                else None
            )

        preparation = prepare_cross_owner_recheck(
            workflow_id=revision.workflow_id,
            initial=initial,
            lane=lane,
            frozen_owner_input=frozen_input,
            owner_input_ref=owner_input_ref,
            review_round=revision.review_round,
            required_findings=required_findings,
            read_result=read_result,
        )
        if preparation.mode == "continue_existing":
            acceptance = accept_cross_owner_recheck(
                preparation=preparation,
                result=None,
            )
            return context.model_copy(
                update={
                    "status": "recheck_resumed",
                    "recheck_preparation": preparation,
                    "recheck_acceptance": acceptance,
                    "error": None,
                }
            )
        return context.model_copy(
            update={
                "status": "recheck_ready",
                "recheck_preparation": preparation,
                "recheck_acceptance": None,
                "error": None,
            }
        )

    @staticmethod
    def recheck_requires_agent(value: Any) -> bool:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        return bool(
            context.status == "recheck_ready"
            and context.recheck_preparation is not None
            and context.recheck_preparation.mode == "invoke_agent"
        )

    def accept_recheck(self, value: Any) -> DeclarativeCrossOwnerRuntimeContext:
        """Accept a fresh typed verdict or its same-run persisted result."""

        if not isinstance(value, Mapping):
            raise TypeError("Cross owner recheck acceptance requires context and result")
        context = _model(value.get("context"), DeclarativeCrossOwnerRuntimeContext)
        preparation = context.recheck_preparation
        if preparation is None:
            raise ValueError("Cross owner recheck acceptance has no preparation")
        if preparation.mode == "continue_existing":
            acceptance = accept_cross_owner_recheck(
                preparation=preparation,
                result=None,
            )
        else:
            result = _model(
                value.get("result"),
                DeclarativeCrossOwnerRecheckAgentResult,
            )
            if result.status != "completed" or result.submission is None:
                return context.model_copy(
                    update={
                        "status": "failed",
                        "error": result.error or "Cross owner recheck Agent failed",
                    }
                )

            def write_result(ref: str, submission: CrossOwnerVerdictSubmission) -> str:
                self.store.write_json(ref, submission.model_dump(mode="json"))
                return ref

            acceptance = accept_cross_owner_recheck(
                preparation=preparation,
                result=result.submission,
                write_immutable=write_result,
            )
        return context.model_copy(
            update={
                "status": "recheck_accepted",
                "recheck_acceptance": acceptance,
                "error": None,
            }
        )

    async def advance_round(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Advance the typed pending/resolved owner state after one recheck."""

        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        initial = context.acceptance
        acceptance = context.recheck_acceptance
        if context.status == "failed" or initial is None or acceptance is None:
            return context

        def write_result(ref: str, submission: Any) -> str:
            self.store.write_json(
                ref,
                submission.model_dump(mode="json")
                if hasattr(submission, "model_dump")
                else submission,
            )
            return ref

        progress = await advance_cross_owner_round(
            workflow_id=initial.workflow_id,
            initial_input_ref=initial.owner_input_ref,
            initial_result_ref=initial.result_ref,
            initial_result=initial.result,
            acceptance=acceptance,
            previous=context.round_progress,
            main_decision=(
                context.main_acceptance
                if context.main_acceptance is not None
                and context.main_acceptance.trigger == "reviewer_escalation"
                else None
            ),
            write_immutable=write_result,
        )
        return context.model_copy(
            update={
                "status": (
                    "round_revision_pending"
                    if progress.next_action == "revise"
                    else "round_completed"
                ),
                "round_progress": progress,
                "error": None,
            }
        )

    @staticmethod
    def round_needs_revision(value: Any) -> bool:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        return context.status == "round_revision_pending"

    async def complete_owner_round(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerPipelineOutcome:
        """Promote one fully resolved owner round into its typed pipeline output."""

        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        progress = context.round_progress
        if context.status != "round_completed" or progress is None:
            return DeclarativeCrossOwnerPipelineOutcome(
                owner_module_id=context.owner_module_id,
                status="failed",
                error=context.error or "Cross owner round is not complete",
            )
        lane = promote_cross_owner_pipeline_completion(
            workspace=self.workspace,
            store=self.store,
            lane=progress.lane,
            initial_result_ref=progress.initial_result_ref,
            verdict_ref=progress.verdict_ref,
        )
        pipeline = _CrossOwnerPipelineResult(
            owner_module_id=progress.owner_module_id,
            initial_input_ref=progress.initial_input_ref,
            initial_result_ref=progress.initial_result_ref,
            initial_result=progress.initial_result,
            lane=lane,
            verdict_ref=progress.verdict_ref,
            verdict=progress.verdict,
            finding_refs=progress.finding_refs,
            verdict_refs=progress.verdict_refs,
            review_exception_refs=context.review_exception_refs,
            findings=progress.findings,
            verdicts=progress.verdicts,
        ).model_dump(mode="json")
        return DeclarativeCrossOwnerPipelineOutcome(
            owner_module_id=context.owner_module_id,
            status="completed",
            pipeline=pipeline,
        )

    def complete_owner_without_findings(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerPipelineOutcome:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        acceptance = context.acceptance
        # A persisted initial result (same-run resume) carries its acceptance.
        if context.status not in {"initial_accepted", "initial_resumed"} or acceptance is None:
            raise ValueError("Cross owner pipeline cannot complete before initial acceptance")
        if any(f.impact == "blocking" for f in acceptance.result.findings):
            raise ValueError("Cross owner findings require the revision/recheck lifecycle")
        review_round = context.preparation.review_round if context.preparation else 0
        completion_ref = (
            f"Work/runs/{acceptance.run_id}/reviews/"
            f"cross-owner-completion-r{review_round}-{acceptance.owner_module_id}.json"
        )
        completion = ReviewCompletionRecord(
            review_protocol_version=3,
            lifecycle="cross",
            run_id=acceptance.run_id,
            reviewer_agent_id="cross-module-reviewer",
            reviewer_session_key=acceptance.reviewer_session_key,
            subject_refs=[
                context.preparation.owner_input.owner_subject_ref
                if context.preparation is not None
                else acceptance.owner_input_ref
            ],
            finding_refs=[acceptance.result_ref] if acceptance.result.findings else [],
            verdict_refs=[],
            resolved_finding_ids=[],
            deferred_advisory_ids=[f.id for f in acceptance.result.findings],
        )
        self.store.write_json(completion_ref, completion.model_dump(mode="json"))
        pipeline = {
            "owner_module_id": acceptance.owner_module_id,
            "initial_input_ref": acceptance.owner_input_ref,
            "initial_result_ref": acceptance.result_ref,
            "initial_result": acceptance.result.model_dump(mode="json"),
            "completion_ref": completion_ref,
            "completion": completion.model_dump(mode="json"),
            "findings": [f.model_dump(mode="json") for f in acceptance.result.findings],
            "synthesis_inputs": [
                item.model_dump(mode="json")
                for item in acceptance.result.synthesis_inputs
            ],
        }
        return DeclarativeCrossOwnerPipelineOutcome(
            owner_module_id=acceptance.owner_module_id,
            status="completed",
            pipeline=pipeline,
        )

    def reduce(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise TypeError("Cross owner reduction requires state and outcomes")
        state = _state_from(value.get("state", value))
        outcomes = value.get("outcomes")
        if not isinstance(outcomes, Mapping):
            raise ValueError("Cross owner reduction requires all five branch outcomes")
        parsed = {
            str(owner): _model(outcome, DeclarativeCrossOwnerPipelineOutcome)
            for owner, outcome in outcomes.items()
        }
        if set(parsed) != set(REPORT_MODULE_IDS):
            raise ValueError("Cross owner reduction requires exactly five owner outcomes")
        pipelines: dict[str, dict[str, Any]] = {}
        finding_ids: list[str] = []
        findings_by_owner: dict[str, list[Any]] = {}
        synthesis_by_id: dict[str, CrossSynthesisInput] = {}
        verdicts = []
        subject_refs: dict[str, str] = {}
        review_exception_refs = state.setdefault("review_exception_refs", [])
        for owner_module_id, outcome in parsed.items():
            if outcome.status != "completed" or not outcome.pipeline:
                raise ValueError(
                    outcome.error or f"Cross owner lane {owner_module_id} did not complete"
                )
            result = _model(outcome.pipeline.get("initial_result"), CrossOwnerFindingSubmission)
            if result.owner_module_id != owner_module_id:
                raise ValueError("Cross owner outcome owner/result mismatch")
            if any(f.impact == "blocking" for f in result.findings) or outcome.pipeline.get("lane") is not None:
                completed = _model(outcome.pipeline, _CrossOwnerPipelineResult)
                state.setdefault("module_submissions", {})[owner_module_id] = (
                    completed.lane.module
                )
                state.setdefault("specialist_submissions", {})[owner_module_id] = (
                    completed.lane.module
                )
                state.setdefault("module_review_completion_refs", {})[owner_module_id] = (
                    completed.lane.local_review_ref
                )
                subject = completed.lane.completion.subject
                subject_refs[owner_module_id] = (
                    subject.ref
                    if hasattr(subject, "ref")
                    else f"Work/runs/{state['run_id']}/modules/"
                    f"{owner_module_id}-r{completed.lane.module.revision}.json"
                )
                state.setdefault("module_subject_refs", {})[owner_module_id] = (
                    subject_refs[owner_module_id]
                )
                verdicts.extend(completed.verdicts)
                review_exception_refs.extend(completed.review_exception_refs)
                finding_ids.extend(finding.id for finding in completed.findings)
                findings_by_owner[owner_module_id] = list(completed.findings)
            else:
                metadata = _metadata_map(state)
                subject_refs[owner_module_id] = _module_subject_ref(
                    state,
                    metadata,
                    _model(state["module_submissions"][owner_module_id], ModuleSubmission),
                )
                finding_ids.extend(finding.id for finding in result.findings)
                findings_by_owner[owner_module_id] = list(result.findings)
            for item in result.synthesis_inputs:
                existing = synthesis_by_id.get(item.id)
                if existing is not None and existing != item:
                    raise ValueError(
                        "Cross owner synthesis reused an id with different content"
                    )
                synthesis_by_id[item.id] = item
            pipelines[owner_module_id] = outcome.pipeline

        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("Cross owner findings reused an id")

        run_id = str(state["run_id"])
        cross_findings = CrossReviewFindingSubmission(
            coverage=[
                CrossReviewCoverageEntry(
                    module_id=owner,
                    checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
                )
                for owner in REPORT_MODULE_IDS
            ],
            findings=[
                finding
                for owner in REPORT_MODULE_IDS
                for finding in findings_by_owner[owner]
            ],
            synthesis_inputs=list(synthesis_by_id.values()),
        )
        finding_ref = f"Work/runs/{run_id}/reviews/cross-findings-r0.json"
        self.store.write_json(finding_ref, cross_findings.model_dump(mode="json"))
        cross_verdicts = CrossReviewVerdictSubmission(
            coverage=[
                CrossReviewCoverageEntry(
                    module_id=owner,
                    checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
                )
                for owner in REPORT_MODULE_IDS
            ],
            verdicts=verdicts,
            new_findings=[],
            synthesis_inputs=list(synthesis_by_id.values()),
        )
        verdict_ref = f"Work/runs/{run_id}/reviews/cross-verdicts-r1.json"
        self.store.write_json(verdict_ref, cross_verdicts.model_dump(mode="json"))
        completion_ref = f"Work/runs/{run_id}/reviews/cross-completion.json"
        completion = ReviewCompletionRecord(
            review_protocol_version=3,
            lifecycle="cross",
            run_id=run_id,
            reviewer_agent_id="cross-module-reviewer",
            reviewer_session_key="cross-owner-wave",
            subject_refs=[subject_refs[module_id] for module_id in REPORT_MODULE_IDS],
            finding_refs=[finding_ref],
            verdict_refs=[verdict_ref],
            resolved_finding_ids=sorted(item.finding_id for item in verdicts),
            deferred_advisory_ids=sorted(
                f.id for f in cross_findings.findings
                if f.impact == "advisory"
                and f.id not in {v.finding_id for v in verdicts}
            ),
        )
        self.store.write_json(completion_ref, completion.model_dump(mode="json"))
        pack = CrossDecisionPack(
            version=2,
            run_id=run_id,
            module_ids=list(REPORT_MODULE_IDS),
            cross_review_completion_ref=completion_ref,
            synthesis_inputs=cross_findings.synthesis_inputs,
            deferred_advisories=[
                f for f in cross_findings.findings
                if f.id in completion.deferred_advisory_ids
            ],
        )
        pack_ref = f"Work/runs/{run_id}/reviews/cross-decision-pack.json"
        self.store.write_json(pack_ref, pack.model_dump(mode="json"))
        state.update(
            {
                "cross_owner_outcomes": pipelines,
                "cross_review_completion_ref": completion_ref,
                "cross_decision_pack_ref": pack_ref,
                "cross_synthesis_inputs": cross_findings.synthesis_inputs,
            }
        )
        return state


CapabilityCrossOwnerRuntime = CrossOwnerRuntime


__all__ = [
    "CrossOwnerAgentInvoker",
    "CrossOwnerRuntime",
    "CapabilityCrossOwnerRuntime",
]
