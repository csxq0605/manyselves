"""Declarative vertical slice for one Reporting module review lane.

This module owns Reporting-specific definitions and adapters.  The Kernel only
executes neutral actions and never learns module, auditor, or revision concepts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
)
from manyselves.kernel.contracts import ContractAdapter, build_contract_catalog
from manyselves.kernel.definitions import (
    AgentDefinition,
    DefinitionKind,
    DefinitionRegistry,
    RecoveryPolicyDefinition,
    TaskDefinition,
    WorkflowDefinition,
    specialize_workflow,
)
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.ports import AgentInvocationOutcome, AgentInvoker, WorkflowStateStore
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState, WorkflowStatus
from manyselves.runtime.semantic_trace import SemanticEventKind, SemanticTraceRecorder
from manyselves.runtime.workflow_host import (
    InMemoryWorkflowEventSink,
    WorkflowRuntimeHost,
)

from .input_contracts import (
    ModuleReviewInput,
    ModuleRevisionDiff,
    ModuleRevisionInput,
    ValidationReport,
    module_content_view,
)
from .review_lifecycle import (
    ReviewLifecycleError,
    _apply_module_patch,
    _validate_module_findings,
    _validate_verdicts,
)
from .revision_diff import build_revision_diff


class ModuleSubjectValidator(Protocol):
    """Capability adapter for the current structural validation result."""

    def __call__(
        self,
        run_id: str,
        subject: ModuleSubmission,
        subject_ref: str,
    ) -> ValidationReport: ...


class DeclarativeModuleLaneError(RuntimeError):
    """Raised when the WP-07 single-revision vertical slice cannot complete."""


def build_module_lane_definitions(
    module_id: str,
    *,
    lifecycle_id: str = "initial",
    revision: int = 1,
) -> tuple[DefinitionRegistry, dict[str, ContractAdapter], WorkflowDefinition]:
    """Specialize the packaged file workflow for one Reporting module."""

    _capability, registry = load_distribution_reporting_capability()
    template = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-module-review-lane",
    )
    if not isinstance(template, WorkflowDefinition):
        raise TypeError("distribution-module-review-lane is not a workflow")
    author_id = f"module-{module_id}-specialist"
    workflow = specialize_workflow(
        template,
        {
            "module_id": module_id,
            "author_id": author_id,
            "revision_task_id": f"module-{module_id}-revision",
            "lifecycle_id": lifecycle_id,
            "revision": revision,
        },
        workflow_id=f"distribution-module-{module_id}-review-lane",
    )
    return registry, build_contract_catalog(registry), workflow


async def execute_declarative_module_lane(
    *,
    run_id: str,
    workflow_id: str,
    module: ModuleSubmission,
    initial_scope: set[str],
    lifecycle_id: str,
    agent_invokers: Mapping[str, AgentInvoker],
    validate_subject: ModuleSubjectValidator,
    state_store: WorkflowStateStore,
    trace: SemanticTraceRecorder | None = None,
    return_state: bool = False,
) -> ModuleSubmission | tuple[ModuleSubmission, WorkflowState]:
    """Execute the explicit WP-07 path without changing the Legacy default."""

    definitions, contracts, workflow = build_module_lane_definitions(
        module.module_id,
        lifecycle_id=lifecycle_id,
        revision=module.revision + 1,
    )
    workflow.state = {
        "report_run_id": run_id,
        "current_module": module,
        "initial_scope": sorted(initial_scope),
    }
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)
    lane_run_id = f"{run_id}--module-{module.module_id}-{lifecycle_id}"
    try:
        state = state_store.load(lane_run_id)
    except FileNotFoundError:
        state = WorkflowState.for_plan(lane_run_id, plan)

    tools = {
        "build-module-initial-review-input": lambda values: _initial_review_input(
            values,
            validate_subject,
            lifecycle_id,
        ),
        "has-module-findings": _has_module_findings,
        "build-module-revision-input": lambda values: _revision_input(
            values,
            lifecycle_id,
        ),
        "apply-module-revision": _apply_revision,
        "build-module-recheck-input": lambda values: _recheck_input(
            values,
            validate_subject,
            lifecycle_id,
        ),
        "complete-module-recheck": _complete_recheck,
    }
    runtime_agents: Mapping[str, AgentInvoker] = agent_invokers
    if trace is not None:
        runtime_agents = {
            agent_id: _TraceAgentInvoker(
                invoker,
                trace=trace,
                workflow_id=workflow_id,
                contracts=contracts,
            )
            for agent_id, invoker in agent_invokers.items()
        }
        trace.record(
            SemanticEventKind.WORKFLOW_STARTED,
            workflow_id=workflow_id,
            status="running",
        )

    completed = await WorkflowRuntimeHost(
        executors,
        state_store,
        InMemoryWorkflowEventSink(),
    ).execute(
        plan,
        state,
        RuntimeContext(
            tools=tools,
            contracts=contracts,
            agents=runtime_agents,
            definitions=definitions,
        ),
    )
    if completed.status is not WorkflowStatus.COMPLETED:
        raise DeclarativeModuleLaneError("declarative module lane did not complete")
    result = ModuleSubmission.model_validate(completed.outputs["result"])
    if trace is not None:
        trace.record(
            SemanticEventKind.OUTPUT_PUBLISHED,
            workflow_id=workflow_id,
            action_id=f"module-{module.module_id}-review-lane",
            output_contract="module_submission",
            output_id=f"module-{module.module_id}-r{result.revision}",
            status="completed",
        )
        trace.record(
            SemanticEventKind.WORKFLOW_COMPLETED,
            workflow_id=workflow_id,
            status="completed",
        )
    if return_state:
        return result, completed
    return result


def _initial_review_input(
    values: Mapping[str, Any],
    validate_subject: ModuleSubjectValidator,
    lifecycle_id: str,
) -> ModuleReviewInput:
    run_id = str(values["run_id"])
    module = ModuleSubmission.model_validate(values["module"])
    scope = set(values["scope"])
    subject_ref = _subject_ref(run_id, module)
    validation = validate_subject(run_id, module, subject_ref)
    return ModuleReviewInput(
        phase="initial",
        run_id=run_id,
        module_id=module.module_id,
        lifecycle_id=lifecycle_id,
        review_round=0,
        subject_ref=subject_ref,
        subject_revision=module.revision,
        subject=module_content_view(module, scope),
        evidence=[],
        required_submodule_ids=sorted(scope),
        validation_report_ref=_validation_ref(run_id, module, 0),
        validation_report=validation,
    )


def _has_module_findings(value: Any) -> bool:
    return bool(ModuleReviewFindingSubmission.model_validate(value).findings)


def _revision_input(
    values: Mapping[str, Any],
    lifecycle_id: str,
) -> ModuleRevisionInput:
    run_id = str(values["run_id"])
    module = ModuleSubmission.model_validate(values["module"])
    findings = ModuleReviewFindingSubmission.model_validate(values["findings"])
    scope = {finding.target_submodule_id for finding in findings.findings}
    _validate_module_findings(
        findings.findings,
        module,
        scope,
        id_prefix=f"M-{module.module_id}-{lifecycle_id}-r0-",
    )
    return ModuleRevisionInput(
        run_id=run_id,
        module_id=module.module_id,
        subject_ref=_subject_ref(run_id, module),
        subject=module_content_view(module, scope),
        target_submodule_ids=sorted(scope),
        module_findings=findings.findings,
    )


def _apply_revision(values: Mapping[str, Any]) -> ModuleSubmission:
    module = ModuleSubmission.model_validate(values["module"])
    revision = ModuleRevisionSubmission.model_validate(values["revision"])
    findings = ModuleReviewFindingSubmission.model_validate(values["findings"])
    return _apply_module_patch(
        module,
        revision,
        target_submodule_ids={
            finding.target_submodule_id for finding in findings.findings
        },
        required_finding_ids={finding.id for finding in findings.findings},
    )


def _recheck_input(
    values: Mapping[str, Any],
    validate_subject: ModuleSubjectValidator,
    lifecycle_id: str,
) -> ModuleReviewInput:
    run_id = str(values["run_id"])
    baseline = ModuleSubmission.model_validate(values["baseline"])
    module = ModuleSubmission.model_validate(values["module"])
    findings = ModuleReviewFindingSubmission.model_validate(values["findings"])
    scope = {finding.target_submodule_id for finding in findings.findings}
    raw_delta = build_revision_diff(baseline, module)
    changed_narratives = set(raw_delta["changed_submodule_narratives"])
    revision_diff = ModuleRevisionDiff(
        module_id=module.module_id,
        from_revision=baseline.revision,
        to_revision=module.revision,
        changed_submodule_narratives=sorted(changed_narratives),
        changed_statement_refs=[],
        evidence_ids_added=raw_delta["source_ids_added"],
        evidence_ids_removed=raw_delta["source_ids_removed"],
    )
    subject_ref = _subject_ref(run_id, module)
    validation = validate_subject(run_id, module, subject_ref)
    return ModuleReviewInput(
        phase="recheck",
        run_id=run_id,
        module_id=module.module_id,
        lifecycle_id=lifecycle_id,
        review_round=1,
        subject_ref=subject_ref,
        subject_revision=module.revision,
        subject=module_content_view(module, changed_narratives),
        evidence=[],
        required_submodule_ids=sorted(scope),
        required_findings=findings.findings,
        revision_responses=module.revision_responses,
        baseline_subject_ref=_subject_ref(run_id, baseline),
        revision_diff_ref=(
            f"Work/runs/{run_id}/reviews/module/{lifecycle_id}/"
            f"{module.module_id}/recheck-diff-r1.json"
        ),
        revision_diff=revision_diff,
        validation_report_ref=_validation_ref(run_id, module, 1),
        validation_report=validation,
    )


def _complete_recheck(values: Mapping[str, Any]) -> ModuleSubmission:
    module = ModuleSubmission.model_validate(values["module"])
    findings = ModuleReviewFindingSubmission.model_validate(values["findings"])
    verdicts = ModuleReviewVerdictSubmission.model_validate(values["verdicts"])
    required_ids = {finding.id for finding in findings.findings}
    _validate_verdicts(verdicts.verdicts, required_ids)
    if verdicts.new_findings or any(
        verdict.verdict != "resolved" for verdict in verdicts.verdicts
    ):
        raise DeclarativeModuleLaneError(
            "WP-07 vertical slice requires the scripted recheck to close all findings"
        )
    return module


def _subject_ref(run_id: str, module: ModuleSubmission) -> str:
    return f"Work/runs/{run_id}/modules/{module.module_id}-r{module.revision}.json"


def _validation_ref(run_id: str, module: ModuleSubmission, review_round: int) -> str:
    return (
        f"Work/runs/{run_id}/reviews/module/{module.module_id}/"
        f"validation-r{module.revision}-review-r{review_round}.json"
    )


class _TraceAgentInvoker:
    def __init__(
        self,
        inner: AgentInvoker,
        *,
        trace: SemanticTraceRecorder,
        workflow_id: str,
        contracts: Mapping[str, ContractAdapter],
    ) -> None:
        self._inner = inner
        self._trace = trace
        self._workflow_id = workflow_id
        self._contracts = contracts
        self._seen_conversations: set[str] = set()

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: Any,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        return await self._invoke_and_trace(
            agent,
            task,
            conversation,
            task_id=task_id,
            invoke=lambda: self._inner.invoke(
                agent,
                task,
                value,
                conversation,
                task_id=task_id,
            ),
        )

    async def _invoke_and_trace(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        conversation: Any,
        *,
        task_id: str,
        invoke: Callable[[], Awaitable[AgentInvocationOutcome]],
    ) -> AgentInvocationOutcome:
        self._trace.record(
            SemanticEventKind.ACTION_STARTED,
            workflow_id=self._workflow_id,
            action_id=task_id,
        )
        if conversation.conversation_id not in self._seen_conversations:
            self._trace.record(
                SemanticEventKind.CONVERSATION_CREATED,
                workflow_id=self._workflow_id,
                action_id=task_id,
                agent_id=agent.id,
                conversation_key=conversation.key.value,
            )
            self._seen_conversations.add(conversation.conversation_id)
        self._trace.record(
            SemanticEventKind.AGENT_INVOKED,
            workflow_id=self._workflow_id,
            action_id=task_id,
            agent_id=agent.id,
            conversation_key=conversation.key.value,
            input_contract=task.input_contract,
            output_contract=task.output_contract,
        )
        try:
            outcome = await invoke()
            if outcome.status != "ok":
                raise DeclarativeModuleLaneError(
                    outcome.error or f"agent {agent.id} returned {outcome.status}"
                )
            validated = self._contracts[task.output_contract].validate(outcome.result)
        except BaseException:
            self._trace.record(
                SemanticEventKind.ACTION_FAILED,
                workflow_id=self._workflow_id,
                action_id=task_id,
                status="failed",
            )
            raise
        self._trace.record(
            SemanticEventKind.CONTRACT_VALIDATED,
            workflow_id=self._workflow_id,
            action_id=task_id,
            output_contract=task.output_contract,
            status="passed",
        )
        self._trace.record(
            SemanticEventKind.BRANCH_SELECTED,
            workflow_id=self._workflow_id,
            action_id=task_id,
            branch=_branch_for_output(task.output_contract, validated),
        )
        self._trace.record(
            SemanticEventKind.ACTION_COMPLETED,
            workflow_id=self._workflow_id,
            action_id=task_id,
            status="completed",
        )
        return outcome.model_copy(update={"result": validated})

    async def invoke_with_recovery(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: Any,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition,
    ) -> AgentInvocationOutcome:
        invoke_with_recovery = getattr(self._inner, "invoke_with_recovery", None)
        if callable(invoke_with_recovery):
            async def invoke() -> AgentInvocationOutcome:
                return await invoke_with_recovery(
                    agent,
                    task,
                    value,
                    conversation,
                    task_id=task_id,
                    recovery_policy=recovery_policy,
                )
        else:
            async def invoke() -> AgentInvocationOutcome:
                return await self._inner.invoke(
                    agent,
                    task,
                    value,
                    conversation,
                    task_id=task_id,
                )
        return await self._invoke_and_trace(
            agent,
            task,
            conversation,
            task_id=task_id,
            invoke=invoke,
        )


def _branch_for_output(output_contract: str, value: Any) -> str:
    if output_contract == "module_review_finding_submission":
        return "revision_required" if value.findings else "lane_completed"
    if output_contract == "module_revision_submission":
        return "reviewer_recheck"
    if output_contract == "module_review_verdict_submission":
        return (
            "lane_completed"
            if not value.new_findings
            and all(verdict.verdict == "resolved" for verdict in value.verdicts)
            else "revision_required"
        )
    raise ReviewLifecycleError(f"unknown module lane output contract: {output_contract}")
