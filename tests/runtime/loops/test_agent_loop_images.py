"""User messages may carry images; they reach the Provider, survive restore and count toward the budget."""

from types import SimpleNamespace

from manyselves.config.schema import AgentDefaults
from manyselves.runtime.loops.agent_loop import IMAGE_TOKEN_ESTIMATE, AgentLoop, _estimate_tokens
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.base import ImageInput, LLMProvider, LLMResponse, Message
from manyselves.runtime.tools.registry import ToolRegistry


def test_restored_conversation_keeps_images(tmp_path):
    loop = AgentLoop(agent_type="test", workspace=tmp_path, tools=ToolRegistry(),
                     bus=MessageBus(), config=AgentDefaults(working_memory_tokens=4096),
                     llm_provider=SimpleNamespace(model="main-model"))
    image = {"path": str(tmp_path / "p.png"), "media_type": "image/png"}
    loop.restore_conversation([{"role": "user", "content": "读图", "images": [image]}])
    [message] = loop._conversation_history
    assert message.images == (ImageInput(image["path"], "image/png"),)


def test_images_count_toward_the_token_estimate():
    plain = Message(role="user", content="读图")
    with_image = Message(role="user", content="读图", images=(ImageInput("/x.png", "image/png"),))
    assert _estimate_tokens([with_image]) - _estimate_tokens([plain]) == IMAGE_TOKEN_ESTIMATE


class _CapturingProvider(LLMProvider):
    """Records the messages of every request and answers at once."""

    def __init__(self) -> None:
        super().__init__("key", model="vision-model")
        self.requests: list[list[Message]] = []

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        self.requests.append(list(messages))
        return LLMResponse(content="done")


async def test_turn_images_reach_the_provider_request(tmp_path):
    import asyncio

    from manyselves.runtime.agent_execution import AgentExecutionService, AgentTurnRequest

    image_path = tmp_path / "p.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    provider = _CapturingProvider()
    service = AgentExecutionService(bus)
    try:
        session = await service.start_or_restore(
            workflow_id="wf", conversation_key="conv", runtime_id="reader",
            session_id="session-reader",
            session_factory=lambda: AgentLoop(
                agent_type="reader", workspace=tmp_path, tools=ToolRegistry(), bus=bus,
                config=AgentDefaults(), llm_provider=provider,
            ),
        )
        await asyncio.wait_for(service.dispatch_turn(session, AgentTurnRequest(
            content="读图", images=({"path": str(image_path), "media_type": "image/png"},),
            message_id="m1", workflow_id="wf", run_id="run", task_id="task",
            task_attempt_id="attempt",
        )), timeout=10)
    finally:
        await service.close_workflow("wf")
        bus.shutdown()
        await bus_task

    assert provider.requests
    sent = [m for m in provider.requests[0] if m.role == "user" and m.images]
    assert [m.images for m in sent] == [(ImageInput(str(image_path), "image/png"),)]
