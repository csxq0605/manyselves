"""What each role reads, assembled by the program from the table's structure.

Every conversation gets only what its step needs, and the two sides of every
comparison it is asked to make are there in full:

- ``project_map``: one page for every role.  Sites (the table's room column),
  the objects at each site, which modules hold records on them and how many
  are NG; once judged, each flagged row in one line; once related, the
  relations.  It shows where things are, never a conclusion.
- ``judge_facts``: a judge reads its own module's records, every other
  module's records on the same objects, the project-level records (summary
  sheets and documents that belong to no site) and the unassigned ones.
- ``object_batches``: the object reviewer reads all modules' rows and
  records on objects judged by several modules.
- ``cross_batches``: the cross-module reviewer reads every flagged row and
  every end of a relation in full, with the records they cite, as one prefix
  shared by every slice; a slice is the relations it checks and the rows it
  searches for missing ones.  When the prefix is too much for one
  conversation it is split by site, and a system-level scope reads the rows
  that belong to no single site with every flagged row in one line.
- ``writing_groups``: a writer reads its sections' rows, what they cite, the
  rows they relate to and the story lines through them.
- ``compact_row``: a row without its empty fields; ``brief_row`` is what the
  chief reads of a flagged row.

Sites, objects and records come from fixed columns (``EvidenceItem.place``
and ``subject``); sizes are budgets in characters, never counts tuned to a
run.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from ..domain.judgment_ledger import JudgmentRow
from .joint_dossiers import object_key

ATTENTION = {"concern", "undetermined"}
PROJECT_LEVEL = "项目级记录（不属于某个配电房）"
_DISPOSITION = {
    "concern": "关注",
    "undetermined": "不能判定",
    "no_concern": "无问题",
    "not_applicable": "不适用",
}
# The input table records each checkpoint's state as OK or NG.
_STATE = re.compile(r"=\s*(OK|NG)(?![A-Za-z])")

Render = Callable[[Sequence[Any]], str]


# -- rows -------------------------------------------------------------------


def compact(value: Any) -> Any:
    """``value`` without empty fields (None, "", [], {}), at every depth."""

    if isinstance(value, Mapping):
        kept = {key: compact(item) for key, item in value.items()}
        return {key: item for key, item in kept.items() if item not in (None, "", [], {})}
    if isinstance(value, list):
        return [compact(item) for item in value]
    return value


def compact_row(row: JudgmentRow) -> dict[str, Any]:
    return compact(row.model_dump(mode="json", exclude_none=True))


def _cut(text: Any, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def finding_of(row: JudgmentRow) -> str:
    """What a flagged row found, in the row's own words."""

    if row.disposition == "concern":
        return row.finding or row.hazard
    if row.disposition == "undetermined" and row.gap is not None:
        return row.gap.missing
    return ""


def brief_row(row: JudgmentRow, *, basis_chars: int = 120) -> dict[str, Any]:
    """What the chief reads of a flagged row: what was found, why it matters, what to do."""

    return compact(
        {
            "id": row.id,
            "submodule_id": row.submodule_id,
            "object": row.object,
            "checkpoint": row.checkpoint,
            "disposition": row.disposition,
            "evidence_status": row.evidence_status,
            "risk_level": row.risk_level,
            "finding": row.finding,
            "hazard": row.hazard,
            "action": row.action.model_dump(mode="json") if row.action else None,
            "gap": row.gap.model_dump(mode="json") if row.gap else None,
            # The chief argues from relations that hold; a pending one stays
            # with its section in chapter 2.
            "relations": [
                {"row_id": r.row_id, "relation": r.relation, "reason": r.reason}
                for r in row.relations
                if not r.pending
            ],
            "basis": _cut(row.basis, basis_chars),
        }
    )


