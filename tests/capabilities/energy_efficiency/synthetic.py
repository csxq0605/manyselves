"""A small made-up site for the energy-efficiency tests.

Nothing here comes from a customer.  Two weeks, three meters of three kinds:

- ``AC1`` air compressor, 15-minute interval energy (timestamp at the end):
  40 kW on weekdays 08:00-20:00, 12 kW at night and all weekend;
- ``IM1`` moulding machine, 15-minute cumulative readings: 30 kW while a
  batch runs, 8 kW between batches, 1 kW otherwise; the meter is reset once;
- ``LT1`` lighting, 5-minute instant power: 6 kW from 07:00 to 22:00 every
  day, weekends too.

Production batches run on weekdays 08:00-11:30 and 12:30-17:00; the
calendar marks weekends as non_production.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import yaml

START = datetime(2026, 3, 2)  # a Monday
DAYS = 14
END = START + timedelta(days=DAYS)
RESET_AT = datetime(2026, 3, 9, 3, 0)


def _weekday(moment: datetime) -> bool:
    return moment.weekday() < 5


def _batches() -> list[tuple[datetime, datetime]]:
    batches = []
    for offset in range(DAYS):
        day = START + timedelta(days=offset)
        if not _weekday(day):
            continue
        batches.append((day.replace(hour=8), day.replace(hour=11, minute=30)))
        batches.append((day.replace(hour=12, minute=30), day.replace(hour=17)))
    return batches


def compressor_kw(moment: datetime) -> float:
    if _weekday(moment) and 8 <= moment.hour < 20:
        return 40.0
    return 12.0


def moulding_kw(moment: datetime) -> float:
    for begin, end in _batches():
        if begin <= moment < end:
            return 30.0
    day = moment.replace(hour=0, minute=0)
    if _weekday(moment) and day.replace(hour=8) <= moment < day.replace(hour=17):
        return 8.0
    return 1.0


def lighting_kw(moment: datetime) -> float:
    return 6.0 if 7 <= moment.hour < 22 else 0.0


def _write_csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def write_site(folder: Path, *, prechecks: bool = True) -> Path:
    """Write the made-up site into ``folder`` and return it."""

    folder.mkdir(parents=True, exist_ok=True)
    step = timedelta(minutes=15)
    rows = []
    moment = START
    while moment < END:
        # The timestamp marks the end of the interval.
        rows.append([(moment + step).strftime("%Y-%m-%d %H:%M"), round(compressor_kw(moment) * 0.25, 3)])
        moment += step
    _write_csv(folder / "compressor.csv", ["时间", "电量kWh"], rows)

    rows = []
    reading = 5000.0
    moment = START
    rows.append([moment.strftime("%Y-%m-%d %H:%M"), reading])
    while moment < END:
        reading += moulding_kw(moment) * 0.25
        moment += step
        if moment == RESET_AT:
            reading = 0.0
        rows.append([moment.strftime("%Y-%m-%d %H:%M"), round(reading, 3)])
    _write_csv(folder / "moulding.csv", ["时间", "读数kWh"], rows)

    rows = []
    moment = START
    while moment < END:
        rows.append([moment.strftime("%Y-%m-%d %H:%M"), lighting_kw(moment)])
        moment += timedelta(minutes=5)
    _write_csv(folder / "lighting.csv", ["时间", "功率kW"], rows)

    _write_csv(
        folder / "production.csv",
        ["机台", "开始", "结束", "产品", "数量"],
        [
            ["IM-01", begin.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M"), "壳体", 100]
            for begin, end in _batches()
        ],
    )
    calendar = []
    for offset in range(DAYS):
        day = START + timedelta(days=offset)
        state = "production" if _weekday(day) else "non_production"
        calendar.append(
            [day.strftime("%Y-%m-%d %H:%M"), (day + timedelta(days=1)).strftime("%Y-%m-%d %H:%M"), state]
        )
    _write_csv(folder / "calendar.csv", ["开始", "结束", "状态"], calendar)

    manifest = {
        "site": {"name": "示例工厂"},
        "period": {"start": START.isoformat(sep=" "), "end": END.isoformat(sep=" ")},
        "meters": [
            {
                "id": "AC1",
                "name": "空压机",
                "file": "compressor.csv",
                "time_column": "时间",
                "value_column": "电量kWh",
                "semantics": "interval_energy",
                "unit": "kWh",
                "interval_minutes": 15,
                "timestamp_marks": "end",
                "group": "动力站",
            },
            {
                "id": "IM1",
                "name": "注塑机 1",
                "file": "moulding.csv",
                "time_column": "时间",
                "value_column": "读数kWh",
                "semantics": "cumulative_energy",
                "unit": "kWh",
                "interval_minutes": 15,
                "group": "注塑车间",
                "production_machines": ["IM-01"],
            },
            {
                "id": "LT1",
                "name": "车间照明",
                "file": "lighting.csv",
                "time_column": "时间",
                "value_column": "功率kW",
                "semantics": "instant_power",
                "unit": "kW",
                "interval_minutes": 5,
                "group": "注塑车间",
            },
        ],
        "production": {
            "file": "production.csv",
            "machine_column": "机台",
            "start_column": "开始",
            "end_column": "结束",
            "product_column": "产品",
            "quantity_column": "数量",
        },
        "calendar": {
            "file": "calendar.csv",
            "start_column": "开始",
            "end_column": "结束",
            "state_column": "状态",
        },
        "tariff": {
            "periods": [
                {"name": "峰", "start": "08:00", "end": "22:00", "price": 1.0},
                {"name": "谷", "start": "22:00", "end": "08:00", "price": 0.4},
            ]
        },
    }
    if prechecks:
        manifest["prechecks"] = [
            {
                "method": "daily-clock-window",
                "meters": ["AC1"],
                "params": {"start_time": "22:00", "end_time": "06:00", "min_avg_kw": 5},
            }
        ]
    (folder / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return folder


__all__ = ["END", "RESET_AT", "START", "compressor_kw", "lighting_kw", "moulding_kw", "write_site"]
