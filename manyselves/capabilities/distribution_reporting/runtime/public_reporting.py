"""Capability-owned runtime binding for the public Reporting Workflow roots."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from manyselves.kernel.contracts import ContractAdapter, build_contract_catalog
from manyselves.kernel.conversations import ConversationRegistry
from manyselves.kernel.definitions import (
    DefinitionKind,
    DefinitionRegistry,
    WorkflowDefinition,
    specialize_workflow,
)
from manyselves.kernel.executors import (
    RuntimeContext,
    build_builtin_executor_registry,
)
from manyselves.kernel.ports import AgentInvoker, WorkflowStateStore
from manyselves.kernel.workflow import (
    ResolvedPlan,
    WorkflowCompiler,
    WorkflowState,
)
from manyselves.runtime.conversation_store import FileConversationStore
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.tool_adapter import CapabilityToolAdapterFactory
from manyselves.runtime.workflow_host import (
    FileWorkflowEventSink,
    WorkflowEventSink,
    WorkflowRuntimeHost,
)

from .. import load_distribution_reporting_capability
from .entrypoint_tools import (
    build_public_entrypoint_tool_implementations,
    specialize_module_cohort_workflow,
)
from .evidence_readiness import build_evidence_readiness_tool_implementations
from .models.module_cohort import DeclarativeModuleLaneOutcome
from .models.reporting import ReportRequest
from .module_agent_bridge import ModuleAuthoringAgentBridge
from .module_lane_definitions import register_module_runtime_lane_specializations
from .module_lane_tools import build_module_lane_tool_implementations
from .preparation_tools import build_preparation_tool_implementations
from .storage import ReportingStore

WorkflowSpecializer = Callable[[DefinitionRegistry], Any]


class PublicReportingWorkflowRuntime:
    """Compile and execute the public full/module file-defined roots.

    The runtime owns only definition loading, typed Capability Tool binding, and
    Run-local Host setup.  Preparation, readiness, and module Agent behavior are
    supplied by the existing Capability implementations and injected module
    runtime; the Kernel remains unaware of Reporting semantics.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        input_snapshot: Any,
        snapshot_content: Any,
        runtime_photo_ids: Any,
        module_runtime: Any | None = None,
        workflow_specializers: Iterable[WorkflowSpecializer] = (),
        additional_tool_implementations: Mapping[str, Any] | None = None,
        state_store: WorkflowStateStore | None = None,
        events: WorkflowEventSink | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.input_snapshot = input_snapshot
        self.snapshot_content = snapshot_content
        self.runtime_photo_ids = runtime_photo_ids
        self.module_runtime = module_runtime
        self.workflow_specializers = tuple(workflow_specializers)
        self.additional_tool_implementations = dict(
            additional_tool_implementations or {}
        )
        self.state_store = state_store or FileWorkflowStateStore(self.workspace)
        self.events = events or FileWorkflowEventSink(self.workspace)
        self.executors = build_builtin_executor_registry()

    def compile_plan(
        self,
        request: ReportRequest | Any,
        *,
        workflow_id: str | None = None,
    ) -> tuple[DefinitionRegistry, dict[str, ContractAdapter], ResolvedPlan]:
        """Build the root plan after registering existing child specializers."""

        validated = ReportRequest.model_validate(request)
        selected_workflow = self._workflow_id(validated, workflow_id)
        definitions, workflow = self._definitions(
            validated,
            selected_workflow,
        )
        plan = WorkflowCompiler(self.executors).compile(workflow, definitions)
        return definitions, build_contract_catalog(definitions), plan

    async def execute(
        self,
        request: ReportRequest | Any,
        run_id: str,
        *,
        workflow_id: str | None = None,
    ) -> WorkflowState:
        """Start or resume one public root from a request and Run identity."""

        validated_request = ReportRequest.model_validate(request)
        selected_workflow = self._workflow_id(validated_request, workflow_id)
        definitions, contracts, plan = self.compile_plan(
            validated_request,
            workflow_id=selected_workflow,
        )
        try:
            state = self.state_store.load(run_id)
            plan = self.state_store.load_plan(run_id)
        except FileNotFoundError:
            self.state_store.save_plan(run_id, plan)
            if plan.input_variable is None:
                raise TypeError(
                    f"public workflow has no input variable: {selected_workflow}"
                )
            state = WorkflowState.for_plan(
                run_id,
                plan,
                initial_variables={
                    plan.input_variable: validated_request,
                    "run-id": run_id,
                },
            )

        tools = self._tools(definitions, contracts, plan)
        agents = self._agent_invokers()
        return await WorkflowRuntimeHost(
            self.executors,
            self.state_store,
            self.events,
        ).execute(
            plan,
            state,
            RuntimeContext(
                tools=tools,
                agents=agents,
                contracts=contracts,
                definitions=definitions,
                conversations=ConversationRegistry(FileConversationStore(self.workspace)),
                subworkflows=plan.subworkflow_plans,
            ),
        )

    def _definitions(
        self,
        request: ReportRequest,
        workflow_id: str,
    ) -> tuple[DefinitionRegistry, WorkflowDefinition]:
        _capability, source = load_distribution_reporting_capability()
        replacements: dict[str, WorkflowDefinition] = {}
        registry = DefinitionRegistry()
        for definition in source.all():
            if (
                definition.kind == DefinitionKind.WORKFLOW
                and definition.id == "distribution-module-cohort"
            ):
                continue
            registry.register(definition)

        register_module_runtime_lane_specializations(registry)
        for specializer in self.workflow_specializers:
            specializer(registry)

        cohort_template = source.require(
            DefinitionKind.WORKFLOW,
            "distribution-module-cohort",
        )
        if not isinstance(cohort_template, WorkflowDefinition):
            raise TypeError("distribution-module-cohort is not a workflow")
        cohort = specialize_module_cohort_workflow(
            cohort_template,
            request.target_modules,
        )
        cohort = specialize_workflow(
            cohort,
            {"max_concurrency": len(request.target_modules)},
        )
        replacements["distribution-module-cohort"] = cohort

        root = source.require(DefinitionKind.WORKFLOW, workflow_id)
        if not isinstance(root, WorkflowDefinition):
            raise TypeError(f"public workflow is not a workflow: {workflow_id}")
        if cohort.id != "distribution-module-cohort":
            actions = deepcopy(root.actions)
            for action in actions:
                if action.get("id") == "run-module-cohort":
                    action["workflow"] = cohort.id
            root = root.model_copy(update={"actions": actions})
        replacements[root.id] = root

        replacement_ids = set(replacements)
        replacement_ids.add(cohort.id)
        rebuilt = DefinitionRegistry()
        for definition in registry.all():
            if definition.kind == DefinitionKind.WORKFLOW and (
                definition.id in replacement_ids
            ):
                continue
            rebuilt.register(definition)
        rebuilt.register(cohort)
        rebuilt.register(root)
        return rebuilt, root

    @staticmethod
    def _workflow_id(request: ReportRequest, workflow_id: str | None) -> str:
        selected = workflow_id or (
            "full-report" if request.operation == "full_report" else "module-report"
        )
        if selected not in {"full-report", "module-report"}:
            raise ValueError(f"unsupported public Reporting workflow: {selected}")
        return selected

    def _tools(
        self,
        definitions: DefinitionRegistry,
        contracts: Mapping[str, ContractAdapter],
        plan: ResolvedPlan,
    ) -> dict[str, Any]:
        reporting_store = ReportingStore(self.workspace)
        implementations = {
            **build_public_entrypoint_tool_implementations(),
            **build_preparation_tool_implementations(
                workspace=self.workspace,
                input_snapshot=self.input_snapshot,
                snapshot_content=self.snapshot_content,
                runtime_photo_ids=self.runtime_photo_ids,
                store=reporting_store,
            ),
            **build_evidence_readiness_tool_implementations(),
            **self._module_tool_implementations(),
            **build_module_lane_tool_implementations(store=reporting_store),
            **self.additional_tool_implementations,
        }
        factory = CapabilityToolAdapterFactory(
            "distribution-reporting",
            implementations,
            contracts,
        )
        tools: dict[str, Any] = {}
        for tool_id in self._plan_tool_ids(plan):
            definition = definitions.require(DefinitionKind.TOOL, tool_id)
            implementation_id = definition.implementation.rsplit(":", maxsplit=1)[-1]
            if implementation_id not in implementations:
                # The Host will persist the completed prefix and expose the
                # real missing Core-owned boundary when that action is reached.
                continue
            tools[tool_id] = factory.build(definition)
        return tools

    def _module_tool_implementations(self) -> dict[str, Any]:
        runtime = self.module_runtime

        def method(attribute: str) -> Any | None:
            if runtime is None:
                return None
            implementation = getattr(runtime, attribute, None)
            return implementation if callable(implementation) else None

        start_lane_impl = getattr(runtime, "start_lane", None)

        def start_lane(value: Mapping[str, Any]) -> Any:
            lane_outcome = value.get("lane_outcomes", {}).get(value["module_id"])
            if lane_outcome is not None:
                lane_outcome = DeclarativeModuleLaneOutcome.model_validate(lane_outcome)
            return start_lane_impl(
                str(value["module_id"]),
                value["state"],
                "public-reporting",
                lane_outcome,
            )
        mappings = {
            "prepare-current-module-authoring": "prepare_author_lane",
            "module-authoring-requires-agent": "author_requires_agent",
            "accept-current-module-authoring": "accept_author_lane",
            "resume-current-module-authoring": "resume_author_lane",
            "module-lane-can-review": "can_review_lane",
            "prepare-current-module-review": "prepare_review_lane",
            "module-review-preflight-needs-revision": "review_preflight_needs_revision",
            "prepare-current-module-preflight-revision": "prepare_preflight_revision_lane",
            "accept-current-module-preflight-revision": "accept_preflight_revision_lane",
            "module-review-requires-agent": "review_requires_agent",
            "accept-current-module-review": "accept_review_lane",
            "module-review-needs-revision": "review_needs_revision",
            "module-review-needs-recheck": "review_needs_recheck",
            "prepare-current-module-revision": "prepare_revision_lane",
            "accept-current-module-revision": "accept_revision_lane",
            "prepare-current-module-author-exception": "prepare_author_exception_lane",
            "prepare-current-module-recheck": "prepare_recheck_lane",
            "module-recheck-requires-agent": "recheck_requires_agent",
            "accept-current-module-recheck": "accept_recheck_lane",
            "resume-current-module-review": "resume_review_lane",
            "resume-current-module-recheck": "resume_recheck_lane",
            "module-lane-has-deferred-main-exception": "lane_has_deferred_main_exception",
            "module-lane-retries-preflight-revision": "lane_retries_preflight_revision",
            "module-preflight-revision-needs-recheck": "preflight_revision_needs_recheck",
            "prepare-current-module-main-exception": "prepare_main_exception_lane",
            "module-main-exception-requires-agent": "main_exception_requires_agent",
            "accept-current-module-main-exception": "accept_main_exception_lane",
            "module-main-exception-requests-user": "main_exception_requests_user",
            "apply-current-module-main-exception-user-input": "apply_main_exception_user_input",
            "route-current-module-after-main-exception": "route_after_main_exception",
            "complete-current-module-lane": "complete_lane",
            "prepare-module-cohort": "prepare_lanes",
            "reduce-module-cohort": "reduce_lanes",
        }
        implementations: dict[str, Any] = {}
        if callable(start_lane_impl):
            implementations["start-current-module-lane"] = start_lane
        for tool_id, attribute in mappings.items():
            implementation = method(attribute)
            if implementation is not None:
                implementations[tool_id] = implementation
        return implementations

    def _agent_invokers(self) -> Mapping[str, AgentInvoker]:
        if self.module_runtime is None:
            return {}
        invokers = dict(getattr(self.module_runtime, "agent_invokers", {}))
        execution = getattr(self.module_runtime, "agent_execution", None)
        session_factory = getattr(self.module_runtime, "agent_session_factory", None)
        if execution is None or not callable(session_factory):
            return invokers
        for agent_id in tuple(invokers):
            if agent_id.startswith("module-") and agent_id.endswith("-specialist"):
                invokers[agent_id] = ModuleAuthoringAgentBridge(
                    self.workspace,
                    execution=execution,
                    session_factory=session_factory,
                )
        return invokers

    @staticmethod
    def _plan_tool_ids(plan: ResolvedPlan) -> tuple[str, ...]:
        tool_ids: list[str] = []
        seen: set[str] = set()
        for candidate in (plan, *plan.subworkflow_plans.values()):
            for tool_id in candidate.tool_ids:
                if tool_id not in seen:
                    seen.add(tool_id)
                    tool_ids.append(tool_id)
        return tuple(tool_ids)


__all__ = [
    "PublicReportingWorkflowRuntime",
]
