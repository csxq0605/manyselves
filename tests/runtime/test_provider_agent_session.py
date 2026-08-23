from dataclasses import dataclass
from pathlib import Path
from typing import Any

from manyselves.runtime.provider_agent_session import ProviderAgentSessionFactory


@dataclass
class ScriptedSessionLoop:
    persist_handoff_summary: bool = True
    tools: Any = None
    config: Any = None
    llm_provider: Any = None
    artifact_gateway: Any = None
    usage_run_id: str | None = None
    usage_task_id: str | None = None
    _system_prompt_override: str | None = None


def test_provider_session_factory_passes_provider_loop_inputs_and_applies_runtime_option(
    tmp_path: Path,
) -> None:
    loop = ScriptedSessionLoop()
    captured: dict[str, Any] = {}

    def build_loop(**kwargs: Any) -> ScriptedSessionLoop:
        captured.update(kwargs)
        return loop

    loop_kwargs = {
        "agent_type": "runtime-agent",
        "workspace": tmp_path,
        "tools": object(),
        "bus": object(),
        "config": object(),
        "llm_provider": object(),
        "system_prompt": "typed prompt",
        "provider_attempt_observer": object(),
        "provider_recovery_decider": object(),
    }
    factory = ProviderAgentSessionFactory(
        loop_builder=build_loop,
        loop_kwargs=loop_kwargs,
        persist_handoff_summary=False,
    )

    created = factory()

    assert created is loop
    assert captured == loop_kwargs
    assert loop.persist_handoff_summary is False


def test_provider_session_factory_rebinds_one_existing_loop_for_next_typed_task(
    tmp_path: Path,
) -> None:
    loop = ScriptedSessionLoop(
        tools="old-tools",
        config="old-config",
        llm_provider="old-provider",
        artifact_gateway="old-gateway",
        usage_run_id="old-run",
        usage_task_id="old-task",
        _system_prompt_override="old prompt",
    )
    factory = ProviderAgentSessionFactory(
        loop_builder=lambda **_kwargs: loop,
        loop_kwargs={
            "agent_type": "runtime-agent",
            "workspace": tmp_path,
            "bus": object(),
            "tools": "next-tools",
            "config": "next-config",
            "llm_provider": "next-provider",
            "artifact_gateway": "next-gateway",
            "usage_run_id": "next-run",
            "usage_task_id": "next-task",
            "system_prompt": "next prompt",
        },
        persist_handoff_summary=False,
    )

    factory.reconfigure(loop)

    assert loop.tools == "next-tools"
    assert loop.config == "next-config"
    assert loop.llm_provider == "next-provider"
    assert loop.artifact_gateway == "next-gateway"
    assert loop.usage_run_id == "next-run"
    assert loop.usage_task_id == "next-task"
    assert loop._system_prompt_override == "next prompt"
    assert loop.persist_handoff_summary is False
