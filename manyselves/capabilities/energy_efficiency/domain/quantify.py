"""Compute every number of every candidate item.

For each item the program measures the item's own windows, replays its
finding method over the whole data period, and adds deterministic stability
checks: the two halves of the period, the share of the single largest
occurrence, and how much the result moves when the windows shift by one
recording interval.  It also finds which items count the same records of
the same meter (overlap), prices energy when a confirmed tariff is given,
and flags numbers in the item's text that match nothing the item cites or
the program computed.  Nothing here ranks, merges or drops items.

Numbers of different meters are never added together: the meters may be
nested (a main meter and its sub-meters), and that is not known here.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .evidence import has_evidence, load_evidence
from .items import PartitionSubmission
from .methods import (
    MENU,
    NO_METHOD,
    MethodContext,
    MethodRun,
    Occurrence,
    base_portions,
    item_kwh,
    parse_params,
    run_method,
)
from .prepare import meter_totals, method_context
from .series import MeterSeries, WindowMeasure
from .store import SiteData, read_json

SCALAR_FIELDS: dict[str, str] = {
    "window_energy_kwh": "本项时间窗里的电量（kWh；只对单表电量项）",
    "window_avg_kw": "本项时间窗里的平均功率（kW；只对单表项）",
    "window_coverage": "本项时间窗的数据覆盖（0-1）",
    "replay_occurrences": "按寻找方式在全期回放得到的出现次数",
    "replay_hits": "其中达到阈值的次数（没设阈值时为空）",
    "replay_item_kwh": "全期回放中与本项相关的电量：有阈值时为命中部分；基底方法为基底电量（有阈值时只算命中的天）；否则为全部出现",
    "replay_share_of_meter": "replay_item_kwh 占该表全期电量的比例",
    "replay_mean_coverage": "全期回放各次出现的平均数据覆盖",
    "top_share": "最大的一次出现占全部的比例（越接近 1 越像是由一次造成）",
    "halves_ratio": "后半期 / 前半期的平均功率（按各次出现里实有的记录；基底方法比基底功率）",
    "boundary_max_change": "时间窗前后移动一个记录间隔或两头各收窄一个间隔后，平均功率的最大相对变化；基底方法是百分位上下移 5 时本项数值的最大相对变化",
    "window_cost": "本项时间窗里的电费（只在提供电价时；有记录跨电价时段、不能整条计价时为空）",
    "replay_cost": "全期回放相关电量的电费（同上；方法没能回放或没有数据时为空，回放了但没有命中时为 0）",
    "overlap_items": "与本项计入同一块表同一批记录的其他项数",
    "overlap_max_shared_kwh": "与其他某一项共同计入的最大电量（kWh；每条记录取两项各自计入部分的较小者）",
    "check_issue_count": "程序检查后仍未解决的问题数",
    "number_hint_count": "文字中对不上证据和程序计算的数的个数",
}


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


def _iso(value: datetime) -> str:
    return pd.Timestamp(value).isoformat(sep=" ")


@dataclass
class _MeterBasis:
    """The records an item counts on one meter, with the kWh it counts of each."""

    occurrences: list[Occurrence]
    portions: dict[int, float] = field(default_factory=dict)

    @property
    def indexes(self) -> set[int]:
        return set(self.portions)


def _portions(series: MeterSeries, counted: list[Occurrence], baseload: bool) -> dict[int, float]:
    portions: dict[int, float] = {}
    for occurrence in counted:
        measure = occurrence.measure
        if baseload:
            base_kw = occurrence.extra.get("base_kw")
            if base_kw is None or not series.has_energy:
                portions.update({index: 0.0 for index in series.index_set(measure)})
                continue
            portions.update(base_portions(series, measure.first_index, measure.last_index, base_kw))
        else:
            for index in series.index_set(measure):
                portions[index] = float(series.values[index]) if series.has_energy else 0.0
    return portions


def _quantity(measures: list[WindowMeasure], has_energy: bool) -> float | None:
    with_data = [item for item in measures if item.records]
    if not with_data:
        return None
    if has_energy:
        return float(sum(item.energy_kwh or 0.0 for item in with_data))
    powers = [item.avg_kw for item in with_data if item.avg_kw is not None]
    return float(statistics.fmean(powers)) if powers else None


def _avg_kw(measures: list[WindowMeasure], has_energy: bool) -> float | None:
    """Average power over the records the windows hold: energy / covered hours, or mean kW."""

    with_data = [item for item in measures if item.records]
    if not with_data:
        return None
    if has_energy:
        hours = sum(item.covered_minutes for item in with_data) / 60
        return float(sum(item.energy_kwh or 0.0 for item in with_data) / hours) if hours > 0 else None
    weights = [(item.avg_kw, item.covered_minutes) for item in with_data if item.avg_kw is not None]
    total = sum(weight for _value, weight in weights)
    if not weights:
        return None
    if total <= 0:
        return float(statistics.fmean(value for value, _weight in weights))
    return float(sum(value * weight for value, weight in weights) / total)


def _counted(run: MethodRun) -> list[Occurrence]:
    if run.summary.get("hits") is not None:
        return [item for item in run.occurrences if item.hit]
    return [item for item in run.occurrences if item.measure.records]


def _shifts(
    series: MeterSeries,
    windows: list[tuple[datetime, datetime]],
) -> dict[str, Any] | None:
    """How much the average power moves when every window moves or narrows by one record.

    Average power, not energy, so a narrower window is not "smaller" only
    because it is shorter.
    """

    base = _avg_kw(series.measure_many(windows), series.has_energy) if windows else None
    if not base:
        return None
    step = timedelta(minutes=series.interval_minutes)
    variants = {
        "earlier": [(start - step, end - step) for start, end in windows],
        "later": [(start + step, end + step) for start, end in windows],
    }
    if all(end - start > 2 * step for start, end in windows):
        variants["narrower"] = [(start + step, end - step) for start, end in windows]
    changes: dict[str, float | None] = {}
    for name, shifted in variants.items():
        value = _avg_kw(series.measure_many(shifted), series.has_energy)
        changes[name] = None if value is None else _r((value - base) / base, 4)
    known = [abs(value) for value in changes.values() if value is not None]
    return {"step_minutes": series.interval_minutes, "changes": changes, "max_change": max(known) if known else None}


def _robustness(
    series: MeterSeries,
    counted: list[Occurrence],
    period: tuple[datetime, datetime],
    shiftable: bool,
) -> dict[str, Any]:
    measures = [item.measure for item in counted]
    base = _quantity(measures, series.has_energy)
    start, end = period
    middle = start + (end - start) / 2
    first = [item for item in measures if item.start < middle]
    second = [item for item in measures if item.start >= middle]
    halves: dict[str, Any] = {"first_count": len(first), "second_count": len(second), "ratio": None}
    # Average power of each half, so missing records do not read as change.
    first_value = _avg_kw(first, series.has_energy)
    second_value = _avg_kw(second, series.has_energy)
    if first_value and second_value is not None:
        halves["ratio"] = _r(second_value / first_value, 4)
    top_share = None
    if series.has_energy and base:
        largest = max(item.energy_kwh or 0.0 for item in measures)
        top_share = _r(largest / base, 4)
    coverages = [item.coverage for item in measures]
    return {
        "counted": len(measures),
        "halves": halves,
        "top_share": top_share,
        "boundary": _shifts(series, [(item.start, item.end) for item in measures]) if shiftable else None,
        "mean_coverage": _r(statistics.fmean(coverages), 4) if coverages else None,
        "low_coverage": sum(1 for value in coverages if value < 0.9),
    }


def _base_robustness(series: MeterSeries, counted: list[Occurrence], period: tuple[datetime, datetime]) -> dict[str, Any]:
    """Stability of a baseload item, on each day's base energy (or base power)."""

    start, end = period
    middle = start + (end - start) / 2
    # Halves compare the base power of the days; missing records do not move it.
    powers = [(item.measure.start, item.extra["base_kw"]) for item in counted if item.extra.get("base_kw") is not None]
    first = [value for day, value in powers if day < middle]
    second = [value for day, value in powers if day >= middle]
    ratio = None
    if first and second and statistics.fmean(first):
        ratio = _r(statistics.fmean(second) / statistics.fmean(first), 4)
    days = [item.extra["base_kwh"] for item in counted if item.extra.get("base_kwh") is not None]
    total = sum(days)
    coverages = [item.measure.coverage for item in counted]
    return {
        "counted": len(counted),
        "halves": {"first_count": len(first), "second_count": len(second), "ratio": ratio},
        "top_share": _r(max(days) / total, 4) if series.has_energy and total else None,
        "boundary": None,
        "mean_coverage": _r(statistics.fmean(coverages), 4) if coverages else None,
        "low_coverage": sum(1 for value in coverages if value < 0.9),
    }


