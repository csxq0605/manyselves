"""Characterize the backend message boundary and project startup structure."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from manyselves.app import BackendAPIImpl
from manyselves.config import ConfigManager
from manyselves.core.project_structure import ensure_project_structure
from manyselves.interfaces.types import UserMessage
from manyselves.runtime.loops import MessageBus


async def _append_async(messages: list[UserMessage], message: UserMessage) -> None:
    messages.append(message)


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_backend_publishes_user_message_with_existing_mapping() -> None:
    bus = MessageBus()
    api = BackendAPIImpl(ConfigManager(), bus)
    seen: list[UserMessage] = []

    async def on_user_message(message: UserMessage) -> None:
        await _append_async(seen, message)

    bus.subscribe(UserMessage, on_user_message)
    processor = asyncio.create_task(bus.process_queue())

    await api.send_user_message("hello", "sub", message_id="m-1")
    await _wait_until(lambda: len(seen) == 1)
    bus.shutdown()
    await processor

    assert seen[0].agent_type == "main"
    assert seen[0].message_id == "m-1"


def test_project_startup_uses_canonical_structure(tmp_path: Path) -> None:
    ensure_project_structure(tmp_path)

    assert {"Inputs", "Knowledge", "Templates", "Work", "Outputs"} <= {
        child.name for child in tmp_path.iterdir()
    }


