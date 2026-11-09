"""Facts about what the data measures, computed so the inferences about it can cite them.

The program cannot tell what a meter covers or what a column of the
production report means; it can measure how the data behaves.  Each fact
here is one such measurement with an id (coverage and gaps, the smallest
step of the readings, how often the power sits at zero, the power in each
production state, how the power rises while each machine runs, how two
meters move together, how the meter compares with the energy the report
writes per batch).  The caliber drafter reads them and writes inferences,
each citing the facts it rests on; a person confirms them on site.

The thresholds below only decide which measurements are worth listing;
none of them decides what a meter is.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .inputs import QUANTITY_BASES, Batch
from .items import INPUT_KINDS
from .series import MeterSeries
from .states import state_label
from .store import SiteData, read_json, write_json

CALIBER = "CALIBER"
# A machine needs this many placed batches before its lift on a meter is listed.
MIN_BATCHES = 3
# Machines listed per meter, by how much the meter's power rises while they run.
TOP_MACHINES = 3
# Pairs of meters listed: hours in common, correlation, share of hours one stays under the other.
PAIR_HOURS = 48
PAIR_CORRELATION = 0.7
PAIR_UNDER = 0.95
MAX_PAIRS = 30


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None or not np.isfinite(value) else round(float(value), digits)


def _t(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")


def _fact(fact_id: str, meter_ids: list[str], topic: str, text: str, values: dict[str, Any]) -> dict[str, Any]:
    return {"id": fact_id, "meter_ids": meter_ids, "topic": topic, "text": text, "values": values}


# -- one meter -------------------------------------------------------------


def _gaps(series: MeterSeries) -> list[tuple[datetime, float]]:
    """Where records stop and how long (hours) before they start again."""

    if len(series) < 2:
        return []
    if series.kind == "instant":
        steps = (series.starts[1:] - series.starts[:-1]) / np.timedelta64(1, "m")
        missing = steps - series.interval_minutes
        places = np.nonzero(steps > 1.5 * series.interval_minutes)[0]
    else:
        missing = (series.starts[1:] - series.ends[:-1]) / np.timedelta64(1, "m")
        places = np.nonzero(missing > 0)[0]
    return [(pd.Timestamp(series.starts[index + 1]).to_pydatetime(), float(missing[index]) / 60) for index in places]


def coverage_fact(spec_id: str, series: MeterSeries, period: tuple[datetime, datetime]) -> dict[str, Any]:
    start, end = period
    whole = series.measure(start, end)
    span = series.span()
    gaps = _gaps(series)
    longest = max(gaps, key=lambda gap: gap[1]) if gaps else None
    values = {
        "first": _t(span[0]) if span else None,
        "last": _t(span[1]) if span else None,
        "coverage": round(whole.coverage, 4),
        "gaps": len(gaps),
        "longest_gap_hours": _r(longest[1]) if longest else None,
        "longest_gap_ends": _t(longest[0]) if longest else None,
        "starts_late_hours": _r((span[0] - start).total_seconds() / 3600) if span else None,
        "ends_early_hours": _r((end - span[1]).total_seconds() / 3600) if span else None,
    }
    if not span:
        text = f"{spec_id} 在分析期内没有可用记录"
    else:
        text = f"{spec_id} 首条记录 {values['first']}，末条 {values['last']}，覆盖分析期 {whole.coverage:.1%}"
        if values["starts_late_hours"] and values["starts_late_hours"] >= 24:
            text += f"，比分析期开始晚 {values['starts_late_hours']} 小时"
        if gaps:
            text += f"；中间断档 {len(gaps)} 段，最长 {values['longest_gap_hours']} 小时（到 {values['longest_gap_ends']} 恢复）"
    return _fact(f"{spec_id}-COVER", [spec_id], "记录覆盖", text, values)


def resolution_fact(spec_id: str, series: MeterSeries) -> dict[str, Any] | None:
    """The smallest step of the readings and the power it stands for in one record."""

    values = series.values[np.isfinite(series.values)]
    positive = values[values > 0]
    if len(positive) < 10:
        return None
    if series.has_energy:
        smallest = float(positive.min())
        distinct = np.unique(np.round(positive, 4))
        steps = np.diff(distinct)
        step = float(steps[steps > 0].min()) if (steps > 0).any() else smallest
        step = min(step, smallest)
        ratios = positive / step
        on_grid = float(np.mean(np.abs(ratios - np.round(ratios)) < 0.02))
        hours = float(np.median(series.durations_h)) if len(series) else 0.0
        power = step / hours if hours > 0 and on_grid >= 0.9 else None
        text = f"{spec_id} 每条记录的最小正电量 {_r(smallest, 4)} kWh" + (
            f"（按每条 {_r(hours * 60, 0)} 分钟相当于 {_r(smallest / hours)} kW；零和它之间没有读数）；" if hours > 0 else "；"
        )
        if on_grid >= 0.9:
            text += f"{on_grid:.0%} 的正值是 {_r(step, 4)} kWh 的整数倍"
            if power:
                text += f"，功率最小一步约 {_r(power)} kW"
        else:
            text += "电量没有明显的固定步长"
        return _fact(
            f"{spec_id}-RES",
            [spec_id],
            "读数精度",
            text,
            {"smallest_kwh": _r(smallest, 4), "step_kwh": _r(step, 4), "share_on_grid": round(on_grid, 3), "power_step_kw": _r(power)},
        )
    distinct = np.unique(np.round(values, 6))
    steps = np.diff(distinct)
    step = float(steps[steps > 0].min()) if len(steps) and (steps > 0).any() else None
    if step is None:
        return None
    text = f"{spec_id} 功率读数相邻不同值的最小差 {_r(step, 4)} kW，共 {len(distinct)} 个不同值"
    return _fact(f"{spec_id}-RES", [spec_id], "读数精度", text, {"smallest_step_kw": _r(step, 4), "distinct": len(distinct)})


def low_fact(spec_id: str, series: MeterSeries) -> dict[str, Any] | None:
    """How often the power sits at zero or near it, and its longest stretch there."""

    powers = series.record_powers()
    known = np.isfinite(powers)
    if known.sum() < 10:
        return None
    high = float(np.percentile(powers[known], 95))
    zero = known & (powers <= 0)
    near = known & (powers <= 0.01 * high) if high > 0 else zero
    longest = 0.0
    run = 0.0
    lengths = series.durations_h if series.kind != "instant" else np.full(len(series), series.interval_minutes / 60)
    for index in range(len(series)):
        if zero[index] and (run == 0 or series.follows(index)):
            run += float(lengths[index])
        elif zero[index]:
            run = float(lengths[index])
        else:
            run = 0.0
        longest = max(longest, run)
    values = {
        "p95_kw": _r(high),
        "share_zero": round(float(zero.sum() / known.sum()), 4),
        "share_below_1pct_p95": round(float(near.sum() / known.sum()), 4),
        "longest_zero_hours": _r(longest),
        "median_kw": _r(float(np.median(powers[known]))),
    }
    text = (
        f"{spec_id} 记录功率中位数 {values['median_kw']} kW、95 分位 {values['p95_kw']} kW；"
        f"为零的记录占 {values['share_zero']:.1%}，不到 95 分位 1% 的占 {values['share_below_1pct_p95']:.1%}"
        + (f"，连续为零最长 {values['longest_zero_hours']} 小时" if longest > 0 else "")
    )
    return _fact(f"{spec_id}-LOW", [spec_id], "功率高低分布", text, values)


def state_fact(spec_id: str, rows: list[dict[str, Any]] | None, has_machines: bool) -> dict[str, Any] | None:
    if not rows:
        return None
    shown = [row for row in rows if row["avg_kw"] is not None]
    if not shown:
        return None
    text = f"{spec_id} 各状态平均功率：" + "；".join(
        f"{state_label(row['state'], has_machines)} {row['avg_kw']} kW（{row['covered_hours']} 小时有记录）" for row in shown
    )
    return _fact(
        f"{spec_id}-STATE",
        [spec_id],
        "生产状态下的功率",
        text,
        {row["state"]: {"avg_kw": row["avg_kw"], "covered_hours": row["covered_hours"]} for row in shown},
    )


def _merged(spans: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for low, high in sorted(spans):
        if merged and low <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


def _power_inside(series: MeterSeries, spans: list[tuple[datetime, datetime]]) -> tuple[float, float]:
    """Energy-like sum (kW × covered hours) and covered hours inside the spans."""

    total = hours = 0.0
    for low, high in spans:
        measure = series.measure(low, high)
        if measure.avg_kw is None or measure.covered_minutes <= 0:
            continue
        total += measure.avg_kw * measure.covered_minutes / 60
        hours += measure.covered_minutes / 60
    return total, hours


def _placed(batches: list[Batch]) -> dict[str, list[Batch]]:
    by_machine: dict[str, list[Batch]] = {}
    for batch in batches:
        if batch.problem is None and batch.start is not None and batch.end is not None:
            by_machine.setdefault(batch.machine, []).append(batch)
    return by_machine


def machine_fact(
    spec_id: str,
    series: MeterSeries,
    by_machine: dict[str, list[Batch]],
    mapped: list[str],
    coverage: tuple[datetime, datetime] | None,
) -> dict[str, Any] | None:
    """How much the meter's power rises while each machine runs, against while it does not."""

    if coverage is None or not len(series):
        return None
    whole_sum, whole_hours = _power_inside(series, [coverage])
    if whole_hours <= 0:
        return None
    rows = []
    for machine, batches in by_machine.items():
        if len(batches) < MIN_BATCHES:
            continue
        spans = _merged([(batch.start, batch.end) for batch in batches])  # type: ignore[misc]
        inside, inside_hours = _power_inside(series, spans)
        outside_hours = whole_hours - inside_hours
        if inside_hours < 2 or outside_hours < 2:
            continue
        running = inside / inside_hours
        idle = (whole_sum - inside) / outside_hours
        rows.append(
            {
                "machine": machine,
                "batches": len(batches),
                "running_kw": _r(running),
                "not_running_kw": _r(idle),
                "lift": _r(running / idle) if idle > 0 else None,
                "mapped": machine in mapped,
            }
        )
    if not rows:
        return None
    rows.sort(key=lambda row: (row["lift"] is None, -(row["lift"] or 0)))
    shown = rows[:TOP_MACHINES] + [row for row in rows[TOP_MACHINES:] if row["mapped"]]
    text = f"{spec_id} 按机台在产时段比本表平均功率（在产 / 不在产）：" + "；".join(
        f"{row['machine']}{'（manifest 挂在本表）' if row['mapped'] else ''} {row['running_kw']} / {row['not_running_kw']} kW"
        + (f"，{row['lift']} 倍" if row["lift"] is not None else "")
        + f"，{row['batches']} 批"
        for row in shown
    )
    if len(rows) > len(shown):
        text += f"；另有 {len(rows) - len(shown)} 个机台抬升更小，未列"
    return _fact(f"{spec_id}-MACHINES", [spec_id], "本表随哪些机台起落", text, {"machines": shown, "machines_compared": len(rows)})