def _baseload_value(run: MethodRun, has_energy: bool) -> float | None:
    """The number a baseload item stands for: its base energy, or the median base power of its days."""

    if has_energy:
        return item_kwh(run)
    if run.summary.get("hits") is None:
        return run.summary.get("median_base_kw")
    powers = [item.extra["base_kw"] for item in run.occurrences if item.hit and item.extra.get("base_kw") is not None]
    return _r(statistics.median(powers)) if powers else None


def _baseload_boundary(
    series: MeterSeries,
    params: dict[str, Any],
    ctx: MethodContext,
    base_run: MethodRun,
) -> dict[str, Any] | None:
    """How much the item's own number moves when the percentile moves by five.

    With thresholds that number covers only the days that hit, so a
    percentile that changes which days hit shows here.
    """

    base = _baseload_value(base_run, series.has_energy)
    if not base:
        return None
    percentile = parse_params("baseload", params).percentile
    changes: dict[str, float | None] = {}
    for name, value in (("lower", max(1.0, percentile - 5)), ("higher", min(50.0, percentile + 5))):
        if value == percentile:
            continue
        run = run_method("baseload", {**params, "percentile": value}, series, ctx, None)
        other = _baseload_value(run, series.has_energy)
        changes[f"{name}_p{value:g}"] = None if other is None else _r((other - base) / base, 4)
    known = [abs(value) for value in changes.values() if value is not None]
    return {"changes": changes, "max_change": max(known) if known else None}


