"""Minimal sequential compiler for the first neutral workflow slice."""

from typing import Any, Protocol

from pydantic import ValidationError

from manyselves.kernel.definitions import (
    DefinitionKind,
    DefinitionReferenceError,
    DefinitionRegistry,
    ToolDefinition,
    WorkflowDefinition,
)

from .models import (
    ActionKind,
    EndWorkflowAction,
    InvokeToolAction,
    ResolvedAction,
    ResolvedPlan,
    SetVariableAction,
    ValidateContractAction,
)


class CompilerError(ValueError):
    """Raised when a workflow cannot become an executable resolved plan."""


class ActionKindRegistry(Protocol):
    def has(self, kind: ActionKind | str) -> bool: ...


_ACTION_MODELS: dict[ActionKind, type[Any]] = {
    ActionKind.SET_VARIABLE: SetVariableAction,
    ActionKind.INVOKE_TOOL: InvokeToolAction,
    ActionKind.VALIDATE_CONTRACT: ValidateContractAction,
    ActionKind.END_WORKFLOW: EndWorkflowAction,
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
        contract_ids: list[str] = []
        end_actions: list[EndWorkflowAction] = []

        for payload in workflow.actions:
            raw_kind = payload.get("kind")
            try:
                kind = ActionKind(raw_kind)
            except ValueError as exc:
                raise CompilerError(f"unknown action kind: {raw_kind}") from exc
            if not self._executors.has(kind):
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
            )
            if isinstance(action, EndWorkflowAction):
                end_actions.append(action)
            actions.append(action)

        if len(end_actions) != 1 or not actions or actions[-1] is not end_actions[0]:
            raise CompilerError(
                "minimal sequential workflow requires one final end_workflow action"
            )
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
            tool_ids=tool_ids,
            contract_ids=contract_ids,
            final_output_contract=workflow.output_contract,
        )

    def _resolve_action(
        self,
        action: ResolvedAction,
        definitions: DefinitionRegistry,
        defined_variables: set[str],
        tool_ids: list[str],
        contract_ids: list[str],
    ) -> None:
        if isinstance(action, SetVariableAction):
            defined_variables.add(action.variable)
            return
        if isinstance(action, InvokeToolAction):
            self._require_variable(action.input_variable, defined_variables, action.id)
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
        self._require_variable(action.output_variable, defined_variables, action.id)

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