def batch_fact(
    spec_id: str,
    series: MeterSeries,
    own: list[Batch],
    stopped_kw: float | None,
) -> dict[str, Any] | None:
    """The meter during its own machines' batches: records present, power above a stopped plant."""

    if not own:
        return None
    measures = series.measure_many([(batch.start, batch.end) for batch in own])  # type: ignore[misc]
    without = sum(1 for measure in measures if measure.coverage < 0.5)
    powers = [measure.avg_kw for measure in measures if measure.coverage >= 0.5 and measure.avg_kw is not None]
    values: dict[str, Any] = {
        "batches": len(own),
        "batches_without_records": without,
        "median_batch_kw": _r(statistics.median(powers)) if powers else None,
    }
    text = f"{spec_id} 挂的机台有 {len(own)} 个可定位批次，其中 {without} 个批次时段本表记录不到一半"
    if powers:
        text += f"；其余批次平均功率中位数 {values['median_batch_kw']} kW"
    if stopped_kw is not None and powers:
        flat = sum(1 for power in powers if power <= 1.1 * stopped_kw)
        values["batches_not_above_stopped"] = flat
        values["stopped_kw"] = stopped_kw
        text += f"，{flat} 个批次的平均功率不高于全厂停产时（{stopped_kw} kW）的 1.1 倍"
    return _fact(f"{spec_id}-BATCH", [spec_id], "挂表机台批次时段的读数", text, values)


