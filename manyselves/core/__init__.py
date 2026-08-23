"""Core runtime for Manyselves."""

__all__ = [
    "ToolRegistry",
    "MessageBus",
    "AgentLoop",
    "LoopManager",
]


def __getattr__(name: str):
    """Load legacy convenience exports without importing reporting eagerly."""

    if name == "ToolRegistry":
        from .tools import ToolRegistry

        return ToolRegistry
    if name in {"AgentLoop", "LoopManager", "MessageBus"}:
        from .loops import AgentLoop, LoopManager, MessageBus

        return {
            "AgentLoop": AgentLoop,
            "LoopManager": LoopManager,
            "MessageBus": MessageBus,
        }[name]
    raise AttributeError(name)
