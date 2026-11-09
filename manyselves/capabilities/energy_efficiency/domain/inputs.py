"""The input format of one site's energy data and its normalization.

A site's input folder holds ``manifest.yaml`` and the data files it names.
The manifest says what every column means; the reading below infers nothing
about a column's meaning from its name or its values.  (When the folder has
no manifest, the model drafts one from the program's survey of the files and
writes what it could not tell as assumptions; see ``manifest_draft``.)  In
particular:

- ``interval_energy`` values are the energy of one recording interval, and
  ``timestamp_marks`` says whether the timestamp is the start or the end of
  that interval;
- ``cumulative_energy`` values are meter readings; the energy of an interval
  is the difference of two adjacent readings.  A reading that goes down is a
  break: no difference is taken across it or next to it, and nothing is
  filled in or spread over the gap (see ``_difference_readings``);
- ``instant_power`` values are power at an instant.  They are never turned
  into energy;
- ``interval_avg_power`` values are the true average power over one
  recording interval (a person confirmed this for the meters at hand), so
  the energy of the interval is that power times the interval's length
  (rectangle rule).  Nothing is extrapolated past the data and no energy is
  apportioned across an interval's edge.

Rows that cannot be read are counted in the meter's quality notes, not
repaired.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime, time, timedelta
from functools import lru_cache
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


def _inside_folder(value: str) -> str:
    value = value.replace("\\", "/")
    parts = PurePosixPath(value)
    if parts.is_absolute() or re.match(r"^[A-Za-z]:", value) or ".." in parts.parts:
        raise ValueError(f"file must be a path inside the input folder, not {value!r}")
    return value


class TableSource(_Strict):
    file: str = Field(min_length=1, description="相对 manifest 所在文件夹的路径")
    sheet: str | None = None
    encoding: str = "utf-8-sig"
    header_row: int = Field(
        default=1, ge=1, description="表头在第几行（从 1 数）；上面的标题行不读"
    )
    header_rows: int = Field(
        default=1,
        ge=1,
        le=2,
        description=(
            "表头占几行。占两行时，下一行有字的列名写成“上一行/下一行”，"
            "上一行是合并单元格时沿用左边的字；重名的列依次加 .1、.2"
        ),
    )
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
        return _inside_folder(value)

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
    filter_column: str | None = Field(
        default=None,
        min_length=1,
        description="多块表放在同一张长表里时，用这一列筛出本表的行（和 filter_value 一起写）",
    )
    filter_value: str | None = Field(default=None, min_length=1, description="filter_column 等于这个值的行属于本表")

    @model_validator(mode="after")
    def filter_is_whole(self) -> "MeterSpec":
        if (self.filter_column is None) != (self.filter_value is None):
            raise ValueError(f"meter {self.id}: filter_column and filter_value go together")
        return self

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
    def has_energy(self) -> bool:
        """Whether the normalized data holds energy.

        Interval average power is turned into energy; instant power is not.
        """

        return self.semantics != "instant_power"

    @property
    def unit_kind(self) -> Literal["energy", "power"]:
        """What the normalized data holds: energy, or power that is never energy."""

        return "energy" if self.has_energy else "power"


class ProductionSpec(TableSource):
    machine_column: str = Field(min_length=1)
    start_column: str = Field(min_length=1)
    end_column: str = Field(min_length=1)
    start_time_column: str | None = Field(
        default=None,
        min_length=1,
        description="开始的日期和时刻分两列写时，时刻所在的列；start_column 这时只取日期",
    )
    end_time_column: str | None = Field(
        default=None, min_length=1, description="结束的时刻所在的列；end_column 这时只取日期"
    )
    product_column: str | None = None
    product_group_columns: list[str] = Field(
        default_factory=list,
        description="单耗另外按这些列分组各给一张表（例如型号规格、成品品号）；不写时只按 product_column 分",
    )
    quantity_column: str | None = None
    quantity_basis: Literal["input", "output", "qualified"] | None = Field(
        default=None,
        description=(
            "数量列是哪种实际量（知识库 3.4.4）：input 投入量、output 成品量、qualified 合格品量；"
            "由人填写，计划产量不能代替实际产出；不填时不算单耗"
        ),
    )
    quantity_unit: str | None = Field(default=None, min_length=1, description="数量的单位，例如 件、吨")
    reported_energy_column: str | None = Field(
        default=None, min_length=1, description="报表里每批的用电量列；程序拿它和挂表的计量电量比，不当作计量数据"
    )
    reported_energy_unit: Literal["Wh", "kWh", "MWh"] = "kWh"

    @model_validator(mode="after")
    def basis_needs_column(self) -> "ProductionSpec":
        if (self.quantity_basis or self.quantity_unit) and not self.quantity_column:
            raise ValueError("quantity_basis and quantity_unit describe quantity_column, which is not given")
        return self


QUANTITY_BASES = {"input": "投入量", "output": "成品量", "qualified": "合格品量"}


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


UnpricedReason = Literal["outside_dates", "not_covered", "crosses_prices"]
UNPRICED_REASONS: dict[str, str] = {
    "outside_dates": "不在电价的适用日期内",
    "not_covered": "落在电价没有覆盖的时刻",
    "crosses_prices": "跨两个不同价格的电价时段",
}


class TariffSpec(_Strict):
    """A time-of-use price per kWh that a person has confirmed for the site.

    ``valid_from`` / ``valid_until`` bound the time the prices apply to
    ([valid_from, valid_until), site local time); a record outside them is
    not priced.  Without them the prices apply to the whole data period.
    """

    currency: str = "元"
    periods: list[TariffPeriod] = Field(min_length=1)
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def no_overlap(self) -> "TariffSpec":
        spans = sorted(span for period in self.periods for span in period.minutes())
        for (_s1, e1), (s2, _e2) in zip(spans, spans[1:], strict=False):
            if s2 < e1:
                raise ValueError("tariff periods overlap")
        for value in (self.valid_from, self.valid_until):
            if value is not None and value.tzinfo:
                raise ValueError("tariff dates use site local time without a time zone")
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("tariff valid_until must be after valid_from")
        return self

    def price_status(self, start: datetime, end: datetime) -> tuple[tuple[str, float] | None, UnpricedReason | None]:
        """The price of all of [start, end), or why the record has none."""

        if (self.valid_from is not None and start < self.valid_from) or (
            self.valid_until is not None and end > self.valid_until
        ):
            return None, "outside_dates"
        return self._price_within_day(start, end)

    def price_at(self, moment: datetime) -> tuple[str, float] | None:
        minute = moment.hour * 60 + moment.minute
        for period in self.periods:
            if any(start <= minute < end for start, end in period.minutes()):
                return period.name, period.price
        return None

    def price_over(self, start: datetime, end: datetime) -> tuple[str, float] | None:
        """The price of all of [start, end), or None when it has none."""

        return self.price_status(start, end)[0]

    def _price_within_day(
        self, start: datetime, end: datetime
    ) -> tuple[tuple[str, float] | None, UnpricedReason | None]:
        """Periods with the same price that touch (22:00-24:00 and 00:00-08:00,
        or one 00:00-24:00 period across midnight) count as one price.  Time
        that no period covers has no price.
        """

        length = (end - start).total_seconds() / 60
        if length <= 0:
            return None, "not_covered"
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
        if covered < length - 1e-6:
            return None, "not_covered"
        if len(found) != 1:
            return None, "crosses_prices"
        ((price, names),) = found.items()
        return ("/".join(names), price), None


class TariffFileSpec(_Strict):
    """Time-of-use prices read from a site's tariff workbook, one tariff per month.

    The workbook has a price sheet (plan, month, one column per period name
    holding the price per kWh) and a period sheet (plan, month, then pairs of
    period name and hour ranges such as ``7:00 - 11:00`` with several ranges
    split by line breaks or 、).  Each month with hour ranges becomes one
    tariff valid for that calendar month of ``year``.  A period with hour
    ranges and no price is an input error; a period without hour ranges is
    not used that month.
    """

    file: str = Field(min_length=1, description="相对 manifest 所在文件夹的路径")
    plan: str = Field(min_length=1, description="电价方案编号，例如 1G3；表里写 1G3/2G3 的行也算")
    year: int = Field(ge=2000, le=2100, description="表里只写月份，这里写是哪一年")
    currency: str = "元"
    price_sheet: str = "分时电价"
    period_sheet: str = "时段划分"

    @field_validator("file")
    @classmethod
    def inside_folder(cls, value: str) -> str:
        return _inside_folder(value)


class TariffSchedule:
    """The tariffs that apply over the data period; each record is priced by the one whose dates hold it."""

    def __init__(self, tariffs: list[TariffSpec], currency: str) -> None:
        if not tariffs:
            raise ValueError("a tariff schedule needs at least one tariff")
        if len(tariffs) > 1:
            for tariff in tariffs:
                if tariff.valid_from is None or tariff.valid_until is None:
                    raise ValueError("with more than one tariff each needs valid_from and valid_until")
            spans = sorted((tariff.valid_from, tariff.valid_until) for tariff in tariffs)  # type: ignore[type-var]
            for (_s1, e1), (s2, _e2) in zip(spans, spans[1:], strict=False):
                if s2 < e1:
                    raise ValueError("tariff dates overlap")
        self.tariffs = sorted(tariffs, key=lambda item: item.valid_from or datetime.min)
        self.currency = currency

    def price_status(self, start: datetime, end: datetime) -> tuple[tuple[str, float] | None, UnpricedReason | None]:
        """The price of all of [start, end), or why the record has none."""

        for tariff in self.tariffs:
            if (tariff.valid_from is None or tariff.valid_from <= start) and (
                tariff.valid_until is None or end <= tariff.valid_until
            ):
                return tariff.price_status(start, end)
        return None, "outside_dates"

    def price_over(self, start: datetime, end: datetime) -> tuple[str, float] | None:
        return self.price_status(start, end)[0]

    def as_list(self) -> list[dict[str, Any]]:
        return [tariff.model_dump(mode="json") for tariff in self.tariffs]


_HOUR_RANGE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–—~～至到]\s*(\d{1,2}):(\d{2})")
_MONTH = re.compile(r"^\s*(\d{1,2})\s*月?\s*$")


def _month_of(value: Any) -> int | None:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        number = float(value)
        return int(number) if number.is_integer() and 1 <= number <= 12 else None
    match = _MONTH.match(str(value))
    if match and 1 <= int(match.group(1)) <= 12:
        return int(match.group(1))
    return None


def _plan_matches(cell: Any, plan: str) -> bool:
    text = _text(cell)
    return text == plan or plan in [part.strip() for part in re.split(r"[/、,，]", text)]


def load_tariff_file(folder: Path, spec: TariffFileSpec) -> list[TariffSpec]:
    path = safe_relative(folder, spec.file)
    try:
        prices = pd.read_excel(path, sheet_name=spec.price_sheet, dtype=object)
        periods = pd.read_excel(path, sheet_name=spec.period_sheet, header=None, dtype=object)
    except (ValueError, OSError, LookupError, ImportError, SyntaxError, zipfile.BadZipFile) as exc:
        raise InputFormatError(f"{spec.file} cannot be read as a tariff workbook: {exc}") from exc
    prices.columns = [str(column).strip() for column in prices.columns]
    if len(prices.columns) < 3:
        raise InputFormatError(f"{spec.file}:{spec.price_sheet} needs plan, month and price columns")
    plan_col, month_col = prices.columns[0], prices.columns[1]
    price_rows: dict[int, dict[str, Any]] = {}
    for _index, row in prices.iterrows():
        month = _month_of(row[month_col])
        if month is not None and _plan_matches(row[plan_col], spec.plan):
            price_rows[month] = {name: row[name] for name in prices.columns[2:]}
    tariffs: list[TariffSpec] = []
    for index in range(1, len(periods)):
        row = periods.iloc[index].tolist()
        if len(row) < 2 or not _plan_matches(row[0], spec.plan):
            continue
        month = _month_of(row[1])
        if month is None:
            continue
        where = f"{spec.file}:{spec.period_sheet} 第 {index + 1} 行"
        entries: list[TariffPeriod] = []
        for position in range(2, len(row) - 1, 2):
            name = _text(row[position])
            ranges = _HOUR_RANGE.findall(_text(row[position + 1]))
            if not name or not ranges:
                continue
            raw = price_rows.get(month, {}).get(name)
            price = parse_numbers(pd.Series([raw])).iloc[0]
            if pd.isna(price):
                raise InputFormatError(f"{where}：{month} 月的“{name}”有小时区间，但{spec.price_sheet}里没有它的单价")
            for h1, m1, h2, m2 in ranges:
                entries.append(
                    TariffPeriod(name=name, start=f"{int(h1):02d}:{m1}", end=f"{int(h2):02d}:{m2}", price=float(price))
                )
        if not entries:
            continue
        first = datetime(spec.year, month, 1)
        until = datetime(spec.year + (month == 12), month % 12 + 1, 1)
        try:
            tariffs.append(TariffSpec(currency=spec.currency, periods=entries, valid_from=first, valid_until=until))
        except ValueError as exc:
            raise InputFormatError(f"{where}：{exc}") from exc
    if not tariffs:
        raise InputFormatError(f"{spec.file} has no month with hour ranges for plan {spec.plan}")
    return tariffs


def tariff_schedule(folder: Path, manifest: "Manifest") -> TariffSchedule | None:
    """Every tariff the manifest gives, resolved; None when it gives none."""

    source = manifest.tariff
    if source is None:
        return None
    try:
        if isinstance(source, TariffFileSpec):
            return TariffSchedule(load_tariff_file(folder, source), source.currency)
        if isinstance(source, TariffSpec):
            return TariffSchedule([source], source.currency)
        currencies = {tariff.currency for tariff in source}
        if len(currencies) != 1:
            raise InputFormatError(f"tariffs use different currencies: {sorted(currencies)}")
        return TariffSchedule(list(source), source[0].currency)
    except InputFormatError:
        raise
    except ValueError as exc:
        raise InputFormatError(f"tariff: {exc}") from exc


class SiteFactsSpec(TableSource):
    """A site survey table: one question per row with the answer found on site.

    Answers are given to the model as background with their row id; rows whose
    status is not one of ``confirmed_states`` are marked unconfirmed.  The
    program never turns an answer into a number or a rule.
    """

    id_column: str = Field(min_length=1, description="每题编号所在的列，例如 序号")
    topic_column: str = Field(min_length=1, description="问题摘要所在的列")
    answer_column: str = Field(min_length=1, description="现场核实结果所在的列；空的行不读")
    question_column: str | None = Field(default=None, min_length=1, description="完整问题所在的列（可不写）")
    status_column: str | None = Field(default=None, min_length=1, description="答复状态所在的列（可不写）")
    confirmed_states: list[str] = Field(
        default_factory=list, description="状态列里算“已核实”的写法，例如 已答复、已确认；其余状态的答复标为未确认"
    )


class SiteFact(_Strict):
    id: str
    row: int
    topic: str
    question: str | None = None
    answer: str
    status: str | None = None
    confirmed: bool


class Assumption(_Strict):
    """What the person who prepared the inputs inferred about the data and could not confirm.

    It is shown to the model as an inference and listed in the report as 待现场确认.
    """

    id: str = Field(pattern=_ID, description="编号，例如 A1")
    topic: str = Field(min_length=1, description="关于什么，例如 E06 表的计量范围")
    inference: str = Field(min_length=1, description="推断的内容")
    basis: str = Field(min_length=1, description="从哪些数据或资料推出来的")


class AssetListSpec(TableSource):
    """The site's list of metering points (台账): codes, names, energy-use categories, equipment.

    Matched to the meters by code or name; every match is a candidate for a
    person to confirm, never a fact the program relies on.
    """

    code_column: str | None = Field(default=None, min_length=1, description="点位或表计编号所在的列")
    name_column: str | None = Field(default=None, min_length=1, description="点位或表计名称所在的列")
    category_column: str | None = Field(default=None, min_length=1, description="用能类别所在的列")
    equipment_column: str | None = Field(default=None, min_length=1, description="关联设备所在的列")

    @model_validator(mode="after")
    def code_or_name(self) -> "AssetListSpec":
        if not self.code_column and not self.name_column:
            raise ValueError("assets needs code_column or name_column")
        return self


class TreeJsonSpec(_Strict):
    """The plant tree as nested JSON: each node holds its id, name and children."""

    format: Literal["json"]
    file: str = Field(min_length=1)
    nodes_path: list[str | int] = Field(
        default_factory=list, description="从文件根走到最上层节点（列表或单个节点）的键，例如 [data, list]"
    )
    id_key: str = "id"
    name_key: str = "name"
    children_key: str = "children"

    @field_validator("file")
    @classmethod
    def inside_folder(cls, value: str) -> str:
        return _inside_folder(value)


class TreeTableSpec(TableSource):
    """The plant tree as a table: one node per row with its parent's id."""

    format: Literal["table"]
    id_column: str = Field(min_length=1)
    parent_column: str = Field(min_length=1, description="上级节点编号所在的列；最上层的行留空")
    name_column: str = Field(min_length=1)