def reported_fact(spec_id: str, series: MeterSeries, own: list[Batch]) -> dict[str, Any] | None:
    """Metered energy in the batches against the energy the report writes for them."""

    if not series.has_energy:
        return None
    pairs = []
    for batch in own:
        if batch.reported_energy_kwh is None:
            continue
        measure = series.measure(batch.start, batch.end)  # type: ignore[arg-type]
        if measure.coverage >= 0.9 and measure.energy_kwh is not None:
            pairs.append((measure.energy_kwh, batch.reported_energy_kwh))
    if len(pairs) < MIN_BATCHES:
        return None
    metered = sum(pair[0] for pair in pairs)
    reported = sum(pair[1] for pair in pairs)
    ratios = [pair[0] / pair[1] for pair in pairs if pair[1] > 0]
    values = {
        "batches": len(pairs),
        "metered_kwh": _r(metered, 1),
        "reported_kwh": _r(reported, 1),
        "ratio": _r(metered / reported, 3) if reported > 0 else None,
        "ratio_p10": _r(float(np.percentile(ratios, 10)), 3) if ratios else None,
        "ratio_p90": _r(float(np.percentile(ratios, 90)), 3) if ratios else None,
    }
    text = (
        f"{spec_id} 在 {len(pairs)} 个记录齐全的批次里计量 {values['metered_kwh']} kWh，"
        f"报表写的这些批次用电 {values['reported_kwh']} kWh，比值 {values['ratio']}"
        + (f"（逐批 10–90 分位 {values['ratio_p10']}–{values['ratio_p90']}）" if ratios else "")
    )
    return _fact(f"{spec_id}-REPORTED", [spec_id], "表计和报表电量", text, values)


