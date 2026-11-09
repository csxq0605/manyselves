"""A manifest for a folder that has none: the model drafts it, the program tries it.

The program surveys the files (``survey``); the manifest drafter writes a
manifest from the survey, putting what it could not tell into the manifest's
assumptions.  The program then reads the data with it.  A file, column or
sheet that cannot be read blocks; readings that do not behave like the
declared kind (a cumulative meter whose readings often go down, interval
values that only ever grow), spacing that does not fit the declared
interval, meters of a long table left out and machines no batch names are
sent back once.  After that round the program keeps the corrected draft if
it reads, else the first one if it reads, and stops the run if neither does;
what is still odd is listed for the person with the draft's origin.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .inputs import (
    MANIFEST_NAME,
    InputFormatError,
    Manifest,
    _text,
    load_batches,
    load_calendar,
    load_site_facts,
    manifest_files,
    normalize_meter,
    parse_numbers,
    read_meter_table,
    read_table,
    safe_relative,
    tariff_schedule,
)
from .store import FROZEN_INPUTS, ORIGIN_FILE, run_dir, write_json
from .survey import survey_folder
from .topology import build_topology

MANIFEST_KEY = "MANIFEST"
SURVEY_FILE = "survey.json"


def _frozen(workspace: Path, run_id: str) -> Path:
    return run_dir(workspace, run_id) / FROZEN_INPUTS


def survey_brief(workspace: Path, run_id: str) -> dict[str, Any]:
    """The drafter's brief when the frozen inputs have no manifest; no brief when they have one."""

    folder = _frozen(workspace, run_id)
    if (folder / MANIFEST_NAME).is_file():
        return {"run_id": run_id, "briefs": []}
    survey = survey_folder(folder)
    write_json(run_dir(workspace, run_id) / SURVEY_FILE, survey)
    brief = {"key": MANIFEST_KEY, "run_id": run_id, **survey}
    return {"run_id": run_id, "briefs": [brief]}


def _never_down(values: Any) -> float | None:
    numbers = parse_numbers(values).dropna()
    if len(numbers) < 10:
        return None
    return float((numbers.diff().dropna() >= 0).mean())


