from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.core.loops.agent_loop import AgentLoop
from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider, LLMResponse, Message as LLMMessage
from manyselves.core.reporting.agent_runner import ReportingAgentRunner
from manyselves.core.reporting.agentic_models import TaskEnvelope
from manyselves.core.reporting.config import AgentDefinition
from manyselves.core.reporting.context_rebase import ReportingContextRebuilder
from manyselves.core.reporting.context_state import EvidenceSlice, TaskStateStore
from manyselves.core.tools.registry import ToolRegistry


class _NaturalCompletionProvider(LLMProvider):
    """Offline Provider fake that records the exact requests it receives."""

    def __init__(self) -> None:
        super().__init__("offline", model="context-rebase-fake")
        self.requests: list[list[tuple[str, str]]] = []

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        self.requests.append(
            [(str(message.role), str(message.content or "")) for message in messages]
        )
        return LLMResponse(content="本轮未提交 typed result。")


def _definition(tmp_path: Path) -> AgentDefinition:
    return AgentDefinition(
        name="shared-reporting-role",
        description="offline reporting role",
        tools=[],
        instructions="<role>只返回当前任务的简短结果。</role>",
        source_path=tmp_path / "shared-reporting-role.md",
    )


@pytest.mark.asyncio
async def test_runner_injects_typed_rebuilder_and_drops_cross_task_history(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    provider = _NaturalCompletionProvider()
    runner = ReportingAgentRunner(
        tmp_path,
        bus,
        provider,
        AgentDefaults(max_tool_iterations=1),
        timeout=5,
    )
    definition = _definition(tmp_path)
    first = TaskEnvelope(
        task_id="task-old",
        run_id="run-context-rebase",
        agent_id=definition.id,
        objective="OLD_OBJECTIVE_SHOULD_NOT_REAPPEAR",
    )
    second = TaskEnvelope(
        task_id="task-new",
        run_id="run-context-rebase",
        agent_id=definition.id,
        objective="NEW_OBJECTIVE_ONLY",
    )

    try:
        first_result = await runner.run(
            definition,
            first,
            [],
            workflow_id="wf-context-rebase",
        )
        second_result = await runner.run(
            definition,
            second,
            [],
            workflow_id="wf-context-rebase",
        )
        loop = runner._sessions[("wf-context-rebase", definition.id)][0]
    finally:
        await runner.close_workflow("wf-context-rebase")
        bus.shutdown()
        await bus_task

    assert first_result.status.value == "incomplete"
    assert second_result.status.value == "incomplete"
    assert len(provider.requests) == 2
    assert getattr(loop, "context_rebuilder", None) is not None

    old_payload = "\n".join(content for _role, content in provider.requests[0])
    new_payload = "\n".join(content for _role, content in provider.requests[1])
    assert "OLD_OBJECTIVE_SHOULD_NOT_REAPPEAR" in old_payload
    assert "NEW_OBJECTIVE_ONLY" in new_payload
    assert "OLD_OBJECTIVE_SHOULD_NOT_REAPPEAR" not in new_payload
    # A task transition carries a fresh typed capsule, not the old task's
    # transcript or a copied task_context block.
    assert "<typed_task_state>" in new_payload
    assert "<task_context>" in new_payload


@pytest.mark.asyncio
async def test_runner_resume_uses_verified_capsule_and_hash_drift_fails_closed(
    tmp_path: Path,
) -> None:
    definition = _definition(tmp_path)
    envelope = TaskEnvelope(
        task_id="task-resume",
        run_id="run-context-resume",
        agent_id=definition.id,
        objective="恢复并继续剩余工作",
    )

    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    first_provider = _NaturalCompletionProvider()
    first_runner = ReportingAgentRunner(
        tmp_path,
        bus,
        first_provider,
        AgentDefaults(max_tool_iterations=1),
        timeout=5,
    )
    try:
        first = await first_runner.run(
            definition,
            envelope,
            [],
            workflow_id="wf-context-resume",
        )
        assert first.status.value == "incomplete"
    finally:
        await first_runner.close_workflow("wf-context-resume")

    state_store = TaskStateStore(
        tmp_path,
        envelope.run_id,
        envelope.task_id,
        envelope.revision,
    )
    manifest = state_store.load()
    capsule = manifest.task_state.model_copy(
        update={"remaining_work": ["调用 submit_result 完成本任务"]}
    ).with_hash()
    state_store.save(
        manifest.model_copy(
            update={"task_state_capsule": capsule, "manifest_sha256": None}
        ).with_hash()
    )

    resumed_provider = _NaturalCompletionProvider()
    resumed_runner = ReportingAgentRunner(
        tmp_path,
        bus,
        resumed_provider,
        AgentDefaults(max_tool_iterations=1),
        timeout=5,
    )
    try:
        resumed = await resumed_runner.run(
            definition,
            envelope.model_copy(),
            [],
            workflow_id="wf-context-resume",
        )
    finally:
        await resumed_runner.close_workflow("wf-context-resume")

    assert resumed.status.value == "incomplete"
    resumed_payload = "\n".join(content for _role, content in resumed_provider.requests[0])
    assert "调用 submit_result 完成本任务" in resumed_payload
    assert "<task_context>" in resumed_payload

    # Tampering with the persisted manifest is not a migration signal.  A
    # subsequent dispatch must fail before making another Provider request.
    pointer = state_store.current_pointer()
    assert pointer is not None
    manifest_path = state_store.root / str(pointer["ref"])
    raw = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(raw.replace("恢复并继续剩余工作", "TAMPERED"), encoding="utf-8")

    drift_provider = _NaturalCompletionProvider()
    drift_runner = ReportingAgentRunner(
        tmp_path,
        bus,
        drift_provider,
        AgentDefaults(max_tool_iterations=1),
        timeout=5,
    )
    try:
        with pytest.raises(ValueError, match="context manifest"):
            await drift_runner.run(
                definition,
                envelope.model_copy(),
                [],
                workflow_id="wf-context-resume",
            )
    finally:
        await drift_runner.close_workflow("wf-context-resume")
        bus.shutdown()
        await bus_task
    assert drift_provider.requests == []


@pytest.mark.asyncio
async def test_legacy_loop_without_rebuilder_keeps_conversation_path(tmp_path: Path) -> None:
    provider = _NaturalCompletionProvider()
    loop = AgentLoop(
        agent_type="legacy-reporting-loop",
        workspace=tmp_path,
        tools=ToolRegistry(),
        bus=MessageBus(),
        config=AgentDefaults(),
        llm_provider=provider,
    )
    response = await loop._chat_with_retries(
        [
            # A legacy caller still controls the full working context and does
            # not opt into typed rebuilding.
            LLMMessage(role="user", content="legacy full context")
        ],
        None,
        "legacy-turn",
    )
    assert response.content == "本轮未提交 typed result。"
    assert loop.context_rebuilder is None
    assert provider.requests[0] == [("user", "legacy full context")]


@pytest.mark.asyncio
async def test_agent_loop_commits_successful_context_delivery_before_next_round(
    tmp_path: Path,
) -> None:
    provider = _NaturalCompletionProvider()
    evidence_text = "E-0001 现场开关温升达到 85 摄氏度，需复核接点电阻。" * 60
    rebuilder = ReportingContextRebuilder()
    rebuilder.begin_task(
        "run-delivery",
        "task-delivery",
        objective="基于当前证据完成判断",
        evidence=[
            EvidenceSlice(
                ref="Work/runs/run-delivery/preparation/evidence.jsonl",
                sha256=hashlib.sha256(evidence_text.encode()).hexdigest(),
                content=evidence_text,
            )
        ],
    )
    loop = AgentLoop(
        agent_type="typed-context-loop",
        workspace=tmp_path,
        tools=ToolRegistry(),
        bus=MessageBus(),
        config=AgentDefaults(),
        llm_provider=provider,
        context_rebuilder=rebuilder,
    )

    await loop._chat_with_retries([], None, "round-1", phase="initial")
    await loop._chat_with_retries([], None, "round-2", phase="tool_followup")

    first_payload = "\n".join(content for _role, content in provider.requests[0])
    second_payload = "\n".join(content for _role, content in provider.requests[1])
    assert evidence_text in first_payload
    assert evidence_text not in second_payload
    assert "E-0001 现场开关温升达到 85 摄氏度" in second_payload
    assert "#sha256=" in second_payload
    assert rebuilder.manifest is not None
    assert rebuilder.manifest.task_state is not None
    assert rebuilder.manifest.task_state.state["provider_context_delivery_count"] == 2


def test_semantic_continuation_keeps_only_bounded_prior_output_bridge(
    tmp_path: Path,
) -> None:
    rebuilder = ReportingContextRebuilder(tmp_path)
    rebuilder.begin_task("run-bridge", "task-bridge", objective="完成结构化提交")
    prior = "已完成判断：风险来自接点电阻升高。" * 400
    ReportingAgentRunner._context_reason_turn(
        rebuilder,
        content="请直接提交既有结论，不要重新检索。",
        turn_kind="submission_correction",
        prior_output=prior,
    )
    rebuilt = rebuilder.rebuild([])
    payload = "\n".join(message.content for message in rebuilt.messages)
    assert "请直接提交既有结论" in payload
    assert "风险来自接点电阻升高" in payload
    assert prior not in payload
    assert len(rebuilder.manifest.partial_tail[0]["content"]) == 2048
