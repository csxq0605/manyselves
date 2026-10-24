"""Chapters 1 and 3, cut into small packets for a fact check.

One editor reading both chapters against every cited row misses things: it
has too much in front of it, and the tables drawn from the chief's structured
fields never reach it.  The program cuts the chapters into units (a paragraph,
a table row, a risk, an action group), gives each unit what it needs (the rows
it cites or names, their source records, the values and counts of the columns
it names) and packs a few units per packet.  A checker sees one packet in a
fresh conversation and checks facts only.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..domain.judgment_ledger import JudgmentRow
from ..domain.value_slots import fill_slots, slot_lines, slots_for_evidence
from .grounds import GROUND_ID, grounded

# A packet holds at most this many units and about this many characters.
MAX_UNITS = 8
MAX_CHARS = 15000
# A row's basis is cut to this many characters in a packet; a hazard, action
# or gap to the shorter length.  Source records are given whole: a checker
# that sees half a record may "correct" a value from the half it lacks.
MAX_TEXT = 300
SHORT_TEXT = 120

# At most this many other places resting on the same row are named with a finding.
SAME_ELSEWHERE = 10

_CHAPTER_1 = "1"
_CHAPTER_3 = "3"


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in str(text or "").split("\n") if p.strip()]


def chief_units(
    chapter1: Mapping[str, Any] | None,
    chapter3: Mapping[str, Any] | None,
    groups: Sequence[Mapping[str, Any]],
    edited: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Every paragraph, table row, risk and action group of chapters 1 and 3.

    ``row_ids`` are the rows a structured unit cites itself; a paragraph
    cites none and gets the rows of the objects it names.  ``edited`` holds
    the text sections an editor's revision rewrote: 1.1-1.3 replace their
    chapter 1 fields, and a rewritten 3.1 is checked paragraph by paragraph
    in place of the structured layers it was drawn from.
    """

    edited = dict(edited or {})

    units: list[dict[str, Any]] = []

    def add(unit_id: str, chapter: str, where: str, text: str, *, row_ids=(), module_id=None):
        if str(text).strip():
            units.append({
                "unit_id": unit_id, "chapter": chapter, "where": where, "text": str(text),
                "row_ids": list(row_ids), "module_id": module_id,
            })

    one = chapter1 or {}
    for field, section in (
        ("assessment_background", "1.1"), ("overall_assessment", "1.2"),
        ("regional_executive_summary", "1.3"),
    ):
        for n, paragraph in enumerate(_paragraphs(edited.get(section, one.get(field, ""))), 1):
            add(f"{section}-段{n}", _CHAPTER_1, f"{section} 第{n}段", paragraph)
    for health in one.get("dimension_health") or []:
        add(
            f"1.2-健康度-{health.get('module_id')}", _CHAPTER_1, "1.2 健康度表",
            f"{health.get('status', '')}｜{health.get('key_indicators', '')}｜风险等级 {health.get('risk_level', '')}",
            module_id=health.get("module_id"),
        )
    for n, risk in enumerate(one.get("key_risks") or [], 1):
        add(
            f"1.2-重点风险-{n}", _CHAPTER_1, "1.2 重点风险表",
            f"{risk.get('description', '')}｜危害：{risk.get('hazard', '')}｜建议：{risk.get('action', '')}",
            row_ids=risk.get("row_ids") or (),
        )
    for n, phase in enumerate(one.get("phases") or [], 1):
        add(
            f"1.2-阶段-{n}", _CHAPTER_1, "1.2 实施阶段",
            f"{phase.get('phase', '')}（{phase.get('time', '')}）：{phase.get('tasks', '')}；{phase.get('effect', '')}",
        )
    three = dict(chapter3 or {})
    if "3.1" in edited:
        for n, paragraph in enumerate(_paragraphs(edited["3.1"]), 1):
            add(f"3.1-段{n}", _CHAPTER_3, f"3.1 第{n}段", paragraph)
        three = {**three, "risk_summary": [], "risk_details": [], "cross_findings": []}
    for n, line in enumerate(three.get("risk_summary") or [], 1):
        add(f"3.1-汇总-{n}", _CHAPTER_3, "3.1 风险汇总", line)
    for details in three.get("risk_details") or []:
        for n, point in enumerate(details.get("points") or [], 1):
            add(
                f"3.1-详述-{details.get('module_id')}-{n}", _CHAPTER_3, "3.1 详述", point,
                module_id=details.get("module_id"),
            )
    for n, finding in enumerate(three.get("cross_findings") or [], 1):
        add(
            f"3.1-综合-{n}", _CHAPTER_3, "3.1 跨对象综合结论",
            f"{finding.get('subject', '')}：{finding.get('conclusion', '')}",
            row_ids=finding.get("row_ids") or (),
        )
    by_group = {group["group_id"]: group for group in groups}
    for action in three.get("actions") or []:
        group = by_group.get(action.get("group_id"))
        add(
            f"3.2-{action.get('group_id')}", _CHAPTER_3, "3.2 改善行动速查表",
            f"发现问题：{action.get('problem', '')}｜风险与危害：{action.get('hazard', '')}"
            f"｜解决方案：{action.get('solution', '')}"
            + (f"｜备注：{action['note']}" if action.get("note") else "")
            + (f"｜过渡安排：{action['interim']}" if action.get("interim") else ""),
            row_ids=(group or {}).get("row_ids") or (),
        )
    return units


