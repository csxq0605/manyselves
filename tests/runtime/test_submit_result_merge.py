"""A correction carries only what was wrong and is merged into the rejected submission."""

from manyselves.runtime.tools.submit_result import merge_correction

SCHEMA = {
    "type": "object",
    "required": ["rows", "relations"],
    "properties": {"rows": {}, "relations": {}, "note": {}},
    "additionalProperties": False,
}
REJECTED = {
    "rows": [{"id": "J-1", "v": "bad"}, {"id": "J-2", "v": "ok"}],
    "relations": {"J-1": [{"to": "J-9"}], "J-2": [{"to": "J-8", "reason": None}]},
    "row": "misnamed",
}


def test_only_the_wrong_rows_are_resent():
    merged = merge_correction(
        REJECTED,
        {"rows": [{"id": "J-1", "v": "fixed"}], "relations": {"J-2": [{"to": "J-8", "reason": "r"}]}},
        schema=SCHEMA,
    )
    assert merged["rows"] == [{"id": "J-1", "v": "fixed"}, {"id": "J-2", "v": "ok"}]
    assert merged["relations"]["J-1"] == [{"to": "J-9"}]
    assert merged["relations"]["J-2"] == [{"to": "J-8", "reason": "r"}]
    # The misnamed field the contract rejected does not come back.
    assert "row" not in merged


def test_a_full_resubmission_stands_on_its_own():
    full = {"rows": [{"id": "J-1", "v": "a"}, {"id": "J-3", "v": "b"}], "relations": {}}
    assert merge_correction(REJECTED, full, schema=SCHEMA) == full
