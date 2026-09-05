"""Business-neutral resources supplied to Capability runtime bindings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..config.schema import AgentDefaults

if TYPE_CHECKING:
    from .loops.bus import MessageBus
    from .providers.base import LLMProvider


@dataclass(frozen=True, slots=True, init=False)
class RuntimeServicesView:
    """Read-only resources, optionally resolving replaceable provider state live."""

    workspace: Path | None
    bus: MessageBus
    global_knowledge_root: Path | None
    _resolve_provider: Callable[[], LLMProvider | None]
    _resolve_defaults: Callable[[], AgentDefaults]

    def __init__(
        self,
        workspace: Path | None,
        bus: MessageBus,
        active_provider: LLMProvider | None,
        agent_defaults: AgentDefaults,
        global_knowledge_root: Path | None,
        *,
        resolve_provider: Callable[[], LLMProvider | None] | None = None,
        resolve_defaults: Callable[[], AgentDefaults] | None = None,
    ) -> None:
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "bus", bus)
        object.__setattr__(self, "global_knowledge_root", global_knowledge_root)
        object.__setattr__(
            self, "_resolve_provider", resolve_provider or (lambda: active_provider)
        )
        object.__setattr__(
            self, "_resolve_defaults", resolve_defaults or (lambda: agent_defaults)
        )

    @property
    def active_provider(self) -> LLMProvider | None:
        return self._resolve_provider()

    @property
    def agent_defaults(self) -> AgentDefaults:
        return self._resolve_defaults()


__all__ = ["RuntimeServicesView"]
