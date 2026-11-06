"""The fixed menu of finding methods.

A finding method turns a loss item into something the program can replay
over the whole data period: the model picks a method and its parameters, and
the program computes every occurrence.  This is the provisional menu of the
vertical slice; it is to be aligned with the methods of the source system
(P01-P13) once that code is available.  No method holds a site-specific
threshold: thresholds are parameters the model or a person sets.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .inputs import Batch, CalendarWindow, clock_window
from .series import MeterSeries, WindowMeasure, days_between

_CLOCK = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$|^24:00$")
NO_METHOD = "none"


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Thresholds(_Params):
    min_kwh: float | None = Field(
        default=None, ge=0, description="一次出现的电量不低于此值才算命中（只用于电量表）"
    )
    min_avg_kw: float | None = Field(
        default=None, ge=0, description="一次出现的平均功率不低于此值才算命中"
    )


class Window(_Params):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def ordered(self) -> "Window":
        if self.start.tzinfo or self.end.tzinfo:
            raise ValueError("windows use site local time without a time zone")
        if self.end <= self.start:
            raise ValueError("window end must be after its start")
        return self


def overlapping_windows(windows: list[Window]) -> list[str]:
    """Pairs of windows that share time; counting both would count records twice."""

    ordered = sorted(windows, key=lambda window: (window.start, window.end))
    clashes = []
    for before, after in zip(ordered, ordered[1:], strict=False):
        if after.start < before.end:
            clashes.append(f"{before.start}~{before.end} 与 {after.start}~{after.end}")
    return clashes


class FixedWindowsParams(Thresholds):
    windows: list[Window] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def disjoint(self) -> "FixedWindowsParams":
        clashes = overlapping_windows(self.windows)
        if clashes:
            raise ValueError(f"windows must not overlap: {'; '.join(clashes)}")
        return self


class DailyClockParams(Thresholds):
    start_time: str = Field(description="每天开始时刻 HH:MM")
    end_time: str = Field(description="结束时刻 HH:MM；早于或等于开始时刻表示跨到次日")
    days: Literal["all", "weekday", "weekend"] = "all"

    @field_validator("start_time", "end_time")
    @classmethod
    def clock(cls, value: str) -> str:
        if not _CLOCK.match(value):
            raise ValueError(f"time of day must be HH:MM, not {value!r}")
        return value

    @model_validator(mode="after")
    def not_empty(self) -> "DailyClockParams":
        if self.start_time == "24:00":
            raise ValueError("start_time 24:00 is not a time of the day; use 00:00 with the days of the next day")
        if self.start_time == self.end_time:
            raise ValueError("start_time and end_time must differ")
        return self


class CalendarStateParams(Thresholds):
    state: str = Field(min_length=1, description="日历里的状态名，例如 non_production")


class BaseloadParams(_Params):
    percentile: float = Field(default=10, ge=1, le=50, description="取每天区间平均功率的第几百分位作基底")
    min_share: float | None = Field(
        default=None, ge=0, le=1, description="基底电量占当天电量的比例不低于此值才算命中"
    )
    min_avg_kw: float | None = Field(default=None, ge=0, description="基底功率不低于此值才算命中")


class ProductionGapsParams(Thresholds):
    machine: str = Field(min_length=1, description="生产记录里的机台名，须已在 manifest 里挂到这块表")
    min_gap_minutes: float = Field(default=0, ge=0)
    max_gap_minutes: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def ordered(self) -> "ProductionGapsParams":
        if self.max_gap_minutes is not None and self.max_gap_minutes < self.min_gap_minutes:
            raise ValueError("max_gap_minutes must not be below min_gap_minutes")
        return self


@dataclass(frozen=True)
class MethodContext:
    period: tuple[datetime, datetime]
    calendar: list[CalendarWindow] | None = None
    batches: list[Batch] | None = None
    machines: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Occurrence:
    measure: WindowMeasure
    hit: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        value = self.measure.as_dict()
        if self.hit is not None:
            value["hit"] = self.hit
        value.update(self.extra)
        return value


@dataclass
class MethodRun:
    method_id: str
    meter_id: str
    status: Literal["ok", "needs_input", "not_applicable"]
    reason: str | None = None
    occurrences: list[Occurrence] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def as_dict(self, *, top: int = 10) -> dict[str, Any]:
        value: dict[str, Any] = {
            "method_id": self.method_id,
            "meter_id": self.meter_id,
            "outcome": self.status,
        }
        if self.reason:
            value["reason"] = self.reason
        if self.status == "ok":
            value["summary"] = self.summary
            value["top_occurrences"] = [item.as_dict() for item in top_occurrences(self, top)]
        return value


@dataclass(frozen=True)
class MethodSpec:
    id: str
    title: str
    description: str
    params: type[_Params]
    needs: frozenset[str]
    shiftable: bool
    run: Callable[[MeterSeries, Any, MethodContext], MethodRun]

    def menu_entry(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "needs": sorted(self.needs),
            "params_schema": self.params.model_json_schema(),
        }


def _hit(measure: WindowMeasure, params: Thresholds) -> bool | None:
    if params.min_kwh is None and params.min_avg_kw is None:
        return None
    if measure.records == 0:
        return False
    checks = []
    if params.min_kwh is not None:
        checks.append(measure.energy_kwh is not None and measure.energy_kwh >= params.min_kwh)
    if params.min_avg_kw is not None:
        checks.append(measure.avg_kw is not None and measure.avg_kw >= params.min_avg_kw)
    return all(checks)


def _threshold_problem(series: MeterSeries, params: Any) -> str | None:
    if getattr(params, "min_kwh", None) is not None and not series.has_energy:
        return f"表 {series.meter_id} 是功率数据，没有电量，min_kwh 不适用；请改用 min_avg_kw"
    return None


def _windowed(
    method_id: str,
    series: MeterSeries,
    params: Thresholds,
    windows: list[tuple[datetime, datetime]],
) -> MethodRun:
    problem = _threshold_problem(series, params)
    if problem:
        return MethodRun(method_id, series.meter_id, "not_applicable", problem)
    occurrences = []
    for start, end in windows:
        measure = series.measure(start, end)
        occurrences.append(Occurrence(measure, _hit(measure, params)))
    return MethodRun(method_id, series.meter_id, "ok", occurrences=occurrences)


def _fixed_windows(series: MeterSeries, params: FixedWindowsParams, ctx: MethodContext) -> MethodRun:
    del ctx
    windows = [(window.start, window.end) for window in params.windows]
    return _windowed("fixed-windows", series, params, windows)


def _day_matches(day: datetime, days: str) -> bool:
    if days == "all":
        return True
    weekend = day.weekday() >= 5
    return weekend if days == "weekend" else not weekend


def _daily_clock(series: MeterSeries, params: DailyClockParams, ctx: MethodContext) -> MethodRun:
    start, end = ctx.period
    windows = []
    for day in days_between(start, end):
        if not _day_matches(day, params.days):
            continue
        begin, finish = clock_window(day, params.start_time, params.end_time)
        if begin >= start and finish <= end:
            windows.append((begin, finish))
    return _windowed("daily-clock-window", series, params, windows)


def _calendar_state(series: MeterSeries, params: CalendarStateParams, ctx: MethodContext) -> MethodRun:
    if ctx.calendar is None:
        return MethodRun("calendar-state", series.meter_id, "needs_input", "manifest 里没有经人确认的日历")
    states = sorted({window.state for window in ctx.calendar})
    if params.state not in states:
        return MethodRun(
            "calendar-state",
            series.meter_id,
            "needs_input",
            f"日历里没有状态 {params.state}；现有状态：{states}",
        )
    start, end = ctx.period
    spans = []
    for window in ctx.calendar:
        if window.state != params.state:
            continue
        begin, finish = max(window.start, start), min(window.end, end)
        if finish > begin:
            spans.append((begin, finish))
    # Rows of one state that overlap or touch are one stretch of that state:
    # measured apart, overlapping rows would count shared records twice and
    # a record across the point where two rows touch would be lost to both.
    windows: list[tuple[datetime, datetime]] = []
    merged = 0
    for begin, finish in sorted(spans):
        if windows and begin <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], finish))
            merged += 1
        else:
            windows.append((begin, finish))
    run = _windowed("calendar-state", series, params, windows)
    run.summary["rows_merged"] = merged
    return run


def _weighted_percentile(values: np.ndarray, weights: np.ndarray, percentile: float) -> float:
    order = np.argsort(values)
    values, weights = values[order], weights[order]
    cumulative = np.cumsum(weights)
    target = percentile / 100 * cumulative[-1]
    return float(values[min(int(np.searchsorted(cumulative, target, "left")), len(values) - 1)])


def base_power(series: MeterSeries, lo: int, hi: int, percentile: float) -> float | None:
    """The base power of records lo..hi: a low percentile of their power.

    Instant readings weigh the same; interval records weigh as long as they
    last, so one record that spans a gap in the data does not count as one
    ordinary sample.
    """

    if hi <= lo:
        return None
    if series.kind == "instant":
        return float(np.percentile(series.values[lo:hi], percentile))
    hours = series.durations_h[lo:hi]
    with np.errstate(divide="ignore", invalid="ignore"):
        powers = series.values[lo:hi] / np.where(hours > 0, hours, np.nan) if series.has_energy else series.values[lo:hi]
    usable = (hours > 0) & ~np.isnan(powers)
    return _weighted_percentile(powers[usable], hours[usable], percentile) if usable.any() else None


def base_portions(series: MeterSeries, lo: int, hi: int, base_kw: float) -> dict[int, float]:
    """Energy of each record up to the base power: min(P, base) x hours (kWh)."""

    portions: dict[int, float] = {}
    for index in range(lo, hi):
        hours = float(series.durations_h[index])
        if hours <= 0:
            continue
        power = float(series.values[index]) / hours
        portions[index] = min(power, base_kw) * hours
    return portions


def _baseload(series: MeterSeries, params: BaseloadParams, ctx: MethodContext) -> MethodRun:
    if params.min_share is not None and not series.has_energy:
        return MethodRun(
            "baseload",
            series.meter_id,
            "not_applicable",
            f"表 {series.meter_id} 是功率数据，没有电量，min_share 不适用；请改用 min_avg_kw",
        )
    start, end = ctx.period
    occurrences = []
    for day in days_between(start, end):
        day_end = day + timedelta(days=1)
        if day < start or day_end > end:
            continue
        measure = series.measure(day, day_end)
        lo, hi = measure.first_index, measure.last_index
        extra: dict[str, Any] = {"base_kw": None, "base_kwh": None, "base_share": None}
        hit: bool | None = None
        if hi > lo:
            base_kw = base_power(series, lo, hi, params.percentile)
            if base_kw is not None:
                extra["base_kw"] = round(base_kw, 3)
                if series.has_energy and measure.energy_kwh:
                    base_kwh = sum(base_portions(series, lo, hi, base_kw).values())
                    extra["base_kwh"] = round(base_kwh, 3)
                    extra["base_share"] = round(base_kwh / measure.energy_kwh, 4)
                checks = []
                if params.min_share is not None:
                    checks.append(extra["base_share"] is not None and extra["base_share"] >= params.min_share)
                if params.min_avg_kw is not None:
                    checks.append(base_kw >= params.min_avg_kw)
                hit = all(checks) if checks else None
        if hit is None and (params.min_share is not None or params.min_avg_kw is not None):
            hit = False
        occurrences.append(Occurrence(measure, hit, extra))
    run = MethodRun("baseload", series.meter_id, "ok", occurrences=occurrences)
    bases = [item.extra["base_kwh"] for item in occurrences if item.extra["base_kwh"] is not None]
    run.summary["total_base_kwh"] = _r(sum(bases)) if bases else None
    hit_bases = [item.extra["base_kwh"] or 0.0 for item in occurrences if item.hit]
    thresholds = params.min_share is not None or params.min_avg_kw is not None
    measured = any(item.measure.records for item in occurrences)
    run.summary["hit_base_kwh"] = _r(sum(hit_bases)) if thresholds and series.has_energy and measured else None
    kws = [item.extra["base_kw"] for item in occurrences if item.extra["base_kw"] is not None]
    run.summary["median_base_kw"] = _r(statistics.median(kws)) if kws else None
    return run


def _production_gaps(series: MeterSeries, params: ProductionGapsParams, ctx: MethodContext) -> MethodRun:
    if ctx.batches is None:
        return MethodRun("production-gaps", series.meter_id, "needs_input", "manifest 里没有生产记录")
    if params.machine not in ctx.machines.get(series.meter_id, []):
        mapped = ctx.machines.get(series.meter_id, [])
        return MethodRun(
            "production-gaps",
            series.meter_id,
            "needs_input",
            f"机台 {params.machine} 没有在 manifest 里挂到表 {series.meter_id}（已挂：{mapped or '无'}）",
        )
    problem = _threshold_problem(series, params)
    if problem:
        return MethodRun("production-gaps", series.meter_id, "not_applicable", problem)
    own = [batch for batch in ctx.batches if batch.machine == params.machine]
    usable = sorted(
        (batch for batch in own if batch.problem is None),
        key=lambda batch: (batch.start, batch.end),
    )
    start, end = ctx.period
    overlapping = 0
    latest = None
    latest_row = None
    gaps: list[tuple[datetime, datetime, int, int]] = []
    for batch in usable:
        assert batch.start is not None and batch.end is not None
        if latest is None:
            latest, latest_row = batch.end, batch.row
            continue
        if batch.start < latest:
            # Runs while an earlier batch still runs: no gap; keep the later end.
            overlapping += 1
            if batch.end > latest:
                latest, latest_row = batch.end, batch.row
            continue
        if batch.start > latest:
            gaps.append((latest, batch.start, latest_row, batch.row))
        latest, latest_row = batch.end, batch.row
    # A batch with only a start ran from that start for an unknown time, so
    # the first gap that ends after it may hold it; a batch with only an end
    # ran for an unknown time up to it, so the last gap that starts before it
    # may hold it.  Those gaps are not used.
    blocked: set[int] = set()
    for batch in own:
        if batch.problem is None:
            continue
        if batch.start is not None:
            later = [number for number, gap in enumerate(gaps) if gap[1] > batch.start]
            blocked.update(later[:1])
        if batch.end is not None:
            earlier = [number for number, gap in enumerate(gaps) if gap[0] < batch.end]
            blocked.update(earlier[-1:])
        if batch.start is not None and batch.end is not None:
            # End not after start: either time may be the wrong one, so what
            # either alone would block is blocked, and every gap between them.
            low, high = min(batch.start, batch.end), max(batch.start, batch.end)
            blocked.update(number for number, gap in enumerate(gaps) if gap[0] <= high and gap[1] >= low)
    occurrences = []
    crossing = 0
    for number, (gap_start, gap_end, before_row, after_row) in enumerate(gaps):
        minutes = (gap_end - gap_start).total_seconds() / 60
        if minutes < params.min_gap_minutes:
            continue
        if params.max_gap_minutes is not None and minutes > params.max_gap_minutes:
            continue
        if gap_start < start or gap_end > end:
            continue
        if number in blocked:
            crossing += 1
            continue
        measure = series.measure(gap_start, gap_end)
        occurrences.append(
            Occurrence(
                measure,
                _hit(measure, params),
                {"after_batch_row": before_row, "before_batch_row": after_row},
            )
        )
    run = MethodRun("production-gaps", series.meter_id, "ok", occurrences=occurrences)
    run.summary["batches_unusable"] = len(own) - len(usable)
    run.summary["batches_overlapping"] = overlapping
    run.summary["gaps_skipped_unusable_batch"] = crossing
    return run


MENU: dict[str, MethodSpec] = {
    spec.id: spec
    for spec in (
        MethodSpec(
            "fixed-windows",
            "指定时间窗",
            "量指定的 1-20 个时间窗里的电量或平均功率。只覆盖这几个窗口，不能在全期回放。",
            FixedWindowsParams,
            frozenset(),
            True,
            _fixed_windows,
        ),
        MethodSpec(
            "daily-clock-window",
            "每天固定时段",
            "在全期每一天（可只看工作日或周末）量同一个钟点时段的电量或平均功率，例如夜间 22:00-06:00。",
            DailyClockParams,
            frozenset(),
            True,
            _daily_clock,
        ),
        MethodSpec(
            "calendar-state",
            "日历状态时段",
            "量经人确认的日历里某种状态（例如非生产）的全部时段。需要 manifest 提供日历。",
            CalendarStateParams,
            frozenset({"calendar"}),
            True,
            _calendar_state,
        ),
        MethodSpec(
            "baseload",
            "每日基底负荷",
            "每天取各条记录平均功率的低百分位（按记录时长加权）作基底功率；基底电量是每条记录不超过基底功率的那部分电量之和，再算它占当天电量的比例。",
            BaseloadParams,
            frozenset(),
            False,
            _baseload,
        ),
        MethodSpec(
            "production-gaps",
            "生产批次间隙",
            "按生产记录找同一机台的空档：从此前所有批次里最晚的结束到下一批开始；可能有时间不全的批次落在里面的空档不用。量空档里这块表的电量或平均功率。需要生产记录，且机台已挂到这块表。",
            ProductionGapsParams,
            frozenset({"production"}),
            True,
            _production_gaps,
        ),
    )
}


class MethodParamsError(ValueError):
    pass


def parse_params(method_id: str, params: dict[str, Any]) -> Any:
    spec = MENU.get(method_id)
    if spec is None:
        raise MethodParamsError(f"unknown method {method_id!r}; the menu has {sorted(MENU)}")
    try:
        return spec.params.model_validate(params)
    except ValidationError as exc:
        raise MethodParamsError(f"{method_id} parameters are not valid: {exc}") from exc


def run_method(
    method_id: str,
    params: dict[str, Any],
    series: MeterSeries,
    ctx: MethodContext,
    meter_total_kwh: float | None,
) -> MethodRun:
    spec = MENU[method_id]
    run = spec.run(series, parse_params(method_id, params), ctx)
    if run.status == "ok":
        run.summary = {**summarize(run.occurrences, series.has_energy, meter_total_kwh), **run.summary}
        if method_id == "baseload":
            # share_of_meter stays the share of the whole days' energy beside it.
            base = item_kwh(run)
            run.summary["base_share_of_meter"] = (
                _r(base / meter_total_kwh, 4) if base is not None and meter_total_kwh else None
            )
    return run


def summarize(
    occurrences: list[Occurrence],
    has_energy: bool,
    meter_total_kwh: float | None,
) -> dict[str, Any]:
    with_data = [item for item in occurrences if item.measure.records > 0]
    powers = [item.measure.avg_kw for item in with_data if item.measure.avg_kw is not None]
    hits = [item for item in occurrences if item.hit]
    any_threshold = any(item.hit is not None for item in occurrences)
    summary: dict[str, Any] = {
        "occurrences": len(occurrences),
        "with_data": len(with_data),
        "hits": len(hits) if any_threshold else None,
        "mean_avg_kw": _r(statistics.fmean(powers)) if powers else None,
        "median_avg_kw": _r(statistics.median(powers)) if powers else None,
        "max_avg_kw": _r(max(powers)) if powers else None,
        "mean_coverage": _r(statistics.fmean(item.measure.coverage for item in occurrences), 4)
        if occurrences
        else None,
        "low_coverage": sum(1 for item in occurrences if item.measure.coverage < 0.9),
        "total_kwh": None,
        "hit_kwh": None,
        "share_of_meter": None,
    }
    if has_energy and with_data:
        # Nothing measured is an empty number, not zero.
        total = sum(item.measure.energy_kwh or 0.0 for item in with_data)
        summary["total_kwh"] = _r(total)
        if any_threshold:
            summary["hit_kwh"] = _r(sum(item.measure.energy_kwh or 0.0 for item in hits))
        if meter_total_kwh:
            counted = summary["hit_kwh"] if any_threshold else summary["total_kwh"]
            summary["share_of_meter"] = _r(counted / meter_total_kwh, 4)
    return summary


def item_kwh(run: MethodRun) -> float | None:
    """The energy the item stands for: base energy, hit energy or all occurrences."""

    if run.method_id == "baseload":
        if run.summary.get("hit_base_kwh") is not None:
            return run.summary["hit_base_kwh"]
        return run.summary.get("total_base_kwh")
    if run.summary.get("hits") is not None:
        return run.summary.get("hit_kwh")
    return run.summary.get("total_kwh")


def top_occurrences(run: MethodRun, count: int) -> list[Occurrence]:
    def size(item: Occurrence) -> float:
        value = item.measure.energy_kwh if item.measure.energy_kwh is not None else item.measure.avg_kw
        return -1.0 if value is None else float(value)

    return sorted(run.occurrences, key=size, reverse=True)[:count]


def menu_entries() -> list[dict[str, Any]]:
    return [spec.menu_entry() for spec in MENU.values()]


def _r(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


__all__ = [
    "MENU",
    "NO_METHOD",
    "MethodContext",
    "MethodParamsError",
    "MethodRun",
    "MethodSpec",
    "Occurrence",
    "base_portions",
    "item_kwh",
    "overlapping_windows",
    "menu_entries",
    "parse_params",
    "run_method",
    "summarize",
    "top_occurrences",
]
