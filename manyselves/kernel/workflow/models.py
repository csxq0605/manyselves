"""Resolved actions, plans, and authoritative workflow state."""

from copy import deepcopy
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ActionKind(StrEnum):
    SET_VARIABLE = "set_variable"
    INVOKE_TOOL = "invoke_tool"
    CREATE_CONVERSATION = "create_conversation"
    RESOLVE_CONVERSATION = "resolve_conversation"
    INVOKE_AGENT = "invoke_agent"
    IF = "if"
    CONDITION_GROUP = "condition_group"
    GOTO = "goto"
    FOR_EACH = "for_each"
    PARALLEL = "parallel"
    JOIN = "join"
    SUBWORKFLOW = "subworkflow"
    VALIDATE_CONTRACT = "validate_contract"
    REQUEST_INPUT = "request_input"
    PUBLISH_RESULT = "publish_result"
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
    kind: Literal[ActionKind.SET_VARIABLE] = ActionKind.SET_VARIABLE
    variable: str = Field(min_length=1)
    value: Any


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
    mode: Literal["ephemeral", "run", "persistent"] = "run"
    output_variable: str = Field(min_length=1)


class ResolveConversationAction(ResolvedActionBase):
    kind: Literal[ActionKind.RESOLVE_CONVERSATION] = ActionKind.RESOLVE_CONVERSATION
    agent: str = Field(min_length=1)
    conversation_key: str = Field(min_length=1)
    mode: Literal["run", "persistent"] = "run"
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
    input_variable: str = Field(min_length=1)
    child_input_variable: str = Field(default="input", min_length=1)
    child_output_name: str = Field(default="result", min_length=1)
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


class PublishResultAction(ResolvedActionBase):
    kind: Literal[ActionKind.PUBLISH_RESULT] = ActionKind.PUBLISH_RESULT
    output: str = Field(min_length=1)
    input_variable: str = Field(min_length=1)
    output_name: str = Field(min_length=1)


class EndWorkflowAction(ResolvedActionBase):
    kind: Literal[ActionKind.END_WORKFLOW] = ActionKind.END_WORKFLOW
    output_variable: str = Field(min_length=1)
    output_name: str = Field(default="result", min_length=1)


ResolvedAction = (
    SetVariableAction
    | InvokeToolAction
    | CreateConversationAction
    | ResolveConversationAction
    | InvokeAgentAction
    | IfAction
    | ConditionGroupAction
    | GotoAction
    | ForEachAction
    | ParallelAction
    | JoinAction
    | SubworkflowAction
    | ValidateContractAction
    | RequestInputAction
    | PublishResultAction
    | EndWorkflowAction
)


class ResolvedPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    workflow_version: str
    actions: list[ResolvedAction]
    initial_state: dict[str, Any] = Field(default_factory=dict)
    entry_action_id: str | None = None
    max_iterations: int | None = Field(default=None, ge=1)
    tool_ids: list[str] = Field(default_factory=list)
    agent_ids: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    workflow_ids: list[str] = Field(default_factory=list)
    contract_ids: list[str] = Field(default_factory=list)
    interaction_ids: list[str] = Field(default_factory=list)
    output_ids: list[str] = Field(default_factory=list)
    final_output_contract: str | None = None


class ActionExecutionState(BaseModel):
    status: ActionExecutionStatus = ActionExecutionStatus.PENDING
    output: Any = None
    error: str | None = None


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

    @classmethod
    def for_plan(cls, run_id: str, plan: ResolvedPlan) -> "WorkflowState":
        return cls(
            run_id=run_id,
            workflow_id=plan.workflow_id,
            variables=deepcopy(plan.initial_state),
            actions={action.id: ActionExecutionState() for action in plan.actions},
            next_action_id=plan.entry_action_id,
        )
