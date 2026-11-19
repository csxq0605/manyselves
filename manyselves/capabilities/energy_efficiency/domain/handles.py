"""Tables of the foundation and the handles that name their values.

Every table has one row per key and a column header that says each column's
unit, basis, status, method and the parameter assumptions it rests on.  A
row's handle is ``<TABLE>.<key>.<key>…`` and a value's handle is the row's
handle plus ``.<column>`` (``SL.E04.ALL_IDLE.p50_kw``).  A finder cites a
value by its handle and the program reads it back; nobody copies numbers by
hand.  Row status, limits and assumptions add to the column's.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "manyselves.energy.foundation.table.1"
HANDLE = re.compile(r"^[A-Z]{2,5}(\.[\w\-:]+)+$")
_PART = re.compile(r"[^\w\-:]+")


def handle_part(value: Any) -> str:
    """A key as it may stand in a handle; a key that had to change gets a short hash so it stays unique."""

    text = str(value)
    cleaned = _PART.sub("_", text).strip("_")
    if cleaned == text:
        return text
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    return f"{cleaned}-{digest[:4]}" if cleaned else f"X{digest[:6]}"


@dataclass
class Column:
    unit: str | None
    method: str
    basis: str = "DERIVED"
    status: str = "CONFIRMED"
    assumption_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "basis": self.basis,
            "status": self.status,
            "method": self.method,
            "assumption_ids": list(self.assumption_ids),
        }


class Table:
    def __init__(
        self,
        name: str,
        description: str,
        key_columns: list[str],
        columns: dict[str, Column],
        limits: list[str] | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Z]{2,5}", name):
            raise ValueError(f"table names are 2-5 capital letters, not {name!r}")
        self.name = name
        self.description = description
        self.key_columns = key_columns
        self.columns = columns
        self.limits = list(limits or [])
        self.rows: list[dict[str, Any]] = []
        self._ids: set[str] = set()

    def add(
        self,
        keys: list[Any],
        values: dict[str, Any],
        *,
        status: str | None = None,
        limits: list[str] | None = None,
        assumption_ids: list[str] | None = None,
    ) -> str:
        if len(keys) != len(self.key_columns):
            raise ValueError(f"{self.name}: {len(self.key_columns)} keys expected")
        unknown = set(values) - set(self.columns)
        if unknown:
            raise ValueError(f"{self.name}: unknown columns {sorted(unknown)}")
        row_id = ".".join([self.name, *(handle_part(key) for key in keys)])
        if row_id in self._ids:
            raise ValueError(f"{self.name}: row {row_id} repeats")
        self._ids.add(row_id)
        row = {"id": row_id, **dict(zip(self.key_columns, keys, strict=True)), **values}
        row["status"] = status
        row["limits"] = list(limits or [])
        row["assumption_ids"] = sorted(set(assumption_ids or []))
        self.rows.append(row)
        return row_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.name,
            "schema_version": SCHEMA,
            "description": self.description,
            "key_columns": self.key_columns,
            "columns": {name: column.as_dict() for name, column in self.columns.items()},
            "limits": self.limits,
            "rows": self.rows,
        }


class HandleError(ValueError):
    """A handle that names nothing in the foundation."""


class Foundation:
    """Read access to a run's foundation tables by handle."""

    def __init__(self, tables: dict[str, dict[str, Any]]) -> None:
        self.tables = tables
        self._rows: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for table in tables.values():
            for row in table["rows"]:
                self._rows[row["id"]] = (table, row)

    @classmethod
    def load(cls, directory: Path) -> "Foundation":
        tables = {}
        for path in sorted(Path(directory).glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema_version") == SCHEMA:
                tables[data["table"]] = data
        return cls(tables)

    def has_row(self, row_id: str) -> bool:
        return row_id in self._rows

    def row(self, row_id: str) -> dict[str, Any]:
        if row_id not in self._rows:
            raise HandleError(f"没有这一行：{row_id}")
        return self._rows[row_id][1]

    def table_of(self, row_id: str) -> dict[str, Any]:
        return self._rows[row_id][0]

    def split(self, handle: str) -> tuple[str, str | None]:
        """A handle as (row id, column); the column is None for a row handle."""

        if not HANDLE.match(handle or ""):
            raise HandleError(f"不是句柄：{handle!r}（形如 SL.<表编号>.ALL_IDLE.p50_kw）")
        if handle in self._rows:
            return handle, None
        row_id, _, column = handle.rpartition(".")
        if row_id in self._rows:
            table = self._rows[row_id][0]
            if column in table["columns"]:
                return row_id, column
            raise HandleError(f"{row_id} 没有列 {column}（有：{', '.join(table['columns'])}）")
        raise HandleError(f"底座里没有 {handle}")

    def value(self, handle: str) -> dict[str, Any]:
        """One value with what it rests on: the column's header merged with the row's additions."""

        row_id, column = self.split(handle)
        table, row = self._rows[row_id]
        if column is None:
            raise HandleError(f"{handle} 是一行，不是一个值；在后面加列名，例如 {handle}.{next(iter(table['columns']))}")
        header = table["columns"][column]
        return {
            "id": handle,
            "value": row.get(column),
            "unit": header["unit"],
            "basis": header["basis"],
            "status": row["status"] or header["status"],
            "method": header["method"],
            "limits": [*table["limits"], *row["limits"]],
            "assumption_ids": sorted({*header["assumption_ids"], *row["assumption_ids"]}),
        }

    def row_view(self, row_id: str) -> dict[str, Any]:
        """A row with its columns' units, for a reader."""

        table, row = self._rows[row_id]
        return {
            "id": row_id,
            "table": table["table"],
            "values": {
                name: {"value": row.get(name), "unit": header["unit"]}
                for name, header in table["columns"].items()
            },
            "keys": {name: row[name] for name in table["key_columns"]},
            "status": row["status"],
            "limits": [*table["limits"], *row["limits"]],
            "assumption_ids": sorted({*row["assumption_ids"], *(a for h in table["columns"].values() for a in h["assumption_ids"])}),
        }

    def rows(self, table: str, **filters: Any) -> list[dict[str, Any]]:
        data = self.tables.get(table)
        if data is None:
            raise HandleError(f"没有表 {table}（有：{', '.join(sorted(self.tables))}）")
        unknown = set(filters) - set(data["key_columns"])
        if unknown:
            raise HandleError(f"表 {table} 只能按 {data['key_columns']} 筛选")
        return [
            row
            for row in data["rows"]
            if all(str(row.get(name)) == str(value) for name, value in filters.items())
        ]


def numbers_match(written: Any, actual: Any) -> bool:
    """Whether a number a finder wrote is the stored value, allowing for its rounding."""

    if isinstance(actual, bool) or isinstance(written, bool):
        return written == actual
    if isinstance(actual, (int, float)) and isinstance(written, (int, float)):
        if actual == written:
            return True
        text = repr(float(written))
        decimals = len(text.split(".")[1].rstrip("0")) if "." in text else 0
        if round(float(actual), decimals) == round(float(written), decimals):
            return True
        scale = max(abs(float(actual)), 1e-9)
        return abs(float(actual) - float(written)) / scale <= 0.005
    return str(written).strip() == str(actual).strip()


__all__ = [
    "Column",
    "Foundation",
    "HANDLE",
    "HandleError",
    "SCHEMA",
    "Table",
    "handle_part",
    "numbers_match",
]