# -- between meters ----------------------------------------------------------


def hourly_kw(series: MeterSeries) -> pd.Series | None:
    """Average power of each clock hour the records cover almost wholly; None for records longer than an hour."""

    if not len(series) or series.interval_minutes > 60:
        return None
    if series.kind == "instant":
        frame = pd.DataFrame({"hour": pd.DatetimeIndex(series.starts).floor("h"), "kw": series.values})
        grouped = frame.groupby("hour")["kw"].agg(["mean", "count"])
        needed = max(1, int(0.75 * 60 / series.interval_minutes))
        return grouped.loc[grouped["count"] >= needed, "mean"]
    hours = pd.DatetimeIndex(series.starts).floor("h")
    inside = pd.DatetimeIndex(series.ends) <= hours + pd.Timedelta(hours=1)
    frame = pd.DataFrame({"hour": hours[inside], "kwh": series.values[inside], "h": series.durations_h[inside]})
    grouped = frame.groupby("hour")[["kwh", "h"]].sum()
    grouped = grouped[grouped["h"] >= 0.9]
    return grouped["kwh"] / grouped["h"]


def pair_facts(hourly: dict[str, pd.Series]) -> list[dict[str, Any]]:
    """Pairs of meters whose hourly power moves together while one stays under the other."""

    found = []
    ids = sorted(hourly)
    for number, left in enumerate(ids):
        for right in ids[number + 1 :]:
            joined = pd.concat([hourly[left], hourly[right]], axis=1, join="inner").dropna()
            if len(joined) < PAIR_HOURS:
                continue
            a, b = joined.iloc[:, 0].to_numpy(), joined.iloc[:, 1].to_numpy()
            if a.std() == 0 or b.std() == 0:
                continue
            correlation = float(np.corrcoef(a, b)[0, 1])
            if correlation < PAIR_CORRELATION:
                continue
            upper, lower = (left, right) if a.mean() >= b.mean() else (right, left)
            big, small = (a, b) if upper == left else (b, a)
            tolerance = 0.01 * float(np.percentile(big, 95))
            under = float(np.mean(small <= big * 1.02 + tolerance))
            ratio = float(small.mean() / big.mean()) if big.mean() > 0 else None
            if under < PAIR_UNDER and (ratio is None or ratio < 0.9):
                continue
            found.append(
                _fact(
                    f"PAIR-{upper}-{lower}",
                    [upper, lower],
                    "两表的关系",
                    f"{lower} 和 {upper} 共同 {len(joined)} 个整点小时，小时功率相关 {correlation:.2f}；"
                    f"{lower} 不超过 {upper} 的小时占 {under:.0%}，平均功率之比 {ratio:.2f}"
                    if ratio is not None
                    else f"{lower} 和 {upper} 共同 {len(joined)} 个整点小时，小时功率相关 {correlation:.2f}",
                    {"hours": len(joined), "correlation": round(correlation, 3), "share_under": round(under, 3), "mean_ratio": _r(ratio, 3)},
                )
            )
    found.sort(key=lambda fact: -fact["values"]["correlation"])
    return found[:MAX_PAIRS]


