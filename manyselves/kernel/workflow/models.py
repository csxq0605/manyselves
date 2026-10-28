"""Resolved actions, plans, and authoritative workflow state."""

from copy import deepcopy
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ActionKind(StrEnum):
    SET_VARIABLE = "set_variable"
    APPEND_VARIABLE = "append_variable"
    MERGE_VARIABLE = "merge_variable"
    INVOKE_TOOL = "invoke_tool"
    CREATE_CONVERSATION = "create_conversation"
    RESOLVE_CONVERSATION = "resolve_conversation"
    RESET_CONVERSATION = "reset_conversation"
    INVOKE_AGENT = "invoke_agent"
    IF = "if"
    CONDITION_GROUP = "condition_group"
    GOTO = "goto"
    FOR_EACH = "for_each"
    PARALLEL = "parallel"
    JOIN = "join"
    SUBWORKFLOW = "subworkflow"
    MAP = "map"
    FORK_CONVERSATION = "fork_conversation"
    UPSERT_RECORDS = "upsert_records"
    SELECT_RECORDS = "select_records"
    VALIDATE_CONTRACT = "validate_contract"
    REQUEST_INPUT = "request_input"
    WAIT_INPUT = "wait_input"
    PUBLISH_RESULT = "publish_result"
    FAIL_WORKFLOW = "fail_workflow"
    END_WORKFLOW = "end_workflow"


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


class ActionExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


class ResolvedActionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)


class SetVariableAction(ResolvedActionBase):
    """Set a literal value, copy a variable or one of its fields, or build an object.

    ``value_variables`` maps object keys to variables and sets the object
    built from their current values.
    """

    kind: Literal[ActionKind.SET_VARIABLE] = ActionKind.SET_VARIABLE
    variable: str = Field(min_length=1)
    value: Any = None
    value_variable: str | None = Field(default=None, min_length=1)
    field: str | None = Field(default=None, min_length=1)
    value_variables: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def field_reads_a_variable(self) -> "SetVariableAction":
        if self.field is not None and self.value_variable is None:
            raise ValueError("set_variable field requires value_variable")
        sources = [
            self.value is not None,
            self.value_variable is not None,
            bool(self.value_variables),
        ]
        if sum(sources) > 1:
            raise ValueError(
                "set_variable takes one of value, value_variable or value_variables"
            )
        return self


class AppendVariableAction(ResolvedActionBase):
    kind: Literal[ActionKind.APPEND_VARIABLE] = ActionKind.APPEND_VARIABLE
    variable: str = Field(min_length=1)
    value: Any = None
    value_variable: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def has_one_value_binding(self) -> "AppendVariableAction":
        # A serialized plan carries ``value: null`` beside ``value_variable``;
        # that null is the unset default, not a second binding.
        has_literal = "value" in self.model_fields_set and not (
            self.value is None and self.value_variable is not None
        )
        if has_literal == (self.value_variable is not None):
            raise ValueError("append_variable requires exactly one value binding")
        return self


class MergeVariableAction(ResolvedActionBase):
    kind: Literal[ActionKind.MERGE_VARIABLE] = ActionKind.MERGE_VARIABLE
    variable: str = Field(min_length=1)
    value: dict[str, Any] | None = None
    value_variable: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def has_one_value_binding(self) -> "MergeVariableAction":
        # A serialized plan carries ``value: null`` beside ``value_variable``;
        # that null is the unset default, not a second binding.
        has_literal = "value" in self.model_fields_set and not (
            self.value is None and self.value_variable is not None
        )
        if has_literal == (self.value_variable is not None):
            raise ValueError("merge_variable requires exactly one value binding")
        return self


class InvokeToolAction(ResolvedActionBase):
    kind: Literal[ActionKind.INVOKE_TOOL] = ActionKind.INVOKE_TOOL
    tool: str = Field(min_length=1)
    input_variable: str | None = Field(default=None, min_length=1)
    input_variables: dict[str, str] = Field(default_factory=dict)
    output_variable: str = Field(min_length=1)

    @model_validator(mode="after")
    def has_one_input_binding_mode(self) -> "InvokeToolAction":
        if bool(self.input_variable) == bool(self.input_variables):
            raise ValueError("invoke_tool requires exactly one input binding mode")
        return self


