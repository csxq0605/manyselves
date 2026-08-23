"""Characterization for the Capability-owned Final Auditor Provider path."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from manyselves.application.runtime_services import RuntimeServicesView
from manyselves.capabilities.distribution_reporting.runtime.agent_result_payload import (
    load_agent_result_payload,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    FinalChapterLaneFindingSubmission,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.final_chapter import (
    DeclarativeFinalChapterContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    FinalChapterLaneInput,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
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


def _context(tmp_path: Path, run_id: str) -> DeclarativeFinalChapterContext:
    contract = FinalChapterLaneInput(
        phase="initial",
        run_id=run_id,
        subject_ref=f"Work/runs/{run_id}/edited-revisions/chief-r0.json",
        chapter_id="1",
        review_focus=["检查章节事实、风险和验证闭环。"],
        section_ids=["1.1", "1.2", "1.3"],
        section_bodies={
            "1.1": "背景正文。",
            "1.2": "发现正文。",
            "1.3": "区域摘要正文。",
        },
    )
    input_ref = f"Work/runs/{run_id}/context/final-chapter-1-input-r0.json"
    ReportingStore(tmp_path).write_json(input_ref, contract.model_dump(mode="json"))
    envelope = TaskEnvelope(
        task_id="final-chapter-1-r0",
        run_id=run_id,
        agent_id="chief-editor-auditor",
        objective="审查 Chapter 1 的指定小节并提交 lane finding。",
        input_refs=[input_ref],
        constraints=["只审查 Chapter 1 的三个指定小节。"],
        allowed_outputs=["final_chapter_lane_finding_submission"],
        allowed_tools=["submit_result"],
        input_contract_kind="final_chapter_lane_input",
        input_contract_ref=input_ref,
        artifact_delivery_modes={input_ref: "inline"},
        inline_context="Final Auditor skill: keep findings inside the assigned lane.",
    )
    return DeclarativeFinalChapterContext(
        chapter_id="1",
        status="ready",
        contract=contract,
        input_ref=input_ref,
        envelope=envelope,
    )


@pytest.mark.asyncio
async def test_final_provider_uses_real_submit_tool_and_reuses_one_session(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.final_provider import (
        build_final_provider_composition,
    )

    run_id = "final-provider-run"
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    built: list[dict[str, object]] = []
    loops: list[object] = []

    class Loop:
        def __init__(self, kwargs: dict[str, object]) -> None:
            self.kwargs = kwargs
            self.received: list[UserMessage] = []
            self._callback = None

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
                submit_result = self.kwargs["tools"].get("submit_result")
                await submit_result(
                    kind="final_chapter_lane_finding_submission",
                    run_id=run_id,
                    chapter_id="1",
                    checked_section_ids=["1.1", "1.2", "1.3"],
                    findings=[],
                    residual_risks=[],
                )

            self._callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self._callback is not None:
                bus.unsubscribe(UserMessage, self._callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    def loop_builder(**kwargs: object) -> Loop:
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
    composition = build_final_provider_composition(
        services,
        execution=AgentExecutionService(bus, timeout=1),
        loop_builder=loop_builder,
    )
    context = _context(tmp_path, run_id)
    conversation = ConversationRecord(
        conversation_id="final-provider-conversation",
        key=ConversationKey(
            agent_id="chief-editor-auditor",
            value="final-chapter-1",
            mode=ConversationMode.RUN,
        ),
        run_id=run_id,
    )
    agent = AgentDefinition(
        id="chief-editor-auditor",
        version="1.0.0",
        description="Final Auditor",
        instructions="审查指定章节并提交 typed finding。",
        tools=["submit_result"],
    )
    task = TaskDefinition(
        id="final-chapter-review",
        version="1.0.0",
        description="Final initial lane",
        agent=agent.id,
        objective="Review the assigned final chapter.",
        input_contract="declarative_final_chapter_context",
        output_contract="declarative_final_chapter_agent_result",
        tools=["submit_result"],
    )
    try:
        first = await composition.agent_invokers[agent.id].invoke(
            agent,
            task,
            context.model_dump(mode="json"),
            conversation,
            task_id="invoke-final-chapter-auditor",
        )
        second = await composition.agent_invokers[agent.id].invoke(
            agent,
            task,
            context.model_dump(mode="json"),
            conversation,
            task_id="invoke-final-chapter-auditor-again",
        )
    finally:
        await composition.close()
        bus.shutdown()
        await bus_task

    assert first.status == "ok"
    assert second.status == "ok"
    assert len(loops) == 1
    assert len(built) == 1
    assert built[0]["llm_provider"] is services.active_provider
    assert set(loops[0].kwargs["tools"].get_all()) == {"submit_result"}
    assert [message.session_id for message in loops[0].received] == [
        conversation.external_session_id,
        conversation.external_session_id,
    ]
    assert conversation.external_session_id == "final-chapter-1"
    result_path = tmp_path / "Work/runs/final-provider-run/results/final-chapter-1-r0.json"
    loaded = load_agent_result_payload(tmp_path, result_path)
    assert isinstance(loaded.payload, FinalChapterLaneFindingSubmission)
    assert loaded.identity.task_id == "final-chapter-1-r0"
    assert loaded.identity.agent_id == "chief-editor-auditor"
    assert loaded.identity.session_id == conversation.external_session_id


def test_final_provider_does_not_load_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from manyselves.capabilities.distribution_reporting.runtime.final_provider "
                "import build_final_provider_composition\n"
                "print(sorted(name for name in sys.modules if name.startswith('manyselves.core.reporting')))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "[]"
