"""An Agent that produces several contracts gets one small, stable submit_result."""

from __future__ import annotations

from typing import Any

import pytest

from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.definitions import ContractDefinition
from manyselves.runtime.tools.submit_result import GENERIC_SCHEMA, SubmitResultTool


def _contract(contract_id: str, schema: dict[str, Any]):
    return build_contract_adapter(
        ContractDefinition(
            id=contract_id,
            version="1.0.0",
            description=contract_id,
            adapter="json_schema",
            schema=schema,
        )
    )


ITEM = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
CONTRACTS = {
    "beta": _contract(
        "beta",
        {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/Item"}}},
            "required": ["items"],
            "$defs": {"Item": ITEM},
        },
    ),
    "alpha": _contract(
        "alpha",
        {
            "type": "object",
            "properties": {"item": {"$ref": "#/$defs/Item"}},
            "required": ["item"],
            "$defs": {"Item": ITEM},
        },
    ),
}


class _Store:
    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []

    def save(self, **record: Any) -> str:
        self.saved.append(record)
        return f"results/{record['task_id']}.json"


class _Bus:
    async def publish(self, _message: Any) -> None:
        return None


def _tool(contract_id: str, store: _Store, **extra: Any) -> SubmitResultTool:
    return SubmitResultTool(
        sender="s",
        workflow_id="w",
        run_id="r",
        task_id="revise",
        task_attempt_id="a",
        session_id="sess",
        conversation_id="c",
        agent_id="agent",
        input_value={},
        output_contract_id=contract_id,
        output_contract=CONTRACTS[contract_id],
        store=store,  # type: ignore[arg-type]
        bus=_Bus(),
        generic=True,
        **extra,
    )


def test_every_task_of_the_agent_sees_the_same_small_schema() -> None:
    first = _tool("alpha", _Store())
    second = _tool("beta", _Store())
    assert first.input_schema == second.input_schema == GENERIC_SCHEMA
    assert first.description == second.description


@pytest.mark.asyncio
async def test_the_flat_result_is_validated_against_the_task_contract() -> None:
    store = _Store()
    tool = _tool("alpha", store, store_task_id="revise--revise-judgments")
    wrong = await tool(items=[])
    assert wrong["status"] == "correction_required"
    assert wrong["output_contract"] == "alpha"
    accepted = await tool(item={"x": "ok"})
    assert accepted["accepted"] is True
    assert store.saved[-1]["result"] == {"item": {"x": "ok"}}
    assert store.saved[-1]["task_id"] == "revise--revise-judgments"


@pytest.mark.asyncio
async def test_an_empty_submission_gets_a_correction() -> None:
    tool = _tool("alpha", _Store())
    result = await tool()
    assert result["status"] == "correction_required"


def test_an_agent_with_one_contract_keeps_the_plain_schema() -> None:
    tool = SubmitResultTool(
        sender="s", workflow_id="w", run_id="r", task_id="t", task_attempt_id="a",
        session_id="sess", conversation_id="c", agent_id="agent", input_value={},
        output_contract_id="alpha", output_contract=CONTRACTS["alpha"],
        store=_Store(), bus=_Bus(),  # type: ignore[arg-type]
    )
    assert tool.generic is False
    assert tool.input_schema == CONTRACTS["alpha"].json_schema()


def test_the_task_message_carries_the_task_schema_when_the_tool_is_generic() -> None:
    import json

    from manyselves.kernel.definitions import TaskDefinition
    from manyselves.runtime.definition_agent import DefinitionAgentInvoker
    from manyselves.runtime.tools.registry import ToolRegistry

    task = TaskDefinition(
        id="t", version="1.0.0", description="t", agent="agent", objective="do it",
        input_contract="alpha", output_contract="beta",
    )
    tools = ToolRegistry()
    tools.register(_tool("beta", _Store()))
    prompt = DefinitionAgentInvoker._prompt(task, {}, tools)
    schema = json.dumps(CONTRACTS["beta"].json_schema(), ensure_ascii=False, separators=(",", ":"))
    assert f'<output_schema contract="beta">\n{schema}\n</output_schema>' in prompt
    assert "output_schema 填写" in prompt
