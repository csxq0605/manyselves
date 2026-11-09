"""The fixed menu of finding methods.

A finding method turns a loss item into something the program can replay
over the whole data period: the model picks a method and its parameters, and
the program computes every occurrence.  Each method is a chain of operators
of the knowledge base 《能效诊断知识库：九步骤完整版》 (appendix A, OP ids),
following its expert scenario chains (appendix C, ZQ/BS/HB ids); the ids sit
on every menu entry.  A method only computes (step 2 of the nine): it never
says an occurrence is a loss.  No method holds a site-specific threshold:
thresholds are parameters the model or a person sets.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .inputs import QUANTITY_BASES, Batch, CalendarWindow, clock_window
from .series import MeterSeries, WindowMeasure, days_between
from .states import STATES, Segment, segments, state_windows

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


class Exclusion(_Params):
    """Time taken out of the method's windows (OP004 difference)."""

    production_machines: list[str] = Field(
        default_factory=list, description="扣掉这些机台在生产记录里的批次时段"
    )
    calendar_states: list[str] = Field(default_factory=list, description="扣掉日历里这些状态的时段")

    @model_validator(mode="after")
    def something(self) -> "Exclusion":
        if not self.production_machines and not self.calendar_states:
            raise ValueError("exclude needs production_machines or calendar_states")
        return self


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
    exclude: Exclusion | None = Field(default=None, description="从每天的时段里扣掉的时间，例如生产批次")

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
    exclude: Exclusion | None = Field(default=None, description="从这些时段里扣掉的时间，例如生产批次")


def _ascending(values: list[float], name: str) -> None:
    if any(later <= earlier for earlier, later in zip(values, values[1:], strict=False)):
        raise ValueError(f"{name} must rise strictly")


class ProductionGapsParams(Thresholds):
    machine: str = Field(min_length=1, description="生产记录里的机台名，须已在 manifest 里挂到这块表")
    min_gap_minutes: float = Field(default=0, ge=0)
    max_gap_minutes: float | None = Field(default=None, gt=0)
    band_edges_kw: list[float] = Field(
        default_factory=list,
        max_length=3,
        description="可选：把空档里每条记录按功率分带的界线（kW，从小到大，1-3 个）；分带只是计数，不说明是待机还是漏记生产",
    )

    @model_validator(mode="after")
    def ordered(self) -> "ProductionGapsParams":
        if self.max_gap_minutes is not None and self.max_gap_minutes < self.min_gap_minutes:
            raise ValueError("max_gap_minutes must not be below min_gap_minutes")
        if any(edge <= 0 for edge in self.band_edges_kw):
            raise ValueError("band_edges_kw must be above 0")
        _ascending(self.band_edges_kw, "band_edges_kw")
        return self


class ThresholdSegmentsParams(Thresholds):
    comparator: Literal["below", "at_least", "between"] = Field(
        description="逐条记录的功率怎样算满足：below 低于 high_kw；at_least 不低于 low_kw；between 两者之间"
    )
    low_kw: float | None = Field(default=None, ge=0)
    high_kw: float | None = Field(default=None, gt=0)
    min_minutes: float = Field(gt=0, description="连续满足至少多长才算一段（知识库无预设，必须给）")
    exclude: Exclusion | None = Field(default=None, description="落在这些时间里的记录不参与，例如生产批次")

    @model_validator(mode="after")
    def bounds(self) -> "ThresholdSegmentsParams":
        need = {"below": ("high_kw",), "at_least": ("low_kw",), "between": ("low_kw", "high_kw")}[self.comparator]
        missing = [name for name in need if getattr(self, name) is None]
        if missing:
            raise ValueError(f"comparator {self.comparator} needs {missing}")
        extra = [name for name in ("low_kw", "high_kw") if name not in need and getattr(self, name) is not None]
        if extra:
            raise ValueError(f"comparator {self.comparator} does not use {extra}")
        if self.comparator == "between" and self.high_kw <= self.low_kw:  # type: ignore[operator]
            raise ValueError("high_kw must be above low_kw")
        return self


class PowerEventsParams(Thresholds):
    on_kw: float = Field(gt=0, description="功率不低于此值的记录算在运行（知识库无预设，必须给）")
    merge_gap_minutes: float = Field(
        ge=0, description="两段运行之间低于 on_kw 的间隔不超过这么长就连成一次事件（OP037 maximum_gap）"
    )
    min_minutes: float = Field(gt=0, description="连起来后至少多长才算一次事件")


