"""The program's lenses: loss = actual − reference, each reference written down.

A lens is one kind of reference.  Every lens runs over every meter it applies
to and keeps its excess cell by cell on the ledger, so lenses can be laid over
each other (overlap), summed per meter (union) and compared (saturation).  A
candidate is one lens on one meter (sometimes one part of it, e.g. between
batches or with the plant stopped); it carries its excess under every
reference level the preferences list, the default level and what each level
means.  Nothing here says what is a loss: the screening rules and a person do.

Lenses (ids are stable; the screening and the workbook name them):

- ``L1`` 无需求用电: production meters with no batch registered or with the
  whole plant stopped (by power), and auxiliary meters that follow production
  while the plant is stopped: energy above the meter's idle floor (P22).
- ``L2a`` / ``L2b`` 本厂先例: per production meter and comparable group, each
  batch's processing intensity (energy in RUN ÷ quantity) and its preparation
  energy against the group's quantiles (P23, default P24).
- ``L2c`` 本厂先例·日电量: auxiliary and non-production meters, each day's
  energy against the days with the closest production energy nearby (P25–P28).
- ``L3`` 同类对标: a comparable group run on several production meters: the
  gap between a meter's own reference and the best other meter's; counted in
  totals only when a person confirmed the group (A05 peer_group).
- ``L4`` 驱动残差: days whose energy is far above the fit on production (or
  the rolling median when the meter does not follow production) (P29, P30).
- ``L5`` 批内停机: production meters below RUN inside the processing window
  while the plant is producing: energy above the idle floor.
- ``L6`` 费用结构: production energy in processing windows priced above the
  tariff's middle level: money, an upper bound, never in the energy totals.
- ``D1`` 账实不符: production meters in RUN with no batch registered: a data
  question, not a loss, never in the totals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .grid import Grid, daterange_text
from .handles import Foundation, handle_part
from .preparation import ParameterSet
from .store import read_json, write_json

ANALYSIS_DIR = "analysis"
LENS_FILE = "lenses.json"
LENS_ARRAYS = "lenses.npz"
LENSES: dict[str, tuple[str, str]] = {
    "L1": ("无需求用电", "没有生产需求时（产线没登记批次或全厂停；随生产的辅助表在全厂停时）高于底功率的电"),
    "L2a": ("本厂先例·加工单耗", "同线同组批次的运行单耗（运行档电量 ÷ 数量）高于组内参照的部分"),
    "L2b": ("本厂先例·准备段", "同线同组批次的准备段电量高于组内参照的部分"),
    "L2c": ("本厂先例·日电量", "日电量高于前后几天里产线电量最相近几天的参照的部分"),
    "L3": ("同类对标", "同一可比组在别的产线上做得更省：本线参照高于最好的别线参照的部分"),
    "L4": ("驱动残差·异常日", "日电量高出拟合值很多的天"),
    "L5": ("批内停机", "加工窗口里、全厂仍在产时，本线低于运行档时高于底功率的电"),
    "L6": ("费用结构", "加工窗口里落在高于中间档电价时段的电和多付的钱（上限，不进电量合计）"),
    "D1": ("账实不符", "没登记批次、产线却在运行档的电（数据问题，不是损耗）"),
    "R": ("人定参照规则", "偏好里人确认的参照规则（P39）"),
    "M": ("模型提议的参照", "模型交的参照规格，参照待人确认"),
}
GENERIC = ("L2c", "L4")  # say where and how much, not by which mechanism: the residual is measured against them
NOT_IN_UNION = ("L6", "D1")


def level_label(quantile: float) -> str:
    return "组内最低" if quantile == 0 else f"P{int(round(quantile * 100))}"


@dataclass
class Candidate:
    """One lens on one meter: the excess under each reference level and the cells of the default one."""

    id: str
    lens: str
    meter: str
    part: str | None
    title: str
    selection: str
    levels: list[dict[str, Any]]
    default: str
    cells: np.ndarray
    source: str = "program"
    in_union: bool = True
    money: float | None = None
    spec_id: str | None = None
    notes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    preferences: list[str] = field(default_factory=list)

    @property
    def excess(self) -> float:
        return float(self.cells.sum())

    def level(self, label: str) -> dict[str, Any] | None:
        return next((level for level in self.levels if level["label"] == label), None)

    @property
    def conservative(self) -> str:
        """The level with the smallest excess: the reference most favourable to the meter."""

        return min(self.levels, key=lambda level: level["excess_kwh"])["label"] if self.levels else self.default

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "lens": self.lens,
            "meter": self.meter,
            "part": self.part,
            "title": self.title,
            "selection": self.selection,
            "levels": self.levels,
            "default": self.default,
            "conservative": self.conservative,
            "excess_kwh": round(self.excess, 2),
            "source": self.source,
            "in_union": self.in_union,
            "money": None if self.money is None else round(self.money, 2),
            "spec_id": self.spec_id,
            "notes": self.notes,
            "details": self.details,
            "preferences": self.preferences,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], cells: np.ndarray) -> "Candidate":
        return cls(
            id=data["id"],
            lens=data["lens"],
            meter=data["meter"],
            part=data.get("part"),
            title=data["title"],
            selection=data["selection"],
            levels=data["levels"],
            default=data["default"],
            cells=cells,
            source=data.get("source", "program"),
            in_union=data.get("in_union", True),
            money=data.get("money"),
            spec_id=data.get("spec_id"),
            notes=list(data.get("notes", [])),
            details=dict(data.get("details", {})),
            preferences=list(data.get("preferences", [])),
        )


@dataclass
class LensRun:
    candidates: list[Candidate]
    examined: dict[str, np.ndarray]  # lens -> meters × cells, the cells the lens had a reference for
    notes: dict[str, Any]
    driven: dict[str, float | None]
    batches: list[dict[str, Any]] = field(default_factory=list)  # each batch of a production meter, without its cells


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None or not np.isfinite(value) else round(float(value), digits)


def _quantile(values: np.ndarray, q: float) -> float:
    return float(np.quantile(values, q)) if len(values) else float("nan")


def _spread(target: np.ndarray, energy: np.ndarray, mask: np.ndarray, amount: float) -> None:
    """Lay ``amount`` over the cells of ``mask`` in proportion to their energy."""

    if amount <= 0:
        return
    weights = np.where(mask, energy, 0.0)
    total = weights.sum()
    if total > 0:
        target += weights / total * amount
    elif mask.any():
        target[mask] += amount / mask.sum()


def _clock_table(grid: Grid, energy: np.ndarray) -> np.ndarray:
    """The meter's energy as days by clock slots (NaN where the grid has no cell)."""

    slots = np.minimum((grid.minute / (grid.step_h * 60)).astype(int), grid.cells_per_day - 1)
    table = np.full((len(grid.day_keys), grid.cells_per_day), np.nan)
    table[grid.day, slots] = energy
    return table


