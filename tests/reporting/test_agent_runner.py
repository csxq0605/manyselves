import asyncio
import json
from pathlib import Path

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider, LLMResponse, LLMToolCall
from manyselves.core.reporting.agent_runner import ReportingAgentRunner
from manyselves.core.reporting.agentic_models import AgentRunStatus, ModuleSubmission, TaskEnvelope
from manyselves.core.reporting.config import load_packaged_agents
from manyselves.interfaces.types import PeerQueryMessage, PeerReplyMessage


class DirectSubmissionProvider(LLMProvider):
    def __init__(self):
        super().__init__("test", model="scripted")
        self.system_prompts: list[str] = []

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        self.system_prompts.append(messages[0].content)
        if any(message.is_tool_result for message in messages):
            return LLMResponse(content="已提交。")
        return LLMResponse(
            content="",
            tool_calls=[
                LLMToolCall(
                    id="submit-1",
                    name="submit_result",
                    arguments={
                        "payload": {
                            "kind": "module_submission",
                            "module_id": "2.1",
                            "markdown": "配电结构分析呈现系统关系，而非固定字段复述。",
                            "submodule_narratives": {
                                "2.1.1": "负荷与容量分析。",
                                "2.1.2": "关键负荷路径分析。",
                                "2.1.3": "自动切换分析。",
                                "2.1.4": "并联闭锁分析。",
                                "2.1.5": "无功补偿分析。",
                            },
                            "claims": [
                                {
                                    "id": "C-2.1-001",
                                    "module_id": "2.1",
                                    "submodule_id": "2.1.1",
                                    "text": "配电结构分析呈现系统关系，而非固定字段复述。",
                                    "claim_type": "technical_interpretation",
                                    "source_ids": [],
                                    "confidence": 0.5,
                                    "footnote_required": False,
                                    "unresolved": True,
                                }
                            ],
                            "source_ids": [],
                            "unresolved_questions": ["待补充一次系统图"],
                            "revision": 0,
                        }
                    },
                )
            ],
        )


class NonRetryableFailureProvider(LLMProvider):
    def __init__(self):
        super().__init__("test", model="failing")

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        raise ValueError("invalid provider request")


class RepeatedInvalidSubmissionProvider(LLMProvider):
    """Keeps issuing the same invalid typed submission after seeing its error."""

    def __init__(self):
        super().__init__("test", model="repeated-invalid")
        self.calls = 0

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        self.calls += 1
        return LLMResponse(
            content="",
            tool_calls=[
                LLMToolCall(
                    id=f"invalid-{self.calls}",
                    name="submit_result",
                    arguments={
                        "payload": {
                            "kind": "plan_submission",
                            "rationale": "规划已完成。",
                            "module_tasks": {"wrong": []},
                        }
                    },
                )
            ],
        )


class RepeatedIncompletePlanProvider(LLMProvider):
    """Keeps submitting a type-valid plan that omits a required specialist."""

    def __init__(self):
        super().__init__("test", model="repeated-incomplete")
        self.calls = 0

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        self.calls += 1
        return LLMResponse(
            content="",
            tool_calls=[
                LLMToolCall(
                    id=f"incomplete-{self.calls}",
                    name="submit_result",
                    arguments={
                        "payload": {
                            "kind": "plan_submission",
                            "rationale": "只安排一个模块。",
                            "module_tasks": [
                                {
                                    "task_id": "module-2.1",
                                    "run_id": "run-incomplete-plan",
                                    "agent_id": "module-2.1-specialist",
                                    "objective": "完成 2.1 分析。",
                                    "allowed_outputs": ["module_submission"],
                                }
                            ],
                        }
                    },
                )
            ],
        )


