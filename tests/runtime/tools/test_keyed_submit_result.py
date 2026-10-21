"""An Agent that produces several contracts gets one stable submit_result schema."""

from __future__ import annotations

from typing import Any

import pytest

from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.definitions import ContractDefinition
from manyselves.runtime.tools.submit_result import SubmitResultTool, keyed_schema


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
        all_contracts=CONTRACTS,
        **extra,
    )


def test_every_task_of_the_agent_sees_the_same_schema_with_separate_definitions() -> None:
    first = _tool("alpha", _Store()).input_schema
    second = _tool("beta", _Store()).input_schema
    assert first == second == keyed_schema(CONTRACTS)
    assert list(first["properties"]) == ["alpha", "beta"]
    assert set(first["$defs"]) == {"alpha__Item", "beta__Item"}
    assert first["properties"]["alpha"]["properties"]["item"]["$ref"] == "#/$defs/alpha__Item"
    assert "$defs" not in first["properties"]["beta"]


@pytest.mark.asyncio
async def test_a_task_submits_only_under_its_own_contract_name() -> None:
    store = _Store()
    tool = _tool("alpha", store, store_task_id="revise--revise-judgments")
    wrong = await tool(beta={"items": []})
    assert wrong["status"] == "correction_required"
    assert "alpha" in wrong["error"] and "beta" in wrong["error"]
    accepted = await tool(alpha={"item": {"x": "ok"}})
    assert accepted["accepted"] is True
    assert store.saved[-1]["result"] == {"item": {"x": "ok"}}
    assert store.saved[-1]["task_id"] == "revise--revise-judgments"


def test_an_agent_with_one_contract_keeps_the_plain_schema() -> None:
    tool = SubmitResultTool(
        sender="s", workflow_id="w", run_id="r", task_id="t", task_attempt_id="a",
        session_id="sess", conversation_id="c", agent_id="agent", input_value={},
        output_contract_id="alpha", output_contract=CONTRACTS["alpha"],
        store=_Store(), bus=_Bus(),  # type: ignore[arg-type]
        all_contracts={"alpha": CONTRACTS["alpha"]},
    )
    assert tool.keyed is False
    assert tool.input_schema == CONTRACTS["alpha"].json_schema()
