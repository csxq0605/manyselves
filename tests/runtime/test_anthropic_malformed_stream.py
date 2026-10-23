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


def _real_stream_provider(chunks: list[str]):
    """A provider over the SDK's own message stream, fed these tool-input chunks."""

    from anthropic._models import construct_type
    from anthropic.lib.streaming import AsyncMessageStream
    from anthropic.types import RawMessageStreamEvent

    raw_events = [
        {"type": "message_start", "message": {
            "id": "m", "type": "message", "role": "assistant", "model": "m",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 0},
        }},
        {"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "t1", "name": "submit_result", "input": {},
        }},
        *(
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": chunk}}
            for chunk in chunks
        ),
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
         "usage": {"output_tokens": 42}},
        {"type": "message_stop"},
    ]
    events = [construct_type(type_=RawMessageStreamEvent, value=e) for e in raw_events]

    class Raw:
        def __init__(self):
            async def produce():
                for event in events:
                    yield event
            self._iterator = produce()

        async def __aiter__(self):
            async for item in self._iterator:
                yield item

        async def close(self):
            return None

    class Manager:
        async def __aenter__(self):
            from anthropic import NOT_GIVEN
            self.stream = AsyncMessageStream(Raw(), output_format=NOT_GIVEN)
            return self.stream

        async def __aexit__(self, *args):
            return False

    class Messages:
        def stream(self, **kwargs):
            return Manager()

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.api_base = "https://example.com/anthropic"
    provider.model = "m"
    provider._supports_cache = False
    provider.client = SimpleNamespace(messages=Messages())
    return provider


async def _final(provider):
    chunks = [c async for c in provider.chat_stream([Message(role="user", content="go")])]
    return chunks[-1]


@pytest.mark.asyncio
async def test_an_unclosed_object_is_repaired_without_a_new_sample():
    provider = _real_stream_provider([
        '{"rows": [{"id": "J-1", "action": {"m": "x"}',
        ', {"id": "J-2", "action": {"m": "y"}}]}',
    ])

    final = await _final(provider)

    assert final.tool_calls[0].arguments == {
        "rows": [{"id": "J-1", "action": {"m": "x"}}, {"id": "J-2", "action": {"m": "y"}}]
    }
    assert final.usage["output_tokens"] == 42
    assert final.stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_a_raw_line_break_in_a_string_is_accepted():
    provider = _real_stream_provider(['{"sections": {"2.1": "第一段', '\n第二段"}}'])

    final = await _final(provider)

    assert final.tool_calls[0].arguments == {"sections": {"2.1": "第一段\n第二段"}}


@pytest.mark.asyncio
async def test_an_input_that_cannot_be_repaired_is_still_resampled():
    provider = _real_stream_provider(['{"a" ', '1}'])

    with pytest.raises(MalformedModelOutputError):
        await _final(provider)