def total_fact(hourly: dict[str, pd.Series]) -> dict[str, Any] | None:
    """The meter with the highest power against the sum of all the others, hour by hour."""

    if len(hourly) < 3:
        return None
    frame = pd.concat(hourly, axis=1, join="inner").dropna()
    if len(frame) < PAIR_HOURS:
        return None
    top = str(frame.mean().idxmax())
    rest = frame.drop(columns=[top]).sum(axis=1)
    if rest.std() == 0 or frame[top].std() == 0:
        return None
    correlation = float(np.corrcoef(frame[top], rest)[0, 1])
    ratio = float(rest.mean() / frame[top].mean()) if frame[top].mean() > 0 else None
    text = (
        f"平均功率最高的 {top} 和其余 {len(hourly) - 1} 块表之和比（共同 {len(frame)} 个整点小时）："
        f"相关 {correlation:.2f}" + (f"，其余之和是它的 {ratio:.2f} 倍" if ratio is not None else "")
    )
    return _fact(
        "PLANT-TOTAL",
        sorted(hourly),
        "有没有总表",
        text,
        {"top": top, "hours": len(frame), "correlation": round(correlation, 3), "rest_over_top": _r(ratio, 3)},
    )


def production_facts(site: SiteData, batches: list[Batch] | None) -> list[dict[str, Any]]:
    facts = []
    spec = site.manifest.production
    if spec is None or batches is None:
        return facts
    machines = sorted({batch.machine for batch in batches})
    mapped = {machine for meter in site.manifest.meters for machine in meter.production_machines}
    unmetered = {machine: sum(1 for batch in batches if batch.machine == machine) for machine in machines if machine not in mapped}
    idle = sorted(mapped - set(machines))
    text = f"生产记录里有 {len(machines)} 个机台，manifest 把 {len(set(machines) & mapped)} 个挂到了表上"
    if unmetered:
        text += "；没有挂表的：" + "、".join(f"{machine}（{count} 批）" for machine, count in unmetered.items())
    if idle:
        text += "；挂了表但生产记录里没有批次的：" + "、".join(idle)
    facts.append(
        _fact("PLANT-MACHINES", [], "机台和表的对应", text, {"machines": machines, "unmetered": unmetered, "mapped_without_batches": idle})
    )
    problems: dict[str, int] = {}
    for batch in batches:
        if batch.problem:
            problems[batch.problem] = problems.get(batch.problem, 0) + 1
    quantities = [batch.quantity for batch in batches if batch.quantity is not None]
    text = f"生产记录 {len(batches)} 批"
    if problems:
        text += "，" + "，".join(
            f"{count} 批{'缺开始或结束时间' if problem == 'time_missing' else '结束不晚于开始'}" for problem, count in problems.items()
        )
    if spec.quantity_column:
        basis = QUANTITY_BASES.get(spec.quantity_basis or "", "未写明")
        text += f"；数量列“{spec.quantity_column}”按 {basis} 读，{len(quantities)} 批有数量"
        if quantities:
            text += f"，中位数 {_r(statistics.median(quantities))}{spec.quantity_unit or ''}"
    else:
        text += "；没有指定数量列"
    if spec.reported_energy_column:
        text += f"；报表电量列“{spec.reported_energy_column}”有 {sum(1 for batch in batches if batch.reported_energy_kwh is not None)} 批有值"
    facts.append(
        _fact(
            "PLANT-PROD",
            [],
            "生产记录的口径",
            text,
            {"batches": len(batches), "problems": problems, "quantity_column": spec.quantity_column, "quantity_basis": spec.quantity_basis},
        )
    )
    return facts


