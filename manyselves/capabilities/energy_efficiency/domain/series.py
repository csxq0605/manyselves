"""Measure one meter's normalized data over time windows.

A window counts only the records that lie wholly inside it.  An interval
that crosses the window's edge is left out and counted as ``partial``; its
energy is never split by time.  Coverage is the share of the window's time
that the counted records cover.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import numpy as np
import pandas as pd

Kind = Literal["energy", "avg_power", "instant"]


@dataclass(frozen=True)
class WindowMeasure:
    start: datetime
    end: datetime
    energy_kwh: float | None
    avg_kw: float | None
    covered_minutes: float
    coverage: float
    records: int
    partial: int
    first_index: int
    last_index: int

    @property
    def window_minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60

    def as_dict(self) -> dict[str, object]:
        return {
            "start": _iso(self.start),
            "end": _iso(self.end),
            "energy_kwh": _round(self.energy_kwh),
            "avg_kw": _round(self.avg_kw),
            "coverage": round(self.coverage, 4),
            "records": self.records,
            "partial_excluded": self.partial,
        }


def _iso(value: datetime) -> str:
    return pd.Timestamp(value).isoformat(sep=" ")


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


class MeterSeries:
    """One meter's records, sorted and without overlaps."""

    def __init__(self, meter_id: str, kind: Kind, frame: pd.DataFrame, interval_minutes: int):
        self.meter_id = meter_id
        self.kind: Kind = kind
        self.interval_minutes = interval_minutes
        if kind == "instant":
            self.starts = frame["time"].to_numpy(dtype="datetime64[ns]")
            self.ends = self.starts
            self.values = frame["kw"].to_numpy(dtype=float)
        else:
            self.starts = frame["start"].to_numpy(dtype="datetime64[ns]")
            self.ends = frame["end"].to_numpy(dtype="datetime64[ns]")
            column = "kwh" if kind == "energy" else "avg_kw"
            self.values = frame[column].to_numpy(dtype=float)
        self.durations_h = (self.ends - self.starts).astype("timedelta64[s]").astype(float) / 3600

    @property
    def has_energy(self) -> bool:
        return self.kind == "energy"

    def __len__(self) -> int:
        return len(self.values)

    def span(self) -> tuple[datetime, datetime] | None:
        if not len(self):
            return None
        return (
            pd.Timestamp(self.starts[0]).to_pydatetime(),
            pd.Timestamp(self.ends[-1]).to_pydatetime(),
        )

    def inside(self, start: datetime, end: datetime) -> tuple[int, int, int]:
        """Index range of the records wholly inside [start, end), and the partial count."""

        a = np.datetime64(pd.Timestamp(start), "ns")
        b = np.datetime64(pd.Timestamp(end), "ns")
        if self.kind == "instant":
            lo = int(np.searchsorted(self.starts, a, "left"))
            hi = int(np.searchsorted(self.starts, b, "left"))
            return lo, hi, 0
        lo = int(np.searchsorted(self.starts, a, "left"))
        hi = int(np.searchsorted(self.ends, b, "right"))
        hi = max(hi, lo)
        # Records that overlap the window but are not wholly inside it.
        touch_lo = int(np.searchsorted(self.ends, a, "right"))
        touch_hi = int(np.searchsorted(self.starts, b, "left"))
        partial = max(0, touch_hi - touch_lo - (hi - lo))
        return lo, hi, partial

    def measure(self, start: datetime, end: datetime) -> WindowMeasure:
        lo, hi, partial = self.inside(start, end)
        window_minutes = (end - start).total_seconds() / 60
        count = hi - lo
        energy: float | None = None
        avg: float | None = None
        if self.kind == "instant":
            covered = min(window_minutes, count * self.interval_minutes)
            if count:
                avg = float(self.values[lo:hi].mean())
        else:
            hours = float(self.durations_h[lo:hi].sum())
            covered = hours * 60
            if self.kind == "energy":
                energy = float(self.values[lo:hi].sum()) if count else None
                avg = energy / hours if count and hours > 0 else None
            elif count and hours > 0:
                avg = float((self.values[lo:hi] * self.durations_h[lo:hi]).sum() / hours)
        coverage = covered / window_minutes if window_minutes > 0 else 0.0
        return WindowMeasure(
            start=start,
            end=end,
            energy_kwh=energy,
            avg_kw=avg,
            covered_minutes=covered,
            coverage=min(1.0, coverage),
            records=count,
            partial=partial,
            first_index=lo,
            last_index=hi,
        )

    def measure_many(self, windows: list[tuple[datetime, datetime]]) -> list[WindowMeasure]:
        return [self.measure(start, end) for start, end in windows]

    def interval_powers(self, start: datetime, end: datetime) -> np.ndarray:
        """Average power of each record wholly inside the window (kW)."""

        lo, hi, _partial = self.inside(start, end)
        if self.kind == "energy":
            hours = self.durations_h[lo:hi]
            with np.errstate(divide="ignore", invalid="ignore"):
                powers = np.where(hours > 0, self.values[lo:hi] / hours, np.nan)
            return powers[~np.isnan(powers)]
        return self.values[lo:hi]

    def index_set(self, measure: WindowMeasure) -> range:
        return range(measure.first_index, measure.last_index)

    def energy_of(self, indexes: set[int]) -> float | None:
        if not self.has_energy:
            return None
        if not indexes:
            return 0.0
        return float(self.values[sorted(indexes)].sum())


def days_between(start: datetime, end: datetime) -> list[datetime]:
    first = datetime.combine(start.date(), datetime.min.time())
    days = []
    current = first
    while current < end:
        days.append(current)
        current += timedelta(days=1)
    return days


def day_windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    return [(day, day + timedelta(days=1)) for day in days_between(start, end)]


__all__ = ["Kind", "MeterSeries", "WindowMeasure", "day_windows", "days_between"]
