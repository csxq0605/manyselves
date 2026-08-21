"""Reporting-owned declarations for the five Cross owner pipelines."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
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
    CrossOwnerFindingSubmission,
    CrossOwnerVerdictSubmission,
    ModuleReviewFindingSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
    TaskEnvelope,
)
from .declarative_module_runtime_lane import (
    DeclarativeModuleReviewAgentResult,
    DeclarativeModuleRevisionAgentResult,
)
from .models import REPORT_MODULE_IDS
from .review_lifecycle import (
    CrossOwnerInitialReviewAcceptance,
    CrossOwnerInitialReviewPreparation,
    CrossOwnerLocalReviewAcceptance,
    CrossOwnerLocalReviewPreparation,
    CrossOwnerRecheckAcceptance,
    CrossOwnerRecheckPreparation,
    CrossOwnerRevisionAcceptance,
    CrossOwnerRevisionPreparation,
    CrossOwnerRoundProgress,
    CrossReviewCoordinator,
    ModuleInitialReviewPreparation,
    ModuleRevisionPreparation,
)


class DeclarativeCrossOwnerPipelineOutcome(BaseModel):
    """Serializable result retained after one Cross owner branch drains."""

    model_config = ConfigDict(extra="forbid")

    owner_module_id: str
    status: Literal["completed", "failed"]
    pipeline: dict[str, Any] | None = None
    error: str | None = None


class DeclarativeCrossOwnerInitialAgentResult(BaseModel):
    """Typed result returned by the declared initial Cross reviewer."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: CrossOwnerFindingSubmission | None = None
    error: str | None = None