# -- the step --------------------------------------------------------------


def compute_facts(site: SiteData) -> list[dict[str, Any]]:
    period = site.period()
    batches = site.batches()
    profiles = {item["meter_id"]: item for item in read_json(site.path("profiles.json"))["profiles"]}
    states = read_json(site.path("states.json")) if site.path("states.json").is_file() else None
    coverage = None
    if states and states.get("coverage"):
        coverage = (
            max(pd.Timestamp(states["coverage"]["start"]).to_pydatetime(), period[0]),
            min(pd.Timestamp(states["coverage"]["end"]).to_pydatetime(), period[1]),
        )
        if coverage[1] <= coverage[0]:
            coverage = None
    by_machine = _placed(batches) if batches else {}
    facts: list[dict[str, Any]] = []
    hourly: dict[str, pd.Series] = {}
    for spec in site.manifest.meters:
        series = site.series(spec.id)
        facts.append(coverage_fact(spec.id, series, period))
        machines = list(spec.production_machines)
        rows = profiles.get(spec.id, {}).get("states")
        stopped = next((row["avg_kw"] for row in rows or [] if row["state"] == "plant_stopped"), None)
        own = [batch for machine in machines for batch in by_machine.get(machine, [])]
        quality = site.quality(spec.id)
        notes = quality.notes()
        if quality.intervals_excluded_at_edges <= 4 and not quality.flat_throughout:
            # Two or three intervals at the ends of an export say nothing about what the meter measures.
            notes = [note for note in notes if not note.startswith("数据开头到第一次上升")]
        candidates = [
            resolution_fact(spec.id, series),
            low_fact(spec.id, series),
            _fact(f"{spec.id}-QUALITY", [spec.id], "读数处理", f"{spec.id} 读入时：" + "；".join(notes), {"notes": notes})
            if notes
            else None,
            state_fact(spec.id, rows, bool(machines)),
            machine_fact(spec.id, series, by_machine, machines, coverage) if by_machine else None,
            batch_fact(spec.id, series, own, stopped),
            reported_fact(spec.id, series, own),
        ]
        facts += [fact for fact in candidates if fact is not None]
        series_hourly = hourly_kw(series)
        if series_hourly is not None and len(series_hourly):
            hourly[spec.id] = series_hourly
    facts += production_facts(site, batches)
    total = total_fact(hourly)
    if total:
        facts.append(total)
    facts += pair_facts(hourly)
    return facts


