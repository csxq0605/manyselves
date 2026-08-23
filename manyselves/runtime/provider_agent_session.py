"""Business-neutral construction of one configured Provider AgentLoop."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .agent_execution import AgentSessionLoop


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
        return loop


__all__ = ["ProviderAgentSessionFactory"]