def row_line(row: JudgmentRow, *, finding_chars: int = 60) -> str:
    level = f"，{row.risk_level}" if row.risk_level else ""
    found = finding_of(row)
    return (
        f"{row.id} {row.submodule_id}｜{row.object}｜{row.checkpoint}"
        f"（{_DISPOSITION.get(row.disposition, row.disposition)}{level}）"
        + (f"：{_cut(found, finding_chars)}" if found else "")
    )


def _index(ledger: Mapping[str, Sequence[JudgmentRow]]) -> dict[str, tuple[str, JudgmentRow]]:
    return {row.id: (module_id, row) for module_id, rows in ledger.items() for row in rows}


def _flagged(ledger: Mapping[str, Sequence[JudgmentRow]]) -> list[tuple[str, JudgmentRow]]:
    return [
        (module_id, row)
        for module_id, rows in ledger.items()
        for row in rows
        if row.disposition in ATTENTION
    ]


# -- sites and objects --------------------------------------------------------


class Sites:
    """Records by site and object, as the table's columns give them."""

    def __init__(self, items: Iterable[Any]) -> None:
        self.items = {item.id: item for item in items}

    def place(self, evidence_id: str) -> str:
        item = self.items.get(evidence_id)
        return (getattr(item, "place", None) or PROJECT_LEVEL) if item else PROJECT_LEVEL

    def subject(self, evidence_id: str) -> str | None:
        item = self.items.get(evidence_id)
        return item.subject if item is not None else None

    def row_places(self, row: JudgmentRow) -> list[str]:
        return list(dict.fromkeys(self.place(e) for e in row.evidence_ids if e in self.items))

    def row_subjects(self, row: JudgmentRow) -> list[str]:
        """The objects a row's records are on (summary entries name no object)."""

        return list(
            dict.fromkeys(
                self.items[e].subject
                for e in row.evidence_ids
                if e in self.items and getattr(self.items[e], "place", None)
            )
        )

    def site_of(self, row: JudgmentRow) -> str:
        """The one site a row is at, or the project level when it spans several or none."""

        places = [p for p in self.row_places(row) if p != PROJECT_LEVEL]
        return places[0] if len(set(places)) == 1 else PROJECT_LEVEL

    def objects(self) -> dict[str, dict[str, list[Any]]]:
        """Site -> object -> its records, in the table's order."""

        found: dict[str, dict[str, list[Any]]] = {}
        for item in self.items.values():
            place = getattr(item, "place", None) or PROJECT_LEVEL
            found.setdefault(place, {}).setdefault(item.subject, []).append(item)
        if PROJECT_LEVEL in found:
            found[PROJECT_LEVEL] = found.pop(PROJECT_LEVEL)
        return found

    def same_object_items(self, module_id: str, own: Iterable[Any] | None = None) -> list[Any]:
        """Other modules' records on the objects this module's records (or ``own``) are on."""

        mine = {
            object_key(item.subject)
            for item in (own if own is not None else self.items.values())
            if item.module_id == module_id and getattr(item, "place", None)
        }
        return [
            item
            for item in self.items.values()
            if item.module_id not in (module_id, None)
            and getattr(item, "place", None)
            and object_key(item.subject) in mine
        ]

    def project_items(self, module_id: str) -> list[Any]:
        """Other modules' records that belong to no site (summary sheets, documents)."""

        return [
            item
            for item in self.items.values()
            if item.module_id not in (module_id, None) and not getattr(item, "place", None)
        ]


def _module_counts(records: Sequence[Any]) -> str:
    counts: dict[str, list[int]] = {}
    for item in records:
        entry = counts.setdefault(item.module_id or "待归属", [0, 0])
        entry[0] += 1
        entry[1] += "NG" in _STATE.findall(item.fact)
    return " ".join(
        f"{module}:{n}" + (f"(NG{ng})" if ng else "") for module, (n, ng) in counts.items()
    )


