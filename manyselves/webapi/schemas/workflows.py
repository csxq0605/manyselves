"""Generic Capability, Workflow, Run, Output, and Cost DTOs."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _ProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CapabilitySummary(_ProjectionModel):
    id: str
    version: str
    description: str
    workflow_ids: list[str] = Field(alias="workflowIds")


class CapabilityListResponse(_ProjectionModel):
    capabilities: list[CapabilitySummary]


class WorkflowSummary(_ProjectionModel):
    id: str
    capability_id: str = Field(alias="capabilityId")
    version: str
    description: str
    input_contract: str | None = Field(alias="inputContract")
    output_contract: str | None = Field(alias="outputContract")
    runnable: bool


class WorkflowListResponse(_ProjectionModel):
    workflows: list[WorkflowSummary]


class WorkflowInputSchemaResponse(_ProjectionModel):
    workflow_id: str = Field(alias="workflowId")
    contract_id: str | None = Field(alias="contractId")
    schema_: dict[str, Any] = Field(alias="schema")


class WorkflowRunStartRequest(_ProjectionModel):
    workflow_id: str = Field(alias="workflowId")
    input: dict[str, Any]


class WorkflowRunInputRequest(_ProjectionModel):
    input_id: str | None = Field(default=None, alias="inputId")
    values: dict[str, Any] = Field(default_factory=dict)


class WorkflowRunAcceptedResponse(_ProjectionModel):
    command_id: UUID = Field(alias="commandId")
    status: Literal["accepted"] = "accepted"
    run_id: str = Field(alias="runId")
    task_id: str | None = Field(default=None, alias="taskId")
    capability_id: str = Field(alias="capabilityId")
    workflow_id: str = Field(alias="workflowId")


class WorkflowRunSummary(_ProjectionModel):
    run_id: str = Field(alias="runId")
    capability_id: str = Field(alias="capabilityId")
    workflow_id: str = Field(alias="workflowId")
    status: str
    active: bool
    task_id: str | None = Field(default=None, alias="taskId")


class WorkflowRunResponse(_ProjectionModel):
    run: WorkflowRunSummary
    state: dict[str, Any]
    waiting_input: list[dict[str, Any]] = Field(alias="waitingInput")


class WorkflowOutput(_ProjectionModel):
    path: str
    exists: bool
    size: int


class WorkflowOutputListResponse(_ProjectionModel):
    run_id: str = Field(alias="runId")
    outputs: list[WorkflowOutput]


class WorkflowCostResponse(_ProjectionModel):
    run_id: str = Field(alias="runId")
    usage: dict[str, Any]