class TreeNode(_Strict):
    id: str
    name: str
    parent: str | None
    depth: int
    path: list[str]


class AssetRow(_Strict):
    row: int
    code: str | None
    name: str | None
    category: str | None
    equipment: str | None


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
    tariff: TariffSpec | list[TariffSpec] | TariffFileSpec | None = None
    prechecks: list[PrecheckSpec] = Field(default_factory=list)
    site_facts: SiteFactsSpec | None = None
    assets: AssetListSpec | None = None
    plant_tree: TreeJsonSpec | TreeTableSpec | None = Field(default=None, discriminator="format")
    assumptions: list[Assumption] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_assumptions(self) -> "Manifest":
        ids = [item.id for item in self.assumptions]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"assumption ids repeat: {duplicates}")
        return self

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
    sources = (manifest.production, manifest.calendar, manifest.tariff, manifest.site_facts, manifest.assets, manifest.plant_tree)
    for source in sources:
        if isinstance(source, (TableSource, TariffFileSpec, TreeJsonSpec)):
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


def _header_names(rows: pd.DataFrame) -> list[str]:
    """Column names from one or two header rows; repeated names get .1, .2 like pandas."""

    top = [_text(value) for value in rows.iloc[0].tolist()]
    names = list(top)
    if len(rows) > 1:
        bottom = [_text(value) for value in rows.iloc[1].tolist()]
        carried = ""
        for index, (upper, lower) in enumerate(zip(top, bottom, strict=True)):
            carried = upper or carried
            if lower:
                names[index] = f"{carried}/{lower}" if carried else lower
            else:
                carried = upper
    seen: dict[str, int] = {}
    unique = []
    for name in names:
        name = name or "Unnamed"
        count = seen.get(name, 0)
        seen[name] = count + 1
        unique.append(name if count == 0 else f"{name}.{count}")
    return unique


