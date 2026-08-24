"""Business-neutral resources supplied to Capability runtime bindings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..config.schema import AgentDefaults

if TYPE_CHECKING:
    from ..core.loops.bus import MessageBus
    from ..core.providers.base import LLMProvider


@dataclass(frozen=True, slots=True)
class RuntimeServicesView:
    """Read-only view of resources owned by one account RuntimeHost."""

    workspace: Path | None
    bus: MessageBus
    active_provider: LLMProvider | None
    agent_defaults: AgentDefaults
    global_knowledge_root: Path | None


__all__ = ["RuntimeServicesView"]
