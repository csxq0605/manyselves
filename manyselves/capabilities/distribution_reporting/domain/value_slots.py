"""Values in report prose are filled by the program, by slot.

A writer does not copy numbers.  Every value the input table declares (one
column of one record), every calculation and every per-checkpoint object list
gets a slot id; the prose names the slot and the program writes the value
with its unit when the report is composed:

- ``〔V-0001〕`` one declared value of one record, with its column's unit;
- ``〔K-0001〕`` one calculation with its unit, or a count as a bare number
  (the prose adds the measure word: 个, 台, 条);
- ``〔S-0001〕`` one checkpoint's object list, ``〔S-0001:NG〕`` only the
  objects with that status;
- ``〔V-0001:对象〕`` the object's name and its value together (a space
  between them), so values
  listed for several objects cannot be paired with the wrong one;
- ``〔S-0001:取值〕`` a numeric checkpoint's distinct values, smallest first;
- ``〔#:V-0003、V-0007〕`` the number of objects the listed slots belong to,
  for a count of objects the writer picked ("所列 N 个"), as distinct from a
  checkpoint's total, which is a ``K`` count.

Slots are recommended, not required: a writer may write any number, a limit,
a date or a figure of its own reasoning.  Only what the program can tie to
the table is checked, and only a structural certainty sends the text back: a
slot that does not exist, or one object's value slot written next to another
object alone.  A number the table can only suggest is wrong (a value with a
declared unit next to one object that is another object's value) is a hint
for the editor.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

VARIANTS = ("OK", "NG", "对象", "取值")
# Any suffix after the colon is recognised, so a slot written in a form the
# program does not know (〔V-0089:文体中心配电房/1D1〕) is still a slot: it is
# filled, checked and reported, never left raw in the report.
SLOT = re.compile(r"〔([VKS]-\d{4,})(?:[:：]([^〔〕\n]{1,80}))?〕")
_ID = r"[VKS]-\d{4,}"
COUNT = re.compile(rf"〔#[:：]({_ID}(?:\s*[,，、]\s*{_ID})*)〕")


def _count_ids(match: re.Match[str]) -> list[str]:
    return re.findall(_ID, match.group(1))


def strip_slots(text: str) -> str:
    return SLOT.sub("", COUNT.sub("", text))


EMPTY = "无"
UNRESOLVED = "（数值待核）"


def format_value(value: float, unit: str) -> str:
    return f"{float(value):g}{unit or ''}"


def slot_table(
    items: Sequence[Any],
    calculations: Sequence[Any],
    lists: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Slot id -> what the program writes and what it is, for every citable value.

    A record contributes one slot per declared measurement, and one for its
    own value when no measurement declares it.  A record routed to several
    sections is one source cell and gets its slots once.
    """

    slots: dict[str, dict[str, Any]] = {}
    seen: dict[tuple[Any, ...], str] = {}

    def add_value(item: Any, quantity: str, label: str, value: float, unit: str) -> None:
        source = item.source
        key = (source.file_id, source.sheet, source.cell, quantity)
        if key in seen:
            slots[seen[key]]["evidence_ids"].append(item.id)
            return
        slot_id = f"V-{len(seen) + 1:04d}"
        seen[key] = slot_id
        slots[slot_id] = {
            "text": format_value(value, unit),
            "subject": item.subject,
            "label": label,
            "value": float(value),
            "unit": unit or "",
            "evidence_ids": [item.id],
        }

    for item in items:
        declared = False
        for measurement in item.measurements:
            if measurement.value is None:
                continue
            add_value(
                item,
                measurement.quantity,
                measurement.label or measurement.quantity,
                float(measurement.value),
                measurement.unit,
            )
            declared = declared or (
                isinstance(item.value, (int, float))
                and float(measurement.value) == float(item.value)
                and measurement.unit == item.unit
            )
        if (
            not declared
            and isinstance(item.value, (int, float))
            and not isinstance(item.value, bool)
            and item.unit
        ):
            add_value(
                item, "value", item.checkpoint_field or item.fact.split("=", 1)[0],
                float(item.value), item.unit,
            )
    for calc in calculations:
        slots[calc.id] = {
            "text": format_value(calc.value, calc.unit),
            "subject": calc.subject,
            "label": calc.quantity,
            "value": float(calc.value),
            "unit": calc.unit or "",
            "evidence_ids": list(calc.evidence_ids),
        }
    for list_id, spec in lists.items():
        if spec.get("kind") == "numeric":
            entries = spec.get("entries") or []
            text = "、".join(
                f"{entry['subject']} {format_value(entry['value'], spec.get('unit') or '')}"
                for entry in entries
            )
            distinct = sorted({float(entry["value"]) for entry in entries})
            slots[list_id] = {
                "text": text,
                "label": spec.get("name", ""),
                "evidence_ids": list(spec.get("evidence_ids") or ()),
                "取值": "、".join(format_value(v, spec.get("unit") or "") for v in distinct),
            }
        else:
            status = spec.get("status") or {}
            slots[list_id] = {
                "text": "；".join(
                    f"{key} {'、'.join(subjects)}" for key, subjects in status.items() if subjects
                ),
                "label": spec.get("name", ""),
                "evidence_ids": list(spec.get("evidence_ids") or ()),
                **{key: "、".join(subjects) or EMPTY for key, subjects in status.items()},
            }
    return slots


