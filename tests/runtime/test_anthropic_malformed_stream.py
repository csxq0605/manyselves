"""Broken streamed tool JSON becomes a retryable error that keeps what arrived."""

from types import SimpleNamespace

import pytest
from jiter import from_json

from manyselves.runtime.providers.anthropic_provider import AnthropicProvider
from manyselves.runtime.providers.base import Message
from manyselves.runtime.runtime_errors import MalformedModelOutputError


def _jiter_error() -> ValueError:
    try:
        from_json(b'{"a" {', partial_mode=True)
    except ValueError as exc:
        return exc
    raise AssertionError("jiter accepted broken JSON")


def _provider(snapshot) -> AnthropicProvider:
    class Stream:
        current_message_snapshot = snapshot

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise _jiter_error()

    class Messages:
        def stream(self, **kwargs):
            return Stream()

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.api_base = "https://example.com/anthropic"
    provider.model = "m"
    provider._supports_cache = False
    provider.client = SimpleNamespace(messages=Messages())
    return provider


@pytest.mark.asyncio
async def test_broken_tool_json_is_reported_with_the_received_text():
    block = SimpleNamespace(type="tool_use", name="submit_result", input={})
    setattr(block, "__json_buf", b'{"a"')
    snapshot = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok"), block])
    provider = _provider(snapshot)

    with pytest.raises(MalformedModelOutputError) as caught:
        async for _ in provider.chat_stream([Message(role="user", content="go")]):
            pass

    assert "expected" in str(caught.value)
    assert caught.value.raw_output == 'ok\n<tool_use name="submit_result">{"a"</tool_use>'


@pytest.mark.asyncio
async def test_other_value_errors_keep_their_type():
    provider = _provider(SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")]))

    with pytest.raises(ValueError) as caught:
        async for _ in provider.chat_stream([Message(role="user", content="go")]):
            pass

    assert not isinstance(caught.value, MalformedModelOutputError)
