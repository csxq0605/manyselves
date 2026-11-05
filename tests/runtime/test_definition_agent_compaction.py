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
from manyselves.runtime.conversation_history import (
    FileConversationHistoryStore,
    drop_rejected_attempts,
)
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


class LookupProvider(Provider):
    """Call lookup with each scripted page in turn, then submit."""

    def __init__(self, pages: list[str]) -> None:
        super().__init__([{"answer": "done"}])
        self.pages = list(pages)

    async def chat_stream(self, messages, *args, **kwargs):
        if str(messages[0].content or "").startswith(SUMMARY_WRITER) or not self.pages:
            async for chunk in super().chat_stream(messages, *args, **kwargs):
                yield chunk
            return
        self.task_requests.append(list(messages))
        self.calls += 1
        call = LLMToolCall(
            id=f"call-{self.calls}", name="lookup", arguments={"text": self.pages.pop(0)}
        )
        yield LLMStreamChunk(
            tool_calls=[call],
            done=True,
            usage={"input_tokens": 1, "output_tokens": 1},
            stop_reason="tool_use",
        )


class ScriptedProvider(Provider):
    """Answer each task request with the next scripted step.

    A step is ``("call", [(tool, arguments), ...])`` for one round of tool
    calls, or ``("say", text)`` for a reply that submits nothing.
    """

    def __init__(self, steps: list[tuple[str, Any]]) -> None:
        super().__init__([])
        self.steps = list(steps)

    async def chat_stream(self, messages, *args, **kwargs):
        if str(messages[0].content or "").startswith(SUMMARY_WRITER):
            async for chunk in super().chat_stream(messages, *args, **kwargs):
                yield chunk
            return
        self.task_requests.append(list(messages))
        self.calls += 1
        kind, value = self.steps.pop(0)
        usage = {"input_tokens": 1, "output_tokens": 1}
        if kind == "say":
            yield LLMStreamChunk(delta=value)
            yield LLMStreamChunk(done=True, usage=usage, stop_reason="end_turn")
            return
        calls = [
            LLMToolCall(id=f"call-{self.calls}-{index}", name=name, arguments=arguments)
            for index, (name, arguments) in enumerate(value)
        ]
        yield LLMStreamChunk(
            tool_calls=calls, done=True, usage=usage, stop_reason="tool_use"
        )


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        answers: list[dict[str, Any]],
        provider: Provider | None = None,
        strategy: str = "model_summary",
        max_tool_iterations: int = 200,
        page_chars: int = 3000,
        max_tool_calls_per_round: int = 4,
        working_memory_tokens: int = 4096,
    ) -> None:
        self.bus = MessageBus()
        self.provider = provider or Provider(answers)
        self.loops: list[AgentLoop] = []
        self.lookups: list[str] = []
        self.registry, self.contracts = _definitions()

        def lookup(arguments: dict[str, Any]) -> dict[str, Any]:
            # Each page is long enough that a few of them fill working memory.
            self.lookups.append(arguments["text"])
            return {"answer": f"{arguments['text']}:" + "丙" * page_chars}

        tools = CapabilityToolAdapterFactory("neutral", {"lookup": lookup}, self.contracts)

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
                    working_memory_tokens=working_memory_tokens,
                    context_compaction_strategy=strategy,
                    max_tool_iterations=max_tool_iterations,
                    max_tool_calls_per_round=max_tool_calls_per_round,
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

    async def ask(
        self,
        task_id: str,
        text: str,
        *,
        budget: int | None = None,
        tools: list[str] | None = None,
    ):
        return await self.invoker.invoke_with_recovery(
            self.registry.require("agent", "answerer"),
            _task(budget=budget, tools=tools),
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
@pytest.mark.parametrize("strategy", ["model_summary", "structured_handoff"])
async def test_the_summary_keeps_the_task_constraints_after_the_task_is_evicted(
    tmp_path: Path, strategy: str
) -> None:
    # The summary writer returns no constraints, and the second turn's
    # compaction evicts the first task message; the task's own objective and
    # constraints must still reach the model through the summary.
    harness = Harness(
        tmp_path, [{"answer": "one"}, {"answer": "two"}], strategy=strategy
    )

    async def steps():
        await harness.ask("first", "甲" * 9000)
        return await harness.ask("second", "第二问")

    outcome = await harness.run(steps)

    assert outcome.result == {"answer": "two"}
    sent = harness.provider.task_requests[-1]
    summaries = [m.content for m in sent if SUMMARY in (m.content or "")]
    assert len(summaries) == 1
    payload = json.loads(summaries[0].split("\n", 1)[1].rsplit("\n", 1)[0])
    assert "Be brief." in payload["constraints"]
    assert payload["active_task"]["objective"] == "Answer the question in one word."
    assert not _requests(sent, "甲" * 100)


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

    assert provider.summary_requests >= 1
    assert any(SUMMARY in (m.content or "") for m in provider.task_requests[1])
    assert outcome.status == "incomplete"
    assert "no_progress after max_tokens" in (outcome.error or "")
    assert len(provider.task_requests) == 2


@pytest.mark.asyncio
async def test_a_new_tool_result_evicted_within_a_slice_is_progress(
    tmp_path: Path,
) -> None:
    # Slices of two tool rounds; the call that hits a slice boundary is not
    # run.  The second slice reads a new page (p3) and then re-reads p2;
    # compaction evicts the p3 round before the slice ends, so the history
    # alone shows only a repeat of the first slice.
    provider = LookupProvider(["p1", "p2", "p3", "p3", "p2", "p4"])
    harness = Harness(tmp_path, [], provider=provider, max_tool_iterations=2)

    outcome = await harness.run(lambda: harness.ask("only", "问题", tools=["lookup"]))

    assert harness.provider.summary_requests >= 1
    assert outcome.status == "ok"
    assert outcome.result == {"answer": "done"}


@pytest.mark.asyncio
async def test_a_slice_that_only_rereads_the_same_page_stops(tmp_path: Path) -> None:
    provider = LookupProvider(["p1"] * 10)
    harness = Harness(tmp_path, [], provider=provider, max_tool_iterations=2)

    outcome = await harness.run(lambda: harness.ask("only", "问题", tools=["lookup"]))

    assert harness.provider.summary_requests >= 1
    assert outcome.status == "incomplete"
    assert "no_progress after tool_slice_boundary" in (outcome.error or "")


def test_a_turn_whose_request_was_evicted_still_drops_its_rejected_attempts() -> None:
    def call(call_id: str) -> dict[str, Any]:
        return {"id": call_id, "name": "submit_result", "arguments": {}}

    task = "<task>answer</task>"
    summary = {"role": "user", "content": f"{SUMMARY}\n{{}}\n</context_handoff_summary>"}
    attempts = [
        {"role": "assistant", "content": "", "tool_calls": [call("1")]},
        {"role": "tool", "content": "invalid", "tool_call_id": "1", "is_tool_result": True},
        {"role": "assistant", "content": "", "tool_calls": [call("2")]},
        {"role": "tool", "content": "ok", "tool_call_id": "2", "is_tool_result": True},
    ]

    # The turn starts after the summary; its request is still there.
    history = [summary, {"role": "user", "content": task}, *attempts]
    kept = drop_rejected_attempts(history, 1, request=task + "\n")
    assert [m["content"] for m in kept] == [summary["content"], task, "", "ok"]
    # Compaction evicted the request: the turn starts with a rejected attempt.
    kept = drop_rejected_attempts([summary, *attempts], 1, request=task)
    assert [m["content"] for m in kept] == [summary["content"], "", "ok"]
    assert [c["id"] for m in kept for c in m.get("tool_calls") or ()] == ["2"]


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


# A turn whose request is too large to keep once its tool results arrive:
# compaction in the middle of the tool loop evicts the request, while the
# earlier turn and this turn's lookups still fit.  The model then submits a
# rejected result, answers in prose, is told to submit, and submits again.
LARGE_REQUEST = "第二问" + "乙" * 5000
REJECTED_THEN_ACCEPTED = [
    ("call", [("lookup", {"text": "p1"})]),
    ("call", [("lookup", {"text": "p2"})]),
    ("call", [("lookup", {"text": "p3"})]),
    ("call", [("submit_result", {"wrong": 1})]),
    ("say", "答案大概是二"),
    ("call", [("submit_result", {"answer": "two"})]),
]


def _saved(tmp_path: Path, conversation_id: str) -> list[LLMMessage]:
    stored = FileConversationHistoryStore(tmp_path, RUN_ID).load(conversation_id)
    assert stored is not None
    return [
        LLMMessage(
            role=item["role"],
            content=item["content"],
            tool_calls=[
                LLMToolCall(call["id"], call["name"], call["arguments"])
                for call in item.get("tool_calls") or ()
            ]
            or None,
            tool_call_id=item.get("tool_call_id"),
            is_tool_result=bool(item.get("is_tool_result")),
        )
        for item in stored
    ]


def _looked_up(history: list[LLMMessage]) -> list[str]:
    return [
        call.arguments["text"]
        for message in history
        for call in message.tool_calls or ()
        if call.name == "lookup"
    ]


def _superseded(history: list[LLMMessage]) -> list[str]:
    """What is left of the turn's rejected attempts and correction prompts."""

    markers = (
        "correction_required",
        "<submission_correction>",
        "<tool_input_correction",
        "答案大概是二",
    )
    found = [
        marker
        for message in history
        for marker in markers
        if marker in (message.content or "")
    ]
    return found + ["wrong" for arguments in _submitted(history) if "wrong" in arguments]


@pytest.mark.asyncio
@pytest.mark.parametrize("compacted", [True, False])
async def test_an_accepted_resubmission_is_all_that_stays_of_its_attempts(
    tmp_path: Path, compacted: bool
) -> None:
    provider = ScriptedProvider(
        [
            ("call", [("submit_result", {"answer": "one"})]),
            *REJECTED_THEN_ACCEPTED,
            ("call", [("submit_result", {"answer": "three"})]),
        ]
    )
    harness = Harness(
        tmp_path,
        [],
        provider=provider,
        page_chars=600,
        working_memory_tokens=4096 if compacted else 1_000_000,
    )

    async def steps():
        await harness.ask("first", "第一问")
        second = await harness.ask("second", LARGE_REQUEST, tools=["lookup"])
        saved = _saved(tmp_path, harness.conversation.conversation_id)
        live = harness.history()
        summaries = provider.summary_requests
        third = await harness.ask("third", "第三问")
        return second, saved, live, summaries, third

    second, saved, live, summaries, third = await harness.run(steps)

    assert second.status == "ok"
    assert second.result == {"answer": "two"}
    # The scenario happened as described: within the turn the model was
    # told why its submission was rejected, and then told to submit.
    sent_in_turn = provider.task_requests[1:7]
    assert any("<tool_input_correction" in (m.content or "") for m in sent_in_turn[4])
    assert any("<submission_correction>" in (m.content or "") for m in sent_in_turn[5])
    if compacted:
        assert summaries >= 1
        # Compaction inside the tool loop evicted the turn's own request.
        assert not _requests(saved, "第二问")
        assert sum(SUMMARY in (m.content or "") for m in saved) == 1
    else:
        assert summaries == 0
        assert len(_requests(saved, "第二问")) == 1

    # The saved history keeps the accepted submission once, none of the
    # superseded attempts or correction prompts, and the rest: the earlier
    # turn, this turn's lookups and their results.
    assert [m.content for m in saved] == [m.content for m in live]
    assert _superseded(saved) == []
    assert _submitted(saved) == [{"answer": "one"}, {"answer": "two"}]
    assert len(_requests(saved, "第一问")) == 1
    assert _looked_up(saved) == ["p1", "p2", "p3"]
    assert sum("p3:" in (m.content or "") for m in saved if m.is_tool_result) == 1

    # The next step receives that history, then its own request.
    assert third.result == {"answer": "three"}
    received = provider.task_requests[-1]
    assert _superseded(received) == []
    assert _submitted(received) == [{"answer": "one"}, {"answer": "two"}]
    assert [m.content for m in received[1:-1]] == [m.content for m in saved]
    assert "第三问" in received[-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("compacted", [True, False])
async def test_a_rejected_submission_beside_the_accepted_one_does_not_stay(
    tmp_path: Path, compacted: bool
) -> None:
    # In the last round the model looks something up, submits a rejected
    # result, then the correction that is accepted, then once more (skipped).
    provider = ScriptedProvider(
        [
            ("call", [("submit_result", {"answer": "one"})]),
            ("call", [("lookup", {"text": "p1"})]),
            ("call", [("lookup", {"text": "p2"})]),
            ("call", [("lookup", {"text": "p3"})]),
            (
                "call",
                [
                    ("lookup", {"text": "p4"}),
                    ("submit_result", {"wrong": 1}),
                    ("submit_result", {"answer": "two"}),
                    ("submit_result", {"answer": "four"}),
                ],
            ),
            ("call", [("submit_result", {"answer": "three"})]),
        ]
    )
    harness = Harness(
        tmp_path,
        [],
        provider=provider,
        page_chars=600,
        working_memory_tokens=4096 if compacted else 1_000_000,
    )

    async def steps():
        await harness.ask("first", "第一问")
        second = await harness.ask("second", LARGE_REQUEST, tools=["lookup"])
        saved = _saved(tmp_path, harness.conversation.conversation_id)
        live = harness.history()
        summaries = provider.summary_requests
        third = await harness.ask("third", "第三问")
        return second, saved, live, summaries, third

    second, saved, live, summaries, third = await harness.run(steps)

    assert second.status == "ok"
    assert second.result == {"answer": "two"}
    if compacted:
        # Compaction inside the tool loop evicted the turn's own request.
        assert summaries >= 1
        assert not _requests(saved, "第二问")
    else:
        assert summaries == 0
        assert len(_requests(saved, "第二问")) == 1

    # Of the last round, the lookup and the accepted submission stay; the
    # rejected submission, its correction and the skipped one do not.
    skipped = "a terminal result already ended this turn"
    assert [m.content for m in saved] == [m.content for m in live]
    assert _superseded(saved) == []
    assert not any(skipped in (m.content or "") for m in saved)
    assert _submitted(saved) == [{"answer": "one"}, {"answer": "two"}]
    assert len(_requests(saved, "第一问")) == 1
    assert _looked_up(saved) == ["p1", "p2", "p3", "p4"]
    assert sum("p4:" in (m.content or "") for m in saved if m.is_tool_result) == 1

    # The next step receives that history, then its own request.
    assert third.result == {"answer": "three"}
    received = provider.task_requests[-1]
    assert _superseded(received) == []
    assert not any(skipped in (m.content or "") for m in received)
    assert _submitted(received) == [{"answer": "one"}, {"answer": "two"}]
    assert [m.content for m in received[1:-1]] == [m.content for m in saved]
    assert "第三问" in received[-1].content


# The batch limit is two calls per round; the submission comes after it.
PAST_THE_BATCH = [
    ("lookup", {"text": "p1"}),
    ("lookup", {"text": "p2"}),
    ("lookup", {"text": "p3"}),
]


@pytest.mark.asyncio
async def test_a_submission_past_the_batch_is_accepted_in_the_same_round(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [("call", [*PAST_THE_BATCH, ("submit_result", {"answer": "two"})])]
    )
    harness = Harness(
        tmp_path, [], provider=provider, page_chars=10, max_tool_calls_per_round=2
    )

    outcome = await harness.run(lambda: harness.ask("only", "问题", tools=["lookup"]))

    assert outcome.status == "ok"
    assert outcome.result == {"answer": "two"}
    assert len(provider.task_requests) == 1
    # Read-only calls past the batch still run.
    assert harness.lookups == ["p1", "p2", "p3"]


@pytest.mark.asyncio
async def test_a_rejected_submission_past_the_batch_is_corrected(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            ("call", [*PAST_THE_BATCH, ("submit_result", {"wrong": 1})]),
            ("call", [("submit_result", {"answer": "two"})]),
        ]
    )
    harness = Harness(
        tmp_path, [], provider=provider, page_chars=10, max_tool_calls_per_round=2
    )

    outcome = await harness.run(lambda: harness.ask("only", "问题", tools=["lookup"]))

    assert outcome.status == "ok"
    assert outcome.result == {"answer": "two"}
    # The submission was checked as usual and the model was told why.
    correction = [
        m.content
        for m in provider.task_requests[1]
        if "<tool_input_correction" in (m.content or "")
    ]
    assert len(correction) == 1
    assert "'answer' is a required property" in correction[0]
    history = harness.history()
    assert _submitted(history) == [{"answer": "two"}]
    assert _looked_up(history) == ["p1", "p2", "p3"]
