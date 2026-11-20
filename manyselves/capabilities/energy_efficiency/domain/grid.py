"""The electricity ledger (电量账): every meter's energy on one time grid, with the state axes.

The foundation writes it once (``foundation/grid.npz`` and ``grid.json``);
the lenses, the specs and the probes read it.  Every cell is one step of the
finest meter interval.  Beside each meter's energy it holds the axes a time
selection may name, all from the data or the registered records:

- the whole plant stopped by power (P20) or not, and the registered plant state;
- each production meter's power band (OFF / LOW / RUN) and batch phase
  (preparing, processing, between batches, outside the records) and batch;
- dates, weekdays, clock, the day status and the tariff period.

A time selection (``When``) names only these axes, so it reads the same at any
site: no word lists, no table of which object has which loss.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .store import read_json, write_json

GRID_FILE = "grid.npz"
GRID_META = "grid.json"
# Registered codes of a production meter (foundation): NO_RECORD, BETWEEN, UNKNOWN, PREPARING, PROCESSING, CONFLICT, NOT_COMMISSIONED.
_NO_RECORD, _BETWEEN, _UNKNOWN, _PREPARING, _PROCESSING, _CONFLICT = range(6)
PHASES = {
    "PREPARING": (_PREPARING,),
    "PROCESSING": (_PROCESSING, _CONFLICT),
    "BETWEEN": (_BETWEEN,),
    "OUTSIDE": (_NO_RECORD,),
}
PHASE_LABELS = {"PREPARING": "批次准备段", "PROCESSING": "批次加工段", "BETWEEN": "同机两批之间", "OUTSIDE": "生产记录之外"}
BAND_CODES = {"OFF": 0, "LOW": 1, "RUN": 2}
PLANT_BY_POWER = {"STOP": "全厂停（按功率判）", "RUN": "全厂没停（按功率判）"}
_CLOCK = re.compile(r"^([01]\d|2[0-4]):([0-5]\d)-([01]\d|2[0-4]):([0-5]\d)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SelectorError(ValueError):
    pass


class When(BaseModel):
    """A time selection on the ledger's axes; every condition given must hold."""

    model_config = ConfigDict(extra="forbid")

    plant: Literal["STOP", "RUN"] | None = Field(default=None, description="全厂按功率判：STOP 停，RUN 没停")
    registered: list[str] | None = Field(default=None, description="登记的全厂状态（foundation 的 PLANT_STATES）")
    line: str | None = Field(default=None, description="band、phase 说的是哪条产线表；不写时指对象本身（对象要是产线表）")
    band: list[Literal["OFF", "LOW", "RUN"]] | None = Field(default=None, description="产线表的功率档")
    phase: list[Literal["PREPARING", "PROCESSING", "BETWEEN", "OUTSIDE"]] | None = Field(default=None, description="产线表的批次段")
    dates: list[str] | None = Field(default=None, description="日期或日期段：2026-07-05 或 2026-07-01..2026-07-31")
    weekdays: list[int] | None = Field(default=None, description="星期，1 是周一，7 是周日")
    clock: str | None = Field(default=None, description="钟点段 HH:MM-HH:MM，可跨零点，如 23:00-06:00")
    tariff: list[str] | None = Field(default=None, description="电价时段名（电价文件里的写法）")
    day_status: Literal["OPERATING", "REST"] | None = Field(default=None, description="日状态（底座 DAY 表）")

    @field_validator("weekdays")
    @classmethod
    def weekday_range(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and (not value or any(day < 1 or day > 7 for day in value)):
            raise ValueError("weekdays are 1 (Monday) to 7 (Sunday)")
        return value

    @field_validator("clock")
    @classmethod
    def clock_form(cls, value: str | None) -> str | None:
        if value is not None and not _CLOCK.match(value):
            raise ValueError(f"clock is HH:MM-HH:MM, not {value!r}")
        return value

    @field_validator("dates")
    @classmethod
    def date_form(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("dates is a non-empty list")
        for entry in value:
            parts = entry.split("..")
            if len(parts) > 2 or not all(_DATE.match(part) for part in parts):
                raise ValueError(f"a date is YYYY-MM-DD or YYYY-MM-DD..YYYY-MM-DD, not {entry!r}")
            try:
                days = [date.fromisoformat(part) for part in parts]
            except ValueError as exc:
                raise ValueError(f"{entry!r} is not a date") from exc
            if len(days) == 2 and days[1] < days[0]:
                raise ValueError(f"{entry!r} ends before it starts")
        return value

    def text(self) -> str:
        parts = []
        if self.plant:
            parts.append(PLANT_BY_POWER[self.plant])
        if self.registered:
            parts.append("登记全厂状态 " + "/".join(self.registered))
        if self.band or self.phase:
            what = f"产线 {self.line}" if self.line else "本表"
            if self.band:
                parts.append(f"{what}功率档 " + "/".join(self.band))
            if self.phase:
                parts.append(f"{what}" + "/".join(PHASE_LABELS[item] for item in self.phase))
        if self.dates:
            parts.append("日期 " + "、".join(self.dates))
        if self.weekdays:
            parts.append("星期" + "、".join("一二三四五六日"[day - 1] for day in self.weekdays))
        if self.clock:
            parts.append(f"钟点 {self.clock}")
        if self.tariff:
            parts.append("电价时段 " + "/".join(self.tariff))
        if self.day_status:
            parts.append("开工日" if self.day_status == "OPERATING" else "休息日")
        return "，".join(parts) or "全期"


def _clock_mask(minute: np.ndarray, text: str) -> np.ndarray:
    match = _CLOCK.match(text)
    assert match is not None
    start = int(match.group(1)) * 60 + int(match.group(2))
    end = int(match.group(3)) * 60 + int(match.group(4))
    if start == end:
        return np.ones(len(minute), dtype=bool)
    if start < end:
        return (minute >= start) & (minute < end)
    return (minute >= start) | (minute < end)


class Grid:
    """The ledger read back: arrays over cells, one row per meter."""

    def __init__(self, directory: Path) -> None:
        data = np.load(Path(directory) / GRID_FILE, allow_pickle=False)
        meta = read_json(Path(directory) / GRID_META)
        self.meta = meta
        self.time = data["time"].astype("datetime64[ns]")
        self.step_h = float(meta["step_hours"])
        self.cells = len(self.time)
        self.meters: list[str] = list(meta["meters"])
        self.index = {meter_id: position for position, meter_id in enumerate(self.meters)}
        self.kwh = data["kwh"]
        self.ok = np.isfinite(self.kwh)
        self.energy = np.nan_to_num(self.kwh)
        self.power = self.energy / self.step_h
        self.lines: list[str] = list(meta["lines"])
        self.line_index = {meter_id: position for position, meter_id in enumerate(self.lines)}
        self.band = data["band"]
        self.phase = data["phase"]
        self.batch = data["batch"]
        self.task_ids: list[str] = list(meta["task_ids"])
        self.plant = data["plant"]
        self.plant_states: list[str] = list(meta["plant_states"])
        self.stop = data["stop"].astype(bool)
        self.running = data["running"]
        self.line_known = data["line_known"].astype(bool)
        self.tariff = data["tariff"]
        self.tariff_names: list[str] = list(meta["tariff_names"])
        self.price = data["price"]
        self.reference_price = data["reference_price"]
        self.names: dict[str, str] = meta["names"]
        self.groups: dict[str, str | None] = meta["groups"]
        self.intervals_minutes: dict[str, float] = meta.get("intervals_minutes", {})
        self.currency: str | None = meta.get("currency")
        self.day_status: dict[str, str] = meta.get("day_status", {})
        stamps = pd.DatetimeIndex(self.time)
        days = stamps.normalize()
        self.day_keys: list[str] = [day.date().isoformat() for day in pd.DatetimeIndex(sorted(set(days)))]
        position = {key: number for number, key in enumerate(self.day_keys)}
        self.day = np.array([position[day.date().isoformat()] for day in days], dtype=np.int32)
        self.weekday = stamps.dayofweek.to_numpy() + 1
        self.minute = (stamps.hour * 60 + stamps.minute).to_numpy()
        self.cells_per_day = int(round(24 / self.step_h))

    # Per meter ---------------------------------------------------------------
    def row(self, meter_id: str) -> int:
        return self.index[meter_id]

    def energy_of(self, meter_id: str) -> np.ndarray:
        return self.energy[self.index[meter_id]]

    def power_of(self, meter_id: str) -> np.ndarray:
        return self.power[self.index[meter_id]]

    def interval_cells(self, meter_id: str) -> int:
        """How many cells one recording interval of this meter spans (1 when the grid does not say)."""

        minutes = self.intervals_minutes.get(meter_id)
        return max(1, int(round(minutes / 60 / self.step_h))) if minutes else 1

    def ok_of(self, meter_id: str) -> np.ndarray:
        return self.ok[self.index[meter_id]]

    def total(self, meter_id: str) -> float:
        return float(self.energy_of(meter_id).sum())

    def coverage(self, meter_id: str) -> float:
        return float(self.ok_of(meter_id).mean()) if self.cells else 0.0

    def daily(self, values: np.ndarray) -> np.ndarray:
        return np.bincount(self.day, weights=values, minlength=len(self.day_keys))

    def full_days(self, meter_id: str) -> np.ndarray:
        """Days on which every cell of the meter has data."""

        counts = np.bincount(self.day, weights=self.ok_of(meter_id).astype(float), minlength=len(self.day_keys))
        expected = np.bincount(self.day, minlength=len(self.day_keys))
        return (counts == expected) & (expected >= self.cells_per_day)

    def line_energy(self) -> np.ndarray:
        if not self.lines:
            return np.zeros(self.cells)
        return self.energy[[self.index[meter_id] for meter_id in self.lines]].sum(axis=0)

    def in_batch(self, line: str) -> np.ndarray:
        phase = self.phase[self.line_index[line]]
        return np.isin(phase, (_PREPARING, _PROCESSING, _CONFLICT))

    def phase_of(self, line: str, name: str) -> np.ndarray:
        return np.isin(self.phase[self.line_index[line]], PHASES[name])

    def band_of(self, line: str) -> np.ndarray:
        return self.band[self.line_index[line]]

    # Selections --------------------------------------------------------------
    def mask(self, when: When | dict[str, Any] | None, meter_id: str | None = None) -> np.ndarray:
        """The cells a selection holds, for one meter (band and phase default to it)."""

        if when is None:
            return np.ones(self.cells, dtype=bool)
        if isinstance(when, dict):
            when = When.model_validate(when)
        selected = np.ones(self.cells, dtype=bool)
        if when.plant == "STOP":
            selected &= self.stop
        elif when.plant == "RUN":
            selected &= self.line_known & ~self.stop
        if when.registered:
            unknown = [state for state in when.registered if state not in self.plant_states]
            if unknown:
                raise SelectorError(f"登记的全厂状态只有 {self.plant_states}，没有 {unknown}")
            codes = [self.plant_states.index(state) for state in when.registered]
            selected &= np.isin(self.plant, codes)
        if when.band or when.phase:
            line = when.line or meter_id
            if line is None or line not in self.line_index:
                raise SelectorError(
                    f"band 和 phase 要说是哪条产线表（line）；{line or '没写'} 不是产线表，产线表有 {self.lines}"
                )
            if when.band:
                selected &= np.isin(self.band_of(line), [BAND_CODES[name] for name in when.band])
            if when.phase:
                hold = np.zeros(self.cells, dtype=bool)
                for name in when.phase:
                    hold |= self.phase_of(line, name)
                selected &= hold
        elif when.line is not None and when.line not in self.line_index:
            raise SelectorError(f"{when.line} 不是产线表，产线表有 {self.lines}")
        if when.dates:
            hold = np.zeros(self.cells, dtype=bool)
            stamps = self.time
            for entry in when.dates:
                parts = entry.split("..")
                first = np.datetime64(parts[0])
                last = np.datetime64(parts[-1]) + np.timedelta64(1, "D")
                hold |= (stamps >= first) & (stamps < last)
            selected &= hold
        if when.weekdays:
            selected &= np.isin(self.weekday, when.weekdays)
        if when.clock:
            selected &= _clock_mask(self.minute, when.clock)
        if when.tariff:
            unknown = [name for name in when.tariff if name not in self.tariff_names]
            if unknown:
                raise SelectorError(f"电价时段只有 {self.tariff_names}，没有 {unknown}")
            selected &= np.isin(self.tariff, [self.tariff_names.index(name) for name in when.tariff])
        if when.day_status:
            statuses = np.array([self.day_status.get(key) == when.day_status for key in self.day_keys])
            selected &= statuses[self.day] if len(statuses) else selected
        return selected

    def cell_text(self, index: int) -> str:
        return pd.Timestamp(self.time[index]).strftime("%Y-%m-%d %H:%M")

    def end_text(self, index: int) -> str:
        return (pd.Timestamp(self.time[index]) + pd.Timedelta(hours=self.step_h)).strftime("%Y-%m-%d %H:%M")


def _reference_price(tariff: Any) -> float:
    """The hour-weighted median price of one tariff: the middle level of its periods, not its cheapest."""

    weighted = sorted((period.price, sum(end - start for start, end in period.minutes())) for period in tariff.periods)
    total = sum(minutes for _price, minutes in weighted)
    running = 0
    for price, minutes in weighted:
        running += minutes
        if running * 2 >= total:
            return float(price)
    return float(weighted[-1][0])


def export_grid(builder: Any, directory: Path) -> None:
    """Write the ledger from a foundation builder (called at the end of build_foundation)."""

    meters = list(builder.specs)
    cells = builder.cells
    kwh = np.vstack([builder.power_at_cell[meter_id] * builder.step_h for meter_id in meters]) if meters else np.zeros((0, cells))
    lines = list(builder.production_meters)
    band = np.vstack([builder.band_at_cell[meter_id] for meter_id in lines]) if lines else np.zeros((0, cells), dtype=np.int8)
    phase = (
        np.vstack([builder.meter_codes(meter_id, builder.grid_mid) for meter_id in lines]).astype(np.int8)
        if lines
        else np.zeros((0, cells), dtype=np.int8)
    )
    batch = np.full((len(lines), cells), -1, dtype=np.int32)
    task_ids: list[str] = []
    rows = getattr(builder, "task_rows", [])
    for row in rows:
        source = row["batch"]
        meter_id = row.get("meter")
        if meter_id not in lines or source.problem or not (source.start and source.end):
            continue
        low = np.datetime64(pd.Timestamp(source.start), "ns")
        high = np.datetime64(pd.Timestamp(source.end), "ns")
        inside = (builder.grid_mid >= low) & (builder.grid_mid < high)
        if not inside.any():
            continue
        batch[lines.index(meter_id)][inside] = len(task_ids)
        task_ids.append(row["id"])
    tariff = np.full(cells, -1, dtype=np.int16)
    price = np.full(cells, np.nan)
    reference = np.full(cells, np.nan)
    names: list[str] = []
    schedule = builder.site.tariff()
    currency = None
    if schedule is not None:
        currency = schedule.currency
        starts = pd.DatetimeIndex(builder.grid)
        step = timedelta(hours=builder.step_h)
        cache: dict[tuple[Any, ...], tuple[int, float, float]] = {}
        for position, moment in enumerate(starts):
            begin = moment.to_pydatetime()
            chosen = None
            for number, spec in enumerate(schedule.tariffs):
                if (spec.valid_from is None or spec.valid_from <= begin) and (spec.valid_until is None or begin + step <= spec.valid_until):
                    chosen = number
                    break
            if chosen is None:
                continue
            key = (chosen, begin.hour, begin.minute)
            if key not in cache:
                status, _reason = schedule.tariffs[chosen].price_status(begin, begin + step)
                if status is None:
                    cache[key] = (-1, np.nan, np.nan)
                else:
                    name, value = status
                    if name not in names:
                        names.append(name)
                    cache[key] = (names.index(name), float(value), _reference_price(schedule.tariffs[chosen]))
            tariff[position], price[position], reference[position] = cache[key]
    np.savez_compressed(
        Path(directory) / GRID_FILE,
        time=builder.grid.astype("datetime64[ns]"),
        kwh=kwh,
        band=band.astype(np.int8),
        phase=phase,
        batch=batch,
        plant=builder.plant.astype(np.int8),
        stop=builder.power_stop.astype(np.int8),
        running=builder.running_lines.astype(np.int16),
        line_known=builder.line_known.astype(np.int8),
        tariff=tariff,
        price=price,
        reference_price=reference,
    )
    from .foundation import PLANT_STATES  # the foundation imports this module

    write_json(
        Path(directory) / GRID_META,
        {
            "step_hours": builder.step_h,
            "meters": meters,
            "lines": lines,
            "task_ids": task_ids,
            "plant_states": list(PLANT_STATES),
            "tariff_names": names,
            "currency": currency,
            "names": {meter_id: builder.specs[meter_id].name for meter_id in meters},
            "groups": {meter_id: builder.specs[meter_id].group for meter_id in meters},
            "intervals_minutes": {meter_id: builder.specs[meter_id].interval_minutes for meter_id in meters},
            "day_status": getattr(builder, "day_status", {}),
            "line_power_p95": builder.line_power_p95,
            "note": "每格是最细表间隔的一步；电量按记录功率 × 步长摊到格；没数的格是 NaN",
        },
    )


def load_grid(site: Any) -> Grid:
    from .foundation import FOUNDATION_DIR

    return Grid(site.path(FOUNDATION_DIR))


def daterange_text(days: list[str]) -> str:
    """Days as compact ranges: 2026-07-05..2026-07-07、2026-07-12."""

    if not days:
        return ""
    parsed = sorted(date.fromisoformat(day) for day in days)
    parts = []
    first = last = parsed[0]
    for day in parsed[1:]:
        if day == last + timedelta(days=1):
            last = day
            continue
        parts.append(first.isoformat() if first == last else f"{first.isoformat()}..{last.isoformat()}")
        first = last = day
    parts.append(first.isoformat() if first == last else f"{first.isoformat()}..{last.isoformat()}")
    return "、".join(parts)


__all__ = ["GRID_FILE", "GRID_META", "Grid", "SelectorError", "When", "daterange_text", "export_grid", "load_grid"]
