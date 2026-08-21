"""Minimal sequential compiler for the first neutral workflow slice."""

from typing import Any, Protocol

from pydantic import ValidationError

from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.definitions import (
    AgentDefinition,
    ContractDefinition,
    DefinitionKind,
    DefinitionReferenceError,
    DefinitionRegistry,
    GateDefinition,
    InteractionDefinition,
    OutputDefinition,
    RecoveryPolicyDefinition,
    TaskDefinition,
    ToolDefinition,
    WorkflowDefinition,
)

from .models import (
    ActionKind,
    AppendVariableAction,
    ConditionGroupAction,
    CreateConversationAction,
    EndWorkflowAction,
    EvaluateGateAction,
    FailWorkflowAction,
    ForEachAction,
    GotoAction,
    IfAction,
    InvokeAgentAction,
    InvokeToolAction,
    JoinAction,
    MergeVariableAction,
    ParallelAction,
    PublishResultAction,
    RequestInputAction,
    ResetConversationAction,
    ResolveConversationAction,
    ResolvedAction,
    ResolvedPlan,
    SetVariableAction,
    SubworkflowAction,
    ValidateContractAction,
    WaitInputAction,
)


class CompilerError(ValueError):
    """Raised when a workflow cannot become an executable resolved plan."""


class ActionKindRegistry(Protocol):
    def has(self, kind: ActionKind | str) -> bool: ...


_ACTION_MODELS: dict[ActionKind, type[Any]] = {
    ActionKind.SET_VARIABLE: SetVariableAction,
    ActionKind.APPEND_VARIABLE: AppendVariableAction,
    ActionKind.MERGE_VARIABLE: MergeVariableAction,
    ActionKind.INVOKE_TOOL: InvokeToolAction,
    ActionKind.CREATE_CONVERSATION: CreateConversationAction,
    ActionKind.RESOLVE_CONVERSATION: ResolveConversationAction,
    ActionKind.RESET_CONVERSATION: ResetConversationAction,
    ActionKind.INVOKE_AGENT: InvokeAgentAction,
    ActionKind.IF: IfAction,
    ActionKind.CONDITION_GROUP: ConditionGroupAction,
    ActionKind.GOTO: GotoAction,
    ActionKind.FOR_EACH: ForEachAction,
    ActionKind.PARALLEL: ParallelAction,
    ActionKind.JOIN: JoinAction,
    ActionKind.SUBWORKFLOW: SubworkflowAction,
    ActionKind.VALIDATE_CONTRACT: ValidateContractAction,
    ActionKind.EVALUATE_GATE: EvaluateGateAction,
    ActionKind.REQUEST_INPUT: RequestInputAction,
    ActionKind.WAIT_INPUT: WaitInputAction,
    ActionKind.PUBLISH_RESULT: PublishResultAction,
    ActionKind.FAIL_WORKFLOW: FailWorkflowAction,
    ActionKind.END_WORKFLOW: EndWorkflowAction,
}

_CONTROL_ACTION_KINDS = {
    ActionKind.IF,
    ActionKind.CONDITION_GROUP,
    ActionKind.GOTO,
    ActionKind.FOR_EACH,
    ActionKind.PARALLEL,
    ActionKind.JOIN,
    ActionKind.SUBWORKFLOW,
}