@lru_cache(maxsize=8)
def _load_frame(
    path: str, mtime_ns: int, size: int, sheet: str | None, encoding: str, header_row: int, header_rows: int
) -> pd.DataFrame:
    """One file read once per run, however many meters it holds."""

    del mtime_ns, size
    suffix = Path(path).suffix.casefold()
    first = header_row - 1
    if suffix == ".csv":
        # Title lines above the header may have fewer fields, so they are skipped, not parsed.
        raw = pd.read_csv(path, dtype=str, encoding=encoding, keep_default_na=False, header=None, skiprows=first)
        first = 0
    elif suffix in {".xlsx", ".xlsm"}:
        raw = pd.read_excel(path, sheet_name=sheet or 0, dtype=object, header=None)
    else:
        raise InputFormatError(f"{Path(path).name}: only .csv and .xlsx files are read")
    if len(raw) < first + header_rows:
        raise InputFormatError(f"{Path(path).name} has no header at row {header_row}")
    frame = raw.iloc[first + header_rows :].reset_index(drop=True)
    frame.columns = _header_names(raw.iloc[first : first + header_rows])
    return frame


def read_table(folder: Path, source: TableSource, columns: list[str]) -> pd.DataFrame:
    """The table below its header; row ``i`` of the result is file row ``first_data_row(source) + i``."""

    path = safe_relative(folder, source.file)
    stat = path.stat()
    try:
        frame = _load_frame(
            str(path), stat.st_mtime_ns, stat.st_size, source.sheet, source.encoding, source.header_row, source.header_rows
        ).copy()
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
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise InputFormatError(f"{source.file} has no column {missing}")
    return frame


