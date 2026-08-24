"""Business-neutral declarative definition models."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DefinitionKind(StrEnum):
    """Kinds understood by the definition layer."""

    CAPABILITY = "capability"
    AGENT = "agent"
    TOOL = "tool"
    CONTRACT = "contract"
    TASK = "task"
    RECOVERY = "recovery"
    WORKFLOW = "workflow"
    INTERACTION = "interaction"
    OUTPUT = "output"


class DefinitionBase(BaseModel):
    """Fields shared by every external definition."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = Field(min_length=1)


class CapabilityDefinition(DefinitionBase):
    """Entry point that indexes one capability's definition locations."""

    kind: Literal[DefinitionKind.CAPABILITY] = DefinitionKind.CAPABILITY
    agents: str
    workflows: str
    tasks: str
    contracts: str
    tools: str
    recovery: str
    interactions: str | None = None
    outputs: str | None = None
    entrypoints: list[str] = Field(default_factory=list)
    runtime: str | None = Field(default=None, min_length=1)


class AgentDefinition(DefinitionBase):
    """External description of an agent identity and its declared access."""

    kind: Literal[DefinitionKind.AGENT] = DefinitionKind.AGENT
    instructions: str = Field(min_length=1)
    model: str = "inherit"
    profile: str | None = None
    tools: list[str] = Field(default_factory=list)
    accepts: list[str] = Field(default_factory=list)
    produces: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)
    conversation_mode: str = "task"
    limits: dict[str, Any] = Field(default_factory=dict)


class ToolDefinition(DefinitionBase):
    """External description of a tool implementation and its contracts."""

    kind: Literal[DefinitionKind.TOOL] = DefinitionKind.TOOL
    implementation: str = Field(min_length=1)
    input_contract: str = Field(min_length=1)
    output_contract: str = Field(min_length=1)
    error_contract: str | None = None
    side_effect: Literal[
        "pure_read",
        "run_local_write",
        "shared_write",
        "external_network",
        "terminal",
        "ordered_state",
    ] = "ordered_state"
    parallel_safe: bool = False
    reuse_result: bool = False
    model_visible: bool = True
    instructions: str | None = None


class ContractDefinition(DefinitionBase):
    """Definition for a Pydantic or JSON Schema contract adapter."""

    kind: Literal[DefinitionKind.CONTRACT] = DefinitionKind.CONTRACT
    adapter: Literal["pydantic", "json_schema"]
    model: str | None = None
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")

    @model_validator(mode="after")
    def implementation_matches_adapter(self) -> "ContractDefinition":
        if self.adapter == "pydantic" and not self.model:
            raise ValueError("pydantic contract requires model")
        if self.adapter == "json_schema" and self.schema_ is None:
            raise ValueError("json_schema contract requires schema")
        return self


class TaskDefinition(DefinitionBase):
    """Reusable requirements for one agent invocation."""

    kind: Literal[DefinitionKind.TASK] = DefinitionKind.TASK
    agent: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    input_contract: str = Field(min_length=1)
    output_contract: str = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    recovery: str | None = None
    completion: dict[str, Any] = Field(default_factory=dict)


class RecoveryRule(BaseModel):
    """Capability-selected response to a generic recovery event."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1)
    prompt: str | None = None
    max_attempts: int | None = Field(default=None, ge=1)


class RecoveryPolicyDefinition(DefinitionBase):
    """Mapping from generic recovery events to generic recovery actions."""

    kind: Literal[DefinitionKind.RECOVERY] = DefinitionKind.RECOVERY
    rules: dict[str, RecoveryRule] = Field(default_factory=dict)


class InteractionDefinition(DefinitionBase):
    """File-defined user or external input requested by a workflow."""

    kind: Literal[DefinitionKind.INTERACTION] = DefinitionKind.INTERACTION
    interaction_type: Literal["form", "decision", "confirmation"] = "form"
    input_contract: str = Field(min_length=1)
    title: str = Field(min_length=1)
    submit_label: str = Field(default="Submit", min_length=1)


class OutputDefinition(DefinitionBase):
    """File-defined result published by a workflow."""

    kind: Literal[DefinitionKind.OUTPUT] = DefinitionKind.OUTPUT
    output_type: Literal["value", "artifact", "json"] = "value"
    contract: str | None = None
    label: str = Field(min_length=1)


class WorkflowDefinition(DefinitionBase):
    """Uncompiled file workflow and its declared definition dependencies."""

    kind: Literal[DefinitionKind.WORKFLOW] = DefinitionKind.WORKFLOW
    tasks: list[str] = Field(default_factory=list)
    recovery: list[str] = Field(default_factory=list)
    interactions: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    input_variable: str | None = Field(default=None, min_length=1)
    input_contract: str | None = None
    output_contract: str | None = None
    parameters: list[str] = Field(default_factory=list)
    max_iterations: int | None = Field(default=None, ge=1)
    state: dict[str, Any] = Field(default_factory=dict)
    actions: list[dict[str, Any]] = Field(default_factory=list)


Definition = (
    CapabilityDefinition
    | AgentDefinition
    | ToolDefinition
    | ContractDefinition
    | TaskDefinition
    | RecoveryPolicyDefinition
    | InteractionDefinition
    | OutputDefinition
    | WorkflowDefinition
)


DEFINITION_MODELS: dict[DefinitionKind, type[DefinitionBase]] = {
    DefinitionKind.CAPABILITY: CapabilityDefinition,
    DefinitionKind.AGENT: AgentDefinition,
    DefinitionKind.TOOL: ToolDefinition,
    DefinitionKind.CONTRACT: ContractDefinition,
    DefinitionKind.TASK: TaskDefinition,
    DefinitionKind.RECOVERY: RecoveryPolicyDefinition,
    DefinitionKind.INTERACTION: InteractionDefinition,
    DefinitionKind.OUTPUT: OutputDefinition,
    DefinitionKind.WORKFLOW: WorkflowDefinition,
}
