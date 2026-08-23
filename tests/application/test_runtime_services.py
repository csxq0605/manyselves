"""Characterization for the neutral account-scoped runtime composition view."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from manyselves.application.runtime_host import RuntimeHost
from manyselves.config import ConfigManager
from manyselves.config.schema import AgentDefaults, AppConfig


def _host(
    tmp_path: Path,
    *,
    main_loop: object | None = None,
    provider_manager: object | None = None,
    workspace: Path | None = None,
) -> RuntimeHost:
    config = ConfigManager(config_path=tmp_path / "manyselves.config.yaml")
    config._config = AppConfig(  # noqa: SLF001
        agents={"defaults": AgentDefaults(model="characterization-model")}
    )
    host = RuntimeHost.create(config)
    manager = SimpleNamespace(
        get_loop=lambda agent_id: main_loop if agent_id == "main" else None,
        _provider_manager=provider_manager,
    )
    host._loop_manager = manager  # noqa: SLF001
    host._workspace = workspace  # noqa: SLF001
    host._global_knowledge_root = tmp_path / "Knowledge"  # noqa: SLF001
    return host


def test_runtime_services_view_projects_existing_host_resources(tmp_path: Path) -> None:
    from manyselves.application.runtime_services import (
        build_runtime_services_view,
    )

    provider = object()
    loop = SimpleNamespace(llm_provider=provider)
    workspace = tmp_path / "project"
    view = build_runtime_services_view(
        _host(tmp_path, main_loop=loop, workspace=workspace)
    )

    assert view.workspace == workspace
    assert view.bus is not None
    assert view.active_provider is provider
    assert view.agent_defaults.model == "characterization-model"
    assert view.global_knowledge_root == tmp_path / "Knowledge"


def test_runtime_services_view_falls_back_to_existing_manager_selection(
    tmp_path: Path,
) -> None:
    from manyselves.application.runtime_services import (
        build_runtime_services_view,
    )

    provider = object()

    class ExistingProviderManager:
        def get_active_provider(self) -> object:
            return provider

    view = build_runtime_services_view(
        _host(
            tmp_path,
            provider_manager=ExistingProviderManager(),
            workspace=tmp_path / "project",
        )
    )

    assert view.active_provider is provider


@pytest.mark.parametrize("workspace", [None])
def test_runtime_services_view_preserves_degraded_host_resources(
    tmp_path: Path,
    workspace: Path | None,
) -> None:
    from manyselves.application.runtime_services import (
        build_runtime_services_view,
    )

    class NoProviderManager:
        def get_active_provider(self) -> object:
            raise ValueError("No active provider")

    view = build_runtime_services_view(
        _host(
            tmp_path,
            provider_manager=NoProviderManager(),
            workspace=workspace,
        )
    )

    assert view.workspace is None
    assert view.active_provider is None
    assert view.bus is not None