class StateWindowsParams(Thresholds):
    state: Literal["own_running", "plant_running", "plant_stopped", "unknown", "no_record"] = Field(
        description="状态轴上的状态："
        + "；".join(f"{name} {text}" for name, text in STATES.items())
    )
    min_minutes: float = Field(default=0, ge=0, description="一段状态至少多长才算一次出现")
    max_minutes: float | None = Field(default=None, gt=0, description="一段状态最长多长（例如只看短间隙）")

    @model_validator(mode="after")
    def ordered(self) -> "StateWindowsParams":
        if self.max_minutes is not None and self.max_minutes < self.min_minutes:
            raise ValueError("max_minutes must not be below min_minutes")
        return self


class BatchIntensityParams(_Params):
    machine: str = Field(min_length=1, description="生产记录里的机台名，须已在 manifest 里挂到这块表")
    product: str | None = Field(default=None, min_length=1, description="只看这种产品的批次")
    min_kwh_per_unit: float | None = Field(
        default=None, ge=0, description="单耗不低于此值才算命中（单位 kWh/数量单位）"
    )


@dataclass(frozen=True)
class MethodContext:
    period: tuple[datetime, datetime]
    calendar: list[CalendarWindow] | None = None
    batches: list[Batch] | None = None
    machines: dict[str, list[str]] = field(default_factory=dict)
    # What the production quantity counts (input / output / qualified) and its unit, as a person declared them.
    quantity_basis: str | None = None
    quantity_unit: str | None = None
    product_group_columns: tuple[str, ...] = ()
    # The state axis cut from the batches; computed from them when not given.
    segments: list[Segment] | None = None

    def state_segments(self) -> list[Segment] | None:
        if self.batches is None:
            return None
        if self.segments is not None:
            return self.segments
        return segments(self.batches, self.period)[0]


UNKNOWN_HIT = "无数据，无法判断"


@dataclass
class Occurrence:
    """One time the method's pattern occurs.

    ``judged`` says a threshold was set.  A judged occurrence with no data
    has ``hit`` None: it is unknown, not a miss.
    """

    measure: WindowMeasure
    hit: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    judged: bool = False

    def as_dict(self) -> dict[str, Any]:
        value = self.measure.as_dict()
        if self.judged:
            value["hit"] = UNKNOWN_HIT if self.hit is None else self.hit
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
    thresholded: bool = False

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
    operators: tuple[str, ...] = ()
    scenarios: tuple[str, ...] = ()
    kb_note: str = ""

    def menu_entry(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "needs": sorted(self.needs),
            "operators": list(self.operators),
            "kb_scenarios": list(self.scenarios),
            "kb_note": self.kb_note,
            "params_schema": self.params.model_json_schema(),
        }


def _thresholded(params: Thresholds) -> bool:
    return params.min_kwh is not None or params.min_avg_kw is not None


def _hit(measure: WindowMeasure, params: Thresholds) -> bool | None:
    """Whether the occurrence reaches the thresholds; None when there is nothing to judge.

    No records, or no value for a threshold, is unknown rather than a miss.
    """

    if not _thresholded(params) or measure.records == 0:
        return None
    checks = []
    if params.min_kwh is not None:
        if measure.energy_kwh is None:
            return None
        checks.append(measure.energy_kwh >= params.min_kwh)
    if params.min_avg_kw is not None:
        if measure.avg_kw is None:
            return None
        checks.append(measure.avg_kw >= params.min_avg_kw)
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
    judged = _thresholded(params)
    occurrences = []
    for start, end in windows:
        measure = series.measure(start, end)
        occurrences.append(Occurrence(measure, _hit(measure, params), judged=judged))
    return MethodRun(method_id, series.meter_id, "ok", occurrences=occurrences, thresholded=judged)


Span = tuple[datetime, datetime]


def _merge(spans: list[Span]) -> list[Span]:
    """Spans that overlap or touch, joined (OP004 union)."""

    merged: list[Span] = []
    for begin, finish in sorted(spans):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
        else:
            merged.append((begin, finish))
    return merged


def _subtract(windows: list[Span], spans: list[Span]) -> list[Span]:
    """What is left of each window once the spans are taken out (OP004 difference)."""

    cuts = _merge(spans)
    pieces: list[Span] = []
    for begin, finish in windows:
        cursor = begin
        for low, high in cuts:
            if high <= cursor or low >= finish:
                continue
            if low > cursor:
                pieces.append((cursor, low))
            cursor = max(cursor, high)
            if cursor >= finish:
                break
        if cursor < finish:
            pieces.append((cursor, finish))
    return pieces


