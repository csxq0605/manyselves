import asyncio
from pathlib import Path

import pytest

from autoreport.config.schema import AgentDefaults
from autoreport.core.loops.bus import MessageBus
from autoreport.core.providers.base import LLMProvider, LLMResponse, LLMToolCall
from autoreport.core.reporting.agent_runner import ReportingAgentRunner
from autoreport.core.reporting.agentic_models import AgentRunStatus, ModuleSubmission, TaskEnvelope
from autoreport.core.reporting.config import load_packaged_workflow


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
                            "claims": [
                                {
                                    "id": "C-2.1-001",
                                    "module_id": "2.1",
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


@pytest.mark.asyncio
async def test_reporting_agent_runner_uses_real_isolated_loop_and_can_finish_without_research(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    provider = DirectSubmissionProvider()
    agents, _workflow = load_packaged_workflow()
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
    assert isinstance(result.payload, ModuleSubmission)
    assert "01_页面导入知识库" not in provider.system_prompts[0]
    assert "02_本地skill提示词资料_禁止导入" not in provider.system_prompts[0]
    assert "负荷率必须保留计算口径" in provider.system_prompts[0]
    assert "剩余电流大于 10A" not in provider.system_prompts[0]
    assert (tmp_path / "Work/runs/run-test/results/module-2.1.json").is_file()
