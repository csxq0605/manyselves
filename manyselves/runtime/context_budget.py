"""Request limits read from the model's declared capability.

The Provider configuration declares the model's context window and max output
per request.  Every Agent request may use that output limit, and history is
compacted only when it would no longer fit the window next to that output.
``AgentDefaults`` may set lower values on purpose; the fallbacks below apply
only when neither the configuration nor the model declares anything.

A typed task input (the packet a Capability hands one Agent turn) stays in
every request of that turn: history compaction keeps it and only shrinks the
rest.  A packet above the working memory leaves compaction nothing it can
free, and a packet above the window fails outright.  Callers that can split
their input into independent parts size each part with
:func:`retained_input_budget` before sending.
"""

from __future__ import annotations

from typing import Any

from .loops.agent_loop import _SAFETY_BUFFER, _estimate_text_tokens

# Used only when neither the configuration nor the model declares a value.
DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_MAX_OUTPUT_TOKENS = 8192


def estimate_text_tokens(text: str) -> int:
    """Provider token estimate used by the Agent loop's own budget checks."""

    return _estimate_text_tokens(text)


def provider_context_window(*owners: Any) -> int:
    """First positive context window declared by a config or Provider object."""

    return (
        _first_positive(owners, ("context_window", "max_context_tokens", "context_length"))
        or DEFAULT_CONTEXT_WINDOW
    )


def _first_positive(owners: tuple[Any, ...], names: tuple[str, ...]) -> int | None:
    for owner in owners:
        for name in names:
            value = getattr(owner, name, None)
            if value is None:
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
    return None


def request_output_limit(config: Any, provider: Any = None) -> int:
    """Max output of one request: an explicit setting, else the model's own."""

    return (
        _first_positive((config,), ("max_tokens",))
        or _first_positive((provider,), ("max_output_tokens",))
        or DEFAULT_MAX_OUTPUT_TOKENS
    )


def working_memory_limit(config: Any, provider: Any = None) -> int:
    """History size that triggers compaction.

    An explicit setting wins.  Otherwise history may grow until it would no
    longer fit the window next to the request's output.
    """

    explicit = _first_positive((config,), ("working_memory_tokens",))
    if explicit is not None:
        return explicit
    window = provider_context_window(config, provider)
    return max(1, window - request_output_limit(config, provider) - _SAFETY_BUFFER)


def retained_input_budget(
    *,
    context_window: int,
    max_output: int,
    working_memory_tokens: int,
    tool_tokens: int = 0,
) -> int:
    """Largest retained input the window and the working memory can hold."""

    window_room = int(context_window) - int(max_output) - int(tool_tokens) - _SAFETY_BUFFER
    return max(0, min(window_room, int(working_memory_tokens)))


def retained_input_budget_for(config: Any, provider: Any = None) -> int:
    """:func:`retained_input_budget` for an Agent config and its Provider."""

    return retained_input_budget(
        context_window=provider_context_window(config, provider),
        max_output=request_output_limit(config, provider),
        working_memory_tokens=working_memory_limit(config, provider),
    )


__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "estimate_text_tokens",
    "provider_context_window",
    "request_output_limit",
    "retained_input_budget",
    "retained_input_budget_for",
    "working_memory_limit",
]
