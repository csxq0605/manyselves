"""LLM Provider base classes and interfaces."""

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ProviderRequestDisposition(StrEnum):
    """Forensic status of a failed physical provider request.

    Transport failures default to ``accepted_or_unknown`` because the absence
    of a first token does not prove that the provider did not receive the
    request. This value is telemetry, not a recovery lock: retry policy is
    determined separately from whether a response was received locally.
    """

    NOT_SENT = "not_sent"
    DEFINITELY_REJECTED = "definitely_rejected"
    ACCEPTED_OR_UNKNOWN = "accepted_or_unknown"


class ProviderRequestError(RuntimeError):
    """Fallback wrapper for exceptions that cannot carry adapter metadata."""

    def __init__(
        self,
        original: BaseException,
        disposition: ProviderRequestDisposition,
    ) -> None:
        super().__init__(str(original))
        self.original = original
        self.attempt_disposition = disposition.value
        self.status_code = getattr(original, "status_code", None)
        self.response = getattr(original, "response", None)


def provider_request_disposition(
    exc: BaseException,
) -> ProviderRequestDisposition:
    """Read adapter evidence, conservatively treating missing evidence as ambiguous."""

    value = getattr(exc, "attempt_disposition", None)
    try:
        return ProviderRequestDisposition(value)
    except (TypeError, ValueError):
        return ProviderRequestDisposition.ACCEPTED_OR_UNKNOWN


def infer_provider_request_disposition(
    exc: BaseException,
    *,
    stream_opened: bool = False,
) -> ProviderRequestDisposition:
    """Infer only dispositions that an HTTP adapter can establish safely.

    A response that explicitly rejects the request before a stream opens is
    definitive.  Conflict, timeout, connection, and server failures remain
    ambiguous because they can occur after the provider accepted work.
    """

    if stream_opened:
        return ProviderRequestDisposition.ACCEPTED_OR_UNKNOWN
    status_code = getattr(exc, "status_code", None)
    if not isinstance(status_code, int):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    if status_code in {400, 401, 403, 404, 405, 413, 415, 422, 425, 429}:
        return ProviderRequestDisposition.DEFINITELY_REJECTED
    return ProviderRequestDisposition.ACCEPTED_OR_UNKNOWN


def annotate_provider_request_failure(
    exc: BaseException,
    disposition: ProviderRequestDisposition,
) -> BaseException:
    """Attach adapter disposition without erasing the SDK exception type."""

    retry_after = provider_retry_after(exc)
    if retry_after is not None:
        try:
            setattr(exc, "retry_after", retry_after)
        except (AttributeError, TypeError):
            pass

    try:
        setattr(exc, "attempt_disposition", disposition.value)
    except (AttributeError, TypeError):
        return ProviderRequestError(exc, disposition)
    return exc


def provider_retry_after(exc: BaseException) -> float | None:
    """Extract a bounded Retry-After hint without making it mandatory.

    SDKs expose headers differently.  This helper is deliberately advisory:
    admission still applies its shared cooldown and jitter, while the absence
    of a parseable header falls back to the controller's default delay.
    """

    original = getattr(exc, "original", exc)
    value = getattr(original, "retry_after", None)
    response = getattr(original, "response", None)
    headers = getattr(response, "headers", None)
    if value is None and headers is not None:
        try:
            value = headers.get("retry-after") or headers.get("Retry-After")
        except AttributeError:
            value = None
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        # HTTP-date values are optional and provider SDKs usually normalize
        # them before exposing the exception.  Keep this path conservative.
        return None
    if parsed < 0:
        return 0.0
    return min(parsed, 3600.0)


