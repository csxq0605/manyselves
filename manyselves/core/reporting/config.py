"""Declarative Agent and reporting workflow definitions."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from manyselves.capabilities.distribution_reporting.runtime.artifact_access import (
    ConfigurationError,
)
from manyselves.kernel.definitions import (
    AgentDefinition as KernelAgentDefinition,
)
from manyselves.kernel.definitions import (
    DefinitionKind,
    load_capability,
)

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
    "review_findings",
    "revision_responses",
    "resolution_verdicts",
    "cross_findings",
    "cross_synthesis_inputs",
    "cross_owner_findings",
    "cross_owner_synthesis_inputs",
    "chief_editor_input",
    "chief_chapter_lane_input",
    "chief_chapter_lane_submission",
    "chief_chapter_lane_revision_submission",
    "final_review_input",
    "final_chapter_lane_input",
    "final_chapter_lane_finding_submission",
    "final_chapter_lane_verdict_submission",
    "project_evidence",
    "photo_manifest",
    "final_findings",
    "validation_reports",
    "review_completions",
    "report_state",
    "citation_plan",
    "output_artifacts",
}


class AgentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    model: str = "inherit"
    tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list, alias="disallowedTools")
    max_turns: int = Field(default=8, ge=1, le=40, alias="maxTurns")
    max_tokens: int | None = Field(default=None, ge=1024, le=65536, alias="maxTokens")
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


def _load_agent_definition(path: Path) -> AgentDefinition:
    path = Path(path)
    data, instructions = _frontmatter(path.read_text(encoding="utf-8"), path)
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

    return _load_agent_definition(path)


def _load_agent_definitions(directory: Path) -> dict[str, AgentDefinition]:
    agents: dict[str, AgentDefinition] = {}
    for path in sorted(Path(directory).glob("*.md")):
        definition = _load_agent_definition(path)
        if definition.name in agents:
            raise ConfigurationError(f"duplicate agent name: {definition.name}")
        agents[definition.name] = definition
    return agents


def load_agent_definitions(directory: Path) -> dict[str, AgentDefinition]:
    """Load current-schema Agent definitions and reject duplicate names."""

    return _load_agent_definitions(directory)


def project_reporting_agent(
    definition: KernelAgentDefinition,
    *,
    source_path: Path | None = None,
) -> AgentDefinition:
    """Project a file Agent snapshot into this retired runner's local model."""

    limits = definition.limits
    capability_root = (
        Path(__file__).resolve().parents[2]
        / "capabilities"
        / "distribution_reporting"
    )
    return AgentDefinition(
        name=definition.id,
        description=definition.description,
        model=definition.model,
        tools=list(definition.tools),
        disallowedTools=list(limits.get("disallowed_tools", ())),
        maxTurns=int(limits.get("max_turns", 8)),
        maxTokens=limits.get("max_tokens"),
        effort=str(limits.get("effort", "medium")),
        memory=definition.conversation_mode,
        background=bool(limits.get("background", True)),
        reads=list(definition.accepts),
        writes=list(definition.produces),
        instructions=definition.instructions,
        source_path=(
            source_path
            if source_path is not None
            else capability_root / "agents" / f"{definition.id}.md"
        ),
    )


def load_packaged_agents() -> dict[str, AgentDefinition]:
    """Compatibility import for the packaged distribution-reporting Agents."""

    capability_root = (
        Path(__file__).resolve().parents[2]
        / "capabilities"
        / "distribution_reporting"
    )
    _, registry = load_capability(capability_root / "capability.yaml")
    return {
        definition.id: project_reporting_agent(
            definition,
            source_path=capability_root / "agents" / f"{definition.id}.md",
        )
        for definition in registry.all(DefinitionKind.AGENT)
        if isinstance(definition, KernelAgentDefinition)
    }