def blocked_by_incomplete(pieces: list[Span], incomplete: list[Batch]) -> set[int]:
    """Pieces an incomplete batch may fall in, so they cannot be measured as free of it.

    A batch with only a start ran from it for an unknown time: the first
    piece that ends after the start may hold it.  A batch with only an end
    ran for an unknown time up to it: the last piece that starts before it
    may hold it.  End not after start: either time may be the wrong one, so
    what either alone would block is blocked, and every piece between them.
    """

    blocked: set[int] = set()
    for batch in incomplete:
        if batch.start is not None:
            later = [number for number, piece in enumerate(pieces) if piece[1] > batch.start]
            blocked.update(later[:1])
        if batch.end is not None:
            earlier = [number for number, piece in enumerate(pieces) if piece[0] < batch.end]
            blocked.update(earlier[-1:])
        if batch.start is not None and batch.end is not None:
            low, high = min(batch.start, batch.end), max(batch.start, batch.end)
            blocked.update(number for number, piece in enumerate(pieces) if piece[0] <= high and piece[1] >= low)
    return blocked


@dataclass
class _Excluded:
    spans: list[Span]
    incomplete: list[Batch]
    problem: str | None = None


def _exclusion(exclude: Exclusion | None, ctx: MethodContext) -> _Excluded:
    if exclude is None:
        return _Excluded([], [])
    spans: list[Span] = []
    incomplete: list[Batch] = []
    if exclude.production_machines:
        if ctx.batches is None:
            return _Excluded([], [], "要扣掉生产批次，但 manifest 里没有生产记录")
        known = {batch.machine for batch in ctx.batches}
        unknown = sorted(set(exclude.production_machines) - known)
        if unknown:
            return _Excluded([], [], f"生产记录里没有机台 {unknown}")
        for batch in ctx.batches:
            if batch.machine not in exclude.production_machines:
                continue
            if batch.problem is None:
                assert batch.start is not None and batch.end is not None
                spans.append((batch.start, batch.end))
            else:
                incomplete.append(batch)
    if exclude.calendar_states:
        if ctx.calendar is None:
            return _Excluded([], [], "要扣掉日历状态，但 manifest 里没有经人确认的日历")
        states = {window.state for window in ctx.calendar}
        unknown = sorted(set(exclude.calendar_states) - states)
        if unknown:
            return _Excluded([], [], f"日历里没有状态 {unknown}；现有状态：{sorted(states)}")
        spans += [(window.start, window.end) for window in ctx.calendar if window.state in exclude.calendar_states]
    return _Excluded(_merge(spans), incomplete)


def _excluded_windows(
    method_id: str,
    series: MeterSeries,
    params: Any,
    windows: list[Span],
    ctx: MethodContext,
) -> MethodRun:
    """Measure the windows once the excluded time is taken out; each piece left is one occurrence."""

    excluded = _exclusion(getattr(params, "exclude", None), ctx)
    if excluded.problem:
        return MethodRun(method_id, series.meter_id, "needs_input", excluded.problem)
    if getattr(params, "exclude", None) is None:
        return _windowed(method_id, series, params, windows)
    pieces = _subtract(windows, excluded.spans)
    step = timedelta(minutes=series.interval_minutes)
    # A piece shorter than one record cannot hold a whole record: nothing to measure there.
    short = [piece for piece in pieces if piece[1] - piece[0] < step]
    pieces = [piece for piece in pieces if piece[1] - piece[0] >= step]
    blocked = blocked_by_incomplete(pieces, excluded.incomplete)
    kept = [piece for number, piece in enumerate(pieces) if number not in blocked]
    run = _windowed(method_id, series, params, kept)
    run.summary["windows_before_exclusion"] = len(windows)
    run.summary["pieces_too_short"] = len(short)
    run.summary["pieces_skipped_incomplete_batch"] = len(blocked)
    return run


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
    return _excluded_windows("daily-clock-window", series, params, windows, ctx)


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
    run = _excluded_windows("calendar-state", series, params, windows, ctx)
    run.summary["rows_merged"] = merged
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
    # Gaps an incomplete batch may fall in are not used.
    blocked = blocked_by_incomplete(
        [(gap[0], gap[1]) for gap in gaps], [batch for batch in own if batch.problem is not None]
    )
    judged = _thresholded(params)
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
        extra: dict[str, Any] = {"after_batch_row": before_row, "before_batch_row": after_row}
        if params.band_edges_kw:
            extra.update(power_bands(series, measure, params.band_edges_kw))
        occurrences.append(Occurrence(measure, _hit(measure, params), extra, judged=judged))
    run = MethodRun("production-gaps", series.meter_id, "ok", occurrences=occurrences, thresholded=judged)
    if params.band_edges_kw:
        run.summary["band_totals"] = _band_totals(occurrences)
    run.summary["batches_unusable"] = len(own) - len(usable)
    run.summary["batches_overlapping"] = overlapping
    run.summary["gaps_skipped_unusable_batch"] = crossing
    return run


