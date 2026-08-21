"""Minimal sequential compiler for the first neutral workflow slice."""

from typing import Any, Protocol

from pydantic import ValidationError

from manyselves.kernel.definitions import (
    AgentDefinition,
    DefinitionKind,
    DefinitionReferenceError,
    DefinitionRegistry,
    InteractionDefinition,
    OutputDefinition,
    TaskDefinition,
    ToolDefinition,
    WorkflowDefinition,
)

from .models import (
    ActionKind,
    ConditionGroupAction,
    CreateConversationAction,
    EndWorkflowAction,
    ForEachAction,
    GotoAction,
    IfAction,
    InvokeAgentAction,
    InvokeToolAction,
    JoinAction,
    ParallelAction,
    PublishResultAction,
    RequestInputAction,
    ResolveConversationAction,
    ResolvedAction,
    ResolvedPlan,
    SetVariableAction,
    SubworkflowAction,
    ValidateContractAction,
)


class CompilerError(ValueError):
    """Raised when a workflow cannot become an executable resolved plan."""


class ActionKindRegistry(Protocol):
    def has(self, kind: ActionKind | str) -> bool: ...


_ACTION_MODELS: dict[ActionKind, type[Any]] = {
    ActionKind.SET_VARIABLE: SetVariableAction,
    ActionKind.INVOKE_TOOL: InvokeToolAction,
    ActionKind.CREATE_CONVERSATION: CreateConversationAction,
    ActionKind.RESOLVE_CONVERSATION: ResolveConversationAction,
    ActionKind.INVOKE_AGENT: InvokeAgentAction,
    ActionKind.IF: IfAction,
    ActionKind.CONDITION_GROUP: ConditionGroupAction,
    ActionKind.GOTO: GotoAction,
    ActionKind.FOR_EACH: ForEachAction,
    ActionKind.PARALLEL: ParallelAction,
    ActionKind.JOIN: JoinAction,
    ActionKind.SUBWORKFLOW: SubworkflowAction,
    ActionKind.VALIDATE_CONTRACT: ValidateContractAction,
    ActionKind.REQUEST_INPUT: RequestInputAction,
    ActionKind.PUBLISH_RESULT: PublishResultAction,
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
        agent_ids: list[str] = []
        task_ids: list[str] = []
        contract_ids: list[str] = []
        workflow_ids: list[str] = []
        interaction_ids: list[str] = []
        output_ids: list[str] = []
        end_actions: list[EndWorkflowAction] = []

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
                contract_ids,
                agent_ids,
                task_ids,
                workflow_ids,
                interaction_ids,
                output_ids,
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
        self._validate_control_flow(actions, workflow.max_iterations)
        if workflow.output_contract:
            self._require(
                definitions,
                DefinitionKind.CONTRACT,
                workflow.output_contract,
                workflow.id,
            )
            _append_unique(contract_ids, workflow.output_contract)
        return ResolvedPlan(
            workflow_id=workflow.id,
            workflow_version=workflow.version,
            actions=actions,
            initial_state=workflow.state,
            entry_action_id=actions[0].id,
            max_iterations=workflow.max_iterations,
            tool_ids=tool_ids,
            agent_ids=agent_ids,
            task_ids=task_ids,
            workflow_ids=workflow_ids,
            contract_ids=contract_ids,
            interaction_ids=interaction_ids,
            output_ids=output_ids,
            final_output_contract=workflow.output_contract,
        )

    def _resolve_action(
        self,
        action: ResolvedAction,
        definitions: DefinitionRegistry,
        defined_variables: set[str],
        tool_ids: list[str],
        contract_ids: list[str],
        agent_ids: list[str],
        task_ids: list[str],
        workflow_ids: list[str],
        interaction_ids: list[str],
        output_ids: list[str],
    ) -> None:
        if isinstance(action, SetVariableAction):
            defined_variables.add(action.variable)
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
            _append_unique(contract_ids, tool.input_contract)
            _append_unique(contract_ids, tool.output_contract)
            defined_variables.add(action.output_variable)
            return
        if isinstance(action, (CreateConversationAction, ResolveConversationAction)):
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
            for contract_id in (task.input_contract, task.output_contract):
                self._require(
                    definitions,
                    DefinitionKind.CONTRACT,
                    contract_id,
                    action.id,
                )
                _append_unique(contract_ids, contract_id)
            _append_unique(agent_ids, agent.id)
            _append_unique(task_ids, task.id)
            defined_variables.add(action.output_variable)
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
            return
        if isinstance(action, RequestInputAction):
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
                self._require(
                    definitions,
                    DefinitionKind.CONTRACT,
                    output.contract,
                    action.id,
                )
                _append_unique(contract_ids, output.contract)
            _append_unique(output_ids, output.id)
            return
        self._require_variable(action.output_variable, defined_variables, action.id)

    @staticmethod
    def _validate_control_flow(
        actions: list[ResolvedAction],
        max_iterations: int | None,
    ) -> None:
        positions = {action.id: index for index, action in enumerate(actions)}
        targets: list[tuple[str, str]] = []
        back_edge = False
        parallel_ids = {
            action.id: action for action in actions if isinstance(action, ParallelAction)
        }
        for action in actions:
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
            for target in action_targets:
                targets.append((action.id, target))
                if target in positions and positions[target] <= positions[action.id]:
                    back_edge = True
        for owner, target in targets:
            if target not in positions:
                raise CompilerError(f"action {owner} references missing action target: {target}")
        if back_edge and max_iterations is None:
            raise CompilerError("workflow with a back edge requires max_iterations")

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
