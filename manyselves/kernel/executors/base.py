"""Action executor interface and registry."""

import ast
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any, Protocol, cast

from manyselves.kernel.contracts import ContractAdapter, ContractValidationError
from manyselves.kernel.conversations import (
    ConversationKey,
    ConversationRecord,
    ConversationRegistry,
)
from manyselves.kernel.definitions import (
    AgentDefinition,
    DefinitionKind,
    DefinitionRegistry,
    GateDefinition,
    InteractionDefinition,
    OutputDefinition,
    RecoveryPolicyDefinition,
    TaskDefinition,
)
from manyselves.kernel.ports.agent import AgentInvocationOutcome
from manyselves.kernel.ports.tool import ToolInvocationOutcome
from manyselves.kernel.workflow import (
    ActionKind,
    AppendVariableAction,
    CreateConversationAction,
    EndWorkflowAction,
    EvaluateGateAction,
    FailWorkflowAction,
    InvokeAgentAction,
    InvokeToolAction,
    MergeVariableAction,
    PublishResultAction,
    RequestInputAction,
    ResetConversationAction,
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
    parallel_result_updates: dict[str, dict[str, dict[str, Any]]] = field(
        default_factory=dict
    )
    parallel_state_updates: dict[str, dict[str, dict[str, Any]]] = field(
        default_factory=dict
    )
    subworkflow_state_updates: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_action_id: str | None = None
    waiting_input: dict[str, Any] | None = None
    clear_waiting_input: bool = False
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


class AppendVariableExecutor:
    kind = ActionKind.APPEND_VARIABLE

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        resolved = cast(AppendVariableAction, action)
        current = state.variables[resolved.variable]
        if not isinstance(current, list):
            raise RuntimeExecutionError(
                f"append_variable {resolved.id} requires a list: {resolved.variable}"
            )
        value = (
            state.variables[resolved.value_variable]
            if resolved.value_variable is not None
            else resolved.value
        )
        updated = [*deepcopy(current), deepcopy(value)]
        return ActionResult(output=updated, variable_updates={resolved.variable: updated})


class MergeVariableExecutor:
    kind = ActionKind.MERGE_VARIABLE

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        resolved = cast(MergeVariableAction, action)
        current = state.variables[resolved.variable]
        value = (
            state.variables[resolved.value_variable]
            if resolved.value_variable is not None
            else resolved.value
        )
        if not isinstance(current, Mapping) or not isinstance(value, Mapping):
            raise RuntimeExecutionError(
                f"merge_variable {resolved.id} requires object values: {resolved.variable}"
            )
        updated = {**deepcopy(dict(current)), **deepcopy(dict(value))}
        return ActionResult(output=updated, variable_updates={resolved.variable: updated})


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


class ResetConversationExecutor:
    kind = ActionKind.RESET_CONVERSATION

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        resolved = cast(ResetConversationAction, action)
        _restore_conversations(state, context.conversations)
        record = context.conversations.reset(
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
        recovery_policy = None
        if task.recovery is not None:
            recovery = context.definitions.require(
                DefinitionKind.RECOVERY,
                task.recovery,
            )
            if not isinstance(recovery, RecoveryPolicyDefinition):
                raise RuntimeExecutionError(
                    f"definition is not a recovery policy: {task.recovery}"
                )
            recovery_policy = recovery
        try:
            invoker = context.agents[agent.id]
        except KeyError as exc:
            raise RuntimeExecutionError(f"missing agent adapter: {agent.id}") from exc
        conversation = state.variables[resolved.conversation_variable]
        if not isinstance(conversation, ConversationRecord):
            conversation = ConversationRecord.model_validate(conversation)
        if conversation.key.agent_id != agent.id:
            raise RuntimeExecutionError(
                f"conversation belongs to agent {conversation.key.agent_id}, not {agent.id}"
            )
        value = state.variables[resolved.input_variable]
        try:
            input_contract = context.contracts[task.input_contract]
            output_contract = context.contracts[task.output_contract]
        except KeyError as exc:
            raise RuntimeExecutionError(f"missing agent contract adapter: {exc.args[0]}") from exc
        validated_input = input_contract.validate(value)
        recovery_invoke = getattr(invoker, "invoke_with_recovery", None)
        if recovery_policy is not None:
            if not callable(recovery_invoke):
                raise RuntimeExecutionError(
                    f"agent adapter does not support declared recovery: {agent.id}"
                )
            outcome = recovery_invoke(
                agent,
                task,
                validated_input,
                conversation,
                task_id=resolved.id,
                recovery_policy=recovery_policy,
            )
        else:
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


class RequestInputExecutor:
    kind = ActionKind.REQUEST_INPUT

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        resolved = cast(RequestInputAction, action)
        if context.definitions is None:
            raise RuntimeExecutionError("input execution requires definitions")
        interaction = context.definitions.require(
            DefinitionKind.INTERACTION,
            resolved.interaction,
        )
        if not isinstance(interaction, InteractionDefinition):
            raise RuntimeExecutionError(
                f"definition is not an interaction: {resolved.interaction}"
            )
        try:
            contract = context.contracts[interaction.input_contract]
        except KeyError as exc:
            raise RuntimeExecutionError(
                f"missing interaction contract adapter: {interaction.input_contract}"
            ) from exc
        if resolved.output_variable in state.variables:
            value = contract.validate(state.variables[resolved.output_variable])
            return ActionResult(
                output=value,
                variable_updates={resolved.output_variable: value},
                clear_waiting_input=True,
            )
        waiting = {
            "input_id": resolved.id,
            "interaction_id": interaction.id,
            "interaction_type": interaction.interaction_type,
            "contract_id": interaction.input_contract,
            "title": interaction.title,
            "description": interaction.description,
            "submit_label": interaction.submit_label,
            "schema": contract.json_schema(),
        }
        return ActionResult(
            output=waiting,
            waiting_input=waiting,
            workflow_status=WorkflowStatus.WAITING,
        )


class WaitInputExecutor(RequestInputExecutor):
    """Explicit wait spelling with the same persisted interaction protocol."""

    kind = ActionKind.WAIT_INPUT


class EvaluateGateExecutor:
    kind = ActionKind.EVALUATE_GATE

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        resolved = cast(EvaluateGateAction, action)
        if context.definitions is None:
            raise RuntimeExecutionError("gate execution requires definitions")
        definition = context.definitions.require(DefinitionKind.GATE, resolved.gate)
        if not isinstance(definition, GateDefinition):
            raise RuntimeExecutionError(f"definition is not a gate: {resolved.gate}")
        value = state.variables[resolved.input_variable]
        status = "pass"
        if definition.contract is not None:
            try:
                value = context.contracts[definition.contract].validate(value)
            except KeyError as exc:
                raise RuntimeExecutionError(
                    f"missing gate contract adapter: {definition.contract}"
                ) from exc
            except ContractValidationError:
                status = "fail"
        if status == "pass" and definition.expression is not None:
            status = (
                "pass"
                if _evaluate_gate_expression(definition.expression, value)
                else "fail"
            )
        if status == "pass" and definition.validator_tool is not None:
            status = await _invoke_gate_validator(
                context,
                definition.validator_tool,
                value,
                task_id=resolved.id,
            )
        target = {
            "pass": definition.on_pass,
            "fail": definition.on_fail,
            "wait": definition.on_wait,
        }[status]
        result = {"status": status, "value": value}
        return ActionResult(
            output=result,
            variable_updates={resolved.output_variable: result},
            next_action_id=target,
        )


class FailWorkflowExecutor:
    kind = ActionKind.FAIL_WORKFLOW

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        resolved = cast(FailWorkflowAction, action)
        message = (
            str(state.variables[resolved.error_variable])
            if resolved.error_variable is not None
            else resolved.message
        )
        raise RuntimeExecutionError(message or f"workflow failed at {resolved.id}")


class PublishResultExecutor:
    kind = ActionKind.PUBLISH_RESULT

    async def execute(
        self,
        action: ResolvedAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        resolved = cast(PublishResultAction, action)
        if context.definitions is None:
            raise RuntimeExecutionError("output execution requires definitions")
        definition = context.definitions.require(
            DefinitionKind.OUTPUT,
            resolved.output,
        )
        if not isinstance(definition, OutputDefinition):
            raise RuntimeExecutionError(
                f"definition is not an output: {resolved.output}"
            )
        value = state.variables[resolved.input_variable]
        if definition.contract is not None:
            try:
                value = context.contracts[definition.contract].validate(value)
            except KeyError as exc:
                raise RuntimeExecutionError(
                    f"missing output contract adapter: {definition.contract}"
                ) from exc
        return ActionResult(
            output=value,
            output_updates={resolved.output_name: value},
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
        if resolved.output_contract is not None:
            try:
                output = context.contracts[resolved.output_contract].validate(output)
            except KeyError as exc:
                raise RuntimeExecutionError(
                    f"missing final output contract adapter: {resolved.output_contract}"
                ) from exc
            except ContractValidationError as exc:
                raise RuntimeExecutionError(
                    f"final output contract {resolved.output_contract} rejected the result"
                ) from exc
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


def _evaluate_gate_expression(expression: str, value: Any) -> bool:
    def evaluate(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name) and node.id == "value":
            return value
        if isinstance(node, ast.Subscript):
            return evaluate(node.value)[evaluate(node.slice)]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not evaluate(node.operand)
        if isinstance(node, ast.BoolOp):
            values = [bool(evaluate(item)) for item in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare):
            left = evaluate(node.left)
            for operator, comparator in zip(node.ops, node.comparators, strict=True):
                right = evaluate(comparator)
                matched = (
                    left == right
                    if isinstance(operator, ast.Eq)
                    else left != right
                    if isinstance(operator, ast.NotEq)
                    else left < right
                    if isinstance(operator, ast.Lt)
                    else left <= right
                    if isinstance(operator, ast.LtE)
                    else left > right
                    if isinstance(operator, ast.Gt)
                    else left >= right
                    if isinstance(operator, ast.GtE)
                    else left in right
                    if isinstance(operator, ast.In)
                    else left not in right
                    if isinstance(operator, ast.NotIn)
                    else None
                )
                if matched is None:
                    raise RuntimeExecutionError("unsupported gate comparison")
                if not matched:
                    return False
                left = right
            return True
        raise RuntimeExecutionError("unsupported gate expression")

    try:
        return bool(evaluate(ast.parse(expression, mode="eval")))
    except (SyntaxError, KeyError, TypeError) as exc:
        raise RuntimeExecutionError(f"invalid gate expression: {expression}") from exc


async def _invoke_gate_validator(
    context: RuntimeContext,
    tool_id: str,
    value: Any,
    *,
    task_id: str,
) -> str:
    try:
        tool = context.tools[tool_id]
    except KeyError as exc:
        raise RuntimeExecutionError(f"missing gate validator adapter: {tool_id}") from exc
    invoke = getattr(tool, "invoke", None)
    outcome = invoke(value, task_id=task_id) if callable(invoke) else tool(value)
    if isawaitable(outcome):
        outcome = await outcome
    if isinstance(outcome, ToolInvocationOutcome):
        if outcome.status != "ok":
            raise RuntimeExecutionError(
                outcome.error or f"gate validator {tool_id} returned {outcome.status}"
            )
        outcome = outcome.result
    if isinstance(outcome, Mapping):
        if outcome.get("status") in {"pass", "fail", "wait"}:
            return str(outcome["status"])
        if "passed" in outcome:
            return "pass" if bool(outcome["passed"]) else "fail"
    if isinstance(outcome, str) and outcome in {"pass", "fail", "wait"}:
        return outcome
    return "pass" if bool(outcome) else "fail"


def build_builtin_executor_registry() -> ExecutorRegistry:
    registry = ExecutorRegistry()
    registry.register(SetVariableExecutor())
    registry.register(AppendVariableExecutor())
    registry.register(MergeVariableExecutor())
    registry.register(InvokeToolExecutor())
    registry.register(CreateConversationExecutor())
    registry.register(ResolveConversationExecutor())
    registry.register(ResetConversationExecutor())
    registry.register(InvokeAgentExecutor())
    registry.register(ValidateContractExecutor())
    registry.register(EvaluateGateExecutor())
    registry.register(RequestInputExecutor())
    registry.register(WaitInputExecutor())
    registry.register(PublishResultExecutor())
    registry.register(FailWorkflowExecutor())
    registry.register(EndWorkflowExecutor())
    return registry
