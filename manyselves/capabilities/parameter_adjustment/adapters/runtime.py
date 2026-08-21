"""Generic runtime binding for the parameter-adjustment Capability."""

import asyncio
from pathlib import Path
from threading import Thread
from typing import Any
from uuid import UUID

from manyselves.core.usage_ledger import UsageLedger
from manyselves.kernel.contracts import ContractAdapter, build_contract_adapter
from manyselves.kernel.conversations import ConversationRegistry
from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionKind,
    DefinitionRegistry,
    ToolDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import (
    RuntimeContext,
    build_builtin_executor_registry,
)
from manyselves.kernel.ports import AgentInvocationOutcome
from manyselves.kernel.workflow import (
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
    resume_waiting_input,
)
from manyselves.runtime.capability_binding import (
    CapabilityRunInputError,
    CapabilityRunNotFoundError,
)
from manyselves.runtime.conversation_store import FileConversationStore
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.tool_adapter import (
    CapabilityToolAdapter,
    CapabilityToolAdapterFactory,
)
from manyselves.runtime.workflow_host import FileWorkflowEventSink, WorkflowRuntimeHost

from .. import load_parameter_adjustment_capability

_UNSET = object()


def _normalize_parameter(arguments: dict[str, Any]) -> int:
    return arguments["value"]


class _ParameterAdjuster:
    async def invoke(
        self,
        _agent: Any,
        _task: Any,
        _value: Any,
        _conversation: Any,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        return AgentInvocationOutcome(result={"value": 10})


class ParameterAdjustmentRuntimeBinding:
    """Execute and project one neutral Capability through the generic Run port."""

    capability_id = "parameter-adjustment"

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self._store = FileWorkflowStateStore(self.workspace)
        self._executors = build_builtin_executor_registry()

    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]:
        if workflow_id != self.capability_id:
            raise ValueError(f"workflow is not runnable: {workflow_id}")
        _capability, registry, contracts, tools, plan = self._compiled(values)
        run_id = f"{workflow_id}-{command_id.hex}"
        try:
            state = self._store.load(run_id)
        except FileNotFoundError:
            state = WorkflowState.for_plan(run_id, plan)
            self._store.save_plan(run_id, plan)
        await self._execute(plan, state, registry, contracts, tools)
        return {"run_id": run_id, "task_id": None}

    def provide_input(
        self,
        command_id: UUID,
        run_id: str,
        *,
        input_id: str | None,
        values: Any,
    ) -> dict[str, Any]:
        del command_id
        if input_id is None:
            raise CapabilityRunInputError("parameter-adjustment has no unnamed input")
        try:
            state = self._store.load(run_id)
            plan = self._store.load_plan(run_id)
        except FileNotFoundError as exc:
            raise CapabilityRunNotFoundError(run_id) from exc
        _capability, registry = load_parameter_adjustment_capability()
        contracts = self._contracts(registry)
        tools = self._tools(registry, contracts)
        resumed = resume_waiting_input(
            plan,
            state,
            input_id=input_id,
            values=values,
            contracts=contracts,
        )
        self._store.save(resumed)
        self._run_coroutine(self._execute(plan, resumed, registry, contracts, tools))
        return {"run_id": run_id, "task_id": None}

    def get_run(self, run_id: str) -> dict[str, Any]:
        state = self._load_state(run_id)
        waiting_input = [state.waiting_input] if state.waiting_input is not None else []
        return {
            "run": {
                "run_id": run_id,
                "capability_id": self.capability_id,
                "workflow_id": state.workflow_id,
                "status": state.status.value,
                "active": state.status
                in {
                    WorkflowStatus.PENDING,
                    WorkflowStatus.RUNNING,
                },
                "task_id": None,
            },
            "state": state.model_dump(mode="json"),
            "waiting_input": waiting_input,
        }

    def get_outputs(self, run_id: str) -> dict[str, Any]:
        state = self._load_state(run_id)
        return {
            "run_id": run_id,
            "outputs": [
                {"id": output_id, "kind": "value", "value": value}
                for output_id, value in state.outputs.items()
            ],
        }

    def get_cost(self, run_id: str) -> dict[str, Any]:
        self._load_state(run_id)
        return {
            "run_id": run_id,
            "usage": UsageLedger(self.workspace, run_id).summarize(group_by="stage"),
        }

    def _compiled(
        self,
        values: Any = _UNSET,
    ) -> tuple[
        Any,
        DefinitionRegistry,
        dict[str, ContractAdapter],
        dict[str, CapabilityToolAdapter],
        Any,
    ]:
        capability, registry = load_parameter_adjustment_capability()
        workflow = registry.require(DefinitionKind.WORKFLOW, self.capability_id)
        if not isinstance(workflow, WorkflowDefinition):
            raise TypeError(f"definition is not a workflow: {self.capability_id}")
        contracts = self._contracts(registry)
        if values is not _UNSET:
            workflow = workflow.model_copy(deep=True)
            workflow.state = {
                "parameters": contracts["parameter-input"].validate(values)
            }
        tools = self._tools(registry, contracts)
        plan = WorkflowCompiler(self._executors).compile(workflow, registry)
        return capability, registry, contracts, tools, plan

    async def _execute(
        self,
        plan: Any,
        state: WorkflowState,
        registry: DefinitionRegistry,
        contracts: dict[str, ContractAdapter],
        tools: dict[str, CapabilityToolAdapter],
    ) -> WorkflowState:
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
                agents={"parameter-adjuster": _ParameterAdjuster()},
                definitions=registry,
                conversations=ConversationRegistry(
                    FileConversationStore(self.workspace)
                ),
            ),
        )

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
        contracts: dict[str, ContractAdapter],
    ) -> dict[str, CapabilityToolAdapter]:
        factory = CapabilityToolAdapterFactory(
            self.capability_id,
            {"normalize-parameter": _normalize_parameter},
            contracts,
        )
        return {
            definition.id: factory.build(definition)
            for definition in registry.all(DefinitionKind.TOOL)
            if isinstance(definition, ToolDefinition)
        }

    def _load_state(self, run_id: str) -> WorkflowState:
        try:
            state = self._store.load(run_id)
        except FileNotFoundError as exc:
            raise CapabilityRunNotFoundError(run_id) from exc
        if state.workflow_id != self.capability_id:
            raise CapabilityRunNotFoundError(run_id)
        return state

    @staticmethod
    def _run_coroutine(coroutine):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)

        result: list[Any] = []
        error: list[BaseException] = []

        def run_in_thread() -> None:
            try:
                result.append(asyncio.run(coroutine))
            except BaseException as exc:
                error.append(exc)

        thread = Thread(target=run_in_thread)
        thread.start()
        thread.join()
        if error:
            raise error[0]
        return result[0]


def build_runtime_binding(
    *,
    workspace: Path,
    host: Any,
) -> ParameterAdjustmentRuntimeBinding:
    """Build the Capability binding without depending on the application host."""

    del host
    return ParameterAdjustmentRuntimeBinding(workspace)


__all__ = ["ParameterAdjustmentRuntimeBinding", "build_runtime_binding"]
