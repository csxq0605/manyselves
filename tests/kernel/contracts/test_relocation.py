"""A field submitted at the wrong level goes back where the contract puts it."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field

from manyselves.kernel.contracts.adapters import (
    ContractValidationError,
    JsonSchemaContractAdapter,
    PydanticContractAdapter,
)


class Link(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_id: str
    relation: str


class Considered(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relation: str


class Declined(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_id: str
    reason: str


class Detail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = ""


class Links(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relations: dict[str, list[Link]] = Field(default_factory=dict)
    considered: list[Considered] = Field(default_factory=list)
    declined: list[Declined] = Field(default_factory=list)
    detail: Detail = Field(default_factory=Detail)


LINK = {"row_id": "J-2.4-1", "relation": "负荷—发热"}


def test_a_list_nested_in_a_map_goes_back_beside_the_map() -> None:
    adapter = PydanticContractAdapter(Links)
    value = adapter.validate({
        "relations": {"J-2.1-1": [LINK], "considered": [{"relation": "环境—局放"}]},
        "declined": [],
    })
    assert set(value.relations) == {"J-2.1-1"}
    assert [c.relation for c in value.considered] == ["环境—局放"]


def test_a_field_one_level_too_high_goes_down_into_its_object() -> None:
    value = PydanticContractAdapter(Links).validate({"relations": {}, "note": "见附表"})
    assert value.detail.note == "见附表"


def test_an_uncertain_place_is_named_instead_of_the_wrong_parents_rules() -> None:
    adapter = PydanticContractAdapter(Links)
    with pytest.raises(ContractValidationError) as raised:
        # Misplaced, and its entries do not fit where it belongs either.
        adapter.validate({"relations": {"considered": [{"relation": "x", "weight": 1}]}})
    message = str(raised.value)
    assert "considered 应放在 最外层 里" in message and "不是 relations 里面" in message
    assert "relations.considered" not in message.split("\n", 2)[-1]
    # Filled already where it belongs: nothing is moved, the error stands.
    with pytest.raises(ContractValidationError):
        adapter.validate({"relations": {"considered": [{"relation": "x"}]}, "considered": []})


def test_a_valid_submission_and_a_json_schema_contract() -> None:
    assert PydanticContractAdapter(Links).validate({"relations": {"J-1": [LINK]}}).relations
    adapter = JsonSchemaContractAdapter({
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "findings": {"type": "object", "additionalProperties": {"type": "object", "required": ["problem"]}},
            "declined": {"type": "object", "additionalProperties": {"type": "string"}},
        },
    })
    moved = adapter.validate({"findings": {"2.4.1": {"problem": "p"}, "declined": {"2.4.2": "理由"}}})
    assert moved == {"findings": {"2.4.1": {"problem": "p"}}, "declined": {"2.4.2": "理由"}}
