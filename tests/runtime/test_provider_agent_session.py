from dataclasses import dataclass
from pathlib import Path
from typing import Any

from manyselves.runtime.provider_agent_session import ProviderAgentSessionFactory


@dataclass
class ScriptedSessionLoop:
    persist_handoff_summary: bool = True


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