def project_map(
    sites: Sites,
    ledger: Mapping[str, Sequence[JudgmentRow]] | None = None,
    *,
    with_relations: bool = False,
) -> str:
    """One page: sites, objects, records per module; flagged rows and relations once known."""

    lines = [
        "# 项目图谱",
        "程序按表格的“配电房”列和对象列整理，只说明位置和状态，不是结论。"
        "每个对象一行：各模块的记录条数（NG 条数）"
        + ("；之后是落在这个对象上的关注和不能判定的行（行编号 小节｜对象｜检查点（处置，风险）：发现）。"
           "一行的记录在几个对象上，就在每个对象下各列一次" if ledger is not None else "。"),
    ]
    by_subject: dict[str, list[JudgmentRow]] = {}
    unplaced: list[JudgmentRow] = []
    quiet: dict[str, int] = {}
    if ledger is not None:
        for _module, row in [(m, r) for m, rows in ledger.items() for r in rows]:
            subjects = sites.row_subjects(row)
            if row.disposition not in ATTENTION:
                for subject in subjects:
                    quiet[subject] = quiet.get(subject, 0) + 1
                continue
            if not subjects:
                unplaced.append(row)
            for subject in subjects:
                by_subject.setdefault(subject, []).append(row)
    for place, objects in sites.objects().items():
        lines.append(f"\n## {place}")
        for subject, records in objects.items():
            line = f"- {subject}｜记录 {_module_counts(records)}"
            if ledger is not None:
                flagged = by_subject.get(subject, [])
                if quiet.get(subject):
                    line += f"｜另有 {quiet[subject]} 行判为无问题或不适用"
                lines.append(line)
                lines.extend(f"  - {row_line(row)}" for row in flagged)
            else:
                lines.append(line)
    if unplaced:
        lines.append("\n## 只引汇总表或项目级资料的关注和不能判定行")
        lines.extend(f"- {row_line(row)}" for row in unplaced)
    if ledger is not None and with_relations:
        relations = [
            (row, relation)
            for rows in ledger.values()
            for row in rows
            for relation in row.relations
            if not relation.pending
        ]
        if relations:
            lines.append("\n## 已记下的跨模块关联（行 → 所关联的行：关系；理由）")
            lines.extend(
                f"- {row.id} → {relation.row_id}：{relation.relation}；{_cut(relation.reason, 60)}"
                for row, relation in relations
            )
    return "\n".join(lines) + "\n"


# -- packs ------------------------------------------------------------------


def _size(value: Any) -> int:
    return len(value) if isinstance(value, str) else len(json.dumps(value, ensure_ascii=False))


def _cited(rows: Iterable[JudgmentRow], sites: Sites) -> list[Any]:
    return [
        sites.items[e]
        for e in dict.fromkeys(e for row in rows for e in row.evidence_ids)
        if e in sites.items
    ]


def _batches(
    entries: Sequence[tuple[str, int]], budget_chars: int
) -> list[list[str]]:
    """Consecutive keys grouped so that each group's size stays within the budget."""

    batches: list[list[str]] = []
    size = 0
    for key, cost in entries:
        if batches and size + cost <= budget_chars:
            batches[-1].append(key)
            size += cost
            continue
        batches.append([key])
        size = cost
    return batches