_TEXT_FIELDS = {"1.1": "assessment_background", "1.2": "overall_assessment", "1.3": "regional_executive_summary"}


def chief_pieces(chapter1: Mapping[str, Any] | None, chapter3: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The editable pieces of chapters 1 and 3 under the place ids of ``chief_units``.

    ``units`` are text: the paragraphs of 1.1-1.3 and the 3.1 summary and
    detail lines.  The others are the structured items as they stand.
    """

    one, three = dict(chapter1 or {}), dict(chapter3 or {})
    units: dict[str, str] = {}
    for section, field in _TEXT_FIELDS.items():
        for n, paragraph in enumerate(_paragraphs(one.get(field, "")), 1):
            units[f"{section}-段{n}"] = paragraph
    for n, line in enumerate(three.get("risk_summary") or [], 1):
        units[f"3.1-汇总-{n}"] = line
    for details in three.get("risk_details") or []:
        for n, point in enumerate(details.get("points") or [], 1):
            units[f"3.1-详述-{details.get('module_id')}-{n}"] = point
    return {
        "units": units,
        "health": {f"1.2-健康度-{h.get('module_id')}": h for h in one.get("dimension_health") or []},
        "key_risks": {f"1.2-重点风险-{n}": r for n, r in enumerate(one.get("key_risks") or [], 1)},
        "phases": {f"1.2-阶段-{n}": p for n, p in enumerate(one.get("phases") or [], 1)},
        "cross_findings": {f"3.1-综合-{n}": f for n, f in enumerate(three.get("cross_findings") or [], 1)},
    }


def place_chief_fix(
    chapter1: Mapping[str, Any], chapter3: Mapping[str, Any], fix: Any
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Put each piece of the chief's fix back where its place id points.

    Returns the two chapters and the place ids that point nowhere.
    """

    one = json.loads(json.dumps(chapter1 or {}, ensure_ascii=False))
    three = json.loads(json.dumps(chapter3 or {}, ensure_ascii=False))
    unplaced: list[str] = []

    def put(items: list[Any], n: int, value: Any, place: str) -> None:
        if 1 <= n <= len(items):
            items[n - 1] = value
        else:
            unplaced.append(place)

    for place, text in (fix.units or {}).items():
        text = str(text).strip()
        if match := re.fullmatch(r"(1\.[123])-段(\d+)", place):
            field = _TEXT_FIELDS[match.group(1)]
            paragraphs = _paragraphs(one.get(field, ""))
            n = int(match.group(2))
            if 1 <= n <= len(paragraphs) and text:
                paragraphs[n - 1] = text
                one[field] = ("\n\n" if "\n\n" in str(one.get(field, "")) else "\n").join(paragraphs)
            else:
                unplaced.append(place)
        elif match := re.fullmatch(r"3\.1-汇总-(\d+)", place):
            put(three.setdefault("risk_summary", []), int(match.group(1)), text, place)
        elif match := re.fullmatch(r"3\.1-详述-(.+)-(\d+)", place):
            details = next(
                (d for d in three.get("risk_details") or [] if str(d.get("module_id")) == match.group(1)), None
            )
            if details is None:
                unplaced.append(place)
            else:
                put(details.setdefault("points", []), int(match.group(2)), text, place)
        else:
            unplaced.append(place)
    health = {str(h.get("module_id")): h for h in one.get("dimension_health") or []}
    for row in fix.health or []:
        dumped = row.model_dump(mode="json")
        if str(dumped.get("module_id")) in health:
            health[str(dumped["module_id"])].clear()
            health[str(dumped["module_id"])].update(dumped)
        else:
            unplaced.append(f"1.2-健康度-{dumped.get('module_id')}")
    for field, chapter, prefix, items_key in (
        ("key_risks", one, "1.2-重点风险-", "key_risks"),
        ("phases", one, "1.2-阶段-", "phases"),
        ("cross_findings", three, "3.1-综合-", "cross_findings"),
    ):
        for place, item in (getattr(fix, field) or {}).items():
            n = place[len(prefix):] if place.startswith(prefix) else ""
            if n.isdigit():
                put(chapter.setdefault(items_key, []), int(n), item.model_dump(mode="json"), place)
            else:
                unplaced.append(place)
    patches = {action.group_id: action.model_dump(mode="json") for action in fix.actions or []}
    known = {str(a.get("group_id")) for a in three.get("actions") or []}
    three["actions"] = [patches.get(str(a.get("group_id")), a) for a in three.get("actions") or []]
    unplaced += [f"3.2-{g}" for g in sorted(set(patches) - known)]
    if str(fix.action_lead or "").strip():
        three["action_lead"] = fix.action_lead
    return one, three, unplaced


def _named(text: str, names: Iterable[str]) -> list[str]:
    """Names whose every part (房间/柜号) is in the text, spaces ignored."""

    plain = re.sub(r"\s+", "", text)
    found = []
    for name in names:
        parts = [re.sub(r"\s+", "", p) for p in str(name).split("/") if p.strip()]
        if parts and all(part in plain for part in parts):
            found.append(name)
    return found


def _column(label: str) -> str:
    return re.split(r"[（(]", str(label), maxsplit=1)[0].strip()


def _cut(text: Any, limit: int = MAX_TEXT) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _row_view(row: JudgmentRow) -> dict[str, Any]:
    """What a fact check needs of a row: its object, conclusion and strength.

    The values themselves come with the row's records.  The basis is not
    checked, but without it a checker cannot tell which value a row rests on
    and may "correct" a unit from the wrong record.
    """

    view = {
        "id": row.id,
        "object": row.object,
        "checkpoint": row.checkpoint,
        "disposition": row.disposition,
        "evidence_status": row.evidence_status,
        "evidence_ids": list(row.evidence_ids),
        "basis": _cut(row.basis),
    }
    if row.finding:
        view["finding"] = _cut(row.finding)
    if row.risk_level:
        view["risk_level"] = row.risk_level
    if row.hazard:
        view["hazard"] = _cut(row.hazard, SHORT_TEXT)
    if row.action is not None:
        view["action"] = _cut(f"{row.action.measure}（{row.action.timeframe}）", SHORT_TEXT)
    if row.gap is not None:
        view["gap"] = _cut(row.gap.missing, SHORT_TEXT)
    return view


def chief_fact_packets(
    *,
    chapter1: Mapping[str, Any] | None,
    chapter3: Mapping[str, Any] | None,
    ledger: Mapping[str, Sequence[JudgmentRow]],
    groups: Sequence[Mapping[str, Any]],
    slots: Mapping[str, Mapping[str, Any]],
    fact_texts: Mapping[str, str],
    run_id: str,
    edited: Mapping[str, str] | None = None,
    max_units: int = MAX_UNITS,
    max_chars: int = MAX_CHARS,
    prefix: str = "F-",
) -> list[dict[str, Any]]:
    """The fact-check packets: units with their rows, records, values and counts.

    ``prefix`` starts every packet id, so packets planned in separate batches
    (chapter 3 while chapter 1 is written, then chapter 1) never share one.
    """

    rows = {row.id: row for module_rows in ledger.values() for row in module_rows}
    module_of = {row.id: m for m, module_rows in ledger.items() for row in module_rows}
    # A named object brings its rows with a problem or a gap; a unit's own
    # row_ids bring every row they cite.
    by_object: dict[str, list[str]] = {}
    for row in rows.values():
        if row.disposition in {"concern", "undetermined"}:
            by_object.setdefault(row.object, []).append(row.id)
    # Each column's counts and distinct values, found by the column's header
    # label in the text or by the evidence of the rows a unit rests on.
    columns: dict[str, list[str]] = {}
    column_evidence: dict[str, set[str]] = {}
    lists: dict[str, str] = {}
    for slot_id, slot in slots.items():
        label = str(slot.get("label", ""))
        if slot_id.startswith("K-") and label.endswith("_count"):
            column = _column(slot.get("subject") or "")
            columns.setdefault(column, []).append(slot_id)
            column_evidence.setdefault(column, set()).update(slot.get("evidence_ids") or ())
        elif slot_id.startswith("S-"):
            lists[_column(label)] = slot_id
    columns.pop("", None)
    module_evidence = {
        m: {e for row in module_rows for e in row.evidence_ids} for m, module_rows in ledger.items()
    }

    prepared = []
    for unit in chief_units(chapter1, chapter3, groups, edited):
        text = fill_slots(unit["text"], slots)
        # A unit that cites rows is held against them; a paragraph cites none
        # and is held against the rows of the objects it names.
        cited = [r for r in unit["row_ids"] if r in rows]
        row_ids = cited or list(dict.fromkeys(
            r for name in _named(text, by_object) for r in by_object[name]
        ))
        evidence = [e for r in row_ids for e in rows[r].evidence_ids]
        # A unit that rests on no row (a dimension's health) is held against
        # its whole dimension's columns.
        resting = set(evidence) or module_evidence.get(unit["module_id"] or "", set())
        named_columns = [column for column in columns if column in text]
        used = [
            column for column in columns
            if column in named_columns or column_evidence.get(column, set()) & resting
        ]
        lines = slot_lines([s for column in used for s in columns[column]], slots)
        for column in used:
            slot_id = lists.get(column)
            if slot_id is None:
                continue
            if column in named_columns:
                lines.update(slot_lines([slot_id], slots))
            if slots[slot_id].get("取值"):
                lines[f"〔{slot_id}:取值〕"] = f"{column}的不同取值={slots[slot_id]['取值']}"
        values = slot_lines(
            [s for s in slots_for_evidence(evidence, slots) if s.startswith("V-")], slots
        )
        module = unit["module_id"] or (
            Counter(module_of[r] for r in row_ids).most_common(1)[0][0] if row_ids else "综合"
        )
        prepared.append({
            "unit": {
                "unit_id": unit["unit_id"], "where": unit["where"], "text": text,
                "row_ids": row_ids, "counts": list(lines),
            },
            "counts": lines,
            "module": module,
            "rows": row_ids,
            "evidence": list(dict.fromkeys(evidence)),
            "values": values,
        })

    # Within a dimension, units resting on the same rows sit next to each
    # other, so a packet carries each row and its records once.
    order = sorted(
        range(len(prepared)),
        key=lambda i: (prepared[i]["module"], min(prepared[i]["rows"], default="~"), i),
    )
    packets: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def size(packet: Mapping[str, Any]) -> int:
        return len(json.dumps(packet, ensure_ascii=False))

    def new(module: str) -> dict[str, Any]:
        return {
            "run_id": run_id, "packet_id": f"{prefix}{len(packets) + 1:02d}", "module": module,
            "units": [], "rows": {}, "facts": {}, "values": {}, "counts": {},
        }

    for index in order:
        item = prepared[index]
        # Units are taken in dimension order, so a packet's rows overlap; a
        # packet closes on its unit count or its size, not on a dimension.
        if current is None or len(current["units"]) >= max_units:
            current = new(item["module"])
            packets.append(current)
        trial = {
            **current,
            "units": [*current["units"], item["unit"]],
            "rows": {**current["rows"], **{r: _row_view(rows[r]) for r in item["rows"]}},
            "facts": {**current["facts"], **{e: str(fact_texts.get(e, "")) for e in item["evidence"]}},
            "values": {**current["values"], **item["values"]},
            "counts": {**current["counts"], **item["counts"]},
        }
        if current["units"] and size(trial) > max_chars:
            current = new(item["module"])
            packets.append(current)
            trial = {
                **current,
                "units": [item["unit"]],
                "rows": {r: _row_view(rows[r]) for r in item["rows"]},
                "facts": {e: str(fact_texts.get(e, "")) for e in item["evidence"]},
                "values": dict(item["values"]),
                "counts": dict(item["counts"]),
            }
        current.update(trial)
    for packet in packets:
        packet["rows"] = list(packet["rows"].values())
    return packets


def packet_material(packet: Mapping[str, Any]) -> dict[str, str]:
    """A packet's material by id: its rows, records, values and counts."""

    material: dict[str, str] = {}
    for row in packet.get("rows") or []:
        material[row["id"]] = "｜".join(
            str(row.get(key, "")) for key in (
                "object", "checkpoint", "disposition", "evidence_status", "risk_level", "basis",
            ) if row.get(key)
        )
    material.update({e: str(text) for e, text in (packet.get("facts") or {}).items()})
    for key, line in {**(packet.get("values") or {}), **(packet.get("counts") or {})}.items():
        for slot_id in GROUND_ID.findall(str(key)):
            material[slot_id] = f"{material[slot_id]}；{line}" if slot_id in material else str(line)
    return material


def apply_fact_findings(
    packets: Sequence[Mapping[str, Any]],
    results: Mapping[str, Any],
    ledger_material: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The checkers' findings on chapters 1 and 3, for the chief's one revision.

    Each finding is held to the packet it was written on: a blocking finding
    citing nothing of that packet cannot be checked and becomes minor
    (see ``grounds``).  A packet whose check failed is left out; a finding on
    a unit the packet does not hold is ignored.  ``ledger_material`` (every
    row and record of the ledger) grounds a finding on a row the checker found
    through the project map rather than in its packet.
    """

    # Where each unit sits and which rows it rests on, across all packets, so
    # a fix in one place can be carried to the others resting on the same row.
    rests = {
        unit["unit_id"]: (unit["where"], set(unit.get("row_ids") or ()))
        for packet in packets
        for unit in packet["units"]
    }
    findings: dict[str, Any] = {}
    row_fixes: dict[str, list[dict[str, str]]] = {}
    for packet in packets:
        result = results.get(packet["packet_id"])
        found = (result or {}).get("findings") if isinstance(result, Mapping) else None
        if not found:
            continue
        where = {unit["unit_id"]: unit["where"] for unit in packet["units"]}
        for unit_id, finding in grounded(
            {u: f for u, f in found.items() if u in where},
            {**(ledger_material or {}), **packet_material(packet)},
        ).items():
            rows = {g for g in finding.get("grounds") or {} if g.startswith("J-")}
            elsewhere = list(dict.fromkeys(
                f"{place}（{other}）"
                for other, (place, rested) in rests.items()
                if other != unit_id and rows & rested
            ))
            held = {row["id"] for row in packet["rows"]}
            also = [
                r for r in dict.fromkeys(finding.pop("rows_also", None) or ())
                if r in held
            ] if finding.get("severity") == "blocking" else []
            finding.pop("rows_also", None)
            for row_id in also:
                row_fixes.setdefault(row_id, []).append({
                    "problem": f"{where[unit_id]}写的说法，判断行自己也这样写：{finding.get('problem', '')}",
                    "suggestion": finding.get("suggestion", ""),
                })
            findings[unit_id] = {
                **finding,
                "problem": f"{where[unit_id]}：{finding.get('problem', '')}",
                "suggestion": finding.get("suggestion", ""),
                **({"same_elsewhere": elsewhere[:SAME_ELSEWHERE]} if elsewhere else {}),
            }
    blocking = any(f.get("severity") == "blocking" for f in findings.values())
    return {
        "check": {"findings": findings, "blocking": blocking},
        "blocking": blocking,
        # Errors the cited rows carry themselves: their authors fix the rows,
        # so the problem tables and chapter 2 change with chapters 1 and 3.
        "row_fixes": {
            row_id: {
                "problem": "；".join(f["problem"] for f in found),
                "severity": "blocking",
                "suggestion": "；".join(f["suggestion"] for f in found if f["suggestion"]),
            }
            for row_id, found in row_fixes.items()
        },
    }


__all__ = [
    "chief_pieces",
    "place_chief_fix",
    "MAX_CHARS", "MAX_UNITS", "apply_fact_findings", "chief_fact_packets", "chief_units",
    "packet_material",
]