class _Hourly:
    """Hourly average power of each meter over the period, for percentile ranks."""

    def __init__(self, site: SiteData, period: tuple[datetime, datetime]) -> None:
        self.site = site
        self.period = period
        self.cache: dict[str, np.ndarray] = {}

    def powers(self, meter_id: str) -> np.ndarray:
        if meter_id not in self.cache:
            series = self.site.series(meter_id)
            start, end = self.period
            values = []
            cursor = start.replace(minute=0, second=0, microsecond=0)
            while cursor < end:
                finish = cursor + timedelta(hours=1)
                if cursor >= start and finish <= end:
                    measure = series.measure(cursor, finish)
                    if measure.records and measure.avg_kw is not None:
                        values.append(measure.avg_kw)
                cursor = finish
            self.cache[meter_id] = np.array(sorted(values))
        return self.cache[meter_id]

    def rank(self, meter_id: str, value: float | None) -> float | None:
        powers = self.powers(meter_id)
        if value is None or not len(powers):
            return None
        return _r(float(np.searchsorted(powers, value, "right")) / len(powers), 4)


def _price_records(
    site: SiteData, series: MeterSeries, portions: dict[int, float], measured: bool = False
) -> dict[str, Any] | None:
    """Price the kWh counted of each record at the tariff of the period that holds the record.

    A record that crosses from one tariff period into another is not split;
    it stays unpriced and the cost is then left empty, because a partial sum
    would read as the whole cost.  Nothing counted is a cost of 0 when the
    data was there (``measured``) and empty when it was not.
    """

    tariff = site.manifest.tariff
    if tariff is None or not series.has_energy or not (portions or measured):
        return None
    cost = 0.0
    unpriced = 0
    unpriced_kwh = 0.0
    for index in sorted(portions):
        start = pd.Timestamp(series.starts[index]).to_pydatetime()
        end = pd.Timestamp(series.ends[index]).to_pydatetime()
        price = tariff.price_over(start, end)
        if price is None:
            unpriced += 1
            unpriced_kwh += portions[index]
            continue
        cost += portions[index] * price[1]
    return {
        "cost": _r(cost, 2) if unpriced == 0 else None,
        "priced_part": _r(cost, 2),
        "currency": tariff.currency,
        "unpriced_records": unpriced,
        "unpriced_kwh": _r(unpriced_kwh),
    }


