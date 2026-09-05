"""Business-neutral construction of one configured Provider AgentLoop."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_execution import AgentSessionLoop, AgentSessionRestore


def agent_session_handoff_path(
    workspace: Path,
    run_id: str,
    agent_type: str,
) -> Path:
    """Return the existing bounded Agent handoff path for one run identity."""

    safe_agent = "".join(
        char if char.isalnum() or char in "-_." else "_"
        for char in str(agent_type)
    )
    return (
        Path(workspace)
        / "Work"
        / "runs"
        / str(run_id)
        / "agent-conversations"
        / f"{safe_agent}.handoff.json"
    )


def load_agent_session_handoff(
    workspace: Path,
    run_id: str,
    agent_type: str,
) -> AgentSessionRestore | None:
    """Load the bounded restart projection written by ``AgentLoop``."""

    path = agent_session_handoff_path(workspace, run_id, agent_type)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, Mapping)
        or payload.get("run_id") != str(run_id)
        or payload.get("agent_type") != str(agent_type)
        or not isinstance(payload.get("summary"), Mapping)
    ):
        raise ValueError("persisted Agent handoff does not match its session identity")
    return AgentSessionRestore(
        messages=(),
        handoff_summary=dict(payload["summary"]),
    )


@dataclass(frozen=True)
class ProviderAgentSessionFactory:
    """Create a configured AgentLoop without knowing its domain owner.

    The caller supplies the concrete loop builder and its already-resolved
    constructor inputs.  This keeps Provider, tools, prompt, config, and
    observers at the composition boundary while the Runtime owns the session
    factory passed to AgentExecutionService.
    """

    loop_builder: Callable[..., AgentSessionLoop]
    loop_kwargs: Mapping[str, Any]
    persist_handoff_summary: bool

    def __call__(self) -> AgentSessionLoop:
        loop = self.loop_builder(**dict(self.loop_kwargs))
        loop.persist_handoff_summary = self.persist_handoff_summary
        restore = None
        if self.persist_handoff_summary:
            workspace = self.loop_kwargs.get("workspace")
            run_id = self.loop_kwargs.get("usage_run_id")
            agent_type = self.loop_kwargs.get("agent_type")
            if workspace is not None and run_id and agent_type:
                restore = load_agent_session_handoff(
                    Path(workspace),
                    str(run_id),
                    str(agent_type),
                )
        loop.initial_session_restore = restore
        return loop

    def reconfigure(self, loop: AgentSessionLoop) -> None:
        """Rebind one reused Provider loop to the caller's next typed task."""

        attributes = {
            "tools": "tools",
            "config": "config",
            "llm_provider": "llm_provider",
            "artifact_gateway": "artifact_gateway",
            "usage_run_id": "usage_run_id",
            "usage_task_id": "usage_task_id",
            "system_prompt": "_system_prompt_override",
        }
        for source, target in attributes.items():
            if source in self.loop_kwargs:
                setattr(loop, target, self.loop_kwargs[source])
        loop.persist_handoff_summary = self.persist_handoff_summary


__all__ = [
    "ProviderAgentSessionFactory",
    "agent_session_handoff_path",
    "load_agent_session_handoff",
]
