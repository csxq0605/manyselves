"""Values in report prose are filled by the program, by slot.

A writer does not copy numbers.  Every value the input table declares (one
column of one record), every calculation and every per-checkpoint object list
gets a slot id; the prose names the slot and the program writes the value
with its unit when the report is composed:

- ``〔V-0001〕`` one declared value of one record, with its column's unit;
- ``〔K-0001〕`` one calculation or count, with its unit;
- ``〔S-0001〕`` one checkpoint's object list, ``〔S-0001:NG〕`` only the
  objects with that status;
- ``〔#:V-0003、V-0007〕`` the number of objects the listed slots belong to,
  for a count of objects the writer picked ("所列 N 个"), as distinct from a
  checkpoint's total, which is a ``K`` count.

The check is structural: a slot must exist, and an Arabic numeral left in the
prose outside a slot must be part of something the program already knows to
carry digits (an object's name, a section number, a list marker, a standard's
code, the request's own text, a row's measure or timeframe).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

SLOT = re.compile(r"〔([VKS]-\d{4,})(?:[:：](OK|NG))?〕")
_ID = r"[VKS]-\d{4,}"
COUNT = re.compile(rf"〔#[:：]({_ID}(?:\s*[,，、]\s*{_ID})*)〕")


def _count_ids(match: re.Match[str]) -> list[str]:
    return re.findall(_ID, match.group(1))


def strip_slots(text: str) -> str:
    return SLOT.sub("", COUNT.sub("", text))
_DIGITS = re.compile(r"\d")
# A section number (2.4.1), a list marker at a line start, a chapter reference.
_SECTION_NUMBER = re.compile(r"(?<![\d.])\d+(?:\.\d+)+(?![\d.])")
_LIST_MARKER = re.compile(r"^\s*\d+[.、)）]\s*", re.MULTILINE)
_CHAPTER = re.compile(r"第\s*\d+\s*[章节]")


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
            "evidence_ids": list(calc.evidence_ids),
        }
    for list_id, spec in lists.items():
        if spec.get("kind") == "numeric":
            entries = spec.get("entries") or []
            text = "、".join(
                f"{entry['subject']}{format_value(entry['value'], spec.get('unit') or '')}"
                for entry in entries
            )
            slots[list_id] = {
                "text": text,
                "label": spec.get("name", ""),
                "evidence_ids": list(spec.get("evidence_ids") or ()),
            }
        else:
            status = spec.get("status") or {}
            slots[list_id] = {
                "text": "；".join(
                    f"{key} {'、'.join(subjects)}" for key, subjects in status.items() if subjects
                ),
                "label": spec.get("name", ""),
                "evidence_ids": list(spec.get("evidence_ids") or ()),
                **{key: "、".join(subjects) for key, subjects in status.items()},
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


def fill_slots(text: str, slots: Mapping[str, Mapping[str, Any]]) -> str:
    """Write each slot's value into the prose; an unknown slot is left as written."""

    def value(match: re.Match[str]) -> str:
        slot = slots.get(match.group(1))
        if slot is None:
            return match.group(0)
        if match.group(2):
            return str(slot.get(match.group(2), ""))
        return str(slot["text"])

    def count(match: re.Match[str]) -> str:
        ids = _count_ids(match)
        if any(slot_id not in slots for slot_id in ids):
            return match.group(0)
        subjects = {slots[slot_id].get("subject") or slot_id for slot_id in ids}
        return str(len(subjects))

    return SLOT.sub(value, COUNT.sub(count, text))


def slot_findings(
    text: str,
    slots: Mapping[str, Mapping[str, Any]],
    *,
    names: Iterable[str] = (),
    section_ids: Iterable[str] = (),
    digit_texts: Iterable[str] = (),
) -> list[str]:
    """Unknown slots, and Arabic numerals written outside slots.

    ``names`` are object names (and their parts); ``digit_texts`` are texts the
    prose may repeat as they are (standard codes, the request, a row's measure
    and timeframe).  Their digits are not values the writer computed.
    """

    problems = [
        f"占位符 {match.group(0)} 不存在"
        for match in SLOT.finditer(text)
        if match.group(1) not in slots
        or (match.group(2) and match.group(2) not in slots[match.group(1)])
    ]
    problems += [
        f"计数占位符 {match.group(0)} 里有不存在的占位符"
        for match in COUNT.finditer(text)
        if any(slot_id not in slots for slot_id in _count_ids(match))
    ]
    rest = strip_slots(text)
    known_sections = set(section_ids)
    rest = _SECTION_NUMBER.sub(
        lambda m: "" if m.group(0) in known_sections else m.group(0), rest
    )
    rest = _LIST_MARKER.sub("", rest)
    rest = _CHAPTER.sub("", rest)
    for known in sorted(
        {t.strip() for t in [*names, *digit_texts] if t and _DIGITS.search(t)},
        key=len,
        reverse=True,
    ):
        rest = rest.replace(known, "")
    left = sorted(set(re.findall(r"[\d.]*\d[\d.]*\s*[A-Za-z%℃‰个台处倍]*", rest)))
    if left:
        problems.append(
            "正文里有不是占位符的阿拉伯数字：" + "、".join(left[:12])
            + ("等" if len(left) > 12 else "")
        )
    return problems


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
        named = {
            s for s in subjects
            if s in sentence or (s.rsplit("/", 1)[-1].strip() or s) in sentence
        }
        if len(named) != 1:
            continue
        (subject,) = named
        for match in SLOT.finditer(sentence):
            slot = slots.get(match.group(1))
            if (
                slot
                and match.group(1).startswith("V-")
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
    "SLOT",
    "strip_slots",
    "fill_slots",
    "format_value",
    "mispaired_slots",
    "object_names",
    "quote_findings",
    "slot_findings",
    "slot_lines",
    "slot_table",
    "slots_for_evidence",
]