def band_labels(edges: list[float]) -> list[str]:
    bounds = [None, *edges, None]
    labels = []
    for low, high in zip(bounds, bounds[1:], strict=False):
        if low is None:
            labels.append(f"低于 {high:g} kW")
        elif high is None:
            labels.append(f"不低于 {low:g} kW")
        else:
            labels.append(f"{low:g}-{high:g} kW")
    return labels


def power_bands(series: MeterSeries, measure: WindowMeasure, edges: list[float]) -> dict[str, Any]:
    """Hours and energy of the window's records in each power band, and the time no record covers.

    Each record goes to the band of its own power; ``unknown_hours`` is the
    window's time that no whole record covers (missing data and records cut
    by the window's edge), never filled in.
    """

    labels = band_labels(edges)
    hours = [0.0] * len(labels)
    energy = [0.0] * len(labels)
    powers = series.record_powers()
    step_h = series.interval_minutes / 60
    for index in range(measure.first_index, measure.last_index):
        power = powers[index]
        if np.isnan(power):
            continue
        band = int(np.searchsorted(np.array(edges), power, "right"))
        length = float(series.durations_h[index]) if series.kind != "instant" else step_h
        hours[band] += length
        if series.has_energy:
            energy[band] += float(series.values[index])
    window_h = measure.window_minutes / 60
    return {
        "bands": [
            {"band": label, "hours": _r(hours[number]), "kwh": _r(energy[number]) if series.has_energy else None}
            for number, label in enumerate(labels)
        ],
        "unknown_hours": _r(max(0.0, window_h - measure.covered_minutes / 60)),
    }


def _band_totals(occurrences: list[Occurrence]) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for item in occurrences:
        for band in item.extra.get("bands", []):
            entry = totals.setdefault(band["band"], {"band": band["band"], "hours": 0.0, "kwh": None})
            entry["hours"] = _r(entry["hours"] + band["hours"])
            if band["kwh"] is not None:
                entry["kwh"] = _r((entry["kwh"] or 0.0) + band["kwh"])
    unknown = sum(item.extra.get("unknown_hours", 0.0) for item in occurrences)
    return [*totals.values(), {"band": "无记录", "hours": _r(unknown), "kwh": None}]


def _qualifies(power: float, params: ThresholdSegmentsParams) -> bool:
    if np.isnan(power):
        return False
    if params.comparator == "below":
        return power < params.high_kw  # type: ignore[operator]
    if params.comparator == "at_least":
        return power >= params.low_kw  # type: ignore[operator]
    return params.low_kw <= power < params.high_kw  # type: ignore[operator]


def _runs(series: MeterSeries, keep: np.ndarray) -> list[tuple[int, int]]:
    """Index ranges [lo, hi) of records that qualify one after another with no data missing (OP005)."""

    runs: list[tuple[int, int]] = []
    lo: int | None = None
    for index in range(len(series)):
        if keep[index] and lo is not None and series.follows(index):
            continue
        if lo is not None:
            runs.append((lo, index))
            lo = None
        if keep[index]:
            lo = index
    if lo is not None:
        runs.append((lo, len(series)))
    return runs


def _in_period(series: MeterSeries, ctx: MethodContext) -> np.ndarray:
    start, end = ctx.period
    a = np.datetime64(pd.Timestamp(start), "ns")
    b = np.datetime64(pd.Timestamp(end), "ns")
    return (series.starts >= a) & (series.ends <= b) if series.kind != "instant" else (series.starts >= a) & (series.starts < b)


def _outside(series: MeterSeries, spans: list[Span]) -> np.ndarray:
    """Records that share no time with any of the spans."""

    free = np.ones(len(series), dtype=bool)
    for low, high in spans:
        a = np.datetime64(pd.Timestamp(low), "ns")
        b = np.datetime64(pd.Timestamp(high), "ns")
        if series.kind == "instant":
            free &= ~((series.starts >= a) & (series.starts < b))
        else:
            free &= ~((series.starts < b) & (series.ends > a))
    return free