def first_data_row(source: TableSource) -> int:
    """The file row (counted from 1) of the table's first data row."""

    return source.header_row + source.header_rows


def read_meter_table(folder: Path, spec: MeterSpec) -> pd.DataFrame:
    """The meter's own rows; in a long table, those whose filter column holds the meter's value."""

    columns = [spec.time_column, spec.value_column]
    if spec.filter_column is None:
        return read_table(folder, spec, columns)
    frame = read_table(folder, spec, [*columns, spec.filter_column])
    keep = frame[spec.filter_column].map(_text) == spec.filter_value
    if not keep.any():
        raise InputFormatError(
            f"{spec.file}: no row has {spec.filter_column} = {spec.filter_value!r} (meter {spec.id})"
        )
    return frame.loc[keep, columns].reset_index(drop=True)


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
    intervals_excluded_at_edges: int = 0
    flat_throughout: bool = False
    long_intervals: int = 0
    outside_period_excluded: int = 0
    excluded_spans: list[dict[str, str]] = Field(
        default_factory=list, description="未纳入的时段：起、止、原因（相接的合成一段，最多列 MAX_EXCLUDED_SPANS 段）"
    )
    excluded_spans_more: int = 0
    semantics: str | None = None
    declared_interval_minutes: int | None = None
    median_spacing_minutes: float | None = None
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
        if self.flat_throughout:
            notes.append(
                "累计读数从头到尾没有变化；分不清是一直停机还是通讯中断时记下的异常值，"
                f"{self.intervals_excluded_at_edges} 个区间都未纳入"
            )
        elif self.intervals_excluded_at_edges:
            notes.append(
                "数据开头到第一次上升、最后一次上升到结尾的读数无从核对（导出时正好断线也会是这样），"
                f"这几段共 {self.intervals_excluded_at_edges} 个区间未纳入"
            )
        if self.long_intervals:
            notes.append(f"{self.long_intervals} 个区间跨过缺测时段，只按整段使用，不摊分")
        spacing = self.spacing_note()
        if spacing:
            notes.append(spacing)
        if self.outside_period_excluded:
            notes.append(f"{self.outside_period_excluded} 条记录不完全在分析期内，未纳入")
        return notes


    def spacing_note(self) -> str | None:
        """A note when the timestamps are not as far apart as the manifest says."""

        declared, actual = self.declared_interval_minutes, self.median_spacing_minutes
        if declared is None or actual is None or self.semantics == "cumulative_energy":
            return None
        if 0.9 * declared <= actual <= 1.1 * declared:
            return None
        head = f"相邻记录的时间间隔中位数是 {actual:g} 分钟，manifest 写的是 {declared} 分钟"
        if self.semantics == "interval_energy":
            if actual > declared:
                return (
                    f"{head}；每条电量仍按 {declared} 分钟一段使用。若每条其实是 {actual:g} 分钟的电量，"
                    "平均功率会偏高；若只是每隔几段导出一条，电量会偏少。请人核对 interval_minutes"
                )
            return f"{head}；与前一段重叠的记录已不纳入，电量可能偏少。请人核对 interval_minutes"
        if self.semantics == "interval_avg_power":
            if actual > declared:
                return (
                    f"{head}；每条只代表 {declared} 分钟，中间没有记录的时间不算入电量，覆盖率会偏低。"
                    "请人核对 interval_minutes"
                )
            return f"{head}；与前一段重叠的记录已不纳入。请人核对 interval_minutes"
        return f"{head}；覆盖率按每条 {declared} 分钟估算，会因此失真。请人核对 interval_minutes"