def _spread_by_shape(target: np.ndarray, grid: Grid, energy: np.ndarray, table: np.ndarray, day: int, peers: np.ndarray, amount: float) -> None:
    """Lay a day's excess where that day ran above its reference days at the same clock time.

    A daily lens knows how much a day was over, not when.  The excess goes
    to the clock slots where the day's energy was above the median of the
    reference days at that slot; when it was above nowhere (the excess is
    spread thin over the whole day), in proportion to energy over the day.
    """

    mask = grid.day == day
    peers = peers[peers != day]
    if amount <= 0:
        return
    if len(peers):
        with np.errstate(all="ignore"):
            reference = np.nanmedian(table[peers], axis=0)
        slots = np.minimum((grid.minute[mask] / (grid.step_h * 60)).astype(int), grid.cells_per_day - 1)
        above = np.clip(energy[mask] - np.nan_to_num(reference[slots], nan=0.0), 0.0, None)
        if above.sum() > 0:
            target[np.flatnonzero(mask)] += above / above.sum() * amount
            return
    _spread(target, energy, mask, amount)


class _Lenses:
    def __init__(self, grid: Grid, foundation: Foundation, params: ParameterSet, confirmations: list[Any]) -> None:
        self.grid = grid
        self.foundation = foundation
        self.params = params
        self.h = grid.step_h
        self.candidates: list[Candidate] = []
        self.examined: dict[str, np.ndarray] = {}
        self.notes: dict[str, Any] = {}
        self.lines = [meter_id for meter_id in grid.lines if grid.ok_of(meter_id).any()]
        self.others = [meter_id for meter_id in grid.meters if meter_id not in grid.line_index and grid.ok_of(meter_id).any()]
        self.line_daily = grid.daily(grid.line_energy())
        self.minimum_kw: dict[str, tuple[float, str]] = {}
        for entry in confirmations:
            if entry.kind == "service" and entry.result != "rejected" and entry.minimum_kw is not None:
                self.minimum_kw[entry.subject] = (float(entry.minimum_kw), entry.confirmed_by)
        self.tasks = {row["id"]: row for row in (foundation.rows("TASK") if "TASK" in foundation.tables else [])}
        self.group_status = {}
        if "GRP" in foundation.tables:
            for row in foundation.rows("GRP"):
                status = str(row.get("comparable") or "CANDIDATE")
                self.group_status[row["group"]] = status.split("（")[0]

    def blank(self) -> np.ndarray:
        return np.zeros((len(self.grid.meters), self.grid.cells), dtype=bool)

    def add(self, candidate: Candidate) -> None:
        self.candidates.append(candidate)

    # Driven by production ----------------------------------------------------
    def driven(self) -> dict[str, float | None]:
        threshold = float(self.params.value("driven_correlation"))
        self.rho: dict[str, float | None] = {}
        for meter_id in self.others:
            full = self.grid.full_days(meter_id)
            if full.sum() < 10:
                self.rho[meter_id] = None
                continue
            own = pd.Series(self.grid.daily(self.grid.energy_of(meter_id))[full])
            lines = pd.Series(self.line_daily[full])
            if own.nunique() < 2 or lines.nunique() < 2:
                self.rho[meter_id] = None
                continue
            value = own.rank().corr(lines.rank())
            self.rho[meter_id] = None if value is None or not np.isfinite(value) else float(value)
        self.driven_meters = [meter_id for meter_id, value in self.rho.items() if value is not None and value >= threshold]
        return self.rho

    def floor(self, meter_id: str, mask: np.ndarray) -> tuple[float, str]:
        if meter_id in self.minimum_kw:
            value, by = self.minimum_kw[meter_id]
            return value, f"现场必要功率 {value:g} kW（{by} 确认）"
        quantile = float(self.params.value("idle_floor_quantile", meter_id))
        power = self.grid.power_of(meter_id)[mask & self.grid.ok_of(meter_id)]
        value = _quantile(power, quantile) if len(power) else 0.0
        return value, f"底功率 {value:.2f} kW（本表{'不在运行档' if meter_id in self.grid.line_index else '全厂停'}时功率的 P{int(round(quantile * 100))}）"

    def _floor_candidate(
        self, lens: str, meter_id: str, part: str | None, title: str, selection: str, mask: np.ndarray, floor: tuple[float, str]
    ) -> Candidate:
        power = self.grid.power_of(meter_id)
        mask = mask & self.grid.ok_of(meter_id)
        value, text = floor
        at_floor = np.where(mask, np.clip(power - value, 0, None) * self.h, 0.0)
        at_zero = np.where(mask, np.clip(power, 0, None) * self.h, 0.0)
        hours = float(mask.sum()) * self.h
        return Candidate(
            id="-".join(filter(None, [lens, handle_part(meter_id), part])),
            lens=lens,
            meter=meter_id,
            part=part,
            title=title,
            selection=selection,
            levels=[
                {"label": "零", "reference": "零（这些时段本可以不用电）", "excess_kwh": round(float(at_zero.sum()), 2)},
                {"label": "底功率", "reference": text, "excess_kwh": round(float(at_floor.sum()), 2)},
            ],
            default="底功率",
            cells=at_floor,
            details={"hours": round(hours, 2), "floor_kw": _round(value, 3), "energy_kwh": round(float(at_zero.sum()), 2)},
            preferences=["P20", "P22"],
        )

    # L1 ------------------------------------------------------------------------
    def no_demand(self) -> None:
        grid = self.grid
        examined = self.blank()
        self.floors: dict[str, tuple[float, str]] = {}
        for meter_id in self.lines:
            run = grid.band_of(meter_id) == 2
            ok = grid.ok_of(meter_id)
            in_batch = grid.in_batch(meter_id)
            self.floors[meter_id] = self.floor(meter_id, ~run)
            idle = ok & ~run & (~in_batch | grid.stop)
            examined[grid.row(meter_id)] = idle
            between = idle & ~grid.stop
            stopped = idle & grid.stop
            self.add(
                self._floor_candidate(
                    "L1", meter_id, "between", "没登记批次、全厂没停时，本线不在运行档的用电", "本线没登记批次，全厂没停（按功率），本线不在运行档", between, self.floors[meter_id]
                )
            )
            self.add(
                self._floor_candidate(
                    "L1", meter_id, "stop", "全厂停时本线的用电", "全厂停（按功率），本线不在运行档；含跨停产的登记批次", stopped, self.floors[meter_id]
                )
            )
        for meter_id in self.driven_meters:
            mask = grid.stop & grid.ok_of(meter_id)
            if not mask.any():
                continue
            self.floors[meter_id] = self.floor(meter_id, grid.stop)
            examined[grid.row(meter_id)] = mask
            self.add(
                self._floor_candidate(
                    "L1", meter_id, "stop", "全厂停时这块随生产的表的用电", "全厂停（按功率）", mask, self.floors[meter_id]
                )
            )
        self.examined["L1"] = examined
        self.notes["L1"] = {
            "stop_hours": round(float(grid.stop.sum()) * self.h, 2),
            "stop_days": daterange_text(sorted({grid.day_keys[day] for day in np.unique(grid.day[grid.stop])})),
            "driven_meters": self.driven_meters,
        }

    # Batches -------------------------------------------------------------------
    def batches(self) -> None:
        """Each usable batch of a production meter: its cells, run energy, preparation energy and group."""

        grid = self.grid
        coverage = float(self.params.value("screen_min_coverage"))
        self.batch_rows: list[dict[str, Any]] = []
        for meter_id in self.lines:
            line = grid.line_index[meter_id]
            index = grid.batch[line]
            run = grid.band_of(meter_id) == 2
            processing = grid.phase_of(meter_id, "PROCESSING")
            preparing = grid.phase_of(meter_id, "PREPARING")
            energy = grid.energy_of(meter_id)
            for number in np.unique(index[index >= 0]):
                task_id = grid.task_ids[number]
                row = self.tasks.get(task_id)
                if row is None:
                    continue
                cells = index == number
                usable = (
                    row.get("kwh_per_unit") is not None
                    and (row.get("coverage") or 0) >= coverage
                    and not row.get("shared_meter")
                )
                run_cells = cells & processing & run
                prep_cells = cells & preparing
                self.batch_rows.append(
                    {
                        "task": task_id,
                        "meter": meter_id,
                        "group": row.get("group_key") or row.get("product") or "未写",
                        "quantity": row.get("quantity"),
                        "usable": usable,
                        "end": row.get("end"),
                        "run_cells": run_cells,
                        "prep_cells": prep_cells,
                        "run_kwh": float(energy[run_cells].sum()),
                        "run_hours": float(run_cells.sum()) * self.h,
                        "prep_kwh": float(energy[prep_cells].sum()),
                        "prep_hours": float(prep_cells.sum()) * self.h,
                    }
                )

    def _batch_lens(self, lens: str, key: str, cells_key: str) -> dict[tuple[str, str], dict[str, float]]:
        """L2a / L2b: each batch against its line-and-group quantiles; returns the per-group references."""

        grid = self.grid
        quantiles = [float(value) for value in self.params.value("batch_reference_quantiles")]
        default_q = float(self.params.value("batch_default_quantile"))
        if default_q not in quantiles:
            quantiles = sorted({*quantiles, default_q})
        examined = self.blank()
        references: dict[tuple[str, str], dict[str, float]] = {}
        for meter_id in self.lines:
            rows = [row for row in self.batch_rows if row["meter"] == meter_id and row["usable"] and row[key] > 0]
            groups: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                groups.setdefault(row["group"], []).append(row)
            cells = np.zeros(grid.cells)
            totals = {q: 0.0 for q in quantiles}
            group_details = []
            batch_details = []
            energy = grid.energy_of(meter_id)
            for group, members in sorted(groups.items()):
                if len(members) < 2:
                    continue
                if lens == "L2a":
                    values = np.array([row[key] / row["quantity"] for row in members])
                    weights = np.array([row["quantity"] for row in members], dtype=float)
                else:
                    values = np.array([row[key] for row in members])
                    weights = np.ones(len(members))
                refs = {q: _quantile(values, q) for q in quantiles}
                references[(meter_id, group)] = refs
                for q in quantiles:
                    totals[q] += float((np.clip(values - refs[q], 0, None) * weights).sum())
                for value, weight, row in zip(values, weights, members, strict=True):
                    examined[grid.row(meter_id)] |= row[cells_key]
                    excess = max(0.0, (value - refs[default_q]) * weight)
                    _spread(cells, energy, row[cells_key], excess)
                    if excess > 0:
                        batch_details.append(
                            {
                                "task": row["task"],
                                "group": group,
                                "end": row["end"],
                                "value": _round(value, 5),
                                "reference": _round(refs[default_q], 5),
                                "excess_kwh": _round(excess, 1),
                                "hours": _round(row["run_hours"] if lens == "L2a" else row["prep_hours"], 2),
                            }
                        )
                group_details.append(
                    {
                        "group": group,
                        "batches": len(members),
                        **{level_label(q): _round(refs[q], 5) for q in quantiles},
                        "max": _round(float(values.max()), 5),
                    }
                )
            if not group_details:
                continue
            batch_details.sort(key=lambda entry: -(entry["excess_kwh"] or 0))
            unit = "kWh/单位数量" if lens == "L2a" else "kWh/批"
            self.add(
                Candidate(
                    id=f"{lens}-{handle_part(meter_id)}",
                    lens=lens,
                    meter=meter_id,
                    part=None,
                    title=f"批次{'运行单耗' if lens == 'L2a' else '准备段电量'}高于同线同组参照",
                    selection="本线批次的" + ("加工段运行档" if lens == "L2a" else "准备段"),
                    levels=[
                        {
                            "label": level_label(q),
                            "reference": f"同线同组批次{'单耗' if lens == 'L2a' else '准备段电量'}的{level_label(q)}（{unit}，各组分别取）",
                            "excess_kwh": round(totals[q], 2),
                        }
                        for q in quantiles
                    ],
                    default=level_label(default_q),
                    cells=cells,
                    details={
                        "groups": group_details,
                        "batches_above": len(batch_details),
                        "batches": sum(entry["batches"] for entry in group_details),
                        "top_batches": batch_details[:10],
                    },
                    preferences=["P08", "P23", "P24", "P38"],
                )
            )
        self.examined[lens] = examined
        return references

    def peers(self, references: dict[tuple[str, str], dict[str, float]]) -> None:
        """L3: what the best other line's own reference says on top of this line's."""

        grid = self.grid
        quantiles = sorted({float(value) for value in self.params.value("batch_reference_quantiles")} | {float(self.params.value("batch_default_quantile"))})
        default_q = float(self.params.value("batch_default_quantile"))
        examined = self.blank()
        by_group: dict[str, list[str]] = {}
        for meter_id, group in references:
            by_group.setdefault(group, []).append(meter_id)
        groups = 0
        for group, meters in sorted(by_group.items()):
            if len(meters) < 2:
                continue
            status = self.group_status.get(group, "CANDIDATE")
            if status == "REJECTED":
                continue
            groups += 1
            for meter_id in sorted(meters):
                others = [other for other in meters if other != meter_id]
                rows = [
                    row for row in self.batch_rows if row["meter"] == meter_id and row["group"] == group and row["usable"] and row["run_kwh"] > 0
                ]
                quantity = sum(row["quantity"] for row in rows)
                totals = {}
                best = {}
                for q in quantiles:
                    peer = min(references[(other, group)][q] for other in others)
                    best[q] = (peer, min(others, key=lambda other: references[(other, group)][q]))
                    totals[q] = max(0.0, references[(meter_id, group)][q] - peer) * quantity
                cells = np.zeros(grid.cells)
                gap = max(0.0, references[(meter_id, group)][default_q] - best[default_q][0])
                energy = grid.energy_of(meter_id)
                for row in rows:
                    examined[grid.row(meter_id)] |= row["run_cells"]
                    _spread(cells, energy, row["run_cells"], gap * row["quantity"])
                confirmed = status == "CONFIRMED"
                self.add(
                    Candidate(
                        id=f"L3-{handle_part(meter_id)}-{handle_part(group)}",
                        lens="L3",
                        meter=meter_id,
                        part=group,
                        title=f"可比组 {group}：本线参照高于别线最好的参照",
                        selection=f"本线 {group} 批次的加工段运行档",
                        levels=[
                            {
                                "label": level_label(q),
                                "reference": f"别线同组单耗的{level_label(q)}里最低的（{best[q][1]}）",
                                "excess_kwh": round(totals[q], 2),
                            }
                            for q in quantiles
                        ],
                        default=level_label(default_q),
                        cells=cells,
                        in_union=confirmed,
                        notes=[] if confirmed else ["可比组没人确认（A05 peer_group），只给方向，不进合计"],
                        details={
                            "group": group,
                            "comparable": status,
                            "own_reference": _round(references[(meter_id, group)][default_q], 5),
                            "best_other": best[default_q][1],
                            "best_reference": _round(best[default_q][0], 5),
                            "quantity": _round(quantity, 2),
                            "batches": len(rows),
                        },
                        preferences=["P08", "P23", "P24"],
                    )
                )
        self.examined["L3"] = examined
        self.notes["L3"] = {"groups_on_several_lines": groups}

    # Days ----------------------------------------------------------------------
    def day_precedent(self) -> None:
        grid = self.grid
        window = int(self.params.value("day_window_days"))
        neighbours = int(self.params.value("day_neighbours"))
        quantiles = [float(value) for value in self.params.value("day_reference_quantiles")]
        default_q = float(self.params.value("day_default_quantile"))
        if default_q not in quantiles:
            quantiles = sorted({*quantiles, default_q})
        examined = self.blank()
        for meter_id in self.others:
            full = np.flatnonzero(grid.full_days(meter_id))
            if len(full) < 3:
                continue
            daily = grid.daily(grid.energy_of(meter_id))
            energy = grid.energy_of(meter_id)
            table = _clock_table(grid, energy)
            cells = np.zeros(grid.cells)
            totals = {q: 0.0 for q in quantiles}
            days = []
            for day in full:
                near = full[np.abs(full - day) <= window]
                order = near[np.argsort(np.abs(self.line_daily[near] - self.line_daily[day]), kind="stable")][:neighbours]
                for q in quantiles:
                    totals[q] += max(0.0, daily[day] - _quantile(daily[order], q))
                excess = max(0.0, daily[day] - _quantile(daily[order], default_q))
                mask = grid.day == day
                examined[grid.row(meter_id)] |= mask
                if excess > 0:
                    _spread_by_shape(cells, grid, energy, table, day, order, excess)
                    days.append((grid.day_keys[day], round(excess, 1)))
            days.sort(key=lambda entry: -entry[1])
            self.add(
                Candidate(
                    id=f"L2c-{handle_part(meter_id)}",
                    lens="L2c",
                    meter=meter_id,
                    part=None,
                    title="日电量高于产线电量相近日子的参照",
                    selection="本表数据完整的天",
                    levels=[
                        {
                            "label": level_label(q),
                            "reference": f"前后 {window} 天里产线日电量最接近的 {neighbours} 天，本表日电量的{level_label(q)}",
                            "excess_kwh": round(totals[q], 2),
                        }
                        for q in quantiles
                    ],
                    default=level_label(default_q),
                    cells=cells,
                    details={"days_above": len(days), "full_days": int(len(full)), "top_days": days[:10]},
                    preferences=["P25", "P26", "P27", "P28"],
                )
            )
        self.examined["L2c"] = examined

    def driver_residual(self) -> None:
        grid = self.grid
        sigma_times = float(self.params.value("residual_sigma"))
        share = float(self.params.value("residual_min_share"))
        examined = self.blank()
        fits = {}
        for meter_id in self.others:
            full = np.flatnonzero(grid.full_days(meter_id))
            if len(full) < 7:
                continue
            daily = grid.daily(grid.energy_of(meter_id))[full]
            if meter_id in self.driven_meters:
                slope, intercept = np.polyfit(self.line_daily[full], daily, 1)
                fit = intercept + slope * self.line_daily[full]
                fits[meter_id] = f"按产线日电量线性拟合：{intercept:.1f} + {slope:.4f} × 产线电量"
            else:
                fit = pd.Series(daily).rolling(15, center=True, min_periods=5).median().to_numpy()
                fits[meter_id] = "前后共 15 天的滚动中位"
            residual = daily - fit
            finite = np.isfinite(residual)
            if not finite.any():
                continue
            center = np.median(residual[finite])
            sigma = 1.4826 * np.median(np.abs(residual[finite] - center))
            median = float(np.median(daily))
            hit = finite & (residual > sigma_times * sigma) & (residual > share * median)
            cells = np.zeros(grid.cells)
            energy = grid.energy_of(meter_id)
            table = _clock_table(grid, energy)
            window = int(self.params.value("day_window_days"))
            quiet = full[~hit]
            days = []
            for position, day in enumerate(full):
                examined[grid.row(meter_id)] |= grid.day == day
                if hit[position]:
                    peers = quiet[np.abs(quiet - day) <= window]
                    _spread_by_shape(cells, grid, energy, table, day, peers, float(residual[position]))
                    days.append((grid.day_keys[day], round(float(residual[position]), 1)))
            total = float(cells.sum())
            self.add(
                Candidate(
                    id=f"L4-{handle_part(meter_id)}",
                    lens="L4",
                    meter=meter_id,
                    part=None,
                    title="异常日：日电量高出拟合值很多",
                    selection="本表数据完整的天",
                    levels=[{"label": "拟合值", "reference": fits[meter_id] + f"；残差超过 {sigma_times:g} 倍稳健标准差且超过日中位电量的 {share:g} 倍", "excess_kwh": round(total, 2)}],
                    default="拟合值",
                    cells=cells,
                    details={"days": days, "sigma_kwh": _round(float(sigma), 1), "median_day_kwh": _round(median, 1), "fit": fits[meter_id]},
                    preferences=["P21", "P29", "P30"],
                )
            )
        self.examined["L4"] = examined

    # Shapes and money ----------------------------------------------------------
    def in_batch_stops(self) -> None:
        grid = self.grid
        examined = self.blank()
        for meter_id in self.lines:
            processing = grid.phase_of(meter_id, "PROCESSING")
            examined[grid.row(meter_id)] = processing & grid.ok_of(meter_id)
            mask = processing & (grid.band_of(meter_id) != 2) & ~grid.stop
            self.add(
                self._floor_candidate(
                    "L5", meter_id, None, "加工窗口里本线停着、全厂仍在产的用电", "本线加工段，不在运行档，全厂没停（按功率）", mask, self.floors[meter_id]
                )
            )
        self.examined["L5"] = examined

    def tariff(self) -> None:
        grid = self.grid
        if not grid.tariff_names:
            return
        examined = self.blank()
        dear = np.isfinite(grid.price) & np.isfinite(grid.reference_price) & (grid.price > grid.reference_price)
        for meter_id in self.lines:
            processing = grid.phase_of(meter_id, "PROCESSING") & grid.ok_of(meter_id)
            examined[grid.row(meter_id)] = processing
            mask = processing & dear
            energy = grid.energy_of(meter_id)
            cells = np.where(mask, energy, 0.0)
            money = float((cells * np.nan_to_num(grid.price - grid.reference_price)).sum())
            names = sorted({grid.tariff_names[code] for code in np.unique(grid.tariff[mask]) if code >= 0})
            self.add(
                Candidate(
                    id=f"L6-{handle_part(meter_id)}",
                    lens="L6",
                    meter=meter_id,
                    part=None,
                    title="加工落在高价时段：多付的电费（上限）",
                    selection="本线加工段，电价高于中间档的时段（" + ("、".join(names) or "电价表里没有这样的时段") + "）",
                    levels=[{"label": "中间档电价", "reference": "按时长加权的中位电价；钱是上限，挪不挪要看排产约束", "excess_kwh": round(float(cells.sum()), 2)}],
                    default="中间档电价",
                    cells=cells,
                    in_union=False,
                    money=money,
                    notes=["只是上限：能不能挪到低价时段要看交货、换线等排产约束"],
                    details={"periods": names, "currency": grid.currency},
                    preferences=[],
                )
            )
        self.examined["L6"] = examined

    def unregistered_run(self) -> None:
        grid = self.grid
        for meter_id in self.lines:
            outside = grid.phase_of(meter_id, "BETWEEN") | grid.phase_of(meter_id, "OUTSIDE")
            mask = outside & (grid.band_of(meter_id) == 2) & grid.ok_of(meter_id)
            cells = np.where(mask, grid.energy_of(meter_id), 0.0)
            days = sorted({grid.day_keys[day] for day in np.unique(grid.day[mask])})
            self.add(
                Candidate(
                    id=f"D1-{handle_part(meter_id)}",
                    lens="D1",
                    meter=meter_id,
                    part=None,
                    title="没登记批次、本线却在运行档",
                    selection="本线批次之外，运行档",
                    levels=[{"label": "零", "reference": "零：没登记批次就不该到运行档（可能是漏登、试机或登记时间不准）", "excess_kwh": round(float(cells.sum()), 2)}],
                    default="零",
                    cells=cells,
                    in_union=False,
                    notes=["数据问题，不是损耗：生产记录和电表对不上"],
                    details={"hours": round(float(mask.sum()) * self.h, 2), "days": daterange_text(days)},
                    preferences=[],
                )
            )


