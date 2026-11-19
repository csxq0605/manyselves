"""The foundation: states, events and measures that every finder reads by handle.

After the files are read the program builds, with no model:

- the company model (``company_model``): objects, links, inferences, data gaps,
  the parameters in force (A04) and the confirmations applied (A05);
- the state layer: each machine's registered state from the production
  records (batches from their start column, which is 开工), each production
  meter's power band from its own data, the plant state on a common time
  grid, and each day's status;
- the measure tables (``handles``): data quality, energy by object and plant
  state, conditional load, registered state against power band, demand
  following, batches, comparable groups by model, transitions, time-of-use
  bands and peak coincidence.

Every number is a description of the data ("统计参考，资格未核"): nothing here
says what is a loss, what is necessary, what a baseline is or what anything
is worth.  A value that rests on a parameter nobody set carries that
parameter's id, so the checklist can ask for it.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .company_model import SCHEMA as MODEL_SCHEMA
from .company_model import build_registry, peer_group_status, task_record
from .grid import export_grid
from .handles import Column, Foundation, HandleError, Table, numbers_match
from .inputs import Batch
from .preparation import PREFERENCES, ParameterSet, day_range, source_register
from .series import MeterSeries, days_between
from .states import _unknown_spans, coverage_of
from .store import SiteData, read_json, write_json

FOUNDATION_DIR = "foundation"
MODEL_FILE = "company-model.json"
REFERENCE_NOTE = "统计参考，资格未核：只描述数据，不说明是否损耗、是否必要"

# Registered state of a machine (or of a meter, from its machines), lowest priority first.
NO_RECORD, BETWEEN, UNKNOWN, PREPARING, PROCESSING, CONFLICT, NOT_COMMISSIONED = range(7)
REGISTERED = {
    NO_RECORD: "NO_RECORD",
    BETWEEN: "BETWEEN_TASKS",
    UNKNOWN: "UNKNOWN",
    PREPARING: "PREPARING",
    PROCESSING: "PROCESSING",
    CONFLICT: "CONFLICT",
    NOT_COMMISSIONED: "NOT_COMMISSIONED",
}
REGISTERED_LABELS = {
    "NO_RECORD": "生产记录之外（本机第一批之前、最后一批之后）",
    "BETWEEN_TASKS": "同机两批之间",
    "UNKNOWN": "有时间不全的批次，判断不了",
    "PREPARING": "批次准备段（开工到批内时刻）",
    "PROCESSING": "批次时段（有批内时刻时指加工段）",
    "CONFLICT": "同机两批时间重叠",
    "NOT_COMMISSIONED": "经确认未投用",
}
BANDS = {0: "OFF", 1: "LOW", 2: "RUN"}
PLANT_STATES = ("NO_RECORD", "UNKNOWN", "ALL_IDLE", "TRANSITION_ONLY", "PARTIAL", "FULL")
PLANT_LABELS = {
    "NO_RECORD": "生产记录覆盖期之外",
    "UNKNOWN": "没有在产批次，但有时间不全的批次，判断不了",
    "ALL_IDLE": "没有登记批次，也没有产线表处在 RUN 档",
    "TRANSITION_ONLY": "没有批次在加工，但有批次在准备段，或有产线表处在 RUN 档",
    "PARTIAL": "在加工的机台少于在用机台 × P05",
    "FULL": "在加工的机台不少于在用机台 × P05",
}
BAND_KEYS = ("band_reference_quantile", "band_off_fraction", "band_run_fraction")


def _iso(moment: Any) -> str | None:
    if moment is None:
        return None
    return pd.Timestamp(moment).isoformat(sep=" ")


def _r(value: float | None, digits: int = 3) -> float | None:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return round(float(value), digits)


def nearest_rank(values: np.ndarray | list[float], quantile: float) -> float | None:
    data = np.sort(np.asarray(values, dtype=float)[np.isfinite(np.asarray(values, dtype=float))])
    if not len(data):
        return None
    rank = max(1, math.ceil(quantile * len(data)))
    return float(data[min(rank, len(data)) - 1])


@dataclass
class _Records:
    """One meter's records as arrays."""

    start: np.ndarray
    end: np.ndarray
    mid: np.ndarray
    hours: np.ndarray
    power: np.ndarray
    energy: np.ndarray | None
    follows: np.ndarray

    @classmethod
    def of(cls, series: MeterSeries) -> "_Records":
        start = series.starts.astype("datetime64[ns]")
        if series.kind == "instant":
            step = np.timedelta64(series.interval_minutes, "m")
            nxt = np.append(start[1:], start[-1] + step) if len(start) else start
            end = np.minimum(start + step, nxt) if len(start) else start
        else:
            end = series.ends.astype("datetime64[ns]")
        hours = (end - start).astype("timedelta64[s]").astype(float) / 3600
        follows = np.zeros(len(start), dtype=bool)
        if len(start) > 1:
            follows[1:] = start[1:] == end[:-1]
        return cls(
            start=start,
            end=end,
            mid=start + (end - start) / 2,
            hours=hours,
            power=series.record_powers(),
            energy=series.values.astype(float) if series.has_energy else None,
            follows=follows,
        )


def _machine_codes(
    batches: list[Batch],
    unknown: list[tuple[datetime, datetime]],
    times: np.ndarray,
    commissioned: bool,
) -> np.ndarray:
    """The registered state of one machine at each of ``times``."""

    codes = np.full(len(times), NO_RECORD, dtype=np.int8)
    if not commissioned:
        codes[:] = NOT_COMMISSIONED
        return codes
    valid = sorted(
        (batch for batch in batches if batch.problem is None and batch.start and batch.end),
        key=lambda item: (item.start, item.end),
    )

    def mask(low: datetime, high: datetime) -> np.ndarray:
        return (times >= np.datetime64(pd.Timestamp(low), "ns")) & (times < np.datetime64(pd.Timestamp(high), "ns"))

    for before, after in zip(valid, valid[1:], strict=False):
        if after.start > before.end:  # type: ignore[operator]
            codes[mask(before.end, after.start)] = np.maximum(codes[mask(before.end, after.start)], BETWEEN)  # type: ignore[arg-type]
    for low, high in unknown:
        inside = mask(low, high)
        codes[inside] = np.maximum(codes[inside], UNKNOWN)
    count = np.zeros(len(times), dtype=np.int16)
    for batch in valid:
        inside = mask(batch.start, batch.end)  # type: ignore[arg-type]
        count += inside
        if batch.phase_start is not None and batch.phase_start > batch.start:  # type: ignore[operator]
            prepare = mask(batch.start, batch.phase_start)  # type: ignore[arg-type]
            codes[prepare] = np.maximum(codes[prepare], PREPARING)
            process = mask(batch.phase_start, batch.end)  # type: ignore[arg-type]
        else:
            process = inside
        codes[process] = np.maximum(codes[process], PROCESSING)
    codes[count >= 2] = CONFLICT
    return codes


def _bands(power: np.ndarray, reference: float | None, off: float, run: float) -> np.ndarray:
    bands = np.full(len(power), -1, dtype=np.int8)
    if reference is None or reference <= 0:
        return bands
    finite = np.isfinite(power)
    bands[finite & (power < off * reference)] = 0
    bands[finite & (power >= off * reference) & (power < run * reference)] = 1
    bands[finite & (power >= run * reference)] = 2
    return bands