@pytest.mark.asyncio
async def test_reporting_agent_runner_replies_when_a_peer_is_not_running(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    runner = ReportingAgentRunner(
        tmp_path, bus, DirectSubmissionProvider(), AgentDefaults(max_tool_iterations=5)
    )
    query = PeerQueryMessage(
        workflow_id="wf-test",
        task_id="report-plan",
        query_id="query-unavailable",
        sender="report-planner",
        recipient="main-agent",
        source_session_id="session-plan",
        question="请确认项目边界。",
    )

    await runner._route_peer_query(query)

    reply = await bus._queue.get()
    assert isinstance(reply, PeerReplyMessage)
    assert reply.recipient == "report-planner"
    assert reply.target_session_id == "session-plan"
    assert "not available" in reply.answer


@pytest.mark.asyncio
async def test_reporting_agent_runner_uses_real_isolated_loop_and_can_finish_without_research(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    provider = DirectSubmissionProvider()
    agents = load_packaged_agents()
    runner = ReportingAgentRunner(
        tmp_path, bus, provider, AgentDefaults(max_tool_iterations=5), timeout=5
    )
    envelope = TaskEnvelope(
        task_id="module-2.1",
        run_id="run-test",
        agent_id="module-2.1-specialist",
        objective="完成 2.1 分析",
        allowed_outputs=["module_submission"],
    )

    try:
        result = await runner.run(
            agents["module-2.1-specialist"],
            envelope,
            [],
            workflow_id="wf-test",
            session_key="specialist-2.1",
        )
    finally:
        await runner.close_workflow("wf-test")
        bus.shutdown()
        await bus_task

    assert result.status is AgentRunStatus.COMPLETED
    assert len(provider.system_prompts) == 1
    assert isinstance(result.payload, ModuleSubmission)
    assert "负荷率必须保留计算口径" in provider.system_prompts[0]
    assert "剩余电流大于 10A" not in provider.system_prompts[0]
    assert (tmp_path / "Work/runs/run-test/results/module-2.1.json").is_file()
    summaries = list((tmp_path / "Work/runs/run-test/session-summaries").glob("*.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8"))
    assert summary["agent_id"] == "module-2.1-specialist"
    assert summary["status"] == "completed"
    assert summary["context_only"] is True
    usage_rows = [
        json.loads(line)
        for line in (tmp_path / ".manyselves/usage/run-test.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(usage_rows) == 1
    assert usage_rows[0]["status"] == "success"
    assert runner._routers == {}


@pytest.mark.asyncio
async def test_reporting_agent_runner_fails_immediately_when_isolated_loop_errors(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    runner = ReportingAgentRunner(
        tmp_path,
        bus,
        NonRetryableFailureProvider(),
        AgentDefaults(max_tool_iterations=5),
        timeout=600,
    )
    envelope = TaskEnvelope(
        task_id="report-plan",
        run_id="run-fail-fast",
        agent_id="report-planner",
        objective="规划报告",
        allowed_outputs=["plan_submission"],
    )

    try:
        with pytest.raises(RuntimeError, match="数据或参数校验失败"):
            await asyncio.wait_for(
                runner.run(
                    load_packaged_agents()["report-planner"],
                    envelope,
                    [],
                    workflow_id="wf-fail-fast",
                ),
                timeout=1,
            )
    finally:
        await runner.close_workflow("wf-fail-fast")
        bus.shutdown()
        await bus_task


@pytest.mark.asyncio
async def test_reporting_agent_runner_stops_after_repeated_identical_submission_validation_error(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    provider = RepeatedInvalidSubmissionProvider()
    runner = ReportingAgentRunner(
        tmp_path, bus, provider, AgentDefaults(max_tool_iterations=10), timeout=5
    )
    envelope = TaskEnvelope(
        task_id="report-plan",
        run_id="run-invalid-submission",
        agent_id="report-planner",
        objective="规划报告",
        allowed_outputs=["plan_submission"],
    )

    try:
        with pytest.raises(RuntimeError, match="重复的 submit_result 参数校验错误"):
            await runner.run(
                load_packaged_agents()["report-planner"],
                envelope,
                [],
                workflow_id="wf-invalid-submission",
            )
    finally:
        await runner.close_workflow("wf-invalid-submission")
        bus.shutdown()
        await bus_task

    assert provider.calls == 2


@pytest.mark.asyncio
async def test_reporting_agent_runner_stops_after_repeated_incomplete_plan(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    provider = RepeatedIncompletePlanProvider()
    runner = ReportingAgentRunner(
        tmp_path, bus, provider, AgentDefaults(max_tool_iterations=10), timeout=5
    )
    envelope = TaskEnvelope(
        task_id="report-plan",
        run_id="run-incomplete-plan",
        agent_id="report-planner",
        objective="规划报告",
        allowed_outputs=["plan_submission"],
        expected_plan_agent_ids=["module-2.1-specialist", "module-2.2-specialist"],
    )

    try:
        with pytest.raises(RuntimeError, match="重复的 submit_result 参数校验错误"):
            await runner.run(
                load_packaged_agents()["report-planner"],
                envelope,
                [],
                workflow_id="wf-incomplete-plan",
            )
    finally:
        await runner.close_workflow("wf-incomplete-plan")
        bus.shutdown()
        await bus_task

    assert provider.calls == 2
