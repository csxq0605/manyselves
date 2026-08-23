"""Agent loop management for Manyselves."""

__all__ = [
    "MessageBus",
    "AgentLoop",
    "LoopManager",
]


def __getattr__(name: str):
    """Keep the reporting-aware loop manager out of neutral imports."""

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
