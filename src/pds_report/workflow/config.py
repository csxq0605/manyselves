from __future__ import annotations

from importlib.resources.abc import Traversable
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pds_report.domain.contracts import CARRIER_CONTRACTS

ALLOWED_CARRIERS = frozenset(CARRIER_CONTRACTS)


class ConfigurationError(ValueError):
    """Raised when declarative Agent or workflow configuration is invalid."""


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentDefinition(ConfigModel):
    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9-]*$")
    role: str = Field(min_length=1)
    reads: list[str]
    writes: list[str]
    tools: list[str] = Field(default_factory=list)
    instructions: str = Field(min_length=1)


class PhaseDefinition(ConfigModel):
    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9-]*$")
    mode: Literal["pipeline", "parallel"]
    agents: list[str] = Field(min_length=1)
    needs: list[str] = Field(default_factory=list)


class WorkflowDefinition(ConfigModel):
    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9-]*$")
    phases: list[PhaseDefinition] = Field(min_length=1)


def _as_mapping(value: object, path: Traversable) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{path}: configuration root must be a mapping")
    return value


def load_agent_definition(path: Traversable) -> AgentDefinition:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"{path}: cannot read agent definition: {exc}") from exc

    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ConfigurationError(f"{path}: expected Markdown frontmatter delimited by ---")

    frontmatter, instructions = text[4:].split("\n---\n", maxsplit=1)
    try:
        raw = _as_mapping(yaml.safe_load(frontmatter), path)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{path}: invalid YAML frontmatter: {exc}") from exc

    missing_contracts = [name for name in ("reads", "writes") if name not in raw]
    if missing_contracts:
        raise ConfigurationError(f"{path}: reads and writes must both be declared")

    raw["instructions"] = instructions.strip()
    try:
        agent = AgentDefinition.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"{path}: invalid agent definition: {exc}") from exc

    unknown = (set(agent.reads) | set(agent.writes)) - ALLOWED_CARRIERS
    if unknown:
        raise ConfigurationError(f"{path}: unknown carriers: {', '.join(sorted(unknown))}")
    return agent


def load_agent_definitions(directory: Traversable) -> dict[str, AgentDefinition]:
    agents: dict[str, AgentDefinition] = {}
    paths = sorted(
        (path for path in directory.iterdir() if path.name.endswith(".md")),
        key=lambda path: path.name,
    )
    for path in paths:
        agent = load_agent_definition(path)
        if agent.id in agents:
            raise ConfigurationError(f"duplicate agent id: {agent.id}")
        agents[agent.id] = agent
    if not agents:
        raise ConfigurationError(f"{directory}: no agent definitions found")
    return agents


def _assert_acyclic(phases: list[PhaseDefinition]) -> None:
    dependencies = {phase.id: phase.needs for phase in phases}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(phase_id: str) -> None:
        if phase_id in visiting:
            raise ConfigurationError(f"workflow phase dependency cycle at {phase_id}")
        if phase_id in visited:
            return
        visiting.add(phase_id)
        for dependency in dependencies[phase_id]:
            visit(dependency)
        visiting.remove(phase_id)
        visited.add(phase_id)

    for phase_id in dependencies:
        visit(phase_id)


def load_workflow(
    path: Traversable,
    agents: dict[str, AgentDefinition],
) -> WorkflowDefinition:
    try:
        raw = _as_mapping(yaml.safe_load(path.read_text(encoding="utf-8")), path)
    except OSError as exc:
        raise ConfigurationError(f"{path}: cannot read workflow: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{path}: invalid workflow YAML: {exc}") from exc

    raw_phases = raw.get("phases")
    if not isinstance(raw_phases, list):
        raise ConfigurationError(f"{path}: phases must be a list")

    normalized: list[dict[str, object]] = []
    previous: str | None = None
    for item in raw_phases:
        if not isinstance(item, dict):
            raise ConfigurationError(f"{path}: every phase must be a mapping")
        phase = dict(item)
        if "needs" not in phase:
            phase["needs"] = [previous] if previous else []
        normalized.append(phase)
        if isinstance(phase.get("id"), str):
            previous = phase["id"]

    raw["phases"] = normalized
    try:
        workflow = WorkflowDefinition.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"{path}: invalid workflow: {exc}") from exc

    phase_ids = [phase.id for phase in workflow.phases]
    duplicate_phases = {phase_id for phase_id in phase_ids if phase_ids.count(phase_id) > 1}
    if duplicate_phases:
        raise ConfigurationError(
            f"{path}: duplicate phase ids: {', '.join(sorted(duplicate_phases))}"
        )

    known_phases = set(phase_ids)
    for phase in workflow.phases:
        unknown_agents = set(phase.agents) - set(agents)
        if unknown_agents:
            raise ConfigurationError(
                f"{path}: unknown agents: {', '.join(sorted(unknown_agents))}"
            )
        unknown_needs = set(phase.needs) - known_phases
        if unknown_needs:
            raise ConfigurationError(
                f"{path}: unknown phase dependencies: {', '.join(sorted(unknown_needs))}"
            )
        if len(phase.agents) != len(set(phase.agents)):
            raise ConfigurationError(f"{path}: phase {phase.id} repeats an agent")

    _assert_acyclic(workflow.phases)
    return workflow