def _cell_of(times: np.ndarray, grid_start: np.datetime64, step: np.timedelta64, cells: int) -> np.ndarray:
    index = ((times - grid_start) // step).astype(np.int64)
    index[(index < 0) | (index >= cells)] = -1
    return index


def _record_at(records: _Records, times: np.ndarray) -> np.ndarray:
    """The record holding each moment, or -1."""

    if not len(records.start):
        return np.full(len(times), -1, dtype=np.int64)
    place = np.searchsorted(records.start, times, "right") - 1
    ok = (place >= 0) & (times < records.end[np.clip(place, 0, None)])
    place[~ok] = -1
    return place


def _longest(hours: np.ndarray, follows: np.ndarray, selected: np.ndarray) -> float:
    longest = current = 0.0
    previous = False
    for index in range(len(hours)):
        if selected[index]:
            current = current + hours[index] if previous and follows[index] else hours[index]
            longest = max(longest, current)
            previous = True
        else:
            previous = False
            current = 0.0
    return longest


def _percentiles(values: np.ndarray) -> dict[str, float | None]:
    return {
        "p10_kw": _r(nearest_rank(values, 0.10)),
        "p50_kw": _r(nearest_rank(values, 0.50)),
        "p90_kw": _r(nearest_rank(values, 0.90)),
    }


def _fit(x: np.ndarray, y: np.ndarray) -> tuple[float | None, float | None, float | None]:
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if len(x) < 3 or float(np.ptp(x)) == 0:
        return None, None, None
    slope, intercept = np.polyfit(x, y, 1)
    predicted = intercept + slope * x
    total = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float(((y - predicted) ** 2).sum()) / total if total > 0 else None
    return float(intercept), float(slope), r2


class _Builder:
    def __init__(self, site: SiteData) -> None:
        self.site = site
        self.manifest = site.manifest
        self.params: ParameterSet = site.parameter_set()
        self.period = site.period()
        self.batches = site.batches() or []
        self.specs = {spec.id: spec for spec in self.manifest.meters}
        self.series = {meter_id: site.series(meter_id) for meter_id in self.specs}
        self.records = {meter_id: _Records.of(series) for meter_id, series in self.series.items()}
        self.tables: dict[str, Table] = {}
        confirmations = site.confirmations()
        self.confirmations = list(confirmations.confirmations) if confirmations else []
        start, end = self.period
        step_minutes = min(spec.interval_minutes for spec in self.specs.values())
        self.step = np.timedelta64(step_minutes, "m")
        self.step_h = step_minutes / 60
        self.grid_start = np.datetime64(pd.Timestamp(start), "ns")
        cells = int((np.datetime64(pd.Timestamp(end), "ns") - self.grid_start) // self.step)
        self.cells = max(cells, 0)
        self.grid = self.grid_start + np.arange(self.cells) * self.step
        self.grid_mid = self.grid + self.step / 2

    # Machines -----------------------------------------------------------------
    def machines(self) -> None:
        by_machine: dict[str, list[Batch]] = defaultdict(list)
        for batch in self.batches:
            if batch.machine:
                by_machine[batch.machine].append(batch)
        self.by_machine = dict(by_machine)
        self.coverage = coverage_of(self.batches) if self.batches else None
        unknown, self.unplaced = _unknown_spans(self.batches, self.coverage) if self.coverage else ([], 0)
        self.unknown: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
        for machine, span in unknown:
            self.unknown[machine].append(span)
        self.commissioned = {machine: True for machine in by_machine}
        for entry in self.confirmations:
            if entry.kind == "commissioning" and entry.subject in self.commissioned and entry.result != "rejected":
                self.commissioned[entry.subject] = bool(entry.commissioned)
        start, end = self.period
        self.in_use = sorted(
            machine
            for machine, own in by_machine.items()
            if self.commissioned[machine]
            and any(
                batch.start and batch.end and batch.start < end and batch.end > start
                for batch in own
            )
        )
        self.machine_meter = {machine: meter.id for meter in self.manifest.meters for machine in meter.production_machines}

    def codes_at(self, machine: str, times: np.ndarray) -> np.ndarray:
        return _machine_codes(self.by_machine.get(machine, []), self.unknown.get(machine, []), times, self.commissioned.get(machine, True))

    def meter_codes(self, meter_id: str, times: np.ndarray) -> np.ndarray:
        machines = [m for m in self.specs[meter_id].production_machines if m in self.by_machine]
        if not machines:
            return np.full(len(times), NO_RECORD, dtype=np.int8)
        codes = [self.codes_at(machine, times) for machine in machines]
        commissioned = [code for code, machine in zip(codes, machines, strict=True) if self.commissioned.get(machine, True)]
        if not commissioned:
            return codes[0]
        return np.maximum.reduce(commissioned)

    # States -------------------------------------------------------------------
    def states(self) -> None:
        self.production_meters = [
            meter_id for meter_id, spec in self.specs.items() if spec.production_machines and len(self.series[meter_id])
        ]
        self.own_codes: dict[str, np.ndarray] = {}
        self.band: dict[str, np.ndarray] = {}
        self.band_info: dict[str, dict[str, Any]] = {}
        for meter_id in self.production_meters:
            records = self.records[meter_id]
            codes = self.meter_codes(meter_id, records.mid)
            self.own_codes[meter_id] = codes
            quantile = self.params.value("band_reference_quantile", meter_id)
            off = self.params.value("band_off_fraction", meter_id)
            run = self.params.value("band_run_fraction", meter_id)
            source = records.power[codes == PROCESSING]
            if not np.isfinite(source).any() and self.params.value("band_reference_fallback"):
                source = records.power[codes == PREPARING]
            reference = nearest_rank(source, quantile) if len(source) else None
            self.band[meter_id] = _bands(records.power, reference, off, run)
            alternative = _bands(
                records.power,
                reference,
                (self.params.alternatives("band_off_fraction") or [off])[0],
                (self.params.alternatives("band_run_fraction") or [run])[0],
            )
            self.band_info[meter_id] = {
                "reference_kw": reference,
                "off": off,
                "run": run,
                "alternative": alternative,
                "assumptions": self.params.assumed_ids(BAND_KEYS, meter_id),
            }
        # Each production meter's band and power at the grid.
        self.band_at_cell: dict[str, np.ndarray] = {}
        self.power_at_cell: dict[str, np.ndarray] = {}
        for meter_id, records in self.records.items():
            place = _record_at(records, self.grid_mid)
            power = np.full(self.cells, np.nan)
            power[place >= 0] = records.power[place[place >= 0]]
            self.power_at_cell[meter_id] = power
            if meter_id in self.band:
                band = np.full(self.cells, -1, dtype=np.int8)
                band[place >= 0] = self.band[meter_id][place[place >= 0]]
                self.band_at_cell[meter_id] = band
        # Machines at the grid.
        self.machine_cells = {machine: self.codes_at(machine, self.grid_mid) for machine in self.in_use}
        processing = np.zeros(self.cells, dtype=np.int16)
        preparing = np.zeros(self.cells, dtype=np.int16)
        unknown = np.zeros(self.cells, dtype=np.int16)
        for codes in self.machine_cells.values():
            processing += (codes == PROCESSING) | (codes == CONFLICT)
            preparing += codes == PREPARING
            unknown += codes == UNKNOWN
        running_meters = np.zeros(self.cells, dtype=np.int16)
        for band in self.band_at_cell.values():
            running_meters += band == 2
        full = self.params.value("full_fraction")
        plant = np.full(self.cells, PLANT_STATES.index("ALL_IDLE"), dtype=np.int8)
        plant[unknown > 0] = PLANT_STATES.index("UNKNOWN")
        plant[(preparing > 0) | (running_meters > 0)] = PLANT_STATES.index("TRANSITION_ONLY")
        busy = processing > 0
        plant[busy] = PLANT_STATES.index("PARTIAL")
        plant[busy & (processing >= full * max(len(self.in_use), 1))] = PLANT_STATES.index("FULL")
        if self.coverage is None:
            plant[:] = PLANT_STATES.index("NO_RECORD")
        else:
            low = np.datetime64(pd.Timestamp(self.coverage[0]), "ns")
            high = np.datetime64(pd.Timestamp(self.coverage[1]), "ns")
            plant[(self.grid_mid < low) | (self.grid_mid >= high)] = PLANT_STATES.index("NO_RECORD")
        self.plant = plant
        self.processing, self.preparing = processing, preparing
        # The whole plant stopped, judged from power (P20), beside the registered state:
        # no production meter in RUN and their summed power far below its usual level.
        line_power = np.zeros(self.cells)
        known = np.zeros(self.cells, dtype=bool)
        for meter_id in self.production_meters:
            power = self.power_at_cell[meter_id]
            finite = np.isfinite(power)
            line_power[finite] += power[finite]
            known |= finite
        self.running_lines = running_meters
        self.line_power = line_power
        self.line_known = known
        top = nearest_rank(line_power[known], 0.95) if known.any() else None
        fraction = float(self.params.value("plant_stop_fraction"))
        if top:
            self.power_stop = known & (running_meters == 0) & (line_power < fraction * top)
        else:
            self.power_stop = np.zeros(self.cells, dtype=bool)
        self.line_power_p95 = top
        self.plant_assumptions = sorted(
            {item for info in self.band_info.values() for item in info["assumptions"]}
            | set(self.params.assumed_ids(("full_fraction",)))
        )

    def plant_of_records(self, meter_id: str) -> np.ndarray:
        cell = _cell_of(self.records[meter_id].mid, self.grid_start, self.step, self.cells)
        states = np.full(len(cell), -1, dtype=np.int8)
        states[cell >= 0] = self.plant[cell[cell >= 0]]
        return states

    # Days ---------------------------------------------------------------------
    def days(self) -> None:
        start, end = self.period
        hours_threshold = self.params.value("day_run_hours")
        alternatives = self.params.alternatives("day_run_hours")
        assumed = False  # P01 is an analysis preference: a choice, not a site assumption
        confirmed: dict[str, tuple[str, str]] = {}
        for entry in self.confirmations:
            if entry.kind == "calendar" and entry.result != "rejected" and entry.day_status:
                first, last = day_range(entry.subject)
                day = first
                while day <= last:
                    confirmed[day.isoformat()] = (entry.day_status, entry.confirmed_by)
                    day += timedelta(days=1)
        calendar = self.site.calendar() or []
        first_day = pd.Timestamp(start).normalize()
        cell_day = ((self.grid_mid - np.datetime64(first_day, "ns")) // np.timedelta64(1, "D")).astype(np.int64)
        registered_cells = (self.processing + self.preparing) > 0
        run_hours_by_meter: dict[str, dict[str, float]] = {}
        for meter_id in self.production_meters:
            records = self.records[meter_id]
            day_keys = pd.to_datetime(records.mid).normalize()
            frame = pd.DataFrame({"day": day_keys, "hours": records.hours * (self.band[meter_id] == 2)})
            run_hours_by_meter[meter_id] = {
                pd.Timestamp(key).date().isoformat(): float(value) for key, value in frame.groupby("day")["hours"].sum().items()
            }
        table = Table(
            "DAY",
            "每天是开工日还是休息日，以及据以判断的登记批次时长和产线表 RUN 时长",
            ["date"],
            {
                "day_status": Column(None, "有确认的日历用确认结果；否则按 P01", basis="DERIVED"),
                "status_basis": Column(None, "USER_CONFIRMED 或 ASSUMED", basis="DERIVED"),
                "registered_hours": Column("h", "当天有任一机台在登记批次（准备段或加工段）的时长", basis="REGISTERED"),
                "max_run_hours": Column("h", "当天各产线表 RUN 档时长的最大值", assumption_ids=[]),
                "stop_hours_by_power": Column("h", "当天按功率判为全厂停（P20）的时长；和登记状态并列，不改日状态", assumption_ids=["P20"]),
                "run_meter": Column(None, "RUN 时长最大的表"),
                "calendar_labels": Column(None, "人给的日历文件里覆盖当天的状态（原样）", basis="REGISTERED"),
                "status_at_alt_low": Column(None, f"P01 取 {alternatives[0]} h 时的判定" if alternatives else "无备选"),
                "status_at_alt_high": Column(None, f"P01 取 {alternatives[-1]} h 时的判定" if alternatives else "无备选"),
            },
            limits=[REFERENCE_NOTE],
        )
        self.day_status: dict[str, str] = {}
        changed_low = changed_high = 0
        for day in days_between(start, end):
            key = day.date().isoformat()
            index = (day - first_day.to_pydatetime()).days
            in_day = cell_day == index if index >= 0 else np.zeros(self.cells, dtype=bool)
            registered = float(registered_cells[in_day].sum()) * self.step_h
            stopped = float(self.power_stop[in_day].sum()) * self.step_h
            runs = {meter_id: hours.get(key, 0.0) for meter_id, hours in run_hours_by_meter.items()}
            best = max(runs, key=runs.get) if runs else None  # type: ignore[arg-type]
            run_hours = runs[best] if best else 0.0

            def status(threshold: float) -> str:
                return "OPERATING" if registered > 0 or run_hours >= threshold else "REST"

            decided = status(hours_threshold)
            low_status = status(alternatives[0]) if alternatives else decided
            high_status = status(alternatives[-1]) if alternatives else decided
            basis = "ASSUMED" if assumed else "REGISTERED"
            assumptions = ["P01", *self.plant_assumptions] if assumed else list(self.plant_assumptions)
            if key in confirmed:
                decided, _by = confirmed[key]
                basis = "USER_CONFIRMED"
                assumptions = []
            else:
                changed_low += low_status != decided
                changed_high += high_status != decided
            labels = sorted(
                {
                    window.state
                    for window in calendar
                    if window.start < day + timedelta(days=1) and window.end > day
                }
            )
            self.day_status[key] = decided
            table.add(
                [key],
                {
                    "day_status": decided,
                    "status_basis": basis,
                    "registered_hours": _r(registered, 2),
                    "max_run_hours": _r(run_hours, 2),
                    "stop_hours_by_power": _r(stopped, 2),
                    "run_meter": best if run_hours > 0 else None,
                    "calendar_labels": labels,
                    "status_at_alt_low": low_status,
                    "status_at_alt_high": high_status,
                },
                status="CONFIRMED" if basis == "USER_CONFIRMED" else ("ASSUMED" if basis == "ASSUMED" else None),
                assumption_ids=assumptions,
            )
        self.tables["DAY"] = table
        summary = Table(
            "DAYS",
            "开工日、休息日的天数，以及 P01 取备选值时会改判的天数",
            ["scope"],
            {
                "operating_days": Column("天", "DAY 里 OPERATING 的天数"),
                "rest_days": Column("天", "DAY 里 REST 的天数"),
                "confirmed_days": Column("天", "有人确认的天数", basis="USER_CONFIRMED"),
                "changed_at_alt_low": Column("天", "P01 取低备选值时改判的天数（只数没确认的天）"),
                "changed_at_alt_high": Column("天", "P01 取高备选值时改判的天数（只数没确认的天）"),
            },
            limits=[REFERENCE_NOTE],
        )
        statuses = list(self.day_status.values())
        summary.add(
            ["ALL"],
            {
                "operating_days": statuses.count("OPERATING"),
                "rest_days": statuses.count("REST"),
                "confirmed_days": sum(1 for key in self.day_status if key in confirmed),
                "changed_at_alt_low": changed_low,
                "changed_at_alt_high": changed_high,
            },
            status="ASSUMED" if assumed else None,
            assumption_ids=["P01", *self.plant_assumptions] if assumed else self.plant_assumptions,
        )
        self.tables["DAYS"] = summary

    # Plant stopped by power ---------------------------------------------------
    def power_stops(self) -> None:
        """STP: each stretch the plant stood still by power; PWS: each meter's power stopped against producing."""

        stp = Table(
            "STP",
            "按功率判的全厂停（P20）：每段的起止、时长和全部表的电量；和登记的全厂状态并列",
            ["start"],
            {
                "end": Column(None, "结束"),
                "hours": Column("h", "时长"),
                "weekdays": Column(None, "覆盖的星期（1 是周一）"),
                "kwh_all": Column("kWh", "这段时间所有表的电量（表之间可能有上下级，只作参考）", basis="MEASURED"),
                "kwh_lines": Column("kWh", "这段时间产线表的电量", basis="MEASURED"),
            },
            limits=[REFERENCE_NOTE, "按功率判，不看登记；登记的批次可能跨过这段（如跨休息日保温）"],
        )
        stop = self.power_stop
        energy = {meter_id: np.nan_to_num(power) * self.step_h for meter_id, power in self.power_at_cell.items()}
        index = 0
        while index < self.cells:
            if not stop[index]:
                index += 1
                continue
            end = index
            while end < self.cells and stop[end]:
                end += 1
            part = slice(index, end)
            start_time = pd.Timestamp(self.grid[index])
            weekdays = sorted({int(pd.Timestamp(moment).isoweekday()) for moment in self.grid_mid[part]})
            stp.add(
                [_iso(start_time.to_pydatetime())],
                {
                    "end": _iso((start_time + pd.Timedelta(hours=(end - index) * self.step_h)).to_pydatetime()),
                    "hours": _r((end - index) * self.step_h, 2),
                    "weekdays": weekdays,
                    "kwh_all": _r(sum(float(values[part].sum()) for values in energy.values()), 1),
                    "kwh_lines": _r(sum(float(energy[meter_id][part].sum()) for meter_id in self.production_meters), 1),
                },
                assumption_ids=["P20", *self.plant_assumptions],
            )
            index = end
        self.tables["STP"] = stp
        pws = Table(
            "PWS",
            "每块表在全厂停（按功率）和在产时的平均功率：停产时降不降",
            ["meter"],
            {
                "stop_hours": Column("h", "全厂停时本表有数的时长"),
                "stop_kwh": Column("kWh", "全厂停时本表的电量", basis="MEASURED"),
                "stop_kw": Column("kW", "全厂停时本表的平均功率"),
                "run_kw": Column("kW", "在产时（在 RUN 的产线数不少于非停时的中位数）本表的平均功率"),
                "ratio": Column(None, "stop_kw ÷ run_kw"),
                "stop_share": Column(None, "全厂停时电量占本表全期电量"),
            },
            limits=[REFERENCE_NOTE, "停时功率高只说明停产时没降，是不是必要服务要看用途"],
        )
        busy = self.line_known & ~stop
        usual = float(np.median(self.running_lines[busy])) if busy.any() else 0.0
        producing = busy & (self.running_lines >= max(usual, 1.0))
        for meter_id, power in self.power_at_cell.items():
            finite = np.isfinite(power)
            if not finite.any():
                continue
            stopped = finite & stop
            running = finite & producing
            total = float(power[finite].sum()) * self.step_h
            stop_kwh = float(power[stopped].sum()) * self.step_h
            stop_kw = float(power[stopped].mean()) if stopped.any() else None
            run_kw = float(power[running].mean()) if running.any() else None
            pws.add(
                [meter_id],
                {
                    "stop_hours": _r(float(stopped.sum()) * self.step_h, 2),
                    "stop_kwh": _r(stop_kwh, 1),
                    "stop_kw": _r(stop_kw, 2),
                    "run_kw": _r(run_kw, 2),
                    "ratio": _r(stop_kw / run_kw, 3) if stop_kw is not None and run_kw else None,
                    "stop_share": _r(stop_kwh / total, 4) if total > 0 else None,
                },
                assumption_ids=["P20", *self.plant_assumptions],
            )
        self.tables["PWS"] = pws
        self.producing = producing

    # Tables -------------------------------------------------------------------
    def quality(self) -> None:
        table = Table(
            "DQ",
            "每块表的数据质量：记录数、首末时刻、未纳入的时段和读数问题",
            ["meter"],
            {
                "records": Column("条", "读入后可用的记录数", basis="MEASURED"),
                "first": Column(None, "第一条记录", basis="MEASURED"),
                "last": Column(None, "最后一条记录", basis="MEASURED"),
                "period_coverage": Column(None, "可用记录覆盖数据期的比例", basis="MEASURED"),
                "semantics": Column(None, "manifest 写的读数类型", basis="REGISTERED"),
                "unit": Column(None, "manifest 写的单位", basis="REGISTERED"),
                "interval_minutes": Column("min", "manifest 写的间隔", basis="REGISTERED"),
                "excluded_spans": Column("段", "未纳入的时段数（含读数下降、首尾无从核对、负值、重叠）", basis="MEASURED"),
                "reading_decreases": Column("次", "累计读数下降次数", basis="MEASURED"),
                "flat_throughout": Column(None, "累计读数整期不变", basis="MEASURED"),
                "notes": Column(None, "程序读入时记下的问题", basis="MEASURED"),
            },
        )
        self.flat: list[str] = []
        start, end = self.period
        for meter_id, spec in self.specs.items():
            quality = self.site.quality(meter_id)
            series = self.series[meter_id]
            covered = series.measure(start, end).coverage if len(series) else 0.0
            total = float(np.nansum(series.values)) if series.has_energy and len(series) else None
            if quality.flat_throughout or not len(series) or (series.has_energy and total == 0):
                self.flat.append(meter_id)
            table.add(
                [meter_id],
                {
                    "records": quality.records,
                    "first": quality.first,
                    "last": quality.last,
                    "period_coverage": _r(covered, 4),
                    "semantics": spec.semantics,
                    "unit": spec.unit,
                    "interval_minutes": spec.interval_minutes,
                    "excluded_spans": len(quality.excluded_spans) + quality.excluded_spans_more,
                    "reading_decreases": len(quality.reading_decreases),
                    "flat_throughout": quality.flat_throughout,
                    "notes": quality.notes(),
                },
            )
        self.tables["DQ"] = table

    def by_plant_state(self) -> None:
        state_hours = {state: float((self.plant == number).sum()) * self.step_h for number, state in enumerate(PLANT_STATES)}
        mx = Table(
            "MX",
            "对象 × 全厂状态：每块表在每种全厂状态下的时长和电量（全厂各专业共用的首屏矩阵）",
            ["meter", "plant_state"],
            {
                "hours": Column("h", "本表在该状态下有记录的时长（每条记录按中点归到一个状态）"),
                "kwh": Column("kWh", "本表在该状态下的电量", basis="MEASURED"),
                "share_of_meter": Column(None, "占本表全期电量"),
                "share_of_state": Column(None, "占该状态下各表电量之和（按 P06 各表不嵌套）", assumption_ids=self.params.assumed_ids(("meters_independent",))),
            },
            limits=[REFERENCE_NOTE],
        )
        sl = Table(
            "SL",
            "状态条件负荷：每块表在每种全厂状态下的功率分布和最长连续时长",
            ["meter", "plant_state"],
            {
                "hours": Column("h", "有记录的时长"),
                "p10_kw": Column("kW", "区间功率 P10（nearest-rank）"),
                "p50_kw": Column("kW", "区间功率 P50（nearest-rank）"),
                "p90_kw": Column("kW", "区间功率 P90（nearest-rank）"),
                "longest_hours": Column("h", "连续处在该状态的最长时长（记录首尾相接才算连续）"),
            },
            limits=[REFERENCE_NOTE],
        )
        per_state_total: dict[str, float] = defaultdict(float)
        rows: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        self.record_states: dict[str, np.ndarray] = {}
        for meter_id in self.specs:
            records = self.records[meter_id]
            states = self.plant_of_records(meter_id)
            self.record_states[meter_id] = states
            total = float(np.nansum(records.energy)) if records.energy is not None else None
            for number, state in enumerate(PLANT_STATES):
                selected = states == number
                if not selected.any():
                    continue
                kwh = float(np.nansum(records.energy[selected])) if records.energy is not None else None
                if kwh is not None:
                    per_state_total[state] += kwh
                rows.append(
                    (
                        meter_id,
                        state,
                        {
                            "hours": _r(float(records.hours[selected].sum()), 2),
                            "kwh": _r(kwh, 1),
                            "share_of_meter": _r(kwh / total, 4) if kwh is not None and total else None,
                        },
                        {
                            "hours": _r(float(records.hours[selected].sum()), 2),
                            **_percentiles(records.power[selected]),
                            "longest_hours": _r(_longest(records.hours, records.follows, selected), 2),
                        },
                    )
                )
        for meter_id, state, mx_values, sl_values in rows:
            share = None
            if mx_values["kwh"] is not None and per_state_total.get(state):
                share = _r(mx_values["kwh"] / per_state_total[state], 4)
            mx.add([meter_id, state], {**mx_values, "share_of_state": share}, assumption_ids=self.plant_assumptions)
            sl.add([meter_id, state], sl_values, assumption_ids=self.plant_assumptions)
        self.tables["MX"] = mx
        self.tables["SL"] = sl
        pst = Table(
            "PST",
            "全厂状态：各状态的时长，以及该状态下各表电量之和（按 P06 各表不嵌套）",
            ["plant_state"],
            {
                "hours": Column("h", f"按 {int(self.step_h * 60)} 分钟网格数"),
                "share_of_period": Column(None, "占数据期"),
                "kwh_all_meters": Column("kWh", "该状态下各表电量之和", assumption_ids=self.params.assumed_ids(("meters_independent",))),
                "meaning": Column(None, "状态的含义", basis="CONVENTION"),
            },
            limits=[REFERENCE_NOTE, "全厂状态只按生产记录和产线表功率档定，不是停产确认"],
        )
        period_hours = self.cells * self.step_h
        for state in PLANT_STATES:
            if state_hours[state] <= 0:
                continue
            pst.add(
                [state],
                {
                    "hours": _r(state_hours[state], 2),
                    "share_of_period": _r(state_hours[state] / period_hours, 4) if period_hours else None,
                    "kwh_all_meters": _r(per_state_total.get(state), 1),
                    "meaning": PLANT_LABELS[state],
                },
                assumption_ids=self.plant_assumptions,
            )
        self.tables["PST"] = pst
        # Weekly P50 in ALL_IDLE: a change in a stopped plant's load shows here.
        weekly = Table(
            "SLW",
            "每周全厂 ALL_IDLE 时各表的功率 P50，用来看停产负荷前后有没有变化",
            ["meter", "week"],
            {"hours": Column("h", "当周 ALL_IDLE 有记录的时长"), "p50_kw": Column("kW", "区间功率 P50")},
            limits=[REFERENCE_NOTE],
        )
        idle = PLANT_STATES.index("ALL_IDLE")
        for meter_id in self.specs:
            records = self.records[meter_id]
            selected = self.record_states[meter_id] == idle
            if not selected.any():
                continue
            weeks = pd.to_datetime(records.mid[selected]).to_period("W-SUN").start_time
            frame = pd.DataFrame({"week": weeks, "power": records.power[selected], "hours": records.hours[selected]})
            for week, group in frame.groupby("week"):
                weekly.add(
                    [meter_id, pd.Timestamp(week).date().isoformat()],
                    {"hours": _r(float(group["hours"].sum()), 2), "p50_kw": _r(nearest_rank(group["power"].to_numpy(), 0.5))},
                    assumption_ids=self.plant_assumptions,
                )
        self.tables["SLW"] = weekly

    def crosstab(self) -> None:
        xs = Table(
            "XS",
            "登记状态 × 功率档：产线表在每种登记状态下处在 OFF、LOW、RUN 的时长和电量",
            ["meter", "registered_state", "band"],
            {"hours": Column("h", "时长"), "kwh": Column("kWh", "电量", basis="MEASURED")},
            limits=[REFERENCE_NOTE],
        )
        band_table = Table(
            "BAND",
            "产线表功率档的界线和各档时长；备选界线下的时长一并给出",
            ["meter"],
            {
                "reference_kw": Column("kW", "参考功率：本表机台批次时段区间功率按 P02 取分位数"),
                "off_below_kw": Column("kW", "低于它为 OFF（参考 × P03）"),
                "run_from_kw": Column("kW", "不低于它为 RUN（参考 × P04）"),
                "hours_off": Column("h", "OFF 时长"),
                "hours_low": Column("h", "LOW 时长"),
                "hours_run": Column("h", "RUN 时长"),
                "alt_hours_off": Column("h", "P03、P04 取备选值时的 OFF 时长"),
                "alt_hours_low": Column("h", "备选值时的 LOW 时长"),
                "alt_hours_run": Column("h", "备选值时的 RUN 时长"),
            },
            limits=[REFERENCE_NOTE, "功率档是对数据的描述，不是设备状态的确认"],
        )
        for meter_id in self.production_meters:
            records = self.records[meter_id]
            info = self.band_info[meter_id]
            band = self.band[meter_id]
            assumptions = info["assumptions"]
            reference = info["reference_kw"]
            alternative = info["alternative"]
            band_table.add(
                [meter_id],
                {
                    "reference_kw": _r(reference),
                    "off_below_kw": _r(reference * info["off"]) if reference else None,
                    "run_from_kw": _r(reference * info["run"]) if reference else None,
                    **{f"hours_{name.lower()}": _r(float(records.hours[band == code].sum()), 2) for code, name in BANDS.items()},
                    **{f"alt_hours_{name.lower()}": _r(float(records.hours[alternative == code].sum()), 2) for code, name in BANDS.items()},
                },
                status=None if reference else "PARTIAL",
                limits=[] if reference else ["本表机台没有批次时段的记录，算不出参考功率"],
                assumption_ids=assumptions,
            )
            if not reference:
                continue
            codes = self.own_codes[meter_id]
            for code, state in REGISTERED.items():
                for band_code, band_name in BANDS.items():
                    selected = (codes == code) & (band == band_code)
                    if not selected.any():
                        continue
                    xs.add(
                        [meter_id, state, band_name],
                        {
                            "hours": _r(float(records.hours[selected].sum()), 2),
                            "kwh": _r(float(np.nansum(records.energy[selected])), 1) if records.energy is not None else None,
                        },
                        assumption_ids=assumptions,
                    )
        self.tables["XS"] = xs
        self.tables["BAND"] = band_table

    def tasks(self) -> None:
        production = self.manifest.production
        if production is None:
            return
        basis = production.quantity_basis
        unit = production.quantity_unit or "数量单位"
        group_columns = self.params.value("peer_group_columns") or []
        start, end = self.period
        table = Table(
            "TASK",
            "每批：从开工（批次开始列）到完工的电量、成品量单耗、准备段和加工段、登记的停机时长和推断的批内 OFF 时长",
            ["machine", "row"],
            {
                "start": Column(None, "批次开始（manifest 的 start_column）", basis="REGISTERED"),
                "phase_start": Column(None, "批内时刻（manifest 的 phase_start_column）", basis="REGISTERED"),
                "end": Column(None, "批次结束", basis="REGISTERED"),
                "hours": Column("h", "批次时长", basis="REGISTERED"),
                "prep_hours": Column("h", "准备段时长"),
                "meter": Column(None, "机台挂的表", basis="REGISTERED"),
                "shared_meter": Column(None, "这块表还挂了别的机台，电量分不开"),
                "kwh": Column("kWh", "批次时段内完整记录的电量（边界区间不摊分）", basis="MEASURED"),
                "coverage": Column(None, "批次时段被完整记录覆盖的比例", basis="MEASURED"),
                "prep_kwh": Column("kWh", "准备段电量", basis="MEASURED"),
                "process_kwh": Column("kWh", "加工段电量", basis="MEASURED"),
                "avg_kw": Column("kW", "批次平均功率"),
                "quantity": Column(unit, f"数量（{ {'input': '投入量', 'output': '成品量', 'qualified': '合格品量'}.get(basis or '', '口径未声明') }）", basis="REGISTERED"),
                "kwh_per_unit": Column(f"kWh/{unit}", "电量 ÷ 数量；同机两批重叠、表挂多机台、数量缺失或不大于 0、不在数据期内的不算"),
                "no_intensity_reason": Column(None, "没算单耗的原因"),
                "product": Column(None, "产品列", basis="REGISTERED"),
                "group_key": Column(None, "可比组（按 P08 的列）"),
                "groups": Column(None, "其余分组列的值", basis="REGISTERED"),
                "stop_hours": Column("h", "登记的停机和换线时长之和（只有时长、没有起止时刻）", basis="REGISTERED"),
                "off_hours": Column("h", "批次加工段里功率档为 OFF 的时长"),
                "in_period": Column(None, "批次完全在数据期内"),
                "problem": Column(None, "批次时间的问题", basis="REGISTERED"),
            },
            limits=[REFERENCE_NOTE],
        )
        self.task_rows: list[dict[str, Any]] = []
        machine_batches = {machine: own for machine, own in self.by_machine.items()}
        for batch in sorted(self.batches, key=lambda item: (item.machine, item.row)):
            meter_id = self.machine_meter.get(batch.machine)
            values: dict[str, Any] = {
                "start": _iso(batch.start),
                "phase_start": _iso(batch.phase_start),
                "end": _iso(batch.end),
                "hours": _r((batch.end - batch.start).total_seconds() / 3600, 3) if batch.start and batch.end else None,
                "prep_hours": _r((batch.phase_start - batch.start).total_seconds() / 3600, 3) if batch.phase_start and batch.start else None,
                "meter": meter_id,
                "shared_meter": bool(meter_id and len(self.specs[meter_id].production_machines) > 1),
                "quantity": batch.quantity,
                "product": batch.product,
                "group_key": " / ".join(self._group_value(batch, column) for column in group_columns) if group_columns else None,
                "groups": dict(batch.groups),
                "stop_hours": _r(sum(batch.stop_hours.values()), 3) if batch.stop_hours else None,
                "in_period": bool(batch.start and batch.end and batch.start >= start and batch.end <= end),
                "problem": batch.problem,
            }
            limits: list[str] = []
            assumptions: list[str] = []
            reason = None
            if batch.problem:
                reason = "批次时间不全或不合理"
            elif meter_id is None:
                reason = "机台没有对应的表"
                limits.append("机台没有对应的表，没有电量")
            if meter_id is not None and batch.start and batch.end and batch.problem is None:
                series = self.series[meter_id]
                whole = series.measure(batch.start, batch.end)
                values.update(
                    {
                        "kwh": _r(whole.energy_kwh, 2),
                        "coverage": _r(whole.coverage, 4),
                        "avg_kw": _r(whole.avg_kw, 2),
                    }
                )
                if batch.phase_start and batch.phase_start > batch.start:
                    values["prep_kwh"] = _r(series.measure(batch.start, batch.phase_start).energy_kwh, 2)
                    values["process_kwh"] = _r(series.measure(batch.phase_start, batch.end).energy_kwh, 2)
                if meter_id in self.band and self.band_info[meter_id]["reference_kw"]:
                    records = self.records[meter_id]
                    low = np.datetime64(pd.Timestamp(batch.phase_start or batch.start), "ns")
                    high = np.datetime64(pd.Timestamp(batch.end), "ns")
                    inside = (records.mid >= low) & (records.mid < high)
                    values["off_hours"] = _r(float(records.hours[inside & (self.band[meter_id] == 0)].sum()), 2)
                    assumptions += self.band_info[meter_id]["assumptions"]
                overlapping = any(
                    other is not batch
                    and other.problem is None
                    and other.start < batch.end  # type: ignore[operator]
                    and other.end > batch.start  # type: ignore[operator]
                    for other in machine_batches.get(batch.machine, [])
                )
                if overlapping:
                    reason = "与同机台另一批次时间重叠，电量分不开"
                elif values["shared_meter"]:
                    reason = "表还挂了别的机台，电量分不开"
                elif not basis:
                    reason = "数量口径没有声明"
                elif batch.quantity is None:
                    reason = "没有数量"
                elif batch.quantity <= 0:
                    reason = "数量不大于 0"
                elif not values["in_period"]:
                    reason = "批次不完全在数据期内"
                elif whole.energy_kwh is None:
                    reason = "批次时段里没有记录"
                else:
                    values["kwh_per_unit"] = _r(whole.energy_kwh / batch.quantity, 5)
                if whole.coverage < 1 and values.get("kwh_per_unit") is not None:
                    limits.append("边界区间不摊分，电量只含批次内完整的记录")
            values["no_intensity_reason"] = reason
            row_id = table.add([batch.machine, f"R{batch.row}"], values, limits=limits, assumption_ids=assumptions)
            self.task_rows.append({"id": row_id, "batch": batch, **values})
        self.tables["TASK"] = table
        self.groups(group_columns, unit)
        self.transitions(group_columns)
        self.stops()

    def _group_value(self, batch: Batch, column: str) -> str:
        production = self.manifest.production
        if production is not None and column == production.product_column:
            return batch.product or "未写"
        return batch.groups.get(column) or "未写"

    def groups(self, group_columns: list[str], unit: str) -> None:
        grp = Table(
            "GRP",
            "单耗可比组（按 P08 分组，中试和常规一起）：组内单耗分布和成员",
            ["group"],
            {
                "batches": Column("批", "组内有单耗的批次数"),
                "machines": Column(None, "组内的机台"),
                "median": Column(f"kWh/{unit}", "单耗中位数"),
                "p25": Column(f"kWh/{unit}", "单耗 P25（nearest-rank）"),
                "p75": Column(f"kWh/{unit}", "单耗 P75（nearest-rank）"),
                "min": Column(f"kWh/{unit}", "最小"),
                "max": Column(f"kWh/{unit}", "最大"),
                "cv": Column(None, "变异系数（标准差 ÷ 均值）"),
                "of_sums": Column(f"kWh/{unit}", "组内电量之和 ÷ 数量之和"),
                "members": Column(None, "成员批次的句柄"),
                "comparable": Column(None, "可比条件：CANDIDATE 没人核过，CONFIRMED 或 REJECTED 由人定", basis="REGISTERED"),
            },
            limits=[REFERENCE_NOTE, "组内跨机台、跨批量的可比条件没人核过；不是基准"],
        )
        by_machine = Table(
            "GRPM",
            "可比组 × 机台：同一组在不同机台上的单耗",
            ["group", "machine"],
            {
                "batches": Column("批", "批次数"),
                "median": Column(f"kWh/{unit}", "单耗中位数"),
                "min": Column(f"kWh/{unit}", "最小"),
                "max": Column(f"kWh/{unit}", "最大"),
                "of_sums": Column(f"kWh/{unit}", "电量之和 ÷ 数量之和"),
            },
            limits=[REFERENCE_NOTE, "不同机台之间可比没人核过"],
        )
        members: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.task_rows:
            if row.get("kwh_per_unit") is not None and row["group_key"] is not None:
                members[row["group_key"]].append(row)
        for key, rows in sorted(members.items()):
            values = np.array([row["kwh_per_unit"] for row in rows], dtype=float)
            mean = float(values.mean())
            status, by = peer_group_status(self.site, key)
            grp.add(
                [key],
                {
                    "batches": len(rows),
                    "machines": sorted({row["batch"].machine for row in rows}),
                    "median": _r(float(np.median(values)), 5),
                    "p25": _r(nearest_rank(values, 0.25), 5),
                    "p75": _r(nearest_rank(values, 0.75), 5),
                    "min": _r(float(values.min()), 5),
                    "max": _r(float(values.max()), 5),
                    "cv": _r(float(values.std(ddof=1)) / mean, 4) if len(values) > 1 and mean else None,
                    "of_sums": _r(sum(row["kwh"] for row in rows) / sum(row["quantity"] for row in rows), 5),
                    "members": [row["id"] for row in rows],
                    "comparable": status if by is None else f"{status}（{by}）",
                },
                status=status if status != "CONFIRMED" else None,
                assumption_ids=self.params.assumed_ids(("peer_group_columns",)),
            )
            per_machine: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                per_machine[row["batch"].machine].append(row)
            for machine, own in sorted(per_machine.items()):
                numbers = np.array([row["kwh_per_unit"] for row in own], dtype=float)
                by_machine.add(
                    [key, machine],
                    {
                        "batches": len(own),
                        "median": _r(float(np.median(numbers)), 5),
                        "min": _r(float(numbers.min()), 5),
                        "max": _r(float(numbers.max()), 5),
                        "of_sums": _r(sum(row["kwh"] for row in own) / sum(row["quantity"] for row in own), 5),
                    },
                )
        self.tables["GRP"] = grp
        self.tables["GRPM"] = by_machine

    def transitions(self, group_columns: list[str]) -> None:
        tr = Table(
            "TR",
            "同机相邻两批之间：转换类型、间隔时长和电量、下一批准备段",
            ["machine", "seq"],
            {
                "from_row": Column(None, "前一批的行号", basis="REGISTERED"),
                "to_row": Column(None, "后一批的行号", basis="REGISTERED"),
                "kind": Column(None, "SAME_PRODUCT 同组续产；PRODUCT_CHANGE 换组；OVERLAP 两批重叠（按 P08 的列比）"),
                "from_group": Column(None, "前一批的组"),
                "to_group": Column(None, "后一批的组"),
                "gap_start": Column(None, "前一批完工", basis="REGISTERED"),
                "gap_end": Column(None, "后一批开工", basis="REGISTERED"),
                "gap_hours": Column("h", "间隔时长"),
                "gap_kwh": Column("kWh", "间隔里完整记录的电量", basis="MEASURED"),
                "gap_avg_kw": Column("kW", "间隔平均功率"),
                "gap_coverage": Column(None, "间隔被完整记录覆盖的比例", basis="MEASURED"),
                "crosses_rest_day": Column(None, "间隔里有 DAY 判为 REST 的日子"),
                "next_prep_hours": Column("h", "后一批的准备段时长"),
                "next_prep_kwh": Column("kWh", "后一批的准备段电量", basis="MEASURED"),
                "shared_meter": Column(None, "表还挂了别的机台"),
            },
            limits=[REFERENCE_NOTE],
        )
        summary = Table(
            "TRS",
            "每台机的转换汇总：按类型的次数、间隔时长和电量",
            ["machine", "kind"],
            {
                "count": Column("次", "次数"),
                "gap_hours_total": Column("h", "间隔时长合计"),
                "gap_kwh_total": Column("kWh", "间隔电量合计（有记录的部分）"),
                "gap_hours_median": Column("h", "间隔时长中位数"),
                "gap_hours_p90": Column("h", "间隔时长 P90（nearest-rank）"),
                "gap_kwh_median": Column("kWh", "间隔电量中位数"),
                "gap_avg_kw_median": Column("kW", "间隔平均功率中位数"),
                "next_prep_hours_median": Column("h", "后一批准备段时长中位数"),
            },
            limits=[REFERENCE_NOTE],
        )
        rest_days = {key for key, status in getattr(self, "day_status", {}).items() if status == "REST"}
        assumed_days = self.params.assumed_ids(("day_run_hours",))
        for machine, own in sorted(self.by_machine.items()):
            valid = sorted(
                (batch for batch in own if batch.problem is None and batch.start and batch.end),
                key=lambda item: (item.start, item.end),
            )
            meter_id = self.machine_meter.get(machine)
            series = self.series.get(meter_id) if meter_id else None
            collected: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for number, (before, after) in enumerate(zip(valid, valid[1:], strict=False), start=1):
                from_group = " / ".join(self._group_value(before, column) for column in group_columns) if group_columns else None
                to_group = " / ".join(self._group_value(after, column) for column in group_columns) if group_columns else None
                gap_hours = (after.start - before.end).total_seconds() / 3600  # type: ignore[operator]
                kind = "OVERLAP" if gap_hours < 0 else ("SAME_PRODUCT" if from_group == to_group else "PRODUCT_CHANGE")
                values: dict[str, Any] = {
                    "from_row": before.row,
                    "to_row": after.row,
                    "kind": kind,
                    "from_group": from_group,
                    "to_group": to_group,
                    "gap_start": _iso(before.end),
                    "gap_end": _iso(after.start),
                    "gap_hours": _r(gap_hours, 3),
                    "shared_meter": bool(meter_id and len(self.specs[meter_id].production_machines) > 1),
                }
                if after.phase_start and after.phase_start > after.start:  # type: ignore[operator]
                    values["next_prep_hours"] = _r((after.phase_start - after.start).total_seconds() / 3600, 3)  # type: ignore[operator]
                if gap_hours > 0:
                    day = pd.Timestamp(before.end).normalize()
                    crossed = False
                    while day < pd.Timestamp(after.start):
                        if day.date().isoformat() in rest_days:
                            crossed = True
                            break
                        day += pd.Timedelta(days=1)
                    values["crosses_rest_day"] = crossed
                if series is not None and gap_hours > 0:
                    measure = series.measure(before.end, after.start)  # type: ignore[arg-type]
                    values.update(
                        {
                            "gap_kwh": _r(measure.energy_kwh, 2),
                            "gap_avg_kw": _r(measure.avg_kw, 2),
                            "gap_coverage": _r(measure.coverage, 4),
                        }
                    )
                if series is not None and values.get("next_prep_hours"):
                    values["next_prep_kwh"] = _r(series.measure(after.start, after.phase_start).energy_kwh, 2)  # type: ignore[arg-type]
                tr.add(
                    [machine, number],
                    values,
                    limits=[] if series is not None else ["机台没有对应的表，没有电量"],
                    assumption_ids=assumed_days if gap_hours > 0 else [],
                )
                collected[kind].append(values)
            for kind, rows in sorted(collected.items()):
                hours = [row["gap_hours"] for row in rows if row["gap_hours"] is not None and row["gap_hours"] > 0]
                kwh = [row["gap_kwh"] for row in rows if row.get("gap_kwh") is not None]
                avg = [row["gap_avg_kw"] for row in rows if row.get("gap_avg_kw") is not None]
                prep = [row["next_prep_hours"] for row in rows if row.get("next_prep_hours") is not None]
                summary.add(
                    [machine, kind],
                    {
                        "count": len(rows),
                        "gap_hours_total": _r(sum(hours), 2),
                        "gap_kwh_total": _r(sum(kwh), 1) if kwh else None,
                        "gap_hours_median": _r(statistics.median(hours), 3) if hours else None,
                        "gap_hours_p90": _r(nearest_rank(hours, 0.9), 3) if hours else None,
                        "gap_kwh_median": _r(statistics.median(kwh), 2) if kwh else None,
                        "gap_avg_kw_median": _r(statistics.median(avg), 2) if avg else None,
                        "next_prep_hours_median": _r(statistics.median(prep), 3) if prep else None,
                    },
                )
        self.tables["TR"] = tr
        self.tables["TRS"] = summary

    def stops(self) -> None:
        production = self.manifest.production
        if production is None:
            return
        table = Table(
            "STOP",
            "每台机：数据期内批次登记的停机和换线时长，和批次加工段里功率档 OFF 的时长（推断）对照",
            ["machine"],
            {
                "registered_hours": Column("h", "登记时长之和（只有时长、没有起止）", basis="REGISTERED"),
                "registered_by_column": Column("h", "按列分的登记时长", basis="REGISTERED"),
                "off_hours": Column("h", "批次加工段里 OFF 的时长之和"),
                "difference_hours": Column("h", "OFF 时长减登记停机时长"),
                "batches": Column("批", "数据期内的批次数"),
            },
            limits=[REFERENCE_NOTE, "登记停机没有起止时刻，两者只能按总量对照"],
        )
        for machine in sorted(self.by_machine):
            rows = [row for row in self.task_rows if row["batch"].machine == machine and row["in_period"]]
            if not rows:
                continue
            by_column: dict[str, float] = defaultdict(float)
            for row in rows:
                for column, hours in row["batch"].stop_hours.items():
                    by_column[column] += hours
            registered = sum(by_column.values()) if production.stop_hours_columns else None
            off = [row["off_hours"] for row in rows if row.get("off_hours") is not None]
            meter_id = self.machine_meter.get(machine)
            if registered is None and not off:
                continue
            table.add(
                [machine],
                {
                    "registered_hours": _r(registered, 2),
                    "registered_by_column": {column: _r(hours, 2) for column, hours in by_column.items()},
                    "off_hours": _r(sum(off), 2) if off else None,
                    "difference_hours": _r(sum(off) - registered, 2) if off and registered is not None else None,
                    "batches": len(rows),
                },
                assumption_ids=self.band_info.get(meter_id, {}).get("assumptions", []) if meter_id else [],
            )
        self.tables["STOP"] = table

    def demand(self) -> None:
        busy_hours = float(self.params.value("demand_busy_min_hours"))
        df = Table(
            "DF",
            "没挂机台的表：按被服务机台中处在准备段或加工段的台数分档，各档的时长和功率分位",
            ["meter", "active"],
            {
                "hours": Column("h", "该档时长（网格）"),
                "p10_kw": Column("kW", "本表功率 P10"),
                "p50_kw": Column("kW", "本表功率 P50"),
                "p90_kw": Column("kW", "本表功率 P90"),
            },
            limits=[REFERENCE_NOTE, "在产台数只是需求代理，不是实测服务量"],
        )
        dfs = Table(
            "DFS",
            "没挂机台的表：本表功率对被服务产线功率之和的线性拟合，以及在产 0 台时和在产台数最多时的功率 P50 之比",
            ["meter"],
            {
                "served": Column(None, "被服务的机台（服务关系确认前按 P07 为全部机台）"),
                "served_basis": Column(None, "USER_CONFIRMED 或 ASSUMED"),
                "intercept_kw": Column("kW", "截距：被服务产线功率为 0 时的拟合功率（固定部分）"),
                "slope": Column(None, "斜率：被服务产线每 1 kW 对应本表的 kW"),
                "r2": Column(None, "拟合优度"),
                "cells": Column("个", "参与拟合的网格数"),
                "idle_p50_kw": Column("kW", "在产台数为 0 时的功率 P50"),
                "busy_p50_kw": Column("kW", f"在产台数最多、且至少 {busy_hours:g} 小时（P10）的那一档的功率 P50"),
                "idle_ratio": Column(None, "idle_p50_kw ÷ busy_p50_kw"),
            },
            limits=[REFERENCE_NOTE, "拟合只描述两组数的线性关系"],
        )
        confirmed: dict[str, list[str]] = {}
        for entry in self.confirmations:
            if entry.kind == "service" and entry.result != "rejected" and entry.serves:
                confirmed[entry.subject] = list(self.in_use) if entry.serves == ["ALL"] else list(entry.serves)
        assumed = self.params.assumed_ids(("default_service_scope",))
        for meter_id, spec in self.specs.items():
            if spec.production_machines or not len(self.series[meter_id]):
                continue
            served = confirmed.get(meter_id, list(self.in_use))
            basis = "USER_CONFIRMED" if meter_id in confirmed else "ASSUMED"
            active = np.zeros(self.cells, dtype=np.int16)
            for machine in served:
                codes = self.machine_cells.get(machine)
                if codes is not None:
                    active += (codes == PROCESSING) | (codes == PREPARING) | (codes == CONFLICT)
            served_meters = sorted({self.machine_meter[m] for m in served if m in self.machine_meter})
            served_power = np.zeros(self.cells)
            for other in served_meters:
                served_power = served_power + np.nan_to_num(self.power_at_cell[other], nan=np.nan)
            own = self.power_at_cell[meter_id]
            assumptions = (assumed if basis == "ASSUMED" else []) + self.plant_assumptions
            p50_by_active: dict[int, tuple[float, float | None]] = {}
            for count in sorted(set(active.tolist())):
                selected = (active == count) & np.isfinite(own)
                if not selected.any():
                    continue
                hours = float(selected.sum()) * self.step_h
                values = _percentiles(own[selected])
                p50_by_active[count] = (hours, values["p50_kw"])
                df.add([meter_id, count], {"hours": _r(hours, 2), **values}, assumption_ids=assumptions)
            intercept, slope, r2 = _fit(served_power, own)
            idle = p50_by_active.get(0, (0.0, None))[1]
            busy_candidates = [count for count, (hours, _p) in p50_by_active.items() if count > 0 and hours >= busy_hours]
            busy = p50_by_active[max(busy_candidates)][1] if busy_candidates else None
            dfs.add(
                [meter_id],
                {
                    "served": served if basis == "USER_CONFIRMED" else "ALL",
                    "served_basis": basis,
                    "intercept_kw": _r(intercept, 2),
                    "slope": _r(slope, 4),
                    "r2": _r(r2, 3),
                    "cells": int((np.isfinite(served_power) & np.isfinite(own)).sum()),
                    "idle_p50_kw": idle,
                    "busy_p50_kw": busy,
                    "idle_ratio": _r(idle / busy, 3) if idle is not None and busy else None,
                },
                status="ASSUMED" if basis == "ASSUMED" else None,
                assumption_ids=assumptions,
            )
        self.tables["DF"] = df
        self.tables["DFS"] = dfs

    def tou(self) -> None:
        schedule = self.site.tariff()
        if schedule is None:
            return
        table = Table(
            "TOU",
            "每块表每月在各电价时段的电量和占比（只看时段，不算电费）",
            ["meter", "month", "band"],
            {"kwh": Column("kWh", "电量", basis="MEASURED"), "share": Column(None, "占本表当月电量")},
            limits=["电价时段按 manifest 的电价；跨两个价格或落在电价没覆盖的时刻的记录记为“未覆盖”"],
        )
        cache: dict[tuple[Any, ...], str] = {}
        for meter_id in self.specs:
            records = self.records[meter_id]
            if records.energy is None or not len(records.start):
                continue
            totals: dict[tuple[str, str], float] = defaultdict(float)
            month_totals: dict[str, float] = defaultdict(float)
            starts = pd.to_datetime(records.start)
            ends = pd.to_datetime(records.end)
            for index in range(len(starts)):
                begin, finish = starts[index], ends[index]
                key = (begin.month, begin.year, begin.hour, begin.minute, (finish - begin).total_seconds())
                name = cache.get(key)
                if name is None:
                    price, _reason = schedule.price_status(begin.to_pydatetime(), finish.to_pydatetime())
                    name = price[0] if price else "未覆盖"
                    cache[key] = name
                month = f"{begin.year:04d}-{begin.month:02d}"
                energy = records.energy[index]
                if not np.isfinite(energy):
                    continue
                totals[(month, name)] += float(energy)
                month_totals[month] += float(energy)
            for (month, name), kwh in sorted(totals.items()):
                table.add(
                    [meter_id, month, name],
                    {"kwh": _r(kwh, 1), "share": _r(kwh / month_totals[month], 4) if month_totals[month] else None},
                )
        self.tables["TOU"] = table

    def peaks(self) -> None:
        energy_meters = [meter_id for meter_id, spec in self.specs.items() if spec.has_energy and len(self.series[meter_id])]
        if not energy_meters:
            return
        children = {
            child
            for entry in self.confirmations
            if entry.kind == "meter_topology" and entry.result != "rejected"
            for child in entry.children
        }
        top_level = [meter_id for meter_id in energy_meters if meter_id not in children]
        assumed = self.params.assumed_ids(("meters_independent",)) if not children else []
        stack = np.vstack([self.power_at_cell[meter_id] for meter_id in top_level])
        complete = np.isfinite(stack).all(axis=0)
        plant = np.where(complete, np.nansum(stack, axis=0), np.nan)
        pk = Table(
            "PK",
            "全厂每月最大网格功率（各表相加）、出现时刻和当时各表的功率",
            ["scope", "month"],
            {
                "max_kw": Column("kW", f"各表功率之和的月最大值（{int(self.step_h * 60)} 分钟网格；只用各表都有记录的网格）"),
                "at": Column(None, "出现时刻（网格开始）"),
                "cells_complete": Column("个", "各表都有记录的网格数"),
                "top_meters": Column(None, "当时功率最大的 5 块表：[表, kW, 占比]"),
            },
            limits=["各表相加按 P06（不嵌套）；户号归属没有确认时只能是全厂代理", "需量电费按户号计，这里不算钱"],
        )
        months = pd.to_datetime(self.grid).to_period("M").astype(str)
        for month in sorted(set(months)):
            selected = (months == month) & complete
            if not selected.any():
                continue
            index = np.flatnonzero(selected)[int(np.nanargmax(plant[selected]))]
            value = float(plant[index])
            contributions = sorted(
                ((meter_id, float(self.power_at_cell[meter_id][index])) for meter_id in top_level),
                key=lambda item: item[1],
                reverse=True,
            )[:5]
            pk.add(
                ["PLANT", month],
                {
                    "max_kw": _r(value, 1),
                    "at": _iso(self.grid[index]),
                    "cells_complete": int(selected.sum()),
                    "top_meters": [[meter_id, _r(kw, 1), _r(kw / value, 4) if value else None] for meter_id, kw in contributions],
                },
                status="ASSUMED" if assumed else None,
                assumption_ids=assumed,
            )
        self.tables["PK"] = pk
        top_n = int(self.params.value("peak_top_intervals"))
        order = np.argsort(np.where(np.isfinite(plant), plant, -np.inf))[::-1]
        top = [index for index in order[:top_n] if np.isfinite(plant[index])]
        pkc = Table(
            "PKC",
            "削峰对象：各表在全厂功率最高的 N 个网格（P09）里的平均功率和平均占比",
            ["meter"],
            {
                "mean_kw_top": Column("kW", "这些网格里本表的平均功率"),
                "mean_share_top": Column(None, "这些网格里本表占全厂的平均比例"),
                "cells": Column("个", "网格数"),
            },
            limits=["各表相加按 P06；这里不算钱"],
        )
        for meter_id in top_level:
            if not top:
                break
            values = self.power_at_cell[meter_id][top]
            shares = values / plant[top]
            pkc.add(
                [meter_id],
                {
                    "mean_kw_top": _r(float(np.mean(values)), 2),
                    "mean_share_top": _r(float(np.mean(shares)), 4),
                    "cells": len(top),
                },
                assumption_ids=assumed + self.params.assumed_ids(("peak_top_intervals",)),
            )
        self.tables["PKC"] = pkc

    # Plant timeline for charts and evidence cards --------------------------------
    def plant_runs(self) -> list[list[str]]:
        runs: list[list[str]] = []
        if not self.cells:
            return runs
        current = int(self.plant[0])
        begin = 0
        for index in range(1, self.cells + 1):
            if index == self.cells or int(self.plant[index]) != current:
                runs.append(
                    [
                        _iso(self.grid[begin]),  # type: ignore[list-item]
                        _iso(self.grid[index - 1] + self.step),  # type: ignore[list-item]
                        PLANT_STATES[current],
                    ]
                )
                if index < self.cells:
                    current = int(self.plant[index])
                    begin = index
        return runs


def build_foundation(workspace: Path, run_id: str) -> dict[str, Any]:
    """Build and write the company model and the foundation of one run."""

    site = SiteData(workspace, run_id)
    builder = _Builder(site)
    builder.machines()
    builder.quality()
    builder.states()
    builder.power_stops()
    builder.days()
    builder.by_plant_state()
    builder.crosstab()
    builder.tasks()
    builder.demand()
    builder.tou()
    builder.peaks()
    sources = source_register(site.inputs, site.manifest)
    listed = site.path("sources.json")
    files = read_json(listed)["files"] if listed.is_file() else [{"file": entry["file"], "sha256": entry["sha256"]} for entry in sources]
    write_json(listed, {"files": files, "register": sources})
    registry = build_registry(site, builder.params, sources, builder.flat)
    directory = site.path(FOUNDATION_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    for name, table in builder.tables.items():
        write_json(directory / f"{name}.json", table.as_dict())
    write_json(
        site.path(f"{FOUNDATION_DIR}/state.json"),
        {
            "grid_minutes": int(builder.step_h * 60),
            "plant_states": {state: PLANT_LABELS[state] for state in PLANT_STATES},
            "registered_states": REGISTERED_LABELS,
            "plant_runs": builder.plant_runs(),
            "machines_in_use": builder.in_use,
            "unplaced_batches": builder.unplaced,
            "production_coverage": None
            if builder.coverage is None
            else {"start": _iso(builder.coverage[0]), "end": _iso(builder.coverage[1])},
        },
    )
    counts = {
        "Meter": len(registry["objects"]["Meter"]),
        "Asset": len(registry["objects"]["Asset"]),
        "ProductionTask": len(builder.tables["TASK"].rows) if "TASK" in builder.tables else 0,
        "TransitionEvent": len(builder.tables["TR"].rows) if "TR" in builder.tables else 0,
        "StopRecord": sum(1 for batch in builder.batches if batch.stop_hours),
        "CalendarDay": len(builder.tables["DAY"].rows),
        "PeerGroup": len(builder.tables["GRP"].rows) if "GRP" in builder.tables else 0,
    }
    model = {
        "schema_version": MODEL_SCHEMA,
        "task": task_record(site),
        "sources": sources,
        **registry,
        "event_tables": {
            "ProductionTask": "TASK",
            "TransitionEvent": "TR",
            "StopRecord": "TASK.stop_hours / STOP",
            "CalendarDay": "DAY",
            "PeerGroup": "GRP",
        },
        "counts": counts,
        "parameters": builder.params.as_list(),
    }
    write_json(site.path(MODEL_FILE), model)
    foundation = Foundation({name: table.as_dict() for name, table in builder.tables.items()})
    write_json(site.path(f"{FOUNDATION_DIR}/digest.json"), {"text": plant_digest(site, foundation, model)})
    export_grid(builder, directory)
    return {
        "run_id": run_id,
        "tables": {name: len(table.rows) for name, table in builder.tables.items()},
        "objects": counts,
        "gaps": len(model["gaps"]),
        "parameters_assumed": sorted({item["id"] for item in model["parameters"] if item["basis"] == "ASSUMED"}),
        "confirmations": {key: len(value) for key, value in model["confirmations"].items()},
    }


def load_foundation(site: SiteData) -> Foundation:
    return Foundation.load(site.path(FOUNDATION_DIR))


def scope_objects(site: SiteData, meter_ids: list[str]) -> tuple[set[str], set[str]]:
    """The meters and the machines on them that a scope may read."""

    machines = {machine for meter_id in meter_ids for machine in site.spec(meter_id).production_machines}
    return set(meter_ids), machines


def row_in_scope(table: dict[str, Any], row: dict[str, Any], meters: set[str], machines: set[str]) -> bool:
    """A row keyed by a meter or a machine belongs to that object's scope; a plant-wide row to every scope."""

    keys = table["key_columns"]
    if "meter" in keys and row.get("meter") not in meters:
        return False
    if "machine" in keys and row.get("machine") not in machines:
        return False
    return True


def load_model(site: SiteData) -> dict[str, Any] | None:
    path = site.path(MODEL_FILE)
    return read_json(path) if path.is_file() else None


# Digest ------------------------------------------------------------------------


def _fmt(value: Any, unit: str | None = None) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        text = f"{value:.4g}" if abs(value) < 1000 else f"{value:,.0f}"
    else:
        text = str(value)
    return f"{text} {unit}" if unit and unit not in {"个", "批", "次", "段", "条", "天"} else (f"{text}{unit}" if unit else text)


def plant_digest(site: SiteData, foundation: Foundation, model: dict[str, Any]) -> str:
    """The plant-wide part of the first screen, shared by every scope."""

    lines = [
        "【底座首屏】以下数字都是程序对数据的描述（统计参考，资格未核），不说明是否损耗、是否必要；"
        "方括号里是句柄，可用 foundation-values 读回，引用时写进 cited_values。"
    ]
    states = foundation.rows("PST")
    if states:
        lines.append(
            "全厂状态（按生产记录和产线表功率档）："
            + "；".join(
                f"{row['plant_state']} {_fmt(row['hours'], 'h')}，各表合计 {_fmt(row['kwh_all_meters'], 'kWh')} [{row['id']}]"
                for row in states
            )
        )
    days = foundation.rows("DAYS")
    if days:
        row = days[0]
        lines.append(
            f"日状态：开工日 {row['operating_days']} 天，休息日 {row['rest_days']} 天，人确认 {row['confirmed_days']} 天；"
            f"P01 取备选值时改判 {row['changed_at_alt_low']}/{row['changed_at_alt_high']} 天 [{row['id']}]"
        )
    for row in foundation.rows("PK") if "PK" in foundation.tables else []:
        top = "、".join(f"{meter} {kw} kW" for meter, kw, _share in row["top_meters"][:3])
        lines.append(f"{row['month']} 全厂最大 {_fmt(row['max_kw'], 'kW')}（{row['at']}；{top}）[{row['id']}]")
    assumed = [item for item in model["parameters"] if item["basis"] == "ASSUMED" and not item["meters"]]
    if assumed:
        lines.append(
            "现场假设（没人确认过，结果依赖它们，在现场核实清单里）："
            + "；".join(f"{item['id']} {item['topic']} = {item['value']}" for item in assumed)
        )
    preferences = [item for item in model["parameters"] if item.get("nature") == "preference" and not item["meters"]]
    if preferences:
        lines.append(
            "分析偏好（怎样分析的设定，不是工厂的事实）："
            + "；".join(f"{item['id']} {item['topic']} = {_preference_text(item['value'])}" for item in preferences)
        )
    return "\n".join(lines)


def _preference_text(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        return "、".join(str(item) for item in value) or "（默认）"
    return str(value)


def scope_digest(site: SiteData, meter_ids: list[str], plant_wide: bool = False) -> str:
    """The first screen for a finder: plant-wide lines, then each meter's rows with their handles."""

    foundation = load_foundation(site)
    model = load_model(site) or {"parameters": []}
    lines = [read_json(site.path(f"{FOUNDATION_DIR}/digest.json"))["text"]]
    for meter_id in meter_ids:
        spec = site.spec(meter_id)
        machines = list(spec.production_machines)
        head = f"\n表 {meter_id}（{spec.name}；{'挂机台 ' + '、'.join(machines) if machines else '没挂机台'}）"
        lines.append(head)
        if foundation.has_row(f"DQ.{meter_id}"):
            row = foundation.row(f"DQ.{meter_id}")
            lines.append(
                f"- 数据：{row['records']} 条，覆盖 {_fmt(row['period_coverage'])}，未纳入 {row['excluded_spans']} 段"
                + ("，整期不变" if row["flat_throughout"] else "")
                + f" [DQ.{meter_id}]"
            )
        mx = {row["plant_state"]: row for row in foundation.rows("MX", meter=meter_id)}
        sl = {row["plant_state"]: row for row in foundation.rows("SL", meter=meter_id)}
        if mx:
            parts = []
            for state in PLANT_STATES:
                if state not in mx:
                    continue
                m, s = mx[state], sl.get(state, {})
                parts.append(
                    f"{state} {_fmt(m['hours'], 'h')} {_fmt(m['kwh'], 'kWh')}（占本表 {_fmt(m['share_of_meter'])}）"
                    f" P10/P50/P90 {_fmt(s.get('p10_kw'))}/{_fmt(s.get('p50_kw'))}/{_fmt(s.get('p90_kw'))} kW"
                    f" 最长连续 {_fmt(s.get('longest_hours'), 'h')} [{m['id']} / {s.get('id')}]"
                )
            lines.append("- 按全厂状态：" + "；".join(parts))
        if foundation.has_row(f"BAND.{meter_id}"):
            band = foundation.row(f"BAND.{meter_id}")
            lines.append(
                f"- 功率档：参考 {_fmt(band['reference_kw'], 'kW')}，OFF < {_fmt(band['off_below_kw'], 'kW')}，RUN ≥ {_fmt(band['run_from_kw'], 'kW')}；"
                f"OFF/LOW/RUN {_fmt(band['hours_off'], 'h')}/{_fmt(band['hours_low'], 'h')}/{_fmt(band['hours_run'], 'h')}"
                f"（备选界线 {_fmt(band['alt_hours_off'], 'h')}/{_fmt(band['alt_hours_low'], 'h')}/{_fmt(band['alt_hours_run'], 'h')}）[BAND.{meter_id}]"
            )
            cells = foundation.rows("XS", meter=meter_id)
            if cells:
                lines.append(
                    "- 登记状态×功率档："
                    + "；".join(f"{row['registered_state']}×{row['band']} {_fmt(row['hours'], 'h')} {_fmt(row['kwh'], 'kWh')}" for row in cells)
                    + f" [XS.{meter_id}.<状态>.<档>]"
                )
        for machine in machines:
            transitions = foundation.rows("TRS", machine=machine) if "TRS" in foundation.tables else []
            if transitions:
                lines.append(
                    f"- {machine} 转换："
                    + "；".join(
                        f"{row['kind']} {row['count']} 次，间隔合计 {_fmt(row['gap_hours_total'], 'h')} {_fmt(row['gap_kwh_total'], 'kWh')}，"
                        f"中位 {_fmt(row['gap_hours_median'], 'h')}/{_fmt(row['gap_kwh_median'], 'kWh')} [{row['id']}]"
                        for row in transitions
                    )
                )
            if "STOP" in foundation.tables and foundation.has_row(f"STOP.{machine}"):
                row = foundation.row(f"STOP.{machine}")
                lines.append(
                    f"- {machine} 停机：登记 {_fmt(row['registered_hours'], 'h')}，批内 OFF {_fmt(row['off_hours'], 'h')}，差 {_fmt(row['difference_hours'], 'h')} [STOP.{machine}]"
                )
            groups = [row for row in foundation.rows("GRPM", machine=machine)] if "GRPM" in foundation.tables else []
            if groups:
                groups.sort(key=lambda row: row["batches"], reverse=True)
                shown = groups[:6]
                lines.append(
                    f"- {machine} 单耗（按组，前 {len(shown)} 组/共 {len(groups)} 组）："
                    + "；".join(
                        f"{row['group']} {row['batches']} 批 中位 {_fmt(row['median'])} [{row['id']}；全组 {_group_handle(foundation, row['group'])}]"
                        for row in shown
                    )
                )
        dfs = foundation.row(f"DFS.{meter_id}") if "DFS" in foundation.tables and foundation.has_row(f"DFS.{meter_id}") else None
        if dfs:
            lines.append(
                f"- 对被服务产线功率的拟合（服务对象 {dfs['served']}，{dfs['served_basis']}）：截距 {_fmt(dfs['intercept_kw'], 'kW')}，斜率 {_fmt(dfs['slope'])}，R² {_fmt(dfs['r2'])}；"
                f"在产 0 台时 P50 {_fmt(dfs['idle_p50_kw'], 'kW')}，最多时 {_fmt(dfs['busy_p50_kw'], 'kW')}，比 {_fmt(dfs['idle_ratio'])} [DFS.{meter_id}；分档 DF.{meter_id}.<台数>]"
            )
        tou = foundation.rows("TOU", meter=meter_id) if "TOU" in foundation.tables else []
        if tou:
            by_band: dict[str, float] = defaultdict(float)
            total = sum(row["kwh"] or 0 for row in tou)
            for row in tou:
                by_band[row["band"]] += row["kwh"] or 0
            lines.append(
                "- 电价时段电量（全期）："
                + "、".join(f"{band} {value / total:.0%}" for band, value in by_band.items() if total)
                + f" [TOU.{meter_id}.<月>.<时段>]"
            )
        if "PKC" in foundation.tables and foundation.has_row(f"PKC.{meter_id}"):
            row = foundation.row(f"PKC.{meter_id}")
            lines.append(f"- 全厂最高 {row['cells']} 个网格里平均 {_fmt(row['mean_kw_top'], 'kW')}，占 {_fmt(row['mean_share_top'])} [PKC.{meter_id}]")
    gaps = [
        gap
        for gap in model.get("gaps", [])
        if plant_wide or not gap["objects"] or set(gap["objects"]) & {*meter_ids, *(m for mid in meter_ids for m in site.spec(mid).production_machines)}
    ]
    if gaps:
        lines.append("\n资料缺口（程序列出，推不出的待现场确认，不要猜）：")
        lines += [f"- {gap['id']} {gap['what']}：影响 {', '.join(gap['affects'])}" for gap in gaps]
    return "\n".join(lines)


# Items --------------------------------------------------------------------------

DIGEST_TABLES = ("DQ", "MX", "SL", "BAND", "XS", "TRS", "STOP", "GRPM", "DFS", "TOU", "PKC")
PLANT_TABLES = ("PST", "DAYS", "PK", "GRP")


def cited_view(foundation: Foundation, item: dict[str, Any]) -> dict[str, Any]:
    """An item's cited values read back from the foundation, and the parameters they rest on."""

    cited = []
    for entry in item.get("cited_values", []):
        try:
            value = foundation.value(entry["handle"])
        except HandleError as exc:
            cited.append({"handle": entry["handle"], "written": entry.get("value"), "problem": str(exc)})
            continue
        cited.append(
            {
                "handle": entry["handle"],
                "written": entry.get("value"),
                **{key: value[key] for key in ("value", "unit", "basis", "status", "method", "limits", "assumption_ids")},
                "matches": numbers_match(entry.get("value"), value["value"]),
            }
        )
    statuses = {entry.get("status") for entry in cited if entry.get("status")}
    tables = {entry["handle"].split(".", 1)[0] for entry in cited}
    return {
        "cited": cited,
        "rests_on_parameters": sorted({ident for entry in cited for ident in entry.get("assumption_ids", [])}),
        "rests_on_preferences": sorted(spec.id for spec in PREFERENCES if tables & set(spec.affected)),
        "weakest_status": next((status for status in ("CONFLICT", "REJECTED", "ASSUMED", "CANDIDATE", "PARTIAL") if status in statuses), "CONFIRMED" if statuses else None),
        "mismatched": [entry["handle"] for entry in cited if entry.get("problem") or not entry.get("matches")],
    }


def scope_rows(foundation: Foundation, site: SiteData, meter_ids: list[str]) -> list[dict[str, Any]]:
    """The foundation rows a finder of these meters was shown: its meters' and machines' rows and the plant's."""

    meters, machines = scope_objects(site, [meter_id for meter_id in meter_ids if meter_id in set(site.meter_ids())])
    rows = []
    for name in (*DIGEST_TABLES, *PLANT_TABLES):
        table = foundation.tables.get(name)
        if table is None:
            continue
        keys = table["key_columns"]
        for row in table["rows"]:
            if ("meter" in keys or "machine" in keys) and not row_in_scope(table, row, meters, machines):
                continue
            rows.append(row)
    return rows


def _group_handle(foundation: Foundation, group: str) -> str:
    for row in foundation.rows("GRP", group=group):
        return row["id"]
    return "—"


__all__ = [
    "FOUNDATION_DIR",
    "MODEL_FILE",
    "PLANT_STATES",
    "REFERENCE_NOTE",
    "build_foundation",
    "cited_view",
    "load_foundation",
    "load_model",
    "nearest_rank",
    "row_in_scope",
    "scope_rows",
    "scope_digest",
    "scope_objects",
]
