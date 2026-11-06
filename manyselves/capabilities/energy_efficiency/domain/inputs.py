"""The input format of one site's energy data and its normalization.

A site's input folder holds ``manifest.yaml`` and the data files it names.
The manifest says what every column means; nothing about a column's meaning
is inferred from its name or its values.  In particular:

- ``interval_energy`` values are the energy of one recording interval, and
  ``timestamp_marks`` says whether the timestamp is the start or the end of
  that interval;
- ``cumulative_energy`` values are meter readings; the energy of an interval
  is the difference of two adjacent readings.  A reading that goes down is a
  break: no difference is taken across it or next to it, and nothing is
  filled in or spread over the gap (see ``_difference_readings``);
- ``instant_power`` values are power at an instant.  They are never turned
  into energy;
- ``interval_avg_power`` values are the average power of one recording
  interval.  They are kept as power; whether they may be turned into energy
  is left open (see the handoff notes).

Rows that cannot be read are counted in the meter's quality notes, not
repaired.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from datetime import datetime, time, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MANIFEST_NAME = "manifest.yaml"

Semantics = Literal[
    "interval_energy",
    "cumulative_energy",
    "instant_power",
    "interval_avg_power",
]
ENERGY_UNITS = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}
POWER_UNITS = {"W": 0.001, "kW": 1.0, "MW": 1000.0}
ENERGY_SEMANTICS = {"interval_energy", "cumulative_energy"}
_ID = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
_CLOCK = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$|^24:00$")


class InputFormatError(ValueError):
    """The input folder does not follow the documented format."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TableSource(_Strict):
    file: str = Field(min_length=1, description="相对 manifest 所在文件夹的路径")
    sheet: str | None = None
    encoding: str = "utf-8-sig"
    time_format: str | None = Field(
        default=None,
        description=(
            "时间列的写法（strftime 格式，例如 %d/%m/%Y %H:%M）。不写时只认年在前的写法"
            "（2026-03-02 08:15、2026/3/2 8:15）和 Excel 日期单元格，其他写法算无法读取"
        ),
    )

    @field_validator("file")
    @classmethod
    def inside_folder(cls, value: str) -> str:
        value = value.replace("\\", "/")
        parts = PurePosixPath(value)
        if parts.is_absolute() or re.match(r"^[A-Za-z]:", value) or ".." in parts.parts:
            raise ValueError(f"file must be a path inside the input folder, not {value!r}")
        return value

    @field_validator("time_format")
    @classmethod
    def no_zone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if "%z" in value or "%Z" in value:
            raise ValueError("times are site local time; time_format cannot read a time zone")
        if "%" not in value:
            # pandas reads words such as 'mixed' or 'ISO8601' as "guess per cell".
            raise ValueError(f"time_format must be a strftime layout such as %d/%m/%Y %H:%M, not {value!r}")
        if "%I" in value and "%p" not in value:
            # Without AM/PM a 12-hour clock reads 12:15 as 00:15.
            raise ValueError("time_format uses %I (12-hour clock) without %p; use %H for 24-hour times")
        try:
            pd.to_datetime(pd.Series([None], dtype=object), format=value, errors="coerce")
            datetime(2026, 1, 2, 3, 4, 5).strftime(value)
        except ValueError as exc:
            raise ValueError(f"time_format {value!r} cannot be used: {exc}") from exc
        return value


class MeterSpec(TableSource):
    id: str = Field(pattern=_ID)
    name: str = Field(min_length=1)
    time_column: str = Field(min_length=1)
    value_column: str = Field(min_length=1)
    semantics: Semantics
    unit: str
    interval_minutes: int = Field(gt=0, le=24 * 60)
    timestamp_marks: Literal["start", "end"] | None = None
    group: str | None = None
    production_machines: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unit_matches_semantics(self) -> "MeterSpec":
        units = ENERGY_UNITS if self.semantics in ENERGY_SEMANTICS else POWER_UNITS
        if self.unit not in units:
            raise ValueError(
                f"meter {self.id}: unit {self.unit!r} does not fit {self.semantics}; "
                f"use one of {sorted(units)}"
            )
        if self.semantics in {"interval_energy", "interval_avg_power"}:
            if self.timestamp_marks is None:
                raise ValueError(
                    f"meter {self.id}: {self.semantics} needs timestamp_marks "
                    "(start or end of the interval)"
                )
        elif self.timestamp_marks is not None:
            raise ValueError(f"meter {self.id}: timestamp_marks only applies to interval data")
        return self

    @property
    def unit_kind(self) -> Literal["energy", "power"]:
        return "energy" if self.semantics in ENERGY_SEMANTICS else "power"


