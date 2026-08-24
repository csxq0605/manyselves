"""Capability-owned preparation for the aggregate-existing workflow."""

from __future__ import annotations

import json
from collections.abc import Mapping
from inspect import isawaitable
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from manyselves.capabilities.distribution_reporting.runtime.assets import (
    validate_existing_markdown_modules,
    validate_module_markdown_consistency,
)
from manyselves.capabilities.distribution_reporting.runtime.delivery_tools import (
    build_delivery_tool_implementations,
)
from manyselves.capabilities.distribution_reporting.runtime.final_delivery_binding import (
    build_final_chapter_tool_implementations,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.aggregate_existing import (
    AggregateExistingContext,
    AggregateExistingHandoff,
    AggregateExistingPreparationInput,
    ModuleId,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    AggregateEditorInput,
    ValidationFailure,
    ValidationReport,
    module_content_view,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
    EvidenceItem,
    PhotoAsset,
)
from manyselves.capabilities.distribution_reporting.runtime.public_entrypoints import (
    project_public_aggregate_existing_input,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.kernel.contracts import ContractAdapter, build_contract_adapter
from manyselves.kernel.conversations import ConversationRegistry
from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionKind,
    DefinitionRegistry,
    ToolDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.ports import AgentInvoker, ToolInvocationOutcome
from manyselves.kernel.workflow import (
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
    restore_plan_definition_registry,
)
from manyselves.runtime.capability_binding import (
    CapabilityRunInputError,
    CapabilityRunNotFoundError,
    CapabilityRunStateError,
)
from manyselves.runtime.conversation_store import FileConversationStore
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.workflow_host import (
    FileWorkflowEventSink,
    WorkflowEventSink,
    WorkflowRuntimeHost,
)

from .. import load_distribution_reporting_capability
from .input_snapshot import RunInputSnapshotStore


class InputSnapshotLoader(Protocol):
    """Load the already-created frozen input projection for one run."""

    def __call__(self, run_id: str) -> Any: ...


def _load_snapshot(
    input_snapshot: InputSnapshotLoader | Any,
    run_id: str,
) -> Any:
    if callable(input_snapshot):
        return input_snapshot(run_id)
    loader = getattr(input_snapshot, "load", None)
    if callable(loader):
        return loader(run_id)
    return input_snapshot


def _load_run_evidence(workspace: Path, run_id: str) -> list[EvidenceItem]:
    path = workspace / f"Work/runs/{run_id}/evidence.jsonl"
    if not path.is_file():
        return []
    return [
        EvidenceItem.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _load_run_photos(workspace: Path, run_id: str) -> list[PhotoAsset]:
    path = workspace / f"Work/runs/{run_id}/context/photo-manifest.json"
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [PhotoAsset.model_validate(item) for item in payload.get("assets", [])]


class AggregateExistingTools:
    """Fine-grained source preparation and model-input projection Tools."""

    def __init__(
        self,
        *,
        workspace: Path,
        input_snapshot: InputSnapshotLoader | Any,
        store: ReportingStore,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.input_snapshot = input_snapshot
        self.store = store

    def prepare(
        self,
        value: AggregateExistingPreparationInput,
    ) -> AggregateExistingContext:
        """Read the five frozen module bodies and persist the existing handoff."""

        value = AggregateExistingPreparationInput.model_validate(value)
        request = value.request
        configured_refs = request.source_module_refs or {
            module_id: Path(f"Outputs/Modules/{module_id}.md")
            for module_id in REPORT_MODULE_IDS
        }
        snapshot = _load_snapshot(self.input_snapshot, value.run_id)
        evidence_items = _load_run_evidence(self.workspace, value.run_id)
        photo_assets = _load_run_photos(self.workspace, value.run_id)
        module_refs: dict[ModuleId, str] = {}
        structured_modules: dict[ModuleId, ModuleSubmission] = {}
        markdown_modules: dict[ModuleId, str] = {}

        for module_id in REPORT_MODULE_IDS:
            relative = Path(configured_refs[module_id])
            frozen_relative = Path(snapshot.resolve(relative))
            source = (self.workspace / frozen_relative).resolve()
            if not source.is_relative_to(self.workspace) or not source.is_file():
                raise FileNotFoundError(
                    f"existing module report does not exist: {relative.as_posix()}"
                )
            source_text = source.read_text(encoding="utf-8")
            if not source_text.strip():
                raise ValueError(
                    f"existing module report is empty: {relative.as_posix()}"
                )
            if relative.suffix.casefold() == ".json":
                raw = json.loads(source_text)
                if isinstance(raw, dict) and isinstance(raw.get("payload"), dict):
                    raw = raw["payload"]
                module = ModuleSubmission.model_validate(raw)
                if module.module_id != module_id:
                    raise ValueError(
                        f"structured module ref {relative.as_posix()} contains "
                        f"{module.module_id}, expected {module_id}"
                    )
                structured_modules[module_id] = module
            else:
                markdown_modules[module_id] = source_text
            module_refs[module_id] = frozen_relative.as_posix()

        if structured_modules and set(structured_modules) != set(REPORT_MODULE_IDS):
            raise ValueError(
                "aggregate_existing must use five structured module JSON refs together; "
                "mixing Markdown and JSON would drop evidence bindings"
            )

        source_manifest_ref = (
            f"Work/runs/{value.run_id}/context/aggregate-source-manifest.json"
        )
        integrity_report_ref = (
            f"Work/runs/{value.run_id}/reviews/aggregate-module-integrity.json"
        )
        source_format = (
            "structured_module" if structured_modules else "markdown"
        )
        self.store.write_json(
            source_manifest_ref,
            {
                "source_format": source_format,
                "module_refs": module_refs,
            },
        )

        try:
            if structured_modules:
                validate_module_markdown_consistency(structured_modules)
            else:
                validate_existing_markdown_modules(markdown_modules)
        except ValueError as exc:
            self.store.write_json(
                integrity_report_ref,
                ValidationReport(
                    run_id=value.run_id,
                    subject_ref=source_manifest_ref,
                    validator="aggregate-module-integrity/v1",
                    check_ids=["aggregate.five_modules_and_fixed_sections"],
                    failures=[
                        ValidationFailure(
                            check_id="aggregate.five_modules_and_fixed_sections",
                            target_path="module_refs",
                            message=str(exc),
                        )
                    ],
                    passed=False,
                ).model_dump(mode="json"),
            )
            raise ValueError(f"已有模块汇总输入完整性校验未通过：{exc}") from exc

        self.store.write_json(
            integrity_report_ref,
            ValidationReport(
                run_id=value.run_id,
                subject_ref=source_manifest_ref,
                validator="aggregate-module-integrity/v1",
                check_ids=["aggregate.five_modules_and_fixed_sections"],
                passed=True,
            ).model_dump(mode="json"),
        )

        if structured_modules:
            editor_input = AggregateEditorInput(
                run_id=value.run_id,
                source_format="structured_module",
                approved_module_markers={
                    module_id: f"[[APPROVED_MODULE:{module_id}]]"
                    for module_id in REPORT_MODULE_IDS
                },
                structured_modules={
                    module_id: module_content_view(module)
                    for module_id, module in structured_modules.items()
                },
            )
        else:
            editor_input = AggregateEditorInput(
                run_id=value.run_id,
                source_format="markdown",
                approved_module_markers={
                    module_id: f"[[APPROVED_MODULE:{module_id}]]"
                    for module_id in REPORT_MODULE_IDS
                },
                markdown_modules=markdown_modules,
            )

        editor_input_ref = (
            f"Work/runs/{value.run_id}/context/aggregate-editor-input.json"
        )
        self.store.write_json(
            editor_input_ref,
            editor_input.model_dump(mode="json"),
        )
        return AggregateExistingContext(
            run_id=value.run_id,
            request=request,
            source_format=source_format,
            module_refs=module_refs,
            structured_modules=structured_modules,
            markdown_modules=markdown_modules,
            evidence_items=evidence_items,
            photo_assets=photo_assets,
            source_manifest_ref=source_manifest_ref,
            integrity_report_ref=integrity_report_ref,
            editor_input_ref=editor_input_ref,
            editor_input=editor_input,
        )

    @staticmethod
    def project_editor_input(value: AggregateExistingContext) -> AggregateEditorInput:
        """Project the persisted preparation context into the Agent contract."""

        return AggregateExistingContext.model_validate(value).editor_input


def build_aggregate_existing_tools(
    *,
    workspace: Path,
    input_snapshot: InputSnapshotLoader | Any,
    store: ReportingStore,
) -> AggregateExistingTools:
    return AggregateExistingTools(
        workspace=workspace,
        input_snapshot=input_snapshot,
        store=store,
    )


def build_aggregate_existing_tool_implementations(
    *,
    workspace: Path,
    input_snapshot: InputSnapshotLoader | Any,
    store: ReportingStore | None = None,
) -> dict[str, Any]:
    """Bind only the source-preparation Tools for this migration slice."""

    tools = build_aggregate_existing_tools(
        workspace=workspace,
        input_snapshot=input_snapshot,
        store=store or ReportingStore(workspace),
    )
    implementations = {
        "project-public-aggregate-existing-input": project_public_aggregate_existing_input,
        "prepare-aggregate-existing": tools.prepare,
        "project-aggregate-editor-input": tools.project_editor_input,
        "project-aggregate-existing-handoff": project_aggregate_existing_handoff,
        "project-aggregate-existing-tail": project_aggregate_existing_tail_state,
    }
    implementations.update(
        build_final_chapter_tool_implementations(
            workspace=workspace,
            store=tools.store,
        )
    )
    implementations.update(
        build_delivery_tool_implementations(
            workspace=workspace,
            store=tools.store,
        )
    )
    return implementations


def project_aggregate_existing_handoff(value: Any) -> AggregateExistingHandoff:
    """Keep the editor result and prepared source context in one typed handoff."""

    payload = value if isinstance(value, Mapping) else value
    return AggregateExistingHandoff.model_validate(payload)


def project_aggregate_existing_tail_state(
    value: AggregateExistingHandoff | Any,
) -> dict[str, Any]:
    """Project aggregate output into the existing Final/Delivery state shape.

    This is a pure in-memory projection.  It deliberately does not create a
    final-review completion reference or any Delivery artifact; those belong to
    the subsequent declared subworkflows.
    """

    handoff = AggregateExistingHandoff.model_validate(value)
    context = handoff.context
    return {
        "run_id": context.run_id,
        "request": context.request,
        "edited_report": handoff.edited_report,
        "module_submissions": context.structured_modules,
        "markdown_modules": context.markdown_modules,
        "evidence_items": context.evidence_items,
        "photo_assets": context.photo_assets,
        "aggregate_source_format": context.source_format,
        "aggregate_source_manifest_ref": context.source_manifest_ref,
        "aggregate_integrity_report_ref": context.integrity_report_ref,
        "aggregate_editor_input_ref": context.editor_input_ref,
    }


class _AggregateToolAdapter:
    """Typed Tool bridge local to this Capability's runtime binding."""

    def __init__(
        self,
        implementation: Any,
        input_contract: ContractAdapter,
        output_contract: ContractAdapter,
    ) -> None:
        self._implementation = implementation
        self._input_contract = input_contract
        self._output_contract = output_contract

    async def invoke(
        self,
        arguments: Any,
        *,
        task_id: str,
    ) -> ToolInvocationOutcome:
        del task_id
        validated_input = self._input_contract.validate(arguments)
        result = self._implementation(validated_input)
        if isawaitable(result):
            result = await result
        validated_output = self._output_contract.validate(result)
        return ToolInvocationOutcome(result=validated_output)


class AggregateExistingWorkflowRuntime:
    """Bind this Capability's Agent to the generic file Workflow Host.

    The binding supplies only run-local inputs, Tools, definitions, and the
    injected AgentInvoker mapping.  The Kernel and WorkflowCompiler remain
    unaware of the aggregate-reporting task; selecting the packaged tail
    workflow composes the same aggregate, Final, and Delivery definitions.
    """

    workflow_id = "distribution-aggregate-existing"
    agent_id = "aggregate-editor"

    def __init__(
        self,
        workspace: Path,
        *,
        input_snapshot: InputSnapshotLoader | Any,
        agent_invoker: AgentInvoker | None = None,
        agent_invokers: Mapping[str, AgentInvoker] | None = None,
        workflow_id: str = "distribution-aggregate-existing",
        events: WorkflowEventSink | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.input_snapshot = input_snapshot
        self.workflow_id = workflow_id
        self.agent_invoker = agent_invoker
        self.agent_invokers = dict(agent_invokers or {})
        if agent_invoker is not None:
            self.agent_invokers.setdefault(self.agent_id, agent_invoker)
        self._state_store = FileWorkflowStateStore(self.workspace)
        self._executors = build_builtin_executor_registry()
        self._events = (
            events
            if events is not None
            else FileWorkflowEventSink(self.workspace)
        )

    async def execute(self, value: AggregateExistingPreparationInput | Any) -> WorkflowState:
        """Execute or resume one same-Run aggregate-existing Workflow."""

        registry, contracts, plan = self._compiled()
        if plan.input_contract is None or plan.input_variable is None:
            raise TypeError(
                "distribution-aggregate-existing workflow has no declared input binding"
            )
        validated = contracts[plan.input_contract].validate(value)
        run_id = validated.run_id
        try:
            state = self._state_store.load(run_id)
        except FileNotFoundError:
            self._state_store.save_plan(run_id, plan)
            state = WorkflowState.for_plan(
                run_id,
                plan,
                initial_variables={plan.input_variable: validated},
            )
        tools = self._tools(registry, contracts, plan.tool_ids)
        context = RuntimeContext(
            tools=tools,
            contracts=contracts,
            agents=self.agent_invokers,
            definitions=registry,
            conversations=ConversationRegistry(
                FileConversationStore(self.workspace)
            ),
            plan_tool_factory=self._bind_plan_tool,
        )
        return await WorkflowRuntimeHost(
            self._executors,
            self._state_store,
            self._events,
        ).execute(plan, state, context)

    async def resume(
        self,
        command_id: UUID,
        run_id: str,
    ) -> dict[str, Any]:
        """Resume the persisted incomplete aggregate Plan + State."""

        del command_id
        state = self._load_state(run_id)
        if state.status is WorkflowStatus.COMPLETED:
            return {"run_id": run_id, "task_id": None}
        if state.status is WorkflowStatus.WAITING:
            raise CapabilityRunStateError(
                "run is waiting for input; resume it through the input endpoint"
            )
        try:
            plan = self._state_store.load_plan(run_id)
        except FileNotFoundError as exc:
            raise CapabilityRunNotFoundError(run_id) from exc
        registry = restore_plan_definition_registry(plan)
        contracts = self._contracts(registry)
        await self._execute_public(plan, state, registry, contracts)
        return {"run_id": run_id, "task_id": None}

    def _compiled(
        self,
    ) -> tuple[DefinitionRegistry, dict[str, ContractAdapter], Any]:
        _capability, registry = load_distribution_reporting_capability()
        workflow = registry.require(DefinitionKind.WORKFLOW, self.workflow_id)
        if not isinstance(workflow, WorkflowDefinition):
            raise TypeError(f"definition is not a workflow: {self.workflow_id}")
        contracts = self._contracts(registry)
        plan = WorkflowCompiler(self._executors).compile(workflow, registry)
        return registry, contracts, plan

    @staticmethod
    def _contracts(registry: DefinitionRegistry) -> dict[str, ContractAdapter]:
        return {
            definition.id: build_contract_adapter(definition)
            for definition in registry.all(DefinitionKind.CONTRACT)
            if isinstance(definition, ContractDefinition)
        }

    def _tools(
        self,
        registry: DefinitionRegistry,
        contracts: Mapping[str, ContractAdapter],
        tool_ids: list[str],
    ) -> dict[str, _AggregateToolAdapter]:
        implementations = build_aggregate_existing_tool_implementations(
            workspace=self.workspace,
            input_snapshot=self.input_snapshot,
            store=ReportingStore(self.workspace),
        )
        return {
            tool_id: self._bind_tool(
                registry.require(DefinitionKind.TOOL, tool_id),
                contracts,
                implementations,
            )
            for tool_id in tool_ids
        }

    def _bind_plan_tool(
        self,
        definition: ToolDefinition,
        contracts: Mapping[str, ContractAdapter],
    ) -> _AggregateToolAdapter:
        return self._bind_tool(
            definition,
            contracts,
            build_aggregate_existing_tool_implementations(
                workspace=self.workspace,
                input_snapshot=self.input_snapshot,
                store=ReportingStore(self.workspace),
            ),
        )

    @staticmethod
    def _bind_tool(
        definition: ToolDefinition,
        contracts: Mapping[str, ContractAdapter],
        implementations: Mapping[str, Any],
    ) -> _AggregateToolAdapter:
        implementation_id = definition.implementation.rsplit(":", maxsplit=1)[-1]
        return _AggregateToolAdapter(
            implementations[implementation_id],
            contracts[definition.input_contract],
            contracts[definition.output_contract],
        )


class PublicAggregateExistingWorkflowRuntime(AggregateExistingWorkflowRuntime):
    """Application-facing runtime for the ``aggregate-existing`` file root.

    The public root owns only the user Schema projection, Run identity and
    generic Run projections.  Its declared subworkflow still supplies the
    aggregate, Final and Delivery composition, while Agent invokers remain
    Capability-owned inputs to this runtime.
    """

    workflow_id = "aggregate-existing"

    def __init__(
        self,
        workspace: Path,
        *,
        input_snapshot: InputSnapshotLoader | Any | None = None,
        agent_invoker: AgentInvoker | None = None,
        agent_invokers: Mapping[str, AgentInvoker] | None = None,
        events: WorkflowEventSink | None = None,
        state_store: FileWorkflowStateStore | None = None,
    ) -> None:
        snapshot_store = RunInputSnapshotStore(workspace)
        super().__init__(
            workspace,
            input_snapshot=input_snapshot or snapshot_store,
            agent_invoker=agent_invoker,
            agent_invokers=agent_invokers,
            workflow_id=self.workflow_id,
            events=events,
        )
        if state_store is not None:
            self._state_store = state_store
        self._input_snapshot_store = snapshot_store

    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]:
        if workflow_id != self.workflow_id:
            raise ValueError(f"workflow is not runnable: {workflow_id}")
        registry, contracts, plan = self._compiled()
        if plan.input_contract is None or plan.input_variable is None:
            raise TypeError("aggregate-existing has no declared input binding")
        request = contracts[plan.input_contract].validate(values)
        run_id = self.run_id_for(command_id, workflow_id)
        source_refs = getattr(request, "source_module_refs", None)
        extra_refs = tuple(
            source_refs.values()
            if source_refs
            else (
                Path("Outputs") / "Modules" / f"{module_id}.md"
                for module_id in REPORT_MODULE_IDS
            )
        )
        self._input_snapshot_store.freeze(run_id, extra_refs=extra_refs)
        try:
            state = self._state_store.load(run_id)
            plan = self._state_store.load_plan(run_id)
        except FileNotFoundError:
            self._state_store.save_plan(run_id, plan)
            state = WorkflowState.for_plan(
                run_id,
                plan,
                initial_variables={
                    plan.input_variable: request,
                    "run-id": run_id,
                },
            )
        await self._execute_public(plan, state, registry, contracts)
        return {"run_id": run_id, "task_id": None}

    @property
    def state_store(self) -> FileWorkflowStateStore:
        return self._state_store

    @staticmethod
    def run_id_for(command_id: UUID, workflow_id: str) -> str:
        """Return the Run identity owned by aggregate-existing."""

        return f"{workflow_id}-{command_id.hex}"

    async def provide_input(
        self,
        command_id: UUID,
        run_id: str,
        *,
        input_id: str | None,
        values: Any,
    ) -> dict[str, Any]:
        del command_id, input_id, values
        self._load_state(run_id)
        raise CapabilityRunInputError("aggregate-existing has no waiting input")

    def get_run(self, run_id: str) -> dict[str, Any]:
        state = self._load_state(run_id)
        waiting_input = [state.waiting_input] if state.waiting_input is not None else []
        return {
            "run": {
                "run_id": run_id,
                "capability_id": "distribution-reporting",
                "workflow_id": state.workflow_id,
                "status": state.status.value,
                "active": state.status in {WorkflowStatus.PENDING, WorkflowStatus.RUNNING},
                "task_id": None,
            },
            "state": state.model_dump(mode="json"),
            "waiting_input": waiting_input,
        }

    def get_outputs(self, run_id: str) -> dict[str, Any]:
        state = self._load_state(run_id)
        outputs: list[dict[str, Any]] = []
        for output_id, stored_value in state.outputs.items():
            value = (
                stored_value.model_dump(mode="json")
                if hasattr(stored_value, "model_dump")
                else stored_value
            )
            declared_artifacts: list[Any] = []
            if isinstance(value, Mapping) and isinstance(
                value.get("output_artifacts"), list
            ):
                declared_artifacts = value["output_artifacts"]
                value = {
                    key: value[key]
                    for key in (
                        "run_id",
                        "delivery_completion_ref",
                        "delivery_status",
                        "output_artifacts",
                    )
                    if key in value
                }
            outputs.append({"id": output_id, "kind": "value", "value": value})
            for artifact in declared_artifacts:
                if not isinstance(artifact, Mapping):
                    continue
                path = str(artifact.get("path", ""))
                if not path:
                    continue
                target = Path(path)
                target = target if target.is_absolute() else self.workspace / target
                exists = target.is_file()
                outputs.append(
                    {
                        "id": path,
                        "kind": "artifact",
                        "path": path,
                        "exists": exists,
                        "size": target.stat().st_size if exists else 0,
                    }
                )
        return {"run_id": run_id, "outputs": outputs}

    def get_cost(self, run_id: str) -> dict[str, Any]:
        self._load_state(run_id)
        from manyselves.core.usage_ledger import UsageLedger

        return {
            "run_id": run_id,
            "usage": UsageLedger(self.workspace, run_id).summarize(group_by="stage"),
        }

    async def _execute_public(
        self,
        plan: Any,
        state: WorkflowState,
        registry: DefinitionRegistry,
        contracts: Mapping[str, ContractAdapter],
    ) -> WorkflowState:
        tools = self._tools(registry, contracts, plan.tool_ids)
        return await WorkflowRuntimeHost(
            self._executors,
            self._state_store,
            self._events,
        ).execute(
            plan,
            state,
            RuntimeContext(
                tools=tools,
                agents=self.agent_invokers,
                contracts=contracts,
                definitions=registry,
                conversations=ConversationRegistry(
                    FileConversationStore(self.workspace)
                ),
                plan_tool_factory=self._bind_plan_tool,
                subworkflows=plan.subworkflow_plans,
            ),
        )

    def _load_state(self, run_id: str) -> WorkflowState:
        try:
            state = self._state_store.load(run_id)
        except FileNotFoundError as exc:
            raise CapabilityRunNotFoundError(run_id) from exc
        if state.workflow_id != self.workflow_id:
            raise CapabilityRunNotFoundError(run_id)
        return state


__all__ = [
    "AggregateExistingTools",
    "AggregateExistingWorkflowRuntime",
    "InputSnapshotLoader",
    "PublicAggregateExistingWorkflowRuntime",
    "build_aggregate_existing_tool_implementations",
    "build_aggregate_existing_tools",
]