def run_lenses(grid: Grid, foundation: Foundation, params: ParameterSet, confirmations: list[Any]) -> LensRun:
    lenses = _Lenses(grid, foundation, params, confirmations)
    driven = lenses.driven()
    lenses.no_demand()
    lenses.batches()
    references = lenses._batch_lens("L2a", "run_kwh", "run_cells")
    lenses._batch_lens("L2b", "prep_kwh", "prep_cells")
    lenses.peers(references)
    lenses.day_precedent()
    lenses.driver_residual()
    lenses.in_batch_stops()
    lenses.tariff()
    lenses.unregistered_run()
    batches = [{key: value for key, value in row.items() if not key.endswith("_cells")} for row in lenses.batch_rows]
    return LensRun(lenses.candidates, lenses.examined, lenses.notes, driven, batches)


def save_lenses(directory: Path, run: LensRun) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    arrays = {f"c:{candidate.id}": candidate.cells.astype(np.float32) for candidate in run.candidates}
    arrays.update({f"e:{lens}": mask for lens, mask in run.examined.items()})
    np.savez_compressed(directory / LENS_ARRAYS, **arrays)
    write_json(
        directory / LENS_FILE,
        {
            "candidates": [candidate.as_dict() for candidate in run.candidates],
            "notes": run.notes,
            "driven": run.driven,
            "batches": run.batches,
            "lenses": {lens: {"name": name, "reference": text} for lens, (name, text) in LENSES.items()},
        },
    )


def load_lenses(directory: Path) -> LensRun:
    data = read_json(directory / LENS_FILE)
    arrays = np.load(directory / LENS_ARRAYS, allow_pickle=False)
    candidates = [Candidate.from_dict(entry, arrays[f"c:{entry['id']}"].astype(float)) for entry in data["candidates"]]
    examined = {name[2:]: arrays[name].astype(bool) for name in arrays.files if name.startswith("e:")}
    return LensRun(candidates, examined, data["notes"], data["driven"], data.get("batches", []))


__all__ = [
    "ANALYSIS_DIR",
    "GENERIC",
    "LENSES",
    "NOT_IN_UNION",
    "Candidate",
    "LensRun",
    "level_label",
    "load_lenses",
    "run_lenses",
    "save_lenses",
]
