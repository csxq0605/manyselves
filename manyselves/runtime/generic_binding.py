"""One Run lifecycle for any file-defined Capability.

A Capability supplies its definition file and the Python implementations of
its Tools.  Everything else is generic: compile an entrypoint workflow, start
or resume the Run on the Runtime Host, invoke every Agent through the
definition-driven invoker, and project state, outputs and cost.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from manyselves.kernel.contracts import ContractAdapter, build_contract_adapter
from manyselves.kernel.conversations import ConversationRegistry
from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionKind,
    DefinitionRegistry,
    ToolDefinition,
    WorkflowDefinition,
    load_capability,
)
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.workflow import (
    ResolvedPlan,
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
    restore_plan_definition_registry,
    resume_waiting_input,
)

from .agent_execution import AgentExecutionService, AgentSessionLoop
from .capability_binding import (
    CapabilityRunInputError,
    CapabilityRunNotFoundError,
    CapabilityRunStateError,
)
from .conversation_store import FileConversationStore
from .definition_agent import DefinitionAgentInvoker
from .run_lifecycle import DetachedRunTaskOwner, StartAwareFileWorkflowStateStore
from .tool_adapter import CapabilityToolAdapter, CapabilityToolAdapterFactory
from .tools.result_memory import RunToolResultIndex
from .usage_ledger import UsageLedger
from .workflow_host import FileWorkflowEventSink, WorkflowRuntimeHost

ToolImplementations = Mapping[str, Callable[[Any], Any]]
StartInput = Callable[[str, str, Any], dict[str, Any]]


class GenericCapabilityRuntimeBinding:
    """Run any entrypoint of one Capability through the generic Runtime."""

    def __init__(
        self,
        *,
        capability_file: Path,
        workspace: Path,
        services: Any,
        tool_implementations: ToolImplementations,
        loop_builder: Callable[..., AgentSessionLoop] | None = None,
        start_input: StartInput | None = None,
    ) -> None:
        capability, _registry = load_capability(capability_file)
        self.capability_id = capability.id
        self.entrypoints = tuple(capability.entrypoints)
        self.capability_file = Path(capability_file)
        self.workspace = Path(workspace).resolve()
        self.services = services
        self.tool_implementations = tool_implementations
        self._loop_builder = loop_builder
        self._start_input = start_input
        self._store = StartAwareFileWorkflowStateStore(self.workspace)
        self._executors = build_builtin_executor_registry()
        self._detached = DetachedRunTaskOwner()
        self._execution = (
            AgentExecutionService(services.bus) if services is not None else None
        )

    @property
    def active(self) -> bool:
        return self._detached.active

    @property
    def state_store(self) -> StartAwareFileWorkflowStateStore:
        return self._store

    def run_id_for(self, command_id: UUID, workflow_id: str) -> str:
        return f"{workflow_id}-{command_id.hex}"

    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]:
        registry, plan = self._compile(workflow_id)
        contracts = _contracts(registry)
        run_id = self.run_id_for(command_id, workflow_id)
        # Resolve every Tool the plan reaches before any Run state exists.
        self._tools(plan, contracts, None)
        try:
            state = self._store.load(run_id)
        except FileNotFoundError:
            initial: dict[str, Any] = {}
            if self._start_input is not None:
                # The Capability projects its public input into initial variables.
                try:
                    initial = self._start_input(run_id, workflow_id, values)
                except ValueError as exc:
                    raise CapabilityRunInputError(str(exc)) from exc
            elif plan.input_variable is not None:
                if plan.input_contract is None:
                    raise TypeError(f"workflow {workflow_id} input has no contract")
                try:
                    initial[plan.input_variable] = contracts[plan.input_contract].validate(
                        values
                    )
                except ValueError as exc:
                    raise CapabilityRunInputError(str(exc)) from exc
            state = WorkflowState.for_plan(run_id, plan, initial_variables=initial)
            self._store.save_plan(run_id, plan)
        await self._execute(plan, state, registry)
        return {"run_id": run_id, "task_id": None}

    async def start_detached(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]:
        self._compile(workflow_id)
        run_id = self.run_id_for(command_id, workflow_id)
        return await self._detached.accept_after_persisted_state(
            run_id=run_id,
            state_store=self._store,
            operation=self.start(command_id, workflow_id, values),
        )

    async def provide_input(
        self,
        command_id: UUID,
        run_id: str,
        *,
        input_id: str | None,
        values: Any,
    ) -> dict[str, Any]:
        del command_id
        if input_id is None:
            raise CapabilityRunInputError(f"{self.capability_id} has no unnamed input")
        state = self._load_state(run_id)
        plan = self._load_plan(run_id)
        registry = restore_plan_definition_registry(plan)
        resumed = resume_waiting_input(
            plan,
            state,
            input_id=input_id,
            values=values,
            contracts=_contracts(registry),
        )
        return await self._detached.accept_after_persisted_state(
            run_id=run_id,
            state_store=self._store,
            operation=self._accepted(run_id, self._execute(plan, resumed, registry)),
        )

    async def resume(self, command_id: UUID, run_id: str) -> dict[str, Any]:
        del command_id
        state = self._load_state(run_id)
        if state.status is WorkflowStatus.COMPLETED or self._detached.is_active(run_id):
            return {"run_id": run_id, "task_id": None}
        if state.status is WorkflowStatus.WAITING:
            raise CapabilityRunStateError(
                "run is waiting for input; resume it through the input endpoint"
            )
        plan = self._load_plan(run_id)
        registry = restore_plan_definition_registry(plan)
        return await self._detached.accept_after_persisted_state(
            run_id=run_id,
            state_store=self._store,
            operation=self._accepted(run_id, self._execute(plan, state, registry)),
        )

    def cancel(self, run_id: str) -> Awaitable[Any] | None:
        self._load_state(run_id)
        return self._detached.cancel(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        state = self._load_state(run_id)
        waiting_input = [state.waiting_input] if state.waiting_input is not None else []
        return {
            "run": {
                "run_id": run_id,
                "capability_id": self.capability_id,
                "workflow_id": state.workflow_id,
                "status": state.status.value,
                "active": self._detached.is_active(run_id),
                "task_id": None,
            },
            "state": state.model_dump(mode="json"),
            "waiting_input": waiting_input,
        }

    def get_outputs(self, run_id: str) -> dict[str, Any]:
        state = self._load_state(run_id)
        outputs: list[dict[str, Any]] = []
        for output_id, stored in state.outputs.items():
            value = stored.model_dump(mode="json") if hasattr(stored, "model_dump") else stored
            outputs.append({"id": output_id, "kind": "value", "value": value})
            artifacts = value.get("output_artifacts") if isinstance(value, Mapping) else None
            for artifact in artifacts if isinstance(artifacts, list) else ():
                path = str(artifact.get("path", "")) if isinstance(artifact, Mapping) else ""
                if path:
                    outputs.append(self._artifact_output(path))
        return {"run_id": run_id, "outputs": outputs}

    def _artifact_output(self, path: str) -> dict[str, Any]:
        target = Path(path)
        target = target if target.is_absolute() else self.workspace / target
        exists = target.is_file()
        return {
            "id": path,
            "kind": "artifact",
            "path": path,
            "exists": exists,
            "size": target.stat().st_size if exists else 0,
        }

    def get_cost(self, run_id: str) -> dict[str, Any]:
        self._load_state(run_id)
        return {
            "run_id": run_id,
            "usage": UsageLedger(self.workspace, run_id).summarize(group_by="stage"),
        }

    async def close(self) -> None:
        await self._detached.close()

    def _compile(self, workflow_id: str) -> tuple[DefinitionRegistry, ResolvedPlan]:
        if workflow_id not in self.entrypoints:
            raise ValueError(f"workflow is not runnable: {workflow_id}")
        _capability, registry = load_capability(self.capability_file)
        workflow = registry.require(DefinitionKind.WORKFLOW, workflow_id)
        if not isinstance(workflow, WorkflowDefinition):
            raise TypeError(f"definition is not a workflow: {workflow_id}")
        return registry, WorkflowCompiler(self._executors).compile(workflow, registry)

    async def _execute(
        self,
        plan: ResolvedPlan,
        state: WorkflowState,
        registry: DefinitionRegistry,
    ) -> WorkflowState:
        contracts = _contracts(registry)
        run_id = state.run_id
        tools = self._tools(plan, contracts, run_id)
        factory = self._tool_factory(contracts, run_id)
        agent_ids = _agent_ids(plan)
        invoker = None
        if agent_ids:
            if self.services is None or self._execution is None:
                raise CapabilityRunStateError(
                    f"{self.capability_id} needs model services to run its Agents"
                )
            invoker = DefinitionAgentInvoker(
                self.services,
                workflow_id=f"{self.capability_id}:{run_id}",
                run_id=run_id,
                definitions=registry,
                contracts=contracts,
                tool_factory=factory.build,
                execution=self._execution,
                **(
                    {"loop_builder": self._loop_builder}
                    if self._loop_builder is not None
                    else {}
                ),
            )
        try:
            return await WorkflowRuntimeHost(
                self._executors,
                self._store,
                FileWorkflowEventSink(self.workspace),
            ).execute(
                plan,
                state,
                RuntimeContext(
                    tools=tools,
                    contracts=contracts,
                    agents={agent_id: invoker for agent_id in agent_ids},
                    definitions=registry,
                    conversations=ConversationRegistry(
                        FileConversationStore(self.workspace)
                    ),
                    plan_tool_factory=lambda definition, plan_contracts: (
                        self._tool_factory(plan_contracts, run_id).build(definition)
                    ),
                ),
            )
        finally:
            if invoker is not None:
                await invoker.close()

    @staticmethod
    async def _accepted(run_id: str, execution: Awaitable[Any]) -> dict[str, Any]:
        await execution
        return {"run_id": run_id, "task_id": None}

    def _tool_factory(
        self,
        contracts: Mapping[str, ContractAdapter],
        run_id: str | None,
    ) -> CapabilityToolAdapterFactory:
        return CapabilityToolAdapterFactory(
            self.capability_id,
            self.tool_implementations,
            contracts,
            RunToolResultIndex(self.workspace, run_id) if run_id is not None else None,
        )

    def _tools(
        self,
        plan: ResolvedPlan,
        contracts: Mapping[str, ContractAdapter],
        run_id: str | None,
    ) -> dict[str, CapabilityToolAdapter]:
        """Bind exactly the Tools the frozen plan and its children reference."""

        registry = restore_plan_definition_registry(plan)
        factory = self._tool_factory(contracts, run_id)
        tools: dict[str, CapabilityToolAdapter] = {}
        for tool_id in _plan_tool_ids(plan):
            definition = registry.require(DefinitionKind.TOOL, tool_id)
            if isinstance(definition, ToolDefinition):
                tools[tool_id] = factory.build(definition)
        return tools

    def _load_state(self, run_id: str) -> WorkflowState:
        try:
            state = self._store.load(run_id)
        except FileNotFoundError as exc:
            raise CapabilityRunNotFoundError(run_id) from exc
        if state.workflow_id not in self.entrypoints:
            raise CapabilityRunNotFoundError(run_id)
        return state

    def _load_plan(self, run_id: str) -> ResolvedPlan:
        try:
            return self._store.load_plan(run_id)
        except FileNotFoundError as exc:
            raise CapabilityRunNotFoundError(run_id) from exc


def _contracts(registry: DefinitionRegistry) -> dict[str, ContractAdapter]:
    return {
        definition.id: build_contract_adapter(definition)
        for definition in registry.all(DefinitionKind.CONTRACT)
        if isinstance(definition, ContractDefinition)
    }


def _plan_tool_ids(plan: ResolvedPlan) -> list[str]:
    tool_ids: list[str] = []
    for candidate in (plan, *plan.subworkflow_plans.values()):
        for tool_id in candidate.tool_ids:
            if tool_id not in tool_ids:
                tool_ids.append(tool_id)
    return tool_ids


def _agent_ids(plan: ResolvedPlan) -> list[str]:
    agent_ids: list[str] = []
    pending = [plan]
    while pending:
        current = pending.pop()
        for agent_id in current.agent_ids:
            if agent_id not in agent_ids:
                agent_ids.append(agent_id)
        pending.extend(current.subworkflow_plans.values())
    return agent_ids


__all__ = ["GenericCapabilityRuntimeBinding", "StartInput", "ToolImplementations"]