def _threshold_segments(series: MeterSeries, params: ThresholdSegmentsParams, ctx: MethodContext) -> MethodRun:
    problem = _threshold_problem(series, params)
    if problem:
        return MethodRun("threshold-segments", series.meter_id, "not_applicable", problem)
    excluded = _exclusion(params.exclude, ctx)
    if excluded.problem:
        return MethodRun("threshold-segments", series.meter_id, "needs_input", excluded.problem)
    powers = series.record_powers()
    keep = np.array([_qualifies(float(power), params) for power in powers], dtype=bool) if len(series) else np.zeros(0, bool)
    keep &= _in_period(series, ctx) & _outside(series, excluded.spans)
    windows = []
    short = 0
    for lo, hi in _runs(series, keep):
        begin, finish = series.record_start(lo), series.record_end(hi - 1)
        if (finish - begin).total_seconds() / 60 < params.min_minutes:
            short += 1
            continue
        windows.append((begin, finish))
    blocked = blocked_by_incomplete(windows, excluded.incomplete)
    kept = [window for number, window in enumerate(windows) if number not in blocked]
    run = _windowed("threshold-segments", series, params, kept)
    run.summary["segments_shorter_than_min"] = short
    run.summary["segments_skipped_incomplete_batch"] = len(blocked)
    return run


def _power_events(series: MeterSeries, params: PowerEventsParams, ctx: MethodContext) -> MethodRun:
    problem = _threshold_problem(series, params)
    if problem:
        return MethodRun("power-events", series.meter_id, "not_applicable", problem)
    powers = series.record_powers()
    keep = np.array([not np.isnan(power) and power >= params.on_kw for power in powers], dtype=bool)
    keep &= _in_period(series, ctx) if len(series) else keep
    runs = _runs(series, keep)
    # OP037: join runs whose gap is short and fully recorded; a gap with data
    # missing is not bridged, because what ran there is unknown.
    events: list[list[tuple[int, int]]] = []
    for lo, hi in runs:
        if events:
            last_hi = events[-1][-1][1]
            gap_start, gap_end = series.record_end(last_hi - 1), series.record_start(lo)
            gap_minutes = (gap_end - gap_start).total_seconds() / 60
            recorded = all(series.follows(index) for index in range(last_hi, lo + 1))
            if gap_minutes <= params.merge_gap_minutes and recorded:
                events[-1].append((lo, hi))
                continue
        events.append([(lo, hi)])
    judged = _thresholded(params)
    occurrences = []
    short = 0
    for parts in events:
        begin, finish = series.record_start(parts[0][0]), series.record_end(parts[-1][1] - 1)
        if (finish - begin).total_seconds() / 60 < params.min_minutes:
            short += 1
            continue
        measure = series.measure(begin, finish)
        bridged = [
            series.measure(series.record_end(before[1] - 1), series.record_start(after[0]))
            for before, after in zip(parts, parts[1:], strict=False)
        ]
        running = powers[measure.first_index : measure.last_index]
        extra = {
            "runs_joined": len(parts),
            "bridged_minutes": _r(sum(item.window_minutes for item in bridged)),
            "bridged_kwh": _r(sum(item.energy_kwh or 0.0 for item in bridged)) if series.has_energy else None,
            "peak_kw": _r(float(np.nanmax(running))) if len(running) else None,
        }
        occurrences.append(Occurrence(measure, _hit(measure, params), extra, judged=judged))
    run = MethodRun("power-events", series.meter_id, "ok", occurrences=occurrences, thresholded=judged)
    run.summary["events_shorter_than_min"] = short
    return run


def _state_windows(series: MeterSeries, params: StateWindowsParams, ctx: MethodContext) -> MethodRun:
    method_id = "state-windows"
    pieces = ctx.state_segments()
    if pieces is None:
        return MethodRun(method_id, series.meter_id, "needs_input", "manifest 里没有生产记录，分不出状态")
    machines = ctx.machines.get(series.meter_id, [])
    if params.state == "own_running" and not machines:
        return MethodRun(
            method_id, series.meter_id, "not_applicable", f"表 {series.meter_id} 没有挂机台，没有“本表机台在生产”这个状态"
        )
    problem = _threshold_problem(series, params)
    if problem:
        return MethodRun(method_id, series.meter_id, "not_applicable", problem)
    stretches = state_windows(pieces, machines)[params.state]
    kept = []
    for begin, finish in stretches:
        minutes = (finish - begin).total_seconds() / 60
        if minutes < params.min_minutes or (params.max_minutes is not None and minutes > params.max_minutes):
            continue
        kept.append((begin, finish))
    run = _windowed(method_id, series, params, kept)
    run.summary["stretches_of_state"] = len(stretches)
    run.summary["stretches_outside_length"] = len(stretches) - len(kept)
    return run


