"""Request limits come from the model's declared capability, not fixed caps."""

import asyncio

from manyselves.config.schema import AgentDefaults, ApiConfig
from manyselves.runtime.context_budget import (
    request_output_limit,
    retained_input_budget_for,
    working_memory_limit,
)
from manyselves.runtime.loops.agent_loop import _SAFETY_BUFFER, AgentLoop
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.base import LLMProvider, LLMResponse
from manyselves.runtime.providers.factory import ProviderFactory
from manyselves.runtime.tools.registry import ToolRegistry
from manyselves.runtime.usage_ledger import UsageLedger

WINDOW = 1_000_000
MAX_OUTPUT = 384_000


def _declared_provider() -> LLMProvider:
    config = ApiConfig(
        provider="anthropic", api_key="key", default_model="model",
        context_window=WINDOW, max_output_tokens=MAX_OUTPUT,
    )
    return ProviderFactory.create_provider(
        config.provider, config.api_key, config.api_base, config.default_model,
        context_window=config.context_window,
        max_output_tokens=config.max_output_tokens,
    )


def test_declared_model_limits_set_every_request_limit() -> None:
    provider = _declared_provider()
    defaults = AgentDefaults()
    room = WINDOW - MAX_OUTPUT - _SAFETY_BUFFER

    assert (provider.context_window, provider.max_output_tokens) == (WINDOW, MAX_OUTPUT)
    assert request_output_limit(defaults, provider) == MAX_OUTPUT
    # History compacts only when it would no longer fit next to the output.
    assert working_memory_limit(defaults, provider) == room
    # A retained input is split only when it alone would not fit.
    assert retained_input_budget_for(defaults, provider) == room


def test_explicit_settings_cap_below_the_model() -> None:
    provider = _declared_provider()
    defaults = AgentDefaults(max_tokens=16_384, working_memory_tokens=72_000)

    assert request_output_limit(defaults, provider) == 16_384
    assert working_memory_limit(defaults, provider) == 72_000
    assert retained_input_budget_for(defaults, provider) == 72_000


class _ThinkingProvider(LLMProvider):
    """Answers with thinking and text, recording each request's output limit."""

    def __init__(self) -> None:
        super().__init__("key", model="thinking-model")
        self.context_window = WINDOW
        self.max_output_tokens = MAX_OUTPUT
        self.limits: list[int] = []

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        self.limits.append(max_tokens)
        return LLMResponse(content="done", thinking="想" * 300, stop_reason="end_turn")


async def test_requests_use_the_model_output_and_record_thinking(tmp_path) -> None:
    from manyselves.runtime.agent_execution import AgentExecutionService, AgentTurnRequest

    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    provider = _ThinkingProvider()
    service = AgentExecutionService(bus)
    try:
        session = await service.start_or_restore(
            workflow_id="wf", conversation_key="conv", runtime_id="writer",
            session_id="session-writer",
            session_factory=lambda: AgentLoop(
                agent_type="writer", workspace=tmp_path, tools=ToolRegistry(), bus=bus,
                config=AgentDefaults(), llm_provider=provider,
            ),
        )
        await asyncio.wait_for(service.dispatch_turn(session, AgentTurnRequest(
            content="写", message_id="m1", workflow_id="wf", run_id="run",
            task_id="task", task_attempt_id="attempt",
        )), timeout=10)
    finally:
        await service.close_workflow("wf")
        bus.shutdown()
        await bus_task

    assert provider.limits and set(provider.limits) == {MAX_OUTPUT}
    rows = [
        row
        for path in (tmp_path / ".manyselves" / "usage").glob("*.jsonl")
        for row in UsageLedger(tmp_path, path.stem).rows()
        if row.get("provider_request_sent")
    ]
    assert rows
    row = rows[0]
    assert row["output_limit"] == MAX_OUTPUT
    assert row["stop_reason"] == "end_turn"
    assert row["response_text_chars"] == len("done")
    assert row["response_thinking_chars"] == 300
    assert row["response_thinking_tokens_estimate"] > 0
