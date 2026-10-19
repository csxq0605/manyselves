"""Cancelling the caller of an Agent turn stops that turn's model and tool work."""

import asyncio

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.runtime.agent_execution import AgentExecutionService, AgentTurnRequest
from manyselves.runtime.loops.agent_loop import AgentLoop
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.base import LLMProvider, LLMResponse, LLMToolCall
from manyselves.runtime.tools.registry import Tool, ToolRegistry


class SlowToolCallProvider(LLMProvider):
    """Answers after a delay with a call to the file-writing tool."""

    def __init__(self) -> None:
        super().__init__("key", model="slow-model")
        self.calls = 0

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        self.calls += 1
        await asyncio.sleep(0.2)
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[LLMToolCall(id="write-1", name="write_marker", arguments={})],
            )
        return LLMResponse(content="done")


class WriteMarkerTool(Tool):
    name = "write_marker"
    description = "Write a marker file."

    def __init__(self, path) -> None:
        self.path = path

    async def __call__(self) -> dict:
        self.path.write_text("written", encoding="utf-8")
        return {"status": "ok"}


@pytest.mark.asyncio
async def test_cancelled_turn_does_not_run_tools_afterwards(tmp_path) -> None:
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    marker = tmp_path / "marker.txt"
    tools = ToolRegistry()
    tools.register(WriteMarkerTool(marker))
    provider = SlowToolCallProvider()
    service = AgentExecutionService(bus)
    try:
        session = await service.start_or_restore(
            workflow_id="wf",
            conversation_key="conv",
            runtime_id="agent-a",
            session_id="session-a",
            session_factory=lambda: AgentLoop(
                agent_type="agent-a", workspace=tmp_path, tools=tools, bus=bus,
                config=AgentDefaults(), llm_provider=provider,
            ),
        )
        turn = asyncio.create_task(service.dispatch_turn(session, AgentTurnRequest(
            content="write the marker", message_id="m1", workflow_id="wf",
            run_id="run", task_id="task", task_attempt_id="attempt",
        )))
        await asyncio.sleep(0.05)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        await asyncio.sleep(0.6)
        assert not marker.exists()
    finally:
        await service.close_workflow("wf")
        bus.shutdown()
        await bus_task
