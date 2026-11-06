"""Tools the loss finder calls while it looks at one partition.

Each call names its ``scope`` (``<run id>#<partition id>``, given in the
partition brief) and may only read that partition's meters.  Every result is
kept as evidence and carries its ``evidence_id``.  A call that cannot be
answered returns ``{"status": "error", "error": ...}`` so the model can fix
the call in the same turn.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .evidence import record_evidence
from .inputs import InputFormatError
from .methods import MENU, MethodParamsError, Window, parse_params, run_method
from .prepare import meter_totals, method_context
from .series import WindowMeasure
from .store import RunDataError, SiteData

ROW_LIMIT = 800
NATIVE_HOURS = 48


class ScopeError(ValueError):
    pass


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=3, description="分区说明里的 scope，原样填")


def _local(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is not None:
        raise ValueError("times are site local time without a time zone, e.g. 2026-03-02 20:00")
    return value


class SeriesRequest(_Request):
    meter_id: str
    start: datetime = Field(description="站点本地时间，不带时区，例如 2026-03-02 20:00")
    end: datetime = Field(description="站点本地时间，不带时区")

    @field_validator("start", "end")
    @classmethod
    def local_time(cls, value: datetime | None) -> datetime | None:
        return _local(value)
    granularity: str = Field(
        default="hour",
        pattern="^(native|hour|day)$",
        description="native 原始记录（最多 48 小时）；hour 每小时；day 每天",
    )

    @model_validator(mode="after")
    def ordered(self) -> "SeriesRequest":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class CompareRequest(_Request):
    meter_id: str
    windows: list[Window] = Field(min_length=2, max_length=4)


class MethodRequest(_Request):
    meter_ids: list[str] = Field(min_length=1)
    method_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class ProductionRequest(_Request):
    machine: str | None = None
    start: datetime | None = Field(default=None, description="站点本地时间，不带时区")
    end: datetime | None = Field(default=None, description="站点本地时间，不带时区")

    @field_validator("start", "end")
    @classmethod
    def local_time(cls, value: datetime | None) -> datetime | None:
        return _local(value)


def _scope(workspace: Path, scope: str) -> tuple[SiteData, str, list[str]]:
    run_id, separator, partition_id = scope.partition("#")
    if not separator or not partition_id:
        raise ScopeError(f"scope must look like <run id>#<partition id>, not {scope!r}")
    site = SiteData(workspace, run_id)
    partitions = site.partitions()
    if partition_id not in partitions:
        raise ScopeError(f"unknown partition {partition_id}")
    return site, partition_id, partitions[partition_id]


def _own(meter_id: str, meters: list[str], partition_id: str) -> None:
    if meter_id not in meters:
        raise ScopeError(f"表 {meter_id} 不在分区 {partition_id}（本分区的表：{meters}）")


def _row(measure: WindowMeasure) -> dict[str, Any]:
    row = measure.as_dict()
    row.pop("records")
    return row


def _guarded(handler):
    def call(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return handler(workspace, arguments)
        except (ScopeError, RunDataError, MethodParamsError, InputFormatError, ValidationError) as exc:
            return {"status": "error", "error": str(exc)}

    return call


@_guarded
def energy_series(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    request = SeriesRequest.model_validate(arguments)
    site, partition_id, meters = _scope(workspace, request.scope)
    _own(request.meter_id, meters, partition_id)
    series = site.series(request.meter_id)
    hours = (request.end - request.start).total_seconds() / 3600
    if request.granularity == "native":
        if hours > NATIVE_HOURS:
            raise ScopeError(f"native 最多看 {NATIVE_HOURS} 小时；更长请用 hour 或 day")
        frame = series_rows(series, request.start, request.end)
        rows = frame
    else:
        step = timedelta(hours=1) if request.granularity == "hour" else timedelta(days=1)
        count = int((request.end - request.start) / step) + 1
        if count > ROW_LIMIT:
            raise ScopeError(f"{request.granularity} 粒度最多 {ROW_LIMIT} 行；请缩短时间或改用 day")
        rows = []
        cursor = request.start
        while cursor < request.end:
            finish = min(cursor + step, request.end)
            rows.append(_row(series.measure(cursor, finish)))
            cursor = finish
    if len(rows) > ROW_LIMIT:
        raise ScopeError(f"结果超过 {ROW_LIMIT} 行；请缩短时间")
    result = {
        "meter_id": request.meter_id,
        "unit_kind": "energy" if series.has_energy else "power",
        "granularity": request.granularity,
        "rows": rows,
        "note": "每行只计完全落在该行时段里的记录；跨边界的区间不拆分，计入 partial_excluded。",
    }
    result["evidence_id"] = record_evidence(
        site,
        partition_id=partition_id,
        tool="energy-series",
        arguments=request.model_dump(mode="json", exclude={"scope"}),
        meter_ids=[request.meter_id],
        result=result,
    )
    return result


def series_rows(series, start: datetime, end: datetime) -> list[dict[str, Any]]:
    lo, hi, _partial = series.inside(start, end)
    rows = []
    for index in range(lo, hi):
        begin = pd.Timestamp(series.starts[index]).isoformat(sep=" ")
        if series.kind == "instant":
            rows.append({"time": begin, "kw": round(float(series.values[index]), 3)})
            continue
        row = {"start": begin, "end": pd.Timestamp(series.ends[index]).isoformat(sep=" ")}
        row["kwh" if series.kind == "energy" else "avg_kw"] = round(float(series.values[index]), 3)
        rows.append(row)
    return rows


@_guarded
def compare_windows(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    request = CompareRequest.model_validate(arguments)
    site, partition_id, meters = _scope(workspace, request.scope)
    _own(request.meter_id, meters, partition_id)
    series = site.series(request.meter_id)
    measures = [series.measure(window.start, window.end) for window in request.windows]
    rows = [_row(measure) for measure in measures]
    base = measures[0]
    for row, measure in zip(rows[1:], measures[1:], strict=True):
        if base.avg_kw and measure.avg_kw is not None:
            row["avg_kw_vs_first"] = round(measure.avg_kw / base.avg_kw, 4)
    result = {
        "meter_id": request.meter_id,
        "unit_kind": "energy" if series.has_energy else "power",
        "windows": rows,
        "note": "只是数值比较，不说明原因；两个窗口的生产任务可能不同。",
    }
    result["evidence_id"] = record_evidence(
        site,
        partition_id=partition_id,
        tool="compare-windows",
        arguments=request.model_dump(mode="json", exclude={"scope"}),
        meter_ids=[request.meter_id],
        result=result,
    )
    return result


@_guarded
def try_method(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    request = MethodRequest.model_validate(arguments)
    site, partition_id, meters = _scope(workspace, request.scope)
    for meter_id in request.meter_ids:
        _own(meter_id, meters, partition_id)
    if request.method_id not in MENU:
        raise MethodParamsError(f"unknown method {request.method_id!r}; the menu has {sorted(MENU)}")
    parse_params(request.method_id, request.params)
    ctx = method_context(site)
    totals = meter_totals(site)
    runs = [
        run_method(request.method_id, request.params, site.series(meter_id), ctx, totals.get(meter_id)).as_dict()
        for meter_id in request.meter_ids
    ]
    result = {
        "method_id": request.method_id,
        "params": request.params,
        "runs": runs,
        "note": "程序在全期数据上按这个方法算出的结果；top_occurrences 是按电量（功率表按平均功率）排的前 10 次。",
    }
    result["evidence_id"] = record_evidence(
        site,
        partition_id=partition_id,
        tool="run-method",
        arguments=request.model_dump(mode="json", exclude={"scope"}),
        meter_ids=list(request.meter_ids),
        result=result,
    )
    return result


@_guarded
def production_records(workspace: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    request = ProductionRequest.model_validate(arguments)
    site, partition_id, meters = _scope(workspace, request.scope)
    batches = site.batches()
    if batches is None:
        raise ScopeError("manifest 里没有生产记录")
    own = {machine for meter_id in meters for machine in site.spec(meter_id).production_machines}
    if request.machine is not None and request.machine not in own:
        raise ScopeError(f"机台 {request.machine} 没有挂到本分区的表（本分区机台：{sorted(own) or '无'}）")
    wanted = {request.machine} if request.machine else own
    rows = []
    for batch in batches:
        if batch.machine not in wanted:
            continue
        if request.start and batch.end and batch.end <= request.start:
            continue
        if request.end and batch.start and batch.start >= request.end:
            continue
        rows.append(batch.model_dump(mode="json"))
    if len(rows) > ROW_LIMIT:
        raise ScopeError(f"结果超过 {ROW_LIMIT} 行；请缩短时间或指定机台")
    result = {
        "machines": sorted(wanted),
        "batches": rows,
        "note": "problem 非空的批次时间不全或不合理，生产间隙方法不使用它们。",
    }
    result["evidence_id"] = record_evidence(
        site,
        partition_id=partition_id,
        tool="production-records",
        arguments=request.model_dump(mode="json", exclude={"scope"}),
        meter_ids=[meter_id for meter_id in meters if set(site.spec(meter_id).production_machines) & wanted],
        result=result,
    )
    return result


__all__ = [
    "CompareRequest",
    "MethodRequest",
    "ProductionRequest",
    "SeriesRequest",
    "compare_windows",
    "energy_series",
    "production_records",
    "try_method",
]