def object_batches(
    ledger: Mapping[str, Sequence[JudgmentRow]],
    sites: Sites,
    render: Render,
    *,
    budget_chars: int,
) -> list[dict[str, Any]]:
    """Objects several modules judged, with all their rows and records, in batches."""

    by_subject: dict[str, list[tuple[str, JudgmentRow]]] = {}
    for module_id, rows in ledger.items():
        for row in rows:
            for subject in sites.row_subjects(row):
                by_subject.setdefault(subject, []).append((module_id, row))
    records: dict[str, list[Any]] = {}
    for item in sites.items.values():
        if getattr(item, "place", None):
            records.setdefault(item.subject, []).append(item)
    shared = [
        subject
        for subject, judged in by_subject.items()
        if len({module for module, _row in judged}) >= 2
    ]
    costs = [
        (
            subject,
            sum(_size(compact_row(row)) for _m, row in by_subject[subject])
            + len(render(records.get(subject, []))),
        )
        for subject in shared
    ]
    batches = []
    for number, subjects in enumerate(_batches(costs, budget_chars), start=1):
        rows = list(
            {row.id: row for subject in subjects for _m, row in by_subject[subject]}.values()
        )
        items = list(
            {item.id: item for subject in subjects for item in records.get(subject, [])}.values()
        )
        batches.append(
            {
                "batch_id": f"O-{number:02d}",
                "objects": [
                    {
                        "object": subject,
                        "row_ids": [row.id for _m, row in by_subject[subject]],
                    }
                    for subject in subjects
                ],
                "rows": [compact_row(row) for row in rows],
                "facts": render(items),
            }
        )
    return batches


# A cross-module reviewer's output grows with what it is asked to decide: each
# relation takes about as long to think through, so a slice holds a few, and
# the rows it searches for missing relations are bounded too.
CROSS_SLICE_RELATIONS = 8
CROSS_SLICE_FOCUS_ROWS = 40


def _relation_entry(row: JudgmentRow, relation: Any) -> dict[str, Any]:
    return compact(
        {
            "for_row": row.id,
            "row_id": relation.row_id,
            "relation": relation.relation,
            "reason": relation.reason,
            "mechanism": relation.mechanism,
            "condition": relation.condition,
            **(
                {"status": relation.status, "missing": relation.missing}
                if relation.pending else {}
            ),
        }
    )


