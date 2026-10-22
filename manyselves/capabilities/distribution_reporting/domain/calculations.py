"""Calculation layer over the measurements the fixed-column mappers read.

A value is computed only when every premise is present in the same evidence
record; otherwise the missing premise is recorded instead of a value.  No
threshold or rating is applied here: whether a value is acceptable is a
judgment made against a cited rule.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Calculation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^K-")
    evidence_ids: list[str] = Field(min_length=1)
    subject: str
    quantity: str
    value: float
    unit: str
    formula: str


class CalculationGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    subject: str
    quantity: str
    missing: list[str]


def _measurements(item: Any) -> dict[str, Any]:
    return {measurement.quantity: measurement for measurement in item.measurements}


def compute(items: Iterable[Any]) -> tuple[list[Calculation], list[CalculationGap]]:
    """Return computed values and the records whose premises are missing.

    - remaining capacity: transformer capacity × (1 − load rate);
    - the temperature rise of a measuring point needs the ambient temperature,
      which the mapper already computes or marks missing.
    """

    calculations: list[Calculation] = []
    gaps: list[CalculationGap] = []
    for item in items:
        values = _measurements(item)
        capacity = values.get("transformer_capacity")
        load_rate = values.get("load_rate")
        if capacity is not None and capacity.value is not None:
            if load_rate is not None and load_rate.value is not None:
                calculations.append(
                    Calculation(
                        id=f"K-{len(calculations) + 1:04d}",
                        evidence_ids=[item.id],
                        subject=item.subject,
                        quantity="remaining_capacity",
                        value=round(capacity.value * (1 - load_rate.value / 100), 1),
                        unit=capacity.unit,
                        formula="transformer_capacity × (1 − load_rate)",
                    )
                )
            else:
                gaps.append(
                    CalculationGap(
                        evidence_id=item.id,
                        subject=item.subject,
                        quantity="remaining_capacity",
                        missing=["load_rate"],
                    )
                )
        rise = values.get("temperature_rise")
        if rise is not None and rise.value is None:
            gaps.append(
                CalculationGap(
                    evidence_id=item.id,
                    subject=item.subject,
                    quantity="temperature_rise",
                    missing=[
                        quantity
                        for quantity in rise.derived_from
                        if values.get(quantity) is None or values[quantity].value is None
                    ]
                    or ["ambient_temperature"],
                )
            )
    return calculations, gaps


_STATUS = re.compile(r"^(OK|NG)", re.IGNORECASE)
_COLUMN = re.compile(r"^[A-Z]+")


def _label_and_value(fact: str) -> tuple[str, str] | None:
    head = fact.split("；", 1)[0]
    if "=" not in head:
        return None
    label, value = head.split("=", 1)
    return label.strip(), value.strip()


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def primary_measurement(item: Any) -> Any | None:
    """The measurement the record's own value comes from, if the mapper declared one."""

    value = _number(item.value)
    if value is None:
        return None
    return next(
        (
            m for m in item.measurements
            if m.value is not None and float(m.value) == value and m.unit == item.unit
        ),
        None,
    )


def _checkpoint(item: Any) -> tuple[str, str, str] | None:
    """(sheet, column, label) of the column the record's value was read from.

    The column and its header label come from the mapper's declaration of the
    measurement that holds the value; a record without one keeps its own
    source column and checkpoint field.
    """

    parsed = _label_and_value(item.fact)
    if parsed is None:
        return None
    sheet = item.source.sheet or ""
    primary = primary_measurement(item)
    if primary is not None and (match := _COLUMN.match(primary.source_cell or "")):
        return sheet, match.group(0), primary.label or item.checkpoint_field or parsed[0]
    return sheet, item.source.column or "", item.checkpoint_field or parsed[0]


def _cell(item: Any) -> tuple[str, str, str]:
    source = item.source
    return source.file_id, source.sheet or "", source.cell or item.id


