"""Tests for loop manager (agent lifecycle coordination)."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from manyselves.config.schema import ApiConfig, AppConfig
from manyselves.core.loops.bus import MessageBus
from manyselves.core.loops.manager import LoopManager
from manyselves.interfaces.types import AgentType


@pytest.fixture
def workspace():
    import shutil
    ws = Path(tempfile.mkdtemp()).resolve()
    for d in ["Data", "Data/Processed", "Plots", "Plots/Fig", "Plots/Scripts", "Theory", "Tex", "References"]:
        (ws / d).mkdir(parents=True, exist_ok=True)
    yield ws
    shutil.rmtree(ws, ignore_errors=True)


@pytest.fixture
def config_manager():
    cm = MagicMock()
    cm.config = AppConfig()
    cm.config.providers.configurations.append(
        ApiConfig(
            id="test-provider",
            name="Test",
            provider="anthropic",
            api_key="sk-test-key",
            enabled=True,
        )
    )
    return cm


@pytest.fixture
def gui():
    return AsyncMock()


@pytest.fixture
def manager(workspace, config_manager, gui):
    bus = MessageBus()
    manager = LoopManager(
        workspace=workspace,
        config_manager=config_manager,
        bus=bus,
    )
    manager._provider_manager.register_provider("test-provider", AsyncMock())
    return manager


def test_init(manager):
    assert not manager.is_running


def test_manager_loop_lookup_uses_string_id(manager):
    manager._loops["module-2.4-specialist"] = object()

    assert manager.get_loop("module-2.4-specialist") is manager._loops[
        "module-2.4-specialist"
    ]


def test_subscribes_to_restart(manager):
    assert manager.is_running is False
    # Verify bus subscription
    from manyselves.interfaces.types import RestartRequest
    assert RestartRequest in manager.bus._subscribers


def test_create_tools_for_main(manager):
    tools = manager._create_tools_for_agent(AgentType.MAIN)
    tool_names = {t.name for t in tools.get_all().values()}
    assert "read" in tool_names
    assert "apply_patch" in tool_names
    # MAIN delegates — it does not get the exec/shell tool.
    assert "exec" not in tool_names
    assert "send_to_agent" not in tool_names
    assert "run_reporting_workflow" in tool_names
    assert "resume_reporting_workflow" in tool_names
    assert "revise_reporting_workflow" in tool_names
    assert "project_skill_evolution" in tool_names
    assert "run_product_skill_maintainer" in tool_names
    assert "product_skill_evolution" not in tool_names
    assert "respond" not in tool_names


@pytest.mark.parametrize("legacy_type", [AgentType.DATA_ANALYSIS, AgentType.THEORY, AgentType.PLOTTING])
def test_legacy_agent_ids_do_not_receive_runtime_orchestration_tools(manager, legacy_type):
    tools = manager._create_tools_for_agent(legacy_type)
    tool_names = {t.name for t in tools.get_all().values()}
    assert "exec" not in tool_names
    assert "respond" not in tool_names
    assert "send_to_agent" not in tool_names
    assert "run_reporting_workflow" not in tool_names


def test_create_tools_write_dirs_main(manager, workspace):
    tools = manager._create_tools_for_agent(AgentType.MAIN)
    write_tool = tools.get("apply_patch")
    assert write_tool is not None


@pytest.mark.asyncio
@patch("manyselves.core.loops.manager.ProviderFactory")
async def test_start_creates_loops(mock_factory, manager):
    mock_provider = AsyncMock()
    mock_factory.create_provider.return_value = mock_provider

    await manager.start()
    assert manager.is_running
    assert set(manager._loops) == {"main"}
    assert all(
        legacy not in manager._loops
        for legacy in ("data_analysis", "plotting", "theory", "report")
    )

    await manager.stop()


@pytest.mark.asyncio
@patch("manyselves.core.loops.manager.ProviderFactory")
async def test_stop_clears_loops(mock_factory, manager):
    mock_provider = AsyncMock()
    mock_factory.create_provider.return_value = mock_provider

    await manager.start()
    assert len(manager._loops) > 0

    await manager.stop()
    assert not manager.is_running
    assert len(manager._loops) == 0


@pytest.mark.asyncio
async def test_start_idempotent(manager):
    manager._running = True
    await manager.start()
    assert manager._running is True


@pytest.mark.asyncio
async def test_stop_idempotent(manager):
    await manager.stop()
    assert not manager.is_running


@pytest.mark.asyncio
async def test_create_checkpoint(manager, gui):
    checkpoint_id = await manager.create_checkpoint("main", "Test checkpoint")
    assert checkpoint_id.startswith("cp_main_")


@pytest.mark.asyncio
async def test_set_agent_debug_mode(manager):
    # Manually add a mock loop
    mock_loop = MagicMock()
    manager._loops[AgentType.DATA_ANALYSIS] = mock_loop

    manager.set_agent_debug_mode("data_analysis", True)
    mock_loop.set_debug_mode.assert_called_once_with(True)


def test_get_agent_debug_mode_not_found(manager):
    assert manager.get_agent_debug_mode("data_analysis") is False


def test_get_agent_debug_mode(manager):
    mock_loop = MagicMock()
    mock_loop.debug_mode = True
    manager._loops[AgentType.DATA_ANALYSIS] = mock_loop

    assert manager.get_agent_debug_mode("data_analysis") is True


def test_cancel_main_cascades_to_active_delegated_subagent(manager):
    main_loop = MagicMock()
    plotting_loop = MagicMock()
    manager._loops[AgentType.MAIN] = main_loop
    manager._loops[AgentType.PLOTTING] = plotting_loop
    manager._task_board.create_task(
        source=AgentType.MAIN,
        target=AgentType.PLOTTING,
        brief="Generate figures",
        blocking=True,
    )

    manager.cancel_current_operation("main")

    main_loop.cancel_current.assert_called_once()
    plotting_loop.cancel_current.assert_called_once()
