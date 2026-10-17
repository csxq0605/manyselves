"""Small live smoke test using only synthetic prompts; never reads customer files."""
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "manyselves"))

import httpx
from loguru import logger

from manyselves.config.schema import AgentDefaults, Settings
from manyselves.runtime.loops.agent_loop import AgentLoop, _estimate_tokens, _render_handoff_summary
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.anthropic_provider import AnthropicProvider
from manyselves.runtime.providers.base import LLMToolCall, Message
from manyselves.runtime.tools.registry import ToolRegistry

logger.disable("manyselves")


async def main():
    config = Settings().load_config()
    route = next(c for c in config.providers.configurations if c.id == config.providers.active)
    if route.provider != "anthropic" or urlsplit(route.api_base or "").hostname != "api.anthropic.com":
        raise RuntimeError("The selected route is not the intended official API")
    if not route.enabled or not route.api_key:
        raise RuntimeError("The official API credential is not ready")
    provider = AnthropicProvider(api_key=route.api_key, api_base=route.api_base,
                                 model=route.default_model, extra_headers=route.extra_headers)
    provider.client = provider.client.with_options(timeout=httpx.Timeout(30, connect=10), max_retries=0)
    report = {"started_utc": datetime.now(timezone.utc).isoformat(), "model": provider.model,
              "data_scope": "synthetic_only", "checks": [], "live_generation_requests_started": 0}
    output = ROOT / "audit_manyselves/claude_official_smoke_result.json"

    async def step(name, action):
        started = time.perf_counter()
        try:
            async with asyncio.timeout(45):
                detail = await action()
            item = {"check": name, "ok": bool(detail.pop("ok", True)), **detail}
        except Exception as exc:
            item = {"check": name, "ok": False, "error_class": type(exc).__name__,
                    "http_status": getattr(exc, "status_code", None)}
            body = getattr(exc, "body", None)
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                item["api_error_type"] = body["error"].get("type")
        item["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        report["checks"].append(item)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(item, ensure_ascii=False), flush=True)
        return item["ok"]

    async def basic():
        report["live_generation_requests_started"] += 1
        response = await provider.chat([Message(role="user", content="Synthetic connectivity test. Reply with exactly READY.")],
                                       max_tokens=512, reasoning_enabled=False)
        return {"ok": (response.content or "").strip() == "READY",
                "stop_reason": response.stop_reason, "usage": response.usage}

    tools = [{"name": "synthetic_sample", "description": "Return a fictional test value, not customer data.",
              "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}}]
    messages = [Message(role="user", content="Synthetic tool test. Call synthetic_sample once, then report exactly its returned number. Do not guess.")]

    async def tool_roundtrip():
        report["live_generation_requests_started"] += 1
        response = await provider.chat(messages, tools=tools, max_tokens=512)
        if not response.tool_calls or any(call.name != "synthetic_sample" for call in response.tool_calls):
            return {"ok": False, "reason": "expected_synthetic_tool_not_called", "usage": response.usage}
        messages.append(Message(role="assistant", content=response.content or "", tool_calls=response.tool_calls))
        for call in response.tool_calls:
            messages.append(Message(role="tool", content='{"value": 314159}', is_tool_result=True, tool_call_id=call.id))
        report["live_generation_requests_started"] += 1
        followup = await provider.chat(messages, tools=tools, max_tokens=512)
        return {"ok": "314159" in (followup.content or "") and not followup.tool_calls,
                "first_usage": response.usage, "followup_usage": followup.usage,
                "stop_reason": followup.stop_reason}

    async def compaction():
        agent = AgentLoop(agent_type="synthetic-smoke", workspace=ROOT / "audit_manyselves/synthetic-smoke",
                          tools=ToolRegistry(), bus=MessageBus(), llm_provider=provider,
                          config=AgentDefaults(working_memory_tokens=4096, max_tokens=2048,
                                               context_compaction_model=config.agents.defaults.context_compaction_model))
        agent.persist_handoff_summary = False
        agent.begin_typed_task({"task_id": "synthetic-only", "objective": "retain unresolved fictional exception",
                                "constraints": ["All values are synthetic; do not certify any real installation."]})
        async def count_attempt():
            report["live_generation_requests_started"] += 1
        agent.before_provider_attempt = count_attempt
        history = [Message(role="system", content="Summarize synthetic test history."),
                   Message(role="user", content="Track fictional sample A17; missing confirmation remains unresolved."),
                   Message(role="user", content=_render_handoff_summary({
                       "progress": [], "decisions": ["A17 must remain unresolved until confirmation."],
                       "constraints": [], "remaining_work": ["Obtain confirmation for A17."], "critical_refs": []}))]
        for index in range(22):
            history.extend([
                Message(role="assistant", content="", tool_calls=[LLMToolCall(id=f"s-{index}", name="synthetic_sample", arguments={})]),
                Message(role="tool", tool_call_id=f"s-{index}", is_tool_result=True,
                        content=f"Synthetic observation {index}. " + "Fictional test filler with no findings. " * 30),
            ])
        result = await agent._compact_working_memory_async(history)
        summary = agent.handoff_summary or {}
        serialized = json.dumps(summary)
        return {"ok": summary.get("source") == "model" and "A17" in serialized and result[-2:] == history[-2:],
                "source": summary.get("source"), "before_estimated_tokens": _estimate_tokens(history),
                "after_estimated_tokens": _estimate_tokens(result), "retained_latest_exchange": result[-2:] == history[-2:],
                "summary": summary, "usage": agent._last_usage_record and {
                    k: agent._last_usage_record.get(k) for k in ("model", "phase", "input_tokens", "output_tokens", "usage_source", "status")}}

    try:
        if not await step("official_connection", basic):
            return
        if not await step("tool_roundtrip", tool_roundtrip):
            return
        await step("incremental_compaction", compaction)
    finally:
        await provider.client.close()


if __name__ == "__main__":
    asyncio.run(main())
