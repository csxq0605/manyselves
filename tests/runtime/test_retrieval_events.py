"""Every result an execution takes from a record is an event with its versions.

A small neutral Capability whose counts are known beforehand:

- ``prepare`` (program step) feeds two fresh conversations the same input;
- ``ask-a`` reaches the model; ``ask-b`` has exactly the same context, so it
  replays ``ask-a``'s call record, made by the version executing now;
- ``tick`` counts to two and loops back once to ``ask-a``: on the second pass
  both asks take the completed results of this same version.

First execution (v1): one model call, one call replay from v1, two
same-version results, no program step replay.

Re-executed in place with nothing changed (v2): no model call; ``prepare`` and
both ``tick`` steps replay from the step memo (records of v1); both asks
replay v1's call record; the second pass takes v2's own results.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.runtime.generic_binding import GenericCapabilityRuntimeBinding
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.retrievals import (
    MODEL_CALL_REPLAY,
    PROGRAM_STEP_REPLAY,
    RESULT_RETRIEVED,
    SAME_VERSION_RESULT,
)
from manyselves.runtime.services import RuntimeServicesView
from tests.runtime.scripted_loop import ScriptedLoop

COMMAND = UUID("70000000-0000-4000-8000-000000000001")
FILES = {
    "capability.yaml": """
id: counted
version: 1.0.0
description: A flow whose retrievals are counted
agents: ./agents
workflows: ./workflows
tasks: ./tasks
contracts: ./contracts
tools: ./tools
libraries:
- {standard}
entrypoints:
- counted
""",
    "agents/answerer.md": """---
id: answerer
version: 1.0.0
description: Answers one question
model: inherit
tools: []
accepts:
- question
produces:
- answer
conversation_mode: run
limits: {{}}
---
You answer questions.
""",
    "tasks/ask.yaml": """
id: ask
version: 1.0.0
description: Answer the question
agent: answerer
objective: Answer the question.
input_contract: question
output_contract: answer
tools: []
recovery: typed-turn-recovery
""",
    "contracts/question.yaml": """
id: question
version: 1.0.0
description: A question
adapter: json_schema
schema:
  type: object
  required: [text]
  properties:
    text: {{type: string}}
""",
    "contracts/answer.yaml": """
id: answer
version: 1.0.0
description: An answer
adapter: json_schema
schema:
  type: object
  required: [answer]
  properties:
    answer: {{type: string}}
  additionalProperties: false
""",
    "contracts/count.yaml": """
id: count
version: 1.0.0
description: A count
adapter: json_schema
schema:
  type: integer
""",
    "tools/prepare.yaml": """
id: prepare
version: 1.0.0
description: Prepare the question
implementation: capability:counted:prepare
input_contract: question
output_contract: question
side_effect: pure_read
model_visible: false
""",
    "tools/tick.yaml": """
id: tick
version: 1.0.0
description: Count one more
implementation: capability:counted:tick
input_contract: count
output_contract: count
side_effect: pure_read
model_visible: false
""",
    "workflows/counted.yaml": """
id: counted
version: 1.0.0
description: Ask twice in two conversations, then once more
tasks: [ask]
recovery: [typed-turn-recovery]
input_contract: question
input_variable: question
max_iterations: 40
state:
  count: 0
actions:
- id: prepare
  kind: invoke_tool
  tool: prepare
  input_variable: question
  output_variable: prepared
- id: open-a
  kind: create_conversation
  agent: answerer
  conversation_key: a
  mode: run
  output_variable: conversation-a
- id: open-b
  kind: create_conversation
  agent: answerer
  conversation_key: b
  mode: run
  output_variable: conversation-b
- id: ask-a
  kind: invoke_agent
  agent: answerer
  task: ask
  conversation_variable: conversation-a
  input_variable: prepared
  output_variable: answer-a
- id: ask-b
  kind: invoke_agent
  agent: answerer
  task: ask
  conversation_variable: conversation-b
  input_variable: prepared
  output_variable: answer-b
- id: tick
  kind: invoke_tool
  tool: tick
  input_variable: count
  output_variable: count
- id: check
  kind: if
  condition:
    variable: count
    operator: gte
    value: 2
  then: finish
  otherwise: ask-a
- id: finish
  kind: end_workflow
  output_variable: answer-a
  output_name: result
