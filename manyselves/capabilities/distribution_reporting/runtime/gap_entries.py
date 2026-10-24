"""What an undetermined row lacks, read from the records it cites, and equal gaps merged.

A row that cannot be judged says in its own words what is missing.  What
kind of gap that is the program reads from the records the row cites,
never from the words:

- ``LISTED``: the document checklist records the document as not handed
  over (its availability column is NG);
- ``BLANK``: a cited record keeps a column the table left empty, and the
  row's missing item names that column;
- ``READING``: the records give a reading, a value the judgement needs is
  not in any table (a rating, the side a current was taken on);
- ``OPINION``: every cited record is a summary sheet's entry, another
  author's conclusion with nothing under it;
- ``STATE``: the records give only states (OK, NG, 无);
- ``NONE``: the row cites no record.

The kind fixes the row's evidence status, the default way to close the gap
and the words the report uses for it.  Rows whose gap is the same (kind,
structural key, way to close it) are one gap entry naming every object: the
ledger keeps a row per object, the report says the gap once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..domain.judgment_ledger import JudgmentRow
from .context_packs import Sites, record_fields
from .mappers.common import blank_value
from .mappers.s2_1 import AVAILABILITY

LISTED = "收资表记为不具备"
BLANK = "原表未填写"
READING = "有读数缺判定参数"
OPINION = "只有原评估结论"
STATE = "只有状态记录"
NONE = "没有对应记录"
GAP_KINDS = (LISTED, BLANK, READING, OPINION, STATE, NONE)

# What a row of each kind has to go on.
STATUS_OF_KIND = {
    LISTED: "资料未提供",
    NONE: "资料未提供",
    BLANK: "现场确认",
    READING: "现场确认",
    STATE: "现场确认",
    OPINION: "来源作者意见",
}
# How the gap is closed when the author does not say.
DEFAULT_FILL = {
    LISTED: ["补资料"],
    NONE: ["补资料"],
    BLANK: ["问委托方"],
    READING: ["补资料", "问委托方"],
    STATE: ["现场测量"],
    OPINION: ["补资料"],
}
# The checklist's state for a document not handed over.
_NOT_HANDED_OVER = "NG"
OBJECTS_SHOWN = 6


def _cited(row: JudgmentRow, sites: Sites) -> list[Any]:
    return [sites.items[e] for e in row.evidence_ids if e in sites.items]


def _sheet(item: Any) -> str:
    return getattr(item.source, "sheet", None) or ""


def _listed(item: Any) -> bool:
    return dict(record_fields(item.fact)).get(AVAILABILITY) == _NOT_HANDED_OVER


def _blank_fields(row: JudgmentRow, items: Sequence[Any]) -> list[tuple[str, str]]:
    """The (sheet, column) pairs a cited record left empty that the row says it lacks."""

    missing = row.gap.missing if row.gap else ""
    return list(
        dict.fromkeys(
            (_sheet(item), label)
            for item in items
            for label, value in record_fields(item.fact)
            if blank_value(value) and label in missing
        )
    )


def _read(items: Sequence[Any]) -> bool:
    return any(
        getattr(m, "value", None) is not None for item in items for m in item.measurements or ()
    )


def gap_kind(row: JudgmentRow, sites: Sites) -> str:
    items = _cited(row, sites)
    if not items:
        return NONE
    if any(_listed(item) for item in items):
        return LISTED
    if _blank_fields(row, items):
        return BLANK
    if _read(items):
        return READING
    if all(getattr(item, "summary_entry", False) for item in items):
        return OPINION
    return STATE


def fill_of(row: JudgmentRow, kind: str) -> list[str]:
    return list(row.gap.fill) if row.gap and row.gap.fill else list(DEFAULT_FILL[kind])


def settle_gap_kinds(rows: Sequence[JudgmentRow], sites: Sites) -> list[JudgmentRow]:
    """An undetermined row's evidence status is what its records are, set by the program."""

    settled = []
    for row in rows:
        if row.disposition == "undetermined":
            status = STATUS_OF_KIND[gap_kind(row, sites)]
            if status != row.evidence_status:
                row = row.model_copy(update={"evidence_status": status})
        settled.append(row)
    return settled


def _key(row: JudgmentRow, kind: str, sites: Sites) -> tuple[Any, ...]:
    """Rows with the same key lack the same thing; the key is read from the records only."""

    items = _cited(row, sites)
    fill = tuple(fill_of(row, kind))
    if kind == LISTED:
        return (kind, tuple(sorted(item.id for item in items if _listed(item))), fill)
    if kind == BLANK:
        return (kind, tuple(sorted(_blank_fields(row, items))), fill)
    if kind == OPINION:
        return (kind, tuple(sorted(row.evidence_ids)), fill)
    if kind in (READING, STATE):
        # The same columns read in the same section, short of the same judgement.
        columns = sorted(
            {(_sheet(item), label) for item in items for label, _value in record_fields(item.fact)}
        )
        return (kind, row.submodule_id, tuple(columns), fill)
    return (kind, row.id, fill)


def _document(item: Any) -> str:
    return str(item.subject).rsplit("/", 1)[-1]


