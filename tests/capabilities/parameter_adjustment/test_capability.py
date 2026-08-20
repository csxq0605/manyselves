from pathlib import Path

import pytest

from manyselves.capabilities.parameter_adjustment import (
    execute_parameter_adjustment,
    load_parameter_adjustment_capability,
)
from manyselves.kernel.definitions import DefinitionKind


def test_second_capability_is_neutral_and_loads_its_complete_definition_graph() -> None:
    capability, registry = load_parameter_adjustment_capability()

    assert capability.id == "parameter-adjustment"
    assert {definition.id for definition in registry.all(DefinitionKind.AGENT)} == {
        "parameter-adjuster"
    }
    assert {definition.id for definition in registry.all(DefinitionKind.WORKFLOW)} == {
        "parameter-adjustment"
    }
    serialized = "\n".join(
        definition.model_dump_json() for definition in registry.all()
    ).casefold()
    for reporting_term in (
        "reporting",
        "editor",
        "auditor",
        "cross",
        "chief",
        "module-2.",
    ):
        assert reporting_term not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "expected_agent_calls"),
    [(12, 0), (4, 1)],
)
async def test_second_capability_executes_tool_contract_condition_agent_and_goto(
    tmp_path: Path,
    value: int,
    expected_agent_calls: int,
) -> None:
    result = await execute_parameter_adjustment(
        workspace=tmp_path,
        run_id=f"parameter-{value}",
        values={"value": value},
    )

    assert result.state.outputs == {"result": max(value, 10)}
    assert result.agent_calls == expected_agent_calls
    assert result.state.status == "completed"

