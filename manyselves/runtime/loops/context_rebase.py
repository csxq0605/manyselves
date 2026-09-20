"""Provider-facing typed context rebase primitives.

The Agent runtime keeps a lossless conversation trace for forensics, but a
Provider request must be assembled from a smaller, typed state.  This module
contains the protocol-neutral pieces used by both Capability rebasers and
``AgentLoop``.  In particular, an assistant tool call and all of its matching
results are one indivisible :class:`AtomicToolUnit`.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from ..providers.base import LLMToolCall
from ..providers.base import Message as LLMMessage


def _message_tool_calls(message: Any) -> list[Any]:
    return list(getattr(message, "tool_calls", None) or ())


def _is_tool_result(message: Any) -> bool:
    return bool(
        getattr(message, "is_tool_result", False)
        or getattr(message, "role", None) == "tool"
    )


def _tool_call_id(message: Any) -> str:
    return str(getattr(message, "tool_call_id", "") or "")


@dataclass(frozen=True)
class AtomicToolUnit:
    """A complete assistant tool-call message plus every matching result.

    A Provider must never receive only one side of a tool exchange.  The
    constructor validates ids, duplicate results and unexpected results.  The
    messages are retained exactly (including Anthropic's ``role="user"`` tool
    results) so adapters can choose their native representation later.
    """

    assistant: Any
    results: tuple[Any, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        calls = _message_tool_calls(self.assistant)
        if getattr(self.assistant, "role", None) != "assistant" or not calls:
            raise ValueError("atomic tool unit requires an assistant tool-call message")
        call_ids = [str(getattr(call, "id", "") or "") for call in calls]
        if any(not call_id for call_id in call_ids):
            raise ValueError("assistant tool calls must have ids")
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("assistant tool-call ids must be unique")
        result_ids = [_tool_call_id(result) for result in self.results]
        if any(not result_id for result_id in result_ids):
            raise ValueError("tool results must identify their tool call")
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("tool result ids must be unique")
        expected = set(call_ids)
        actual = set(result_ids)
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise ValueError(
                "tool-call/result unit is incomplete or mismatched: "
                f"missing={missing}, unexpected={unexpected}"
            )
        if any(not _is_tool_result(result) for result in self.results):
            raise ValueError("atomic unit results must be marked as tool results")

    @property
    def call_ids(self) -> tuple[str, ...]:
        return tuple(str(getattr(call, "id", "") or "") for call in _message_tool_calls(self.assistant))

    @property
    def tool_calls(self) -> tuple[Any, ...]:
        return tuple(_message_tool_calls(self.assistant))

    @property
    def assistant_message(self) -> Any:
        return self.assistant

    @property
    def tool_results(self) -> tuple[Any, ...]:
        return self.results

    @property
    def messages(self) -> tuple[Any, ...]:
        return (self.assistant, *self.results)

    @property
    def complete(self) -> bool:
        """Whether all assistant call ids have matching result ids."""

        return True

    def model_dump(self) -> dict[str, Any]:
        """Return a JSON-friendly forensic representation."""

        def dump(value: Any) -> Any:
            if hasattr(value, "model_dump"):
                return value.model_dump(mode="json")
            if hasattr(value, "__dict__"):
                return {str(k): dump(v) for k, v in vars(value).items()}
            if isinstance(value, (tuple, list)):
                return [dump(item) for item in value]
            if isinstance(value, dict):
                return {str(k): dump(v) for k, v in value.items()}
            return value

        return {"assistant": dump(self.assistant), "results": dump(self.results)}

    @classmethod
    def from_messages(
        cls,
        messages: Sequence[Any],
        start: int = 0,
    ) -> "AtomicToolUnit":
        """Parse one complete unit from ``messages`` beginning at ``start``.

        ``ValueError`` is deliberate for an orphan/incomplete exchange.  A
        caller can then drop the malformed forensic tail rather than exposing
        it as a Provider-visible protocol example.
        """

        if start < 0 or start >= len(messages):
            raise ValueError("atomic unit start is outside the message list")
        assistant = messages[start]
        calls = _message_tool_calls(assistant)
        if getattr(assistant, "role", None) != "assistant" or not calls:
            raise ValueError("message at start is not an assistant tool call")
        expected = {str(getattr(call, "id", "") or "") for call in calls}
        results: list[Any] = []
        index = start + 1
        while index < len(messages) and _is_tool_result(messages[index]):
            result = messages[index]
            result_id = _tool_call_id(result)
            if result_id not in expected or result_id in {
                _tool_call_id(item) for item in results
            }:
                break
            results.append(result)
            index += 1
        if len(results) != len(expected):
            raise ValueError("assistant tool call has no complete matching results")
        return cls(assistant=assistant, results=tuple(results))

    @classmethod
    def from_call_and_result(cls, call: Any, result: Any) -> "AtomicToolUnit":
        """Build a one-call atomic unit from a call and result object."""

        parsed = call if isinstance(call, LLMToolCall) else LLMToolCall(
            id=str(getattr(call, "id", None) or (call.get("id") if isinstance(call, dict) else "")),
            name=str(getattr(call, "name", None) or (call.get("name") if isinstance(call, dict) else "")),
            arguments=dict(getattr(call, "arguments", None) or (call.get("arguments", {}) if isinstance(call, dict) else {})),
        )
        assistant = LLMMessage(role="assistant", content="", tool_calls=[parsed])
        result_message = result if isinstance(result, LLMMessage) else LLMMessage(
            role="tool",
            content=str(result if result is not None else ""),
            tool_call_id=parsed.id,
            is_tool_result=True,
        )
        return cls(assistant=assistant, results=(result_message,))


def atomic_tool_units(messages: Sequence[Any]) -> list[AtomicToolUnit]:
    """Return complete tool exchanges in a history, ignoring orphan tails."""

    units: list[AtomicToolUnit] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if getattr(message, "role", None) == "assistant" and _message_tool_calls(message):
            try:
                unit = AtomicToolUnit.from_messages(messages, index)
            except ValueError:
                # A malformed/incomplete unit is not safe to expose.  It is
                # still available in the forensic trace owned by the caller.
                index += 1
                while index < len(messages) and _is_tool_result(messages[index]):
                    index += 1
                continue
            units.append(unit)
            index += len(unit.messages)
            continue
        index += 1
    return units


@dataclass
class RebasedContext:
    """Result returned by a context rebaser for one Provider dispatch."""

    messages: list[LLMMessage]
    tool_definitions: list[dict[str, Any]] = field(default_factory=list)
    manifest: Any | None = None
    dropped_message_count: int = 0
    forensic_only: bool = False
    reason: str | None = None

    @property
    def tools(self) -> list[dict[str, Any]]:
        """Compatibility alias used by callers that call schemas ``tools``."""

        return self.tool_definitions


@runtime_checkable
class ContextRebuilder(Protocol):
    """Protocol accepted by :class:`~manyselves.runtime.loops.agent_loop.AgentLoop`.

    Implementations may be synchronous or asynchronous.  ``AgentLoop`` passes
    the current typed message list and tool schemas and accepts either a
    :class:`RebasedContext`, a ``(messages, tools)`` pair, or a plain message
    list for backwards-compatible experimental hooks.
    """

    def rebuild(
        self,
        messages: Sequence[LLMMessage],
        tool_definitions: Sequence[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> RebasedContext | Sequence[LLMMessage] | Any:
        ...


def normalize_rebased_context(
    value: Any,
    *,
    fallback_messages: Sequence[LLMMessage],
    fallback_tools: Sequence[dict[str, Any]] | None = None,
) -> RebasedContext:
    """Normalize the permissive hook return forms accepted by AgentLoop."""

    fallback_tools = list(fallback_tools or ())
    if isinstance(value, RebasedContext):
        return value
    if isinstance(value, tuple) and len(value) == 2:
        maybe_messages, maybe_tools = value
        if isinstance(maybe_messages, Sequence):
            return RebasedContext(
                messages=list(maybe_messages),
                tool_definitions=list(maybe_tools or fallback_tools),
            )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, dict)):
        return RebasedContext(
            messages=list(value),
            tool_definitions=fallback_tools,
        )
    return RebasedContext(
        messages=list(fallback_messages),
        tool_definitions=fallback_tools,
    )


async def invoke_rebuilder(
    rebuilder: Any,
    messages: Sequence[LLMMessage],
    tool_definitions: Sequence[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> RebasedContext:
    """Invoke a sync/async rebuilder and normalize its return value."""

    if rebuilder is None:
        return RebasedContext(
            messages=list(messages), tool_definitions=list(tool_definitions or ())
        )
    function = getattr(rebuilder, "rebuild", rebuilder)
    try:
        value = function(
            messages=messages,
            tool_definitions=tool_definitions,
            **kwargs,
        )
    except TypeError:
        # A small compatibility concession for early experimental hooks that
        # accepted positional arguments only.
        try:
            value = function(messages, tool_definitions)
        except TypeError:
            value = function(messages)
    if inspect.isawaitable(value):
        value = await value
    return normalize_rebased_context(
        value,
        fallback_messages=messages,
        fallback_tools=tool_definitions,
    )


__all__ = [
    "AtomicToolUnit",
    "ContextRebuilder",
    "RebasedContext",
    "atomic_tool_units",
    "invoke_rebuilder",
    "normalize_rebased_context",
]
