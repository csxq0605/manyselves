"""Compile declarative reporting roles into executable artifact capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from ..artifacts.gateway import ArtifactGateway, ArtifactGrant
from .agentic_models import TaskEnvelope
from .config import AgentDefinition, ConfigurationError


@dataclass(frozen=True)
class CompiledAgentAccess:
    gateway: ArtifactGateway
    readable_refs: tuple[str, ...]
    unreadable_refs: tuple[str, ...]
    tool_names: tuple[str, ...]


def compile_agent_access(
    definition: AgentDefinition,
    envelope: TaskEnvelope,
    refs: list[str],
    *,
    gateway: ArtifactGateway,
) -> CompiledAgentAccess:
    readable: list[str] = []
    unreadable: list[str] = []
    for ref in dict.fromkeys(refs):
        if ref.startswith("artifact:v1:"):
            try:
                gateway.open(ref, limit=1)
                readable.append(ref)
            except Exception:
                unreadable.append(ref)
            continue
        if "/" not in ref and "\\" not in ref:
            continue
        target = (gateway.workspace / ref).resolve()
        if target.is_relative_to(gateway.workspace) and target.is_file():
            readable.append(ref)
        else:
            unreadable.append(ref)
    if unreadable:
        raise ConfigurationError(
            f"{definition.id} cannot read declared task refs {unreadable}; "
            "attach existing project artifacts before starting the AgentLoop"
        )
    declared_tools = tuple(
        dict.fromkeys(
            [
                *definition.tools,
                "open_artifact",
                "open_tool_result",
                "search_text",
            ]
        )
    )
    if envelope.allowed_tools:
        unknown = sorted(set(envelope.allowed_tools) - set(declared_tools))
        if unknown:
            raise ConfigurationError(
                f"{definition.id} task requested undeclared tools {unknown}"
            )
        allowed = {*envelope.allowed_tools, "open_tool_result"}
        tools = tuple(name for name in declared_tools if name in allowed)
    else:
        tools = declared_tools
    return CompiledAgentAccess(gateway, tuple(readable), (), tools)


def scoped_gateway(
    root: ArtifactGateway,
    *,
    workflow_id: str,
    envelope: TaskEnvelope,
    agent_id: str,
    session_id: str,
) -> ArtifactGateway:
    return root.scoped(
        ArtifactGrant(workflow_id, envelope.task_id, agent_id, session_id)
    )