def _missing_text(kind: str, rows: Sequence[JudgmentRow], sites: Sites) -> str:
    if kind == LISTED:
        items = [item for row in rows for item in _cited(row, sites) if _listed(item)]
        return "、".join(dict.fromkeys(_document(item) for item in items))
    if kind == BLANK:
        pairs = dict.fromkeys(pair for row in rows for pair in _blank_fields(row, _cited(row, sites)))
        by_sheet: dict[str, list[str]] = {}
        for sheet, label in pairs:
            by_sheet.setdefault(sheet, []).append(label)
        return "；".join(f"{sheet}的{'、'.join(labels)}" for sheet, labels in by_sheet.items())
    # Rows of one entry lack the same thing in their own words; the first says it for all.
    return next((row.gap.missing for row in rows if row.gap), "")


def wording(kind: str, missing: str) -> str:
    """How the report says a gap of this kind; only a document not handed over is 未提供."""

    return {
        LISTED: f"{missing}未提供（收资表记为不具备）",
        NONE: f"{missing}未提供",
        BLANK: f"原表{missing}未填写",
        READING: f"有记录读数，缺{missing}，不能判定",
        OPINION: f"只有原评估结论，缺支撑记录：{missing}",
        STATE: f"只有状态记录，缺{missing}，不能判定",
    }[kind]


def objects_text(objects: Sequence[str]) -> str:
    shown = "、".join(objects[:OBJECTS_SHOWN])
    return shown + (f"等 {len(objects)} 个对象" if len(objects) > OBJECTS_SHOWN else "")


def gap_entries(
    ledger: Mapping[str, Sequence[JudgmentRow]], sites: Sites
) -> list[dict[str, Any]]:
    """Every gap once: the rows lacking the same thing, in ledger order.

    A row with a gap that is not undetermined (a concern that also lacks a
    document) is an entry of its own kind too.  The entry's section is where
    most of its rows are (the first of them on a tie); the other sections
    point to it.
    """

    grouped: dict[tuple[Any, ...], list[tuple[str, JudgmentRow]]] = {}
    kinds: dict[tuple[Any, ...], str] = {}
    for module_id, rows in ledger.items():
        for row in rows:
            if row.gap is None:
                continue
            kind = gap_kind(row, sites)
            key = _key(row, kind, sites)
            grouped.setdefault(key, []).append((module_id, row))
            kinds[key] = kind
    entries = []
    for index, (key, members) in enumerate(grouped.items(), 1):
        kind = kinds[key]
        rows = [row for _module, row in members]
        sections = list(dict.fromkeys(row.submodule_id for row in rows))
        section = max(sections, key=lambda s: (sum(r.submodule_id == s for r in rows), -sections.index(s)))
        missing = _missing_text(kind, rows, sites)
        objects = list(dict.fromkeys(row.object for row in rows))
        entries.append(
            {
                "gap_id": f"G{index:02d}",
                "kind": kind,
                "missing": missing,
                "wording": wording(kind, missing),
                "objects": objects,
                "row_ids": [row.id for row in rows],
                "modules": list(dict.fromkeys(module for module, _row in members)),
                "sections": sections,
                "section": section,
                "impact": rows[0].gap.impact,
                "requested_input": "；".join(dict.fromkeys(row.gap.requested_input for row in rows)),
                "fill": list(key[-1]),
                "source_ids": list(dict.fromkeys(e for row in rows for e in row.evidence_ids)),
            }
        )
    return entries


def section_gap_entries(
    entries: Sequence[Mapping[str, Any]], rows: Sequence[JudgmentRow], submodule_id: str
) -> list[dict[str, Any]]:
    """The gap entries a section's rows are in: written here once, or pointed to."""

    own = {row.id for row in rows}
    found = []
    for entry in entries:
        here = [row_id for row_id in entry["row_ids"] if row_id in own]
        if not here:
            continue
        write_here = entry["section"] == submodule_id
        found.append(
            {
                "gap_id": entry["gap_id"],
                "kind": entry["kind"],
                "wording": entry["wording"],
                "row_ids": here,
                "write_here": write_here,
                **(
                    {
                        "objects": objects_text(entry["objects"]),
                        "fill": entry["fill"],
                        "requested_input": entry["requested_input"],
                    }
                    if write_here
                    else {"see_section": entry["section"]}
                ),
            }
        )
    return found


def chief_gap_entries(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """What chapters 1 and 3 read of the gaps: each once, with its rows and objects."""

    return [
        {
            "gap_id": entry["gap_id"],
            "wording": entry["wording"],
            "objects": objects_text(entry["objects"]),
            "section": entry["section"],
            "row_ids": list(entry["row_ids"]),
            "fill": list(entry["fill"]),
            "requested_input": entry["requested_input"],
        }
        for entry in entries
    ]


def gap_ids(entries: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Row id -> the gap entry it is in."""

    return {row_id: entry["gap_id"] for entry in entries for row_id in entry["row_ids"]}


def gap_labels(entries: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Row id -> its gap entry as one line (``〔G03〕原表…未填写``)."""

    return {
        row_id: f"〔{entry['gap_id']}〕{entry['wording']}"
        for entry in entries
        for row_id in entry["row_ids"]
    }


def gap_sections(entries: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Row id -> the section that writes its gap."""

    return {row_id: entry["section"] for entry in entries for row_id in entry["row_ids"]}


__all__ = [
    "BLANK",
    "DEFAULT_FILL",
    "GAP_KINDS",
    "LISTED",
    "NONE",
    "OPINION",
    "READING",
    "STATE",
    "STATUS_OF_KIND",
    "chief_gap_entries",
    "fill_of",
    "gap_entries",
    "gap_ids",
    "gap_labels",
    "gap_kind",
    "gap_sections",
    "objects_text",
    "section_gap_entries",
    "settle_gap_kinds",
    "wording",
]