# Month-day written with digits only counts as a date when it can be one
# (03-02, 3/2); 12-95 is a range of two numbers.
_DATE = re.compile(
    r"(?<![\d.])(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}(?![\d.])"
    r"|(?<![\d.])(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号])?)?"
    r"|(?<![\d.])(?:19|20)\d{2}[-/]\d{1,2}(?![\d.\-/])"
    r"|(?<![\d.])\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号])?"
    r"|(?<![\d.])(?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])(?![\d.])"
)
# Codes such as IM1-40, EV-0123456789 or P01-02 are names, not figures.
_CODE = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]*(?:[-.][A-Za-z0-9_]+)+")
_TIME = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?|\d{1,2}\s*[点时](?:\s*\d{1,2}\s*分)?|\d{1,2}\s*(?:号|日|周|月份)")
# A number may stand against its unit (35kW) or end a range (12-95); it may
# not be part of a code (L001) or a longer number.  1,872 is one grouped
# number; 1.52万 is 15200.
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:(%|％)|\s*(万|亿))?(?![0-9_]|\.\d|,\d{3})"
)
# The first number of a range shares the unit written after the second (18.7-25%).
_RANGE_PERCENT = re.compile(r"\s*[-~～–—至到]\s*\d+(?:\.\d+)?\s*[%％]")
_SCALE = {"万": 1e4, "亿": 1e8}