class WorkflowCompiler:
    """Resolve the WP-02 action subset without executing definitions."""

    def __init__(self, executors: ActionKindRegistry) -> None:
        self._executors = executors

    def compile(
        self,
        workflow: WorkflowDefinition,
        definitions: DefinitionRegistry,
    ) -> ResolvedPlan:
        actions: list[ResolvedAction] = []
        action_ids: set[str] = set()
        defined_variables: set[str] = set(workflow.state)
        tool_ids: list[str] = []
        agent_tool_ids: list[str] = []
        tool_implementations: dict[str, str] = {}
        agent_ids: list[str] = []
        task_ids: list[str] = []
        contract_ids: list[str] = []
        workflow_ids: list[str] = []
        interaction_ids: list[str] = []
        output_ids: list[str] = []
        gate_ids: list[str] = []
        recovery_ids: list[str] = []
        conversation_bindings: dict[str, dict[str, str]] = {}
        conversation_agents: dict[str, str] = {}
        variable_contracts: dict[str, str] = {}
        end_actions: list[EndWorkflowAction] = []

        for recovery_id in workflow.recovery:
            recovery = self._require(
                definitions,
                DefinitionKind.RECOVERY,
                recovery_id,
                workflow.id,
            )
            if not isinstance(recovery, RecoveryPolicyDefinition):
                raise CompilerError(f"definition is not recovery: {recovery_id}")
            _append_unique(recovery_ids, recovery_id)
        for gate_id in workflow.gates:
            self._require(definitions, DefinitionKind.GATE, gate_id, workflow.id)
            _append_unique(gate_ids, gate_id)

        for payload in workflow.actions:
            raw_kind = payload.get("kind")
            try:
                kind = ActionKind(raw_kind)
            except ValueError as exc:
                raise CompilerError(f"unknown action kind: {raw_kind}") from exc
            if kind not in _CONTROL_ACTION_KINDS and not self._executors.has(kind):
                raise CompilerError(f"unregistered action kind: {kind}")
            try:
                action = _ACTION_MODELS[kind].model_validate(payload)
            except ValidationError as exc:
                raise CompilerError(str(exc)) from exc
            if action.id in action_ids:
                raise CompilerError(f"duplicate action id: {action.id}")
            action_ids.add(action.id)
            self._resolve_action(
                action,
                definitions,
                defined_variables,
                tool_ids,
                agent_tool_ids,
                tool_implementations,
                contract_ids,
                agent_ids,
                task_ids,
                workflow_ids,
                interaction_ids,
                output_ids,
                gate_ids,
                recovery_ids,
                conversation_bindings,
                conversation_agents,
                variable_contracts,
            )
            if isinstance(action, EndWorkflowAction):
                end_actions.append(action)
            actions.append(action)

        if not end_actions:
            raise CompilerError("workflow requires one final end_workflow action")
        has_control_flow = any(action.kind in _CONTROL_ACTION_KINDS for action in actions)
        if not has_control_flow and (
            len(end_actions) != 1 or not actions or actions[-1] is not end_actions[0]
        ):
            raise CompilerError(
                "minimal sequential workflow requires one final end_workflow action"
            )
        control_flow_edges = self._validate_control_flow(
            actions,
            workflow.max_iterations,
            definitions,
        )
        if workflow.output_contract:
            self._require(
                definitions,
                DefinitionKind.CONTRACT,
                workflow.output_contract,
                workflow.id,
            )
            _append_unique(contract_ids, workflow.output_contract)
            for action in end_actions:
                self._require_assignable(
                    definitions,
                    variable_contracts.get(action.output_variable),
                    workflow.output_contract,
                    action.id,
                )
        return ResolvedPlan(
            workflow_id=workflow.id,
            workflow_version=workflow.version,
            actions=actions,
            initial_state=workflow.state,
            entry_action_id=actions[0].id,
            max_iterations=workflow.max_iterations,
            tool_ids=tool_ids,
            agent_tool_ids=agent_tool_ids,
            tool_implementations=tool_implementations,
            agent_ids=agent_ids,
            task_ids=task_ids,
            workflow_ids=workflow_ids,
            contract_ids=contract_ids,
            interaction_ids=interaction_ids,
            output_ids=output_ids,
            gate_ids=gate_ids,
            recovery_ids=recovery_ids,
            conversation_bindings=conversation_bindings,
            control_flow_edges=control_flow_edges,
            final_output_contract=workflow.output_contract,
        )

    def _resolve_action(
        self,
        action: ResolvedAction,
        definitions: DefinitionRegistry,
        defined_variables: set[str],
        tool_ids: list[str],
        agent_tool_ids: list[str],
        tool_implementations: dict[str, str],
        contract_ids: list[str],
        agent_ids: list[str],
        task_ids: list[str],
        workflow_ids: list[str],
        interaction_ids: list[str],
        output_ids: list[str],
        gate_ids: list[str],
        recovery_ids: list[str],
        conversation_bindings: dict[str, dict[str, str]],
        conversation_agents: dict[str, str],
        variable_contracts: dict[str, str],
    ) -> None:
        if isinstance(action, SetVariableAction):
            defined_variables.add(action.variable)
            variable_contracts.pop(action.variable, None)
            return
        if isinstance(action, (AppendVariableAction, MergeVariableAction)):
            self._require_variable(action.variable, defined_variables, action.id)
            if action.value_variable is not None:
                self._require_variable(
                    action.value_variable,
                    defined_variables,
                    action.id,
                )
            variable_contracts.pop(action.variable, None)
            return
        if isinstance(action, InvokeToolAction):
            input_variables = (
                [action.input_variable]
                if action.input_variable is not None
                else list(action.input_variables.values())
            )
            for variable in input_variables:
                self._require_variable(variable, defined_variables, action.id)
            tool = self._require(
                definitions,
                DefinitionKind.TOOL,
                action.tool,
                action.id,
            )
            if not isinstance(tool, ToolDefinition):
                raise CompilerError(f"definition is not a tool: {action.tool}")
            if action.input_variable is not None:
                self._require_assignable(
                    definitions,
                    variable_contracts.get(action.input_variable),
                    tool.input_contract,
                    action.id,
                )
            self._require(
                definitions,
                DefinitionKind.CONTRACT,
                tool.input_contract,
                action.id,
            )
            self._require(
                definitions,
                DefinitionKind.CONTRACT,
                tool.output_contract,
                action.id,
            )
            _append_unique(tool_ids, action.tool)
            tool_implementations[tool.id] = tool.implementation
            _append_unique(contract_ids, tool.input_contract)
            _append_unique(contract_ids, tool.output_contract)
            defined_variables.add(action.output_variable)
            variable_contracts[action.output_variable] = tool.output_contract
            return
        if isinstance(
            action,
            (CreateConversationAction, ResolveConversationAction, ResetConversationAction),
        ):
            agent = self._require(
                definitions,
                DefinitionKind.AGENT,
                action.agent,
                action.id,
            )
            if not isinstance(agent, AgentDefinition):
                raise CompilerError(f"definition is not an agent: {action.agent}")
            _append_unique(agent_ids, action.agent)
            defined_variables.add(action.output_variable)
            conversation_agents[action.output_variable] = action.agent
            conversation_bindings[action.id] = {
                "agent": action.agent,
                "conversation_key": action.conversation_key,
                "mode": action.mode,
                "output_variable": action.output_variable,
            }
            return
        if isinstance(action, InvokeAgentAction):
            self._require_variable(action.input_variable, defined_variables, action.id)
            self._require_variable(
                action.conversation_variable,
                defined_variables,
                action.id,
            )
            agent = self._require(
                definitions,
                DefinitionKind.AGENT,
                action.agent,
                action.id,
            )
            task = self._require(
                definitions,
                DefinitionKind.TASK,
                action.task,
                action.id,
            )
            if not isinstance(agent, AgentDefinition):
                raise CompilerError(f"definition is not an agent: {action.agent}")
            if not isinstance(task, TaskDefinition):
                raise CompilerError(f"definition is not a task: {action.task}")
            if task.agent != agent.id:
                raise CompilerError(
                    f"action {action.id} binds task {task.id} to agent {agent.id}, "
                    f"but the task declares {task.agent}"
                )
            conversation_agent = conversation_agents.get(action.conversation_variable)
            if conversation_agent is not None and conversation_agent != agent.id:
                raise CompilerError(
                    f"action {action.id} uses conversation for agent "
                    f"{conversation_agent} with agent {agent.id}"
                )
            self._require_assignable(
                definitions,
                variable_contracts.get(action.input_variable),
                task.input_contract,
                action.id,
            )
            for contract_id in (task.input_contract, task.output_contract):
                self._require(
                    definitions,
                    DefinitionKind.CONTRACT,
                    contract_id,
                    action.id,
                )
                _append_unique(contract_ids, contract_id)
            for tool_id in task.tools:
                tool = self._require(
                    definitions,
                    DefinitionKind.TOOL,
                    tool_id,
                    action.id,
                )
                if not isinstance(tool, ToolDefinition):
                    raise CompilerError(f"definition is not a tool: {tool_id}")
                _append_unique(agent_tool_ids, tool.id)
                tool_implementations[tool.id] = tool.implementation
            _append_unique(agent_ids, agent.id)
            _append_unique(task_ids, task.id)
            if task.recovery is not None:
                self._require(
                    definitions,
                    DefinitionKind.RECOVERY,
                    task.recovery,
                    action.id,
                )
                _append_unique(recovery_ids, task.recovery)
            defined_variables.add(action.output_variable)
            variable_contracts[action.output_variable] = task.output_contract
            return
        if isinstance(action, IfAction):
            self._require_variable(action.condition.variable, defined_variables, action.id)
            return
        if isinstance(action, ConditionGroupAction):
            for branch in action.branches:
                self._require_variable(
                    branch.condition.variable,
                    defined_variables,
                    action.id,
                )
            return
        if isinstance(action, GotoAction):
            return
        if isinstance(action, ForEachAction):
            self._require_variable(action.items_variable, defined_variables, action.id)
            defined_variables.add(action.item_variable)
            return
        if isinstance(action, ParallelAction):
            return
        if isinstance(action, JoinAction):
            for variable in action.inputs.values():
                self._require_variable(variable, defined_variables, action.id)
            defined_variables.add(action.output_variable)
            return
        if isinstance(action, SubworkflowAction):
            input_variables = (
                [action.input_variable]
                if action.input_variable is not None
                else list(action.input_variables.values())
            )
            for variable in input_variables:
                self._require_variable(variable, defined_variables, action.id)
            child = self._require(
                definitions,
                DefinitionKind.WORKFLOW,
                action.workflow,
                action.id,
            )
            if not isinstance(child, WorkflowDefinition):
                raise CompilerError(f"definition is not a workflow: {action.workflow}")
            _append_unique(workflow_ids, action.workflow)
            defined_variables.add(action.output_variable)
            if child.output_contract is not None:
                variable_contracts[action.output_variable] = child.output_contract
            return
        if isinstance(action, ValidateContractAction):
            self._require_variable(action.input_variable, defined_variables, action.id)
            self._require(
                definitions,
                DefinitionKind.CONTRACT,
                action.contract,
                action.id,
            )
            _append_unique(contract_ids, action.contract)
            defined_variables.add(action.output_variable)
            variable_contracts[action.output_variable] = action.contract
            return
        if isinstance(action, EvaluateGateAction):
            self._require_variable(action.input_variable, defined_variables, action.id)
            gate = self._require(
                definitions,
                DefinitionKind.GATE,
                action.gate,
                action.id,
            )
            if not isinstance(gate, GateDefinition):
                raise CompilerError(f"definition is not a gate: {action.gate}")
            _append_unique(gate_ids, gate.id)
            if gate.contract is not None:
                self._require_assignable(
                    definitions,
                    variable_contracts.get(action.input_variable),
                    gate.contract,
                    action.id,
                )
                _append_unique(contract_ids, gate.contract)
            if gate.validator_tool is not None:
                validator = self._require(
                    definitions,
                    DefinitionKind.TOOL,
                    gate.validator_tool,
                    action.id,
                )
                if not isinstance(validator, ToolDefinition):
                    raise CompilerError(
                        f"definition is not a tool: {gate.validator_tool}"
                    )
                _append_unique(tool_ids, gate.validator_tool)
                tool_implementations[validator.id] = validator.implementation
            defined_variables.add(action.output_variable)
            variable_contracts.pop(action.output_variable, None)
            return
        if isinstance(action, (RequestInputAction, WaitInputAction)):
            interaction = self._require(
                definitions,
                DefinitionKind.INTERACTION,
                action.interaction,
                action.id,
            )
            if not isinstance(interaction, InteractionDefinition):
                raise CompilerError(
                    f"definition is not an interaction: {action.interaction}"
                )
            self._require(
                definitions,
                DefinitionKind.CONTRACT,
                interaction.input_contract,
                action.id,
            )
            _append_unique(interaction_ids, interaction.id)
            _append_unique(contract_ids, interaction.input_contract)
            defined_variables.add(action.output_variable)
            variable_contracts[action.output_variable] = interaction.input_contract
            return
        if isinstance(action, PublishResultAction):
            self._require_variable(action.input_variable, defined_variables, action.id)
            output = self._require(
                definitions,
                DefinitionKind.OUTPUT,
                action.output,
                action.id,
            )
            if not isinstance(output, OutputDefinition):
                raise CompilerError(f"definition is not an output: {action.output}")
            if output.contract is not None:
                self._require_assignable(
                    definitions,
                    variable_contracts.get(action.input_variable),
                    output.contract,
                    action.id,
                )
                self._require(
                    definitions,
                    DefinitionKind.CONTRACT,
                    output.contract,
                    action.id,
                )
                _append_unique(contract_ids, output.contract)
            _append_unique(output_ids, output.id)
            return
        if isinstance(action, FailWorkflowAction):
            if action.error_variable is not None:
                self._require_variable(action.error_variable, defined_variables, action.id)
            return
        self._require_variable(action.output_variable, defined_variables, action.id)

    @staticmethod
    def _require_assignable(
        definitions: DefinitionRegistry,
        source_contract_id: str | None,
        target_contract_id: str,
        action_id: str,
    ) -> None:
        if source_contract_id is None:
            return
        source = definitions.require(DefinitionKind.CONTRACT, source_contract_id)
        target = definitions.require(DefinitionKind.CONTRACT, target_contract_id)
        if not isinstance(source, ContractDefinition) or not isinstance(
            target, ContractDefinition
        ):
            raise CompilerError(f"invalid contract definition for action {action_id}")
        if not build_contract_adapter(source).is_assignable_to(
            build_contract_adapter(target)
        ):
            raise CompilerError(
                f"contract {source_contract_id} is not assignable to "
                f"{target_contract_id} for action {action_id}"
            )

    @staticmethod
    def _validate_control_flow(
        actions: list[ResolvedAction],
        max_iterations: int | None,
        definitions: DefinitionRegistry,
    ) -> dict[str, list[str]]:
        positions = {action.id: index for index, action in enumerate(actions)}
        targets: list[tuple[str, str]] = []
        successors = {action.id: set[str]() for action in actions}
        parallel_ids = {
            action.id: action for action in actions if isinstance(action, ParallelAction)
        }
        for index, action in enumerate(actions):
            action_targets: list[str] = []
            if isinstance(action, IfAction):
                action_targets.extend([action.then, action.otherwise])
            elif isinstance(action, ConditionGroupAction):
                action_targets.extend(branch.target for branch in action.branches)
                action_targets.append(action.default)
            elif isinstance(action, GotoAction):
                action_targets.append(action.target)
            elif isinstance(action, ForEachAction):
                action_targets.extend([action.body, action.after])
            elif isinstance(action, ParallelAction):
                action_targets.extend(action.branches.values())
                action_targets.append(action.join)
            elif isinstance(action, JoinAction):
                parallel = parallel_ids.get(action.parallel)
                if parallel is None:
                    raise CompilerError(
                        f"join {action.id} references missing parallel action: {action.parallel}"
                    )
                if set(action.inputs) != set(parallel.branches):
                    raise CompilerError(f"join {action.id} inputs do not match parallel branches")
            elif isinstance(action, EvaluateGateAction):
                gate = definitions.require(DefinitionKind.GATE, action.gate)
                if not isinstance(gate, GateDefinition):
                    raise CompilerError(f"definition is not a gate: {action.gate}")
                action_targets.extend(
                    target
                    for target in (gate.on_pass, gate.on_fail, gate.on_wait)
                    if target is not None
                )
            for target in action_targets:
                targets.append((action.id, target))
                successors[action.id].add(target)
            if (
                not action_targets
                and not isinstance(action, EndWorkflowAction)
                and index + 1 < len(actions)
            ):
                successors[action.id].add(actions[index + 1].id)
        for owner, target in targets:
            if target not in positions:
                raise CompilerError(f"action {owner} references missing action target: {target}")
        back_edges = [
            (owner, target)
            for owner, target in targets
            if positions[target] <= positions[owner]
        ]
        if back_edges and max_iterations is None:
            predecessors = {action.id: set[str]() for action in actions}
            for owner, action_targets in successors.items():
                for target in action_targets:
                    predecessors[target].add(owner)
            dominators = _compute_dominators(actions, predecessors)
            action_by_id = {action.id: action for action in actions}
            explicit_targets = _explicit_targets(actions)
            for owner, target in back_edges:
                if target not in dominators.get(owner, set()):
                    raise CompilerError(
                        "workflow with a back edge requires max_iterations unless "
                        "the back edge forms a natural loop"
                    )
                natural_loop = _natural_loop(target, owner, predecessors)
                if not any(
                    isinstance(action_by_id[action_id], (IfAction, ConditionGroupAction))
                    and any(
                        exit_target not in natural_loop
                        for exit_target in explicit_targets[action_id]
                    )
                    for action_id in natural_loop
                ):
                    raise CompilerError(
                        "workflow with a back edge requires max_iterations unless "
                        "its natural loop has an explicit If/ConditionGroup exit"
                    )
        return {
            action.id: sorted(successors[action.id], key=positions.__getitem__)
            for action in actions
        }

    @staticmethod
    def _require(
        definitions: DefinitionRegistry,
        kind: DefinitionKind,
        definition_id: str,
        owner: str,
    ) -> Any:
        try:
            return definitions.require(kind, definition_id)
        except DefinitionReferenceError as exc:
            raise CompilerError(
                f"action {owner} references missing {kind}:{definition_id}"
            ) from exc

    @staticmethod
    def _require_variable(
        variable: str,
        defined_variables: set[str],
        owner: str,
    ) -> None:
        if variable not in defined_variables:
            raise CompilerError(f"action {owner} reads undefined variable: {variable}")


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _explicit_targets(
    actions: list[ResolvedAction],
) -> dict[str, list[str]]:
    """Return declared control-flow targets without inferring domain behavior."""

    targets = {action.id: [] for action in actions}
    for action in actions:
        action_targets = targets[action.id]
        if isinstance(action, IfAction):
            action_targets.extend([action.then, action.otherwise])
        elif isinstance(action, ConditionGroupAction):
            action_targets.extend(branch.target for branch in action.branches)
            action_targets.append(action.default)
        elif isinstance(action, GotoAction):
            action_targets.append(action.target)
        elif isinstance(action, ForEachAction):
            action_targets.extend([action.body, action.after])
        elif isinstance(action, ParallelAction):
            action_targets.extend(action.branches.values())
            action_targets.append(action.join)
    return targets


def _compute_dominators(
    actions: list[ResolvedAction],
    predecessors: dict[str, set[str]],
) -> dict[str, set[str]]:
    """Compute CFG dominators for natural-loop back-edge validation."""

    action_ids = {action.id for action in actions}
    entry = actions[0].id
    dominators = {action_id: set(action_ids) for action_id in action_ids}
    dominators[entry] = {entry}
    changed = True
    while changed:
        changed = False
        for action in actions[1:]:
            incoming = predecessors[action.id]
            if not incoming:
                candidate = {action.id}
            else:
                common = set(action_ids)
                for predecessor in incoming:
                    common.intersection_update(dominators[predecessor])
                candidate = {action.id, *common}
            if candidate != dominators[action.id]:
                dominators[action.id] = candidate
                changed = True
    return dominators


def _natural_loop(
    header: str,
    latch: str,
    predecessors: dict[str, set[str]],
) -> set[str]:
    """Return the natural loop induced by one validated CFG back edge."""

    loop = {header, latch}
    if header == latch:
        return loop
    pending = [latch]
    while pending:
        node = pending.pop()
        for predecessor in predecessors[node]:
            if predecessor in loop:
                continue
            loop.add(predecessor)
            if predecessor != header:
                pending.append(predecessor)
    return loop