def cross_batches(
    ledger: Mapping[str, Sequence[JudgmentRow]],
    sites: Sites,
    render: Render,
    *,
    prefix_chars: int,
) -> list[dict[str, Any]]:
    """The cross-module review: one shared prefix, cut into slices that vary only at the end.

    The prefix is every flagged row and every end of a recorded relation in
    full, with the records they cite; every slice of a scope carries it the
    same, so the slices after the first reuse it.  A slice is a few sites'
    relations to check and their flagged rows (focus_rows) to search for
    relations the authors did not record.  When the prefix is too big for one
    conversation the review is split by site as the linker was, and a
    system-level scope reads the rows that belong to no single site with every
    flagged row in one line.
    """

    index = _index(ledger)
    flagged = [row for _module, row in _flagged(ledger)]
    relations = [
        (row, relation)
        for rows in ledger.values()
        for row in rows
        for relation in row.relations
        if relation.row_id in index
    ]

    def ends(of: Sequence[JudgmentRow], with_relations: Sequence[tuple[JudgmentRow, Any]]) -> list[JudgmentRow]:
        seen: dict[str, JudgmentRow] = {row.id: row for row in of}
        for row, relation in with_relations:
            seen.setdefault(row.id, row)
            seen.setdefault(relation.row_id, index[relation.row_id][1])
        return list(seen.values())

    def scope(
        scope_id: str,
        label: str,
        focus: Sequence[JudgmentRow],
        mine: Sequence[tuple[JudgmentRow, Any]],
        others: Sequence[JudgmentRow] = (),
    ) -> dict[str, Any]:
        rows = ends(focus, mine)
        prefix: dict[str, Any] = {
            "rows": [compact_row(row) for row in rows],
            "facts": render(_cited(rows, sites)) or "（这些行没有引用记录）",
        }
        if others:
            prefix["other_rows"] = [row_line(row) for row in others]
        return {
            "scope_id": scope_id, "scope": label, "prefix": prefix,
            "focus": list(focus), "relations": list(mine),
        }

    scopes = [scope("X", "全部配电房和项目级记录", flagged, relations)]
    if _size(scopes[0]["prefix"]) > prefix_chars:
        by_site: dict[str, list[JudgmentRow]] = {}
        for row in flagged:
            by_site.setdefault(sites.site_of(row), []).append(row)
        system = by_site.pop(PROJECT_LEVEL, [])
        scopes = []
        placed: set[str] = set()
        for number, (site, site_rows) in enumerate(by_site.items(), start=1):
            ids = {row.id for row in site_rows}
            placed |= ids
            scopes.append(scope(
                f"X{number:02d}", f"配电房：{site}", site_rows,
                [(r, rel) for r, rel in relations if r.id in ids],
            ))
        # Relations on rows of no single site, and the system-level rows' own
        # search, go to a scope that sees every flagged row in one line.
        system_ids = {row.id for row in system}
        scopes.append(scope(
            "X系统", "跨配电房和项目级：不属于单个配电房的行，对照全部关注和不能判定行",
            system,
            [(r, rel) for r, rel in relations if r.id not in placed],
            [row for row in flagged if row.id not in system_ids],
        ))
    batches: list[dict[str, Any]] = []
    for entry in scopes:
        # Slices follow the sites, so a slice's relations and focus rows sit together.
        by_site: dict[str, tuple[list[str], list[tuple[JudgmentRow, Any]]]] = {}
        for row in entry["focus"]:
            by_site.setdefault(sites.site_of(row), ([], []))[0].append(row.id)
        for row, relation in entry["relations"]:
            by_site.setdefault(sites.site_of(row), ([], []))[1].append((row, relation))
        slices: list[tuple[list[str], list[tuple[JudgmentRow, Any]]]] = []
        for focus_ids, site_relations in by_site.values():
            parts = max(
                -(-len(site_relations) // CROSS_SLICE_RELATIONS),
                -(-len(focus_ids) // CROSS_SLICE_FOCUS_ROWS),
                1,
            )
            for part in range(parts):
                piece = (
                    focus_ids[part::parts],
                    site_relations[part::parts],
                )
                last = slices[-1] if slices else None
                if (
                    last is not None
                    and len(last[0]) + len(piece[0]) <= CROSS_SLICE_FOCUS_ROWS
                    and len(last[1]) + len(piece[1]) <= CROSS_SLICE_RELATIONS
                ):
                    last[0].extend(piece[0])
                    last[1].extend(piece[1])
                else:
                    slices.append((list(piece[0]), list(piece[1])))
        for focus_ids, slice_relations in slices:
            batches.append(
                {
                    "batch_id": f"{entry['scope_id']}-{len(batches) + 1:02d}",
                    "scope_id": entry["scope_id"],
                    "scope": entry["scope"],
                    **entry["prefix"],
                    "relations": [_relation_entry(row, relation) for row, relation in slice_relations],
                    "focus_rows": focus_ids,
                }
            )
    return batches


JUDGE_GROUP_RECORDS = 60
GROUP_ROW_SPAN = 100


def section_groups(
    section_ids: Sequence[str], records: Mapping[str, int], *, budget_records: int
) -> list[list[str]]:
    """A module's sections in consecutive groups of about ``budget_records`` records each."""

    return _batches([(sid, records.get(sid, 0)) for sid in section_ids], budget_records) or [
        list(section_ids)
    ]


def writing_groups(
    module_id: str,
    sections: Sequence[Mapping[str, Any]],
    *,
    budget_chars: int,
) -> list[list[Mapping[str, Any]]]:
    """A module's sections in consecutive groups whose briefs fit one writer."""

    costs = [(str(index), _size(section)) for index, section in enumerate(sections)]
    return [
        [sections[int(key)] for key in group]
        for group in _batches(costs, budget_chars)
    ] or [[]]


__all__ = [
    "ATTENTION",
    "GROUP_ROW_SPAN",
    "JUDGE_GROUP_RECORDS",
    "section_groups",
    "PROJECT_LEVEL",
    "Sites",
    "brief_row",
    "compact",
    "compact_row",
    "finding_of",
    "object_batches",
    "project_map",
    "cross_batches",
    "row_line",
    "writing_groups",
]
