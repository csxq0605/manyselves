"""Client tool names must survive Antigravity's server-tool name heuristic."""

from types import SimpleNamespace

import pytest

from manyselves.runtime.providers.anthropic_provider import AnthropicProvider
from manyselves.runtime.providers.base import LLMToolCall, Message
from manyselves.runtime.tools.registry import Tool, ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("api_base,expected", [
    ("http://127.0.0.1:8081/antigravity", "client_web_search_2"),
    ("https://api.anthropic.com", "web_search"),
    ("https://example.com/anthropic", "web_search"),
])
async def test_client_search_roundtrip_preserves_runtime_names(streaming, api_base, expected):
    captured = {}
    response = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id="call_2", name=expected,
                                 input={"query": "power"}),
                 SimpleNamespace(type="tool_use", id="call_3", name="empty",
                                 input={"reason": "inspect"})],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        stop_reason="tool_use",
    )

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
    provider.api_base = api_base
    provider.model = "gemini-3.6-flash-medium"
    provider._supports_cache = False
    provider.client = SimpleNamespace(messages=Messages())
    tools = [dict(name=name, description=name, input_schema={"type": "object"})
             for name in ("web_search", "client_web_search", "client_web_search_1", "read")]
    tools.append(dict(name="empty", description="No arguments", input_schema={
        "type": "object", "properties": {}, "additionalProperties": False}))
    messages = [
        Message(role="user", content="Search"),
        Message(role="assistant", content="", tool_calls=[
            LLMToolCall(id="call_1", name="web_search", arguments={"query": "test"})]),
        Message(role="user", content="results", is_tool_result=True, tool_call_id="call_1"),
    ]
    if streaming:
        result = [part async for part in provider.chat_stream(messages, tools=tools)][-1]
    else:
        result = await provider.chat(messages, tools=tools)
    assert captured["tools"][0]["name"] == expected
    assert captured["tools"][1]["name"] == "client_web_search"
    assert captured["messages"][1]["content"][0]["name"] == expected
    assert captured["messages"][2]["content"][0]["tool_use_id"] == "call_1"
    assert result.tool_calls[0].name == "web_search"
    assert result.tool_calls[0].id == "call_2"
    assert result.tool_calls[0].arguments == {"query": "power"}
    assert result.tool_calls[1].arguments == (
        {} if "antigravity" in api_base else {"reason": "inspect"})
    assert tools[0]["name"] == messages[1].tool_calls[0].name == "web_search"


@pytest.mark.parametrize("schema,args,expected", [
    ({"type": "object", "properties": {}, "additionalProperties": False},
     {"reason": "inspect"}, {}),
    ({"type": "object", "properties": {}}, {"reason": "inspect"}, {"reason": "inspect"}),
    ({"type": "object", "properties": {}}, {"unexpected": 1}, {"unexpected": 1}),
    ({"type": "object", "properties": {"reason": {"type": "string"}}},
     {"reason": "inspect"}, {"reason": "inspect"}),
])
def test_antigravity_removes_only_injected_empty_tool_reason(schema, args, expected):
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.api_base = "http://127.0.0.1:8081/antigravity"
    tools = [{"name": "inspect", "input_schema": schema}]
    assert provider._client_tool_arguments("inspect", args, tools) == expected
    assert provider._client_tool_arguments("unknown", args, tools) == args
    provider.api_base = "https://api.anthropic.com"
    assert provider._client_tool_arguments("inspect", args, tools) == args


def test_empty_callable_registry_schema_reaches_provider_adapter():
    class Inspect(Tool):
        name = "inspect"

        async def __call__(self):
            return {"parts": []}

    registry = ToolRegistry()
    registry.register(Inspect())
    tools = registry.get_definitions()
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.api_base = "http://127.0.0.1:8081/antigravity"
    assert provider._client_tool_arguments("inspect", {"reason": "inspect"}, tools) == {}


def test_variadic_callable_registry_schema_stays_open():
    class Inspect(Tool):
        name = "inspect"

        async def __call__(self, **kwargs):
            return kwargs

    registry = ToolRegistry()
    registry.register(Inspect())
    assert "additionalProperties" not in registry.get_definitions()[0]["input_schema"]
