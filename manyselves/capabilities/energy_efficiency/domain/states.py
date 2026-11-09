"""The state axis: what production was doing at each moment (knowledge base 3.4.2).

Built only from the production records and the machines a meter is mapped
to.  For every moment of the data period the plant is in one state:

- ``no_record``: outside the time the production records cover;
- ``unknown``: a batch whose start or end is missing may be running;
- ``plant_running``: some machine has a batch;
- ``plant_stopped``: no machine has a batch.

A meter mapped to machines has one more state, ``own_running`` (one of its
own machines has a batch); ``plant_running`` then means its own machines
are idle while another machine runs, and a missing time of one of its own
machines makes the moment ``unknown`` for it.  The states are facts about
the records, never about a loss: a stopped plant may still need its
utilities.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from .inputs import Batch
from .series import MeterSeries

STATES: dict[str, str] = {
    "own_running": "本表的机台在生产",
    "plant_running": "有机台在生产（本表挂了机台时，指本表机台没生产、别的机台在生产）",
    "plant_stopped": "全厂没有机台在生产",
    "unknown": "有批次缺开始或结束时间，判断不了",
    "no_record": "生产记录覆盖的时间之外",
}
STATE_ORDER = tuple(STATES)
# Coverage of the production records: the first known batch time to the last.
COVERAGE_NOTE = "生产记录覆盖期从最早的批次时间到最晚的批次时间；之外的时间无从判断有没有生产"

Span = tuple[datetime, datetime]


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    running: frozenset[str]
    unknown: frozenset[str]
    recorded: bool

    def state_for(self, machines: frozenset[str]) -> str:
        if not self.recorded:
            return "no_record"
        if machines:
            if self.running & machines:
                return "own_running"
            if self.unknown & machines:
                return "unknown"
        if self.running:
            return "plant_running"
        if self.unknown:
            return "unknown"
        return "plant_stopped"


def _unknown_spans(batches: list[Batch], coverage: Span) -> tuple[list[tuple[str, Span]], int]:
    """Where a batch with a missing or wrong time may be running, and how many cannot be placed at all.

    A batch with only a start may run until the machine's next known batch
    start; one with only an end, from the machine's last known end before it.
    A batch whose end is not after its start may run anywhere between the
    two times.
    """

    spans: list[tuple[str, Span]] = []
    unplaced = 0
    low, high = coverage
    for batch in batches:
        if batch.problem is None:
            continue
        own = [other for other in batches if other.machine == batch.machine and other is not batch]
        if batch.start is not None and batch.end is not None:
            first, last = min(batch.start, batch.end), max(batch.start, batch.end)
            spans.append((batch.machine, (first, max(last, first))))
        elif batch.start is not None:
            later = [other.start for other in own if other.start is not None and other.start > batch.start]
            spans.append((batch.machine, (batch.start, min(later) if later else high)))
        elif batch.end is not None:
            earlier = [other.end for other in own if other.end is not None and other.end < batch.end]
            spans.append((batch.machine, (max(earlier) if earlier else low, batch.end)))
        else:
            unplaced += 1
    return [(machine, span) for machine, span in spans if span[1] > span[0]], unplaced


def coverage_of(batches: list[Batch]) -> Span | None:
    times = [moment for batch in batches for moment in (batch.start, batch.end) if moment is not None]
    if not times:
        return None
    return min(times), max(times)


def segments(batches: list[Batch], period: Span) -> tuple[list[Segment], dict[str, Any]]:
    """The period cut where any machine starts or stops, with what runs in each piece."""

    start, end = period
    coverage = coverage_of(batches)
    running = [
        (batch.machine, (batch.start, batch.end))
        for batch in batches
        if batch.problem is None and batch.start is not None and batch.end is not None
    ]
    unknown, unplaced = _unknown_spans(batches, coverage) if coverage else ([], 0)
    cuts = {start, end}
    if coverage:
        cuts.update(moment for moment in coverage if start < moment < end)
    for _machine, (low, high) in running + unknown:
        cuts.update(moment for moment in (low, high) if start < moment < end)
    points = sorted(cuts)
    running_sets: list[set[str]] = [set() for _ in points[1:]]
    unknown_sets: list[set[str]] = [set() for _ in points[1:]]
    for target, spans in ((running_sets, running), (unknown_sets, unknown)):
        for machine, (low, high) in spans:
            # Pieces wholly inside the span: every cut inside it is a point.
            first = bisect.bisect_left(points, max(low, start))
            last = bisect.bisect_left(points, min(high, end))
            for number in range(first, min(last, len(target))):
                target[number].add(machine)
    pieces = [
        Segment(
            start=low,
            end=high,
            running=frozenset(running_sets[number]),
            unknown=frozenset(unknown_sets[number]),
            recorded=bool(coverage and coverage[0] <= low and high <= coverage[1]),
        )
        for number, (low, high) in enumerate(zip(points, points[1:], strict=False))
    ]
    meta = {
        "coverage": {"start": coverage[0], "end": coverage[1]} if coverage else None,
        "unplaced_batches": unplaced,
        "uncertain_batches": len(unknown),
    }
    return pieces, meta


def state_windows(pieces: list[Segment], machines: list[str]) -> dict[str, list[Span]]:
    """Each state's stretches for a meter with these machines (touching pieces of one state joined)."""

    own = frozenset(machines)
    windows: dict[str, list[Span]] = {state: [] for state in STATE_ORDER}
    for piece in pieces:
        state = piece.state_for(own)
        stretch = windows[state]
        if stretch and stretch[-1][1] == piece.start:
            stretch[-1] = (stretch[-1][0], piece.end)
        else:
            stretch.append((piece.start, piece.end))
    return windows


