"""The plant-wide pass: one look across every meter after the partitions.

A partition sees only its own meters, so what shows only between objects
(a utility running while every production line is stopped, two meters
running in parallel, a line idle while the next one waits) falls between
partitions.  The plant-wide pass reads a compact picture of every meter, the
plant's state axis and what each partition proposed, adds items across
objects and links items of different partitions.  It never drops or
re-ranks a partition's item.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .items import INPUT_KINDS, PLANT, PartitionSubmission, kb_categories
from .methods import menu_entries
from .prepare import _iso, brief_assumptions, method_readiness
from .profile import _num
from .states import STATES, state_label
from .store import SiteData, read_json, write_json

PHENOMENON_CHARS = 160


def _meter_line(profile: dict[str, Any]) -> str:
    parts = [f"{profile['meter_id']} {profile['name']}（分组 {profile['group'] or '未分组'}"]
    if profile["production_machines"]:
        parts[0] += f"，机台 {'、'.join(profile['production_machines'])}"
    parts[0] += "）"
    if profile["period_energy_kwh"] is not None:
        parts.append(f"全期 {_num(profile['period_energy_kwh'])} kWh")
    if profile["period_avg_kw"] is not None:
        parts.append(f"平均 {_num(profile['period_avg_kw'])} kW")
    parts.append(f"覆盖 {profile['period_coverage']:.1%}")
    states = profile.get("states") or []
    if states:
        has_machines = bool(profile["production_machines"])
        parts.append(
            "状态："
            + "；".join(
                f"{row['state']}（{state_label(row['state'], has_machines)}）{row['hours']}h"
                + (f" {row['energy_kwh']} kWh" if row["energy_kwh"] is not None else "")
                + (f" 均 {row['avg_kw']} kW" if row["avg_kw"] is not None else "")
                for row in states
            )
        )
    for name, title in (("weekday", "工作日"), ("weekend", "周末")):
        values = profile["hour_of_day_kw"][name]
        if any(value is not None for value in values):
            parts.append(f"{title}小时曲线 " + " ".join("-" if value is None else _num(value) for value in values))
    if profile["quality_notes"]:
        parts.append("数据说明：" + "；".join(profile["quality_notes"]))
    return "，".join(parts[:1]) + "：" + "；".join(parts[1:])


def _short(text: str) -> str:
    return text if len(text) <= PHENOMENON_CHARS else text[:PHENOMENON_CHARS] + "…"


def plant_brief(workspace: Path, run_id: str, results: dict[str, Any]) -> dict[str, Any]:
    """The brief of the plant-wide pass, and the partition items it may link to."""

    site = SiteData(workspace, run_id)
    start, end = site.period()
    profiles = read_json(site.path("profiles.json"))["profiles"]
    states = read_json(site.path("states.json")) if site.path("states.json").is_file() else None
    items = []
    gaps = []
    for partition_id, result in sorted(results.items()):
        submission = PartitionSubmission.model_validate(result["submission"])
        for item in submission.items:
            items.append(
                {
                    "ref": f"{partition_id}:{item.key}",
                    "meter_ids": item.meter_ids,
                    "method_id": item.finding_method.method_id,
                    "windows": [
                        {"start": _iso(window.start), "end": _iso(window.end)} for window in item.windows[:2]
                    ],
                    "window_count": len(item.windows),
                    "categories": [proposal.item_id for proposal in item.candidate_categories],
                    "phenomenon": _short(item.phenomenon),
                }
            )
        for entry in submission.category_coverage:
            if entry.outcome != "none_found":
                gaps.append(
                    {
                        "partition_id": partition_id,
                        "item_id": entry.item_id,
                        "meter_ids": entry.meter_ids,
                        "outcome": entry.outcome,
                        "missing_inputs": list(entry.missing_inputs),
                    }
                )
    write_json(site.path("plant.json"), {"item_refs": [item["ref"] for item in items]})
    rule_items = [
        {
            "rule_key": item["key"],
            "meter_ids": item["meter_ids"],
            "method_id": item["finding_method"]["method_id"],
            "params": item["finding_method"]["params"],
            "phenomenon": item["phenomenon"],
        }
        for item in read_json(site.path("prechecks.json"))["items"]
    ]
    machines = sorted({batch.machine for batch in site.batches() or []})
    mapped = {machine for meter in site.manifest.meters for machine in meter.production_machines}
    brief = {
        "scope": f"{run_id}#{PLANT}",
        "partition_id": PLANT,
        "label": "全厂关联",
        "site": site.manifest.site.name,
        "period": {"start": _iso(start), "end": _iso(end)},
        "meters": [
            {
                "meter_id": spec.id,
                "name": spec.name,
                "group": spec.group,
                "unit_kind": spec.unit_kind,
                "production_machines": spec.production_machines,
            }
            for spec in site.manifest.meters
        ],
        "plant_states": None
        if states is None
        else {
            "meaning": STATES,
            "coverage": states["coverage"],
            "plant": states["plant"],
            "uncertain_batches": states["uncertain_batches"],
            "unplaced_batches": states["unplaced_batches"],
            "machines_without_meter": sorted(set(machines) - mapped),
        },
        "meters_text": "\n".join(_meter_line(profile) for profile in profiles),
        "methods": menu_entries(),
        "method_readiness": method_readiness(site, site.meter_ids()),
        "partition_items": items,
        "partition_category_gaps": gaps,
        "rule_items": rule_items,
        "site_facts": [
            {"id": fact.id, "topic": fact.topic, "question": fact.question, "answer": fact.answer, "confirmed": fact.confirmed}
            for fact in site.facts() or []
        ],
        "assumptions": brief_assumptions(site),
        "categories": kb_categories(),
        "input_kinds": INPUT_KINDS,
    }
    focus = (site.task_scope() or {}).get("focus")
    if focus:
        brief["focus"] = focus
    return {"run_id": run_id, "briefs": [brief]}


__all__ = ["plant_brief"]
