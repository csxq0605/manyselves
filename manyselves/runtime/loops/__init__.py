"""Agent loop management for Manyselves."""

__all__ = [
    "MessageBus",
    "AgentLoop",
    "LoopManager",
]


def __getattr__(name: str):
    """Keep the fully composed loop manager out of lightweight imports."""

    if name == "AgentLoop":
        from .agent_loop import AgentLoop

        return AgentLoop
    if name == "MessageBus":
        from .bus import MessageBus

        return MessageBus
    if name == "LoopManager":
        from .manager import LoopManager

        return LoopManager
    raise AttributeError(name)