MAX_EXCLUDED_SPANS = 50


def _span_list(pairs: list[tuple[Any, Any]], reason: str) -> list[dict[str, str]]:
    """Time spans left out, touching or overlapping ones joined."""

    merged: list[list[Any]] = []
    for start, end in sorted(pairs):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [
        {"start": pd.Timestamp(start).isoformat(sep=" "), "end": pd.Timestamp(end).isoformat(sep=" "), "reason": reason}
        for start, end in merged
    ]


def _keep_spans(quality: "MeterQuality", spans: list[dict[str, str]]) -> None:
    listed = sorted(quality.excluded_spans + spans, key=lambda span: span["start"])
    quality.excluded_spans = listed[:MAX_EXCLUDED_SPANS]
    quality.excluded_spans_more = max(0, len(listed) - MAX_EXCLUDED_SPANS)


def _spacing(times: pd.Series) -> float | None:
    gaps = times.sort_values().diff().dropna()
    gaps = gaps[gaps > pd.Timedelta(0)]
    if gaps.empty:
        return None
    return round(gaps.median().total_seconds() / 60, 2)


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
      left out, unless that reading itself sits on such a left-out run;
    - when the readings later climb back past the reading just before the
      drop after rising on the way, the interval that climbs past, and when
      they rose straight out of a left-out run onto the flat stretch they
      climb past from (a second error value, such as 0 then 65535), that
      stretch with the step into it;
    - before the drop, the latest climb since the previous break from at or
      below the reading just after the drop to above it, and the flat
      stretch just before the drop with the climb into it (or from the start
      of the data, when the readings were flat from there): it may be a run
      of wrong high readings.  When the readings after the drop come
      straight back past the old reading, which shows the drop itself was
      wrong, only the climb into the stretch is left out; if they then fall
      again before rising, the stretch is left out after all;
    - at the ends of the data, where no drop gives the error away, the
      readings from the start through the first climb and from the last
      climb to the end (an outage logged as 0 or 65535 when the export
      starts or stops, or one failed read), and all of it when the readings
      never change.  These readings cannot vouch for a later drop, and
      readings that came straight back after a drop cannot either when
      they are that end.  These are counted apart.

    A run that kept rising is cut only at its edges, so a reset or rollover
    loses a few intervals per cycle rather than the whole cycle.  The price
    is that a run of three or more wrong readings that keeps rising keeps
    its inner intervals, that a real reset followed by a long idle spell
    loses the idle spell (and the next idle spell too when the readings
    climb past the old reading right after it), that a flat stretch before
    a real reset is lost,
    that the first and last climb of every export and an idle start or end
    are lost (with a wrong reading on an idle start, the idle plateau after
    it too), and that a register that really reaches the error value cannot
    be told from it.  What is left out is counted, never filled or spread.
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
    # The data's own ends: from the start to the first climb, and from the
    # last climb to the end (when the readings do not fall there).  Nothing
    # before or after them shows whether those readings were right.
    last = len(readings) - 1
    head = 0
    while head < last and readings[head + 1] == readings[0]:
        head += 1
    head_run = head < last and readings[head + 1] > readings[head]
    tail = last
    while tail > 0 and readings[tail - 1] == readings[last]:
        tail -= 1
    tail_run = tail >= 1 and readings[tail] > readings[tail - 1]
    # Readings on a flat run left out after a drop or at the start: they
    # cannot vouch for a later drop.
    low_readings: set[int] = set(range(0, head + 1)) if head_run else set()

    def explained_by_stretch(drop: int, high_start: int | None) -> bool:
        # high_start 0 marks a stretch from the start of the data: nothing before it.
        return (
            high_start is not None
            and high_start > 0
            and (high_start - 1) not in low_readings
            and readings[drop] >= readings[high_start - 1]
        )

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
            low_readings.update(range(drop, end if rising else end + 1))
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
                        high_start = first_rise
                    else:
                        high_start = start
                    break
                if readings[start] > readings[start - 1]:
                    first_rise = start if not rose else first_rise
                    rose = True
            else:
                # Flat all the way back to the start of the data: the stretch starts there.
                high_start = 0 if first_rise is None and previous_break == 0 else first_rise
            pending.append([before, index, rises, high_start])
            previous_break = index
            continue
        if now == before:
            continue
        flat_run(index, rising=True)
        low_flat_done = False
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
                if not low_flat_done:
                    low_flat_done = True
                    # The flat stretch the readings climb past from, when the
                    # step into it rose out of a left-out run: a second error
                    # value held below the old reading.
                    flat_start = index - 1
                    while flat_start > 0 and readings[flat_start] == readings[flat_start - 1]:
                        flat_start -= 1
                    if flat_start < index - 1 and (flat_start - 1) in low_readings:
                        excluded.update(range(flat_start, index))
                # A stretch kept because the readings came straight back past
                # it out of a left-out run, now past an older drop too.
                if revisit is not None and revisit[1] > drop and (revisit[0] - 1) in low_readings:
                    excluded.update(range(revisit[0], revisit[1] + 1))
                    revisit = None
            previous_break = index - 1
        rises += 1
    flat_run(len(readings) - 1, rising=False)
    if revisit is not None and revisit[2] == rises and tail_run:
        # The readings that came straight back are the end left out below.
        excluded.update(range(revisit[0], revisit[1] + 1))
    for _level, drop, _rises, high_start in pending:
        if high_start is not None:
            excluded.update(range(high_start, drop + 1))
    edges: set[int] = set()
    if head_run:
        edges.update(range(1, head + 2))
    if tail_run:
        edges.update(range(tail, last + 1))
    if head == last:
        # Flat from start to end: nothing tells an idle meter from a dead link.
        edges.update(range(1, last + 1))
        quality.flat_throughout = last >= 1
    edges -= excluded
    starts, ends, energy = [], [], []
    for index in range(1, len(readings)):
        if index in excluded or index in edges:
            continue
        starts.append(stamps[index - 1])
        ends.append(stamps[index])
        energy.append(float(readings[index] - readings[index - 1]))
    quality.intervals_excluded_at_breaks = sum(1 for index in excluded if 1 <= index < len(readings))
    quality.intervals_excluded_at_edges = len(edges)
    broken = [(stamps[index - 1], stamps[index]) for index in excluded if 1 <= index < len(readings)]
    at_edges = [(stamps[index - 1], stamps[index]) for index in edges]
    _keep_spans(
        quality,
        _span_list(broken, "累计读数下降，或读数停在异常值上、跳入跳回异常读数，不做差分")
        + _span_list(at_edges, "累计读数从头到尾没有变化" if quality.flat_throughout else "数据开头或结尾的读数无从核对"),
    )
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

    quality = MeterQuality(
        rows=len(frame), semantics=spec.semantics, declared_interval_minutes=spec.interval_minutes
    )
    times = parse_times(frame[spec.time_column], f"{spec.file}:{spec.time_column}", spec.time_format)
    values = parse_numbers(frame[spec.value_column])
    quality.unreadable_time_rows = int(times.isna().sum())
    quality.unreadable_value_rows = int((values.isna() & times.notna()).sum())
    factor = (ENERGY_UNITS if spec.semantics in ENERGY_SEMANTICS else POWER_UNITS)[spec.unit]
    raw = pd.DataFrame({"time": times, "value": values * factor}).dropna()
    raw = _dedupe(raw, quality)
    quality.median_spacing_minutes = _spacing(raw["time"])
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
        negative = raw["value"] < 0
        quality.negative_values_excluded = int(negative.sum())
        kept = raw[~negative]
        shift = step if spec.timestamp_marks == "start" else -step
        _keep_spans(
            quality,
            _span_list(
                [(min(time, time + shift), max(time, time + shift)) for time in raw.loc[negative, "time"]], "负值"
            ),
        )
        if spec.timestamp_marks == "start":
            starts, ends = kept["time"], kept["time"] + step
        else:
            starts, ends = kept["time"] - step, kept["time"]
        result = pd.DataFrame({"start": starts.to_numpy(), "end": ends.to_numpy()})
        if spec.semantics == "interval_energy":
            result["kwh"] = kept["value"].to_numpy()
        else:
            # The true average power of the interval times its length (OP016,
            # rectangle rule); the power read stays beside it.
            result["avg_kw"] = kept["value"].to_numpy()
            result["kwh"] = result["avg_kw"] * (spec.interval_minutes / 60)
        result = result.sort_values("start").reset_index(drop=True)
        overlap = result["start"] < result["end"].shift(1)
        quality.overlapping_intervals_excluded = int(overlap.sum())
        _keep_spans(quality, _span_list(list(zip(result.loc[overlap, "start"], result.loc[overlap, "end"], strict=True)), "与前一区间重叠"))
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
    groups: dict[str, str] = Field(default_factory=dict)
    quantity: float | None = None
    reported_energy_kwh: float | None = None
    problem: str | None = None


