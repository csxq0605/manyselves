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
    PeriodSpec,
    SiteFact,
    TariffSchedule,
    TariffSpec,
    load_manifest,
    manifest_files,
    safe_relative,
)
from .series import Kind, MeterSeries
from .states import Segment

FROZEN_INPUTS = "energy-inputs"
PLANT_SCOPE = "PLANT"
DATA_DIR = "energy"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class RunDataError(ValueError):
    """A run's data is missing or a reference into it is not valid."""


def run_dir(workspace: Path, run_id: str) -> Path:
    if not _RUN_ID.match(run_id or ""):
        raise RunDataError(f"not a run id: {run_id!r}")
    return Path(workspace) / "Work" / "runs" / run_id


# Files copied from a folder with no manifest, for the survey and the drafter.
SURVEYED_SUFFIXES = {".csv", ".xlsx", ".xlsm", ".json"}
MAX_SURVEYED_FILES = 200


def freeze_inputs(workspace: Path, run_id: str, input_dir: str) -> Path:
    """Copy the manifest and every file it names into the run, once.

    A folder without a manifest is copied whole (the files the reader could
    take), so the program can survey it and the model draft a manifest.
    """

    target = run_dir(workspace, run_id) / FROZEN_INPUTS
    if target.is_dir():
        return target
    root = Path(workspace).resolve()
    source = (root / input_dir).resolve()
    if not source.is_relative_to(root) or not source.is_dir():
        raise InputFormatError(f"input folder is not a project folder: {input_dir}")
    staging = target.with_name(f"{FROZEN_INPUTS}.partial")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    if (source / MANIFEST_NAME).is_file():
        manifest = load_manifest(source)
        shutil.copy2(source / MANIFEST_NAME, staging / MANIFEST_NAME)
        names = manifest_files(manifest)
    else:
        names = sorted(
            path.relative_to(source).as_posix()
            for path in source.rglob("*")
            if path.is_file()
            and path.suffix.casefold() in SURVEYED_SUFFIXES
            and not any(part.startswith(".") for part in path.relative_to(source).parts)
        )
        if not names:
            raise InputFormatError(f"{input_dir} has no {MANIFEST_NAME} and no .csv, .xlsx or .json file")
        if len(names) > MAX_SURVEYED_FILES:
            raise InputFormatError(f"{input_dir} has {len(names)} data files; at most {MAX_SURVEYED_FILES} without a manifest")
    for name in names:
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


TASK_FILE = "task.json"
# Written when the model drafted the manifest: which draft was kept and what still looked odd.
ORIGIN_FILE = "manifest-origin.json"


def write_task_scope(workspace: Path, run_id: str, scope: dict[str, Any]) -> None:
    write_json(run_dir(workspace, run_id) / DATA_DIR / TASK_FILE, scope)


def task_scope(root: Path) -> dict[str, Any] | None:
    path = root / DATA_DIR / TASK_FILE
    return read_json(path) if path.is_file() else None