def _batch_intensity(series: MeterSeries, params: BatchIntensityParams, ctx: MethodContext) -> MethodRun:
    method_id = "batch-intensity"
    if ctx.batches is None:
        return MethodRun(method_id, series.meter_id, "needs_input", "manifest 里没有生产记录")
    if not ctx.quantity_basis:
        return MethodRun(
            method_id,
            series.meter_id,
            "needs_input",
            "生产记录的数量口径没有经人声明（manifest 的 production.quantity_basis：input 投入量、output 成品量、qualified 合格品量）",
        )
    if params.machine not in ctx.machines.get(series.meter_id, []):
        mapped = ctx.machines.get(series.meter_id, [])
        return MethodRun(
            method_id,
            series.meter_id,
            "needs_input",
            f"机台 {params.machine} 没有在 manifest 里挂到表 {series.meter_id}（已挂：{mapped or '无'}）",
        )
    if not series.has_energy:
        return MethodRun(method_id, series.meter_id, "not_applicable", f"表 {series.meter_id} 是瞬时功率，没有电量，算不了单耗")
    own = [
        batch
        for batch in ctx.batches
        if batch.machine == params.machine and (params.product is None or batch.product == params.product)
    ]
    usable = [batch for batch in own if batch.problem is None]
    start, end = ctx.period
    judged = params.min_kwh_per_unit is not None
    occurrences = []
    for batch in sorted(usable, key=lambda item: (item.start, item.end)):
        assert batch.start is not None and batch.end is not None
        if batch.start < start or batch.end > end:
            continue
        measure = series.measure(batch.start, batch.end)
        shared = any(
            other is not batch and other.start < batch.end and other.end > batch.start  # type: ignore[operator]
            for other in usable
        )
        intensity = None
        reason = None
        if shared:
            reason = "与同机台另一批次时间重叠，电量分不开"
        elif batch.quantity is None:
            reason = "没有数量"
        elif batch.quantity <= 0:
            reason = "数量不大于 0"
        elif measure.energy_kwh is None:
            reason = "批次时段里没有记录"
        else:
            intensity = measure.energy_kwh / batch.quantity
        hit = None
        if judged and intensity is not None:
            hit = intensity >= params.min_kwh_per_unit  # type: ignore[operator]
        extra = {
            "batch_row": batch.row,
            "product": batch.product,
            "groups": dict(batch.groups),
            "quantity": batch.quantity,
            "kwh_per_unit": _r(intensity, 4) if intensity is not None else None,
            "no_intensity_reason": reason,
        }
        occurrences.append(Occurrence(measure, hit, extra, judged=judged))
    run = MethodRun(method_id, series.meter_id, "ok", occurrences=occurrences, thresholded=judged)
    values = [item.extra["kwh_per_unit"] for item in occurrences if item.extra["kwh_per_unit"] is not None]
    pairs = [
        (item.measure.energy_kwh, item.extra["quantity"])
        for item in occurrences
        if item.extra["kwh_per_unit"] is not None
    ]
    mean = statistics.fmean(values) if values else None
    std = statistics.stdev(values) if len(values) > 1 else None
    by_product: dict[str, list[tuple[float, float, float]]] = {}
    by_group: dict[str, dict[str, list[tuple[float, float, float]]]] = {}
    for item in occurrences:
        if item.extra["kwh_per_unit"] is not None:
            row = (item.extra["kwh_per_unit"], item.measure.energy_kwh or 0.0, item.extra["quantity"])
            by_product.setdefault(item.extra["product"] or "未写产品", []).append(row)
            for column in ctx.product_group_columns:
                value = item.extra["groups"].get(column) or "未写"
                by_group.setdefault(column, {}).setdefault(value, []).append(row)

    def _groups(rows_by: dict[str, list[tuple[float, float, float]]]) -> dict[str, dict[str, Any]]:
        return {
            label: {
                "count": len(rows),
                "median": _r(statistics.median(row[0] for row in rows), 4),
                "of_sums": _r(sum(row[1] for row in rows) / sum(row[2] for row in rows), 4),
            }
            for label, rows in sorted(rows_by.items())
        }
    run.summary.update(
        {
            "quantity_basis": QUANTITY_BASES.get(ctx.quantity_basis or "", ctx.quantity_basis),
            # Products are not mixed in one comparison (knowledge base 3.4.4, 4.3.1).
            "by_product": _groups(by_product) if params.product is None and len(by_product) > 1 else None,
            # The same batches split once per grouping a person named (product_group_columns).
            "by_group": {column: _groups(by_group.get(column, {})) for column in ctx.product_group_columns}
            if by_group
            else None,
            "intensity_unit": f"kWh/{ctx.quantity_unit}" if ctx.quantity_unit else "kWh/数量单位",
            "batches_unusable": len(own) - len(usable),
            "batches_without_intensity": sum(1 for item in occurrences if item.extra["kwh_per_unit"] is None),
            "intensity_count": len(values),
            "intensity_mean": _r(mean, 4) if mean is not None else None,
            "intensity_median": _r(statistics.median(values), 4) if values else None,
            "intensity_std": _r(std, 4) if std is not None else None,
            "intensity_cv": _r(std / mean, 4) if std is not None and mean else None,
            "intensity_min": _r(min(values), 4) if values else None,
            "intensity_max": _r(max(values), 4) if values else None,
            # Sum over sum is not the mean of the ratios; both are kept.
            "intensity_of_sums": _r(sum(kwh for kwh, _q in pairs) / sum(q for _k, q in pairs), 4) if pairs else None,
        }
    )
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
            operators=("OP007", "OP013"),
            kb_note="相当于直接给定的状态窗（named_state_windows）",
        ),
        MethodSpec(
            "daily-clock-window",
            "每天固定时段",
            "在全期每一天（可只看工作日或周末）量同一个钟点时段的电量或平均功率，例如夜间 22:00-06:00；"
            "可扣掉生产批次或日历状态的时间，扣完后剩下的每一段算一次出现。",
            DailyClockParams,
            frozenset(),
            True,
            _daily_clock,
            operators=("OP026", "OP004", "OP007", "OP013"),
            scenarios=("ZQ03", "BS-15", "HB-17"),
            kb_note="对应场景里“服务窗外运行”的取窗部分",
        ),
        MethodSpec(
            "calendar-state",
            "日历状态时段",
            "量经人确认的日历里某种状态（例如非生产）的全部时段；同状态相接或重叠的行先合并，"
            "可再扣掉生产批次或其他日历状态。需要 manifest 提供日历。",
            CalendarStateParams,
            frozenset({"calendar"}),
            True,
            _calendar_state,
            operators=("OP004", "OP007", "OP013"),
            scenarios=("ZQ19", "BS-10", "BS-16"),
            kb_note="场景里与参考电量的差值属于基准，这里不算，留给人",
        ),
        MethodSpec(
            "threshold-segments",
            "功率阈值连续段",
            "逐条记录比较功率（低于、不低于或介于给定值），连续满足且中间不缺数、持续不短于 min_minutes 的算一段；"
            "可扣掉生产批次或日历状态里的记录。量每段的电量或平均功率。",
            ThresholdSegmentsParams,
            frozenset(),
            False,
            _threshold_segments,
            operators=("OP005", "OP004", "OP007", "OP013"),
            scenarios=("BS-08", "BS-09", "BS-21"),
            kb_note="场景里与参考电量的差值属于基准，这里不算；分段只是数据上满足条件，不说明是待机",
        ),
        MethodSpec(
            "power-events",
            "按功率识别的运行事件",
            "功率不低于 on_kw 的连续记录算一段运行，两段之间的低功率间隔不超过 merge_gap_minutes 且没有缺数时连成一次事件；"
            "量每次事件的电量、平均功率、峰值功率，被连起来的间隔单列分钟数和电量。",
            PowerEventsParams,
            frozenset(),
            False,
            _power_events,
            operators=("OP005", "OP037", "OP016", "OP007"),
            scenarios=("HB-01", "HB-02", "HB-03", "HB-04", "HB-05"),
            kb_note="事件长短、电量是事实；是否超时或异常要和人给的参考比，这里不判断",
        ),
        MethodSpec(
            "production-gaps",
            "生产批次间隙",
            "按生产记录找同一机台的空档：从此前所有批次里最晚的结束到下一批开始；可能有时间不全的批次落在里面的空档不用。"
            "量空档里这块表的电量或平均功率；给了 band_edges_kw 时再按每条记录的功率分带计时长和电量，没有记录的时间单列。"
            "需要生产记录，且机台已挂到这块表。",
            ProductionGapsParams,
            frozenset({"production"}),
            True,
            _production_gaps,
            operators=("OP004", "OP007", "OP001", "OP013"),
            scenarios=("BS-04",),
            kb_note="分带只是按功率计数，不认定真待机、漏记生产或断电；生产记录和电表是否同一时钟没有核对（OP045 未做）",
        ),
        MethodSpec(
            "state-windows",
            "状态轴时段",
            "按生产记录切出的状态轴，量某个状态（例如全厂停产 plant_stopped、本表机台没生产而别的机台在生产 plant_running）"
            "每一段里这块表的电量或平均功率；可限定一段的最短、最长时长。没挂机台的表（公辅、照明等）也能用，"
            "用来看全厂停产时它是否还在运行。需要生产记录。",
            StateWindowsParams,
            frozenset({"production_any"}),
            True,
            _state_windows,
            operators=("OP004", "OP007", "OP013"),
            scenarios=("BS-16", "BS-14", "ZQ19"),
            kb_note="状态只按生产记录定，停产不等于没有服务需求；与参考电量的差值属于基准，这里不算",
        ),
        MethodSpec(
            "batch-intensity",
            "批次单耗分布",
            "量每个批次时段里这块表的电量，除以批次数量得单耗（数量口径由人在 manifest 声明），给出分布："
            "均值、中位数、标准差、变异系数、最小最大值，以及总电量÷总数量。数量缺失、为 0、与其他批次时间重叠的批次不算单耗。",
            BatchIntensityParams,
            frozenset({"production", "quantity"}),
            False,
            _batch_intensity,
            operators=("OP004", "OP007", "OP015", "OP009"),
            scenarios=("ZQ06", "BS-01", "BS-02", "BS-03"),
            kb_note="可比组资格和对标基准由人定，这里只给分布",
        ),
    )
}


