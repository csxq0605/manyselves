"""A list or object sent as its JSON text is read back where the contract says so."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from manyselves.kernel.contracts.adapters import (
    ContractValidationError,
    JsonSchemaContractAdapter,
    PydanticContractAdapter,
)
from manyselves.kernel.contracts.decoding import read_json_text


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    value: float


class Detail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = ""


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Item]
    groups: dict[str, list[Item]] = Field(default_factory=dict)
    extra_items: list[Item] | None = None
    detail: Detail = Field(default_factory=Detail)
    summary: str = ""
    anything: Any = None
    text_or_list: str | list[str] = ""


ITEMS = [{"id": "E-1", "value": 1.5}, {"id": "E-2", "value": 2}]


def test_a_list_sent_as_text_is_read_back() -> None:
    adapter = PydanticContractAdapter(Submission)
    value = adapter.validate({"items": json.dumps(ITEMS, ensure_ascii=False)})
    assert [item.id for item in value.items] == ["E-1", "E-2"]


def test_text_is_read_back_inside_maps_options_objects_and_lists() -> None:
    adapter = PydanticContractAdapter(Submission)
    value = adapter.validate({
        # Each item sent as text, inside a list sent as text.
        "items": json.dumps([json.dumps(item) for item in ITEMS]),
        "groups": {"G-1": json.dumps(ITEMS[:1])},
        "extra_items": "\n  " + json.dumps(ITEMS[1:]) + "\n",
        "detail": '{"note": "第一行\n第二行"}',  # a raw line break inside the text
    })
    assert [item.id for item in value.items] == ["E-1", "E-2"]
    assert value.groups["G-1"][0].id == "E-1"
    assert value.extra_items is not None and value.extra_items[0].id == "E-2"
    assert value.detail.note == "第一行\n第二行"


def test_a_place_that_allows_text_or_leaves_the_type_open_keeps_the_text() -> None:
    adapter = PydanticContractAdapter(Submission)
    value = adapter.validate({
        "items": json.dumps(ITEMS),
        "summary": "[1, 2]",
        "anything": "[1, 2]",
        "text_or_list": "[1, 2]",
    })
    assert (value.summary, value.anything, value.text_or_list) == ("[1, 2]", "[1, 2]", "[1, 2]")


def test_text_that_is_not_the_declared_kind_is_still_an_error() -> None:
    adapter = PydanticContractAdapter(Submission)
    for wrong in ('[{"id": "E-1", "value": 1,}', "E-1, E-2", json.dumps(ITEMS[0])):
        with pytest.raises(ContractValidationError) as raised:
            adapter.validate({"items": wrong})
        assert "items" in str(raised.value)


def test_errors_after_reading_name_the_items_by_id() -> None:
    adapter = PydanticContractAdapter(Submission)
    with pytest.raises(ContractValidationError) as raised:
        adapter.validate({"items": json.dumps([ITEMS[0], {"id": "E-2", "value": "高"}])})
    assert "items[1]（E-2）.value" in str(raised.value)


def test_reading_back_and_relocation_work_together() -> None:
    # The list arrives as text, and ``note`` one level too high.
    value = PydanticContractAdapter(Submission).validate(
        {"items": json.dumps(ITEMS), "note": "n"}
    )
    assert [item.id for item in value.items] == ["E-1", "E-2"]
    assert value.detail.note == "n"


def test_a_json_schema_contract_reads_text_back_too() -> None:
    adapter = JsonSchemaContractAdapter({
        "type": "object",
        "required": ["rows"],
        "additionalProperties": False,
        "properties": {
            "rows": {"type": "array", "items": {"$ref": "#/$defs/Row"}},
            "label": {"type": "string"},
        },
        "$defs": {"Row": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}},
    })
    assert adapter.validate({"rows": '[{"id": "R-1"}]', "label": "[x]"}) == {
        "rows": [{"id": "R-1"}],
        "label": "[x]",
    }


def test_nothing_to_read_returns_the_value_itself() -> None:
    schema = PydanticContractAdapter(Submission).json_schema()
    value: dict[str, Any] = {"items": ITEMS, "summary": "[1]"}
    same, where = read_json_text(value, schema)
    assert same is value and where == []
    read, where = read_json_text({"items": json.dumps(ITEMS)}, schema)
    assert where == ["items"] and read["items"] == ITEMS