def check_manifest(folder: Path, manifest: Manifest) -> dict[str, Any]:
    """Read the data with the manifest: what blocks, what to send back once, what to tell the person."""

    blocking: list[str] = []
    issues: list[str] = []
    warnings: list[str] = []
    for name in manifest_files(manifest):
        try:
            safe_relative(folder, name)
        except InputFormatError as exc:
            blocking.append(str(exc))
    if blocking:
        return {"ok": False, "blocking": blocking, "issues": issues, "warnings": warnings}
    used: dict[tuple[str, str | None, str], set[str]] = {}
    readable = 0
    for spec in manifest.meters:
        try:
            frame = read_meter_table(folder, spec)
            _normalized, quality = normalize_meter(spec, frame, manifest.period)
        except (InputFormatError, ValueError) as exc:
            blocking.append(f"表 {spec.id}：{exc}")
            continue
        if spec.filter_column:
            used.setdefault((spec.file, spec.sheet, spec.filter_column), set()).add(spec.filter_value or "")
        if quality.records == 0:
            issues.append(f"表 {spec.id}：按这份写法读不出任何记录")
            continue
        readable += 1
        share = _never_down(frame[spec.value_column])
        if spec.semantics == "cumulative_energy" and share is not None and share < 0.9:
            issues.append(f"表 {spec.id}：写成 cumulative_energy，但相邻读数只有 {share:.0%} 不下降，不像累计读数")
        if spec.semantics != "cumulative_energy" and share is not None and share >= 0.99:
            if parse_numbers(frame[spec.value_column]).dropna().nunique() > 10:
                issues.append(f"表 {spec.id}：写成 {spec.semantics}，但相邻读数 {share:.0%} 不下降、一路变大，像累计读数")
        spacing = quality.spacing_note()
        if spacing:
            issues.append(f"表 {spec.id}：{spacing}")
        warnings += [f"表 {spec.id}：{note}" for note in quality.notes() if note != spacing]
    if not readable and not blocking:
        blocking.append("没有一块表读得出记录")
    for key, values in used.items():
        name, _sheet, column = key
        source = next(meter for meter in manifest.meters if (meter.file, meter.sheet, meter.filter_column) == key)
        try:
            frame = read_table(folder, source, [column])
        except InputFormatError:
            continue
        present = sorted({_text(value) for value in frame[column]} - {""})
        left = [value for value in present if value not in values]
        if left:
            issues.append(f"{name} 的 {column} 列还有这些值没有写成表：" + "、".join(left[:30]))
    if manifest.production is not None:
        try:
            batches, _skipped = load_batches(folder, manifest.production)
        except (InputFormatError, ValueError) as exc:
            blocking.append(f"生产记录：{exc}")
        else:
            if not batches:
                issues.append("生产记录按这份写法读不出批次")
            else:
                broken = sum(1 for batch in batches if batch.problem)
                if broken > len(batches) / 2:
                    issues.append(f"生产记录 {len(batches)} 批里 {broken} 批的开始或结束时间读不出或颠倒")
                named = {batch.machine for batch in batches}
                mapped = [machine for meter in manifest.meters for machine in meter.production_machines]
                missing = sorted(set(mapped) - named)
                if missing:
                    issues.append("production_machines 写了生产记录里没有的机台：" + "、".join(missing))
    loaders = [
        ("日历", manifest.calendar, lambda: load_calendar(folder, manifest.calendar)),
        ("电价", manifest.tariff, lambda: tariff_schedule(folder, manifest)),
        ("现场调研", manifest.site_facts, lambda: load_site_facts(folder, manifest.site_facts)),
        ("台账和工厂树", manifest.assets or manifest.plant_tree, lambda: build_topology(folder, manifest)),
    ]
    for label, spec, load in loaders:
        if spec is None:
            continue
        try:
            load()
        except (InputFormatError, ValueError, KeyError) as exc:
            blocking.append(f"{label}：{exc}")
    return {"ok": not blocking and not issues, "blocking": blocking, "issues": issues, "warnings": warnings}


def _parsed(value: dict[str, Any]) -> tuple[Manifest | None, str | None]:
    try:
        return Manifest.model_validate(value), None
    except ValidationError as exc:
        return None, str(exc)


def try_manifest(workspace: Path, run_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    parsed, error = _parsed(manifest)
    if parsed is None:
        return {"run_id": run_id, "ok": False, "blocking": [error], "issues": [], "warnings": []}
    return {"run_id": run_id, **check_manifest(_frozen(workspace, run_id), parsed)}


def settle_manifest(
    workspace: Path, run_id: str, first: dict[str, Any], corrected: dict[str, Any] | None
) -> dict[str, Any]:
    """Write the corrected draft if it reads, else the first; stop the run if neither does."""

    folder = _frozen(workspace, run_id)
    attempts = [("corrected", corrected), ("first", first)] if corrected else [("first", first)]
    last: dict[str, Any] = {}
    for label, value in attempts:
        parsed, error = _parsed(value)
        check = {"ok": False, "blocking": [error], "issues": [], "warnings": []} if parsed is None else check_manifest(folder, parsed)
        if parsed is not None and not check["blocking"]:
            data = parsed.model_dump(mode="json", exclude_defaults=True)
            (folder / MANIFEST_NAME).write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
            origin = {"origin": "drafted", "kept": label, "issues": check["issues"], "warnings": check["warnings"]}
            write_json(run_dir(workspace, run_id) / ORIGIN_FILE, origin)
            return {"run_id": run_id, "ok": True, "kept": label, "meters": len(parsed.meters), **check}
        last = check
    raise InputFormatError("模型起草的读法读不出数据：" + "；".join(last.get("blocking") or ["无"]))


__all__ = ["MANIFEST_KEY", "check_manifest", "settle_manifest", "survey_brief", "try_manifest"]
