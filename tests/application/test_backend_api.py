"""Behavioral contract for the extracted desktop backend adapter."""

import asyncio
from types import SimpleNamespace

import pytest


class _FakeAgentLoop:
    def __init__(self) -> None:
        self._current_session_id = "old-session"
        self._conversation_history = [object()]
        self._message_queue: asyncio.Queue[str] = asyncio.Queue()
        self._message_queue.put_nowait("queued-one")
        self._message_queue.put_nowait("queued-two")
        self.cancel_calls = 0
        self.queue_update_calls = 0

    def cancel_current(self) -> None:
        self.cancel_calls += 1

    async def _publish_queue_update(self) -> None:
        self.queue_update_calls += 1


class _FakeLoopManager:
    def __init__(self, loop: _FakeAgentLoop) -> None:
        self.loop = loop

    def get_loop(self, agent_type: str) -> _FakeAgentLoop | None:
        return self.loop if agent_type == "main" else None


def test_legacy_backend_api_import_is_the_extracted_class() -> None:
    """Moving the adapter must not split the class identity seen by callers."""
    from manyselves.app import BackendAPIImpl as LegacyBackendAPIImpl
    from manyselves.application.backend_api import BackendAPIImpl

    assert LegacyBackendAPIImpl is BackendAPIImpl


@pytest.mark.asyncio
async def test_sync_agent_conversation_replaces_session_history_and_pending_queue() -> None:
    """Conversation restore keeps its session, message, and clear-pending contract."""
    from manyselves.application.backend_api import BackendAPIImpl

    loop = _FakeAgentLoop()
    api = BackendAPIImpl(config_manager=SimpleNamespace(), bus=SimpleNamespace())
    api.set_loop_manager(_FakeLoopManager(loop))

    await api.sync_agent_conversation(
        "main",
        messages=[
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer", "is_tool_result": True},
            {"role": "invalid", "content": "ignored"},
        ],
        session_id="session-42",
        clear_pending=True,
    )

    assert loop._current_session_id == "session-42"
    assert [
        (message.role, message.content, message.is_tool_result)
        for message in loop._conversation_history
    ] == [
        ("user", "question", False),
        ("assistant", "answer", True),
    ]
    assert loop.cancel_calls == 1
    assert loop._message_queue.empty()
    assert loop.queue_update_calls == 1


@pytest.mark.asyncio
async def test_sync_agent_conversation_preserves_pending_queue_when_not_cleared() -> None:
    """A normal restore must not cancel work or discard queued messages."""
    from manyselves.application.backend_api import BackendAPIImpl

    loop = _FakeAgentLoop()
    api = BackendAPIImpl(config_manager=SimpleNamespace(), bus=SimpleNamespace())
    api.set_loop_manager(_FakeLoopManager(loop))

    await api.sync_agent_conversation(
        "main",
        messages=None,
        session_id=None,
        clear_pending=False,
    )

    assert loop._current_session_id is None
    assert loop._conversation_history == []
    assert loop.cancel_calls == 0
    assert loop._message_queue.qsize() == 2
    assert loop.queue_update_calls == 0
