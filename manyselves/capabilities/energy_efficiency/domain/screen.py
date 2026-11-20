"""Screening of the loss items' rows: valid, invalid or pending, from the computed values and the preference rules alone.

Only rows are screened: those of the analyst's items, of a person's rules,
and of the large differences no item adopts (at most pending, S12); the
program's differences themselves are not.  The program runs the rules in order and
the first that holds decides; every
result carries its rule id and the numbers it read.  Nothing the model wrote
enters here: its opinion is shown beside the result, never in it.  A person's
mark in the workbook (取舍) wins over the screening.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .lenses import Candidate
from .preparation import ParameterSet

VALID, INVALID, PENDING = "有效", "无效", "待定"
RULES: dict[str, tuple[str, str]] = {
    "S1": (INVALID, "落在人确认的剩余规则（P40）里，扣掉以后低于电量下限"),
    "S2": (INVALID, "默认参照下的超出低于电量下限（P36）"),
    "S3": (INVALID, "默认参照下的超出低于本表全期电量 × 占比下限（P37）"),
    "S4": (PENDING, "账实不符：生产记录和电表对不上，先核数据"),
    "S5": (PENDING, "表的数据覆盖率低于下限（P38），数可能偏"),
    "S6": (PENDING, "最保守的参照下超出为 0：参照一换就没了"),
    "S10": (PENDING, "只在数据期的一半里出现：前后两半里较小的一半不到较大一半 × P44，可能是一次性的事或从某天起变了"),
    "S11": (PENDING, "规格的时段挪一个记录间隔，超出的变化超过 P45：时段写得太贴"),
    "S7": (PENDING, "可比组没人确认（A05 peer_group），只给方向"),
    "S8": (PENDING, "参照由模型提议、没人确认（确认后抄进偏好 P39）"),
    "S9": (PENDING, "费用视角只是上限，能不能挪要看排产约束"),
    "S12": (PENDING, "够大的程序差额，没有项采用（模型没提、人也没定规则）：只说明比参照多用了电，没人说明为什么像损耗"),
    "S0": (VALID, "以上规则都不成立"),
}


@dataclass
class ScreenContext:
    params: ParameterSet
    meter_kwh: dict[str, float]
    meter_coverage: dict[str, float]


def screen(candidate: Candidate, context: ScreenContext, effective_kwh: float, unclaimed: bool = False) -> dict[str, Any]:
    """The first rule that holds; ``effective_kwh`` is the default excess outside the person's residual rules.

    ``unclaimed``: the row of a large difference no item adopts, at most pending.
    """

    params = context.params
    minimum = float(params.value("screen_min_kwh"))
    share = float(params.value("screen_min_share"))
    coverage_floor = float(params.value("screen_min_coverage"))
    raw = candidate.excess
    energy = context.meter_kwh.get(candidate.meter, 0.0)
    coverage = context.meter_coverage.get(candidate.meter, 1.0)
    conservative = candidate.level(candidate.conservative)
    measured = {
        "excess_kwh": round(raw, 1),
        "effective_kwh": round(effective_kwh, 1),
        "meter_kwh": round(energy, 1),
        "share": round(effective_kwh / energy, 4) if energy > 0 else None,
        "coverage": round(coverage, 4),
        "conservative_kwh": None if conservative is None else conservative["excess_kwh"],
    }

    def result(rule: str) -> dict[str, Any]:
        outcome, text = RULES[rule]
        return {"result": outcome, "rule": rule, "reason": text, "measured": measured}

    if raw >= minimum and effective_kwh < minimum:
        return result("S1")
    if effective_kwh < minimum:
        return result("S2")
    if energy > 0 and effective_kwh < share * energy:
        return result("S3")
    if candidate.lens == "D1":
        return result("S4")
    if coverage < coverage_floor:
        return result("S5")
    if conservative is not None and conservative["excess_kwh"] <= 0:
        return result("S6")
    steady = candidate.details.get("stability") or {}
    measured["half_ratio"] = steady.get("half_ratio")
    if steady.get("half_ratio") is not None and steady["half_ratio"] < float(params.value("stability_half_share")):
        return result("S10")
    change = (steady.get("shift") or {}).get("change_share")
    measured["shift_change_share"] = change
    if change is not None and change > float(params.value("shift_change_share")):
        return result("S11")
    if candidate.lens == "L3" and not candidate.in_union:
        return result("S7")
    if candidate.source == "model":
        return result("S8")
    if candidate.lens == "L6":
        return result("S9")
    if unclaimed:
        return result("S12")
    return result("S0")


__all__ = ["INVALID", "PENDING", "RULES", "VALID", "ScreenContext", "screen"]
