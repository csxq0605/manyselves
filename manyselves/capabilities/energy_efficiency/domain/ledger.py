"""The candidate ledger: lenses and specs laid over each other, screened, and measured for completeness.

``compose`` rebuilds it from the run's files every time (the lenses, the
specs the model handed in so far, the preferences), so each step sees the
same numbers for the same inputs:

- overlap: candidates on the same meter that claim the same cells, with the
  energy they share, and overlap groups;
- union: per meter and cell, the most any lens says is excess (the batch
  precedent and the peer gap add, since the peer gap is defined on top);
- residual: excess the generic lenses (day precedent, driver residual) see
  that no located lens accounts for, less what a person's residual rule
  explains; the model's explanations are listed, not subtracted;
- coverage: per meter, energy no lens looked at, looked at once, twice;
- triggers: where the model must look (plant stopped without the meter
  dropping, comparable groups nobody confirmed, residual unrelated to
  production); every system also gets one look regardless;
- saturation: lenses added greedily by the excess they add, and how many
  meter-days only one lens finds (its independent discoveries);
- screening: the preference rules on the computed values (``screen``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .foundation import load_foundation
from .grid import Grid, load_grid
from .handles import Foundation, handle_part
from .lenses import ANALYSIS_DIR, GENERIC, LENSES, Candidate, LensRun, load_lenses
from .preparation import ParameterSet
from .screen import INVALID, PENDING, VALID, ScreenContext, screen
from .specs import (
    ReferenceSpec,
    ResidualSpec,
    parse_spec,
    preference_specs,
    reference_candidates,
    residual_mask,
)
from .store import SiteData, read_json, write_json

SPECS_FILE = "specs.json"
ROUNDS_FILE = "residual-rounds.json"
LEDGER_FILE = "ledger.json"
OTHER_SYSTEM = "其他"


def system_of(grid: Grid, meter_id: str) -> str:
    return grid.groups.get(meter_id) or OTHER_SYSTEM


def systems(grid: Grid) -> dict[str, list[str]]:
    found: dict[str, list[str]] = defaultdict(list)
    for meter_id in grid.meters:
        if grid.ok_of(meter_id).any():
            found[system_of(grid, meter_id)].append(meter_id)
    return dict(found)


def union_of(grid: Grid, candidates: list[Candidate]) -> np.ndarray:
    """Per meter and cell: the batch precedent and the peer gap add; every other lens takes the max."""

    union = np.zeros((len(grid.meters), grid.cells))
    additive = np.zeros((len(grid.meters), grid.cells))
    for candidate in candidates:
        if not candidate.in_union:
            continue
        row = grid.row(candidate.meter)
        if candidate.lens in ("L2a", "L3"):
            additive[row] += candidate.cells
        else:
            np.maximum(union[row], candidate.cells, out=union[row])
    return np.maximum(union, additive)


@dataclass
class Ledger:
    grid: Grid
    lens_run: LensRun
    params: ParameterSet
    candidates: list[Candidate]
    screening: dict[str, dict[str, Any]]
    overlaps: dict[str, list[dict[str, Any]]]
    overlap_group: dict[str, str]
    coverage: list[dict[str, Any]]
    residual: np.ndarray
    residual_rule: np.ndarray
    residual_model: np.ndarray
    slices: list[dict[str, Any]]
    triggers: list[dict[str, Any]]
    saturation: dict[str, Any]
    specs: list[dict[str, Any]]
    problems: list[str] = field(default_factory=list)
    rounds: list[dict[str, Any]] = field(default_factory=list)

    @property
    def by_id(self) -> dict[str, Candidate]:
        return {candidate.id: candidate for candidate in self.candidates}

    def residual_total(self, open_only: bool = True) -> float:
        return float((self.residual if open_only else self.residual + self.residual_rule).sum())

    def found_total(self) -> float:
        return float(union_of(self.grid, self.candidates).sum())

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = defaultdict(int)
        for entry in self.screening.values():
            counts[entry["result"]] += 1
        return {
            "candidates": len(self.candidates),
            "valid": counts[VALID],
            "pending": counts[PENDING],
            "invalid": counts[INVALID],
            "found_kwh": round(self.found_total(), 1),
            "residual_open_kwh": round(self.residual_total(), 1),
            "residual_model_kwh": round(float((self.residual * self.residual_model).sum()), 1),
            "triggers": len(self.triggers),
            "specs": len(self.specs),
        }


def _overlaps(grid: Grid, candidates: list[Candidate]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    by_meter: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.excess > 0:
            by_meter[candidate.meter].append(candidate)
    pairs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    parent = {candidate.id: candidate.id for candidate in candidates}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for members in by_meter.values():
        for position, first in enumerate(members):
            for second in members[position + 1 :]:
                shared = float(np.minimum(first.cells, second.cells).sum())
                if shared < 1:
                    continue
                pairs[first.id].append({"id": second.id, "kwh": round(shared, 1)})
                pairs[second.id].append({"id": first.id, "kwh": round(shared, 1)})
                if shared >= 0.1 * min(first.excess, second.excess):
                    parent[find(first.id)] = find(second.id)
    groups: dict[str, list[str]] = defaultdict(list)
    for candidate in candidates:
        groups[find(candidate.id)].append(candidate.id)
    names = {}
    number = 0
    for members in sorted(groups.values(), key=lambda ids: ids[0]):
        if len(members) < 2:
            continue
        number += 1
        for member in members:
            names[member] = f"G{number}"
    for entries in pairs.values():
        entries.sort(key=lambda entry: -entry["kwh"])
    return dict(pairs), names


def _week(grid: Grid) -> tuple[np.ndarray, list[str]]:
    days = np.array([np.datetime64(key) for key in grid.day_keys])
    monday = days - ((days.astype("datetime64[D]").view("int64") - 4) % 7).astype("timedelta64[D]")
    keys = sorted({str(day) for day in monday})
    index = {key: number for number, key in enumerate(keys)}
    return np.array([index[str(monday[day])] for day in grid.day]), keys


def _saturation(grid: Grid, candidates: list[Candidate], params: ParameterSet) -> dict[str, Any]:
    members: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.in_union and candidate.excess > 0:
            members[candidate.lens].append(candidate)
    order: list[dict[str, Any]] = []
    chosen: list[str] = []
    left = sorted(members)
    base = 0.0
    while left:
        gains = {lens: float(union_of(grid, [c for name in [*chosen, lens] for c in members[name]]).sum()) - base for lens in left}
        lens = max(gains, key=lambda name: gains[name])
        chosen.append(lens)
        left.remove(lens)
        base += gains[lens]
        order.append({"lens": lens, "name": LENSES.get(lens, (lens, ""))[0], "added_kwh": round(gains[lens], 1), "total_kwh": round(base, 1)})
    share = float(params.value("discovery_min_share"))
    found: dict[str, np.ndarray] = {}
    for lens, group in members.items():
        daily = np.zeros((len(grid.meters), len(grid.day_keys)))
        for candidate in group:
            daily[grid.row(candidate.meter)] += grid.daily(candidate.cells)
        found[lens] = daily
    meter_daily = np.vstack([grid.daily(grid.energy[row]) for row in range(len(grid.meters))]) if grid.meters else np.zeros((0, 0))
    hits = {lens: (daily > 0) & (daily >= share * meter_daily) for lens, daily in found.items()}
    discovery = []
    for lens, hit in hits.items():
        others = np.zeros_like(hit)
        for other, other_hit in hits.items():
            if other != lens:
                others |= other_hit
        alone = hit & ~others
        discovery.append(
            {
                "lens": lens,
                "name": LENSES.get(lens, (lens, ""))[0],
                "slices": int(hit.sum()),
                "independent_slices": int(alone.sum()),
                "independent_kwh": round(float(found[lens][alone].sum()), 1),
            }
        )
    discovery.sort(key=lambda entry: -entry["independent_slices"])
    return {"order": order, "discovery": discovery, "slice": "一块表的一天", "min_share": share}


def load_spec_records(site: SiteData) -> list[dict[str, Any]]:
    path = site.path(f"{ANALYSIS_DIR}/{SPECS_FILE}")
    return read_json(path)["specs"] if path.is_file() else []


def save_spec_records(site: SiteData, records: list[dict[str, Any]]) -> None:
    write_json(site.path(f"{ANALYSIS_DIR}/{SPECS_FILE}"), {"specs": records})


def load_rounds(site: SiteData) -> list[dict[str, Any]]:
    path = site.path(f"{ANALYSIS_DIR}/{ROUNDS_FILE}")
    return read_json(path)["rounds"] if path.is_file() else []


def compose(site: SiteData) -> Ledger:
    grid = load_grid(site)
    lens_run = load_lenses(site.path(ANALYSIS_DIR))
    foundation = load_foundation(site)
    params = site.parameter_set()
    problems: list[str] = []
    candidates = list(lens_run.candidates)
    taken = {candidate.id for candidate in candidates}
    spec_records: list[dict[str, Any]] = []
    # A person's reference rules (P39) are a lens like the others.
    rules, found = preference_specs(params.value("reference_rules") or [], "reference")
    problems += found
    for spec in rules:
        made, failed = reference_candidates(grid, spec, "rule")
        problems += [f"偏好 P39 规则 {spec.id}：{text}" for text in failed]
        for candidate in made:
            if candidate.id not in taken:
                candidates.append(candidate)
                taken.add(candidate.id)
        spec_records.append({"origin": "偏好 P39", "spec": spec.model_dump(mode="json", exclude_none=True), "accepted": True, "problems": failed, "source": "rule"})
    # The model's specs, as settled by each look.
    model_residual: list[ResidualSpec] = []
    for record in load_spec_records(site):
        spec_records.append({**record, "source": "model"})
        if not record.get("accepted"):
            continue
        spec = parse_spec(record["spec"])
        if isinstance(spec, ReferenceSpec):
            made, _failed = reference_candidates(grid, spec, "model")
            for candidate in made:
                if candidate.id not in taken:
                    candidate.details["origin"] = record.get("origin")
                    candidates.append(candidate)
                    taken.add(candidate.id)
        elif isinstance(spec, ResidualSpec):
            model_residual.append(spec)
    # Residual: what the generic lenses see beyond the located ones.
    located = union_of(grid, [c for c in candidates if c.lens not in GENERIC])
    generic = union_of(grid, [c for c in candidates if c.lens in GENERIC])
    residual = np.clip(generic - located, 0, None)
    rule_mask = np.zeros((len(grid.meters), grid.cells), dtype=bool)
    residual_rules, found = preference_specs(params.value("residual_rules") or [], "residual")
    problems += found
    for spec in residual_rules:
        masks, failed = residual_mask(grid, spec)
        problems += [f"偏好 P40 规则 {spec.id}：{text}" for text in failed]
        for meter_id, mask in masks.items():
            rule_mask[grid.row(meter_id)] |= mask
        spec_records.append({"origin": "偏好 P40", "spec": spec.model_dump(mode="json", exclude_none=True), "accepted": True, "problems": failed, "source": "rule"})
    model_mask = np.zeros_like(rule_mask)
    for spec in model_residual:
        masks, _failed = residual_mask(grid, spec)
        for meter_id, mask in masks.items():
            model_mask[grid.row(meter_id)] |= mask
    residual_rule = np.where(rule_mask, residual, 0.0)
    residual_open = np.where(rule_mask, 0.0, residual)
    # Screening.
    meter_kwh = {meter_id: grid.total(meter_id) for meter_id in grid.meters}
    meter_coverage = {meter_id: grid.coverage(meter_id) for meter_id in grid.meters}
    context = ScreenContext(params, meter_kwh, meter_coverage)
    screening = {}
    for candidate in candidates:
        effective = float(candidate.cells[~rule_mask[grid.row(candidate.meter)]].sum())
        screening[candidate.id] = screen(candidate, context, effective)
    overlaps, groups = _overlaps(grid, candidates)
    coverage = _coverage(grid, lens_run, candidates, screening, residual_open, residual_rule, model_mask)
    triggers = _triggers(grid, foundation, candidates, residual_open, lens_run, params)
    slices = _slices(grid, residual_open, model_mask, int(params.value("residual_top_slices")), lens_run)
    saturation = _saturation(grid, candidates, params)
    return Ledger(
        grid=grid,
        lens_run=lens_run,
        params=params,
        candidates=candidates,
        screening=screening,
        overlaps=overlaps,
        overlap_group=groups,
        coverage=coverage,
        residual=residual_open,
        residual_rule=residual_rule,
        residual_model=model_mask,
        slices=slices,
        triggers=triggers,
        saturation=saturation,
        specs=spec_records,
        problems=problems,
        rounds=load_rounds(site),
    )


def _coverage(
    grid: Grid,
    lens_run: LensRun,
    candidates: list[Candidate],
    screening: dict[str, dict[str, Any]],
    residual: np.ndarray,
    residual_rule: np.ndarray,
    model_mask: np.ndarray,
) -> list[dict[str, Any]]:
    counts = np.zeros((len(grid.meters), grid.cells), dtype=np.int16)
    looked: dict[str, list[str]] = defaultdict(list)
    for lens, mask in lens_run.examined.items():
        counts += mask
        for row in np.flatnonzero(mask.any(axis=1)):
            looked[grid.meters[row]].append(lens)
    for candidate in candidates:
        if candidate.lens in ("R", "M"):
            looked[candidate.meter].append(candidate.lens)
    by_result = {
        result: union_of(grid, [c for c in candidates if screening[c.id]["result"] == result])
        for result in (VALID, PENDING, INVALID)
    }
    rows = []
    for meter_id in grid.meters:
        row = grid.row(meter_id)
        energy = grid.energy[row]
        total = float(energy.sum())
        if total <= 0 and not grid.ok[row].any():
            continue
        rows.append(
            {
                "meter": meter_id,
                "name": grid.names.get(meter_id, ""),
                "system": system_of(grid, meter_id),
                "energy_kwh": round(total, 1),
                "data_coverage": round(grid.coverage(meter_id), 4),
                "lenses": sorted(set(looked.get(meter_id, []))),
                "examined_kwh": round(float(energy[counts[row] >= 1].sum()), 1),
                "examined_twice_kwh": round(float(energy[counts[row] >= 2].sum()), 1),
                "not_examined_kwh": round(float(energy[counts[row] == 0].sum()), 1),
                "valid_kwh": round(float(by_result[VALID][row].sum()), 1),
                "pending_kwh": round(float(by_result[PENDING][row].sum()), 1),
                "invalid_kwh": round(float(by_result[INVALID][row].sum()), 1),
                "residual_open_kwh": round(float(residual[row].sum()), 1),
                "residual_model_kwh": round(float(residual[row][model_mask[row]].sum()), 1),
                "residual_rule_kwh": round(float(residual_rule[row].sum()), 1),
            }
        )
    return rows


def _triggers(
    grid: Grid,
    foundation: Foundation,
    candidates: list[Candidate],
    residual: np.ndarray,
    lens_run: LensRun,
    params: ParameterSet,
) -> list[dict[str, Any]]:
    ratio_line = float(params.value("elasticity_trigger"))
    share_line = float(params.value("trigger_min_share"))
    correlation = float(params.value("driven_correlation"))
    triggers = []
    pws = {row["meter"]: row for row in foundation.rows("PWS")} if "PWS" in foundation.tables else {}
    for meter_id in grid.meters:
        if meter_id in grid.line_index:
            continue
        row = pws.get(meter_id)
        if row and row.get("ratio") is not None and row["ratio"] >= ratio_line and (row.get("stop_share") or 0) >= share_line:
            triggers.append(
                {
                    "id": f"T-STOP-{handle_part(meter_id)}",
                    "kind": "stop_no_drop",
                    "title": "停产时不降",
                    "meters": [meter_id],
                    "system": system_of(grid, meter_id),
                    "measured": {"stop_kw": row["stop_kw"], "run_kw": row["run_kw"], "ratio": row["ratio"], "stop_kwh": row["stop_kwh"], "stop_share": row["stop_share"]},
                    "text": f"全厂停时平均 {row['stop_kw']} kW，在产时 {row['run_kw']} kW，比值 {row['ratio']}（≥ P31 {ratio_line:g}）；停时电量 {row['stop_kwh']} kWh，占本表 {row['stop_share']}",
                    "asks": "判这块表停产时的用电是必要服务还是可能的浪费；可能浪费的交参照规格（参照取多少、出处），必要的说明依据或交剩余解释",
                }
            )
    peers: dict[str, list[str]] = defaultdict(list)
    for candidate in candidates:
        if candidate.lens == "L3" and not candidate.in_union:
            peers[candidate.part or ""].append(candidate.meter)
    for group, meters in sorted(peers.items()):
        triggers.append(
            {
                "id": f"T-PEER-{handle_part(group)}",
                "kind": "peer_unconfirmed",
                "title": "可比条件缺",
                "meters": sorted(meters),
                "system": system_of(grid, meters[0]),
                "measured": {"group": group},
                "text": f"可比组 {group} 在 {len(meters)} 条产线上都有批次，没人确认可比（A05 peer_group）",
                "asks": "说明这几条线能不能比（机型、工艺、批量），能比的交可比分组规格（出处），不能比或不知道的写现场问题",
            }
        )
    for meter_id in grid.meters:
        if meter_id in grid.line_index:
            continue
        row = grid.row(meter_id)
        total = grid.total(meter_id)
        left = float(residual[row].sum())
        rho = lens_run.driven.get(meter_id)
        if total > 0 and left >= share_line * total and (rho is None or abs(rho) < correlation):
            triggers.append(
                {
                    "id": f"T-RES-{handle_part(meter_id)}",
                    "kind": "residual_unrelated",
                    "title": "剩余和生产无关",
                    "meters": [meter_id],
                    "system": system_of(grid, meter_id),
                    "measured": {"residual_kwh": round(left, 1), "share": round(left / total, 4), "production_correlation": None if rho is None else round(rho, 3)},
                    "text": f"没解释的超出 {left:.0f} kWh，占本表 {left / total:.1%}；逐日电量和产线电量的秩相关 {('%.2f' % rho) if rho is not None else '算不了'}（< P21 {correlation:g}）",
                    "asks": "说明是什么在推动（天气、作息、别的系统），交驱动假设或剩余解释；数据里没有的写要补的数据；像浪费的交参照规格",
                }
            )
    return triggers


def _slices(grid: Grid, residual: np.ndarray, model_mask: np.ndarray, top: int, lens_run: LensRun) -> list[dict[str, Any]]:
    week, keys = _week(grid)
    rows = []
    for meter_id in grid.meters:
        row = grid.row(meter_id)
        values = residual[row]
        if values.sum() <= 0:
            continue
        per_week = np.bincount(week, weights=values, minlength=len(keys))
        for number in np.flatnonzero(per_week > 0):
            inside = week == number
            part = np.where(inside, values, 0.0)
            daily = grid.daily(part)
            days = [(grid.day_keys[day], round(float(daily[day]), 1)) for day in np.flatnonzero(daily > 0)]
            night = float(part[(grid.minute < 360) | (grid.minute >= 22 * 60)].sum())
            rows.append(
                {
                    "id": f"S-{handle_part(meter_id)}-{keys[number]}",
                    "meter": meter_id,
                    "name": grid.names.get(meter_id, ""),
                    "system": system_of(grid, meter_id),
                    "week": keys[number],
                    "kwh": round(float(per_week[number]), 1),
                    "model_explained_kwh": round(float(part[model_mask[row]].sum()), 1),
                    "days": days,
                    "night_share": round(night / float(part.sum()), 3) if part.sum() > 0 else None,
                    "stop_share": round(float(part[grid.stop].sum()) / float(part.sum()), 3) if part.sum() > 0 else None,
                    "production_correlation": lens_run.driven.get(meter_id),
                }
            )
    rows.sort(key=lambda entry: -(entry["kwh"] - entry["model_explained_kwh"]))
    return rows[:top]


def ledger_record(ledger: Ledger) -> dict[str, Any]:
    """The ledger as JSON: what the workbook, the briefs and the tests read."""

    return {
        "summary": ledger.summary(),
        "candidates": [
            {
                **candidate.as_dict(),
                "system": system_of(ledger.grid, candidate.meter),
                "screen": ledger.screening[candidate.id],
                "overlaps": ledger.overlaps.get(candidate.id, []),
                "overlap_group": ledger.overlap_group.get(candidate.id),
            }
            for candidate in ledger.candidates
        ],
        "coverage": ledger.coverage,
        "triggers": ledger.triggers,
        "slices": ledger.slices,
        "saturation": ledger.saturation,
        "specs": ledger.specs,
        "rounds": ledger.rounds,
        "problems": ledger.problems,
        "lens_notes": ledger.lens_run.notes,
        "driven": ledger.lens_run.driven,
    }


def write_ledger(site: SiteData, ledger: Ledger) -> Path:
    path = site.path(f"{ANALYSIS_DIR}/{LEDGER_FILE}")
    write_json(path, ledger_record(ledger))
    return path


__all__ = [
    "LEDGER_FILE",
    "OTHER_SYSTEM",
    "ROUNDS_FILE",
    "SPECS_FILE",
    "Ledger",
    "compose",
    "ledger_record",
    "load_rounds",
    "load_spec_records",
    "save_spec_records",
    "system_of",
    "systems",
    "union_of",
    "write_ledger",
]