def meter_states(series: MeterSeries, pieces: list[Segment], machines: list[str]) -> list[dict[str, Any]]:
    """Hours, covered hours and energy of one meter in each state.

    Each record goes to the state at its middle, so a record across a
    change of state is counted once; time with no record stays uncovered.
    """

    own = frozenset(machines)
    hours = {state: 0.0 for state in STATE_ORDER}
    for piece in pieces:
        hours[piece.state_for(own)] += (piece.end - piece.start).total_seconds() / 3600
    covered = {state: 0.0 for state in STATE_ORDER}
    energy = {state: 0.0 for state in STATE_ORDER}
    power_sum = {state: 0.0 for state in STATE_ORDER}
    counts = {state: 0 for state in STATE_ORDER}
    if len(series) and pieces:
        starts = np.array([np.datetime64(pd.Timestamp(piece.start), "ns") for piece in pieces])
        if series.kind == "instant":
            middles = series.starts
            lengths = np.full(len(series), series.interval_minutes / 60)
        else:
            middles = series.starts + (series.ends - series.starts) / 2
            lengths = series.durations_h
        places = np.searchsorted(starts, middles, "right") - 1
        names = [piece.state_for(own) for piece in pieces]
        last_end = np.datetime64(pd.Timestamp(pieces[-1].end), "ns")
        for index, place in enumerate(places):
            if place < 0 or middles[index] >= last_end:
                continue
            state = names[place]
            covered[state] += float(lengths[index])
            counts[state] += 1
            if series.has_energy:
                energy[state] += float(series.values[index])
            else:
                power_sum[state] += float(series.values[index])
    total = sum(energy.values()) if series.has_energy else None
    rows = []
    for state in STATE_ORDER:
        if hours[state] <= 0:
            continue
        kwh = energy[state] if series.has_energy and counts[state] else None
        if series.has_energy:
            avg = kwh / covered[state] if kwh is not None and covered[state] > 0 else None
        else:
            avg = power_sum[state] / counts[state] if counts[state] else None
        rows.append(
            {
                "state": state,
                "hours": round(hours[state], 2),
                "covered_hours": round(covered[state], 2),
                "energy_kwh": round(kwh, 1) if kwh is not None else None,
                "share_of_energy": round(kwh / total, 4) if kwh is not None and total else None,
                "avg_kw": round(avg, 2) if avg is not None else None,
            }
        )
    return rows


def plant_summary(pieces: list[Segment]) -> dict[str, Any]:
    """Hours of the plant in each state, and its stopped stretches."""

    windows = state_windows(pieces, [])
    hours = {
        state: round(sum((b - a).total_seconds() for a, b in spans) / 3600, 2)
        for state, spans in windows.items()
        if spans
    }
    stopped = windows["plant_stopped"]
    lengths = sorted(((b - a).total_seconds() / 3600 for a, b in stopped), reverse=True)
    return {
        "hours": hours,
        "stopped_stretches": len(stopped),
        "longest_stopped_hours": [round(value, 2) for value in lengths[:5]],
    }


def state_label(state: str, has_machines: bool) -> str:
    if state == "plant_running":
        return "本表机台没生产、别的机台在生产" if has_machines else "有机台在生产"
    return STATES[state]


def states_text(rows: list[dict[str, Any]], unit_kind: str, has_machines: bool = False) -> str:
    """The meter's state table for the profile text."""

    if not rows:
        return "状态轴：没有生产记录，分不出状态。"
    lines = ["状态轴（按生产记录；每条记录按中点归到一个状态）："]
    for row in rows:
        value = (
            f"电量 {row['energy_kwh']} kWh（占本表 {row['share_of_energy']:.1%}）"
            if unit_kind == "energy" and row["energy_kwh"] is not None and row["share_of_energy"] is not None
            else "无电量"
        )
        lines.append(
            f"- {row['state']}（{state_label(row['state'], has_machines)}）：{row['hours']} 小时，有记录 {row['covered_hours']} 小时，"
            f"{value}，平均 {row['avg_kw']} kW"
        )
    return "\n".join(lines)


__all__ = [
    "COVERAGE_NOTE",
    "STATES",
    "STATE_ORDER",
    "Segment",
    "coverage_of",
    "meter_states",
    "plant_summary",
    "segments",
    "state_label",
    "state_windows",
    "states_text",
]
