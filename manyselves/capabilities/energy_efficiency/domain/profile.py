"""A compact picture of each meter for the model and the register."""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any

from .inputs import MeterQuality, MeterSpec
from .methods import base_power
from .series import MeterSeries, days_between

DAILY_ROWS_LIMIT = 62


def _r(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


def meter_profile(
    spec: MeterSpec,
    series: MeterSeries,
    quality: MeterQuality,
    period: tuple[datetime, datetime],
) -> dict[str, Any]:
    start, end = period
    whole = series.measure(start, end)
    days = []
    for day in days_between(start, end):
        begin, finish = max(day, start), min(day + timedelta(days=1), end)
        measure = series.measure(begin, finish)
        # Coverage of the whole calendar day, so a day the period cuts short
        # does not read as a full day with little use.
        days.append(
            {
                "date": day.date().isoformat(),
                "energy_kwh": _r(measure.energy_kwh),
                "avg_kw": _r(measure.avg_kw),
                "coverage": round(measure.covered_minutes / (24 * 60), 3),
                "from": begin.strftime("%H:%M") if begin > day else None,
                "until": finish.strftime("%H:%M") if finish < day + timedelta(days=1) else None,
            }
        )
    hours: dict[str, list[list[float]]] = {
        "weekday": [[] for _ in range(24)],
        "weekend": [[] for _ in range(24)],
    }
    hourly_powers: list[float] = []
    for day in days_between(start, end):
        kind = "weekend" if day.weekday() >= 5 else "weekday"
        for hour in range(24):
            begin = day + timedelta(hours=hour)
            finish = begin + timedelta(hours=1)
            if begin < start or finish > end:
                continue
            measure = series.measure(begin, finish)
            if measure.records and measure.avg_kw is not None:
                hours[kind][hour].append(measure.avg_kw)
                hourly_powers.append(measure.avg_kw)
    curve = {
        kind: [_r(statistics.fmean(values)) if values else None for values in buckets]
        for kind, buckets in hours.items()
    }
    bases = []
    for day in days_between(start, end):
        finish = day + timedelta(days=1)
        if day < start or finish > end:
            continue
        measure = series.measure(day, finish)
        base = base_power(series, measure.first_index, measure.last_index, 10)
        if base is not None:
            bases.append(base)
    return {
        "meter_id": spec.id,
        "name": spec.name,
        "unit_kind": spec.unit_kind,
        "semantics": spec.semantics,
        "interval_minutes": spec.interval_minutes,
        "group": spec.group,
        "production_machines": spec.production_machines,
        "period_energy_kwh": _r(whole.energy_kwh),
        "period_avg_kw": _r(whole.avg_kw),
        "period_coverage": round(whole.coverage, 4),
        "days": days,
        "hour_of_day_kw": curve,
        "median_daily_p10_kw": _r(statistics.median(bases)) if bases else None,
        "max_hourly_kw": _r(max(hourly_powers)) if hourly_powers else None,
        "quality_notes": quality.notes(),
    }


def _num(value: float) -> str:
    """A number as written: fixed point, no exponent, no trailing zeros."""

    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _value(day: dict[str, Any], unit_kind: str) -> str:
    if unit_kind == "energy":
        value = day["energy_kwh"]
        return "无数据" if value is None else _num(value)
    value = day["avg_kw"]
    return "无数据" if value is None else f"{_num(value)} kW"


def _day_cell(day: dict[str, Any], unit_kind: str) -> str:
    notes = []
    if day.get("from"):
        notes.append(f"{day['from']} 起")
    if day.get("until"):
        notes.append(f"到 {day['until']}")
    if day["coverage"] < 0.999:
        notes.append(f"覆盖全天{day['coverage']:.1%}")
    suffix = f"（{'，'.join(notes)}）" if notes else ""
    return f"{day['date'][5:]} {_value(day, unit_kind)}{suffix}"


def profile_text(profile: dict[str, Any]) -> str:
    """Plain text of one meter's profile, short enough to read in full."""

    kind = "电量" if profile["unit_kind"] == "energy" else "功率（不能换算成电量）"
    lines = [
        f"## 表 {profile['meter_id']} {profile['name']}（{kind}，记录间隔 "
        f"{profile['interval_minutes']} 分钟，分组 {profile['group'] or '未分组'}）"
    ]
    if profile["production_machines"]:
        lines.append(f"挂接机台：{'、'.join(profile['production_machines'])}")
    total = profile["period_energy_kwh"]
    parts = []
    if total is not None:
        parts.append(f"全期电量 {_num(total)} kWh")
    if profile["period_avg_kw"] is not None:
        parts.append(f"平均功率 {_num(profile['period_avg_kw'])} kW")
    parts.append(f"数据覆盖 {profile['period_coverage']:.1%}")
    lines.append("；".join(parts))
    if profile["quality_notes"]:
        lines.append("数据说明：" + "；".join(profile["quality_notes"]))
    days = profile["days"]
    label = "kWh" if profile["unit_kind"] == "energy" else "平均 kW"
    if len(days) <= DAILY_ROWS_LIMIT:
        cells = [_day_cell(day, profile["unit_kind"]) for day in days]
        lines.append(f"逐日（{label}）：" + "，".join(cells))
    else:
        lines.append(f"共 {len(days)} 天，逐日数据较长，需要时用 energy-series 按日查看。")
    for name, title in (("weekday", "工作日"), ("weekend", "周末")):
        values = profile["hour_of_day_kw"][name]
        if any(value is not None for value in values):
            cells = [
                f"{hour:02d} {'-' if value is None else _num(value)}"
                for hour, value in enumerate(values)
            ]
            lines.append(f"小时曲线·{title}（平均 kW）：" + " | ".join(cells))
    tail = []
    if profile["median_daily_p10_kw"] is not None:
        tail.append(f"每日第 10 百分位功率（按记录时长加权）的中位数 {_num(profile['median_daily_p10_kw'])} kW")
    if profile["max_hourly_kw"] is not None:
        tail.append(f"小时平均功率最高 {_num(profile['max_hourly_kw'])} kW")
    if tail:
        lines.append("；".join(tail))
    return "\n".join(lines)


__all__ = ["meter_profile", "profile_text"]