class ProductionSpec(TableSource):
    machine_column: str = Field(min_length=1)
    start_column: str = Field(min_length=1)
    end_column: str = Field(min_length=1)
    product_column: str | None = None
    quantity_column: str | None = None


class CalendarSpec(TableSource):
    """Windows a person has confirmed, each with a state such as non_production."""

    start_column: str = Field(min_length=1)
    end_column: str = Field(min_length=1)
    state_column: str = Field(min_length=1)


class TariffPeriod(_Strict):
    name: str = Field(min_length=1)
    start: str
    end: str
    price: float = Field(ge=0)

    @field_validator("start", "end")
    @classmethod
    def clock(cls, value: str) -> str:
        if not _CLOCK.match(value):
            raise ValueError(f"time of day must be HH:MM, not {value!r}")
        return value

    def minutes(self) -> list[tuple[int, int]]:
        start, end = _clock_minutes(self.start), _clock_minutes(self.end)
        if start == end:
            raise ValueError(f"tariff period {self.name} is empty")
        return [(start, end)] if start < end else [(start, 24 * 60), (0, end)]


class TariffSpec(_Strict):
    """A time-of-use price per kWh that a person has confirmed for the site."""

    currency: str = "元"
    periods: list[TariffPeriod] = Field(min_length=1)

    @model_validator(mode="after")
    def no_overlap(self) -> "TariffSpec":
        spans = sorted(span for period in self.periods for span in period.minutes())
        for (_s1, e1), (s2, _e2) in zip(spans, spans[1:], strict=False):
            if s2 < e1:
                raise ValueError("tariff periods overlap")
        return self

    def price_at(self, moment: datetime) -> tuple[str, float] | None:
        minute = moment.hour * 60 + moment.minute
        for period in self.periods:
            if any(start <= minute < end for start, end in period.minutes()):
                return period.name, period.price
        return None

    def price_over(self, start: datetime, end: datetime) -> tuple[str, float] | None:
        """The price of all of [start, end), or None when the record runs under two prices.

        Periods with the same price that touch (22:00-24:00 and 00:00-08:00,
        or one 00:00-24:00 period across midnight) count as one price.  Time
        that no period covers has no price.
        """

        length = (end - start).total_seconds() / 60
        if length <= 0:
            return None
        begin = start.hour * 60 + start.minute + start.second / 60
        finish = begin + length
        covered = 0.0
        found: dict[float, list[str]] = {}
        for day in range(int(finish // (24 * 60)) + 1):
            for period in self.periods:
                for first, last in period.minutes():
                    low = max(begin, first + day * 24 * 60)
                    high = min(finish, last + day * 24 * 60)
                    if high > low:
                        covered += high - low
                        names = found.setdefault(period.price, [])
                        if period.name not in names:
                            names.append(period.name)
        if len(found) != 1 or covered < length - 1e-6:
            return None
        ((price, names),) = found.items()
        return "/".join(names), price


class PrecheckSpec(_Strict):
    """A finding method a person asked to run on meters before the model looks."""

    method: str = Field(min_length=1)
    meters: list[str] | Literal["all"] = "all"
    params: dict[str, Any] = Field(default_factory=dict)


class SiteSpec(_Strict):
    name: str = Field(min_length=1)
    timezone: str | None = None


class PeriodSpec(_Strict):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def ordered(self) -> "PeriodSpec":
        if self.start.tzinfo or self.end.tzinfo:
            raise ValueError("period uses site local time without a time zone")
        if self.end <= self.start:
            raise ValueError("period end must be after its start")
        return self


class Manifest(_Strict):
    site: SiteSpec
    period: PeriodSpec | None = None
    meters: list[MeterSpec] = Field(min_length=1)
    production: ProductionSpec | None = None
    calendar: CalendarSpec | None = None
    tariff: TariffSpec | None = None
    prechecks: list[PrecheckSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_meters(self) -> "Manifest":
        ids = [meter.id for meter in self.meters]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"meter ids repeat: {duplicates}")
        for check in self.prechecks:
            if check.meters != "all":
                unknown = sorted(set(check.meters) - set(ids))
                if unknown:
                    raise ValueError(f"precheck {check.method} names unknown meters {unknown}")
        machines = [machine for meter in self.meters for machine in meter.production_machines]
        shared = sorted({item for item in machines if machines.count(item) > 1})
        if shared:
            raise ValueError(f"production machines mapped to more than one meter: {shared}")
        return self

    def meter(self, meter_id: str) -> MeterSpec:
        for meter in self.meters:
            if meter.id == meter_id:
                return meter
        raise KeyError(meter_id)


def _clock_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def load_manifest(folder: Path) -> Manifest:
    path = Path(folder) / MANIFEST_NAME
    if not path.is_file():
        raise InputFormatError(f"{MANIFEST_NAME} is missing in {folder}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise InputFormatError(f"{MANIFEST_NAME} cannot be read: {exc}") from exc
    try:
        return Manifest.model_validate(data)
    except ValueError as exc:
        raise InputFormatError(f"{MANIFEST_NAME} does not follow the format: {exc}") from exc


def manifest_files(manifest: Manifest) -> list[str]:
    """Every data file the manifest names, in a stable order."""

    names = [meter.file for meter in manifest.meters]
    for source in (manifest.production, manifest.calendar):
        if source is not None:
            names.append(source.file)
    return sorted(set(names))


def safe_relative(folder: Path, name: str) -> Path:
    root = Path(folder).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise InputFormatError(f"data file is outside the input folder: {name}")
    if not path.is_file():
        raise InputFormatError(f"data file is missing: {name}")
    return path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_table(folder: Path, source: TableSource, columns: list[str]) -> pd.DataFrame:
    path = safe_relative(folder, source.file)
    suffix = path.suffix.casefold()
    try:
        if suffix == ".csv":
            frame = pd.read_csv(path, dtype=str, encoding=source.encoding, keep_default_na=False)
        elif suffix in {".xlsx", ".xlsm"}:
            frame = pd.read_excel(path, sheet_name=source.sheet or 0, dtype=object)
        else:
            raise InputFormatError(f"{source.file}: only .csv and .xlsx files are read")
    except InputFormatError:
        raise
    except (
        UnicodeDecodeError,
        ValueError,
        OSError,
        LookupError,
        ImportError,
        SyntaxError,  # XML parse errors inside a broken .xlsx
        zipfile.BadZipFile,
    ) as exc:
        raise InputFormatError(f"{source.file} cannot be read: {exc}") from exc
    frame.columns = [str(column).strip() for column in frame.columns]
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise InputFormatError(f"{source.file} has no column {missing}")
    return frame


_YEAR_FIRST = re.compile(
    r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?)?$"
)
# A year-first time followed by a zone: the only writing taken as "has a time zone".
_ZONED = re.compile(
    r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:Z|[+-]\d{2}:?\d{2})$"
)
_MIDNIGHT_END = re.compile(r"(?<!\d)24:00(?::00)?(?!\d)")


def _year_first(value: Any) -> Any:
    """One cell as a time when its writing is unambiguous; otherwise NaT.

    ``24:00`` is read as 00:00 of the next day.
    """

    if isinstance(value, pd.Timestamp):
        return value
    if isinstance(value, datetime):
        return pd.Timestamp(value)
    if not isinstance(value, str):
        return pd.NaT
    match = _YEAR_FIRST.match(value)
    if match is None:
        return pd.NaT
    year, month, day, hour, minute, second, fraction = match.groups()
    next_day = (
        hour is not None
        and int(hour) == 24
        and int(minute) == 0
        and int(second or 0) == 0
        and int(fraction or 0) == 0
    )
    try:
        moment = datetime(
            int(year),
            int(month),
            int(day),
            0 if next_day else int(hour or 0),
            int(minute or 0),
            int(second or 0),
            int((fraction or "0").ljust(6, "0")),
        )
        return pd.Timestamp(moment + timedelta(days=1) if next_day else moment)
    except (ValueError, OverflowError):
        return pd.NaT


def _with_format(value: Any, time_format: str) -> Any:
    """One text cell read with exactly ``time_format``; ``24:00`` is 00:00 of the next day."""

    try:
        return pd.Timestamp(datetime.strptime(value, time_format))
    except ValueError:
        pass
    if not _MIDNIGHT_END.search(value):
        return pd.NaT
    try:
        moment = datetime.strptime(_MIDNIGHT_END.sub(lambda match: match.group(0).replace("24", "00", 1), value), time_format)
    except ValueError:
        return pd.NaT
    if moment.time() != time(0, 0):
        return pd.NaT
    try:
        return pd.Timestamp(moment + timedelta(days=1))
    except (ValueError, OverflowError):
        return pd.NaT


def parse_times(values: pd.Series, label: str, time_format: str | None = None) -> pd.Series:
    """Parse local times without guessing how they are written.

    Spreadsheet date cells are read as they are.  With ``time_format`` every
    text cell is read with exactly that format.  Without it only year-first
    writings are read, because a day/month order cannot be told from the
    values.  A cell that cannot be read becomes NaT and is counted, never
    guessed.  A year-first time with a zone (``+08:00``, ``Z``) refuses the
    whole column: the data must be in site local time.
    """

    cleaned = values.map(lambda value: value.strip() if isinstance(value, str) else value)
    cleaned = cleaned.map(lambda value: None if value == "" else value)
    for value in cleaned:
        if isinstance(value, str) and _ZONED.match(value):
            raise InputFormatError(f"{label}: times must be site local time without a time zone")
        if isinstance(value, (pd.Timestamp, datetime)) and value.tzinfo is not None:
            raise InputFormatError(f"{label}: times must be site local time without a time zone")
    if time_format is not None:
        texts = cleaned.map(lambda value: value if isinstance(value, str) else None)
        parsed = pd.to_datetime(texts, format=time_format, errors="coerce")
        if getattr(parsed.dtype, "tz", None) is not None or parsed.dtype == object:
            raise InputFormatError(f"{label}: times must be site local time without a time zone")
        fixes: dict[int, Any] = {}
        for position in np.flatnonzero(parsed.isna().to_numpy()):
            value = cleaned.iloc[position]
            if isinstance(value, (datetime, pd.Timestamp)):
                fixes[int(position)] = value
            elif isinstance(value, str) and _MIDNIGHT_END.search(value):
                fixes[int(position)] = _with_format(value, time_format)
        if fixes:
            merged = parsed.astype(object)
            for position, value in fixes.items():
                merged.iloc[position] = value
            # A moment outside what pandas can hold becomes NaT, like any unreadable cell.
            parsed = pd.to_datetime(merged, errors="coerce")
        return parsed
    return pd.to_datetime(cleaned.map(_year_first), errors="coerce")


def parse_numbers(values: pd.Series) -> pd.Series:
    """Numbers of a column; text, empty cells and infinities are unreadable (NaN)."""

    cleaned = values.map(lambda value: value.strip() if isinstance(value, str) else value)
    numbers = pd.to_numeric(cleaned.replace("", None), errors="coerce").astype(float)
    return numbers.where(np.isfinite(numbers))


class MeterQuality(_Strict):
    rows: int = 0
    unreadable_time_rows: int = 0
    unreadable_value_rows: int = 0
    duplicate_rows_dropped: int = 0
    conflicting_duplicate_times: int = 0
    negative_values_excluded: int = 0
    overlapping_intervals_excluded: int = 0
    reading_decreases: list[str] = Field(default_factory=list)
    intervals_excluded_at_breaks: int = 0
    long_intervals: int = 0
    outside_period_excluded: int = 0
    records: int = 0
    first: str | None = None
    last: str | None = None

    def notes(self) -> list[str]:
        """Plain notes for the model and the register; empty when the data is clean."""

        notes: list[str] = []
        if self.unreadable_time_rows:
            notes.append(f"{self.unreadable_time_rows} 行时间无法读取，未纳入")
        if self.unreadable_value_rows:
            notes.append(f"{self.unreadable_value_rows} 行数值无法读取，未纳入")
        if self.conflicting_duplicate_times:
            notes.append(f"{self.conflicting_duplicate_times} 个时间点有互相矛盾的重复记录，均未纳入")
        if self.negative_values_excluded:
            notes.append(f"{self.negative_values_excluded} 个负值未纳入")
        if self.overlapping_intervals_excluded:
            notes.append(f"{self.overlapping_intervals_excluded} 个与前一区间重叠的区间未纳入")
        if self.reading_decreases:
            shown = "、".join(self.reading_decreases[:5])
            notes.append(
                f"累计读数下降 {len(self.reading_decreases)} 次（{shown}）；分不清是换表清零还是个别读数错误，"
                f"下降处前后、读数停在异常值上的一段、跳入或跳回异常读数的区间，共 {self.intervals_excluded_at_breaks} 个区间不做差分、未纳入"
            )
        if self.long_intervals:
            notes.append(f"{self.long_intervals} 个区间跨过缺测时段，只按整段使用，不摊分")
        if self.outside_period_excluded:
            notes.append(f"{self.outside_period_excluded} 条记录不完全在分析期内，未纳入")
        return notes


def _dedupe(frame: pd.DataFrame, quality: MeterQuality) -> pd.DataFrame:
    """Drop exact repeats; drop every row of a time that has conflicting values."""

    exact = frame.duplicated(subset=["time", "value"], keep="first")
    quality.duplicate_rows_dropped = int(exact.sum())
    frame = frame[~exact]
    conflicting = frame.duplicated(subset=["time"], keep=False)
    quality.conflicting_duplicate_times = int(frame.loc[conflicting, "time"].nunique())
    return frame[~conflicting].sort_values("time").reset_index(drop=True)


def _difference_readings(raw: pd.DataFrame, quality: "MeterQuality") -> pd.DataFrame:
    """Intervals between adjacent readings of a meter register.

    A register only counts up, so a reading below the one before it breaks
    it.  Whether the meter was reset, rolled over or replaced, or some
    readings are wrong (zeros or an error value such as 65535 while the link
    is down), cannot be told from the values, so around a break nothing is
    assumed.  These intervals are left out:

    - the interval across the drop, the one before it and the one after it;
    - after the drop, a run of readings that stays flat (or keeps falling)
      to the next rise or to the end of the data, with the interval that
      rises out of it: stuck or zero readings would otherwise read as no use
      followed by one made-up jump.  When the drop lands back at or above
      the reading before a flat high stretch (one wrong high reading on an
      idle plateau), that stretch explains the drop and only the rise is
      left out;
    - when the readings later climb back past the reading just before the
      drop after rising on the way, the interval that climbs past;
    - before the drop, the latest climb since the previous break from at or
      below the reading just after the drop to above it, or, when the drop
      lands below every reading since that break, the climb into the flat
      stretch just before the drop.  If the readings stayed flat from that
      climb to the drop (a run of wrong high readings), the whole stretch,
      unless the readings after the drop came straight back past the old
      reading, which shows the drop itself was wrong; if they then fall
      again before rising, the stretch is left out after all.  If they rose
      on the way, only that climb.

    A run that kept rising is cut only at its edges, so a reset or rollover
    loses a few intervals per cycle rather than the whole cycle.  The price
    is that a run of three or more wrong readings that keeps rising keeps
    its inner intervals, and that a real reset followed by a long idle spell
    loses the idle spell.  What is left out is counted, never filled or
    spread.
    """

    readings = raw["value"].to_numpy(dtype=float)
    stamps = raw["time"].to_list()
    excluded: set[int] = set()  # interval i runs from reading i-1 to reading i
    # Drops whose readings have not climbed back yet: [level to climb past,
    # drop index, rises before the drop, start of a flat high stretch or
    # None].  Every reading since a drop is below its level, so later drops
    # have lower levels and this is a stack.
    pending: list[list[Any]] = []
    rises = 0
    previous_break = 0
    # A flat high stretch kept because the readings came straight back:
    # (start, drop, rises then).  Falling again before any rise undoes that.
    revisit: tuple[int, int, int] | None = None

    def explained_by_stretch(drop: int, high_start: int | None) -> bool:
        return high_start is not None and readings[drop] >= readings[high_start - 1]

    def flat_run(end: int, rising: bool) -> None:
        """Leave out the flat run after the latest drops, through interval ``end``.

        ``rising`` says interval ``end`` is the rise out of it; otherwise the
        data ends there.
        """

        flat = len(pending)
        while flat > 0 and pending[flat - 1][2] == rises:
            flat -= 1
        for _level, drop, _rises, high_start in pending[flat:]:
            if explained_by_stretch(drop, high_start):
                if rising:
                    excluded.add(end)
                continue
            excluded.update(range(drop, end + 1))
            break

    for index in range(1, len(readings)):
        before, now = readings[index - 1], readings[index]
        if now < before:
            quality.reading_decreases.append(stamps[index].isoformat(sep=" "))
            excluded.update({index - 1, index, index + 1})
            if revisit is not None and revisit[2] == rises:
                excluded.update(range(revisit[0], revisit[1] + 1))
            revisit = None
            high_start = None
            rose = False
            first_rise = None
            for start in range(index - 1, previous_break, -1):
                if readings[start - 1] <= now < readings[start]:
                    if rose:
                        excluded.add(start)
                    else:
                        high_start = start
                    break
                if readings[start] > readings[start - 1]:
                    first_rise = start if not rose else first_rise
                    rose = True
            else:
                high_start = first_rise
            pending.append([before, index, rises, high_start])
            previous_break = index
            continue
        if now == before:
            continue
        flat_run(index, rising=True)
        while pending and pending[-1][0] <= now:
            _level, drop, rises_at_drop, high_start = pending.pop()
            if rises_at_drop == rises:
                # Straight back past the old reading: the low side was wrong.
                if high_start is not None:
                    excluded.add(high_start)
                    revisit = (high_start, drop, rises + 1)
            else:
                excluded.add(index)
                if high_start is not None:
                    excluded.update(range(high_start, drop + 1))
            previous_break = index - 1
        rises += 1
    flat_run(len(readings) - 1, rising=False)
    for _level, drop, _rises, high_start in pending:
        if high_start is not None:
            excluded.update(range(high_start, drop + 1))
    starts, ends, energy = [], [], []
    for index in range(1, len(readings)):
        if index in excluded:
            continue
        starts.append(stamps[index - 1])
        ends.append(stamps[index])
        energy.append(float(readings[index] - readings[index - 1]))
    quality.intervals_excluded_at_breaks = sum(1 for index in excluded if 1 <= index < len(readings))
    return pd.DataFrame(
        {
            "start": pd.Series(starts, dtype=raw["time"].dtype),
            "end": pd.Series(ends, dtype=raw["time"].dtype),
            "kwh": pd.Series(energy, dtype=float),
        }
    )


def normalize_meter(
    spec: MeterSpec,
    frame: pd.DataFrame,
    period: PeriodSpec | None,
) -> tuple[pd.DataFrame, MeterQuality]:
    """Turn one meter's raw rows into intervals (or points) in kWh / kW."""

    quality = MeterQuality(rows=len(frame))
    times = parse_times(frame[spec.time_column], f"{spec.file}:{spec.time_column}", spec.time_format)
    values = parse_numbers(frame[spec.value_column])
    quality.unreadable_time_rows = int(times.isna().sum())
    quality.unreadable_value_rows = int((values.isna() & times.notna()).sum())
    factor = (ENERGY_UNITS if spec.unit_kind == "energy" else POWER_UNITS)[spec.unit]
    raw = pd.DataFrame({"time": times, "value": values * factor}).dropna()
    raw = _dedupe(raw, quality)
    step = pd.Timedelta(minutes=spec.interval_minutes)

    if spec.semantics == "instant_power":
        negative = raw["value"] < 0
        quality.negative_values_excluded = int(negative.sum())
        result = raw.loc[~negative, ["time"]].assign(kw=raw.loc[~negative, "value"])
        if period is not None:
            inside = (result["time"] >= period.start) & (result["time"] < period.end)
            quality.outside_period_excluded = int((~inside).sum())
            result = result[inside]
        result = result.reset_index(drop=True)
        _stamp(quality, result["time"], result["time"])
        return result, quality

    if spec.semantics == "cumulative_energy":
        result = _difference_readings(raw, quality)
    else:
        column = "kwh" if spec.semantics == "interval_energy" else "avg_kw"
        negative = raw["value"] < 0
        quality.negative_values_excluded = int(negative.sum())
        kept = raw[~negative]
        if spec.timestamp_marks == "start":
            starts, ends = kept["time"], kept["time"] + step
        else:
            starts, ends = kept["time"] - step, kept["time"]
        result = pd.DataFrame(
            {"start": starts.to_numpy(), "end": ends.to_numpy(), column: kept["value"].to_numpy()}
        )
        result = result.sort_values("start").reset_index(drop=True)
        overlap = result["start"] < result["end"].shift(1)
        quality.overlapping_intervals_excluded = int(overlap.sum())
        result = result[~overlap]

    if period is not None and len(result):
        inside = (result["start"] >= period.start) & (result["end"] <= period.end)
        quality.outside_period_excluded = int((~inside).sum())
        result = result[inside]
    result = result.reset_index(drop=True)
    durations = result["end"] - result["start"]
    quality.long_intervals = int((durations > step * 1.5).sum())
    _stamp(quality, result["start"], result["end"])
    return result, quality


def _stamp(quality: MeterQuality, starts: pd.Series, ends: pd.Series) -> None:
    quality.records = len(starts)
    if len(starts):
        quality.first = pd.Timestamp(starts.min()).isoformat(sep=" ")
        quality.last = pd.Timestamp(ends.max()).isoformat(sep=" ")


class Batch(_Strict):
    row: int
    machine: str
    start: datetime | None
    end: datetime | None
    product: str | None = None
    quantity: float | None = None
    problem: str | None = None


def _text(value: Any) -> str:
    """A text cell; an empty spreadsheet cell is empty, not 'nan'."""

    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return str(value).strip()


def load_batches(folder: Path, spec: ProductionSpec) -> list[Batch]:
    columns = [spec.machine_column, spec.start_column, spec.end_column]
    columns += [column for column in (spec.product_column, spec.quantity_column) if column]
    frame = read_table(folder, spec, columns)
    starts = parse_times(frame[spec.start_column], f"{spec.file}:{spec.start_column}", spec.time_format)
    ends = parse_times(frame[spec.end_column], f"{spec.file}:{spec.end_column}", spec.time_format)
    quantities = (
        parse_numbers(frame[spec.quantity_column]) if spec.quantity_column else None
    )
    batches: list[Batch] = []
    for index in range(len(frame)):
        start = None if pd.isna(starts.iloc[index]) else starts.iloc[index].to_pydatetime()
        end = None if pd.isna(ends.iloc[index]) else ends.iloc[index].to_pydatetime()
        problem = None
        if start is None or end is None:
            problem = "time_missing"
        elif end <= start:
            problem = "end_not_after_start"
        product = _text(frame[spec.product_column].iloc[index]) if spec.product_column else ""
        quantity = None
        if quantities is not None and not pd.isna(quantities.iloc[index]):
            quantity = float(quantities.iloc[index])
        batches.append(
            Batch(
                row=index + 2,
                machine=_text(frame[spec.machine_column].iloc[index]),
                start=start,
                end=end,
                product=product or None,
                quantity=quantity,
                problem=problem,
            )
        )
    return batches


class CalendarWindow(_Strict):
    row: int
    state: str
    start: datetime
    end: datetime


def load_calendar(folder: Path, spec: CalendarSpec) -> tuple[list[CalendarWindow], int]:
    """The confirmed windows, and how many rows could not be used."""

    frame = read_table(folder, spec, [spec.start_column, spec.end_column, spec.state_column])
    starts = parse_times(frame[spec.start_column], f"{spec.file}:{spec.start_column}", spec.time_format)
    ends = parse_times(frame[spec.end_column], f"{spec.file}:{spec.end_column}", spec.time_format)
    windows: list[CalendarWindow] = []
    unusable = 0
    for index in range(len(frame)):
        state = _text(frame[spec.state_column].iloc[index])
        if pd.isna(starts.iloc[index]) or pd.isna(ends.iloc[index]) or not state:
            unusable += 1
            continue
        start = starts.iloc[index].to_pydatetime()
        end = ends.iloc[index].to_pydatetime()
        if end <= start:
            unusable += 1
            continue
        windows.append(CalendarWindow(row=index + 2, state=state, start=start, end=end))
    return windows, unusable


def clock_of(value: str) -> time:
    if value == "24:00":
        return time(0, 0)
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def clock_window(day: datetime, start: str, end: str) -> tuple[datetime, datetime]:
    """The window of one day from ``start`` to ``end``; past midnight when end <= start."""

    begin = datetime.combine(day.date(), clock_of(start))
    finish = datetime.combine(day.date(), clock_of(end))
    if end == "24:00" or finish <= begin:
        finish += timedelta(days=1)
    return begin, finish


__all__ = [
    "Batch",
    "CalendarSpec",
    "CalendarWindow",
    "ENERGY_SEMANTICS",
    "InputFormatError",
    "MANIFEST_NAME",
    "Manifest",
    "MeterQuality",
    "MeterSpec",
    "PeriodSpec",
    "PrecheckSpec",
    "ProductionSpec",
    "TariffSpec",
    "clock_window",
    "file_sha256",
    "load_batches",
    "load_calendar",
    "load_manifest",
    "manifest_files",
    "normalize_meter",
    "read_table",
    "safe_relative",
]