def readiness(
    spec: MethodSpec, meter_id: str, has_energy: bool, ctx: MethodContext, records: int | None = None
) -> tuple[str, str | None]:
    """Whether the data could feed the method on this meter: ready, needs_input or not_applicable.

    Only what the inputs decide; thresholds and parameters are not judged.
    ``records`` is the meter's count of usable records, when known.
    """

    if records == 0:
        return "not_applicable", "这块表没有可用记录"
    if "calendar" in spec.needs and ctx.calendar is None:
        return "needs_input", "manifest 里没有经人确认的日历"
    if "production_any" in spec.needs and ctx.batches is None:
        return "needs_input", "manifest 里没有生产记录"
    if "production" in spec.needs:
        if ctx.batches is None:
            return "needs_input", "manifest 里没有生产记录"
        if not ctx.machines.get(meter_id):
            return "needs_input", f"没有机台挂到表 {meter_id}"
    if "quantity" in spec.needs and not ctx.quantity_basis:
        return "needs_input", "生产记录的数量口径没有经人声明"
    if spec.id == "batch-intensity" and not has_energy:
        return "not_applicable", "瞬时功率表没有电量"
    return "ready", None


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


def resolved_params(method_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """The parameters the program actually uses, defaults filled in."""

    return parse_params(method_id, params).model_dump(mode="json")


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
        run.summary = {
            **summarize(run.occurrences, series.has_energy, meter_total_kwh, thresholded=run.thresholded),
            **run.summary,
        }
    return run


def summarize(
    occurrences: list[Occurrence],
    has_energy: bool,
    meter_total_kwh: float | None,
    *,
    thresholded: bool = False,
) -> dict[str, Any]:
    """Counts and sums over the occurrences.

    With thresholds, ``hits`` counts the occurrences that reach them and
    ``unknown`` those with nothing to judge (no data); unknown ones are
    neither hits nor misses and add nothing to ``hit_kwh``.
    """

    with_data = [item for item in occurrences if item.measure.records > 0]
    powers = [item.measure.avg_kw for item in with_data if item.measure.avg_kw is not None]
    hits = [item for item in occurrences if item.hit]
    any_threshold = thresholded or any(item.judged for item in occurrences)
    summary: dict[str, Any] = {
        "occurrences": len(occurrences),
        "with_data": len(with_data),
        "hits": len(hits) if any_threshold else None,
        "unknown": sum(1 for item in occurrences if item.hit is None) if any_threshold else None,
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
    """The energy the item involves: hit energy, or all occurrences."""

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
    "UNKNOWN_HIT",
    "item_kwh",
    "overlapping_windows",
    "menu_entries",
    "parse_params",
    "readiness",
    "resolved_params",
    "run_method",
    "summarize",
    "top_occurrences",
]
