"""Capability-owned initial Cross-owner review runtime.

The Cross tail has five independent owner lanes.  This module owns the typed
boundary that prepares those lanes, invokes the shared Cross reviewer through
the neutral Agent runtime, accepts a typed initial submission, and reduces the
no-finding path into the current-run Cross completion and decision pack.

The runtime deliberately consumes module artifact metadata supplied by the
upstream module cohort.  ``CrossOwnerInput`` already requires relation
``sha256`` fields for the Provider contract; this module does not calculate a
new digest or recreate the legacy recovery/CAS machinery when that metadata is
absent.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from manyselves.capabilities.distribution_reporting.domain.cross_specialization import (
    cross_lane_specialization,
)
from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.agent_result_payload import (
    load_agent_result_payload,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_local_regression import (
    build_cross_owner_local_regression_context,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CROSS_REVIEW_DIMENSIONS,
    CrossDecisionPack,
    CrossOwnerFindingSubmission,
    CrossReviewCoverageEntry,
    CrossReviewFindingSubmission,
    CrossSynthesisInput,
    ModuleReviewFindingSubmission,
    ModuleSubmission,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import (
    DeclarativeCrossOwnerInitialAgentResult,
    DeclarativeCrossOwnerPipelineOutcome,
    DeclarativeCrossOwnerRuntimeContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    CrossOwnerInput,
    CrossOwnerRelatedModuleView,
    ModuleContentView,
    ReviewCompletionRecord,
    module_content_view,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleReviewAgentResult,
    DeclarativeModuleRevisionAgentResult,
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
    ModuleLocalRegressionContext,
    ModuleRevisionPreparation,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_acceptance import (
    accept_module_initial_review,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_preparation import (
    prepare_module_local_regression_review,
)
from manyselves.capabilities.distribution_reporting.runtime.module_revision_tools import (
    accept_module_revision,
    load_module_revision_candidate,
    prepare_module_revision,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.capabilities.distribution_reporting.runtime.user_supplements import (
    request_user_supplements,
)
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

REPORT_MODULE_IDS = tuple(REPORT_TAXONOMY)
SessionFactory = Callable[[str], AgentSessionLoop]


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
        "module_artifact_refs",
        "module_artifacts",
        "module_subject_refs",
        "module_refs",
    ):
        candidate = state.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    return {}


def _metadata_entry(metadata: Mapping[str, Any], module_id: str) -> tuple[Any, Any]:
    entry = metadata.get(module_id, {})
    if isinstance(entry, Mapping):
        ref = entry.get("ref") or entry.get("subject_ref") or entry.get("path")
        digest = entry.get("sha256") or entry.get("subject_sha256")
    else:
        ref = getattr(entry, "ref", None) or getattr(entry, "subject_ref", None)
        digest = getattr(entry, "sha256", None) or getattr(entry, "subject_sha256", None)
        if ref is None and isinstance(entry, str):
            ref = entry
    return ref, digest


def _related_view(
    module: ModuleSubmission,
    *,
    subject_ref: str,
    subject_sha256: str,
) -> CrossOwnerRelatedModuleView:
    content = module_content_view(module)
    return CrossOwnerRelatedModuleView(
        module_id=module.module_id,
        revision=module.revision,
        subject_ref=subject_ref,
        subject_sha256=subject_sha256,
        submodule_ids=list(content.submodule_narratives),
        claims=module.claims,
        evidence_ids_by_submodule=content.evidence_ids_by_submodule,
        unresolved_questions=content.unresolved_questions,
    )


def _build_owner_input(
    *,
    state: Mapping[str, Any],
    modules: Mapping[str, ModuleSubmission],
    metadata: Mapping[str, Any],
    owner_module_id: str,
    review_round: int,
) -> tuple[CrossOwnerInput, str]:
    owner = modules[owner_module_id]
    owner_ref, _owner_digest = _metadata_entry(metadata, owner_module_id)
    related_refs: dict[str, str] = {}
    related_revisions: dict[str, int] = {}
    related_hashes: dict[str, str] = {}
    related_views: dict[str, CrossOwnerRelatedModuleView] = {}
    for module_id in REPORT_MODULE_IDS:
        if module_id == owner_module_id:
            continue
        related = modules[module_id]
        related_ref, related_digest = _metadata_entry(metadata, module_id)
        related_refs[module_id] = related_ref
        related_revisions[module_id] = related.revision
        related_hashes[module_id] = related_digest
        related_views[module_id] = _related_view(
            related,
            subject_ref=related_ref,
            subject_sha256=related_digest,
        )

    owner_view: ModuleContentView = module_content_view(owner)
    contract = CrossOwnerInput(
        phase="initial",
        run_id=str(state["run_id"]),
        review_round=review_round,
        owner_module_id=owner_module_id,
        review_focus=list(cross_lane_specialization(owner_module_id).review_focus),
        owner_subject_ref=owner_ref,
        owner_subject_revision=owner.revision,
        owner_subject=owner_view,
        owner_scope_submodule_ids=list(owner_view.submodule_narratives),
        related_module_refs=related_refs,
        related_module_revisions=related_revisions,
        related_module_sha256=related_hashes,
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
) -> TaskEnvelope:
    specialization = cross_lane_specialization(owner_module_id)
    input_ref = (
        f"Work/runs/{state['run_id']}/reviews/"
        f"cross-owner-input-r{review_round}-{owner_module_id}.json"
    )
    return TaskEnvelope(
        task_id=f"cross-owner-{owner_module_id}-r{review_round}-initial",
        run_id=str(state["run_id"]),
        agent_id="cross-module-reviewer",
        objective=(
            f"以 Cross owner {owner_module_id} 身份审查本模块与其他四模块的关系；"
            "只创建归属本模块的 finding，并且 related 模块只读。"
        ),
        input_refs=[input_ref],
        constraints=[
            f"唯一 Cross owner 写作范围是模块 {owner_module_id}",
            "related_module_views 是紧凑只读关系视图，不得修改或创建其他模块 finding",
            "initial 只提交 cross_owner_finding_submission",
            "coverage 仅证明当前 owner 检查过六个维度，不代表 approved",
        ],
        allowed_outputs=["cross_owner_finding_submission"],
        allowed_tools=["submit_result"],
        revision=review_round,
        artifact_delivery_modes={input_ref: "inline"},
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
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.execution = execution
        self.session_factory = session_factory
        self.workflow_id = workflow_id
        self.terminal_task_attempt_id = terminal_task_attempt_id

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
        """Keep the declared recovery port while this slice is initial-only."""

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
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        preparation = context.preparation
        if preparation is None or preparation.envelope is None:
            return AgentInvocationOutcome(
                status="failed",
                session_id=conversation.external_session_id,
                error="Cross owner initial preparation has no TaskEnvelope",
            )
        workflow_id = preparation.workflow_id or self.workflow_id
        run_id = preparation.run_id
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
            content=self._prompt(agent, task, preparation),
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
        outcome = await typed_turn.dispatch(session, request, terminals=(terminal,))
        return TypedAgentTurn.map_outcome(
            outcome,
            session_id=session.session_id,
            decode_result=lambda result_ref: self._decode_result(
                result_ref,
                output_contract=task.output_contract,
            ),
        )

    @staticmethod
    def _prompt(
        agent: AgentDefinition,
        task: TaskDefinition,
        preparation: CrossOwnerInitialReviewPreparation,
    ) -> str:
        envelope = preparation.envelope
        sections = [
            agent.instructions,
            f"Task: {task.objective}",
            json.dumps(
                preparation.owner_input.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            f"Allowed tools: {json.dumps(task.tools, ensure_ascii=False)}",
            f"Output contract: {task.output_contract}",
        ]
        if envelope is not None and envelope.inline_context:
            sections.append(f"Cross owner specialization:\n{envelope.inline_context}")
        return "\n\n".join(sections)

    def _decode_result(self, result_ref: str, *, output_contract: str) -> dict[str, Any]:
        if output_contract == "declarative_cross_owner_recheck_agent_result":
            raise ValueError(
                "Cross owner recheck bridge is not part of the initial composition slice"
            )
        loaded = load_agent_result_payload(self.workspace, result_ref)
        payload = loaded.payload
        submission = CrossOwnerFindingSubmission.model_validate(payload)
        return DeclarativeCrossOwnerInitialAgentResult(
            status="completed",
            submission=submission,
        ).model_dump(mode="json")


class CrossOwnerRuntime:
    """Compose Cross owner preparation, initial acceptance, and reduction."""

    def __init__(
        self,
        workspace: Path,
        *,
        agent_execution: AgentExecutionService | None = None,
        agent_session_factory: SessionFactory | None = None,
        workflow_id: str = "public-reporting",
        store: ReportingStore | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = store or ReportingStore(self.workspace)
        self.workflow_id = workflow_id
        self.agent_execution = agent_execution
        self.agent_session_factory = agent_session_factory
        if agent_execution is not None and callable(agent_session_factory):
            self.agent_invokers: Mapping[str, Any] = {
                "cross-module-reviewer": CrossOwnerAgentInvoker(
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
        modules = _module_submissions(state)
        metadata = _metadata_map(state)
        review_round = int(state.get("cross_review_round", 0))
        refs: dict[str, str] = {}
        inputs: dict[str, CrossOwnerInput] = {}
        for owner_module_id in REPORT_MODULE_IDS:
            contract, ref = _build_owner_input(
                state=state,
                modules=modules,
                metadata=metadata,
                owner_module_id=owner_module_id,
                review_round=review_round,
            )
            self.store.write_json(ref, contract.model_dump(mode="json"))
            refs[owner_module_id] = ref
            inputs[owner_module_id] = contract
        state["cross_owner_input_refs"] = refs
        state["cross_owner_inputs"] = inputs
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
        return DeclarativeCrossOwnerRuntimeContext(
            owner_module_id=owner_module_id,
            status="initial_ready",
            preparation=preparation,
        )

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

    @staticmethod
    def initial_has_findings(value: Any) -> bool:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        return bool(context.acceptance and context.acceptance.result.findings)

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
            findings = list(acceptance.result.findings)
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
            state={"run_id": acceptance.run_id},
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

    async def prepare_local_review(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Prepare the original module Auditor's scoped local regression."""

        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        revision = context.revision_acceptance
        if revision is None:
            raise ValueError("Cross owner local review requires an accepted revision")
        regression_context, local_scope = build_cross_owner_local_regression_context(
            workspace=self.workspace,
            store=self.store,
            run_id=revision.run_id,
            owner_module_id=revision.owner_module_id,
            reviewed_baseline=revision.current,
            revised=revision.revised,
            findings=list(revision.findings),
            review_round=revision.review_round,
            prior_completion_ref=revision.prior_completion_ref,
        )
        prepared = prepare_module_local_regression_review(
            store=self.store,
            workflow_id=revision.workflow_id,
            run_id=revision.run_id,
            current=revision.revised,
            scope=local_scope,
            review_round=revision.review_round,
            regression_context=regression_context,
            user_supplements=revision.user_supplements,
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
        return context.model_copy(
            update={
                "status": "local_review_ready",
                "local_review_preparation": preparation,
                "local_review_acceptance": None,
                "error": None,
            }
        )

    @staticmethod
    def local_review_requires_agent(value: Any) -> bool:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        preparation = context.local_review_preparation
        return bool(
            context.status == "local_review_ready"
            and preparation is not None
            and preparation.mode == "invoke_agent"
        )

    def accept_local_review(self, value: Any) -> DeclarativeCrossOwnerRuntimeContext:
        """Accept one typed original-Auditor local-regression result."""

        if not isinstance(value, Mapping):
            raise TypeError("Cross owner local review acceptance requires context and result")
        context = _model(value.get("context"), DeclarativeCrossOwnerRuntimeContext)
        preparation = context.local_review_preparation
        if preparation is None or preparation.prepared is None:
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
            submission = result.submission
        else:
            submission = _model(raw_result, ModuleReviewFindingSubmission)
        review = accept_module_initial_review(
            preparation=preparation.prepared,
            submission=submission,
            store=self.store,
        )
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
            update={
                "status": "local_review_accepted",
                "local_review_acceptance": acceptance,
                "error": None,
            }
        )

    def complete_owner_without_findings(
        self,
        value: Any,
    ) -> DeclarativeCrossOwnerPipelineOutcome:
        context = _model(value, DeclarativeCrossOwnerRuntimeContext)
        acceptance = context.acceptance
        if context.status != "initial_accepted" or acceptance is None:
            raise ValueError("Cross owner pipeline cannot complete before initial acceptance")
        if acceptance.result.findings:
            raise ValueError("Cross owner findings require the revision/recheck lifecycle")
        review_round = context.preparation.review_round if context.preparation else 0
        completion_ref = (
            f"Work/runs/{acceptance.run_id}/reviews/"
            f"cross-owner-completion-r{review_round}-{acceptance.owner_module_id}.json"
        )
        completion = ReviewCompletionRecord(
            lifecycle="cross",
            run_id=acceptance.run_id,
            reviewer_agent_id="cross-module-reviewer",
            reviewer_session_key=acceptance.reviewer_session_key,
            subject_refs=[
                context.preparation.owner_input.owner_subject_ref
                if context.preparation is not None
                else acceptance.owner_input_ref
            ],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
        )
        self.store.write_json(completion_ref, completion.model_dump(mode="json"))
        pipeline = {
            "owner_module_id": acceptance.owner_module_id,
            "initial_input_ref": acceptance.owner_input_ref,
            "initial_result_ref": acceptance.result_ref,
            "initial_result": acceptance.result.model_dump(mode="json"),
            "completion_ref": completion_ref,
            "completion": completion.model_dump(mode="json"),
            "findings": [],
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
        for owner_module_id, outcome in parsed.items():
            if outcome.status != "completed" or not outcome.pipeline:
                raise ValueError(
                    outcome.error or f"Cross owner lane {owner_module_id} did not complete"
                )
            result = _model(outcome.pipeline.get("initial_result"), CrossOwnerFindingSubmission)
            if result.owner_module_id != owner_module_id:
                raise ValueError("Cross owner outcome owner/result mismatch")
            if result.findings:
                raise ValueError(
                    "Cross owner reduction requires revision/recheck for findings before closure"
                )
            pipelines[owner_module_id] = outcome.pipeline

        metadata = _metadata_map(state)
        subject_refs = {
            owner: _metadata_entry(metadata, owner)[0] for owner in REPORT_MODULE_IDS
        }
        run_id = str(state["run_id"])
        cross_findings = CrossReviewFindingSubmission(
            coverage=[
                CrossReviewCoverageEntry(
                    module_id=owner,
                    checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
                )
                for owner in REPORT_MODULE_IDS
            ],
            findings=[],
            synthesis_inputs=[
                CrossSynthesisInput.model_validate(item)
                for pipeline in pipelines.values()
                for item in pipeline.get("synthesis_inputs", [])
            ],
        )
        finding_ref = f"Work/runs/{run_id}/reviews/cross-findings-r0.json"
        self.store.write_json(finding_ref, cross_findings.model_dump(mode="json"))
        completion_ref = f"Work/runs/{run_id}/reviews/cross-completion.json"
        completion = ReviewCompletionRecord(
            lifecycle="cross",
            run_id=run_id,
            reviewer_agent_id="cross-module-reviewer",
            reviewer_session_key="cross-owner-wave",
            subject_refs=[subject_refs[module_id] for module_id in REPORT_MODULE_IDS],
            finding_refs=[finding_ref],
            verdict_refs=[],
            resolved_finding_ids=[],
        )
        self.store.write_json(completion_ref, completion.model_dump(mode="json"))
        pack = CrossDecisionPack(
            run_id=run_id,
            module_ids=list(REPORT_MODULE_IDS),
            cross_review_completion_ref=completion_ref,
            synthesis_inputs=cross_findings.synthesis_inputs,
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
