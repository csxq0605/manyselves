"""Business-neutral resources projected from one account RuntimeHost.

This module is intentionally an internal composition boundary.  It only
forwards resources that the account-scoped RuntimeHost already owns; it does
not select providers, build tools, interpret workflows, or add lifecycle
policies.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..config.schema import AgentDefaults

if TYPE_CHECKING:
    from ..core.loops.bus import MessageBus
    from ..core.providers.base import LLMProvider
    from .runtime_host import RuntimeHost


@dataclass(frozen=True, slots=True)
class RuntimeServicesView:
    """Read-only view of existing account runtime composition resources."""

    workspace: Path | None
    bus: MessageBus
    active_provider: LLMProvider | None
    agent_defaults: AgentDefaults
    global_knowledge_root: Path | None


def build_runtime_services_view(host: RuntimeHost) -> RuntimeServicesView:
    """Project existing Host resources without creating a second runtime owner.

    ``LoopManager`` normally exposes the selected provider through its Main
    loop.  During degraded startup no Main loop is created, so the existing
    provider-manager selection is read as a fallback.  A missing active
    provider remains ``None`` and is not converted into a new runtime gate.
    """

    manager = host.loop_manager
    main_loop = manager.get_loop("main") if manager is not None else None

    provider = getattr(main_loop, "llm_provider", None)
    if provider is None:
        provider_manager = (
            manager._provider_manager  # noqa: SLF001 - internal composition view
            if manager is not None
            else None
        )
        if provider_manager is not None:
            try:
                provider = provider_manager.get_active_provider()
            except ValueError:
                # ProviderManager's existing no-active-provider/degraded mode.
                provider = None

    config = host.config_manager.config
    return RuntimeServicesView(
        workspace=host.workspace,
        bus=host.bus,
        active_provider=provider,
        agent_defaults=config.agents.defaults,
        global_knowledge_root=host.global_knowledge_root,
    )


__all__ = ["RuntimeServicesView", "build_runtime_services_view"]
