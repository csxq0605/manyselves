"""Action executor interface and registry."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any, Protocol, cast

from manyselves.kernel.contracts import ContractAdapter
from manyselves.kernel.ports.tool import ToolInvocationOutcome
from manyselves.kernel.workflow import (
    ActionKind,
    EndWorkflowAction,
    InvokeToolAction,
    ResolvedAction,
    SetVariableAction,
    ValidateContractAction,
    WorkflowState,
    WorkflowStatus,
)


class RuntimeExecutionError(RuntimeError):
    """Raised when a resolved action cannot execute in its runtime context."""


@dataclass(slots=True)
class RuntimeContext:
    tools: Mapping[str, Callable[[Any], Any]] = field(default_factory=dict)
    contracts: Mapping[str, ContractAdapter] = field(default_factory=dict)


@dataclass(slots=True)
class ActionResult:
    output: Any = None
    variable_updates: dict[str, Any] = field(default_factory=dict)
    output_updates: dict[str, Any] = field(default_factory=dict)
    workflow_status: WorkflowStatus | None = None


class ActionExecutor(Protocol):
    kind: ActionKind

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult: ...


class ExecutorRegistry:
    """Map each resolved action kind to one business-neutral executor."""

    def __init__(self) -> None:
        self._executors: dict[ActionKind, ActionExecutor] = {}

    def register(self, executor: ActionExecutor) -> None:
        self._executors[executor.kind] = executor

    def has(self, kind: ActionKind | str) -> bool:
        return ActionKind(kind) in self._executors

    def require(self, kind: ActionKind | str) -> ActionExecutor:
        normalized = ActionKind(kind)
        try:
            return self._executors[normalized]
        except KeyError as exc:
            raise RuntimeExecutionError(f"missing executor: {normalized}") from exc


class SetVariableExecutor:
    kind = ActionKind.SET_VARIABLE

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        resolved = cast(SetVariableAction, action)
        return ActionResult(
            output=resolved.value,
            variable_updates={resolved.variable: resolved.value},
        )


class InvokeToolExecutor:
    kind = ActionKind.INVOKE_TOOL

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        resolved = cast(InvokeToolAction, action)
        try:
            tool = context.tools[resolved.tool]
        except KeyError as exc:
            raise RuntimeExecutionError(f"missing tool adapter: {resolved.tool}") from exc
        arguments = state.variables[resolved.input_variable]
        invoke = getattr(tool, "invoke", None)
        if callable(invoke):
            outcome = invoke(arguments, task_id=resolved.id)
            if isawaitable(outcome):
                outcome = await outcome
            if not isinstance(outcome, ToolInvocationOutcome):
                raise RuntimeExecutionError(
                    f"tool adapter returned an invalid outcome: {resolved.tool}"
                )
        else:
            output = tool(arguments)
            if isawaitable(output):
                output = await output
            outcome = ToolInvocationOutcome(result=output)
        if outcome.status != "ok":
            raise RuntimeExecutionError(
                outcome.error or f"tool {resolved.tool} returned {outcome.status}"
            )
        return ActionResult(
            output=outcome.model_dump(mode="json"),
            variable_updates={resolved.output_variable: outcome.result},
        )


class ValidateContractExecutor:
    kind = ActionKind.VALIDATE_CONTRACT

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        resolved = cast(ValidateContractAction, action)
        try:
            contract = context.contracts[resolved.contract]
        except KeyError as exc:
            raise RuntimeExecutionError(
                f"missing contract adapter: {resolved.contract}"
            ) from exc
        output = contract.validate(state.variables[resolved.input_variable])
        return ActionResult(
            output=output,
            variable_updates={resolved.output_variable: output},
        )


class EndWorkflowExecutor:
    kind = ActionKind.END_WORKFLOW

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        resolved = cast(EndWorkflowAction, action)
        output = state.variables[resolved.output_variable]
        return ActionResult(
            output=output,
            output_updates={resolved.output_name: output},
            workflow_status=WorkflowStatus.COMPLETED,
        )


def build_builtin_executor_registry() -> ExecutorRegistry:
    registry = ExecutorRegistry()
    registry.register(SetVariableExecutor())
    registry.register(InvokeToolExecutor())
    registry.register(ValidateContractExecutor())
    registry.register(EndWorkflowExecutor())
    return registry
