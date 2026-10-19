"""User messages may carry images; they survive restore and count toward the budget."""

from types import SimpleNamespace

from manyselves.config.schema import AgentDefaults
from manyselves.runtime.loops.agent_loop import IMAGE_TOKEN_ESTIMATE, AgentLoop, _estimate_tokens
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.base import ImageInput, Message
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