def _numbers_in(value: Any, found: list[float]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        found.append(float(value))
    elif isinstance(value, dict):
        for item in value.values():
            _numbers_in(item, found)
    elif isinstance(value, list):
        for item in value:
            _numbers_in(item, found)


def _small(token: str) -> bool:
    """A whole number below 10: too common to check."""

    return token.isdigit() and len(token.lstrip("0") or "0") == 1


def _matches(token: str, percent: bool, known: list[float], scale: float = 1.0) -> bool:
    number = float(token)
    decimals = len(token.split(".")[1]) if "." in token else 0
    half = 0.5 * 10 ** (-decimals)
    candidates = [(number, half)]
    if percent:
        candidates.append((number / 100, half / 100))
    if scale != 1.0:
        candidates.append((number * scale, half * scale))
    for value in known:
        for candidate, rounding in candidates:
            if abs(candidate - value) <= max(rounding, 0.005 * abs(value)):
                return True
    return False


def number_hints(texts: list[str], known: list[float], codes: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Numbers in the model's text that match nothing it cited or the program computed.

    Dates, clock times and codes (``codes`` lists the site's own names, such
    as meter ids, which may be in any case) are not figures and are skipped.
    """

    hints = []
    names = sorted({code for code in codes if code}, key=len, reverse=True)
    for text in texts:
        for name in names:
            text = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", " ", text)
        cleaned = _TIME.sub(" ", _DATE.sub(" ", _CODE.sub(" ", text)))
        for match in _NUMBER.finditer(cleaned):
            written, percent, scale = match.group(1), bool(match.group(2)), _SCALE.get(match.group(3) or "", 1.0)
            percent = percent or bool(_RANGE_PERCENT.match(cleaned, match.end()))
            token = written.replace(",", "")
            if not percent and scale == 1.0 and _small(token):
                continue
            if _matches(token, percent, known, scale):
                continue
            # "100,200" may also be a list of two numbers; "5,000" cannot.
            parts = written.split(",") if "," in written else []
            if (
                parts
                and not any(len(part) > 1 and part.startswith("0") for part in parts)
                and all(_matches(part, percent, known) or _small(part) for part in parts)
            ):
                continue
            shown = written if len(written) <= 24 else f"{written[:20]}…"
            hints.append(
                f"文字里的 {shown}{'%' if percent else match.group(3) or ''} 在引用的证据和程序计算里没有找到对应的数"
            )
    return list(dict.fromkeys(hints))


def _collect_items(site: SiteData, results: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """All items in a stable order with program ids, and the partition records."""

    partitions_meta = read_json(site.path("partitions.json"))["partitions"]
    rule_items = read_json(site.path("prechecks.json"))["items"]
    rule_ids = {item["key"]: f"R{number:03d}" for number, item in enumerate(rule_items, start=1)}
    rule_notes: dict[str, list[str]] = {}
    items: list[dict[str, Any]] = []
    partitions: list[dict[str, Any]] = []
    number = 0
    for meta in partitions_meta:
        partition_id = meta["partition_id"]
        result = results.get(partition_id)
        record: dict[str, Any] = {**meta}
        if result is None:
            record["outcome"] = "failed"
            partitions.append(record)
            continue
        # Fill the defaults the finder may have left out.
        submission = PartitionSubmission.model_validate(result["submission"]).model_dump(mode="json")
        check = result.get("check") or {"issues": []}
        local = {}
        for item in submission["items"]:
            number += 1
            local[item["key"]] = f"L{number:03d}"
        record.update(
            {
                "outcome": "ok",
                "coverage": [
                    {
                        **entry,
                        "item_ids": [local[key] for key in entry["item_keys"]]
                        + [rule_ids[key] for key in entry["rule_keys"] if key in rule_ids],
                    }
                    for entry in submission["coverage"]
                ],
                "notes": submission["notes"],
                "issues": [issue["problem"] for issue in check["issues"] if not issue["key"]]
                + list(result.get("notes") or []),
            }
        )
        partitions.append(record)
        for item in submission["items"]:
            items.append(
                {
                    "id": local[item["key"]],
                    "source": "model",
                    "partition_id": partition_id,
                    "local_key": item["key"],
                    "meter_ids": item["meter_ids"],
                    "windows": [
                        {"start": _iso(pd.Timestamp(window["start"])), "end": _iso(pd.Timestamp(window["end"]))}
                        for window in item["windows"]
                    ],
                    "finding_method": item["finding_method"],
                    "phenomenon": item["phenomenon"],
                    "reasoning": item["reasoning"],
                    "root_cause_candidates": item["root_cause_candidates"],
                    "missing_info": item.get("missing_info", []),
                    "monitoring_suggestion": item.get("monitoring_suggestion"),
                    "related_ids": [local[key] for key in item.get("related_keys", [])],
                    "evidence_ids": item["evidence_ids"],
                    "check_issues": [
                        issue["problem"] for issue in check["issues"] if issue["key"] == item["key"]
                    ],
                    "rule_notes": [],
                }
            )
        for note in submission.get("rule_item_notes", []):
            rule_notes.setdefault(note["rule_key"], []).append(f"{partition_id}：{note['note']}")
    for item in rule_items:
        items.append(
            {
                "id": rule_ids[item["key"]],
                "source": "rule",
                "partition_id": None,
                "local_key": item["key"],
                "meter_ids": item["meter_ids"],
                "windows": item["windows"],
                "finding_method": item["finding_method"],
                "phenomenon": item["phenomenon"],
                "reasoning": "",
                "root_cause_candidates": [],
                "missing_info": [],
                "monitoring_suggestion": None,
                "related_ids": [],
                "evidence_ids": item["evidence_ids"],
                "check_issues": [],
                "rule_notes": rule_notes.get(item["key"], []),
            }
        )
    return items, partitions


def quantify_register(
    workspace: Path,
    run_id: str,
    results: dict[str, Any],
    errors: dict[str, Any],
) -> dict[str, Any]:
    site = SiteData(workspace, run_id)
    period = site.period()
    ctx = method_context(site)
    totals = meter_totals(site)
    hourly = _Hourly(site, period)
    known_meters = set(site.meter_ids())
    items, partitions = _collect_items(site, results)
    for record in partitions:
        if record["outcome"] == "failed":
            record["error"] = str(errors.get(record["partition_id"], "no result"))
    bases: dict[str, dict[str, _MeterBasis]] = {}
    for item in items:
        method = item["finding_method"]
        method_id = method["method_id"]
        spec = MENU.get(method_id)
        item["finding_method"] = {**method, "title": spec.title if spec else "菜单外（不能程序回放）"}
        windows = [
            (pd.Timestamp(window["start"]).to_pydatetime(), pd.Timestamp(window["end"]).to_pydatetime())
            for window in item["windows"]
        ]
        per_meter: dict[str, Any] = {}
        bases[item["id"]] = {}
        for meter_id in item["meter_ids"]:
            if meter_id not in known_meters:
                # Named by the finder but not in the manifest: nothing to measure.
                per_meter[meter_id] = {"unknown_meter": True}
                continue
            series = site.series(meter_id)
            window_measures = series.measure_many(windows)
            whole = _combined(window_measures, series.has_energy)
            entry: dict[str, Any] = {
                "unit_kind": "energy" if series.has_energy else "power",
                "windows": [measure.as_dict() for measure in window_measures],
                "window_total": whole,
                "replay": None,
                "reference": {
                    "window_avg_kw_percentile_of_hours": hourly.rank(meter_id, whole["avg_kw"]),
                },
            }
            baseload = False
            if method_id != NO_METHOD and spec is not None:
                run = run_method(method_id, method["params"], series, ctx, totals.get(meter_id))
                entry["replay"] = run.as_dict(top=5)
                if run.status == "ok":
                    counted = _counted(run)
                    entry["replay"]["item_kwh"] = item_kwh(run)
                    baseload = method_id == "baseload"
                    if baseload:
                        robustness = _base_robustness(series, counted, period)
                        robustness["boundary"] = _baseload_boundary(series, method["params"], ctx, run)
                    else:
                        robustness = _robustness(series, counted, period, spec.shiftable)
                else:
                    counted = [Occurrence(measure) for measure in window_measures]
                    robustness = _robustness(series, counted, period, True)
            else:
                counted = [Occurrence(measure) for measure in window_measures]
                robustness = _robustness(series, counted, period, True)
            basis = _MeterBasis(counted, _portions(series, counted, baseload))
            bases[item["id"]][meter_id] = basis
            entry["robustness"] = robustness
            window_portions = _portions(series, [Occurrence(measure) for measure in window_measures], False)
            entry["cost"] = (
                {
                    "window": _price_records(site, series, window_portions),
                    "replay": _price_records(
                        site, series, basis.portions, bool(entry["replay"]["summary"].get("with_data"))
                    )
                    if entry["replay"] is not None and entry["replay"]["outcome"] == "ok"
                    else None,
                }
                if site.manifest.tariff is not None and series.has_energy
                else None
            )
            per_meter[meter_id] = entry
        item["computed"] = {"per_meter": per_meter, "overlap": [], "number_hints": []}
    _overlaps(site, items, bases)
    for item in items:
        known: list[float] = []
        for evidence_id in item["evidence_ids"]:
            if has_evidence(site, evidence_id):
                _numbers_in(load_evidence(site, evidence_id)["result"], known)
        _numbers_in(item["computed"]["per_meter"], known)
        _numbers_in(item["finding_method"]["params"], known)
        if item["source"] == "model":
            item["computed"]["number_hints"] = number_hints(
                [item["phenomenon"], item["reasoning"]],
                known,
                [*site.meter_ids(), *item["meter_ids"], *item["evidence_ids"]],
            )
        item["scalars"] = _scalars(item)
        item["check_code"] = check_code(item)
    meters = [
        {
            "meter_id": spec.id,
            "name": spec.name,
            "unit_kind": spec.unit_kind,
            "group": spec.group,
            "period_energy_kwh": totals.get(spec.id),
            "quality_notes": site.quality(spec.id).notes(),
        }
        for spec in site.manifest.meters
    ]
    rule_ids = {item["local_key"]: item["id"] for item in items if item["source"] == "rule"}
    prechecks = [
        {**outcome, "item_id": rule_ids.get(outcome.get("rule_key", ""))}
        for outcome in read_json(site.path("prechecks.json"))["outcomes"]
    ]
    return {
        "run_id": run_id,
        "site": site.manifest.site.name,
        "period": {"start": _iso(period[0]), "end": _iso(period[1])},
        "meters": meters,
        "partitions": partitions,
        "prechecks": prechecks,
        "items": items,
        "scalar_fields": SCALAR_FIELDS,
        "counts": {
            "model_items": sum(1 for item in items if item["source"] == "model"),
            "rule_items": sum(1 for item in items if item["source"] == "rule"),
            "partitions": len(partitions),
            "partitions_failed": sum(1 for record in partitions if record["outcome"] == "failed"),
        },
    }


def _combined(measures: list[WindowMeasure], has_energy: bool) -> dict[str, Any]:
    with_data = [item for item in measures if item.records]
    window_minutes = sum(item.window_minutes for item in measures)
    covered = sum(item.covered_minutes for item in measures)
    energy = _quantity(measures, True) if has_energy else None
    avg = None
    hours = covered / 60
    if has_energy and energy is not None and hours > 0:
        avg = energy / hours
    elif not has_energy and with_data:
        weights = [item.covered_minutes for item in with_data if item.avg_kw is not None]
        values = [item.avg_kw for item in with_data if item.avg_kw is not None]
        if weights and sum(weights) > 0:
            avg = sum(value * weight for value, weight in zip(values, weights, strict=True)) / sum(weights)
    return {
        "energy_kwh": _r(energy),
        "avg_kw": _r(avg),
        "coverage": _r(covered / window_minutes, 4) if window_minutes else None,
    }


def check_code(item: dict[str, Any]) -> str:
    """A short code of what the item says, so a mark can be matched to the same item."""

    identity = {
        "source": item["source"],
        "partition_id": item["partition_id"],
        "local_key": item["local_key"],
        "meter_ids": item["meter_ids"],
        "windows": item["windows"],
        "method_id": item["finding_method"]["method_id"],
        "params": item["finding_method"]["params"],
        "phenomenon": item["phenomenon"],
    }
    text = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
    # A leading letter keeps a spreadsheet from reading the code as a number.
    return "C" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _overlaps(site: SiteData, items: list[dict[str, Any]], bases: dict[str, dict[str, _MeterBasis]]) -> None:
    """Items that count the same records of a meter, and the kWh both count."""

    by_meter: dict[str, list[str]] = {}
    for item in items:
        for meter_id in item["meter_ids"]:
            if meter_id in bases[item["id"]]:
                by_meter.setdefault(meter_id, []).append(item["id"])
    index = {item["id"]: item for item in items}
    for meter_id, item_ids in by_meter.items():
        energy = site.series(meter_id).has_energy
        for position, first in enumerate(item_ids):
            for second in item_ids[position + 1 :]:
                one, two = bases[first][meter_id].portions, bases[second][meter_id].portions
                shared = set(one) & set(two)
                if not shared:
                    continue
                kwh = _r(sum(min(one[record], two[record]) for record in shared)) if energy else None
                for this, other in ((first, second), (second, first)):
                    index[this]["computed"]["overlap"].append(
                        {"item_id": other, "meter_id": meter_id, "shared_records": len(shared), "shared_kwh": kwh}
                    )


def _scalars(item: dict[str, Any]) -> dict[str, Any]:
    per_meter = item["computed"]["per_meter"]
    overlap = item["computed"]["overlap"]
    shared = [entry["shared_kwh"] for entry in overlap if entry["shared_kwh"] is not None]
    scalars: dict[str, Any] = {name: None for name in SCALAR_FIELDS}
    scalars.update(
        {
            "overlap_items": len({entry["item_id"] for entry in overlap}),
            "overlap_max_shared_kwh": max(shared) if shared else None,
            "check_issue_count": len(item["check_issues"]),
            "number_hint_count": len(item["computed"]["number_hints"]),
        }
    )
    if len(per_meter) != 1:
        return scalars
    (entry,) = per_meter.values()
    if entry.get("unknown_meter"):
        return scalars
    replay = entry["replay"] or {}
    summary = replay.get("summary") or {}
    robustness = entry["robustness"]
    boundary = robustness.get("boundary") or {}
    cost = entry["cost"] or {}
    scalars.update(
        {
            "window_energy_kwh": entry["window_total"]["energy_kwh"],
            "window_avg_kw": entry["window_total"]["avg_kw"],
            "window_coverage": entry["window_total"]["coverage"],
            "replay_occurrences": summary.get("occurrences"),
            "replay_hits": summary.get("hits"),
            "replay_item_kwh": replay.get("item_kwh"),
            "replay_share_of_meter": summary.get("base_share_of_meter", summary.get("share_of_meter")),
            "replay_mean_coverage": summary.get("mean_coverage"),
            "top_share": robustness.get("top_share"),
            "halves_ratio": robustness["halves"]["ratio"],
            "boundary_max_change": boundary.get("max_change"),
            "window_cost": (cost.get("window") or {}).get("cost"),
            "replay_cost": (cost.get("replay") or {}).get("cost"),
        }
    )
    return scalars


__all__ = ["SCALAR_FIELDS", "check_code", "number_hints", "quantify_register"]
