import pytest
from pydantic import BaseModel

from manyselves.kernel.contracts import (
    ContractValidationError,
    build_contract_adapter,
)
from manyselves.kernel.definitions import ContractDefinition


class AdjustmentInput(BaseModel):
    value: int


def test_pydantic_contract_adapter_validates_and_exposes_schema() -> None:
    adapter = build_contract_adapter(
        ContractDefinition(
            id="adjustment-input",
            version="1.0.0",
            description="Pydantic input",
            adapter="pydantic",
            model=f"{__name__}:AdjustmentInput",
        )
    )

    validated = adapter.validate({"value": 3})

    assert validated == AdjustmentInput(value=3)
    assert adapter.json_schema()["properties"]["value"]["type"] == "integer"
    assert adapter.is_assignable_to(adapter)

    with pytest.raises(ContractValidationError):
        adapter.validate({"value": "not-an-integer"})


def test_json_schema_contract_adapter_validates_without_changing_value() -> None:
    definition = ContractDefinition(
        id="adjustment-input",
        version="1.0.0",
        description="JSON Schema input",
        adapter="json_schema",
        schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    adapter = build_contract_adapter(definition)

    value = {"value": 3}

    assert adapter.validate(value) is value
    assert adapter.json_schema() == definition.schema_
    assert adapter.is_assignable_to(build_contract_adapter(definition))

    with pytest.raises(ContractValidationError):
        adapter.validate({"value": "not-an-integer"})