class CreateConversationAction(ResolvedActionBase):
    kind: Literal[ActionKind.CREATE_CONVERSATION] = ActionKind.CREATE_CONVERSATION
    agent: str = Field(min_length=1)
    conversation_key: str = Field(min_length=1)
    key_variable: str | None = Field(default=None, min_length=1)
    mode: Literal["ephemeral", "run", "persistent"] = "run"
    output_variable: str = Field(min_length=1)


class ResolveConversationAction(ResolvedActionBase):
    kind: Literal[ActionKind.RESOLVE_CONVERSATION] = ActionKind.RESOLVE_CONVERSATION
    agent: str = Field(min_length=1)
    conversation_key: str = Field(min_length=1)
    key_variable: str | None = Field(default=None, min_length=1)
    mode: Literal["run", "persistent"] = "run"
    output_variable: str = Field(min_length=1)


class ResetConversationAction(ResolvedActionBase):
    kind: Literal[ActionKind.RESET_CONVERSATION] = ActionKind.RESET_CONVERSATION
    agent: str = Field(min_length=1)
    conversation_key: str = Field(min_length=1)
    key_variable: str | None = Field(default=None, min_length=1)
    mode: Literal["ephemeral", "run", "persistent"] = "run"
    output_variable: str = Field(min_length=1)


class ForkConversationAction(ResolvedActionBase):
    """Start a new conversation whose history begins with a source conversation.

    The Kernel records only identity and lineage.  The Runtime copies the
    source's completed history so the child shares the source prefix.
    """

    kind: Literal[ActionKind.FORK_CONVERSATION] = ActionKind.FORK_CONVERSATION
    source_variable: str = Field(min_length=1)
    conversation_key: str = Field(min_length=1)
    key_variable: str | None = Field(default=None, min_length=1)
    mode: Literal["ephemeral", "run", "persistent"] = "run"
    output_variable: str = Field(min_length=1)


class InvokeAgentAction(ResolvedActionBase):
    kind: Literal[ActionKind.INVOKE_AGENT] = ActionKind.INVOKE_AGENT
    agent: str = Field(min_length=1)
    task: str = Field(min_length=1)
    conversation_variable: str = Field(min_length=1)
    input_variable: str = Field(min_length=1)
    output_variable: str = Field(min_length=1)


class VariableCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variable: str = Field(min_length=1)
    operator: Literal[
        "eq",
        "ne",
        "lt",
        "lte",
        "gt",
        "gte",
        "truthy",
        "falsy",
        "in",
        "not_in",
    ]
    value: Any = None


class IfAction(ResolvedActionBase):
    kind: Literal[ActionKind.IF] = ActionKind.IF
    condition: VariableCondition
    then: str = Field(min_length=1)
    otherwise: str = Field(min_length=1)


class ConditionBranch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: VariableCondition
    target: str = Field(min_length=1)


class ConditionGroupAction(ResolvedActionBase):
    kind: Literal[ActionKind.CONDITION_GROUP] = ActionKind.CONDITION_GROUP
    branches: list[ConditionBranch] = Field(min_length=1)
    default: str = Field(min_length=1)


class GotoAction(ResolvedActionBase):
    kind: Literal[ActionKind.GOTO] = ActionKind.GOTO
    target: str = Field(min_length=1)


class ForEachAction(ResolvedActionBase):
    kind: Literal[ActionKind.FOR_EACH] = ActionKind.FOR_EACH
    items_variable: str = Field(min_length=1)
    item_variable: str = Field(min_length=1)
    body: str = Field(min_length=1)
    after: str = Field(min_length=1)


class ParallelAction(ResolvedActionBase):
    kind: Literal[ActionKind.PARALLEL] = ActionKind.PARALLEL
    branches: dict[str, str] = Field(min_length=2)
    join: str = Field(min_length=1)
    max_concurrency: int | None = Field(default=None, ge=1)


class JoinAction(ResolvedActionBase):
    kind: Literal[ActionKind.JOIN] = ActionKind.JOIN
    parallel: str = Field(min_length=1)
    inputs: dict[str, str] = Field(min_length=1)
    output_variable: str = Field(min_length=1)