def _text(value: Any) -> str:
    """A text cell; an empty spreadsheet cell is empty, not 'nan'."""

    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return str(value).strip()


def _date_and_clock(dates: pd.Series, clocks: pd.Series, label: str, time_format: str | None) -> pd.Series:
    """Join a date column and a time-of-day column into one time; either unreadable gives NaT."""

    days = parse_times(dates, label, time_format)
    joined = []
    for day, clock in zip(days, clocks, strict=True):
        if pd.isna(day):
            joined.append(pd.NaT)
            continue
        if isinstance(clock, datetime):
            clock = clock.time()
        if isinstance(clock, time):
            minutes = clock.hour * 60 + clock.minute + clock.second / 60
        else:
            match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", _text(clock))
            if not match or int(match.group(1)) > 24 or int(match.group(2)) > 59:
                joined.append(pd.NaT)
                continue
            minutes = int(match.group(1)) * 60 + int(match.group(2)) + int(match.group(3) or 0) / 60
            if minutes > 24 * 60:
                joined.append(pd.NaT)
                continue
        base = pd.Timestamp(day).normalize()
        joined.append(base + pd.Timedelta(minutes=minutes))
    return pd.Series(pd.to_datetime(joined), index=dates.index)


def _batch_times(frame: pd.DataFrame, spec: ProductionSpec, column: str, clock_column: str | None) -> pd.Series:
    label = f"{spec.file}:{column}"
    if clock_column is None:
        return parse_times(frame[column], label, spec.time_format)
    return _date_and_clock(frame[column], frame[clock_column], label, spec.time_format)