def build_provider_request_metrics(
    payload: dict[str, Any],
    *,
    representation: str,
    message_keys: tuple[str, ...] = ("messages", "system"),
    tool_key: str = "tools",
) -> dict[str, Any]:
    """Hash the canonical provider-adapter payload without retaining its content."""

    def canonical(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    message_payload = {
        key: payload[key]
        for key in message_keys
        if key in payload
    }
    tool_payload = payload.get(tool_key, [])
    serialized_request = canonical(payload)
    serialized_messages = canonical(message_payload)
    serialized_tools = canonical(tool_payload)
    return {
        "representation": representation,
        "request_fingerprint": hashlib.sha256(
            serialized_request.encode("utf-8")
        ).hexdigest(),
        "message_fingerprint": hashlib.sha256(
            serialized_messages.encode("utf-8")
        ).hexdigest(),
        "tool_schema_fingerprint": hashlib.sha256(
            serialized_tools.encode("utf-8")
        ).hexdigest(),
        "request_chars": len(serialized_request),
        "message_chars": len(serialized_messages),
        "tool_schema_chars": len(serialized_tools),
    }


@dataclass
class LLMToolCall:
    """Tool call from LLM."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    """Tool result to send back to LLM."""

    tool_call_id: str
    content: str


@dataclass
class Message:
    """Chat message with optional structured content for tool calls/results.

    Providers convert these to API-specific formats:
    - Anthropic: tool_calls → content blocks with type="tool_use",
                 tool_results → user message with type="tool_result" blocks
    - OpenAI-compat: tool_calls → assistant message with tool_calls field,
                     tool_results → separate "tool" role messages
    """

    role: str  # "user", "assistant", "system"
    content: str
    tool_calls: list[LLMToolCall] | None = None  # assistant messages with tool calls
    tool_call_id: str | None = None  # tool result messages (role="tool" for OpenAI)
    is_tool_result: bool = False  # marks this as a tool result message
    thinking: str | None = None  # DeepSeek extended thinking blocks
    cache_control: bool = False  # mark for prompt caching (Anthropic ephemeral cache)


@dataclass
class LLMResponse:
    """Response from LLM."""

    content: str | None
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    usage: dict[str, int] | None = None
    streaming: bool = False  # Whether this is a streaming chunk
    thinking: str | None = None  # DeepSeek extended thinking
    stop_reason: str | None = None  # Provider terminal reason (for example max_tokens)
    request_metrics: dict[str, Any] | None = None  # Canonical payload hashes/chars passed to SDK


@dataclass
class LLMStreamChunk:
    """Single chunk from streaming LLM response."""

    delta: str | None = None  # Text content delta (None if no new text)
    tool_calls: list[LLMToolCall] | None = None  # Final tool calls at end
    done: bool = False  # Whether stream is complete
    thinking: str | None = None  # DeepSeek extended thinking
    usage: dict[str, int] | None = None  # Provider usage, normally on the final chunk
    stop_reason: str | None = None  # Provider terminal reason
    request_metrics: dict[str, Any] | None = None  # Present on the terminal chunk


class LLMProvider(ABC):
    """Base class for LLM providers."""

    def __init__(
        self,
        api_key: str,
        api_base: str | None = None,
        model: str | None = None,
    ):
        """Initialize provider.

        Args:
            api_key: API key for authentication.
            api_base: Optional custom API base URL.
            model: Default model to use.
        """
        self.api_key = api_key
        self.api_base = api_base
        self.model = model

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 8192,
    ) -> LLMResponse:
        """Send chat completion request.

        Args:
            messages: List of chat messages (may contain tool calls/results).
            tools: Optional list of tool definitions for function calling.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.

        Returns:
            LLM response with content and/or tool calls.
        """

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 8192,
        stream_idle_timeout_seconds: float | None = None,
    ):
        """Send streaming chat completion request.

        Yields LLMStreamChunk objects as text arrives.

        Args:
            messages: List of chat messages.
            tools: Optional list of tool definitions.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            stream_idle_timeout_seconds: Optional per-request idle timeout.

        Yields:
            LLMStreamChunk with delta content for each chunk.
        """
        raise NotImplementedError(f"Streaming not implemented for {self.__class__.__name__}")

    async def chat_structured(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.1,
        max_tokens: int = 8192,
    ) -> LLMResponse:
        """Request a JSON object from providers that expose a native JSON mode."""

        raise NotImplementedError(
            f"Structured output not implemented for {self.__class__.__name__}"
        )
