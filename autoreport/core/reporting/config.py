"""Declarative Agent and workflow definitions for reporting phases."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

KNOWN_CARRIERS = {
    "report_request",
    "project_manifest",
    "parsed_artifacts",
    "evidence_items",
    "coverage_matrix",
    "module_tasks",
    "module_drafts",
    "research_notes",
    "claim_ledger",
    "source_ledger",
    "review_issues",
    "report_state",
    "citation_plan",
    "output_artifacts",
}


class ConfigurationError(ValueError):
    """Raised before a reporting workflow enters the running state."""


class AgentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    model: str = "inherit"
    tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list, alias="disallowedTools")
    max_turns: int = Field(default=8, ge=1, le=40, alias="maxTurns")
    effort: Literal["low", "medium", "high"] = "medium"
    memory: Literal["task", "session"] = "task"
    background: bool = True
    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)
    instructions: str = Field(min_length=1)
    source_path: Path

    @property
    def id(self) -> str:
        return self.name

    @model_validator(mode="after")
    def tool_sets_do_not_overlap(self) -> "AgentDefinition":
        overlap = sorted(set(self.tools) & set(self.disallowed_tools))
        if overlap:
            raise ValueError(f"tools also listed in disallowedTools: {overlap}")
        return self


class PhaseDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    mode: Literal["pipeline", "parallel", "barrier"]
    agents: list[str] = Field(default_factory=list)
    pipelines: list["PipelineDefinition"] = Field(default_factory=list)
    needs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def execution_shape_matches_mode(self) -> "PhaseDefinition":
        if self.mode == "barrier":
            if self.agents or self.pipelines:
                raise ValueError("barrier phases cannot execute agents or pipelines")
            if not self.needs:
                raise ValueError("barrier phases require at least one dependency")
            return self
        if self.mode == "pipeline":
            if not self.agents or self.pipelines:
                raise ValueError("pipeline phases require agents and cannot contain pipelines")
            return self
        if bool(self.agents) == bool(self.pipelines):
            raise ValueError("parallel phases require exactly one of agents or pipelines")
        return self


class PipelineDefinition(BaseModel):
    """One independently progressing path inside a parallel workflow phase."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(min_length=1)
    agents: list[str] = Field(min_length=1)
    revision_agent: str | None = Field(default=None, alias="revisionAgent")
    max_revisions: int = Field(default=0, ge=0, le=10, alias="maxRevisions")

    @model_validator(mode="after")
    def revision_is_local_and_bounded(self) -> "PipelineDefinition":
        if self.max_revisions and not self.revision_agent:
            raise ValueError("maxRevisions requires revisionAgent")
        if self.revision_agent and self.revision_agent not in self.agents:
            raise ValueError("revisionAgent must belong to its pipeline")
        return self


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    phases: list[PhaseDefinition] = Field(min_length=1)


def _frontmatter(content: str, path: Path) -> tuple[dict, str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ConfigurationError(f"{path}: missing YAML frontmatter")
    try:
        closing = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ConfigurationError(f"{path}: unterminated YAML frontmatter") from exc
    try:
        data = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{path}: invalid YAML: {exc}") from exc
    body = "\n".join(lines[closing + 1 :]).strip()
    return data, body


def _load_agent_definition(path: Path, *, allow_legacy: bool) -> AgentDefinition:
    path = Path(path)
    data, instructions = _frontmatter(path.read_text(encoding="utf-8"), path)
    if allow_legacy:
        if "name" not in data and "id" in data:
            data["name"] = data.pop("id")
        if "description" not in data and "role" in data:
            data["description"] = data.pop("role")
    try:
        definition = AgentDefinition(
            **data,
            instructions=instructions,
            source_path=path,
        )
    except ValidationError as exc:
        raise ConfigurationError(f"{path}: {exc}") from exc
    unknown = sorted((set(definition.reads) | set(definition.writes)) - KNOWN_CARRIERS)
    if unknown:
        raise ConfigurationError(f"{path}: unknown carriers: {', '.join(unknown)}")
    return definition


def load_agent_definition(path: Path) -> AgentDefinition:
    """Load one Markdown/frontmatter Agent contract using the current schema."""

    return _load_agent_definition(path, allow_legacy=False)


def _load_agent_definitions(
    directory: Path,
    *,
    allow_legacy: bool,
) -> dict[str, AgentDefinition]:
    agents: dict[str, AgentDefinition] = {}
    for path in sorted(Path(directory).glob("*.md")):
        definition = _load_agent_definition(path, allow_legacy=allow_legacy)
        if definition.name in agents:
            raise ConfigurationError(f"duplicate agent name: {definition.name}")
        agents[definition.name] = definition
    return agents


def load_agent_definitions(directory: Path) -> dict[str, AgentDefinition]:
    """Load current-schema Agent definitions and reject duplicate names."""

    return _load_agent_definitions(directory, allow_legacy=False)


def _validate_phase_graph(phases: list[PhaseDefinition]) -> None:
    ids = [phase.id for phase in phases]
    if len(ids) != len(set(ids)):
        raise ConfigurationError("duplicate phase id")
    known = set(ids)
    for phase in phases:
        unknown = sorted(set(phase.needs) - known)
        if unknown:
            raise ConfigurationError(f"phase {phase.id} has unknown dependencies: {unknown}")

    graph = {phase.id: phase.needs for phase in phases}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(phase_id: str) -> None:
        if phase_id in visiting:
            raise ConfigurationError(f"cyclic phase dependency at {phase_id}")
        if phase_id in visited:
            return
        visiting.add(phase_id)
        for dependency in graph[phase_id]:
            visit(dependency)
        visiting.remove(phase_id)
        visited.add(phase_id)

    for phase_id in graph:
        visit(phase_id)


def load_workflow_definition(
    path: Path,
    agents: dict[str, AgentDefinition],
) -> WorkflowDefinition:
    """Load and validate the phase graph before execution."""

    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        workflow = WorkflowDefinition.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError(f"{path}: {exc}") from exc

    _validate_phase_graph(workflow.phases)
    for phase in workflow.phases:
        referenced_agents = set(phase.agents)
        for pipeline in phase.pipelines:
            referenced_agents.update(pipeline.agents)
        unknown = sorted(referenced_agents - set(agents))
        if unknown:
            raise ConfigurationError(f"phase {phase.id} references unknown agents: {unknown}")
        if phase.mode == "parallel" and phase.agents:
            owners: dict[str, str] = {}
            for agent_id in phase.agents:
                for carrier in agents[agent_id].writes:
                    if carrier in owners:
                        raise ConfigurationError(
                            f"parallel phase {phase.id} has conflicting write {carrier}: "
                            f"{owners[carrier]} and {agent_id}"
                        )
                    owners[carrier] = agent_id
    return workflow


def load_packaged_workflow() -> tuple[dict[str, AgentDefinition], WorkflowDefinition]:
    """Load the built-in V2 configuration shipped with AutoReport."""

    templates = Path(__file__).resolve().parents[2] / "templates" / "reporting"
    agents = _load_agent_definitions(templates / "agents", allow_legacy=True)
    workflow = load_workflow_definition(templates / "workflows" / "phase-a.yml", agents)
    return agents, workflow
