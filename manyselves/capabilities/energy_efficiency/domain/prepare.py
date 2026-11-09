"""Program steps before the model looks: parse, profile, prechecks, partitions."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .evidence import record_evidence
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
from .items import INPUT_KINDS, kb_categories
from .methods import (
    MENU,
    MethodContext,
    MethodParamsError,
    menu_entries,
    parse_params,
    readiness,
    resolved_params,
    run_method,
    top_occurrences,
)
from .profile import meter_profile, profile_text
from .states import COVERAGE_NOTE, meter_states, plant_summary, segments
from .store import SiteData, read_json, write_json

RULE_WINDOWS = 5


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
    return {
        "run_id": run_id,
        "site": manifest.site.name,
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


def method_context(site: SiteData) -> MethodContext:
    production = site.manifest.production
    return MethodContext(
        period=site.period(),
        calendar=site.calendar(),
        batches=site.batches(),
        machines={meter.id: list(meter.production_machines) for meter in site.manifest.meters},
        quantity_basis=production.quantity_basis if production else None,
        quantity_unit=production.quantity_unit if production else None,
        product_group_columns=tuple(production.product_group_columns) if production else (),
        segments=site.segments(),
    )


def method_readiness(site: SiteData, meter_ids: list[str]) -> list[dict[str, Any]]:
    """For each menu method and meter, whether the inputs could feed it (program decided)."""

    ctx = method_context(site)
    rows = []
    records = {meter_id: len(site.series(meter_id)) for meter_id in meter_ids}
    for spec in MENU.values():
        for meter_id in meter_ids:
            state, reason = readiness(spec, meter_id, site.spec(meter_id).has_energy, ctx, records[meter_id])
            rows.append({"method_id": spec.id, "meter_id": meter_id, "state": state, "reason": reason})
    return rows


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


def run_prechecks(workspace: Path, run_id: str) -> dict[str, Any]:
    """Run the methods a person listed in the manifest; every hit becomes a rule item."""

    site = SiteData(workspace, run_id)
    ctx = method_context(site)
    totals = meter_totals(site)
    items: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    for index, check in enumerate(site.manifest.prechecks, start=1):
        meters = site.meter_ids() if check.meters == "all" else list(check.meters)
        try:
            parse_params(check.method, check.params)
        except MethodParamsError as exc:
            outcomes.append({"precheck": index, "method_id": check.method, "outcome": "invalid", "reason": str(exc)})
            continue
        spec = MENU[check.method]
        for meter_id in meters:
            run = run_method(check.method, check.params, site.series(meter_id), ctx, totals.get(meter_id))
            entry: dict[str, Any] = {
                "precheck": index,
                "method_id": check.method,
                "meter_id": meter_id,
                "outcome": run.status,
            }
            if run.status != "ok":
                entry["reason"] = run.reason
                outcomes.append(entry)
                continue
            counted = [item for item in run.occurrences if item.hit] if run.summary["hits"] is not None else [
                item for item in run.occurrences if item.measure.records
            ]
            entry["hits"] = run.summary["hits"]
            entry["occurrences"] = run.summary["occurrences"]
            if not counted:
                entry["outcome"] = "none_found"
                outcomes.append(entry)
                continue
            largest = [item for item in top_occurrences(run, len(run.occurrences)) if item in counted]
            shown = sorted(largest[:RULE_WINDOWS], key=lambda item: item.measure.start)
            evidence_id = record_evidence(
                site,
                partition_id=None,
                tool="precheck",
                arguments={
                    "method_id": check.method,
                    "params": check.params,
                    "resolved_params": resolved_params(check.method, check.params),
                    "meter_id": meter_id,
                },
                meter_ids=[meter_id],
                result=run.as_dict(),
            )
            key = f"R{len(items) + 1}"
            hits = run.summary["hits"]
            if hits is None:
                counted_text = f"有数据的出现 {run.summary['with_data']} 次"
            else:
                counted_text = f"命中 {hits} 次"
                if run.summary.get("unknown"):
                    counted_text += f"，{run.summary['unknown']} 次没有数据、无法判断"
            items.append(
                {
                    "key": key,
                    "meter_ids": [meter_id],
                    "windows": [
                        {"start": _iso(item.measure.start), "end": _iso(item.measure.end)} for item in shown
                    ],
                    "finding_method": {
                        "method_id": check.method,
                        "params": check.params,
                        "description": f"manifest 预检第 {index} 条：{spec.title}",
                    },
                    "phenomenon": f"规则检出：{spec.title}，共 {run.summary['occurrences']} 次出现，{counted_text}。",
                    "evidence_ids": [evidence_id],
                }
            )
            entry["outcome"] = "found"
            entry["rule_key"] = key
            outcomes.append(entry)
    write_json(site.path("prechecks.json"), {"items": items, "outcomes": outcomes})
    return {"run_id": run_id, "rule_items": len(items), "outcomes": outcomes}


def partition_meters(workspace: Path, run_id: str, max_meters: int) -> dict[str, Any]:
    """Group meters by their declared group, at most ``max_meters`` per partition."""

    site = SiteData(workspace, run_id)
    groups: dict[str, list[str]] = {}
    for spec in site.manifest.meters:
        groups.setdefault(spec.group or "", []).append(spec.id)
    partitions = []
    for group, meter_ids in groups.items():
        chunks = [meter_ids[i : i + max_meters] for i in range(0, len(meter_ids), max_meters)]
        for number, chunk in enumerate(chunks, start=1):
            label = group or "未分组"
            if len(chunks) > 1:
                label = f"{label}（{number}/{len(chunks)}）"
            partitions.append(
                {"partition_id": f"P{len(partitions) + 1:02d}", "label": label, "meter_ids": chunk}
            )
    write_json(site.path("partitions.json"), {"partitions": partitions})
    briefs = [partition_brief(site, partition) for partition in partitions]
    return {"run_id": run_id, "partitions": briefs}


def partition_brief(site: SiteData, partition: dict[str, Any]) -> dict[str, Any]:
    profiles = {
        item["meter_id"]: item for item in read_json(site.path("profiles.json"))["profiles"]
    }
    prechecks = read_json(site.path("prechecks.json"))
    meter_ids = partition["meter_ids"]
    rule_items = [
        {
            "rule_key": item["key"],
            "meter_ids": item["meter_ids"],
            "method_id": item["finding_method"]["method_id"],
            "params": item["finding_method"]["params"],
            "phenomenon": item["phenomenon"],
        }
        for item in prechecks["items"]
        if set(item["meter_ids"]) & set(meter_ids)
    ]
    start, end = site.period()
    calendar = site.calendar()
    batches = site.batches()
    machines = [machine for meter_id in meter_ids for machine in site.spec(meter_id).production_machines]
    available = {
        "calendar_states": sorted({window.state for window in calendar}) if calendar else [],
        "production_machines": machines if batches is not None else [],
        "production_quantity_basis": site.manifest.production.quantity_basis if site.manifest.production else None,
        "tariff": site.manifest.tariff is not None,
    }
    return {
        "scope": f"{site.run_id}#{partition['partition_id']}",
        "partition_id": partition["partition_id"],
        "label": partition["label"],
        "site": site.manifest.site.name,
        "period": {"start": _iso(start), "end": _iso(end)},
        "meters": [
            {
                "meter_id": meter_id,
                "name": site.spec(meter_id).name,
                "unit_kind": site.spec(meter_id).unit_kind,
                "interval_minutes": site.spec(meter_id).interval_minutes,
                "production_machines": site.spec(meter_id).production_machines,
            }
            for meter_id in meter_ids
        ],
        "available_inputs": available,
        "methods": menu_entries(),
        "method_readiness": method_readiness(site, meter_ids),
        "rule_items": rule_items,
        "profile_text": "\n\n".join(profile_text(profiles[meter_id]) for meter_id in meter_ids),
        "site_facts": [
            {
                "id": fact.id,
                "topic": fact.topic,
                "question": fact.question,
                "answer": fact.answer,
                "status": fact.status,
                "confirmed": fact.confirmed,
            }
            for fact in site.facts() or []
        ],
        "assumptions": brief_assumptions(site),
        "categories": kb_categories(),
        "input_kinds": INPUT_KINDS,
    }


def brief_assumptions(site: SiteData) -> list[dict[str, Any]]:
    return [
        {key: entry[key] for key in ("id", "topic", "inference", "basis", "origin", "meter_ids") if key in entry}
        for entry in site.assumptions()
    ]


__all__ = [
    "meter_totals",
    "method_context",
    "method_readiness",
    "parse_inputs",
    "partition_brief",
    "partition_meters",
    "profile_meters",
    "run_prechecks",
]