class DeclarativeCrossOwnerRecheckAgentResult(BaseModel):
    """Typed result returned by the original Cross owner reviewer."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: CrossOwnerVerdictSubmission | None = None
    error: str | None = None


class DeclarativeCrossOwnerRuntimeContext(BaseModel):
    """Capability-owned state threaded through one Cross owner workflow."""

    model_config = ConfigDict(extra="forbid")

    owner_module_id: str
    status: Literal[
        "initial_ready",
        "initial_resumed",
        "initial_accepted",
        "revision_ready",
        "revision_resumed",
        "revision_accepted",
        "local_review_ready",
        "local_review_resumed",
        "local_review_accepted",
        "recheck_ready",
        "recheck_resumed",
        "recheck_accepted",
        "round_revision_pending",
        "round_completed",
        "failed",
    ]
    preparation: CrossOwnerInitialReviewPreparation | None = None
    acceptance: CrossOwnerInitialReviewAcceptance | None = None
    revision_preparation: CrossOwnerRevisionPreparation | None = None
    revision_acceptance: CrossOwnerRevisionAcceptance | None = None
    local_review_preparation: CrossOwnerLocalReviewPreparation | None = None
    local_review_acceptance: CrossOwnerLocalReviewAcceptance | None = None
    recheck_preparation: CrossOwnerRecheckPreparation | None = None
    recheck_acceptance: CrossOwnerRecheckAcceptance | None = None
    round_progress: CrossOwnerRoundProgress | None = None
    error: str | None = None


class _CrossOwnerInitialInvoker:
    """Invoke one prepared Cross owner through the generic Agent port."""

    def __init__(self, runtime: "DeclarativeCrossOwnerRuntime") -> None:
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
        context = DeclarativeCrossOwnerRuntimeContext.model_validate(value)
        preparation = cast(CrossOwnerInitialReviewPreparation, context.preparation)
        envelope = cast(TaskEnvelope, preparation.envelope)
        try:
            payload = await self._runtime._current_runner._agent(
                "cross-module-reviewer",
                envelope,
                envelope.input_refs,
                preparation.workflow_id,
                session_key=conversation.key.value,
            )
            result = DeclarativeCrossOwnerInitialAgentResult(
                status="completed",
                submission=payload,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            result = DeclarativeCrossOwnerInitialAgentResult(
                status="failed",
                error=str(exc),
            )
        return AgentInvocationOutcome(status="ok", result=result)


class _CrossOwnerRevisionInvoker:
    """Invoke the original module Author for one prepared Cross revision."""

    def __init__(self, runtime: "DeclarativeCrossOwnerRuntime") -> None:
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
        context = DeclarativeCrossOwnerRuntimeContext.model_validate(value)
        preparation = cast(
            CrossOwnerRevisionPreparation,
            context.revision_preparation,
        )
        prepared = cast(ModuleRevisionPreparation, preparation.prepared)
        try:
            payload = await self._runtime._current_runner._agent(
                prepared.specialist_id,
                prepared.envelope,
                prepared.envelope.input_refs,
                preparation.workflow_id,
                session_key=conversation.key.value,
            )
            result = DeclarativeModuleRevisionAgentResult(
                status="completed",
                submission=payload,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            result = DeclarativeModuleRevisionAgentResult(
                status="failed",
                error=str(exc),
            )
        return AgentInvocationOutcome(status="ok", result=result)


class _CrossOwnerLocalReviewInvoker:
    """Invoke the original module Auditor for one Cross local regression."""

    def __init__(self, runtime: "DeclarativeCrossOwnerRuntime") -> None:
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
        context = DeclarativeCrossOwnerRuntimeContext.model_validate(value)
        boundary = cast(
            CrossOwnerLocalReviewPreparation,
            context.local_review_preparation,
        )
        prepared = cast(ModuleInitialReviewPreparation, boundary.prepared)
        envelope = cast(TaskEnvelope, prepared.envelope)
        try:
            payload = await self._runtime._current_runner._agent(
                "evidence-auditor",
                envelope,
                envelope.input_refs,
                boundary.workflow_id,
                session_key=conversation.key.value,
            )
            result = DeclarativeModuleReviewAgentResult(
                status="completed",
                submission=payload,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            result = DeclarativeModuleReviewAgentResult(
                status="failed",
                error=str(exc),
            )
        return AgentInvocationOutcome(status="ok", result=result)


class _CrossOwnerRecheckInvoker:
    """Invoke the original Cross owner reviewer for its first recheck."""

    def __init__(self, runtime: "DeclarativeCrossOwnerRuntime") -> None:
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
        context = DeclarativeCrossOwnerRuntimeContext.model_validate(value)
        preparation = cast(
            CrossOwnerRecheckPreparation,
            context.recheck_preparation,
        )
        envelope = cast(TaskEnvelope, preparation.envelope)
        try:
            payload = await self._runtime._current_runner._agent(
                "cross-module-reviewer",
                envelope,
                envelope.input_refs,
                preparation.workflow_id,
                session_key=conversation.key.value,
            )
            result = DeclarativeCrossOwnerRecheckAgentResult(
                status="completed",
                submission=payload,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            result = DeclarativeCrossOwnerRecheckAgentResult(
                status="failed",
                error=str(exc),
            )
        return AgentInvocationOutcome(status="ok", result=result)


class _CrossOwnerReviewerInvoker:
    """Route the shared Cross reviewer identity by its declared Task."""

    def __init__(self, runtime: "DeclarativeCrossOwnerRuntime") -> None:
        self._routes: Mapping[str, AgentInvoker] = {
            "cross-owner-runtime-initial-review": _CrossOwnerInitialInvoker(runtime),
            "cross-owner-runtime-recheck": _CrossOwnerRecheckInvoker(runtime),
        }

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        return await self._routes[task.id].invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
        )


def register_cross_owner_pipeline_specializations(
    definitions: DefinitionRegistry,
) -> dict[str, WorkflowDefinition]:
    """Register five statically bound owner-pipeline workflow definitions."""

    template = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-pipeline",
    )
    if not isinstance(template, WorkflowDefinition):
        raise TypeError("distribution-cross-owner-pipeline is not a workflow")
    workflows: dict[str, WorkflowDefinition] = {}
    for module_id in REPORT_MODULE_IDS:
        workflow_id = f"distribution-cross-owner-{module_id}-pipeline"
        registered = definitions.get(DefinitionKind.WORKFLOW, workflow_id)
        if registered is not None:
            if not isinstance(registered, WorkflowDefinition):
                raise TypeError(f"{workflow_id} is not a workflow")
            workflows[workflow_id] = registered
            continue
        workflow = specialize_workflow(
            template,
            {
                "owner_module_id": module_id,
                "conversation_key": f"cross-owner-{module_id}",
                "revision_agent_id": f"module-{module_id}-specialist",
                "revision_task_id": f"cross-owner-module-{module_id}-revision-r1",
                "revision_conversation_key": f"module-{module_id}",
                "local_review_conversation_key": f"module-auditor-{module_id}",
            },
            workflow_id=workflow_id,
        )
        definitions.register(workflow)
        workflows[workflow_id] = workflow
    return workflows


def compile_cross_owner_workflows(
    definitions: DefinitionRegistry,
    executors: ExecutorRegistry,
) -> tuple[ResolvedPlan, dict[str, ResolvedPlan]]:
    """Compile the packaged Cross Cohort and its five owner pipelines."""

    cohort = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-cohort",
    )
    if not isinstance(cohort, WorkflowDefinition):
        raise TypeError("distribution-cross-owner-cohort is not a workflow")
    pipelines = {
        workflow_id: WorkflowCompiler(executors).compile(workflow, definitions)
        for workflow_id, workflow in register_cross_owner_pipeline_specializations(
            definitions
        ).items()
    }
    return WorkflowCompiler(executors).compile(cohort, definitions), pipelines


class DeclarativeCrossOwnerRuntime:
    """Bind file-defined Cross actions to the existing Capability lifecycle."""

    def __init__(
        self,
        runner: Any,
        state: dict[str, Any],
        workflow_id: str,
        *,
        compatibility_cross: Any | None = None,
    ) -> None:
        self.current_state = state
        self._runner = runner
        self._workflow_id = workflow_id
        self._compatibility_cross = compatibility_cross
        self._current_runner = getattr(runner, "_runner", runner)
        self._production = hasattr(self._current_runner, "service") and callable(
            getattr(self._current_runner, "_agent", None)
        )
        self._coordinator = (
            CrossReviewCoordinator(
                self._current_runner,
                state,
                workflow_id,
            )
            if self._production
            else None
        )
        self._aggregate_recovered = False
        self._compatibility_invoked = False
        reviewer_invoker = _CrossOwnerReviewerInvoker(self)
        revision_invoker = _CrossOwnerRevisionInvoker(self)
        local_review_invoker = _CrossOwnerLocalReviewInvoker(self)
        self.agent_invokers: Mapping[str, AgentInvoker] = {
            "cross-module-reviewer": reviewer_invoker,
            "evidence-auditor": local_review_invoker,
            **{
                f"module-{module_id}-specialist": revision_invoker
                for module_id in REPORT_MODULE_IDS
            },
        }

    def prepare(self, state: dict[str, Any]) -> dict[str, Any]:
        """Prepare the frozen owner inputs before the declared Parallel."""

        self.current_state = state
        _restore_modules(self.current_state)
        if self._production:
            self._coordinator = CrossReviewCoordinator(
                self._current_runner,
                self.current_state,
                self._workflow_id,
            )
        if self._coordinator is not None:
            self._aggregate_recovered = self._coordinator.prepare()
        return deepcopy(self.current_state)

    def prepare_initial(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Prepare or recover one declared Cross owner initial Agent turn."""

        owner_module_id = str(values["owner_module_id"])
        if self._aggregate_recovered or self._coordinator is None:
            return DeclarativeCrossOwnerRuntimeContext(
                owner_module_id=owner_module_id,
                status="initial_resumed",
            )
        try:
            preparation = self._coordinator.prepare_owner_initial(owner_module_id)
        except BaseException as exc:
            return DeclarativeCrossOwnerRuntimeContext(
                owner_module_id=owner_module_id,
                status="failed",
                error=str(exc),
            )
        if preparation.mode == "invoke_agent":
            return DeclarativeCrossOwnerRuntimeContext(
                owner_module_id=owner_module_id,
                status="initial_ready",
                preparation=preparation,
            )
        acceptance = self._coordinator.accept_owner_initial(preparation)
        return DeclarativeCrossOwnerRuntimeContext(
            owner_module_id=owner_module_id,
            status="initial_resumed",
            preparation=preparation,
            acceptance=acceptance,
        )

    @staticmethod
    def initial_requires_agent(context: DeclarativeCrossOwnerRuntimeContext) -> bool:
        """Return the explicit file-workflow branch decision."""

        return context.status == "initial_ready"

    @staticmethod
    def initial_has_findings(context: DeclarativeCrossOwnerRuntimeContext) -> bool:
        """Route typed initial findings to the original owner Author."""

        return bool(context.acceptance and context.acceptance.result.findings)

    def accept_initial(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Accept one typed initial Agent result for the compatibility continuation."""

        context = DeclarativeCrossOwnerRuntimeContext.model_validate(values["context"])
        result = DeclarativeCrossOwnerInitialAgentResult.model_validate(values["result"])
        if result.status == "failed":
            return context.model_copy(update={"status": "failed", "error": result.error})
        try:
            acceptance = cast(CrossReviewCoordinator, self._coordinator).accept_owner_initial(
                cast(CrossOwnerInitialReviewPreparation, context.preparation),
                result.submission,
            )
        except BaseException as exc:
            return context.model_copy(update={"status": "failed", "error": str(exc)})
        return context.model_copy(update={"status": "initial_accepted", "acceptance": acceptance})

    async def prepare_revision(
        self,
        context: DeclarativeCrossOwnerRuntimeContext,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Prepare or recover the current finding-triggered owner revision."""

        context = DeclarativeCrossOwnerRuntimeContext.model_validate(context)
        try:
            preparation = await cast(
                CrossReviewCoordinator,
                self._coordinator,
            ).prepare_owner_revision(
                cast(CrossOwnerInitialReviewAcceptance, context.acceptance),
                (
                    context.round_progress
                    if context.status == "round_revision_pending"
                    else None
                ),
            )
        except BaseException as exc:
            return context.model_copy(update={"status": "failed", "error": str(exc)})
        if preparation.mode == "invoke_agent":
            return context.model_copy(
                update={
                    "status": "revision_ready",
                    "revision_preparation": preparation,
                }
            )
        acceptance = cast(
            CrossReviewCoordinator,
            self._coordinator,
        ).accept_owner_revision(preparation)
        return context.model_copy(
            update={
                "status": "revision_resumed",
                "revision_preparation": preparation,
                "revision_acceptance": acceptance,
            }
        )

    @staticmethod
    def revision_requires_agent(context: DeclarativeCrossOwnerRuntimeContext) -> bool:
        """Return the explicit revision Agent branch decision."""

        return context.status == "revision_ready"

    def accept_revision(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Accept one typed original-Author revision result."""

        context = DeclarativeCrossOwnerRuntimeContext.model_validate(values["context"])
        result = DeclarativeModuleRevisionAgentResult.model_validate(values["result"])
        if result.status == "failed":
            return context.model_copy(update={"status": "failed", "error": result.error})
        try:
            acceptance = cast(
                CrossReviewCoordinator,
                self._coordinator,
            ).accept_owner_revision(
                cast(CrossOwnerRevisionPreparation, context.revision_preparation),
                cast(ModuleRevisionSubmission, result.submission),
            )
        except BaseException as exc:
            return context.model_copy(update={"status": "failed", "error": str(exc)})
        return context.model_copy(
            update={
                "status": "revision_accepted",
                "revision_acceptance": acceptance,
            }
        )

    async def prepare_local_review(
        self,
        context: DeclarativeCrossOwnerRuntimeContext,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Prepare the original module Auditor local-regression turn."""

        context = DeclarativeCrossOwnerRuntimeContext.model_validate(context)
        if context.status == "failed":
            return context
        try:
            preparation = await cast(
                CrossReviewCoordinator,
                self._coordinator,
            ).prepare_owner_local_review(
                cast(CrossOwnerRevisionAcceptance, context.revision_acceptance)
            )
        except BaseException as exc:
            return context.model_copy(update={"status": "failed", "error": str(exc)})
        if preparation.existing_review is not None:
            acceptance = cast(
                CrossReviewCoordinator,
                self._coordinator,
            ).accept_owner_local_review(preparation)
            return context.model_copy(
                update={
                    "status": "local_review_accepted",
                    "local_review_preparation": preparation,
                    "local_review_acceptance": acceptance,
                }
            )
        return context.model_copy(
            update={
                "status": (
                    "local_review_ready"
                    if preparation.mode == "invoke_agent"
                    else "local_review_resumed"
                ),
                "local_review_preparation": preparation,
            }
        )

    @staticmethod
    def local_review_requires_agent(
        context: DeclarativeCrossOwnerRuntimeContext,
    ) -> bool:
        """Return the explicit original-Auditor branch decision."""

        return context.status == "local_review_ready"

    def accept_local_review(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Accept one typed original-Auditor local-regression result."""

        context = DeclarativeCrossOwnerRuntimeContext.model_validate(values["context"])
        result = DeclarativeModuleReviewAgentResult.model_validate(values["result"])
        if result.status == "failed":
            return context.model_copy(update={"status": "failed", "error": result.error})
        try:
            acceptance = cast(
                CrossReviewCoordinator,
                self._coordinator,
            ).accept_owner_local_review(
                cast(
                    CrossOwnerLocalReviewPreparation,
                    context.local_review_preparation,
                ),
                cast(ModuleReviewFindingSubmission, result.submission),
            )
        except BaseException as exc:
            return context.model_copy(update={"status": "failed", "error": str(exc)})
        return context.model_copy(
            update={
                "status": "local_review_accepted",
                "local_review_acceptance": acceptance,
            }
        )

    async def prepare_recheck(
        self,
        context: DeclarativeCrossOwnerRuntimeContext,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Prepare or recover the first original Cross owner recheck."""

        context = DeclarativeCrossOwnerRuntimeContext.model_validate(context)
        if context.status == "failed":
            return context
        if context.local_review_acceptance is None:
            return context
        try:
            preparation = await cast(
                CrossReviewCoordinator,
                self._coordinator,
            ).prepare_owner_recheck(
                cast(CrossOwnerInitialReviewAcceptance, context.acceptance),
                cast(CrossOwnerRevisionAcceptance, context.revision_acceptance),
                cast(
                    CrossOwnerLocalReviewAcceptance,
                    context.local_review_acceptance,
                ),
            )
        except BaseException as exc:
            return context.model_copy(update={"status": "failed", "error": str(exc)})
        if preparation.mode == "invoke_agent":
            return context.model_copy(
                update={
                    "status": "recheck_ready",
                    "recheck_preparation": preparation,
                }
            )
        acceptance = cast(
            CrossReviewCoordinator,
            self._coordinator,
        ).accept_owner_recheck(preparation)
        return context.model_copy(
            update={
                "status": "recheck_resumed",
                "recheck_preparation": preparation,
                "recheck_acceptance": acceptance,
            }
        )

    @staticmethod
    def recheck_requires_agent(
        context: DeclarativeCrossOwnerRuntimeContext,
    ) -> bool:
        """Return the explicit original Cross reviewer branch decision."""

        return context.status == "recheck_ready"

    def accept_recheck(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Accept one typed original Cross owner verdict."""

        context = DeclarativeCrossOwnerRuntimeContext.model_validate(values["context"])
        result = DeclarativeCrossOwnerRecheckAgentResult.model_validate(values["result"])
        if result.status == "failed":
            return context.model_copy(update={"status": "failed", "error": result.error})
        try:
            acceptance = cast(
                CrossReviewCoordinator,
                self._coordinator,
            ).accept_owner_recheck(
                cast(CrossOwnerRecheckPreparation, context.recheck_preparation),
                cast(CrossOwnerVerdictSubmission, result.submission),
            )
        except BaseException as exc:
            return context.model_copy(update={"status": "failed", "error": str(exc)})
        return context.model_copy(
            update={
                "status": "recheck_accepted",
                "recheck_acceptance": acceptance,
            }
        )

    async def advance_round(
        self,
        context: DeclarativeCrossOwnerRuntimeContext,
    ) -> DeclarativeCrossOwnerRuntimeContext:
        """Advance the existing pending/resolved owner state after recheck."""

        context = DeclarativeCrossOwnerRuntimeContext.model_validate(context)
        if context.status == "failed" or context.recheck_acceptance is None:
            return context
        try:
            progress = await cast(
                CrossReviewCoordinator,
                self._coordinator,
            ).advance_owner_round(
                cast(CrossOwnerInitialReviewAcceptance, context.acceptance),
                context.recheck_acceptance,
                context.round_progress,
            )
        except BaseException as exc:
            return context.model_copy(update={"status": "failed", "error": str(exc)})
        return context.model_copy(
            update={
                "status": (
                    "round_revision_pending"
                    if progress.next_action == "revise"
                    else "round_completed"
                ),
                "round_progress": progress,
            }
        )

    @staticmethod
    def round_needs_revision(
        context: DeclarativeCrossOwnerRuntimeContext,
    ) -> bool:
        """Return the existing typed pending-finding loop decision."""

        return context.status == "round_revision_pending"

    async def complete_owner_round(
        self,
        context: DeclarativeCrossOwnerRuntimeContext,
    ) -> DeclarativeCrossOwnerPipelineOutcome:
        """Promote a closed typed round or retain the compatibility continuation."""

        context = DeclarativeCrossOwnerRuntimeContext.model_validate(context)
        if context.status != "round_completed" or context.round_progress is None:
            return await self.continue_owner(context)
        try:
            pipeline = cast(
                CrossReviewCoordinator,
                self._coordinator,
            ).complete_owner_round(context.round_progress)
        except BaseException as exc:
            return DeclarativeCrossOwnerPipelineOutcome(
                owner_module_id=context.owner_module_id,
                status="failed",
                error=str(exc),
            )
        return DeclarativeCrossOwnerPipelineOutcome(
            owner_module_id=context.owner_module_id,
            status="completed",
            pipeline=pipeline.model_dump(mode="json"),
        )

    async def complete_owner_without_findings(
        self,
        context: DeclarativeCrossOwnerRuntimeContext,
    ) -> DeclarativeCrossOwnerPipelineOutcome:
        """Complete the ordinary accepted initial result with no findings."""

        context = DeclarativeCrossOwnerRuntimeContext.model_validate(context)
        if context.status == "failed":
            return DeclarativeCrossOwnerPipelineOutcome(
                owner_module_id=context.owner_module_id,
                status="failed",
                error=context.error,
            )
        if self._aggregate_recovered:
            return DeclarativeCrossOwnerPipelineOutcome(
                owner_module_id=context.owner_module_id,
                status="completed",
                pipeline={},
            )
        if self._coordinator is None:
            return await self.execute_owner(
                {"owner_module_id": context.owner_module_id}
            )
        try:
            pipeline = self._coordinator.complete_owner_without_findings(
                cast(CrossOwnerInitialReviewAcceptance, context.acceptance)
            )
        except BaseException as exc:
            return DeclarativeCrossOwnerPipelineOutcome(
                owner_module_id=context.owner_module_id,
                status="failed",
                error=str(exc),
            )
        return DeclarativeCrossOwnerPipelineOutcome(
            owner_module_id=context.owner_module_id,
            status="completed",
            pipeline=pipeline.model_dump(mode="json"),
        )

    async def continue_owner(
        self,
        context: DeclarativeCrossOwnerRuntimeContext,
    ) -> DeclarativeCrossOwnerPipelineOutcome:
        """Continue the current owner pipeline after its declared initial boundary."""

        context = DeclarativeCrossOwnerRuntimeContext.model_validate(context)
        if context.status == "failed":
            return DeclarativeCrossOwnerPipelineOutcome(
                owner_module_id=context.owner_module_id,
                status="failed",
                error=context.error,
            )
        return await self.execute_owner(
            {
                "owner_module_id": context.owner_module_id,
                "initial_acceptance": context.acceptance,
                "revision_acceptance": context.revision_acceptance,
                "local_review_acceptance": context.local_review_acceptance,
                "recheck_acceptance": context.recheck_acceptance,
            }
        )

    async def execute_owner(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeCrossOwnerPipelineOutcome:
        """Execute and drain one owner without mutating sibling branch state."""

        owner_module_id = str(values["owner_module_id"])
        initial_acceptance = cast(
            CrossOwnerInitialReviewAcceptance | None,
            values.get("initial_acceptance"),
        )
        revision_acceptance = cast(
            CrossOwnerRevisionAcceptance | None,
            values.get("revision_acceptance"),
        )
        local_review_acceptance = cast(
            CrossOwnerLocalReviewAcceptance | None,
            values.get("local_review_acceptance"),
        )
        recheck_acceptance = cast(
            CrossOwnerRecheckAcceptance | None,
            values.get("recheck_acceptance"),
        )
        if self._aggregate_recovered:
            return DeclarativeCrossOwnerPipelineOutcome(
                owner_module_id=owner_module_id,
                status="completed",
                pipeline={},
            )
        if self._coordinator is None:
            try:
                if (
                    owner_module_id == REPORT_MODULE_IDS[0]
                    and "cross_review_completion_ref" not in self.current_state
                ):
                    if self._compatibility_cross is None:
                        result = self._runner._cross_review(
                            self.current_state,
                            self._workflow_id,
                        )
                    else:
                        result = self._compatibility_cross(self.current_state)
                    if hasattr(result, "__await__"):
                        await result
                    self._compatibility_invoked = True
            except BaseException as exc:
                return DeclarativeCrossOwnerPipelineOutcome(
                    owner_module_id=owner_module_id,
                    status="failed",
                    error=str(exc),
                )
            return DeclarativeCrossOwnerPipelineOutcome(
                owner_module_id=owner_module_id,
                status="completed",
                pipeline={"compatibility": True},
            )
        try:
            pipeline = await self._coordinator.run_owner(
                owner_module_id,
                initial_acceptance=initial_acceptance,
                revision_acceptance=revision_acceptance,
                local_review_acceptance=local_review_acceptance,
                recheck_acceptance=recheck_acceptance,
            )
        except BaseException as exc:
            return DeclarativeCrossOwnerPipelineOutcome(
                owner_module_id=owner_module_id,
                status="failed",
                error=str(exc),
            )
        return DeclarativeCrossOwnerPipelineOutcome(
            owner_module_id=owner_module_id,
            status="completed",
            pipeline=pipeline.model_dump(mode="json"),
        )

    async def reduce(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Finalize the five drained outcomes and publish the shared state."""

        outcomes = {
            module_id: DeclarativeCrossOwnerPipelineOutcome.model_validate(outcome)
            for module_id, outcome in dict(values["outcomes"]).items()
        }
        failures = {
            module_id: outcome.error or "Cross owner pipeline failed"
            for module_id, outcome in outcomes.items()
            if outcome.status == "failed"
        }
        if self._coordinator is not None:
            reduced = {
                module_id: (
                    RuntimeError(failures[module_id])
                    if module_id in failures
                    else outcomes[module_id].pipeline
                )
                for module_id in REPORT_MODULE_IDS
            }
            await self._coordinator.finalize(reduced)
        elif failures:
            first = min(failures, key=float)
            raise RuntimeError(failures[first])
        return deepcopy(self.current_state)


def retry_failed_cross_owner_pipelines(
    plan: ResolvedPlan,
    state: WorkflowState,
) -> WorkflowState:
    """Retry only owner branches whose typed outcome failed."""

    if state.status is not WorkflowStatus.FAILED:
        return state
    branches = state.parallel_results.get("cross-owner-cohort", {})
    failed = {
        module_id
        for module_id in REPORT_MODULE_IDS
        if module_id in branches
        and DeclarativeCrossOwnerPipelineOutcome.model_validate(
            branches[module_id][f"outcome-{module_id}"]
        ).status
        == "failed"
    }
    if not failed:
        return state
    return retry_parallel_branches(
        plan,
        state,
        parallel_action_id="cross-owner-cohort",
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
    "DeclarativeCrossOwnerRuntime",
    "DeclarativeCrossOwnerPipelineOutcome",
    "compile_cross_owner_workflows",
    "register_cross_owner_pipeline_specializations",
    "retry_failed_cross_owner_pipelines",
]
