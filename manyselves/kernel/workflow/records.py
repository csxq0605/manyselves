"""Pure keyed-record and collection helpers used by Map and record Actions.

A record table is a JSON object whose keys identify records.  These helpers
never persist anything; they only compute new values from existing state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from .models import ItemCondition


class RecordError(ValueError):
    """Raised when a collection cannot be read as declared."""


def compare(left: Any, operator: str, right: Any) -> bool:
    """Apply one declared comparison operator."""

    if operator == "eq":
        return left == right
    if operator == "ne":
        return left != right
    if operator == "lt":
        return left < right
    if operator == "lte":
        return left <= right
    if operator == "gt":
        return left > right
    if operator == "gte":
        return left >= right
    if operator == "truthy":
        return bool(left)
    if operator == "falsy":
        return not bool(left)
    if operator == "in":
        return left in right
    if operator == "not_in":
        return left not in right
    raise RecordError(f"unknown operator: {operator}")


def field_value(item: Any, field: str) -> Any:
    """Read a dotted field from a mapping; a missing field reads as ``None``."""

    current = item
    for part in field.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def matches_all(item: Any, conditions: Sequence[ItemCondition]) -> bool:
    return all(
        compare(field_value(item, condition.field), condition.operator, condition.value)
        for condition in conditions
    )


def keyed_items(
    collection: Any,
    *,
    key_field: str | None,
    owner: str,
) -> list[tuple[str, Any]]:
    """Return ``(key, item)`` pairs in a stable order.

    A mapping is a record table and keeps its own key order.  A list needs
    ``key_field``; without it the list position becomes the key.  Duplicate
    keys are an error because results are collected by key.
    """

    pairs: list[tuple[str, Any]]
    if isinstance(collection, Mapping):
        pairs = [(str(key), value) for key, value in collection.items()]
    elif isinstance(collection, (list, tuple)):
        if key_field is None:
            pairs = [(str(index), value) for index, value in enumerate(collection)]
        else:
            pairs = []
            for value in collection:
                key = field_value(value, key_field)
                if key is None or key == "":
                    raise RecordError(
                        f"{owner}: item has no key field {key_field!r}"
                    )
                pairs.append((str(key), value))
    else:
        raise RecordError(f"{owner}: collection must be a list or an object")
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise RecordError(f"{owner}: duplicate item key {key!r}")
        seen.add(key)
    return pairs


def map_items(
    collection: Any,
    *,
    key_field: str | None,
    where: Sequence[ItemCondition],
    owner: str,
) -> list[tuple[str, Any]]:
    """Return the items a Map action dispatches, keyed and filtered."""

    return [
        (key, deepcopy(item))
        for key, item in keyed_items(collection, key_field=key_field, owner=owner)
        if matches_all(item, where)
    ]


def upsert_records(
    table: Any,
    records: Any,
    *,
    key_field: str | None,
    merge: str,
    owner: str,
) -> dict[str, Any]:
    """Return ``table`` with ``records`` added or replaced by key."""

    if not isinstance(table, Mapping):
        raise RecordError(f"{owner}: record table must be an object")
    updated = deepcopy(dict(table))
    for key, record in keyed_items(records, key_field=key_field, owner=owner):
        if merge == "update" and isinstance(updated.get(key), Mapping):
            if not isinstance(record, Mapping):
                raise RecordError(f"{owner}: update merge requires object records")
            updated[key] = {**updated[key], **deepcopy(dict(record))}
        else:
            updated[key] = deepcopy(record)
    return updated


def select_records(
    table: Any,
    *,
    where: Sequence[ItemCondition],
    output: str,
    owner: str,
) -> Any:
    """Return matching records as a table, a list, or a list of keys."""

    if not isinstance(table, Mapping):
        raise RecordError(f"{owner}: record table must be an object")
    selected = {
        str(key): deepcopy(record)
        for key, record in table.items()
        if matches_all(record, where)
    }
    if output == "list":
        return list(selected.values())
    if output == "keys":
        return list(selected)
    return selected


def gather_by_key(
    sources: Mapping[str, Mapping[str, Any] | None],
) -> dict[str, dict[str, Any]]:
    """Several record tables keyed alike, gathered per key.

    ``sources`` maps a source name (for example one reviewer) to its table
    (record id -> value).  The result maps each record id to the values the
    sources gave it, by source, in the sources' order; nothing is merged
    away, so a caller decides how values for one record combine.
    """

    gathered: dict[str, dict[str, Any]] = {}
    for source, table in sources.items():
        for key, value in (table or {}).items():
            gathered.setdefault(str(key), {})[source] = deepcopy(value)
    return gathered


__all__ = [
    "RecordError",
    "gather_by_key",
    "compare",
    "field_value",
    "keyed_items",
    "map_items",
    "matches_all",
    "select_records",
    "upsert_records",
]
