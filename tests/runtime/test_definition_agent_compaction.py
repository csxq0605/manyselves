"""Typed turns on a real AgentLoop whose working memory overflows.

The scripted Provider answers every task request with ``submit_result`` and
every compaction request with a handoff summary, so the tests see what the
loop keeps between turns and what it sends.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.runtime.agent_execution import AgentExecutionService
from manyselves.runtime.conversation_history import drop_rejected_attempts, turn_start
from manyselves.runtime.definition_agent import DefinitionAgentInvoker
from manyselves.runtime.loops.agent_loop import AgentLoop
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.base import LLMStreamChunk, LLMToolCall
from manyselves.runtime.providers.base import Message as LLMMessage
from manyselves.runtime.services import RuntimeServicesView
from manyselves.runtime.tool_adapter import CapabilityToolAdapterFactory
from manyselves.runtime.tools.registry import ToolRegistry
from tests.runtime.test_definition_agent import (
    POLICY,
    RUN_ID,
    _conversation,
    _definitions,
    _task,
)

SUMMARY = "<context_handoff_summary>"
SUMMARY_WRITER = "You are the context-compaction handoff writer"


class Provider:
    """Submit the scripted answers in order; summarize on request."""

    model = "scripted"
    context_window = 128000

    def __init__(self, answers: list[dict[str, Any]]) -> None:
        self.answers = list(answers)
        self.task_requests: list[list[LLMMessage]] = []
        self.summary_requests = 0
        self.calls = 0

    async def chat_stream(self, messages, *args, **kwargs):
        del args, kwargs
        if str(messages[0].content or "").startswith(SUMMARY_WRITER):
            self.summary_requests += 1
            summary = {
                "progress": [f"summary {self.summary_requests}"],
                "decisions": [],
                "constraints": [],
                "remaining_work": [],
                "critical_refs": [],
            }
            yield LLMStreamChunk(delta=json.dumps(summary))
            yield LLMStreamChunk(
                done=True, usage={"input_tokens": 1, "output_tokens": 1}
            )
            return
        self.task_requests.append(list(messages))
        self.calls += 1
        call = LLMToolCall(
            id=f"call-{self.calls}",
            name="submit_result",
            arguments=self.answers.pop(0),
        )
        yield LLMStreamChunk(
            tool_calls=[call],
            done=True,
            usage={"input_tokens": 1, "output_tokens": 1},
            stop_reason="tool_use",
        )


class TruncatingProvider(Provider):
    """Answer every task request with the same output cut off at max_tokens.

    After ``limit`` requests it submits, so a loop that never stops shows up
    as a wrong result instead of a hang.
    """

    def __init__(self, partial: str, limit: int = 6) -> None:
        super().__init__([{"answer": "gave up"}])
        self.partial = partial
        self.limit = limit

    async def chat_stream(self, messages, *args, **kwargs):
        if str(messages[0].content or "").startswith(SUMMARY_WRITER):
            async for chunk in super().chat_stream(messages, *args, **kwargs):
                yield chunk
            return
        if self.calls >= self.limit:
            async for chunk in super().chat_stream(messages, *args, **kwargs):
                yield chunk
            return
        self.task_requests.append(list(messages))
        self.calls += 1
        yield LLMStreamChunk(delta=self.partial)
        yield LLMStreamChunk(
            done=True,
            usage={"input_tokens": 1, "output_tokens": 1},
            stop_reason="max_tokens",
        )


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        answers: list[dict[str, Any]],
        provider: Provider | None = None,
        strategy: str = "model_summary",
    ) -> None:
        self.bus = MessageBus()
        self.provider = provider or Provider(answers)
        self.loops: list[AgentLoop] = []
        self.registry, self.contracts = _definitions()
        tools = CapabilityToolAdapterFactory(
            "neutral", {"lookup": lambda arguments: arguments}, self.contracts
        )

        def build(**kwargs: Any) -> AgentLoop:
            loop = AgentLoop(**kwargs)
            self.loops.append(loop)
            return loop

        self.invoker = DefinitionAgentInvoker(
            RuntimeServicesView(
                workspace=tmp_path,
                bus=self.bus,
                active_provider=self.provider,
                agent_defaults=AgentDefaults(
                    working_memory_tokens=4096,
                    context_compaction_strategy=strategy,
                ),
                global_knowledge_root=None,
            ),
            workflow_id="neutral",
            run_id=RUN_ID,
            definitions=self.registry,
            contracts=self.contracts,
            tool_factory=tools.build,
            execution=AgentExecutionService(self.bus, timeout=5),
            loop_builder=build,
        )
        self.conversation = _conversation("a")

    async def ask(self, task_id: str, text: str, *, budget: int | None = None):
        return await self.invoker.invoke_with_recovery(
            self.registry.require("agent", "answerer"),
            _task(budget=budget),
            {"text": text},
            self.conversation,
            task_id=task_id,
            recovery_policy=POLICY,
        )

    async def run(self, steps):
        bus_task = asyncio.create_task(self.bus.process_queue())
        try:
            return await steps()
        finally:
            await self.invoker.close()
            self.bus.shutdown()
            await bus_task

    def history(self) -> list[LLMMessage]:
        return self.loops[0].conversation_messages()


def _requests(history: list[LLMMessage], text: str) -> list[LLMMessage]:
    return [
        message
        for message in history
        if message.role == "user"
        and not message.is_tool_result
        and text in (message.content or "")
        and SUMMARY not in (message.content or "")
    ]


def _submitted(history: list[LLMMessage]) -> list[dict[str, Any]]:
    return [
        call.arguments
        for message in history
        for call in message.tool_calls or ()
        if call.name == "submit_result"
    ]


@pytest.mark.asyncio
async def test_compaction_before_the_first_request_is_kept_and_the_task_survives(
    tmp_path: Path,
) -> None:
    harness = Harness(
        tmp_path, [{"answer": "one"}, {"answer": "two"}, {"answer": "three"}]
    )

    async def steps():
        await harness.ask("first", "甲" * 9000)
        second = await harness.ask("second", "第二问")
        after_second = harness.history()
        summaries = harness.provider.summary_requests
        await harness.ask("third", "第三问")
        return second, after_second, summaries

    second, history, summaries = await harness.run(steps)

    assert second.result == {"answer": "two"}
    # The turn's own request stays next to its accepted answer.
    assert len(_requests(history, "第二问")) == 1
    assert _submitted(history)[-1] == {"answer": "two"}
    # The compacted request became the history: one summary, the evicted
    # input is gone, and the next turn does not summarize it again.
    assert sum(SUMMARY in (m.content or "") for m in history) == 1
    assert not _requests(history, "甲" * 100)
    assert summaries == 1
    assert harness.provider.summary_requests == 1


@pytest.mark.asyncio
async def test_a_rejected_first_submission_neither_resummarizes_nor_loses_the_task(
    tmp_path: Path,
) -> None:
    harness = Harness(
        tmp_path, [{"answer": "one"}, {"wrong": 1}, {"answer": "two"}]
    )

    async def steps():
        await harness.ask("first", "甲" * 9000)
        return await harness.ask("second", "第二问")

    outcome = await harness.run(steps)

    assert outcome.result == {"answer": "two"}
    history = harness.history()
    assert len(_requests(history, "第二问")) == 1
    assert {"wrong": 1} not in _submitted(history)
    # The tool loop continues from the compacted request instead of
    # rebuilding the full history and summarizing it a second time.
    assert harness.provider.summary_requests == 1
    first, retry = harness.provider.task_requests[1:3]
    assert [m.content for m in retry[: len(first) - 1]] == [
        m.content for m in first[: len(first) - 1]
    ]


@pytest.mark.asyncio
async def test_a_fresh_conversation_does_not_carry_the_earlier_summary(
    tmp_path: Path,
) -> None:
    harness = Harness(
        tmp_path, [{"answer": "one"}, {"answer": "two"}, {"answer": "three"}]
    )

    async def steps():
        await harness.ask("first", "甲" * 9000)
        await harness.ask("second", "第二问")
        return await harness.ask("third", "第三问", budget=50)

    outcome = await harness.run(steps)

    assert outcome.result == {"answer": "three"}
    notes = (tmp_path / f"Work/runs/{RUN_ID}/budget-notes.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"fresh_conversation"' in notes
    sent = harness.provider.task_requests[-1]
    assert not any(SUMMARY in (m.content or "") for m in sent)
    assert [m.role for m in sent[1:2]] == ["user"]
    assert "第三问" in sent[1].content


@pytest.mark.asyncio
async def test_a_new_summary_is_not_progress_for_a_repeated_truncated_output(
    tmp_path: Path,
) -> None:
    # Each truncated output pushes the history over the working-memory
    # threshold, so every continuation request is compacted again.  The
    # summary is written by the runtime, so it must not count as the model
    # moving forward.
    provider = TruncatingProvider("乙" * 5500)
    harness = Harness(tmp_path, [], provider=provider)

    outcome = await harness.run(lambda: harness.ask("only", "问题"))

    assert outcome.status == "incomplete"
    assert "no_progress after max_tokens" in (outcome.error or "")
    assert len(provider.task_requests) == 2


def test_the_turn_is_found_by_its_request_after_compaction_moved_it() -> None:
    def call(call_id: str) -> dict[str, Any]:
        return {"id": call_id, "name": "submit_result", "arguments": {}}

    task = "<task>answer</task>"
    history = [
        {"role": "user", "content": f"{SUMMARY}\n{{}}\n</context_handoff_summary>"},
        {"role": "user", "content": task},
        {"role": "assistant", "content": "", "tool_calls": [call("1")]},
        {"role": "tool", "content": "invalid", "tool_call_id": "1", "is_tool_result": True},
        {"role": "assistant", "content": "", "tool_calls": [call("2")]},
        {"role": "tool", "content": "ok", "tool_call_id": "2", "is_tool_result": True},
    ]

    # Before compaction the history was empty; the request moved to index 1.
    start = turn_start(history, task + "\n")
    assert start == 1
    kept = drop_rejected_attempts(history, start)
    assert [m["content"] for m in kept] == [history[0]["content"], task, "", "ok"]
    # A request that compaction evicted is not guessed at.
    assert turn_start(history[2:], task) is None


def test_replacing_the_conversation_drops_a_summary_it_does_not_contain(
    tmp_path: Path,
) -> None:
    loop = AgentLoop(
        agent_type="test",
        workspace=tmp_path,
        tools=ToolRegistry(),
        bus=MessageBus(),
        config=AgentDefaults(),
        llm_provider=Provider([]),
        system_prompt="system",
    )
    summary = {"progress": ["earlier work"], "sequence": 1}
    loop.restore_conversation([], handoff_summary=summary)

    kept = [LLMMessage(role="user", content=f"{SUMMARY}\n{{}}\n</context_handoff_summary>")]
    loop.replace_conversation(kept)
    assert loop.handoff_summary == summary

    loop.replace_conversation([])
    assert loop.handoff_summary is None
    messages = asyncio.run(loop._build_messages())
    assert [m.role for m in messages] == ["system"]
