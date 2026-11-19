"""Program steps before the foundation: parse the inputs and profile every meter."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .inputs import (
    file_sha256,
    load_batches,
    load_calendar,
    load_site_facts,
    manifest_files,
    normalize_meter,
    read_meter_table,
    tariff_schedule,
)
from .profile import meter_profile
from .states import COVERAGE_NOTE, meter_states, plant_summary, segments
from .store import SiteData, read_json, write_json
from .topology import build_topology


def parse_inputs(workspace: Path, run_id: str) -> dict[str, Any]:
    """Normalize every meter, the production records and the calendar."""

    site = SiteData(workspace, run_id)
    manifest = site.manifest
    write_json(
        site.path("sources.json"),
        {
            "files": [
                {"file": name, "sha256": file_sha256(site.inputs / name)}
                for name in ["manifest.yaml", *manifest_files(manifest)]
            ]
        },
    )
    meters = []
    firsts, lasts = [], []
    for spec in manifest.meters:
        frame = read_meter_table(site.inputs, spec)
        normalized, quality = normalize_meter(spec, frame, manifest.period)
        path = site.series_path(spec.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        normalized.to_csv(path, index=False, date_format="%Y-%m-%d %H:%M:%S")
        if quality.first and quality.last:
            firsts.append(pd.Timestamp(quality.first))
            lasts.append(pd.Timestamp(quality.last))
        meters.append(
            {
                "id": spec.id,
                "name": spec.name,
                "unit_kind": spec.unit_kind,
                "semantics": spec.semantics,
                "group": spec.group,
                "quality": quality.model_dump(mode="json"),
            }
        )
    if manifest.period is not None:
        period = {"start": _iso(manifest.period.start), "end": _iso(manifest.period.end), "declared": True}
    elif firsts:
        period = {"start": _iso(min(firsts)), "end": _iso(max(lasts)), "declared": False}
    else:
        period = None
    write_json(site.path("meters.json"), {"period": period, "meters": meters})

    production = None
    if manifest.production is not None:
        batches, skipped = load_batches(site.inputs, manifest.production)
        write_json(
            site.path("production.json"),
            {"batches": [batch.model_dump(mode="json") for batch in batches], "blank_rows_skipped": skipped},
        )
        production = {
            "batches": len(batches),
            "blank_rows_skipped": skipped,
            "unusable": sum(1 for batch in batches if batch.problem),
            "machines": sorted({batch.machine for batch in batches}),
        }
    calendar = None
    if manifest.calendar is not None:
        windows, unusable = load_calendar(site.inputs, manifest.calendar)
        write_json(
            site.path("calendar.json"),
            {"windows": [window.model_dump(mode="json") for window in windows], "unusable_rows": unusable},
        )
        calendar = {
            "windows": len(windows),
            "unusable_rows": unusable,
            "states": sorted({window.state for window in windows}),
        }
    facts = None
    if manifest.site_facts is not None:
        loaded = load_site_facts(site.inputs, manifest.site_facts)
        write_json(site.path("facts.json"), {"facts": [fact.model_dump(mode="json") for fact in loaded]})
        facts = {"facts": len(loaded), "confirmed": sum(1 for fact in loaded if fact.confirmed)}
    tariff = None
    schedule = tariff_schedule(site.inputs, manifest)
    if schedule is not None:
        write_json(site.path("tariff.json"), {"currency": schedule.currency, "tariffs": schedule.as_list()})
        tariff = {
            "tariffs": len(schedule.tariffs),
            "valid": [
                {"from": _iso(item.valid_from) if item.valid_from else None, "until": _iso(item.valid_until) if item.valid_until else None}
                for item in schedule.tariffs
            ],
        }
    topology = build_topology(site.inputs, manifest)
    if topology is not None:
        write_json(site.path("topology.json"), topology)
    return {
        "run_id": run_id,
        "site": manifest.site.name,
        "topology": None
        if topology is None
        else {"relations": len(topology["relations"]), "notes": len(topology["notes"])},
        "period": period,
        "meters": [
            {
                "id": meter["id"],
                "records": meter["quality"]["records"],
                "notes": len(site.quality(meter["id"]).notes()),
            }
            for meter in meters
        ],
        "production": production,
        "calendar": calendar,
        "tariff": tariff,
        "site_facts": facts,
        "assumptions": len(manifest.assumptions),
    }


def meter_totals(site: SiteData) -> dict[str, float | None]:
    profiles = read_json(site.path("profiles.json"))["profiles"]
    return {item["meter_id"]: item["period_energy_kwh"] for item in profiles}


def profile_meters(workspace: Path, run_id: str) -> dict[str, Any]:
    site = SiteData(workspace, run_id)
    period = site.period()
    batches = site.batches()
    pieces = None
    if batches is not None:
        pieces, meta = segments(batches, period)
        write_json(
            site.path("states.json"),
            {
                **meta,
                "note": COVERAGE_NOTE,
                "plant": plant_summary(pieces),
                "segments": [
                    {
                        "start": piece.start,
                        "end": piece.end,
                        "running": sorted(piece.running),
                        "unknown": sorted(piece.unknown),
                        "recorded": piece.recorded,
                    }
                    for piece in pieces
                ],
            },
        )
    profiles = []
    for spec in site.manifest.meters:
        series = site.series(spec.id)
        profile = meter_profile(spec, series, site.quality(spec.id), period)
        profile["states"] = (
            meter_states(series, pieces, list(spec.production_machines)) if pieces is not None else None
        )
        profiles.append(profile)
    write_json(site.path("profiles.json"), {"profiles": profiles})
    return {
        "run_id": run_id,
        "meters": [
            {
                "id": item["meter_id"],
                "period_energy_kwh": item["period_energy_kwh"],
                "period_avg_kw": item["period_avg_kw"],
                "period_coverage": item["period_coverage"],
            }
            for item in profiles
        ],
    }


def _iso(value: datetime) -> str:
    return pd.Timestamp(value).isoformat(sep=" ")


__all__ = ["meter_totals", "parse_inputs", "profile_meters"]