""",
}


def _capability(root: Path, files: dict[str, str] = FILES) -> Path:
    standard = Path(__file__).resolve().parents[2] / "manyselves/definitions/standard"
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.format(standard=standard).lstrip(), encoding="utf-8")
    return root / "capability.yaml"


async def _execute(
    tmp_path: Path, operation, files: dict[str, str] = FILES
) -> tuple[GenericCapabilityRuntimeBinding, int]:
    """Run ``operation(binding)`` on a fresh binding; return the model turns."""

    bus = MessageBus()
    received: list[Any] = []

    def answer(message: Any) -> Any:
        received.append(message)
        return ("submit", [{"answer": "yes"}])

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    binding = GenericCapabilityRuntimeBinding(
        capability_file=_capability(tmp_path / "capability", files),
        workspace=workspace,
        services=RuntimeServicesView(
            workspace=workspace,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        tool_implementations={
            "prepare": lambda question: dict(question),
            "tick": lambda count: count + 1,
        },
        loop_builder=lambda **kwargs: ScriptedLoop(bus, answer, kwargs),
    )
    bus_task = asyncio.create_task(bus.process_queue())
    try:
        await operation(binding)
    finally:
        await binding.close()
        bus.shutdown()
        await bus_task
    return binding, len(received)


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _retrievals(path: Path) -> list[dict[str, Any]]:
    return [event for event in _events(path) if event["kind"] == RESULT_RETRIEVED]


def _counts(events: list[dict[str, Any]]) -> Counter:
    return Counter(
        (event["data"]["rule"], event["data"]["version"], event["data"]["source_version"])
        for event in events
    )


@pytest.mark.asyncio
async def test_retrieval_events_match_the_known_counts_of_a_flow(tmp_path: Path) -> None:
    binding, first_calls = await _execute(
        tmp_path, lambda binding: binding.start(COMMAND, "counted", {"text": "q"})
    )
    run_id = binding.run_id_for(COMMAND, "counted")
    run_dir = tmp_path / "workspace/Work/runs" / run_id
    assert binding.get_run(run_id)["run"]["status"] == "completed"

    _binding, second_calls = await _execute(
        tmp_path, lambda binding: binding.rerun(COMMAND, run_id)
    )

    assert first_calls == 1 and second_calls == 0
    v1 = _retrievals(run_dir / "versions/v1/workflow-events.jsonl")
    assert _counts(v1) == Counter(
        {
            (MODEL_CALL_REPLAY, 1, 1): 1,
            (SAME_VERSION_RESULT, 1, 1): 2,
        }
    )
    assert [event["action_id"] for event in v1] == ["ask-b", "ask-a", "ask-b"]
    v2 = _retrievals(run_dir / "workflow-events.jsonl")
    assert _counts(v2) == Counter(
        {
            (PROGRAM_STEP_REPLAY, 2, 1): 3,
            (MODEL_CALL_REPLAY, 2, 1): 2,
            (SAME_VERSION_RESULT, 2, 2): 2,
        }
    )
    assert [event["action_id"] for event in v2] == [
        "prepare", "ask-a", "ask-b", "tick", "ask-a", "ask-b", "tick"
    ]
    # The call record and its hits say which version made and which replayed it.
    hits = [
        json.loads(line)
        for line in (run_dir / "agent-memo/hits.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(hit["version"], hit["source_version"]) for hit in hits] == [(1, 1), (2, 1), (2, 1)]
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (run_dir / "agent-memo").glob("*.json")
    ]
    assert [record["version"] for record in records] == [1]


# The same steps, with ``prepare`` both in the Root and in a subworkflow
# ``inner`` that asks once.  Both workflows have a step named ``prepare``.
NESTED_FILES = {
    **{name: text for name, text in FILES.items() if name != "workflows/counted.yaml"},
    "workflows/counted.yaml": """
id: counted
version: 1.0.0
description: Prepare, then ask inside a subworkflow
tasks: [ask]
recovery: [typed-turn-recovery]
input_contract: question
input_variable: question
max_iterations: 40
actions:
- id: prepare
  kind: invoke_tool
  tool: prepare
  input_variable: question
  output_variable: prepared
- id: inner
  kind: subworkflow
  workflow: inner
  input_variable: prepared
  child_input_variable: question
  child_output_name: result
  output_variable: answer
- id: finish
  kind: end_workflow
  output_variable: answer
  output_name: result
""",
    "workflows/inner.yaml": """
id: inner
version: 1.0.0
description: Prepare again and ask once
tasks: [ask]
recovery: [typed-turn-recovery]
input_contract: question
input_variable: question
output_contract: answer
actions:
- id: prepare
  kind: invoke_tool
  tool: prepare
  input_variable: question
  output_variable: prepared
- id: open
  kind: create_conversation
  agent: answerer
  conversation_key: inner
  mode: run
  output_variable: conversation
- id: ask
  kind: invoke_agent
  agent: answerer
  task: ask
  conversation_variable: conversation
  input_variable: prepared
  output_variable: answer
- id: finish
  kind: end_workflow
  output_variable: answer
  output_name: result
""",
}


@pytest.mark.asyncio
async def test_a_retrieval_names_the_workflow_its_step_belongs_to(tmp_path: Path) -> None:
    """``(workflow_id, action_id)`` of a retrieval is that of its step's own events."""

    binding, first_calls = await _execute(
        tmp_path,
        lambda binding: binding.start(COMMAND, "counted", {"text": "q"}),
        NESTED_FILES,
    )
    run_id = binding.run_id_for(COMMAND, "counted")
    assert binding.get_run(run_id)["run"]["status"] == "completed"

    _binding, second_calls = await _execute(
        tmp_path, lambda binding: binding.rerun(COMMAND, run_id), NESTED_FILES
    )

    assert first_calls == 1 and second_calls == 0
    events = _events(tmp_path / "workspace/Work/runs" / run_id / "workflow-events.jsonl")
    retrieved = [
        (event["workflow_id"], event["action_id"], event["data"]["rule"])
        for event in events
        if event["kind"] == RESULT_RETRIEVED
    ]
    assert retrieved == [
        ("counted", "prepare", PROGRAM_STEP_REPLAY),
        ("inner", "prepare", PROGRAM_STEP_REPLAY),
        ("inner", "ask", MODEL_CALL_REPLAY),
    ]
    started = {
        (event["workflow_id"], event["action_id"])
        for event in events
        if event["kind"] == "action.started"
    }
    assert {(workflow, action) for workflow, action, _rule in retrieved} <= started
