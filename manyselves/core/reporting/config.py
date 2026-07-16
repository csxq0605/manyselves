"""Declarative Agent and reporting workflow definitions."""

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


def load_packaged_agents() -> dict[str, AgentDefinition]:
    """Load the built-in reporting Agent identities shipped with Manyselves."""

    templates = Path(__file__).resolve().parents[2] / "templates" / "reporting"
    return _load_agent_definitions(templates / "agents", allow_legacy=True)