def caliber_brief(workspace: Path, run_id: str) -> dict[str, Any]:
    """Compute the facts, keep them for the register, and write the drafter's brief."""

    site = SiteData(workspace, run_id)
    facts = compute_facts(site)
    write_json(site.path("caliber.json"), {"facts": facts})
    start, end = site.period()
    production = site.manifest.production
    brief = {
        "key": CALIBER,
        "run_id": run_id,
        "site": site.manifest.site.name,
        "period": {"start": _t(start), "end": _t(end)},
        "meters": [
            {
                "meter_id": spec.id,
                "name": spec.name,
                "group": spec.group,
                "unit_kind": spec.unit_kind,
                "semantics": spec.semantics,
                "interval_minutes": spec.interval_minutes,
                "production_machines": spec.production_machines,
            }
            for spec in site.manifest.meters
        ],
        "production": None
        if production is None
        else {
            "machine_column": production.machine_column,
            "start_column": production.start_column,
            "end_column": production.end_column,
            "quantity_column": production.quantity_column,
            "quantity_basis": production.quantity_basis,
            "quantity_unit": production.quantity_unit,
            "reported_energy_column": production.reported_energy_column,
        },
        "facts_text": "\n".join(f"[{fact['id']}] {fact['topic']}：{fact['text']}" for fact in facts),
        "fact_ids": [fact["id"] for fact in facts],
        "existing_assumptions": [
            {"id": entry["id"], "topic": entry["topic"], "inference": entry["inference"]} for entry in site.assumptions()
        ],
        "site_facts": [
            {"id": fact.id, "topic": fact.topic, "question": fact.question, "answer": fact.answer, "confirmed": fact.confirmed}
            for fact in site.facts() or []
        ],
        "input_kinds": INPUT_KINDS,
    }
    return {"run_id": run_id, "briefs": [brief]}


def settle_assumptions(workspace: Path, run_id: str, draft: dict[str, Any]) -> dict[str, Any]:
    """Number the drafted inferences, check what they cite and keep them for the briefs and the register.

    A citation that does not exist is dropped and noted; nothing the model
    wrote is judged or removed.
    """

    site = SiteData(workspace, run_id)
    facts = {fact["id"]: fact for fact in read_json(site.path("caliber.json"))["facts"]}
    survey = {fact.id for fact in site.facts() or []}
    meters = set(site.meter_ids())
    taken = {entry.id for entry in site.manifest.assumptions}
    drafted = []
    number = 0
    for entry in draft.get("assumptions", []):
        number += 1
        while f"D{number}" in taken:
            number += 1
        notes = []
        cited = list(dict.fromkeys(entry.get("basis_fact_ids", [])))
        unknown = [fact_id for fact_id in cited if fact_id not in facts and fact_id not in survey]
        if unknown:
            notes.append("引用的依据编号不存在，已去掉：" + "、".join(unknown))
        kept = [fact_id for fact_id in cited if fact_id not in unknown]
        if not kept:
            notes.append("没有一条能对上的依据")
        meter_ids = list(dict.fromkeys(entry.get("meter_ids", [])))
        strangers = [meter_id for meter_id in meter_ids if meter_id not in meters]
        if strangers:
            notes.append("写的表不在本次范围，已去掉：" + "、".join(strangers))
        basis = "；".join(
            f"[{fact_id}] {facts[fact_id]['text']}" if fact_id in facts else f"[{fact_id}] 现场调研答复"
            for fact_id in kept
        )
        drafted.append(
            {
                "id": f"D{number}",
                "origin": "drafted",
                "topic": entry["topic"],
                "inference": entry["inference"],
                "basis": (entry.get("reasoning") or "").strip() + ("。依据：" if entry.get("reasoning") else "依据：") + (basis or "无"),
                "reasoning": (entry.get("reasoning") or "").strip(),
                "basis_fact_ids": kept,
                "meter_ids": [meter_id for meter_id in meter_ids if meter_id in meters],
                "how_to_confirm": entry.get("how_to_confirm", ""),
                "affects": entry.get("affects", ""),
                "check_notes": notes,
            }
        )
    write_json(site.path("assumptions.json"), {"drafted": drafted})
    return {
        "run_id": run_id,
        "drafted": len(drafted),
        "with_notes": sum(1 for entry in drafted if entry["check_notes"]),
    }


__all__ = ["CALIBER", "caliber_brief", "compute_facts", "hourly_kw", "settle_assumptions"]
