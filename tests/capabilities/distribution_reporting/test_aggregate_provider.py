"""Characterization for the aggregate-editor Provider composition."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.agent_result_payload import (
    load_agent_result_payload,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    AgentResult,
    AgentRunStatus,
    EditedReportSubmission,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    AggregateEditorInput,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.config.schema import AgentDefaults
from manyselves.interfaces.types import AgentResultMessage, UserMessage
from manyselves.kernel.conversations import (
    ConversationKey,
    ConversationMode,
    ConversationRecord,
)
from manyselves.kernel.definitions import (
    AgentDefinition,
    RecoveryPolicyDefinition,
    RecoveryRule,
    TaskDefinition,
)
from manyselves.runtime.agent_execution import AgentExecutionService
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.services import RuntimeServicesView


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
async def test_aggregate_provider_shares_declared_tool_and_agent_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime import (
        aggregate_agent_bridge,
        aggregate_provider,
    )
    from manyselves.runtime.tools.registry import ToolRegistry

    captured: dict[str, object] = {}

    def capture_tools(*args, **kwargs):
        del args
        captured["dependencies"] = kwargs["dependencies"]
        return ToolRegistry()

    async def capture_recovery(*args, **kwargs):
        del args
        captured["recovery"] = kwargs["recovery"]
        return _edited_submission().model_dump(mode="json")

    class Loop:
        def restore_conversation(self, messages, *, task_boundaries=(), handoff_summary=None):
            del messages, task_boundaries, handoff_summary

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def wait_until_turn_complete(self) -> None:
            return None

    monkeypatch.setattr(aggregate_provider, "build_module_provider_tools", capture_tools)
    monkeypatch.setattr(
        aggregate_agent_bridge,
        "execute_reporting_recovery",
        capture_recovery,
        raising=False,
    )
    bus = MessageBus()
    runtime = aggregate_provider.AggregateProviderRuntime(
        RuntimeServicesView(
            workspace=tmp_path,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        loop_builder=lambda **kwargs: Loop(),
    )
    value = _aggregate_input("aggregate-schema-run")
    ReportingStore(tmp_path).write_json(
        "Work/runs/aggregate-schema-run/context/aggregate-editor-input.json",
        value.model_dump(mode="json"),
    )
    agent = AgentDefinition(
        id="aggregate-editor",
        version="1.0.0",
        description="Aggregate editor",
        instructions="Aggregate the approved modules.",
        tools=["write_result_part", "list_result_parts", "submit_result"],
    )
    task = TaskDefinition(
        id="aggregate-existing",
        version="1.0.0",
        description="Aggregate existing modules",
        agent=agent.id,
        objective="Aggregate approved modules.",
        input_contract="aggregate_editor_input",
        output_contract="edited_report_submission",
        tools=["write_result_part", "list_result_parts", "submit_result"],
    )
    policy = RecoveryPolicyDefinition(
        id="aggregate-schema-recovery",
        version="1.0.0",
        description="correct invalid structured output",
        rules={
            "invalid_structured_output": RecoveryRule(action="correct"),
        },
    )
    conversation = ConversationRecord(
        conversation_id="aggregate-schema-conversation",
        key=ConversationKey(
            agent_id=agent.id,
            value="aggregate-existing",
            mode=ConversationMode.RUN,
        ),
        run_id=value.run_id,
    )

    bridge = runtime._bridge(
        agent,
        task,
        value,
        conversation,
        recovery_policy=policy,
    )
    dependencies = captured["dependencies"]
    decision = await dependencies.recovery_event_callback(
        "invalid_structured_output",
        {"task_id": task.id},
    )
    try:
        outcome = await bridge.invoke_with_recovery(
            agent,
            task,
            value,
            conversation,
            task_id="invoke-aggregate-existing",
            recovery_policy=policy,
        )
    finally:
        await runtime.close()

    assert outcome.status == "ok"
    assert decision.action.value == "correct"
    assert captured["recovery"] is bridge.recovery_driver
    assert bridge.recovery_driver.snapshot_attempts() == {
        "invalid_structured_output": 1,
    }
    assert bridge.progress_observer is not None


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
        "open_tool_result",
    }
    assert loops[0].kwargs["llm_provider"] is services.active_provider
    assert [message.task_id for message in loops[0].received] == [
        "host-action-aggregate-first",
    ]
    assert [message.session_id for message in loops[0].received] == [
        conversation.external_session_id,
    ]
    assert conversation.external_session_id == "aggregate-existing"
    assert len(terminals) == 1
    assert all(message.sender == "aggregate-editor" for message in terminals)
    assert all(message.task_id == "aggregate-existing" for message in terminals)
    assert all(message.task_attempt_id != "" for message in terminals)
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


@pytest.mark.asyncio
async def test_aggregate_provider_reuses_persisted_completed_result_before_provider(
    tmp_path: Path,
) -> None:
    """The account Provider must load a verified aggregate result before a session."""

    from manyselves.capabilities.distribution_reporting.runtime.aggregate_provider import (
        AggregateProviderRuntime,
    )
    from manyselves.capabilities.distribution_reporting.runtime.completed_result_recovery import (
        build_task_correlation,
    )
    from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
        IdentityLeaseManager,
        TaskAttemptStore,
    )

    run_id = "aggregate-provider-persisted-reuse"
    value = _aggregate_input(run_id)
    input_ref = f"Work/runs/{run_id}/context/aggregate-editor-input.json"
    ReportingStore(tmp_path).write_json(input_ref, value.model_dump(mode="json"))
    agent = AgentDefinition(
        id="aggregate-editor",
        version="1.0.0",
        description="Aggregate editor",
        instructions="Aggregate the approved modules.",
        tools=["write_result_part", "list_result_parts", "submit_result"],
    )
    task = TaskDefinition(
        id="aggregate-existing",
        version="1.0.0",
        description="Aggregate existing modules",
        agent=agent.id,
        objective="Aggregate approved modules.",
        input_contract="aggregate_editor_input",
        output_contract="edited_report_submission",
        tools=list(agent.tools),
    )
    envelope = TaskEnvelope(
        task_id=task.id,
        run_id=run_id,
        agent_id=agent.id,
        objective=task.objective,
        input_refs=[input_ref],
        allowed_outputs=[task.output_contract],
        allowed_tools=list(task.tools),
        input_contract_kind=task.input_contract,
        input_contract_ref=input_ref,
    )
    workflow_id = "distribution-aggregate-existing"
    identity_key = agent.id
    session_id = "aggregate-existing"
    lease_handle = IdentityLeaseManager(tmp_path, run_id).acquire(
        workflow_id,
        identity_key,
    )
    correlation = build_task_correlation(
        tmp_path,
        envelope,
        workflow_id=workflow_id,
        identity_key=identity_key,
        session_id=session_id,
        identity_lease=lease_handle.lease,
    )
    submission = _edited_submission()
    try:
        store = TaskAttemptStore(tmp_path, run_id)
        store.activate(correlation)
        store.persist_result(
            correlation,
            AgentResult(
                task_id=envelope.task_id,
                run_id=run_id,
                agent_id=agent.id,
                session_id=session_id,
                status=AgentRunStatus.COMPLETED,
                payload=submission,
            ).model_dump(mode="json"),
            status="completed",
        )
    finally:
        lease_handle.release()

    provider_calls = 0

    def forbidden_loop_builder(**_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("completed aggregate result must be reused before Provider session")

    bus = MessageBus()
    service = AgentExecutionService(bus)
    runtime = AggregateProviderRuntime(
        RuntimeServicesView(
            workspace=tmp_path,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        execution=service,
        loop_builder=forbidden_loop_builder,
    )
    conversation = ConversationRecord(
        conversation_id="aggregate-provider-persisted-conversation",
        key=ConversationKey(
            agent_id=agent.id,
            value=session_id,
            mode=ConversationMode.RUN,
        ),
        run_id=run_id,
    )
    outcome = await runtime.invoke(
        agent,
        task,
        value,
        conversation,
        task_id="host-aggregate-provider-persisted",
    )

    assert outcome.status == "ok", outcome.error
    assert provider_calls == 0
    assert service.sessions == {}
    assert outcome.session_id == session_id
