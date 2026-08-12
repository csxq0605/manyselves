from __future__ import annotations

from pathlib import Path

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.core.loops.agent_loop import (
    CONTEXT_BUDGET_EXHAUSTED,
    AgentLoop,
    pre_send_context_gate,
    _trim_reporting_context_to_budget,
)
from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import (
    LLMProvider,
    LLMResponse,
    LLMToolCall,
    Message as LLMMessage,
)
from manyselves.core.tools.registry import ToolRegistry
from manyselves.core.usage_ledger import UsageLedger


class _CountingProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__("key", model="fake-gate")
        self.calls = 0

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        self.calls += 1
        return LLMResponse(content="ok")


class _OneRebuild:
    def __init__(self) -> None:
        self.calls = 0

    def rebuild(self, messages, tool_definitions=None, **kwargs):
        self.calls += 1
        return list(messages), list(tool_definitions or ())


def test_gate_blocks_exact_duplicate_but_allows_semantic_correction() -> None:
    message = LLMMessage(role="user", content="same request")
    blocked = pre_send_context_gate(
        [message], previous_messages=[message], phase="initial"
    )
    assert blocked.status == "blocked"
    assert blocked.reason == "duplicate_provider_context"

    allowed = pre_send_context_gate(
        [message], previous_messages=[message], phase="semantic_correction"
    )
    assert allowed.status == "allow"


def test_gate_retains_one_complete_tool_unit_for_typed_continuation() -> None:
    call = LLMToolCall(id="read-1", name="read", arguments={"path": "x"})
    prior = [
        LLMMessage(role="assistant", content="", tool_calls=[call]),
        LLMMessage(
            role="tool",
            content="result",
            tool_call_id="read-1",
            is_tool_result=True,
        ),
    ]
    current = prior + [
        LLMMessage(
            role="user",
            content=(
                "<same_identity_continuation>continue the pending typed task"
                "</same_identity_continuation>"
            ),
        )
    ]

    decision = pre_send_context_gate(
        current,
        previous_messages=prior,
        phase="initial",
    )

    assert decision.status == "allow"
    assert decision.reason == "typed_followup"
    assert decision.duplicate_tool_result_chars == len("result")


def test_gate_still_blocks_consumed_tool_unit_without_new_round() -> None:
    call = LLMToolCall(id="read-1", name="read", arguments={"path": "x"})
    prior = [
        LLMMessage(role="assistant", content="", tool_calls=[call]),
        LLMMessage(
            role="tool",
            content="result",
            tool_call_id="read-1",
            is_tool_result=True,
        ),
    ]

    decision = pre_send_context_gate(
        list(prior),
        previous_messages=prior,
        phase="initial",
    )

    assert decision.status == "blocked"
    assert decision.reason == "duplicate_complete_tool_result"


def test_gate_blocks_duplicate_tool_result_ids_even_in_continuation() -> None:
    call = LLMToolCall(id="read-1", name="read", arguments={"path": "x"})
    duplicate = [
        LLMMessage(role="assistant", content="", tool_calls=[call]),
        LLMMessage(
            role="tool",
            content="result",
            tool_call_id="read-1",
            is_tool_result=True,
        ),
        LLMMessage(
            role="tool",
            content="result again",
            tool_call_id="read-1",
            is_tool_result=True,
        ),
    ]

    decision = pre_send_context_gate(
        duplicate,
        phase="long_output_continuation",
    )

    assert decision.status == "blocked"
    assert decision.reason == "duplicate_complete_tool_result"