def narrowed(manifest: Manifest, scope: dict[str, Any] | None) -> Manifest:
    """The manifest cut to the meters and times the run was asked about."""

    if not scope:
        return manifest
    update: dict[str, Any] = {}
    if scope.get("meters"):
        known = {meter.id for meter in manifest.meters}
        unknown = sorted(set(scope["meters"]) - known)
        if unknown:
            raise InputFormatError(f"the run asks for meters the inputs do not have: {unknown}")
        update["meters"] = [meter for meter in manifest.meters if meter.id in scope["meters"]]
        update["prechecks"] = [
            check.model_copy(update={"meters": [m for m in check.meters if m in scope["meters"]]})
            if check.meters != "all"
            else check
            for check in manifest.prechecks
            if check.meters == "all" or set(check.meters) & set(scope["meters"])
        ]
    if scope.get("start") or scope.get("end"):
        start = pd.Timestamp(scope["start"]).to_pydatetime() if scope.get("start") else None
        end = pd.Timestamp(scope["end"]).to_pydatetime() if scope.get("end") else None
        if manifest.period is not None:
            start = max(start, manifest.period.start) if start else manifest.period.start
            end = min(end, manifest.period.end) if end else manifest.period.end
        if start is None or end is None:
            raise InputFormatError("a run limited by start or end needs both, or a period in the manifest")
        update["period"] = PeriodSpec(start=start, end=end)
    return manifest.model_copy(update=update) if update else manifest


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
        self.manifest: Manifest = narrowed(load_manifest(self.inputs), self.task_scope())

    def task_scope(self) -> dict[str, Any] | None:
        """What the person asked this run to look at: meters, times, a focus."""

        return task_scope(self.root)

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

    def facts(self) -> list[SiteFact] | None:
        path = self.path("facts.json")
        if not path.is_file():
            return None
        return [SiteFact.model_validate(item) for item in read_json(path)["facts"]]

    def tariff(self) -> TariffSchedule | None:
        """The tariffs resolved when the inputs were parsed; None without a tariff."""

        if self.manifest.tariff is None:
            return None
        if getattr(self, "_tariff", None) is None:
            path = self.path("tariff.json")
            if not path.is_file():
                raise RunDataError("the run's tariff has not been prepared")
            data = read_json(path)
            self._tariff = TariffSchedule(
                [TariffSpec.model_validate(item) for item in data["tariffs"]], data["currency"]
            )
        return self._tariff

    def segments(self) -> list[Segment] | None:
        """The state axis cut from the production records; None without them."""

        path = self.path("states.json")
        if not path.is_file():
            return None
        if getattr(self, "_segments", None) is None:
            self._segments = [
                Segment(
                    start=pd.Timestamp(item["start"]).to_pydatetime(),
                    end=pd.Timestamp(item["end"]).to_pydatetime(),
                    running=frozenset(item["running"]),
                    unknown=frozenset(item["unknown"]),
                    recorded=item["recorded"],
                )
                for item in read_json(path)["segments"]
            ]
        return self._segments

    def manifest_origin(self) -> dict[str, Any] | None:
        """How the manifest came about when the model drafted it; None when a person wrote it."""

        path = self.root / ORIGIN_FILE
        return read_json(path) if path.is_file() else None

    def topology(self) -> dict[str, Any] | None:
        """The meters' candidate rows in the asset list and nodes in the plant tree; None without either."""

        path = self.path("topology.json")
        return read_json(path) if path.is_file() else None

    def machine_meter(self, machine: str) -> str | None:
        for meter in self.manifest.meters:
            if machine in meter.production_machines:
                return meter.id
        return None

    def partitions(self) -> dict[str, list[str]]:
        value = read_json(self.path("partitions.json"))
        return {item["partition_id"]: item["meter_ids"] for item in value["partitions"]}

    def scope_meters(self, partition_id: str) -> list[str] | None:
        """The meters a scope may read: a partition's, or every meter for the plant-wide pass."""

        if partition_id == PLANT_SCOPE:
            return self.meter_ids()
        return self.partitions().get(partition_id)

    def assumptions(self) -> list[dict[str, Any]]:
        """The inferences about the data: written in the manifest, then drafted in the run."""

        origin = "manifest-drafted" if self.manifest_origin() else "manifest"
        listed = [
            {**entry.model_dump(mode="json"), "origin": origin, "meter_ids": [], "basis_fact_ids": [], "check_notes": []}
            for entry in self.manifest.assumptions
        ]
        path = self.path("assumptions.json")
        if path.is_file():
            listed += read_json(path)["drafted"]
        return listed


__all__ = [
    "ORIGIN_FILE",
    "PLANT_SCOPE",
    "DATA_DIR",
    "FROZEN_INPUTS",
    "RunDataError",
    "SiteData",
    "freeze_inputs",
    "narrowed",
    "task_scope",
    "write_task_scope",
    "read_json",
    "run_dir",
    "series_kind",
    "write_json",
]
