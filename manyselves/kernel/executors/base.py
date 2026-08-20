"""Action executor interface and registry."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any, Protocol, cast

from manyselves.kernel.contracts import ContractAdapter
from manyselves.kernel.conversations import (
    ConversationKey,
    ConversationRecord,
    ConversationRegistry,
)
from manyselves.kernel.definitions import (
    AgentDefinition,
    DefinitionKind,
    DefinitionRegistry,
    TaskDefinition,
)
from manyselves.kernel.ports.agent import AgentInvocationOutcome
from manyselves.kernel.ports.tool import ToolInvocationOutcome
from manyselves.kernel.workflow import (
    ActionKind,
    CreateConversationAction,
    EndWorkflowAction,
    InvokeAgentAction,
    InvokeToolAction,
    ResolveConversationAction,
    ResolvedAction,
    ResolvedPlan,
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
    agents: Mapping[str, Any] = field(default_factory=dict)
    definitions: DefinitionRegistry | None = None
    conversations: ConversationRegistry = field(default_factory=ConversationRegistry)
    subworkflows: Mapping[str, ResolvedPlan] = field(default_factory=dict)


@dataclass(slots=True)
class ActionResult:
    output: Any = None
    variable_updates: dict[str, Any] = field(default_factory=dict)
    output_updates: dict[str, Any] = field(default_factory=dict)
    conversation_updates: dict[str, ConversationRecord] = field(default_factory=dict)
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
        arguments = (
            state.variables[resolved.input_variable]
            if resolved.input_variable is not None
            else {
                name: state.variables[variable]
                for name, variable in resolved.input_variables.items()
            }
        )
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
            raise RuntimeExecutionError(f"missing contract adapter: {resolved.contract}") from exc
        output = contract.validate(state.variables[resolved.input_variable])
        return ActionResult(
            output=output,
            variable_updates={resolved.output_variable: output},
        )


class CreateConversationExecutor:
    kind = ActionKind.CREATE_CONVERSATION

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        resolved = cast(CreateConversationAction, action)
        _restore_conversations(state, context.conversations)
        record = context.conversations.create_or_resolve(
            ConversationKey(
                agent_id=resolved.agent,
                value=resolved.conversation_key,
                mode=resolved.mode,
            ),
            run_id=state.run_id,
        )
        return ActionResult(
            output=record,
            variable_updates={resolved.output_variable: record},
            conversation_updates={resolved.output_variable: record},
        )


class ResolveConversationExecutor:
    kind = ActionKind.RESOLVE_CONVERSATION

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        resolved = cast(ResolveConversationAction, action)
        _restore_conversations(state, context.conversations)
        record = context.conversations.resolve(
            ConversationKey(
                agent_id=resolved.agent,
                value=resolved.conversation_key,
                mode=resolved.mode,
            ),
            run_id=state.run_id,
        )
        if record is None:
            raise RuntimeExecutionError(
                f"conversation is not registered: {resolved.agent}:{resolved.conversation_key}"
            )
        return ActionResult(
            output=record,
            variable_updates={resolved.output_variable: record},
            conversation_updates={resolved.output_variable: record},
        )


class InvokeAgentExecutor:
    kind = ActionKind.INVOKE_AGENT

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        resolved = cast(InvokeAgentAction, action)
        if context.definitions is None:
            raise RuntimeExecutionError("agent execution requires definitions")
        agent = context.definitions.require(DefinitionKind.AGENT, resolved.agent)
        task = context.definitions.require(DefinitionKind.TASK, resolved.task)
        if not isinstance(agent, AgentDefinition) or not isinstance(task, TaskDefinition):
            raise RuntimeExecutionError("agent action resolved invalid definitions")
        try:
            invoker = context.agents[agent.id]
        except KeyError as exc:
            raise RuntimeExecutionError(f"missing agent adapter: {agent.id}") from exc
        conversation = state.variables[resolved.conversation_variable]
        if not isinstance(conversation, ConversationRecord):
            conversation = ConversationRecord.model_validate(conversation)
        value = state.variables[resolved.input_variable]
        try:
            input_contract = context.contracts[task.input_contract]
            output_contract = context.contracts[task.output_contract]
        except KeyError as exc:
            raise RuntimeExecutionError(f"missing agent contract adapter: {exc.args[0]}") from exc
        validated_input = input_contract.validate(value)
        outcome = invoker.invoke(
            agent,
            task,
            validated_input,
            conversation,
            task_id=resolved.id,
        )
        if isawaitable(outcome):
            outcome = await outcome
        if not isinstance(outcome, AgentInvocationOutcome):
            raise RuntimeExecutionError(f"agent adapter returned an invalid outcome: {agent.id}")
        if outcome.status != "ok":
            raise RuntimeExecutionError(
                outcome.error or f"agent {agent.id} returned {outcome.status}"
            )
        validated_output = output_contract.validate(outcome.result)
        return ActionResult(
            output=outcome.model_dump(mode="json"),
            variable_updates={resolved.output_variable: validated_output},
            conversation_updates={resolved.conversation_variable: conversation},
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


def _restore_conversations(
    state: WorkflowState,
    registry: ConversationRegistry,
) -> None:
    for value in state.conversations.values():
        record = (
            value
            if isinstance(value, ConversationRecord)
            else ConversationRecord.model_validate(value)
        )
        registry.remember(record)


def build_builtin_executor_registry() -> ExecutorRegistry:
    registry = ExecutorRegistry()
    registry.register(SetVariableExecutor())
    registry.register(InvokeToolExecutor())
    registry.register(CreateConversationExecutor())
    registry.register(ResolveConversationExecutor())
    registry.register(InvokeAgentExecutor())
    registry.register(ValidateContractExecutor())
    registry.register(EndWorkflowExecutor())
    return registry