def slot_lines(slot_ids: Iterable[str], slots: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """What each slot holds, as shown to a writer: 〔V-0001〕 -> 对象｜列名=值."""

    shown: dict[str, str] = {}
    for slot_id in dict.fromkeys(slot_ids):
        slot = slots.get(slot_id)
        if slot is None:
            continue
        subject = f"{slot['subject']}｜" if slot.get("subject") else ""
        shown[f"〔{slot_id}〕"] = f"{subject}{slot.get('label', '')}={slot['text']}"
    return shown


def slots_for_evidence(
    evidence_ids: Iterable[str], slots: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    wanted = set(evidence_ids)
    return [
        slot_id
        for slot_id, slot in slots.items()
        if wanted & set(slot.get("evidence_ids") or ())
    ]


def _plain_name(text: str) -> str:
    return re.sub(r"\s+", "", str(text))


def names_own_object(slot: Mapping[str, Any], variant: str) -> bool:
    """Whether a slot's suffix is its own object's name (the whole name or one part)."""

    subject = slot.get("subject")
    if not subject or not variant.strip():
        return False
    parts = {_plain_name(part) for part in str(subject).split("/") if part.strip()}
    wanted = [_plain_name(part) for part in variant.split("/") if part.strip()]
    return _plain_name(variant) == _plain_name(subject) or (
        bool(wanted) and all(part in parts for part in wanted)
    )


def variant_kind(slot: Mapping[str, Any], variant: str | None) -> str | None:
    """The variant a suffix stands for: a known one, 对象 for the slot's own name, else None."""

    if not variant:
        return ""
    if variant in VARIANTS:
        return variant
    if names_own_object(slot, variant):
        return "对象"
    return None


def _variant(slot: Mapping[str, Any], variant: str) -> str | None:
    """What a slot's variant writes, or None when this slot has no such variant."""

    kind = variant_kind(slot, variant)
    if kind == "对象":
        # A space keeps a cabinet number and its value apart ("D1701 186A").
        return f"{slot['subject']} {slot['text']}" if slot.get("subject") else None
    if kind is None:
        return None
    found = slot.get(kind)
    return None if found is None else str(found)


def fill_slots(
    text: str, slots: Mapping[str, Mapping[str, Any]], *, unresolved: str | None = None
) -> str:
    """Write each slot's value into the prose.

    A slot the program cannot fill is left as written; when the report is
    composed (``unresolved`` given) no raw slot reaches it: a slot that exists
    but is written with a suffix it does not have gets its plain value, and a
    slot that does not exist becomes ``unresolved``.
    """

    def value(match: re.Match[str]) -> str:
        slot = slots.get(match.group(1))
        if slot is None:
            return match.group(0) if unresolved is None else unresolved
        if match.group(2):
            filled = _variant(slot, match.group(2))
            if filled is None:
                return match.group(0) if unresolved is None else str(slot["text"])
            return filled
        return str(slot["text"])

    def count(match: re.Match[str]) -> str:
        ids = _count_ids(match)
        if any(slot_id not in slots for slot_id in ids):
            return match.group(0) if unresolved is None else unresolved
        subjects = {slots[slot_id].get("subject") or slot_id for slot_id in ids}
        return str(len(subjects))

    return SLOT.sub(value, COUNT.sub(count, text))


def slot_repairs(text: str, slots: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """What composing does to the slots it cannot fill as written, for the delivery report."""

    repairs = []
    for match in SLOT.finditer(text):
        slot = slots.get(match.group(1))
        if slot is None:
            repairs.append(f"{match.group(0)} 不存在，成稿写成“{UNRESOLVED}”")
        elif match.group(2) and _variant(slot, match.group(2)) is None:
            repairs.append(f"{match.group(0)} 的写法不对，成稿只填了数值 {slot['text']}")
    repairs += [
        f"{match.group(0)} 里有不存在的占位符，成稿写成“{UNRESOLVED}”"
        for match in COUNT.finditer(text)
        if any(slot_id not in slots for slot_id in _count_ids(match))
    ]
    return repairs


def slot_findings(text: str, slots: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Slots the program cannot fill: they would reach the report as written.

    These are the only slot problems that send a text back.  Numbers written
    outside slots are not checked here (see ``value_hints``).
    """

    problems = []
    for match in SLOT.finditer(text):
        slot = slots.get(match.group(1))
        if slot is None:
            problems.append(f"占位符 {match.group(0)} 不存在")
        elif match.group(2) and _variant(slot, match.group(2)) is None:
            problems.append(
                f"占位符 {match.group(0)} 没有“{match.group(2)}”这种写法"
                "（冒号后只能是 OK、NG、对象、取值，或这一格自己的对象名）"
            )
    problems += [
        f"计数占位符 {match.group(0)} 里有不存在的占位符"
        for match in COUNT.finditer(text)
        if any(slot_id not in slots for slot_id in _count_ids(match))
    ]
    return problems


def empty_status_hints(text: str, slots: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """A status list cited for a status no object has: the prose will read "无"."""

    return [
        f"{match.group(0)} 这一检查项没有 {match.group(2)} 对象，成稿会写成“{EMPTY}”，请确认句子仍通顺"
        for match in SLOT.finditer(text)
        if match.group(2) in {"OK", "NG"}
        and (slot := slots.get(match.group(1))) is not None
        and slot.get(match.group(2)) == EMPTY
    ]


def _units(slots: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """The units the table declares for its values, longest first."""

    units = {
        str(slot.get("unit"))
        for slot_id, slot in slots.items()
        if slot_id.startswith("V-") and slot.get("unit")
    }
    return sorted(units, key=len, reverse=True)


def value_hints(text: str, slots: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """A written value next to one object, in a column's unit, that is another object's value.

    Only what the table pins down is compared: a number followed by a unit a
    column declares, in a sentence that names exactly one object.  A number
    that is that object's own value, or nobody's (a limit, a computed figure),
    passes.  The result is a hint; the editor decides.
    """

    units = _units(slots)
    if not units:
        return []
    by_subject: dict[str, dict[str, set[float]]] = {}
    for slot_id, slot in slots.items():
        if slot_id.startswith("V-") and slot.get("subject") and "value" in slot:
            by_subject.setdefault(str(slot["subject"]), {}).setdefault(
                str(slot.get("unit") or ""), set()
            ).add(round(float(slot["value"]), 6))
    number = re.compile(
        r"(?<![\d.])(\d+(?:\.\d+)?)\s*(" + "|".join(re.escape(u) for u in units) + ")"
    )
    hints = []
    for sentence in _SENTENCE.findall(strip_slots(text)):
        named = _named(sentence, by_subject)
        if len(named) != 1:
            continue
        (subject,) = named
        own = by_subject[subject]
        for match in number.finditer(sentence):
            value, unit = round(float(match.group(1)), 6), match.group(2)
            if value in own.get(unit, set()):
                continue
            others = [s for s, values in by_subject.items() if s != subject and value in values.get(unit, set())]
            if others:
                mine = "、".join(f"{v:g}{unit}" for v in sorted(own.get(unit, set()))) or "无此列数值"
                hints.append(
                    f"{subject} 旁写的 {match.group(0)} 是 {others[0]} 的值（{subject} 记录为 {mine}）"
                )
    return hints


def _named(sentence: str, subjects: Iterable[str]) -> set[str]:
    """Objects a sentence names: every part of the name (房间/柜号) is in it.

    A part alone ("变压器", "3#") is shared by objects in many rooms, so it does
    not pin one down.  Spaces are ignored on both sides ("16 号楼").
    """

    plain = re.sub(r"\s+", "", sentence)
    named = set()
    for subject in subjects:
        parts = [re.sub(r"\s+", "", part) for part in str(subject).split("/") if part.strip()]
        if parts and all(part in plain for part in parts):
            named.add(subject)
    return named


_SENTENCE = re.compile(r"[^。；;\n]+")
_QUOTE = re.compile(r"「([^「」]+)」")


def mispaired_slots(text: str, slots: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """A value slot written next to one object that holds another object's value."""

    subjects = {
        str(slot["subject"])
        for slot in slots.values()
        if slot.get("subject")
    }
    problems = []
    for sentence in _SENTENCE.findall(text):
        named = _named(sentence, subjects)
        if len(named) != 1:
            continue
        (subject,) = named
        for match in SLOT.finditer(sentence):
            slot = slots.get(match.group(1))
            if (
                slot
                and match.group(1).startswith("V-")
                and variant_kind(slot, match.group(2)) != "对象"
                and slot.get("subject")
                and slot["subject"] != subject
                and slot["subject"] not in sentence
            ):
                problems.append(f"{subject} 旁写的 {match.group(0)} 是 {slot['subject']} 的值")
    return problems


def quote_findings(text: str, sources: Iterable[str]) -> list[str]:
    """Text inside 「」 must be the source author's own words, as recorded."""

    pool = [" ".join(str(source).split()) for source in sources]
    return [
        f"「{quoted}」"
        for quoted in _QUOTE.findall(text)
        if not any(" ".join(quoted.split()) in source for source in pool)
    ]


def object_names(subjects: Iterable[str]) -> list[str]:
    """Object names and each of their parts (房间/柜号 -> both)."""

    names: list[str] = []
    for subject in subjects:
        parts = [part.strip() for part in str(subject).split("/") if part.strip()]
        names.extend([str(subject), *parts])
    return list(dict.fromkeys(names))


__all__ = [
    "COUNT",
    "EMPTY",
    "SLOT",
    "UNRESOLVED",
    "VARIANTS",
    "empty_status_hints",
    "value_hints",
    "strip_slots",
    "fill_slots",
    "format_value",
    "mispaired_slots",
    "object_names",
    "quote_findings",
    "slot_findings",
    "names_own_object",
    "slot_lines",
    "slot_repairs",
    "slot_table",
    "variant_kind",
    "slots_for_evidence",
]
