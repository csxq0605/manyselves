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
and the words the report uses for it.  Rows whose gap is the same (kind and
structural key) are one gap entry naming every object: the ledger keeps a row
per object, the report says the gap once.  The ways to close it are all its
rows' ways together.

The words of an entry are built only from what the program reads in the
records: the checklist's document names, the table and column names, the
quantities read.  What the authors wrote the row lacks is kept as the entry's
note for the gap table, never put into those words.
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


FILL_ORDER = ("补资料", "现场测量", "问委托方")


def fill_of(row: JudgmentRow, kind: str) -> list[str]:
    return list(row.gap.fill) if row.gap and row.gap.fill else list(DEFAULT_FILL[kind])


def _fills(rows: Sequence[JudgmentRow], kind: str) -> list[str]:
    chosen = {way for row in rows for way in fill_of(row, kind)}
    return [way for way in FILL_ORDER if way in chosen]


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
    if kind == LISTED:
        return (kind, tuple(sorted(item.id for item in items if _listed(item))))
    if kind == BLANK:
        return (kind, tuple(sorted(_blank_fields(row, items))))
    if kind == OPINION:
        return (kind, tuple(sorted(row.evidence_ids)))
    if kind in (READING, STATE):
        # The same columns read in the same section, short of the same judgement.
        columns = sorted(
            {(_sheet(item), label) for item in items for label, _value in record_fields(item.fact)}
        )
        return (kind, row.submodule_id, tuple(columns))
    return (kind, row.id)


def _document(item: Any) -> str:
    return str(item.subject).rsplit("/", 1)[-1]


def _by_sheet(pairs: Sequence[tuple[str, str]]) -> str:
    by_sheet: dict[str, list[str]] = {}
    for sheet, label in dict.fromkeys(pairs):
        if label not in by_sheet.setdefault(sheet, []):
            by_sheet[sheet].append(label)
    return "；".join(
        f"{sheet}的{'、'.join(labels)}" if sheet else "、".join(labels)
        for sheet, labels in by_sheet.items()
    )


def _missing_text(kind: str, rows: Sequence[JudgmentRow], sites: Sites) -> str:
    """What the entry names, from the records alone: documents, tables, columns, quantities."""

    items = list({item.id: item for row in rows for item in _cited(row, sites)}.values())
    if kind == LISTED:
        return "、".join(dict.fromkeys(_document(item) for item in items if _listed(item)))
    if kind == BLANK:
        return _by_sheet(
            [pair for row in rows for pair in _blank_fields(row, _cited(row, sites))]
        )
    if kind == READING:
        return _by_sheet([
            (_sheet(item), m.label or m.quantity)
            for item in items for m in item.measurements or ()
            if getattr(m, "value", None) is not None and (m.label or m.quantity)
        ])
    if kind == STATE:
        return _by_sheet([
            (_sheet(item), item.checkpoint_field or label)
            for item in items for label, _value in record_fields(item.fact)[:1]
        ])
    if kind == OPINION:
        return "、".join(dict.fromkeys(_sheet(item) for item in items if _sheet(item)))
    return ""


def _note(rows: Sequence[JudgmentRow]) -> str:
    """What the authors wrote the rows lack, in their words, for the gap table only."""

    return "；".join(
        dict.fromkeys(row.gap.missing.strip() for row in rows if row.gap and row.gap.missing.strip())
    )


def wording(kind: str, missing: str) -> str:
    """How the report says a gap of this kind; only a document not handed over is 未提供."""

    if kind == LISTED:
        return f"{missing}未提供（收资表记为不具备）"
    if kind == BLANK:
        return f"原表{missing}未填写"
    if kind == READING:
        return f"原表{missing}有记录读数，缺判定所需的参数，不能判定" if missing else "有记录读数，缺判定所需的参数，不能判定"
    if kind == STATE:
        return f"原表{missing}只有状态记录，不能判定" if missing else "只有状态记录，不能判定"
    if kind == OPINION:
        return f"只有{missing}的原评估结论，缺支撑记录" if missing else "只有原评估结论，缺支撑记录"
    return "没有对应记录，不能判定"


def objects_text(objects: Sequence[str]) -> str:
    shown = "、".join(objects[:OBJECTS_SHOWN])
    return shown + (f"等 {len(objects)} 个对象" if len(objects) > OBJECTS_SHOWN else "")


def gap_entries(
    ledger: Mapping[str, Sequence[JudgmentRow]],
    sites: Sites,
    *,
    affects: Mapping[str, Sequence[Mapping[str, str]]] | None = None,
) -> list[dict[str, Any]]:
    """Every gap once: the rows lacking the same thing, in ledger order.

    A row with a gap that is not undetermined (a concern that also lacks a
    document) is an entry of its own kind too.  The entry's section is where
    most of its rows are (the first of them on a tie); the other sections
    point to it.  ``affects`` (row id -> other rows) are the rows of other
    sections a gap bears on, taken out of the relations; the entry lists
    them once, without its own rows.
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
                "fill": _fills(rows, kind),
                "note": _note(rows),
                "affects": _affected(rows, affects or {}),
                "source_ids": list(dict.fromkeys(e for row in rows for e in row.evidence_ids)),
            }
        )
    return entries


def _affected(
    rows: Sequence[JudgmentRow], affects: Mapping[str, Sequence[Mapping[str, str]]]
) -> list[dict[str, str]]:
    own = {row.id for row in rows}
    found: dict[str, dict[str, str]] = {}
    for row in rows:
        for other in affects.get(row.id) or ():
            if other["row_id"] not in own:
                found.setdefault(other["row_id"], dict(other))
    return list(found.values())


def affects_text(affected: Sequence[Mapping[str, str]]) -> str:
    """The rows a gap bears on, one clause per section."""

    by_section: dict[str, list[str]] = {}
    for other in affected:
        named = f"{other['object']}的{other['checkpoint']}"
        listed = by_section.setdefault(other["submodule_id"], [])
        if named not in listed:
            listed.append(named)
    return "；".join(f"{section} {'、'.join(names)}" for section, names in by_section.items())


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
                        **({"affects": affects_text(entry["affects"])} if entry.get("affects") else {}),
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
            **({"affects": affects_text(entry["affects"])} if entry.get("affects") else {}),
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
    "affects_text",
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
