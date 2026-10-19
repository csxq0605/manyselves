"""Size a retained task input before it is sent.

A typed task input (the packet a Capability hands one Agent turn) stays in
every request of that turn: history compaction keeps it and only shrinks the
rest.  Compaction targets ``working_memory_tokens * compaction_target_ratio``
in total.  A packet above that target leaves compaction nothing it can free,
so every later request compacts again and the Agent has no room to work; a
packet above the Provider window fails outright.  Callers that can split
their input (for example by independent objects) size each part with
:func:`retained_input_budget` before sending.
"""

from __future__ import annotations

from typing import Any

from .loops.agent_loop import _SAFETY_BUFFER, _estimate_text_tokens

DEFAULT_CONTEXT_WINDOW = 128_000


def estimate_text_tokens(text: str) -> int:
    """Provider token estimate used by the Agent loop's own budget checks."""

    return _estimate_text_tokens(text)


def provider_context_window(*owners: Any) -> int:
    """First positive context window declared by a config or Provider object."""

    for owner in owners:
        for name in ("context_window", "max_context_tokens", "context_length"):
            value = getattr(owner, name, None)
            if value is None:
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
    return DEFAULT_CONTEXT_WINDOW


def retained_input_budget(
    *,
    context_window: int,
    max_output: int,
    working_memory_tokens: int,
    compaction_target_ratio: float,
    tool_tokens: int = 0,
) -> int:
    """Largest retained input that still leaves compaction and the window room."""

    window_room = int(context_window) - int(max_output) - int(tool_tokens) - _SAFETY_BUFFER
    compaction_target = int(int(working_memory_tokens) * float(compaction_target_ratio))
    return max(0, min(window_room, compaction_target))


def retained_input_budget_for(config: Any, provider: Any = None) -> int:
    """:func:`retained_input_budget` for an Agent config and its Provider."""

    return retained_input_budget(
        context_window=provider_context_window(config, provider),
        max_output=int(config.max_tokens),
        working_memory_tokens=int(config.working_memory_tokens),
        compaction_target_ratio=float(config.working_memory_compaction_target_ratio),
    )


__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "estimate_text_tokens",
    "provider_context_window",
    "retained_input_budget",
    "retained_input_budget_for",
]
