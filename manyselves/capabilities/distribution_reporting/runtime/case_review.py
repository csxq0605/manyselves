"""The same-case review: rows judging the same column of the same table, side by side.

A judgment rests on records, and records of one column of one table (the
same check, recorded the same way) are the same case: an NG in the
compliance sheet's spare-parts column means the same in every room.  Rows
judging such records should draw the same boundary, or say from the records
why not.  The reviewer reads every row on a case together, across rooms and
modules, with every record of that column, so a boundary set once is seen
everywhere it applies.

The case is read from the records alone, never from the words:

- a record's shape is its sheet, its checkpoint column and the labels of its
  readings;
- a row belongs to the cases of its field records (records with a checkpoint
  column or readings); a row resting only on summary or document-list
  entries belongs to the cases of those;
- a case is a shape at least two rows rest on.  A field case shows every
  record of that column, cited or not, so the exceptions are in view; a
  summary case shows the entries its rows cite (other entries are other
  subjects).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ..domain.judgment_ledger import JudgmentRow
from .context_packs import Render, Sites, compact_row

Shape = tuple[str, str, tuple[str, ...]]


def record_shape(item: Any) -> Shape:
    labels = tuple(sorted({
        str(m.label) for m in item.measurements or () if getattr(m, "label", None)
    }))
    return (getattr(item.source, "sheet", None) or "", item.checkpoint_field or "", labels)


def _field(item: Any) -> bool:
    return not item.summary_entry and bool(item.checkpoint_field or item.measurements)


def case_label(shape: Shape) -> str:
    sheet, field, labels = shape
    parts = [sheet or "（无表名）"]
    if field:
        parts.append(f"「{field}」栏")
    if labels:
        parts.append("读数：" + "、".join(labels))
    return " ".join(parts)


def row_cases(row: JudgmentRow, sites: Sites) -> list[Shape]:
    items = [sites.items[e] for e in row.evidence_ids if e in sites.items]
    field = [item for item in items if _field(item)]
    return list(dict.fromkeys(record_shape(item) for item in (field or items)))


def cases(
    ledger: Mapping[str, Sequence[JudgmentRow]], sites: Sites
) -> dict[Shape, list[JudgmentRow]]:
    """Each shape two or more rows rest on, with those rows."""

    found: dict[Shape, list[JudgmentRow]] = {}
    for rows in ledger.values():
        for row in rows:
            for shape in row_cases(row, sites):
                found.setdefault(shape, []).append(row)
    return {shape: rows for shape, rows in found.items() if len(rows) >= 2}


def _records(shape: Shape, rows: Sequence[JudgmentRow], sites: Sites) -> list[Any]:
    cited = [
        sites.items[e]
        for e in dict.fromkeys(e for row in rows for e in row.evidence_ids)
        if e in sites.items and record_shape(sites.items[e]) == shape
    ]
    if not cited or not _field(cited[0]):
        return cited
    return [item for item in sites.items.values() if record_shape(item) == shape]


def case_batches(
    ledger: Mapping[str, Sequence[JudgmentRow]],
    sites: Sites,
    render: Render,
    *,
    budget_chars: int,
) -> list[dict[str, Any]]:
    """The cases in batches within the budget; a case is never split."""

    found = sorted(cases(ledger, sites).items(), key=lambda kv: kv[0])
    entries = []
    for shape, rows in found:
        records = _records(shape, rows, sites)
        cost = sum(len(json.dumps(compact_row(r), ensure_ascii=False)) for r in rows) + len(render(records))
        entries.append((shape, rows, records, cost))
    groups: list[list[tuple[Shape, list[JudgmentRow], list[Any], int]]] = []
    size = 0
    for entry in entries:
        if groups and size + entry[3] <= budget_chars:
            groups[-1].append(entry)
            size += entry[3]
            continue
        groups.append([entry])
        size = entry[3]
    batches = []
    for number, group in enumerate(groups, start=1):
        rows = list({row.id: row for _s, rs, _r, _c in group for row in rs}.values())
        records = list({item.id: item for _s, _rs, rec, _c in group for item in rec}.values())
        batches.append({
            "batch_id": f"C-{number:02d}",
            "cases": [
                {
                    "case": case_label(shape),
                    "row_ids": [row.id for row in rs],
                    "record_ids": [item.id for item in rec],
                }
                for shape, rs, rec, _c in group
            ],
            "rows": [compact_row(row) for row in rows],
            "facts": render(records),
        })
    return batches


__all__ = ["case_batches", "case_label", "cases", "record_shape", "row_cases"]
