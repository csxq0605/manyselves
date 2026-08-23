"""Characterization for the aggregate-editor Provider composition."""

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
    EditedReportSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    AggregateEditorInput,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.config.schema import AgentDefaults
from manyselves.core.loops.bus import MessageBus
from manyselves.interfaces.types import AgentResultMessage, UserMessage
from manyselves.kernel.conversations import (
    ConversationKey,
    ConversationMode,
    ConversationRecord,
)
from manyselves.kernel.definitions import AgentDefinition, TaskDefinition
from manyselves.runtime.agent_execution import AgentExecutionService


def _aggregate_input(run_id: str) -> AggregateEditorInput:
    return AggregateEditorInput(
        run_id=run_id,
        source_format="markdown",
        approved_module_markers={
            module_id: f"[[APPROVED_MODULE:{module_id}]]"
            for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5")
        },
        markdown_modules={
            module_id: f"模块 {module_id} 的既有正文。"
            for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5")
        },
    )


def _edited_submission() -> EditedReportSubmission:
    return EditedReportSubmission(
        title="汇总报告",
        assessment_background="背景正文。",
        findings_overview="发现正文。",
        regional_executive_summary="区域摘要正文。",
        module_narratives={
            module_id: f"[[APPROVED_MODULE:{module_id}]] 模块 {module_id} 的汇总正文。"
            for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5")
        },
        risk_panorama="风险全景正文。",
        dimension_risk_analysis="维度风险正文。",
        data_gap_analysis="数据缺口正文。",
        improvement_action_plan="改进行动正文。",
    )


@pytest.mark.asyncio
async def test_aggregate_provider_uses_real_submit_tool_wire_and_one_session(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.aggregate_provider import (
        build_aggregate_provider_composition,
    )

    run_id = "aggregate-provider-run"
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    built: list[dict[str, object]] = []
    loops: list[object] = []
    terminals: list[AgentResultMessage] = []

    async def capture_terminal(message: AgentResultMessage) -> None:
        terminals.append(message)

    bus.subscribe(AgentResultMessage, capture_terminal)

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
                payload = _edited_submission().model_dump(
                    mode="json",
                    exclude={"special_topic_plan", "protected_claim_ids"},
                )
                await self.kwargs["tools"].get("submit_result")(**payload)

            self.callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self.callback is not None:
                bus.unsubscribe(UserMessage, self.callback)

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
    execution = AgentExecutionService(bus, timeout=1)
    composition = build_aggregate_provider_composition(
        services,
        execution=execution,
        loop_builder=loop_builder,
    )
    conversation = ConversationRecord(
        conversation_id="aggregate-provider-conversation",
        key=ConversationKey(
            agent_id="aggregate-editor",
            value="aggregate-existing",
            mode=ConversationMode.RUN,
        ),
        run_id=run_id,
    )
    agent = AgentDefinition(
        id="aggregate-editor",
        version="1.0.0",
        description="Aggregate editor",
        instructions="整合五个既有模块并提交完整汇总结果。",
        tools=["write_result_part", "list_result_parts", "submit_result"],
    )
    task = TaskDefinition(
        id="aggregate-existing",
        version="1.0.0",
        description="Aggregate existing modules",
        agent=agent.id,
        objective="整合五个既有模块。",
        input_contract="aggregate_editor_input",
        output_contract="edited_report_submission",
        tools=["write_result_part", "list_result_parts", "submit_result"],
    )
    value = _aggregate_input(run_id)
    ReportingStore(tmp_path).write_json(
        f"Work/runs/{run_id}/context/aggregate-editor-input.json",
        value.model_dump(mode="json"),
    )

    try:
        first = await composition.agent_invokers[agent.id].invoke(
            agent,
            task,
            value,
            conversation,
            task_id="host-action-aggregate-first",
        )
        second = await composition.agent_invokers[agent.id].invoke(
            agent,
            task,
            value,
            conversation,
            task_id="host-action-aggregate-second",
        )
        await asyncio.sleep(0)
    finally:
        await composition.close()
        bus.shutdown()
        await bus_task

    assert first.status == "ok"
    assert second.status == "ok"
    assert isinstance(
        EditedReportSubmission.model_validate(first.result), EditedReportSubmission
    )
    assert len(loops) == 1
    assert len(built) == 1
    assert set(loops[0].kwargs["tools"].get_all()) == {
        "write_result_part",
        "list_result_parts",
        "submit_result",
    }
    assert loops[0].kwargs["llm_provider"] is services.active_provider
    assert [message.task_id for message in loops[0].received] == [
        "host-action-aggregate-first",
        "host-action-aggregate-second",
    ]
    assert [message.session_id for message in loops[0].received] == [
        conversation.external_session_id,
        conversation.external_session_id,
    ]
    assert conversation.external_session_id == "aggregate-existing"
    assert len(terminals) == 2
    assert all(message.sender == "aggregate-editor" for message in terminals)
    assert all(message.task_id == "aggregate-existing" for message in terminals)
    assert all(message.task_attempt_id == "" for message in terminals)
    assert all(
        message.workflow_id == "distribution-aggregate-existing"
        for message in terminals
    )
    loaded = load_agent_result_payload(
        tmp_path,
        "Work/runs/aggregate-provider-run/results/aggregate-existing.json",
    )
    assert isinstance(loaded.payload, EditedReportSubmission)
    assert loaded.identity.task_id == "aggregate-existing"
    assert loaded.identity.agent_id == "aggregate-editor"
    assert loaded.identity.session_id == conversation.external_session_id


def test_aggregate_provider_does_not_load_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from manyselves.capabilities.distribution_reporting.runtime.aggregate_provider "
                "import build_aggregate_provider_composition\n"
                "print(sorted(name for name in sys.modules "
                "if name.startswith('manyselves.core.reporting')))\n"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "[]"