class SubworkflowAction(ResolvedActionBase):
    kind: Literal[ActionKind.SUBWORKFLOW] = ActionKind.SUBWORKFLOW
    workflow: str = Field(min_length=1)
    input_variable: str | None = Field(default=None, min_length=1)
    input_variables: dict[str, str] = Field(default_factory=dict)
    child_input_variable: str = Field(default="input", min_length=1)
    child_output_name: str = Field(default="result", min_length=1)
    output_variable: str = Field(min_length=1)

    @model_validator(mode="after")
    def has_one_input_binding_mode(self) -> "SubworkflowAction":
        if bool(self.input_variable) == bool(self.input_variables):
            raise ValueError("subworkflow requires exactly one input binding mode")
        return self


class ItemCondition(BaseModel):
    """Compare one field of a record or list item with a literal value."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    operator: Literal[
        "eq",
        "ne",
        "lt",
        "lte",
        "gt",
        "gte",
        "truthy",
        "falsy",
        "in",
        "not_in",
    ]
    value: Any = None


class MapAction(ResolvedActionBase):
    """Run one child workflow per item of a list or keyed record table.

    Items are identified by ``key_field`` (list items) or by their table key.
    Results are collected into ``output_variable`` keyed the same way.  When
    ``errors_variable`` is declared, item failures are recorded there and the
    remaining items still complete; otherwise any failure fails the action.
    """

    kind: Literal[ActionKind.MAP] = ActionKind.MAP
    items_variable: str = Field(min_length=1)
    key_field: str | None = Field(default=None, min_length=1)
    where: list[ItemCondition] = Field(default_factory=list)
    workflow: str = Field(min_length=1)
    item_input_variable: str = Field(default="item", min_length=1)
    key_input_variable: str | None = Field(default=None, min_length=1)
    input_variables: dict[str, str] = Field(default_factory=dict)
    child_output_name: str = Field(default="result", min_length=1)
    output_variable: str = Field(min_length=1)
    errors_variable: str | None = Field(default=None, min_length=1)
    max_concurrency: int | None = Field(default=None, ge=1)
    # The first item starts alone so requests that open with the same text can
    # reuse what it sent: the others start once its first model response is
    # back (the provider has read the prompt by then), or after at most this
    # many seconds.
    lead_seconds: float | None = Field(default=None, gt=0, le=300)


class UpsertRecordsAction(ResolvedActionBase):
    """Merge records into a keyed table, replacing records with the same key."""

    kind: Literal[ActionKind.UPSERT_RECORDS] = ActionKind.UPSERT_RECORDS
    table_variable: str = Field(min_length=1)
    records_variable: str = Field(min_length=1)
    key_field: str | None = Field(default=None, min_length=1)
    merge: Literal["replace", "update"] = "replace"
    record_contract: str | None = Field(default=None, min_length=1)
    output_variable: str | None = Field(default=None, min_length=1)


class SelectRecordsAction(ResolvedActionBase):
    """Select the records of a keyed table that match every condition."""

    kind: Literal[ActionKind.SELECT_RECORDS] = ActionKind.SELECT_RECORDS
    table_variable: str = Field(min_length=1)
    where: list[ItemCondition] = Field(default_factory=list)
    output: Literal["table", "list", "keys"] = "table"
    output_variable: str = Field(min_length=1)


class ValidateContractAction(ResolvedActionBase):
    kind: Literal[ActionKind.VALIDATE_CONTRACT] = ActionKind.VALIDATE_CONTRACT
    contract: str = Field(min_length=1)
    input_variable: str = Field(min_length=1)
    output_variable: str = Field(min_length=1)


class RequestInputAction(ResolvedActionBase):
    kind: Literal[ActionKind.REQUEST_INPUT] = ActionKind.REQUEST_INPUT
    interaction: str = Field(min_length=1)
    output_variable: str = Field(min_length=1)


class WaitInputAction(ResolvedActionBase):
    kind: Literal[ActionKind.WAIT_INPUT] = ActionKind.WAIT_INPUT
    interaction: str = Field(min_length=1)
    output_variable: str = Field(min_length=1)


class PublishResultAction(ResolvedActionBase):
    kind: Literal[ActionKind.PUBLISH_RESULT] = ActionKind.PUBLISH_RESULT
    output: str = Field(min_length=1)
    input_variable: str = Field(min_length=1)
    output_name: str = Field(min_length=1)


class FailWorkflowAction(ResolvedActionBase):
    kind: Literal[ActionKind.FAIL_WORKFLOW] = ActionKind.FAIL_WORKFLOW
    message: str | None = Field(default=None, min_length=1)
    error_variable: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def has_one_error_binding(self) -> "FailWorkflowAction":
        if bool(self.message) == bool(self.error_variable):
            raise ValueError("fail_workflow requires exactly one error binding")
        return self


class EndWorkflowAction(ResolvedActionBase):
    kind: Literal[ActionKind.END_WORKFLOW] = ActionKind.END_WORKFLOW
    output_variable: str = Field(min_length=1)
    output_name: str = Field(default="result", min_length=1)
    output_contract: str | None = Field(default=None, min_length=1)


ResolvedAction = (
    SetVariableAction
    | AppendVariableAction
    | MergeVariableAction
    | InvokeToolAction
    | CreateConversationAction
    | ResolveConversationAction
    | ResetConversationAction
    | InvokeAgentAction
    | IfAction
    | ConditionGroupAction
    | GotoAction
    | ForEachAction
    | ParallelAction
    | JoinAction
    | SubworkflowAction
    | MapAction
    | ForkConversationAction
    | UpsertRecordsAction
    | SelectRecordsAction
    | ValidateContractAction
    | RequestInputAction
    | WaitInputAction
    | PublishResultAction
    | FailWorkflowAction
    | EndWorkflowAction
)


class ResolvedPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    workflow_version: str
    actions: list[ResolvedAction]
    initial_state: dict[str, Any] = Field(default_factory=dict)
    input_variable: str | None = None
    input_contract: str | None = None
    entry_action_id: str | None = None
    max_iterations: int | None = Field(default=None, ge=1)
    tool_ids: list[str] = Field(default_factory=list)
    agent_tool_ids: list[str] = Field(default_factory=list)
    tool_implementations: dict[str, str] = Field(default_factory=dict)
    agent_ids: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    workflow_ids: list[str] = Field(default_factory=list)
    contract_ids: list[str] = Field(default_factory=list)
    interaction_ids: list[str] = Field(default_factory=list)
    output_ids: list[str] = Field(default_factory=list)
    recovery_ids: list[str] = Field(default_factory=list)
    conversation_bindings: dict[str, dict[str, str]] = Field(default_factory=dict)
    control_flow_edges: dict[str, list[str]] = Field(default_factory=dict)
    parallel_concurrency: dict[str, int] = Field(default_factory=dict)
    final_output_contract: str | None = None
    subworkflow_plans: dict[str, "ResolvedPlan"] = Field(default_factory=dict)
    definition_snapshots: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ActionExecutionState(BaseModel):
    status: ActionExecutionStatus = ActionExecutionStatus.PENDING
    output: Any = None
    error: str | None = None
    # {"category": str, "retryable": bool} when the executor knew how it failed.
    failure: dict[str, Any] | None = None


class WorkflowState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    variables: dict[str, Any] = Field(default_factory=dict)
    conversations: dict[str, Any] = Field(default_factory=dict)
    actions: dict[str, ActionExecutionState] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    waiting_input: dict[str, Any] | None = None
    next_action_index: int = Field(default=0, ge=0)
    next_action_id: str | None = None
    control_steps: int = Field(default=0, ge=0)
    control_frames: dict[str, Any] = Field(default_factory=dict)
    parallel_results: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)
    parallel_states: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)
    subworkflow_states: dict[str, dict[str, Any]] = Field(default_factory=dict)
    map_states: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)

    @classmethod
    def for_plan(
        cls,
        run_id: str,
        plan: ResolvedPlan,
        *,
        initial_variables: dict[str, Any] | None = None,
    ) -> "WorkflowState":
        variables = deepcopy(plan.initial_state)
        if initial_variables is not None:
            variables.update(deepcopy(initial_variables))
        return cls(
            run_id=run_id,
            workflow_id=plan.workflow_id,
            variables=variables,
            actions={action.id: ActionExecutionState() for action in plan.actions},
            next_action_id=plan.entry_action_id,
        )
