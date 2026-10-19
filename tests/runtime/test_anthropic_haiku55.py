"""Haiku 5.5 sampling and thinking parameters on the actual SDK boundary."""
from types import SimpleNamespace

import pytest

from manyselves.runtime.providers.anthropic_provider import AnthropicProvider
from manyselves.runtime.providers.base import Message


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("model", ["claude-haiku-5-5", "claude-haiku-4-5"])
@pytest.mark.parametrize("with_tools", [False, True])
@pytest.mark.parametrize("reasoning", [None, False])
async def test_haiku_request_parameters(streaming, model, with_tools, reasoning):
    captured = {}
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text="{}")],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=2),
                               stop_reason="end_turn")

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def get_final_message(self):
            return response

    class Messages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return response

        def stream(self, **kwargs):
            captured.update(kwargs)
            return Stream()

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.model = model
    provider.api_base = "https://api.anthropic.com"
    provider._supports_cache = True
    provider.client = SimpleNamespace(messages=Messages())
    messages = [Message(role="user", content="summarize")]
    tools = [{"name": "inspect", "description": "Inspect", "input_schema": {"type": "object"}}] if with_tools else None
    if streaming:
        result = [part async for part in provider.chat_stream(messages, tools=tools, reasoning_enabled=reasoning)][-1]
    else:
        result = await provider.chat(messages, tools=tools, reasoning_enabled=reasoning)
    assert result.usage["input_tokens"] == 10
    if model == "claude-haiku-5-5":
        assert "temperature" not in captured
        assert captured["thinking"] == {"type": "disabled" if with_tools else "adaptive"}
        if reasoning is False or with_tools:
            assert captured["output_config"] == {"effort": "low" if reasoning is False else "medium"}
        else:
            assert "output_config" not in captured
    else:
        assert captured["temperature"] == 0.1
        if reasoning is False:
            assert captured["thinking"] == {"type": "disabled"}
        else:
            assert "thinking" not in captured