def test_old_continuation_marker_does_not_authorize_new_exact_replay() -> None:
    call = LLMToolCall(id="read-1", name="read", arguments={"path": "x"})
    prior = [
        LLMMessage(role="assistant", content="", tool_calls=[call]),
        LLMMessage(
            role="tool",
            content="result",
            tool_call_id="read-1",
            is_tool_result=True,
        ),
        LLMMessage(
            role="user",
            content="<same_identity_continuation>old round</same_identity_continuation>",
        ),
    ]

    decision = pre_send_context_gate(
        prior + [LLMMessage(role="user", content="repeat without a new reason")],
        previous_messages=prior,
        phase="initial",
    )

    assert decision.status == "blocked"
    assert decision.reason == "duplicate_complete_tool_result"


@pytest.mark.asyncio
async def test_duplicate_gate_stops_provider_and_records_zero_cost_row(tmp_path: Path) -> None:
    provider = _CountingProvider()
    loop = AgentLoop(
        agent_type="main",
        workspace=tmp_path,
        tools=ToolRegistry(),
        bus=MessageBus(),
        config=AgentDefaults(),
        llm_provider=provider,
    )
    messages = [LLMMessage(role="user", content="same request")]
    first = await loop._chat_with_retries(messages, None, "m-1")
    second = await loop._chat_with_retries(messages, None, "m-2")

    assert first.content == "ok"
    assert provider.calls == 1
    assert second.stop_reason == "pre_send_blocked"
    rows = UsageLedger(tmp_path, "main").rows()
    assert rows[-1]["provider_request_sent"] is False
    assert rows[-1]["attempt_kind"] == "pre_send_block"
    assert rows[-1]["input_tokens"] == 0
    assert UsageLedger(tmp_path, "main").summarize()["totals"]["provider_attempts"] == 1


@pytest.mark.asyncio
async def test_reporting_rebuilder_runs_once_per_round(tmp_path: Path) -> None:
    provider = _CountingProvider()
    rebuilder = _OneRebuild()
    loop = AgentLoop(
        agent_type="main",
        workspace=tmp_path,
        tools=ToolRegistry(),
        bus=MessageBus(),
        config=AgentDefaults(),
        llm_provider=provider,
        context_rebuilder=rebuilder,
    )
    before_caps = (loop.config.max_tokens, loop.config.max_tool_iterations)
    await loop._chat_with_retries(
        [LLMMessage(role="system", content="stable"), LLMMessage(role="user", content="task")],
        None,
        "m-1",
    )
    assert rebuilder.calls == 1
    assert provider.calls == 1
    assert (loop.config.max_tokens, loop.config.max_tool_iterations) == before_caps


def test_reporting_trim_keeps_latest_complete_tool_unit() -> None:
    call = LLMToolCall(id="c-latest", name="read", arguments={"path": "x"})
    messages = [
        LLMMessage(role="system", content="stable"),
        LLMMessage(role="user", content="old" * 1000),
        LLMMessage(role="assistant", content="", tool_calls=[call]),
        LLMMessage(
            role="tool",
            content="latest result",
            tool_call_id="c-latest",
            is_tool_result=True,
        ),
    ]
    trimmed = _trim_reporting_context_to_budget(
        messages, context_window=2048, max_output=32
    )
    assert not trimmed.exhausted
    assert any(item.tool_calls for item in trimmed.messages)
    assert any(item.tool_call_id == "c-latest" for item in trimmed.messages)


@pytest.mark.asyncio
async def test_reporting_budget_exhaustion_is_deterministic_and_offline(tmp_path: Path) -> None:
    provider = _CountingProvider()
    rebuilder = _OneRebuild()
    config = AgentDefaults(max_tokens=8192)
    provider.context_window = 4
    loop = AgentLoop(
        agent_type="main",
        workspace=tmp_path,
        tools=ToolRegistry(),
        bus=MessageBus(),
        config=config,
        llm_provider=provider,
        context_rebuilder=rebuilder,
    )
    response = await loop._chat_with_retries(
        [LLMMessage(role="system", content="stable"), LLMMessage(role="user", content="task")],
        None,
        "m-1",
    )
    assert response.content == CONTEXT_BUDGET_EXHAUSTED
    assert provider.calls == 0
