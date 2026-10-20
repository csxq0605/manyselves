"""Offline S05 experiment: how much of each author request is a reusable prefix.

Drives the production typed-context path (Reporting ``bind_task_context`` →
``TaskContextRebuilder`` → ``AgentLoop._with_live_task_state`` → typed trim →
Provider sanitizing) through a scripted 2.4 author session built from real
archived inputs: the 2.4 fact packet, evidence JSON tool results and report
prose written with the real ``write_result_part`` tool.  Every request is then
serialized by the production Anthropic and OpenAI-compatible converters.

Two cache models are reported per wire format:

* ``auto_prefix``: automatic prefix caching (OpenAI-compatible gateways such
  as MiMo/DeepSeek): leading messages identical to the previous request.
* ``anthropic_breakpoints``: explicit ``cache_control`` breakpoints with the
  documented 20-block lookback against prefixes written by earlier requests.

No Provider call is made.  Token counts use a tokenizer file when given,
otherwise the production estimator.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from loguru import logger

from manyselves.capabilities.distribution_reporting.runtime.collaboration_tools import (
    WriteResultPartTool,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.capabilities.distribution_reporting.runtime.task_context import bind_task_context
from manyselves.config.schema import AgentDefaults
from manyselves.runtime.loops import agent_loop as loop_module
from manyselves.runtime.loops.agent_loop import AgentLoop
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.anthropic_provider import AnthropicProvider
from manyselves.runtime.providers.base import LLMToolCall, Message
from manyselves.runtime.providers.openai_provider import OpenAICompatProvider
from manyselves.runtime.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "docs/research/session-records/2026-10-10_to_2026-10-15/audit_manyselves"
CAP = ROOT / "manyselves/capabilities/distribution_reporting"
logger.disable("manyselves")
LOOKBACK = 20


def _blocks(system, messages) -> list[str]:
    """Flatten a serialized request into cacheable content blocks."""

    out: list[str] = []
    for block in system if isinstance(system, list) else ([system] if system else []):
        out.append(json.dumps(block, ensure_ascii=False, sort_keys=True))
    for message in messages:
        content = message.get("content")
        role = message.get("role")
        extra = {k: v for k, v in message.items() if k not in {"content", "role"}}
        items = content if isinstance(content, list) else [content]
        for index, block in enumerate(items):
            out.append(json.dumps([role, extra if index == 0 else None, block],
                                  ensure_ascii=False, sort_keys=True))
    return out


def _breakpoints(blocks: list[str]) -> list[int]:
    return [i + 1 for i, block in enumerate(blocks) if '"cache_control"' in block]


class _Counter:
    def __init__(self, tokenizer: str | None) -> None:
        self._tok = None
        if tokenizer:
            from tokenizers import Tokenizer

            self._tok = Tokenizer.from_file(tokenizer)
        self._cache: dict[str, int] = {}

    def __call__(self, text: str) -> int:
        if text not in self._cache:
            self._cache[text] = (
                len(self._tok.encode(text, add_special_tokens=False).ids)
                if self._tok else loop_module._estimate_text_tokens(text)
            )
        return self._cache[text]


def _score(requests: list[list[str]], count: _Counter) -> dict:
    total = auto = anth = 0
    previous: list[str] = []
    written: set[str] = set()
    for blocks in requests:
        sizes = [count(b) for b in blocks]
        total += sum(sizes)
        same = 0
        while same < min(len(blocks), len(previous)) and blocks[same] == previous[same]:
            same += 1
        auto += sum(sizes[:same])
        prefix_hash = [hashlib.sha256("\x00".join(blocks[:k]).encode()).hexdigest()
                       for k in range(len(blocks) + 1)]
        hit = 0
        for point in _breakpoints(blocks):
            for k in range(point, max(0, point - LOOKBACK) - 1, -1):
                if prefix_hash[k] in written:
                    hit = max(hit, k)
                    break
        anth += sum(sizes[:hit])
        written.update(prefix_hash[k] for k in _breakpoints(blocks))
        previous = blocks
    return {
        "requests": len(requests),
        "input_tokens": total,
        "auto_prefix_cacheable": auto,
        "auto_prefix_share": round(auto / total, 3),
        "anthropic_breakpoint_cacheable": anth,
        "anthropic_breakpoint_share": round(anth / total, 3),
    }


async def _session() -> list[list[Message]]:
    workspace = Path(tempfile.mkdtemp())
    loop = AgentLoop(agent_type="module-2.4-specialist", workspace=workspace, tools=ToolRegistry(),
                     bus=MessageBus(), llm_provider=None, config=AgentDefaults())
    envelope = TaskEnvelope(task_id="module-2.4", run_id="cache-probe", agent_id="module-2.4-specialist",
                            revision=0, objective="author module 2.4", allowed_outputs=["module_submission"])
    facts = (ARCHIVE / "fact_packet_replay/Work/runs/replay/context/module-2.4-facts.md").read_text(encoding="utf-8")
    evidence = (ARCHIVE / "evidence_compact.txt").read_text(encoding="utf-8")
    prose = (ARCHIVE / "配电安全专家咨询报告.docx.txt").read_text(encoding="utf-8")
    system = (CAP / "agents/module-2.4-specialist.md").read_text(encoding="utf-8")
    projection = bind_task_context(loop, workspace, envelope, output_contract="module_submission",
                                   render_prompt=lambda: facts)
    writer = WriteResultPartTool(envelope.run_id, envelope.task_id, 0, ReportingStore(workspace))
    history = [Message(role="system", content=system, cache_control=True),
               Message(role="user", content=projection.initial_message())]
    sent: list[list[Message]] = []

    async def request() -> None:
        rebased = await projection.rebuild(history, loop=loop)
        messages = loop._with_live_task_state(rebased.messages)
        trim = loop_module._trim_typed_context_to_budget(
            list(messages), context_window=loop._context_window_for_request(),
            max_output=loop.config.max_tokens,
        )
        provider = loop_module._provider_working_messages(trim.messages)
        # Same order as AgentLoop._chat_with_retries; absent before S05.
        rolling = getattr(loop_module, "_with_rolling_cache_breakpoint", None)
        if rolling is not None:
            provider = rolling(provider)
        sent.append(loop_module._sanitize_provider_messages(provider))

    for i in range(6):  # evidence research turns
        await request()
        call = LLMToolCall(id=f"search-{i}", name="search_project_evidence",
                           arguments={"query": f"2.4 检测项 {i}"})
        history += [Message(role="assistant", content="", tool_calls=[call]),
                    Message(role="tool", content=evidence[i * 3000:(i + 1) * 3000],
                            tool_call_id=call.id, is_tool_result=True)]
    for i in range(6):  # drafting turns with the production writer
        await request()
        part_id = f"2.4.{i + 1}.1"
        text = prose[20_000 + i * 2500:20_000 + (i + 1) * 2500]
        saved = await writer(part_id=part_id, content=text)
        call = LLMToolCall(id=f"write-{i}", name="write_result_part",
                           arguments={"part_id": part_id, "content": text})
        history += [Message(role="assistant", content="", tool_calls=[call]),
                    Message(role="tool", content=json.dumps(saved, ensure_ascii=False),
                            tool_call_id=call.id, is_tool_result=True)]
    await request()  # final submission turn
    return sent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tokenizer")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    sent = asyncio.run(_session())
    count = _Counter(args.tokenizer)
    anthropic = object.__new__(AnthropicProvider)
    anthropic._supports_cache = True
    openai = object.__new__(OpenAICompatProvider)
    openai.model = "mimo-v2.5-pro"
    result = {
        "tokenizer": Path(args.tokenizer).name if args.tokenizer else "production-estimator",
        "anthropic_native": _score([_blocks(*anthropic._convert_messages(m)) for m in sent], count),
        "openai_compatible": _score([_blocks(None, openai._convert_messages(m)) for m in sent], count),
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