def statistics(
    items: Iterable[Any], *, start: int = 1
) -> tuple[list[Calculation], list[str], dict[str, dict[str, Any]]]:
    """Counts, object lists and orderings per checkpoint, for the writers to cite.

    A checkpoint is one labelled column of one source sheet. Status columns get
    OK/NG counts with the objects named; numeric columns get the objects sorted
    by value, the largest-to-smallest ratio and each value as a multiple of the
    median. No threshold is applied.

    A record routed to several sections is one source cell: it is counted
    once.  Each group also gets an object list (S-id) the program writes into
    the prose where a writer places it.
    """

    groups: dict[tuple[str, str, str], dict[tuple[str, str, str], list[Any]]] = {}
    for item in items:
        key = _checkpoint(item)
        if key is None:
            continue
        groups.setdefault(key, {}).setdefault(_cell(item), []).append(item)
    calculations: list[Calculation] = []
    lines: list[str] = []
    lists: dict[str, dict[str, Any]] = {}

    def add(subject, quantity, value, unit, evidence_ids, formula) -> str:
        calc = Calculation(
            id=f"K-{start + len(calculations):04d}",
            evidence_ids=list(dict.fromkeys(evidence_ids)),
            subject=subject,
            quantity=quantity,
            value=value,
            unit=unit,
            formula=formula,
        )
        calculations.append(calc)
        return calc.id

    def listed(name: str, spec: dict[str, Any]) -> str:
        list_id = f"S-{len(lists) + 1:04d}"
        lists[list_id] = {"name": name, **spec}
        return list_id

    for (sheet, column, label), cells in groups.items():
        name = f"{label}（{sheet} {column}列）" if column else f"{label}（{sheet}）"
        # One record per source cell; every copy's id stays traceable.
        copies = {cell: [item.id for item in group] for cell, group in cells.items()}
        members = [group[0] for group in cells.values()]

        def ids(chosen: Iterable[Any]) -> list[str]:
            return [i for item in chosen for i in copies[_cell(item)]]

        by_status: dict[str, list[Any]] = {"OK": [], "NG": []}
        for item in members:
            _, value = _label_and_value(item.fact) or ("", "")
            if match := _STATUS.match(value):
                by_status[match.group(1).upper()].append(item)
        objects = list(dict.fromkeys(item.subject for item in members))
        if any(by_status.values()):
            records_id = add(name, "record_count", len(members), "个", ids(members), "记录条数")
            objects_id = add(name, "object_count", len(objects), "个", ids(members), "对象个数")
            list_id = listed(name, {
                "kind": "status",
                "evidence_ids": ids(members),
                "status": {
                    status: list(dict.fromkeys(item.subject for item in chosen))
                    for status, chosen in by_status.items()
                },
            })
            parts = [
                f"{name}[{list_id}]：共 {len(members)} 条记录[{records_id}]、"
                f"{len(objects)} 个对象[{objects_id}]"
            ]
            for status, chosen in by_status.items():
                subjects = list(dict.fromkeys(item.subject for item in chosen))
                if not chosen:
                    continue
                calc_id = add(
                    name, f"{status.lower()}_count", len(subjects), "个",
                    ids(chosen), f"{status} 对象：" + "、".join(subjects),
                )
                parts.append(f"{status} {len(subjects)} 个[{calc_id}]：" + "、".join(subjects))
            lines.append("；".join(parts))
            continue
        numeric = [item for item in members if _number(item.value) is not None and item.unit]
        units = {item.unit for item in numeric}
        if len(numeric) < 2 or len(units) != 1:
            continue
        unit = units.pop()
        ordered = sorted(numeric, key=lambda item: float(item.value), reverse=True)
        values = [float(item.value) for item in ordered]
        median = sorted(values)[len(values) // 2] if len(values) % 2 else (
            sorted(values)[len(values) // 2 - 1] + sorted(values)[len(values) // 2]
        ) / 2
        table = "、".join(f"{item.subject}={float(item.value):g}{unit}" for item in ordered)
        counted = list(dict.fromkeys(item.subject for item in numeric))
        list_id = listed(name, {
            "kind": "numeric",
            "evidence_ids": ids(numeric),
            "unit": unit,
            "entries": [
                {"subject": item.subject, "value": float(item.value), "evidence_id": item.id}
                for item in ordered
            ],
        })
        objects_id = add(name, "object_count", len(counted), "个", ids(numeric), "对象个数")
        line = f"{name}[{list_id}]：{len(counted)} 个对象[{objects_id}]，按数值从大到小 {table}"
        if values[-1] > 0:
            calc_id = add(
                name, "max_to_min_ratio", round(values[0] / values[-1], 1), "倍",
                ids([ordered[0], ordered[-1]]),
                f"{ordered[0].subject} {values[0]:g}{unit} / {ordered[-1].subject} {values[-1]:g}{unit}",
            )
            line += f"；最大/最小≈{values[0] / values[-1]:.1f} 倍[{calc_id}]"
        if values[1] > 0:
            calc_id = add(
                name, "max_to_second_ratio", round(values[0] / values[1], 1), "倍",
                ids([ordered[0], ordered[1]]),
                f"{ordered[0].subject} {values[0]:g}{unit} / {ordered[1].subject} {values[1]:g}{unit}",
            )
            line += f"；最大/次大≈{values[0] / values[1]:.1f} 倍[{calc_id}]"
        median_id = add(name, "median", round(median, 6), unit, ids(ordered), "中位数")
        line += f"；中位数 {median:g}{unit}[{median_id}]"
        if median > 0:
            multiples = []
            for item, value in zip(ordered, values, strict=True):
                calc_id = add(
                    item.subject, "multiple_of_median", round(value / median, 1), "倍",
                    ids([item]), f"{value:g}{unit} / 中位数 {median:g}{unit}",
                )
                multiples.append(f"{item.subject}≈{value / median:.1f} 倍[{calc_id}]")
            line += "；相对中位数：" + "、".join(multiples)
        lines.append(line)
    return calculations, lines, lists


def render_statistics(lines: list[str]) -> str:
    return "\n".join(
        [
            "# 程序统计（按检查项）",
            "以下数量、对象名单、排序和倍数由程序从事实逐条算出，同一源单元格只计一次。"
            "判断行写数量、分组、“几倍”“最高/最低”时只用这里的结果，计算编号填进 calculation_ids；"
            "正文里用占位符引用（见写作要求），不自己抄数。S- 是这一检查项的对象—数值清单。",
            *(lines or ["（无）"]),
        ]
    ) + "\n"


def render_calculations(
    calculations: list[Calculation], gaps: list[CalculationGap]
) -> str:
    """Plain text block for the shared context."""

    lines = [
        "# 计算结果",
        "以下由程序按固定列计算；只证明数值关系，是否超限要按引用的规则判断。",
        "每行为 [计算ID, 对象, 量, 值, 单位, 依据事实, 算式]。",
    ]
    lines.extend(
        f'["{c.id}","{c.subject}","{c.quantity}",{c.value:g},"{c.unit}",'
        f'"{"、".join(c.evidence_ids)}","{c.formula}"]'
        for c in calculations
    )
    if not calculations:
        lines.append("（没有可计算的项）")
    lines.append("\n# 缺少前提、不能计算的项")
    lines.extend(
        f'["{g.evidence_id}","{g.subject}","{g.quantity}","缺：{"、".join(g.missing)}"]'
        for g in gaps
    )
    if not gaps:
        lines.append("（无）")
    return "\n".join(lines) + "\n"


__all__ = [
    "primary_measurement",
    "Calculation",
    "CalculationGap",
    "compute",
    "render_calculations",
    "render_statistics",
    "statistics",
]
