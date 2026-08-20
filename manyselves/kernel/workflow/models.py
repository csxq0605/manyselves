"""Resolved actions, plans, and authoritative workflow state."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ActionKind(StrEnum):
    SET_VARIABLE = "set_variable"
    INVOKE_TOOL = "invoke_tool"
    VALIDATE_CONTRACT = "validate_contract"
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
    input_variable: str = Field(min_length=1)
    output_variable: str = Field(min_length=1)


class ValidateContractAction(ResolvedActionBase):
    kind: Literal[ActionKind.VALIDATE_CONTRACT] = ActionKind.VALIDATE_CONTRACT
    contract: str = Field(min_length=1)
    input_variable: str = Field(min_length=1)
    output_variable: str = Field(min_length=1)


class EndWorkflowAction(ResolvedActionBase):
    kind: Literal[ActionKind.END_WORKFLOW] = ActionKind.END_WORKFLOW
    output_variable: str = Field(min_length=1)
    output_name: str = Field(default="result", min_length=1)


ResolvedAction = (
    SetVariableAction
    | InvokeToolAction
    | ValidateContractAction
    | EndWorkflowAction
)


class ResolvedPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    workflow_version: str
    actions: list[ResolvedAction]
    tool_ids: list[str] = Field(default_factory=list)
    contract_ids: list[str] = Field(default_factory=list)
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

    @classmethod
    def for_plan(cls, run_id: str, plan: ResolvedPlan) -> "WorkflowState":
        return cls(
            run_id=run_id,
            workflow_id=plan.workflow_id,
            actions={action.id: ActionExecutionState() for action in plan.actions},
        )
