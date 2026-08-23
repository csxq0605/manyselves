"""Characterization for the Capability-owned initial Cross Provider path."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from manyselves.application.runtime_services import RuntimeServicesView
from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
    CrossOwnerRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleSubmission,
)
from manyselves.config.schema import AgentDefaults
from manyselves.core.loops.bus import MessageBus
from manyselves.interfaces.types import UserMessage
from manyselves.kernel.conversations import (
    ConversationKey,
    ConversationMode,
    ConversationRecord,
)
from manyselves.kernel.definitions import AgentDefinition, TaskDefinition
from manyselves.runtime.agent_execution import AgentExecutionService


def _state(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "module_submissions": {
            module_id: ModuleSubmission(
                module_id=module_id,
                submodule_narratives={
                    submodule_id: f"内容 {submodule_id}"
                    for submodule_id in REPORT_TAXONOMY[module_id].submodules
                },
                claims=[],
                source_ids=[],
                unresolved_questions=[],
                revision=0,
            )
            for module_id in REPORT_TAXONOMY
        },
        "module_artifact_refs": {
            module_id: {
                "ref": f"Work/runs/{run_id}/modules/{module_id}-r0.json",
                "sha256": "0" * 64,
            }
            for module_id in REPORT_TAXONOMY
        },
    }


@pytest.mark.asyncio
async def test_cross_provider_uses_real_submit_tool_wire_identity_and_one_session(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.cross_provider import (
        build_cross_provider_composition,
    )

    run_id = "cross-provider-run"
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    built: list[dict[str, object]] = []
    loops: list[object] = []

    class Loop:
        def __init__(self, kwargs: dict[str, object]) -> None:
            self.kwargs = kwargs
            self.received: list[UserMessage] = []
            self.callback = None

        def restore_conversation(
            self,
            messages,
            *,
            task_boundaries=(),
            handoff_summary=None,
        ) -> None:
            del messages, task_boundaries, handoff_summary

        async def start(self) -> None:
            async def respond(message: UserMessage) -> None:
                if message.agent_type != self.kwargs["agent_type"]:
                    return
                self.received.append(message)
                await self.kwargs["tools"].get("submit_result")(
                    kind="cross_owner_finding_submission",
                    owner_module_id="2.1",
                    coverage={
                        "module_id": "2.1",
                        "checked_dimensions": [
                            "terminology",
                            "facts",
                            "risk_levels",
                            "dependencies",
                            "propagation",
                            "joint_verification",
                        ],
                    },
                    findings=[],
                    synthesis_inputs=[],
                )

            self.callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self.callback is not None:
                bus.unsubscribe(UserMessage, self.callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    def loop_builder(**kwargs):
        built.append(kwargs)
        loop = Loop(kwargs)
        loops.append(loop)
        return loop

    services = RuntimeServicesView(
        workspace=tmp_path,
        bus=bus,
        active_provider=object(),
        agent_defaults=AgentDefaults(),
        global_knowledge_root=None,
    )
    runtime = CrossOwnerRuntime(tmp_path)
    state = runtime.prepare(_state(run_id))
    composition = build_cross_provider_composition(
        services,
        cross_runtime=runtime,
        execution=AgentExecutionService(bus, timeout=1),
        loop_builder=loop_builder,
    )
    context = runtime.prepare_initial(
        {"state": state, "owner_module_id": "2.1"}
    )
    conversation = ConversationRecord(
        conversation_id="cross-provider-conversation",
        key=ConversationKey(
            agent_id="cross-module-reviewer",
            value="cross-owner-2.1",
            mode=ConversationMode.RUN,
        ),
        run_id=run_id,
    )
    agent = AgentDefinition(
        id="cross-module-reviewer",
        version="1.0.0",
        description="Cross owner reviewer",
        instructions="审查当前 owner 与其他模块的关系。",
        tools=["submit_result"],
    )
    task = TaskDefinition(
        id="cross-owner-runtime-initial-review",
        version="1.0.0",
        description="Cross owner initial",
        agent=agent.id,
        objective="review cross-module interfaces",
        input_contract="cross_owner_input",
        output_contract="declarative_cross_owner_initial_agent_result",
        tools=["submit_result"],
    )
    try:
        first = await composition.agent_invokers[agent.id].invoke(
            agent,
            task,
            context.model_dump(mode="json"),
            conversation,
            task_id="invoke-cross-owner-2.1",
        )
        second = await composition.agent_invokers[agent.id].invoke(
            agent,
            task,
            context.model_dump(mode="json"),
            conversation,
            task_id="invoke-cross-owner-2.1-again",
        )
    finally:
        await composition.close()
        bus.shutdown()
        await bus_task

    assert first.status == "ok"
    assert second.status == "ok"
    assert len(loops) == 1
    assert len(built) == 1
    assert [message.task_id for message in loops[0].received] == [
        "invoke-cross-owner-2.1",
        "invoke-cross-owner-2.1-again",
    ]
    assert [message.task_attempt_id for message in loops[0].received] == [
        "invoke-cross-owner-2.1",
        "invoke-cross-owner-2.1-again",
    ]
    assert [message.session_id for message in loops[0].received] == [
        conversation.external_session_id,
        conversation.external_session_id,
    ]
    assert conversation.external_session_id == "public-reporting:cross-owner-2.1"
    assert set(built[0]["tools"].get_all()) == {"submit_result"}

    result_path = (
        tmp_path
        / "Work/runs"
        / run_id
        / "results"
        / "cross-owner-2.1-r0-initial.json"
    )
    persisted = json.loads(result_path.read_text(encoding="utf-8"))
    assert persisted["agent_id"] == "cross-module-reviewer"
    assert persisted["task_id"] == "cross-owner-2.1-r0-initial"
    assert persisted["session_id"] == conversation.external_session_id
    assert persisted["payload"]["kind"] == "cross_owner_finding_submission"


def test_cross_provider_does_not_load_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from manyselves.capabilities.distribution_reporting.runtime.cross_provider "
                "import build_cross_provider_composition\n"
                "print(sorted(name for name in sys.modules if name.startswith('manyselves.core.reporting')))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "[]"
