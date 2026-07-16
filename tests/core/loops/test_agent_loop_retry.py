import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.core.loops.agent_loop import AgentLoop
from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider, LLMResponse, LLMStreamChunk, LLMToolCall
from manyselves.core.tools.registry import Tool, ToolRegistry
from manyselves.interfaces.types import SystemNotice


class HttpFailureError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class EventuallySuccessfulProvider(LLMProvider):
    def __init__(self, failures: int):
        super().__init__("key", model="retry-model")
        self.failures = failures
        self.calls = 0

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        self.calls += 1
        if self.calls <= self.failures:
            raise HttpFailureError(503, "temporary outage")
        return LLMResponse(content="recovered")


class PartialStreamFailureProvider(LLMProvider):
    def __init__(self):
        super().__init__("key", model="partial-model")
        self.calls = 0

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        raise AssertionError("streaming path should be used")

    async def chat_stream(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        self.calls += 1
        yield LLMStreamChunk(delta="partial")
        raise HttpFailureError(503, "stream disconnected")


class BlockingTool(Tool):
    name = "blocking_tool"
    description = "Wait until cancelled."

    def __init__(self):
        self.started = asyncio.Event()

    async def __call__(self, wait: bool = True) -> dict:
        assert wait is True
        self.started.set()
        await asyncio.Event().wait()
        return {"status": "unexpected"}


def _loop(tmp_path: Path, provider: LLMProvider) -> AgentLoop:
    return AgentLoop(
        agent_type="main",
        workspace=tmp_path,
        tools=ToolRegistry(),
        bus=MessageBus(),
        config=AgentDefaults(),
        llm_provider=provider,
    )


@pytest.mark.asyncio
async def test_provider_retry_is_bounded_and_announced(tmp_path: Path, monkeypatch):
    provider = EventuallySuccessfulProvider(failures=2)
    loop = _loop(tmp_path, provider)

    async def no_wait(_delay: float) -> bool:
        return False

    monkeypatch.setattr(loop, "_wait_before_retry", no_wait)

    response = await loop._chat_with_retries([], None, "message-1")

    assert response.content == "recovered"
    assert provider.calls == 3
    notices = [item for item in list(loop.bus._queue._queue) if isinstance(item, SystemNotice)]
    assert len(notices) == 2
    assert "自动重试（1/2）" in notices[0].content
    assert "自动重试（2/2）" in notices[1].content


@pytest.mark.asyncio
async def test_provider_retry_stops_after_two_retries(tmp_path: Path, monkeypatch):
    provider = EventuallySuccessfulProvider(failures=10)
    loop = _loop(tmp_path, provider)

    async def no_wait(_delay: float) -> bool:
        return False

    monkeypatch.setattr(loop, "_wait_before_retry", no_wait)

    with pytest.raises(RuntimeError, match="已自动重试2次仍失败"):
        await loop._chat_with_retries([], None, "message-1")

    assert provider.calls == 3


@pytest.mark.asyncio
async def test_partial_stream_failure_is_not_retried(tmp_path: Path, monkeypatch):
    provider = PartialStreamFailureProvider()
    loop = _loop(tmp_path, provider)

    async def should_not_wait(_delay: float) -> bool:
        raise AssertionError("partial output must not be retried")

    monkeypatch.setattr(loop, "_wait_before_retry", should_not_wait)

    with pytest.raises(RuntimeError, match="部分内容"):
        await loop._chat_with_retries([], None, "message-1")

    assert provider.calls == 1


@pytest.mark.asyncio
async def test_cancel_current_cancels_active_long_running_tool(tmp_path: Path):
    provider = EventuallySuccessfulProvider(failures=0)
    tool = BlockingTool()
    registry = ToolRegistry()
    registry.register(tool)
    loop = AgentLoop(
        agent_type="main",
        workspace=tmp_path,
        tools=registry,
        bus=MessageBus(),
        config=AgentDefaults(),
        llm_provider=provider,
    )
    response = SimpleNamespace(
        content="",
        thinking=None,
        usage=None,
        tool_calls=[LLMToolCall(id="tool-1", name="blocking_tool", arguments={"wait": True})],
    )

    task = asyncio.create_task(loop._handle_tool_calls(response, "message-1"))
    await asyncio.wait_for(tool.started.wait(), timeout=1)
    loop.cancel_current()
    await asyncio.wait_for(task, timeout=1)

    assert loop._active_tool_task is None
