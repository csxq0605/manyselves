"""Reporting-owned adapters behind the neutral Capability definitions."""

from pathlib import Path

from manyselves.core.reporting.config import AgentDefinition as ReportingAgentDefinition
from manyselves.kernel.definitions import AgentDefinition, DefinitionKind, load_capability


def project_reporting_agent(
    definition: AgentDefinition,
    *,
    source_path: Path | None = None,
) -> ReportingAgentDefinition:
    """Project one Kernel Agent snapshot to the existing Reporting runner model."""

    limits = definition.limits
    capability_root = Path(__file__).resolve().parents[1]
    return ReportingAgentDefinition(
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


def load_reporting_agents() -> dict[str, ReportingAgentDefinition]:
    """Project packaged neutral Agent definitions to the current runtime model."""

    capability_root = Path(__file__).resolve().parents[1]
    _, registry = load_capability(capability_root / "capability.yaml")
    agents: dict[str, ReportingAgentDefinition] = {}
    for definition in registry.all(DefinitionKind.AGENT):
        if not isinstance(definition, AgentDefinition):
            continue
        agents[definition.id] = project_reporting_agent(
            definition,
            source_path=capability_root / "agents" / f"{definition.id}.md",
        )
    return agents


def build_module_lane_definitions(module_id: str):
    """Compatibility adapter for the executable module-Lane specialization."""

    from manyselves.core.reporting.declarative_module_lane import (
        build_module_lane_definitions as build_current_module_lane,
    )

    return build_current_module_lane(module_id)


def build_module_cohort_definition(*, max_concurrency: int | None = None):
    """Compatibility adapter for the executable five-Lane cohort."""

    from manyselves.core.reporting.declarative_module_cohort import (
        build_module_cohort_definition as build_current_module_cohort,
    )

    return build_current_module_cohort(max_concurrency=max_concurrency)


def build_reporting_tail_definition():
    """Compatibility adapter for the executable Cross-to-Delivery tail."""

    from manyselves.core.reporting.declarative_reporting_tail import (
        build_reporting_tail_definition as build_current_reporting_tail,
    )

    return build_current_reporting_tail()


__all__ = [
    "build_module_cohort_definition",
    "build_module_lane_definitions",
    "build_reporting_tail_definition",
    "load_reporting_agents",
    "project_reporting_agent",
]