def load_batches(folder: Path, spec: ProductionSpec) -> tuple[list[Batch], int]:
    """The batches, and how many blank rows (no machine and no times, such as subtotal lines) were skipped."""

    time_columns = [spec.start_column, spec.end_column]
    time_columns += [column for column in (spec.start_time_column, spec.end_time_column) if column]
    columns = [spec.machine_column, *time_columns]
    columns += [
        column for column in (spec.product_column, spec.quantity_column, spec.reported_energy_column) if column
    ]
    columns += spec.product_group_columns
    frame = read_table(folder, spec, list(dict.fromkeys(columns)))
    starts = _batch_times(frame, spec, spec.start_column, spec.start_time_column)
    ends = _batch_times(frame, spec, spec.end_column, spec.end_time_column)
    quantities = (
        parse_numbers(frame[spec.quantity_column]) if spec.quantity_column else None
    )
    reported = parse_numbers(frame[spec.reported_energy_column]) if spec.reported_energy_column else None
    batches: list[Batch] = []
    skipped = 0
    offset = first_data_row(spec)
    for index in range(len(frame)):
        machine = _text(frame[spec.machine_column].iloc[index])
        if not machine and all(not _text(frame[column].iloc[index]) for column in time_columns):
            skipped += 1
            continue
        start = None if pd.isna(starts.iloc[index]) else starts.iloc[index].to_pydatetime()
        end = None if pd.isna(ends.iloc[index]) else ends.iloc[index].to_pydatetime()
        problem = None
        if start is None or end is None:
            problem = "time_missing"
        elif end <= start:
            problem = "end_not_after_start"
        product = _text(frame[spec.product_column].iloc[index]) if spec.product_column else ""
        groups = {column: _text(frame[column].iloc[index]) for column in spec.product_group_columns}
        quantity = None
        if quantities is not None and not pd.isna(quantities.iloc[index]):
            quantity = float(quantities.iloc[index])
        batches.append(
            Batch(
                row=index + offset,
                machine=machine,
                start=start,
                end=end,
                product=product or None,
                groups={column: value for column, value in groups.items() if value},
                quantity=quantity,
                reported_energy_kwh=None
                if reported is None or pd.isna(reported.iloc[index])
                else float(reported.iloc[index]) * ENERGY_UNITS[spec.reported_energy_unit],
                problem=problem,
            )
        )
    return batches, skipped


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
        windows.append(CalendarWindow(row=index + first_data_row(spec), state=state, start=start, end=end))
    return windows, unusable


