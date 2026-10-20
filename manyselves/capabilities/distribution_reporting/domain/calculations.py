"""Calculation layer over the measurements the fixed-column mappers read.

A value is computed only when every premise is present in the same evidence
record; otherwise the missing premise is recorded instead of a value.  No
threshold or rating is applied here: whether a value is acceptable is a
judgment made against a cited rule.
"""

from __future__ import annotations

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


__all__ = ["Calculation", "CalculationGap", "compute", "render_calculations"]
