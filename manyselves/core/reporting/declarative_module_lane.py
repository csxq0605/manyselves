"""Declarative vertical slice for one Reporting module review lane.

This module owns Reporting-specific definitions and adapters.  The Kernel only
executes neutral actions and never learns module, auditor, or revision concepts.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from manyselves.kernel.contracts import ContractAdapter, build_contract_adapter
from manyselves.kernel.definitions import (
    AgentDefinition,
    ContractDefinition,
    DefinitionRegistry,
    TaskDefinition,
    ToolDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import (
    ControlFlowWorkflowExecutor,
    RuntimeContext,
    build_builtin_executor_registry,
)
from manyselves.kernel.ports import AgentInvocationOutcome, AgentInvoker, WorkflowStateStore
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState, WorkflowStatus
from manyselves.runtime.semantic_trace import SemanticEventKind, SemanticTraceRecorder

from .agentic_models import (
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
)
from .config import AgentDefinition as ReportingAgentDefinition
from .config import load_packaged_agents
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


_CONTRACT_MODELS: dict[str, str] = {
    "module_submission": (
        "manyselves.core.reporting.agentic_models:ModuleSubmission"
    ),
    "module_review_input": (
        "manyselves.core.reporting.input_contracts:ModuleReviewInput"
    ),
    "module_review_finding_submission": (
        "manyselves.core.reporting.agentic_models:ModuleReviewFindingSubmission"
    ),
    "module_revision_input": (
        "manyselves.core.reporting.input_contracts:ModuleRevisionInput"
    ),
    "module_revision_submission": (
        "manyselves.core.reporting.agentic_models:ModuleRevisionSubmission"
    ),
    "module_review_verdict_submission": (
        "manyselves.core.reporting.agentic_models:ModuleReviewVerdictSubmission"
    ),
}


def build_module_lane_definitions(
    module_id: str,
) -> tuple[DefinitionRegistry, dict[str, ContractAdapter], WorkflowDefinition]:
    """Build one Reporting-owned workflow definition from current Agent files."""

    current_agents = load_packaged_agents()
    auditor = current_agents["evidence-auditor"]
    author_id = f"module-{module_id}-specialist"
    author = current_agents[author_id]
    registry = DefinitionRegistry()
    contracts: dict[str, ContractAdapter] = {}

    for contract_id, model in _CONTRACT_MODELS.items():
        definition = ContractDefinition(
            id=contract_id,
            version="1.0.0",
            description=f"Current Reporting {contract_id} contract",
            adapter="pydantic",
            model=model,
        )
        registry.register(definition)
        contracts[contract_id] = build_contract_adapter(definition)
    for contract_id, schema in {
        "module_lane_bindings": {"type": "object"},
        "boolean": {"type": "boolean"},
    }.items():
        definition = ContractDefinition(
            id=contract_id,
            version="1.0.0",
            description=f"Internal Reporting {contract_id} value",
            adapter="json_schema",
            schema=schema,
        )
        registry.register(definition)
        contracts[contract_id] = build_contract_adapter(definition)

    registry.register(
        _kernel_agent(
            auditor,
            accepts=["module_review_input"],
            produces=[
                "module_review_finding_submission",
                "module_review_verdict_submission",
            ],
        )
    )
    registry.register(
        _kernel_agent(
            author,
            accepts=["module_revision_input"],
            produces=["module_revision_submission"],
        )
    )
    tasks = [
        TaskDefinition(
            id="module-initial-review",
            version="1.0.0",
            description="Initial module-local semantic review",
            agent="evidence-auditor",
            objective=f"审查模块 {module_id} 的当前正文、Claim 与证据边界。",
            input_contract="module_review_input",
            output_contract="module_review_finding_submission",
            tools=["submit_result"],
        ),
        TaskDefinition(
            id="module-revision",
            version="1.0.0",
            description="One bounded revision by the original module author",
            agent=author_id,
            objective=(
                f"以完整模块 {module_id} 的单一作者身份，一次完成所有明确分配的"
                "定向修订；只替换受影响小节，不重复提交未变正文。"
            ),
            input_contract="module_revision_input",
            output_contract="module_revision_submission",
            tools=["submit_result"],
        ),
        TaskDefinition(
            id="module-recheck",
            version="1.0.0",
            description="Recheck by the original module auditor",
            agent="evidence-auditor",
            objective=(
                f"只对模块 {module_id} 的 required_findings 返回逐项 verdict，"
                "并检查修改回归。"
            ),
            input_contract="module_review_input",
            output_contract="module_review_verdict_submission",
            tools=["submit_result"],
        ),
    ]
    for task in tasks:
        registry.register(task)

    for tool_id, output_contract in {
        "build-module-initial-review-input": "module_review_input",
        "has-module-findings": "boolean",
        "build-module-revision-input": "module_revision_input",
        "apply-module-revision": "module_submission",
        "build-module-recheck-input": "module_review_input",
        "complete-module-recheck": "module_submission",
    }.items():
        registry.register(
            ToolDefinition(
                id=tool_id,
                version="1.0.0",
                description=f"Reporting module lane adapter: {tool_id}",
                implementation=(
                    "manyselves.core.reporting.declarative_module_lane:"
                    f"{tool_id.replace('-', '_')}"
                ),
                input_contract="module_lane_bindings",
                output_contract=output_contract,
                side_effect="pure_read",
                parallel_safe=True,
            )
        )

    workflow = WorkflowDefinition(
        id=f"distribution-module-{module_id}-review-lane",
        version="1.0.0",
        description=f"Declarative review lane for Reporting module {module_id}",
        input_contract="module_submission",
        output_contract="module_submission",
        actions=[],
    )
    return registry, contracts, workflow


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
) -> ModuleSubmission:
    """Execute the explicit WP-07 path without changing the Legacy default."""

    definitions, contracts, workflow = build_module_lane_definitions(module.module_id)
    workflow.state = {
        "report_run_id": run_id,
        "current_module": module,
        "initial_scope": sorted(initial_scope),
    }
    workflow.actions = _module_lane_actions(
        module.module_id,
        lifecycle_id=lifecycle_id,
        revision=module.revision + 1,
    )
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

    completed = await ControlFlowWorkflowExecutor(executors, state_store).execute(
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
    return result


def _kernel_agent(
    current: ReportingAgentDefinition,
    *,
    accepts: list[str],
    produces: list[str],
) -> AgentDefinition:
    return AgentDefinition(
        id=current.id,
        version="1.0.0",
        description=current.description,
        instructions=current.instructions,
        model=current.model,
        profile=current.effort,
        tools=list(current.tools),
        accepts=accepts,
        produces=produces,
        conversation_mode="run",
        limits={
            "max_turns": current.max_turns,
            "max_tokens": current.max_tokens,
        },
    )


def _module_lane_actions(
    module_id: str,
    *,
    lifecycle_id: str,
    revision: int,
) -> list[dict[str, Any]]:
    author_id = f"module-{module_id}-specialist"
    return [
        {
            "id": "build-initial-review-input",
            "kind": "invoke_tool",
            "tool": "build-module-initial-review-input",
            "input_variables": {
                "run_id": "report_run_id",
                "module": "current_module",
                "scope": "initial_scope",
            },
            "output_variable": "initial_review_input",
        },
        {
            "id": "create-module-auditor",
            "kind": "create_conversation",
            "agent": "evidence-auditor",
            "conversation_key": f"module-auditor-{module_id}",
            "mode": "run",
            "output_variable": "auditor_conversation",
        },
        {
            "id": f"module-{module_id}-{lifecycle_id}-review-r0",
            "kind": "invoke_agent",
            "agent": "evidence-auditor",
            "task": "module-initial-review",
            "conversation_variable": "auditor_conversation",
            "input_variable": "initial_review_input",
            "output_variable": "initial_findings",
        },
        {
            "id": "findings-present",
            "kind": "invoke_tool",
            "tool": "has-module-findings",
            "input_variable": "initial_findings",
            "output_variable": "requires_revision",
        },
        {
            "id": "choose-revision",
            "kind": "if",
            "condition": {"variable": "requires_revision", "operator": "truthy"},
            "then": "build-revision-input",
            "otherwise": "finish-without-revision",
        },
        {
            "id": "build-revision-input",
            "kind": "invoke_tool",
            "tool": "build-module-revision-input",
            "input_variables": {
                "run_id": "report_run_id",
                "module": "current_module",
                "findings": "initial_findings",
            },
            "output_variable": "revision_input",
        },
        {
            "id": "create-module-author",
            "kind": "create_conversation",
            "agent": author_id,
            "conversation_key": f"module-{module_id}",
            "mode": "run",
            "output_variable": "author_conversation",
        },
        {
            "id": f"module-revision-r{revision}-{module_id}",
            "kind": "invoke_agent",
            "agent": author_id,
            "task": "module-revision",
            "conversation_variable": "author_conversation",
            "input_variable": "revision_input",
            "output_variable": "revision_submission",
        },
        {
            "id": "apply-revision",
            "kind": "invoke_tool",
            "tool": "apply-module-revision",
            "input_variables": {
                "module": "current_module",
                "revision": "revision_submission",
                "findings": "initial_findings",
            },
            "output_variable": "revised_module",
        },
        {
            "id": "build-recheck-input",
            "kind": "invoke_tool",
            "tool": "build-module-recheck-input",
            "input_variables": {
                "run_id": "report_run_id",
                "baseline": "current_module",
                "module": "revised_module",
                "findings": "initial_findings",
            },
            "output_variable": "recheck_input",
        },
        {
            "id": f"module-{module_id}-{lifecycle_id}-review-r1",
            "kind": "invoke_agent",
            "agent": "evidence-auditor",
            "task": "module-recheck",
            "conversation_variable": "auditor_conversation",
            "input_variable": "recheck_input",
            "output_variable": "recheck_verdicts",
        },
        {
            "id": "complete-recheck",
            "kind": "invoke_tool",
            "tool": "complete-module-recheck",
            "input_variables": {
                "module": "revised_module",
                "findings": "initial_findings",
                "verdicts": "recheck_verdicts",
            },
            "output_variable": "completed_module",
        },
        {
            "id": "finish-with-revision",
            "kind": "end_workflow",
            "output_variable": "completed_module",
            "output_name": "result",
        },
        {
            "id": "finish-without-revision",
            "kind": "end_workflow",
            "output_variable": "current_module",
            "output_name": "result",
        },
    ]


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
            outcome = await self._inner.invoke(
                agent,
                task,
                value,
                conversation,
                task_id=task_id,
            )
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
