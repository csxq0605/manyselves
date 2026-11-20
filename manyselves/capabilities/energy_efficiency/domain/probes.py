"""What the excess of a candidate looks like, and the cause tests the program runs for the model.

Decompositions do not depend on the knowledge base: when and where the
excess falls (days, weekdays, clock, plant state), how concentrated it is,
whether it changed at some date, whether it moves with production, and for
batch lenses whether the batches above the reference drew more power or ran
longer per unit.

A cause test is one of a fixed list; the model picks it and its parameters,
the program computes it and fills in supports / contradicts / cannot_test
with the numbers and the rule (thresholds are preferences).  A cause the data
contradicts is shown as contradicted; the program does not judge the cause.
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from .grid import Grid, SelectorError, When
from .lenses import Candidate
from .preparation import ParameterSet

TEST_TYPES: dict[str, str] = {
    "independent_of_production": "本项逐日超出（没有超出时用本表逐日电量）和产线逐日电量的秩相关绝对值低于 P21：支持“与生产无关”",
    "base_load_when_stopped": "全厂停时本表平均功率 ÷ 在产时不低于 P31：支持“停产时不降”",
    "change_at_date": "date 前后本项逐日超出的均值相差不少于较大者 × P42：支持“从这天起变了”",
    "concentrated": "最大的两成事件（批次视角按批次，其余按天）占本项超出不少于 P43：支持“集中在少数事件”",
    "in_selection": "本项超出落在 when 选出的时段里的比例不少于 P43：支持“集中在这些时段”",
    "only_some_groups": "本项超出落在 groups 这些可比组的批次里的比例不少于 P43（批次视角）：支持“只在某些产品或组”",
    "higher_than_peers": "本线在 groups 里的默认参照高于别线同组的默认参照（批次视角）：支持“比同类高”",
}
BATCH_LENSES = ("L2a", "L2b", "L3")
RESULT_LABELS = {"supports": "支持", "contradicts": "矛盾", "cannot_test": "查不了"}


class TestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "independent_of_production",
        "base_load_when_stopped",
        "change_at_date",
        "concentrated",
        "in_selection",
        "only_some_groups",
        "higher_than_peers",
    ]
    date: str | None = Field(default=None, description="change_at_date：YYYY-MM-DD")
    when: When | None = Field(default=None, description="in_selection：哪些时段")
    groups: list[str] = Field(default_factory=list, description="only_some_groups、higher_than_peers：可比组名")


def _share(part: float, whole: float) -> float | None:
    return None if whole <= 0 else round(part / whole, 3)


def _daily(grid: Grid, cells: np.ndarray) -> np.ndarray:
    return grid.daily(cells)


def _rank_corr(a: np.ndarray, b: np.ndarray) -> float | None:
    if len(a) < 5 or np.std(a) == 0 or np.std(b) == 0:
        return None
    value = pd.Series(a).rank().corr(pd.Series(b).rank())
    return None if value is None or not np.isfinite(value) else round(float(value), 3)


def change_point(daily: np.ndarray, keys: list[str], minimum: int = 7) -> dict[str, Any] | None:
    """The day that best splits the daily excess into two means (at least ``minimum`` days each side)."""

    if len(daily) < 2 * minimum or daily.sum() <= 0:
        return None
    best = None
    for split in range(minimum, len(daily) - minimum + 1):
        before, after = daily[:split].mean(), daily[split:].mean()
        gap = abs(after - before)
        if best is None or gap > best[0]:
            best = (gap, split, before, after)
    assert best is not None
    _gap, split, before, after = best
    return {"date": keys[split], "before_kwh_per_day": round(float(before), 1), "after_kwh_per_day": round(float(after), 1)}


def decompose(grid: Grid, candidate: Candidate, batch_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The generic split of one candidate's excess (default reference)."""

    cells = candidate.cells
    total = float(cells.sum())
    if total <= 0:
        return {"excess_kwh": 0.0}
    hit = cells > 0
    hours = float(hit.sum()) * grid.step_h
    daily = _daily(grid, cells)
    days = [grid.day_keys[day] for day in np.flatnonzero(daily > 0)]
    by_weekday = np.bincount(grid.weekday[hit] - 1, weights=cells[hit], minlength=7)
    blocks = np.bincount(grid.minute[hit] // 360, weights=cells[hit], minlength=4)
    stopped = float(cells[grid.stop].sum())
    ordered = np.sort(daily[daily > 0])[::-1]
    top = max(1, int(np.ceil(len(ordered) * 0.2)))
    line_daily = grid.daily(grid.line_energy())
    result: dict[str, Any] = {
        "excess_kwh": round(total, 1),
        "hours": round(hours, 2),
        "mean_excess_kw": round(total / hours, 2) if hours else None,
        "days": len(days),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "weekday_share": {"一二三四五六日"[number]: _share(float(by_weekday[number]), total) for number in range(7) if by_weekday[number] > 0},
        "clock_share": {label: _share(float(blocks[number]), total) for number, label in enumerate(("0-6点", "6-12点", "12-18点", "18-24点")) if blocks[number] > 0},
        "plant_stop_share": _share(stopped, total),
        "top_days_share": _share(float(ordered[:top].sum()), total),
        "production_correlation": _rank_corr(daily, line_daily),
        "change": change_point(daily, grid.day_keys),
    }
    if candidate.lens in ("L2a", "L3") and batch_rows:
        result["batches"] = batch_split(candidate, batch_rows)
    return result


def stability(grid: Grid, candidate: Candidate) -> dict[str, Any]:
    """Does the excess hold over the whole period: the two halves, the largest day, and for a spec row the shifted hours."""

    cells = candidate.cells
    total = float(cells.sum())
    result: dict[str, Any] = {}
    if total > 0 and len(grid.day_keys) >= 2:
        daily = grid.daily(cells)
        half = len(grid.day_keys) // 2
        first, second = float(daily[:half].sum()), float(daily[half:].sum())
        largest = int(np.argmax(daily))
        result.update(
            {
                "split_day": grid.day_keys[half],
                "first_half_kwh": round(first, 1),
                "second_half_kwh": round(second, 1),
                "half_ratio": round(min(first, second) / max(first, second), 3) if max(first, second) > 0 else None,
                "largest_day": grid.day_keys[largest],
                "largest_day_share": _share(float(daily[largest]), total),
            }
        )
    shift = candidate.details.get("shift")
    if shift:
        result["shift"] = shift
    return result


def batch_split(candidate: Candidate, batch_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Batches above the reference against the rest: run power and run hours per unit."""

    rows = [row for row in batch_rows if row["meter"] == candidate.meter and row["usable"] and row["run_kwh"] > 0 and row["run_hours"] > 0]
    if candidate.lens == "L3":
        rows = [row for row in rows if row["group"] == candidate.part]
    above_ids = {entry["task"] for entry in candidate.details.get("top_batches", [])}
    if candidate.lens == "L2a":
        references = {entry["group"]: entry.get(candidate.default) for entry in candidate.details.get("groups", [])}
        above_ids = {row["task"] for row in rows if references.get(row["group"]) is not None and row["run_kwh"] / row["quantity"] > references[row["group"]]}
    above = [row for row in rows if row["task"] in above_ids]
    rest = [row for row in rows if row["task"] not in above_ids]
    if not above or not rest:
        return None

    def median(values: list[float]) -> float:
        return float(np.median(values))

    power_above = median([row["run_kwh"] / row["run_hours"] for row in above])
    power_rest = median([row["run_kwh"] / row["run_hours"] for row in rest])
    time_above = median([row["run_hours"] / row["quantity"] for row in above])
    time_rest = median([row["run_hours"] / row["quantity"] for row in rest])
    return {
        "above": len(above),
        "rest": len(rest),
        "run_kw_above": round(power_above, 2),
        "run_kw_rest": round(power_rest, 2),
        "power_gap": round(power_above / power_rest - 1, 3) if power_rest else None,
        "hours_per_unit_above": round(time_above, 5),
        "hours_per_unit_rest": round(time_rest, 5),
        "time_gap": round(time_above / time_rest - 1, 3) if time_rest else None,
    }


def run_test(
    grid: Grid,
    candidate: Candidate,
    request: TestRequest,
    params: ParameterSet,
    pws: dict[str, dict[str, Any]],
    batch_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """One cause test: supports, contradicts or cannot_test, with the numbers and the rule."""

    rule = TEST_TYPES[request.type]
    cells = candidate.cells
    total = float(cells.sum())
    share_line = float(params.value("probe_share"))

    def answer(result: str, measured: dict[str, Any], why: str = "") -> dict[str, Any]:
        return {"type": request.type, "result": result, "measured": measured, "rule": rule, "why": why}

    try:
        if request.type == "independent_of_production":
            daily = _daily(grid, cells) if total > 0 else grid.daily(grid.energy_of(candidate.meter))
            full = grid.full_days(candidate.meter)
            rho = _rank_corr(daily[full], grid.daily(grid.line_energy())[full])
            if rho is None:
                return answer("cannot_test", {}, "天数不够或数没有变化")
            limit = float(params.value("driven_correlation"))
            return answer("supports" if abs(rho) < limit else "contradicts", {"rank_correlation": rho, "limit": limit})
        if request.type == "base_load_when_stopped":
            row = pws.get(candidate.meter)
            if not row or row.get("ratio") is None:
                return answer("cannot_test", {}, "没有全厂停或在产的时段")
            limit = float(params.value("elasticity_trigger"))
            measured = {"stop_kw": row["stop_kw"], "run_kw": row["run_kw"], "ratio": row["ratio"], "limit": limit}
            return answer("supports" if row["ratio"] >= limit else "contradicts", measured)
        if total <= 0:
            return answer("cannot_test", {}, "本项在默认参照下没有超出")
        if request.type == "change_at_date":
            if not request.date or request.date not in grid.day_keys:
                return answer("cannot_test", {}, f"date 要是数据期里的一天（{grid.day_keys[0]} 到 {grid.day_keys[-1]}）")
            daily = _daily(grid, cells)
            split = grid.day_keys.index(request.date)
            if split == 0:
                return answer("cannot_test", {}, "这天之前没有数据")
            before, after = float(daily[:split].mean()), float(daily[split:].mean())
            limit = float(params.value("probe_change_share"))
            gap = abs(after - before)
            measured = {"before_kwh_per_day": round(before, 1), "after_kwh_per_day": round(after, 1), "limit": limit}
            return answer("supports" if gap >= limit * max(before, after) else "contradicts", measured)
        if request.type == "concentrated":
            if candidate.lens in BATCH_LENSES:
                values = np.array([entry["excess_kwh"] or 0 for entry in candidate.details.get("top_batches", [])])
                if candidate.lens == "L3" or not len(values):
                    return answer("cannot_test", {}, "本项没有逐批的超出")
                events = "批次"
                denominator = total
            else:
                values = _daily(grid, cells)
                values = values[values > 0]
                events = "天"
                denominator = total
            ordered = np.sort(values)[::-1]
            count = int(candidate.details.get("batches_above", len(ordered))) if candidate.lens in BATCH_LENSES else len(ordered)
            top = max(1, int(np.ceil(count * 0.2)))
            share = _share(float(ordered[:top].sum()), denominator)
            return answer("supports" if share is not None and share >= share_line else "contradicts", {"events": events, "count": count, "top": top, "share": share, "limit": share_line})
        if request.type == "in_selection":
            if request.when is None:
                return answer("cannot_test", {}, "in_selection 要写 when")
            mask = grid.mask(request.when, candidate.meter)
            share = _share(float(cells[mask].sum()), total)
            hours_share = _share(float(mask.sum()), float(grid.cells))
            return answer(
                "supports" if share is not None and share >= share_line else "contradicts",
                {"selection": request.when.text(), "share": share, "hours_share": hours_share, "limit": share_line},
            )
        if request.type == "only_some_groups":
            if candidate.lens not in ("L2a", "L2b") or not request.groups:
                return answer("cannot_test", {}, "只适用于批次先例视角，要写 groups")
            rows = candidate.details.get("top_batches", [])
            part = sum(entry["excess_kwh"] or 0 for entry in rows if entry["group"] in request.groups)
            whole = sum(entry["excess_kwh"] or 0 for entry in rows)
            share = _share(part, whole)
            return answer("supports" if share is not None and share >= share_line else "contradicts", {"share": share, "limit": share_line, "note": "按超出最大的 10 批算"})
        if request.type == "higher_than_peers":
            if candidate.lens not in ("L2a", "L3") or not request.groups:
                return answer("cannot_test", {}, "只适用于批次视角，要写 groups")
            measured = {}
            higher = []
            for group in request.groups:
                own = [row for row in batch_rows if row["meter"] == candidate.meter and row["group"] == group and row["usable"] and row["run_kwh"] > 0]
                peers = [row for row in batch_rows if row["meter"] != candidate.meter and row["group"] == group and row["usable"] and row["run_kwh"] > 0]
                if len(own) < 1 or len(peers) < 1:
                    continue
                own_value = float(np.median([row["run_kwh"] / row["quantity"] for row in own]))
                by_line: dict[str, list[float]] = {}
                for row in peers:
                    by_line.setdefault(row["meter"], []).append(row["run_kwh"] / row["quantity"])
                best = min(float(np.median(values)) for values in by_line.values())
                measured[group] = {"own_median": round(own_value, 5), "best_other_median": round(best, 5)}
                higher.append(own_value > best)
            if not higher:
                return answer("cannot_test", {}, "这些组在别的产线上没有可用的批次")
            return answer("supports" if all(higher) else "contradicts", measured)
    except SelectorError as exc:
        return answer("cannot_test", {}, str(exc))
    return answer("cannot_test", {}, "没有这种检验")


__all__ = ["RESULT_LABELS", "TEST_TYPES", "TestRequest", "change_point", "decompose", "run_test", "stability"]
