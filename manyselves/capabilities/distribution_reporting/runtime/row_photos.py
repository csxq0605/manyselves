"""The photos a concern row shows, taken from the records it cites when its author named none.

The problem table shows a concern row's ``photo_ids``.  Authors fill that
field unevenly, so a row whose cited record carries the site photo can reach
the report without it.  When a concern row names no photo, the program takes
the photos of its own records: records of the row's section (or placed there
by the row), and of the row's object.  A record with no place (a summary
sheet's entry) belongs to the row only when no other concern row of the
section cites it; its photo could show any of them otherwise.  A photo another
row of the section already shows is not repeated.

``photos_not_shown`` lists, for the delivery notes, photos of such records
that still reach no row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..domain.judgment_ledger import JudgmentRow
from .context_packs import Sites


def _same_object(row: JudgmentRow, subject: str) -> bool:
    obj = row.object.split("（")[0]
    subject = subject.split("（")[0]
    return bool(subject) and (obj.startswith(subject) or subject.startswith(obj))


def _citers(rows: Sequence[JudgmentRow]) -> dict[tuple[str, str], int]:
    count: dict[tuple[str, str], int] = {}
    for row in rows:
        if row.disposition != "concern":
            continue
        for e in dict.fromkeys(row.evidence_ids):
            count[(row.submodule_id, e)] = count.get((row.submodule_id, e), 0) + 1
    return count


def own_photos(row: JudgmentRow, sites: Sites, citers: Mapping[tuple[str, str], int]) -> list[str]:
    """The photos of the records that are this row's own, in citing order."""

    found: list[str] = []
    for e in row.evidence_ids:
        item = sites.items.get(e)
        if item is None or not item.photo_refs:
            continue
        if item.submodule_id not in (None, row.submodule_id):
            continue
        if getattr(item, "place", None):
            if not _same_object(row, str(item.subject)):
                continue
        elif citers.get((row.submodule_id, e), 0) != 1:
            continue
        found.extend(p for p in item.photo_refs if p not in found)
    return found


def filled_photos(
    ledger: Mapping[str, Sequence[JudgmentRow]], sites: Sites
) -> dict[str, list[JudgmentRow]]:
    """The ledger with each concern row that named no photo showing its own records' photos."""

    rows = [row for module_rows in ledger.values() for row in module_rows]
    citers = _citers(rows)
    shown: dict[str, set[str]] = {}
    for row in rows:
        shown.setdefault(row.submodule_id, set()).update(row.photo_ids)
    result: dict[str, list[JudgmentRow]] = {}
    for module_id, module_rows in ledger.items():
        result[module_id] = []
        for row in module_rows:
            if row.disposition == "concern" and not row.photo_ids:
                taken = [p for p in own_photos(row, sites, citers) if p not in shown[row.submodule_id]]
                if taken:
                    row = row.model_copy(update={"photo_ids": taken})
                    shown[row.submodule_id].update(taken)
            result[module_id].append(row)
    return result


def photos_not_shown(
    ledger: Mapping[str, Sequence[JudgmentRow]], sites: Sites
) -> list[dict[str, Any]]:
    """Photos of records a concern row cites in its section that no row shows."""

    rows = [row for module_rows in ledger.values() for row in module_rows]
    shown = {p for row in rows for p in row.photo_ids}
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.disposition != "concern":
            continue
        for e in row.evidence_ids:
            item = sites.items.get(e)
            if item is None or item.submodule_id not in (None, row.submodule_id):
                continue
            for photo_id in item.photo_refs or ():
                if photo_id in shown:
                    continue
                entry = found.setdefault(photo_id, {"photo_id": photo_id, "row_ids": [], "evidence_ids": []})
                if row.id not in entry["row_ids"]:
                    entry["row_ids"].append(row.id)
                if e not in entry["evidence_ids"]:
                    entry["evidence_ids"].append(e)
    return list(found.values())


__all__ = ["filled_photos", "own_photos", "photos_not_shown"]
