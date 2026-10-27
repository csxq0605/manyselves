"""A model change replaces the loops; Main must continue the same conversation."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from manyselves.application.runtime_host import RuntimeHost
from manyselves.config.schema import ApiConfig, AppConfig
from manyselves.runtime.providers.base import Message


def _config_manager() -> MagicMock:
    manager = MagicMock()
    manager.config = AppConfig()
    manager.config.providers.configurations.append(
        ApiConfig(
            id="first",
            name="First",
            provider="anthropic",
            api_key="sk-test",
            enabled=True,
        )
    )
    manager.validate_api_keys.return_value = (True, ["first"])
    return manager


async def _replace_and_read(tmp_path: Path, *, restart_in_place: bool = False):
    host = RuntimeHost.create(_config_manager())
    with patch("manyselves.runtime.loops.manager.ProviderFactory") as factory:
        factory.create_provider.return_value = AsyncMock()
        await host.start(tmp_path / "workspace")
        try:
            main = host.loop_manager.get_loop("main")
            main._conversation_history.extend(  # noqa: SLF001
                [
                    Message(role="user", content="先看 2.1 小节"),
                    Message(role="assistant", content="2.1 小节有三条记录"),
                ]
            )
            main._current_session_id = "session-1"  # noqa: SLF001
            main.begin_typed_task({"task_id": "task-1", "kind": "chat"})

            if restart_in_place:
                await host.loop_manager.restart("config_change")
            else:
                await host.replace_loop_manager()

            replacement = host.loop_manager.get_loop("main")
            assert replacement is not main
            built = await replacement._build_messages()  # noqa: SLF001
            return replacement, built
        finally:
            await host.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart_in_place", [False, True])
async def test_idle_model_change_keeps_main_history_and_session(
    tmp_path: Path, restart_in_place: bool
) -> None:
    replacement, built = await _replace_and_read(
        tmp_path, restart_in_place=restart_in_place
    )

    history = replacement.conversation_messages()
    assert [m.content for m in history] == ["先看 2.1 小节", "2.1 小节有三条记录"]
    assert replacement._current_session_id == "session-1"  # noqa: SLF001
    assert replacement.active_task_identity == {"task_id": "task-1", "kind": "chat"}
    assert len(replacement.task_boundaries) == 1
    contents = [m.content for m in built]
    assert "先看 2.1 小节" in contents
    assert "2.1 小节有三条记录" in contents


@pytest.mark.asyncio
async def test_failed_replacement_still_carries_history_into_recovery(
    tmp_path: Path,
) -> None:
    host = RuntimeHost.create(_config_manager())
    with patch("manyselves.runtime.loops.manager.ProviderFactory") as factory:
        factory.create_provider.return_value = AsyncMock()
        await host.start(tmp_path / "workspace")
        try:
            main = host.loop_manager.get_loop("main")
            main._conversation_history.append(Message(role="user", content="一"))  # noqa: SLF001
            main._current_session_id = "session-2"  # noqa: SLF001

            from manyselves.runtime.loops import manager as manager_module

            original_start = manager_module.LoopManager.start
            calls = {"n": 0}

            async def failing_once(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("provider rejected")
                await original_start(self)

            with patch.object(manager_module.LoopManager, "start", failing_once):
                with pytest.raises(RuntimeError, match="provider rejected"):
                    await host.replace_loop_manager()
                await host.replace_loop_manager(recovery=True)

            recovered = host.loop_manager.get_loop("main")
            assert [m.content for m in recovered.conversation_messages()] == ["一"]
            assert recovered._current_session_id == "session-2"  # noqa: SLF001
        finally:
            await host.stop()
