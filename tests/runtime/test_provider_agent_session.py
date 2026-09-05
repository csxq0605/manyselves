import json
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
    initial_session_restore: Any = None


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


def test_provider_session_factory_loads_existing_bounded_handoff(
    tmp_path: Path,
) -> None:
    run_id = "run-restart"
    runtime_id = "public-reporting:module-2.1-specialist:specialist-2.1"
    safe_agent = runtime_id.replace(":", "_")
    handoff_path = (
        tmp_path
        / "Work"
        / "runs"
        / run_id
        / "agent-conversations"
        / f"{safe_agent}.handoff.json"
    )
    handoff_path.parent.mkdir(parents=True)
    handoff_path.write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": run_id,
                "agent_type": runtime_id,
                "summary": {
                    "progress": ["inspected Inputs/source.xlsx"],
                    "decisions": [],
                    "constraints": ["cite project evidence"],
                    "remaining_work": ["finish module 2.1"],
                    "critical_refs": ["E-0001"],
                    "sequence": 3,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    loop = ScriptedSessionLoop()
    factory = ProviderAgentSessionFactory(
        loop_builder=lambda **_kwargs: loop,
        loop_kwargs={
            "agent_type": runtime_id,
            "workspace": tmp_path,
            "usage_run_id": run_id,
        },
        persist_handoff_summary=True,
    )

    created = factory()

    assert created.initial_session_restore is not None
    assert created.initial_session_restore.messages == ()
    assert created.initial_session_restore.handoff_summary == {
        "progress": ["inspected Inputs/source.xlsx"],
        "decisions": [],
        "constraints": ["cite project evidence"],
        "remaining_work": ["finish module 2.1"],
        "critical_refs": ["E-0001"],
        "sequence": 3,
    }


def test_all_reporting_provider_compositions_enable_bounded_handoff() -> None:
    runtime = (
        Path(__file__).parents[2]
        / "manyselves"
        / "capabilities"
        / "distribution_reporting"
        / "runtime"
    )
    providers = (
        "aggregate_provider.py",
        "chief_provider.py",
        "cross_provider.py",
        "final_chief_provider.py",
        "final_provider.py",
        "module_provider.py",
        "template_provider.py",
    )

    for filename in providers:
        source = (runtime / filename).read_text(encoding="utf-8")
        assert "persist_handoff_summary=True" in source
        assert "persist_handoff_summary=False" not in source