def load_site_facts(folder: Path, spec: SiteFactsSpec) -> list[SiteFact]:
    """Every answered row of the survey, in file order."""

    columns = [spec.id_column, spec.topic_column, spec.answer_column]
    columns += [column for column in (spec.question_column, spec.status_column) if column]
    frame = read_table(folder, spec, list(dict.fromkeys(columns)))
    offset = first_data_row(spec)
    facts: list[SiteFact] = []
    for index in range(len(frame)):
        answer = _text(frame[spec.answer_column].iloc[index])
        if not answer:
            continue
        row = index + offset
        status = _text(frame[spec.status_column].iloc[index]) if spec.status_column else ""
        facts.append(
            SiteFact(
                id=_text(frame[spec.id_column].iloc[index]) or f"第{row}行",
                row=row,
                topic=_text(frame[spec.topic_column].iloc[index]),
                question=_text(frame[spec.question_column].iloc[index]) or None if spec.question_column else None,
                answer=answer,
                status=status or None,
                confirmed=status in spec.confirmed_states,
            )
        )
    return facts


MAX_TREE_NODES = 100_000


def load_tree(folder: Path, spec: TreeJsonSpec | TreeTableSpec) -> list[TreeNode]:
    """Every node of the plant tree with its path of names from the top."""

    nodes: list[TreeNode] = []
    if isinstance(spec, TreeTableSpec):
        frame = read_table(folder, spec, list(dict.fromkeys([spec.id_column, spec.parent_column, spec.name_column])))
        rows = {}
        for index in range(len(frame)):
            node_id = _text(frame[spec.id_column].iloc[index])
            if node_id:
                rows[node_id] = (_text(frame[spec.name_column].iloc[index]), _text(frame[spec.parent_column].iloc[index]) or None)

        def path_of(node_id: str) -> list[str]:
            names, seen = [], set()
            current: str | None = node_id
            while current is not None and current in rows and current not in seen:
                seen.add(current)
                names.append(rows[current][0])
                current = rows[current][1]
            return names[::-1]

        for node_id, (name, parent) in rows.items():
            path = path_of(node_id)
            nodes.append(TreeNode(id=node_id, name=name, parent=parent if parent in rows else None, depth=len(path) - 1, path=path))
        return nodes
    path = safe_relative(folder, spec.file)
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputFormatError(f"{spec.file} cannot be read as JSON: {exc}") from exc
    for key in spec.nodes_path:
        try:
            data = data[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise InputFormatError(f"{spec.file}: nodes_path step {key!r} does not exist") from exc
    stack = [(item, None, []) for item in reversed(data if isinstance(data, list) else [data])]
    while stack:
        item, parent, above = stack.pop()
        if not isinstance(item, dict):
            continue
        if len(nodes) >= MAX_TREE_NODES:
            raise InputFormatError(f"{spec.file} has more than {MAX_TREE_NODES} nodes")
        node_id = _text(item.get(spec.id_key))
        name = _text(item.get(spec.name_key))
        names = [*above, name]
        if node_id:
            nodes.append(TreeNode(id=node_id, name=name, parent=parent, depth=len(above), path=names))
        children = item.get(spec.children_key) or []
        if isinstance(children, list):
            stack.extend((child, node_id or parent, names) for child in reversed(children))
    if not nodes:
        raise InputFormatError(f"{spec.file}: no node with {spec.id_key!r} under nodes_path {spec.nodes_path}")
    return nodes


def load_assets(folder: Path, spec: AssetListSpec) -> list[AssetRow]:
    columns = [column for column in (spec.code_column, spec.name_column, spec.category_column, spec.equipment_column) if column]
    frame = read_table(folder, spec, list(dict.fromkeys(columns)))
    offset = first_data_row(spec)
    rows = []

    def cell(column: str | None, index: int) -> str | None:
        return (_text(frame[column].iloc[index]) or None) if column else None

    for index in range(len(frame)):
        code, name = cell(spec.code_column, index), cell(spec.name_column, index)
        if not code and not name:
            continue
        rows.append(
            AssetRow(
                row=index + offset,
                code=code,
                name=name,
                category=cell(spec.category_column, index),
                equipment=cell(spec.equipment_column, index),
            )
        )
    return rows


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
    "Assumption",
    "Batch",
    "CalendarSpec",
    "AssetListSpec",
    "AssetRow",
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
    "SiteFact",
    "SiteFactsSpec",
    "QUANTITY_BASES",
    "TariffFileSpec",
    "TariffSchedule",
    "TariffSpec",
    "TreeJsonSpec",
    "TreeNode",
    "TreeTableSpec",
    "UNPRICED_REASONS",
    "clock_window",
    "file_sha256",
    "load_assets",
    "load_batches",
    "load_calendar",
    "load_manifest",
    "load_site_facts",
    "load_tree",
    "manifest_files",
    "normalize_meter",
    "read_meter_table",
    "read_table",
    "tariff_schedule",
    "safe_relative",
]
