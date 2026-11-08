"""Where one run keeps its frozen inputs, normalized data and results.

``Work/runs/<run_id>/energy-inputs/``  the input folder as it was when the run started
``Work/runs/<run_id>/energy/``         normalized data, evidence and the candidate register
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from .inputs import (
    MANIFEST_NAME,
    Batch,
    CalendarWindow,
    InputFormatError,
    Manifest,
    MeterQuality,
    MeterSpec,
    load_manifest,
    manifest_files,
    safe_relative,
)
from .series import Kind, MeterSeries

FROZEN_INPUTS = "energy-inputs"
DATA_DIR = "energy"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class RunDataError(ValueError):
    """A run's data is missing or a reference into it is not valid."""


def run_dir(workspace: Path, run_id: str) -> Path:
    if not _RUN_ID.match(run_id or ""):
        raise RunDataError(f"not a run id: {run_id!r}")
    return Path(workspace) / "Work" / "runs" / run_id


def freeze_inputs(workspace: Path, run_id: str, input_dir: str) -> Path:
    """Copy the manifest and every file it names into the run, once."""

    target = run_dir(workspace, run_id) / FROZEN_INPUTS
    if (target / MANIFEST_NAME).is_file():
        return target
    root = Path(workspace).resolve()
    source = (root / input_dir).resolve()
    if not source.is_relative_to(root) or not source.is_dir():
        raise InputFormatError(f"input folder is not a project folder: {input_dir}")
    manifest = load_manifest(source)
    staging = target.with_name(f"{FROZEN_INPUTS}.partial")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copy2(source / MANIFEST_NAME, staging / MANIFEST_NAME)
    for name in manifest_files(manifest):
        path = safe_relative(source, name)
        destination = staging / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    staging.rename(target)
    return target


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path) -> Any:
    if not path.is_file():
        raise RunDataError(f"run data is missing: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, pd.Timestamp)):
        return pd.Timestamp(value).isoformat(sep=" ")
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def series_kind(spec: MeterSpec) -> Kind:
    return "energy" if spec.has_energy else "instant"


@lru_cache(maxsize=256)
def _read_series(path: str, mtime_ns: int, kind: Kind) -> pd.DataFrame:
    del mtime_ns
    columns = ["time"] if kind == "instant" else ["start", "end"]
    return pd.read_csv(path, parse_dates=columns)


class SiteData:
    """Read access to one run's frozen manifest and normalized data."""

    def __init__(self, workspace: Path, run_id: str) -> None:
        self.workspace = Path(workspace)
        self.run_id = run_id
        self.root = run_dir(workspace, run_id)
        self.inputs = self.root / FROZEN_INPUTS
        self.data = self.root / DATA_DIR
        if not (self.inputs / MANIFEST_NAME).is_file():
            raise RunDataError(f"run {run_id} has no frozen energy inputs")
        self.manifest: Manifest = load_manifest(self.inputs)

    # Paths -----------------------------------------------------------------
    def series_path(self, meter_id: str) -> Path:
        self.manifest.meter(meter_id)
        return self.data / "series" / f"{meter_id}.csv"

    def path(self, name: str) -> Path:
        return self.data / name

    # Normalized data ---------------------------------------------------------
    def meter_ids(self) -> list[str]:
        return [meter.id for meter in self.manifest.meters]

    def spec(self, meter_id: str) -> MeterSpec:
        try:
            return self.manifest.meter(meter_id)
        except KeyError as exc:
            raise RunDataError(f"unknown meter: {meter_id}") from exc

    def series(self, meter_id: str) -> MeterSeries:
        spec = self.spec(meter_id)
        path = self.series_path(meter_id)
        if not path.is_file():
            raise RunDataError(f"meter {meter_id} has not been prepared")
        kind = series_kind(spec)
        frame = _read_series(str(path), path.stat().st_mtime_ns, kind)
        return MeterSeries(meter_id, kind, frame, spec.interval_minutes)

    def quality(self, meter_id: str) -> MeterQuality:
        meters = read_json(self.path("meters.json"))
        for meter in meters["meters"]:
            if meter["id"] == meter_id:
                return MeterQuality.model_validate(meter["quality"])
        raise RunDataError(f"meter {meter_id} has no quality record")

    def period(self) -> tuple[datetime, datetime]:
        value = read_json(self.path("meters.json"))["period"]
        if value is None:
            raise RunDataError("the run has no data period")
        return (
            pd.Timestamp(value["start"]).to_pydatetime(),
            pd.Timestamp(value["end"]).to_pydatetime(),
        )

    def batches(self) -> list[Batch] | None:
        path = self.path("production.json")
        if not path.is_file():
            return None
        return [Batch.model_validate(item) for item in read_json(path)["batches"]]

    def calendar(self) -> list[CalendarWindow] | None:
        path = self.path("calendar.json")
        if not path.is_file():
            return None
        return [CalendarWindow.model_validate(item) for item in read_json(path)["windows"]]

    def machine_meter(self, machine: str) -> str | None:
        for meter in self.manifest.meters:
            if machine in meter.production_machines:
                return meter.id
        return None

    def partitions(self) -> dict[str, list[str]]:
        value = read_json(self.path("partitions.json"))
        return {item["partition_id"]: item["meter_ids"] for item in value["partitions"]}


__all__ = [
    "DATA_DIR",
    "FROZEN_INPUTS",
    "RunDataError",
    "SiteData",
    "freeze_inputs",
    "read_json",
    "run_dir",
    "series_kind",
    "write_json",
]
