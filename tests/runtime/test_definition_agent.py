"""The generic definition-driven Agent invoker and its recovery semantics."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.conversations import (
    ConversationKey,
    ConversationMode,
    ConversationRecord,
)
from manyselves.kernel.definitions import (
    AgentDefinition,
    ContractDefinition,
    DefinitionRegistry,
    RecoveryPolicyDefinition,
    RecoveryRule,
    TaskDefinition,
    ToolDefinition,
)
from manyselves.runtime.agent_execution import AgentExecutionService
from manyselves.runtime.definition_agent import DefinitionAgentInvoker
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.services import RuntimeServicesView
from manyselves.runtime.tool_adapter import CapabilityToolAdapterFactory
from tests.runtime.scripted_loop import ScriptedLoop

RUN_ID = "run-1"

POLICY = RecoveryPolicyDefinition(
    id="policy",
    version="1.0.0",
    description="Recover typed turns",
    rules={
        "natural_language_without_submission": RecoveryRule(action="correct"),
        "invalid_structured_output": RecoveryRule(action="correct"),
        "max_tokens": RecoveryRule(action="continue"),
        "tool_slice_boundary": RecoveryRule(action="continue"),
        "no_progress": RecoveryRule(action="stop"),
        "completed_tool_result": RecoveryRule(action="reuse_result"),
    },
)


def _definitions() -> tuple[DefinitionRegistry, dict[str, Any]]:
    registry = DefinitionRegistry()
    question = ContractDefinition(
        id="question",
        version="1.0.0",
        description="A question",
        adapter="json_schema",
        schema={
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
        },
    )
    answer = ContractDefinition(
        id="answer",
        version="1.0.0",
        description="An answer",
        adapter="json_schema",
        schema={
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    for contract in (question, answer):
        registry.register(contract)
    registry.register(
        ToolDefinition(
            id="lookup",
            version="1.0.0",
            description="Look up a word",
            implementation="capability:neutral:lookup",
            input_contract="question",
            output_contract="answer",
            side_effect="pure_read",
            parallel_safe=True,
            reuse_result=True,
        )
    )
    registry.register(
        AgentDefinition(
            id="answerer",
            version="1.0.0",
            description="Answers",
            instructions="You answer questions.",
            tools=["lookup"],
            accepts=["question"],
            produces=["answer"],
        )
    )
    contracts = {
        contract.id: build_contract_adapter(contract) for contract in (question, answer)
    }
    return registry, contracts


def _task(*, tools: list[str] | None = None, budget: int | None = None) -> TaskDefinition:
    return TaskDefinition(
        attention_budget_tokens=budget,
        id="answer-question",
        version="1.0.0",
        description="Answer one question",
        agent="answerer",
        objective="Answer the question in one word.",
        input_contract="question",
        output_contract="answer",
        constraints=["Be brief."],
        tools=tools or [],
        recovery="policy",
    )


def _conversation(value: str, *, forked_from: str | None = None) -> ConversationRecord:
    return ConversationRecord(
        conversation_id=f"run:{RUN_ID}:answerer:{value}",
        key=ConversationKey(agent_id="answerer", value=value, mode=ConversationMode.RUN),
        run_id=RUN_ID,
        forked_from=forked_from,
    )


class Harness:
    def __init__(self, tmp_path: Path, script: list[Any], lookups: list[Any]) -> None:
        self.bus = MessageBus()
        self.script = script
        self.loops: list[ScriptedLoop] = []
        registry, contracts = _definitions()
        self.registry = registry
        self.contracts = contracts

        def lookup(arguments: dict[str, Any]) -> dict[str, Any]:
            lookups.append(arguments)
            return {"answer": arguments["text"].upper()}

        factory = CapabilityToolAdapterFactory("neutral", {"lookup": lookup}, contracts)
        self.services = RuntimeServicesView(
            workspace=tmp_path,
            bus=self.bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        )
        self.factory = factory
        self.tmp_path = tmp_path

    def invoker(self) -> DefinitionAgentInvoker:
        def build(**kwargs: Any) -> ScriptedLoop:
            loop = ScriptedLoop(self.bus, self.script, kwargs)
            self.loops.append(loop)
            return loop

        return DefinitionAgentInvoker(
            self.services,
            workflow_id="neutral",
            run_id=RUN_ID,
            definitions=self.registry,
            contracts=self.contracts,
            tool_factory=self.factory.build,
            execution=AgentExecutionService(self.bus, timeout=2),
            loop_builder=build,
        )

    @property
    def agent(self) -> AgentDefinition:
        return self.registry.require("agent", "answerer")


async def _run(harness: Harness, steps):
    bus_task = asyncio.create_task(harness.bus.process_queue())
    try:
        return await steps()
    finally:
        harness.bus.shutdown()
        await bus_task


@pytest.mark.asyncio
async def test_submission_is_validated_and_returned(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [("submit", [{"answer": "yes"}])], [])
    invoker = harness.invoker()

    async def steps():
        try:
            return await invoker.invoke_with_recovery(
                harness.agent, _task(), {"text": "q"}, _conversation("a"),
                task_id="ask", recovery_policy=POLICY,
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.status == "ok" and outcome.result == {"answer": "yes"}
    prompt = harness.loops[0].received[0].content
    assert "Answer the question in one word." in prompt
    assert '"text":"q"' in prompt and "Be brief." in prompt
    assert harness.loops[0].kwargs["system_prompt"] == "You answer questions."


@pytest.mark.asyncio
async def test_schema_error_is_corrected_in_the_same_conversation(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path, [("submit", [{"wrong": 1}, {"answer": "fixed"}])], []
    )
    invoker = harness.invoker()

    async def steps():
        try:
            return await invoker.invoke_with_recovery(
                harness.agent, _task(), {"text": "q"}, _conversation("a"),
                task_id="ask", recovery_policy=POLICY,
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.result == {"answer": "fixed"}
    assert harness.loops[0].feedback[0]["status"] == "correction_required"
    assert len(harness.loops) == 1 and len(harness.loops[0].received) == 1


@pytest.mark.asyncio
async def test_repeated_schema_error_fails_without_guessing(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [("submit", [{"wrong": 1}, {"wrong": 1}])], [])
    invoker = harness.invoker()

    async def steps():
        try:
            return await invoker.invoke_with_recovery(
                harness.agent, _task(), {"text": "q"}, _conversation("a"),
                task_id="ask", recovery_policy=POLICY,
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.status != "ok"
    assert "same validation error was repeated" in (outcome.error or "")


@pytest.mark.asyncio
async def test_natural_language_gets_one_correction_turn(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path, [("say", "I think yes."), ("submit", [{"answer": "yes"}])], []
    )
    invoker = harness.invoker()

    async def steps():
        try:
            return await invoker.invoke_with_recovery(
                harness.agent, _task(), {"text": "q"}, _conversation("a"),
                task_id="ask", recovery_policy=POLICY,
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.result == {"answer": "yes"}
    received = harness.loops[0].received
    assert received[1].turn_kind == "submission_correction"
    assert "<submission_correction>" in received[1].content


@pytest.mark.asyncio
async def test_max_tokens_and_tool_slice_continue_while_progressing(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [
            ("max_tokens", "part one"),
            ("tool_slice", "part two"),
            ("submit", [{"answer": "done"}]),
        ],
        [],
    )
    invoker = harness.invoker()

    async def steps():
        try:
            return await invoker.invoke_with_recovery(
                harness.agent, _task(), {"text": "q"}, _conversation("a"),
                task_id="ask", recovery_policy=POLICY,
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.result == {"answer": "done"}
    kinds = [message.turn_kind for message in harness.loops[0].received]
    assert kinds == ["task_initial", "max_tokens_continuation", "tool_slice_continuation"]


@pytest.mark.asyncio
async def test_continuation_without_progress_stops(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [("max_tokens", "same"), ("max_tokens", "same")],
        [],
    )
    invoker = harness.invoker()

    async def steps():
        try:
            return await invoker.invoke_with_recovery(
                harness.agent, _task(), {"text": "q"}, _conversation("a"),
                task_id="ask", recovery_policy=POLICY,
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.status == "incomplete"
    assert "no_progress" in (outcome.error or "")


@pytest.mark.asyncio
async def test_same_run_resume_reuses_the_completed_result_for_the_same_input(
    tmp_path: Path,
) -> None:
    harness = Harness(
        tmp_path,
        [("submit", [{"answer": "first"}]), ("submit", [{"answer": "other"}])],
        [],
    )

    async def steps():
        first = harness.invoker()
        try:
            done = await first.invoke_with_recovery(
                harness.agent, _task(), {"text": "q"}, _conversation("a"),
                task_id="ask", recovery_policy=POLICY,
            )
        finally:
            await first.close()
        second = harness.invoker()
        try:
            reused = await second.invoke_with_recovery(
                harness.agent, _task(), {"text": "q"}, _conversation("a"),
                task_id="ask", recovery_policy=POLICY,
            )
            changed = await second.invoke_with_recovery(
                harness.agent, _task(), {"text": "new"}, _conversation("a"),
                task_id="ask", recovery_policy=POLICY,
            )
        finally:
            await second.close()
        return done, reused, changed

    done, reused, changed = await _run(harness, steps)

    assert done.result == reused.result == {"answer": "first"}
    assert changed.result == {"answer": "other"}
    assert len(harness.loops) == 2


@pytest.mark.asyncio
async def test_conversation_session_is_reused_and_history_survives_restart(
    tmp_path: Path,
) -> None:
    harness = Harness(
        tmp_path,
        [
            ("submit", [{"answer": "one"}]),
            ("submit", [{"answer": "two"}]),
            ("submit", [{"answer": "three"}]),
        ],
        [],
    )

    async def steps():
        invoker = harness.invoker()
        try:
            await invoker.invoke(
                harness.agent, _task(), {"text": "1"}, _conversation("a"), task_id="t1"
            )
            await invoker.invoke(
                harness.agent, _task(), {"text": "2"}, _conversation("a"), task_id="t2"
            )
        finally:
            await invoker.close()
        restarted = harness.invoker()
        try:
            return await restarted.invoke(
                harness.agent, _task(), {"text": "3"}, _conversation("a"), task_id="t3"
            )
        finally:
            await restarted.close()

    outcome = await _run(harness, steps)

    assert outcome.result == {"answer": "three"}
    first_session, restarted_session = harness.loops
    assert len(first_session.received) == 2
    restored = restarted_session.restored[0]
    assert sum(message.role == "user" for message in restored) == 2


@pytest.mark.asyncio
async def test_fork_starts_from_the_source_history(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [("submit", [{"answer": "shared"}]), ("submit", [{"answer": "forked"}])],
        [],
    )
    source = _conversation("shared")
    fork = _conversation("shared:item-1", forked_from=source.conversation_id)

    async def steps():
        invoker = harness.invoker()
        try:
            await invoker.invoke(
                harness.agent, _task(), {"text": "context"}, source, task_id="read"
            )
            return await invoker.invoke(
                harness.agent, _task(), {"text": "item"}, fork, task_id="judge"
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.result == {"answer": "forked"}
    source_loop, fork_loop = harness.loops
    assert fork_loop.restored[0][0].content == source_loop.history[0].content
    assert '"text":"context"' in fork_loop.restored[0][0].content


@pytest.mark.asyncio
async def test_model_visible_tool_results_are_reused_in_the_same_run(
    tmp_path: Path,
) -> None:
    lookups: list[Any] = []
    harness = Harness(
        tmp_path,
        [
            ("tool_then_submit", {"text": "w"}, {"answer": "a"}),
            ("tool_then_submit", {"text": "w"}, {"answer": "b"}),
        ],
        lookups,
    )
    from manyselves.runtime.tools.result_memory import RunToolResultIndex

    harness.factory = CapabilityToolAdapterFactory(
        "neutral",
        {"lookup": lambda arguments: lookups.append(arguments) or {"answer": "W"}},
        harness.contracts,
        RunToolResultIndex(tmp_path, RUN_ID),
    )

    async def steps():
        invoker = harness.invoker()
        try:
            first = await invoker.invoke(
                harness.agent, _task(tools=["lookup"]), {"text": "1"},
                _conversation("a"), task_id="t",
            )
            second = await invoker.invoke(
                harness.agent, _task(tools=["lookup"]), {"text": "2"},
                _conversation("a"), task_id="t",
            )
        finally:
            await invoker.close()
        return first, second

    first, second = await _run(harness, steps)

    assert first.status == second.status == "ok"
    assert lookups == [{"text": "w"}]
    assert harness.loops[0].feedback == [{"answer": "W"}, {"answer": "W"}]


@pytest.mark.asyncio
async def test_context_fields_are_sent_as_text_ahead_of_the_task(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [("submit", [{"answer": "yes"}])], [])
    invoker = harness.invoker()
    task = _task().model_copy(update={"context_fields": ["background"]})

    async def steps():
        try:
            return await invoker.invoke_with_recovery(
                harness.agent,
                task,
                {"text": "q", "background": "line one\nline two"},
                _conversation("a"),
                task_id="ask",
                recovery_policy=POLICY,
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.status == "ok"
    prompt = harness.loops[0].received[0].content
    assert prompt.startswith('<context name="background">\nline one\nline two\n</context>')
    assert prompt.index("</context>") < prompt.index('<task id="answer-question">')
    assert "background" not in prompt[prompt.index("<input"):]


@pytest.mark.asyncio
async def test_a_batch_task_sends_the_shared_part_first_and_what_varies_last(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, [("submit", [{"answer": "yes"}])], [])
    invoker = harness.invoker()
    task = _task().model_copy(update={
        "context_fields": ["own", "common"],
        "shared_fields": ["common"],
        "static_first": True,
    })

    async def steps():
        try:
            return await invoker.invoke_with_recovery(
                harness.agent,
                task,
                {"text": "q", "own": "mine", "common": "everyone's"},
                _conversation("a"),
                task_id="ask",
                recovery_policy=POLICY,
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.status == "ok"
    prompt = harness.loops[0].received[0].content
    order = [
        prompt.index('<context name="common">'),
        prompt.index('<task id="answer-question">'),
        prompt.index('<context name="own">'),
        prompt.index("<input"),
    ]
    assert order == sorted(order)
    assert '"text":"q"' in prompt[prompt.index("<input"):]


@pytest.mark.asyncio
async def test_declared_image_is_sent_with_the_task_message(tmp_path: Path) -> None:
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos/a.png").write_bytes(b"png")
    harness = Harness(tmp_path, [("submit", [{"answer": "yes"}])], [])
    invoker = harness.invoker()
    task = TaskDefinition.model_validate(
        {
            **_task().model_dump(),
            "image": {"path_field": "photo", "media_type_field": "media"},
        }
    )

    async def steps():
        try:
            return await invoker.invoke_with_recovery(
                harness.agent,
                task,
                {"text": "q", "photo": "photos/a.png", "media": "image/png"},
                _conversation("a"),
                task_id="ask",
                recovery_policy=POLICY,
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.status == "ok"
    assert harness.loops[0].received[0].images == [
        {"path": str((tmp_path / "photos/a.png").resolve()), "media_type": "image/png"}
    ]


@pytest.mark.asyncio
async def test_image_outside_the_workspace_fails_the_task(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [("submit", [{"answer": "yes"}])], [])
    invoker = harness.invoker()
    task = TaskDefinition.model_validate(
        {
            **_task().model_dump(),
            "image": {"path_field": "photo", "media_type_field": "media"},
        }
    )

    async def steps():
        try:
            return await invoker.invoke_with_recovery(
                harness.agent,
                task,
                {"text": "q", "photo": "../x.png", "media": "image/png"},
                _conversation("a"),
                task_id="ask",
                recovery_policy=POLICY,
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.status == "failed" and "not a workspace file" in outcome.error


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [["a.png", "b.jpg"], []])
async def test_listed_images_are_all_sent_and_an_empty_list_sends_none(
    tmp_path: Path, names: list[str]
) -> None:
    (tmp_path / "photos").mkdir()
    for name in names:
        (tmp_path / "photos" / name).write_bytes(b"img")
    harness = Harness(tmp_path, [("submit", [{"answer": "yes"}])], [])
    invoker = harness.invoker()
    task = TaskDefinition.model_validate(
        {
            **_task().model_dump(),
            "image": {"list_field": "photos", "path_field": "path", "media_type_field": "media"},
        }
    )
    listed = [{"path": f"photos/{name}", "media": "image/png"} for name in names]

    async def steps():
        try:
            return await invoker.invoke_with_recovery(
                harness.agent,
                task,
                {"text": "q", "photos": listed},
                _conversation("a"),
                task_id="ask",
                recovery_policy=POLICY,
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.status == "ok"
    assert [image["path"] for image in harness.loops[0].received[0].images or []] == [
        str((tmp_path / "photos" / name).resolve()) for name in names
    ]


def _archive(tmp_path: Path, name: str) -> None:
    """Move results and histories aside, as an in-place re-execution does."""

    run_dir = tmp_path / "Work" / "runs" / RUN_ID
    target = run_dir / "versions" / name
    target.mkdir(parents=True)
    for part in ("agent-results", "conversations"):
        if (run_dir / part).exists():
            (run_dir / part).rename(target / part)


@pytest.mark.asyncio
async def test_re_execution_replays_unchanged_turns_and_reruns_changed_ones(
    tmp_path: Path,
) -> None:
    harness = Harness(
        tmp_path,
        [
            ("submit", [{"answer": "shared"}]),
            ("submit", [{"answer": "forked"}]),
            ("submit", [{"answer": "forked again"}]),
        ],
        [],
    )
    source = _conversation("shared")
    fork = _conversation("shared:item-1", forked_from=source.conversation_id)
    task = _task()

    async def chain(item: str):
        invoker = harness.invoker()
        try:
            shared = await invoker.invoke(
                harness.agent, task, {"text": "context"}, source, task_id="read"
            )
            judged = await invoker.invoke(
                harness.agent, task, {"text": item}, fork, task_id="judge"
            )
            return shared, judged
        finally:
            await invoker.close()

    async def steps():
        first = await chain("item")
        _archive(tmp_path, "v1")
        same = await chain("item")
        loops_after_same = len(harness.loops)
        _archive(tmp_path, "v2")
        changed = await chain("changed item")
        return first, same, loops_after_same, changed

    first, same, loops_after_same, changed = await _run(harness, steps)

    assert [outcome.result for outcome in first] == [
        {"answer": "shared"},
        {"answer": "forked"},
    ]
    # Nothing changed: no model session at all, same results.
    assert loops_after_same == 2
    assert [outcome.result for outcome in same] == [o.result for o in first]
    # Only the changed turn reached the model; it continued the replayed source.
    assert changed[0].result == {"answer": "shared"}
    assert changed[1].result == {"answer": "forked again"}
    assert len(harness.loops) == 3
    restored = harness.loops[-1].restored[0]
    assert '"text":"context"' in restored[0].content
    history = tmp_path / "Work" / "runs" / RUN_ID / "conversations"
    assert len(list(history.glob("*.json"))) == 2


@pytest.mark.asyncio
async def test_a_rejected_submission_leaves_the_history_once_accepted(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path, [("submit", [{"wrong": 1}, {"answer": "fixed"}])], []
    )
    invoker = harness.invoker()

    async def steps():
        try:
            return await invoker.invoke_with_recovery(
                harness.agent, _task(), {"text": "q"}, _conversation("a"),
                task_id="ask", recovery_policy=POLICY,
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.result == {"answer": "fixed"}
    history = harness.loops[0].history
    submitted = [m.tool_calls[0].arguments for m in history if m.tool_calls]
    assert submitted == [{"answer": "fixed"}]
    assert [m.role for m in history] == ["user", "assistant"]


def test_rejected_attempts_are_dropped_but_other_tools_stay() -> None:
    from manyselves.runtime.conversation_history import drop_rejected_attempts

    def call(call_id: str, name: str) -> dict[str, Any]:
        return {"id": call_id, "name": name, "arguments": {}}

    earlier = [{"role": "user", "content": "old"}]
    turn = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": [call("1", "lookup")]},
        {"role": "tool", "content": "found", "tool_call_id": "1", "is_tool_result": True},
        {"role": "assistant", "content": "", "tool_calls": [call("2", "submit_result")]},
        {"role": "tool", "content": "invalid", "tool_call_id": "2", "is_tool_result": True},
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": "I think"},
        {"role": "assistant", "content": "", "tool_calls": [call("3", "submit_result")]},
        {"role": "tool", "content": "ok", "tool_call_id": "3", "is_tool_result": True},
    ]
    kept = drop_rejected_attempts(earlier + turn, 1)
    assert [m["content"] for m in kept] == ["old", "task", "", "found", "", "ok"]
    assert [c["id"] for m in kept for c in m.get("tool_calls") or ()] == ["1", "3"]


@pytest.mark.asyncio
async def test_a_request_over_its_budget_is_still_sent_and_noted(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [("submit", [{"answer": "yes"}])], [])
    invoker = harness.invoker()

    async def steps():
        try:
            return await invoker.invoke_with_recovery(
                harness.agent, _task(budget=50), {"text": "问" * 200},
                _conversation("a"), task_id="ask", recovery_policy=POLICY,
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.status == "ok"
    notes = (tmp_path / f"Work/runs/{RUN_ID}/budget-notes.jsonl").read_text(encoding="utf-8")
    assert '"sent_over_budget"' in notes


@pytest.mark.asyncio
async def test_a_turn_over_budget_with_history_starts_afresh(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [("submit", [{"answer": "one"}]), ("submit", [{"answer": "two"}])],
        [],
    )
    invoker = harness.invoker()
    conversation = _conversation("a")

    async def steps():
        try:
            await invoker.invoke_with_recovery(
                harness.agent, _task(), {"text": "问" * 400},
                conversation, task_id="first", recovery_policy=POLICY,
            )
            return await invoker.invoke_with_recovery(
                harness.agent, _task(budget=300), {"text": "第二问"},
                conversation, task_id="second", recovery_policy=POLICY,
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.status == "ok" and outcome.result == {"answer": "two"}
    contents = [m.content for m in harness.loops[0].history if m.role == "user"]
    assert all("问" * 400 not in (c or "") for c in contents)
    notes = (tmp_path / f"Work/runs/{RUN_ID}/budget-notes.jsonl").read_text(encoding="utf-8")
    assert '"fresh_conversation"' in notes


@pytest.mark.asyncio
async def test_only_a_request_over_the_model_window_fails(tmp_path: Path) -> None:
    from types import SimpleNamespace

    harness = Harness(tmp_path, [("submit", [{"answer": "yes"}])], [])
    harness.services = RuntimeServicesView(
        workspace=tmp_path,
        bus=harness.bus,
        active_provider=SimpleNamespace(context_window=80),
        agent_defaults=AgentDefaults(),
        global_knowledge_root=None,
    )
    invoker = harness.invoker()

    async def steps():
        try:
            return await invoker.invoke_with_recovery(
                harness.agent, _task(budget=50), {"text": "问" * 200},
                _conversation("a"), task_id="ask", recovery_policy=POLICY,
            )
        finally:
            await invoker.close()

    outcome = await _run(harness, steps)

    assert outcome.status == "failed"
    assert "model's window" in (outcome.error or "")
    assert not harness.loops or not harness.loops[0].received


def test_model_identity_tells_service_address_and_parameters_apart() -> None:
    """Same class and model name on another service or settings is another model."""

    from types import SimpleNamespace

    from manyselves.config.schema import AgentDefaults
    from manyselves.runtime.definition_agent import DefinitionAgentInvoker

    class Provider:
        def __init__(self, api_base, config_id="p1", max_output_tokens=None):
            self.model = "same-model"
            self.api_base = api_base
            self.config_id = config_id
            self.max_output_tokens = max_output_tokens
            self.api_key = "never-part-of-identity"

    def identity(provider, defaults=None):
        services = SimpleNamespace(
            active_provider=provider, agent_defaults=defaults or AgentDefaults()
        )
        return DefinitionAgentInvoker._model_identity(SimpleNamespace(services=services))

    base = identity(Provider("https://a.example/v1"))
    assert base == identity(Provider("https://a.example/v1"))
    assert base.startswith("Provider:same-model@")
    assert "a.example" not in base
    assert base != identity(Provider("https://b.example/v1"))
    assert base != identity(Provider("https://a.example/v1", config_id="p2"))
    assert base != identity(Provider("https://a.example/v1", max_output_tokens=4096))
    assert base != identity(
        Provider("https://a.example/v1"), AgentDefaults(temperature=0.7)
    )


@pytest.mark.asyncio
async def test_a_reply_is_recorded_for_the_model_that_answered_it(tmp_path: Path) -> None:
    """Changing the model while a request runs must not relabel its reply."""

    from types import SimpleNamespace

    from manyselves.runtime.agent_memo import FileAgentTurnMemo

    first = SimpleNamespace(model="model-a", api_base="https://a.example/v1")
    second = SimpleNamespace(model="model-b", api_base="https://b.example/v1")
    current = {"provider": first}

    def answer(message):
        # The user switches to B while A's request is still running.
        current["provider"] = second
        return ("submit", [{"answer": "from A"}])

    def redo(message):
        return ("submit", [{"answer": "from B"}])

    script: list[Any] = []
    harness = Harness(tmp_path, lambda message: script[0](message), [])
    harness.services = RuntimeServicesView(
        workspace=tmp_path,
        bus=harness.bus,
        active_provider=None,
        agent_defaults=AgentDefaults(),
        global_knowledge_root=None,
        resolve_provider=lambda: current["provider"],
    )

    async def ask():
        invoker = harness.invoker()
        try:
            return await invoker.invoke(
                harness.agent, _task(), {"text": "q"}, _conversation("a"), task_id="ask"
            )
        finally:
            await invoker.close()

    def recorded_models() -> list[str]:
        memo = FileAgentTurnMemo(tmp_path, RUN_ID)
        return [
            json.loads(path.read_text(encoding="utf-8"))["model"]
            for path in memo.root.glob("*.json")
            if path.name != "model-reuse.json"
        ]

    async def steps():
        script.append(answer)
        answered = await ask()
        models.extend(recorded_models())
        _archive(tmp_path, "v1")
        FileAgentTurnMemo(tmp_path, RUN_ID).set_current_model_only(True)
        script[0] = redo
        recomputed = await ask()
        return answered, recomputed

    models: list[str] = []
    answered, recomputed = await _run(harness, steps)

    assert answered.result == {"answer": "from A"}
    assert models and all(":model-a@" in model for model in models)
    # Recomputing with B redoes the turn A answered.
    assert recomputed.result == {"answer": "from B"}
