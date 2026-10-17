"""Synthetic offline replay. Measures request frequency, not model quality/time."""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "manyselves"))
from loguru import logger
from manyselves.config.schema import AgentDefaults
from manyselves.runtime.loops.agent_loop import AgentLoop, _estimate_tokens
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.base import LLMToolCall, Message
from manyselves.runtime.tools.registry import ToolRegistry


async def replay(threshold, chars, rounds):
    metrics = {"threshold_estimated_tokens": threshold, "tool_result_chars": chars,
               "normal_rounds": rounds, "summary_calls": 0, "summary_source_estimated_tokens": 0,
               "normal_input_estimated_tokens": 0, "compaction_rounds": [],
               "post_compaction_estimated_tokens": [], "provider_calls": 0}
    async def summarize(messages):
        metrics["summary_calls"] += 1
        metrics["summary_source_estimated_tokens"] += _estimate_tokens(messages)
        return {"progress": ["Earlier observations read"],
                "decisions": ["Keep measured values separate from nominal values"],
                "constraints": ["Use cited evidence"], "remaining_work": ["Continue reading"],
                "critical_refs": []}
    agent = AgentLoop(agent_type="offline-probe", workspace=ROOT / "audit_manyselves",
                      tools=ToolRegistry(), bus=MessageBus(), llm_provider=None,
                      config=AgentDefaults(working_memory_tokens=threshold))
    agent._request_model_handoff_summary = summarize
    messages = [Message(role="system", content="Use sources."), Message(role="user", content="Review every measurement.")]
    for n in range(rounds):
        messages.extend([
            Message(role="assistant", content="", tool_calls=[LLMToolCall(
                id=f"read-{n}", name="read", arguments={"ref":f"Work/{n}.json"})]),
            Message(role="tool", tool_call_id=f"read-{n}", is_tool_result=True,
                    content=json.dumps({"ref":f"Work/{n}.json", "content":"x" * chars})),
        ])
        before = metrics["summary_calls"]
        messages = await agent._compact_working_memory_async(messages)
        size = _estimate_tokens(messages)
        metrics["normal_input_estimated_tokens"] += size
        if metrics["summary_calls"] != before:
            metrics["compaction_rounds"].append(n)
            metrics["post_compaction_estimated_tokens"].append(size)
        assert messages[-1].tool_call_id == f"read-{n}"
    return metrics


async def main():
    logger.remove()
    metrics = {"measurement": "synthetic same tool sequence and fixed mock semantic summary; no Provider called",
               "scenarios": [await replay(4096,1600,60), await replay(36000,6000,100)]}
    output = ROOT / f"audit_manyselves/compaction_efficiency_{sys.argv[1]}.json"
    output.write_text(json.dumps(metrics,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(metrics,ensure_ascii=False,indent=2))


asyncio.run(main())
