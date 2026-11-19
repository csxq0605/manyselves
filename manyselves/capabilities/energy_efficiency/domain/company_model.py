"""A03: the company model the program builds from the read files (EveryMe 能效公司模型扩充定义).

Meters measure and assets use energy, so the two are separate objects joined
by ``meters`` links.  Every link and attribute that matters says how it came
about (basis) and how sure it is (status).  The program only registers what
the files and the manifest say; whatever needs a person stays CANDIDATE and
becomes a data gap with the question to ask, until a confirmation (A05)
settles it.  Nothing here reads meaning from words: categories are copied
from the asset list as candidates, and which meter serves which machines is
known only from a confirmation.

Objects with many rows (batches, transitions, calendar days) live in the
foundation tables; the model points at them by table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from .inputs import TariffFileSpec
from .preparation import (
    CONFIRMATION_KINDS,
    ROLES,
    Confirmation,
    ParameterSet,
    day_range,
    source_ids,
)
from .store import SiteData

SCHEMA = "manyselves.energy.company-model.1"


def value(
    content: Any,
    *,
    unit: str | None = None,
    basis: str,
    status: str,
    source_refs: list[dict[str, Any]] | None = None,
    limits: list[str] | None = None,
    assumption_ids: list[str] | None = None,
    derivation: str | None = None,
) -> dict[str, Any]:
    """A value in its wrapper: what it is, where it came from, how sure it is."""

    wrapped: dict[str, Any] = {"value": content, "unit": unit, "basis": basis, "status": status}
    if source_refs:
        wrapped["source_refs"] = source_refs
    if limits:
        wrapped["limits"] = limits
    if assumption_ids:
        wrapped["assumption_ids"] = sorted(set(assumption_ids))
    if derivation:
        wrapped["derivation"] = derivation
    return wrapped


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else pd.Timestamp(moment).isoformat(sep=" ")


def _ref(sources: dict[str, str], file: str, **where: Any) -> dict[str, Any]:
    return {"source_id": sources.get(file), "file": file, **{key: item for key, item in where.items() if item is not None}}


class _Confirmed:
    """The confirmations of one run, looked up by kind and subject, with what each did."""

    def __init__(self, site: SiteData) -> None:
        loaded = site.confirmations()
        self.entries: list[Confirmation] = list(loaded.confirmations) if loaded else []
        self.used: dict[int, list[str]] = {}

    def of(self, kind: str, subject: str | None = None) -> list[tuple[int, Confirmation]]:
        return [
            (number, entry)
            for number, entry in enumerate(self.entries)
            if entry.kind == kind and (subject is None or entry.subject == subject)
        ]

    def note(self, number: int, effect: str) -> None:
        self.used.setdefault(number, []).append(effect)

    def record(self) -> dict[str, Any]:
        applied, unmatched = [], []
        for number, entry in enumerate(self.entries):
            row = {
                "number": number + 1,
                "kind": entry.kind,
                "kind_label": CONFIRMATION_KINDS[entry.kind],
                "subject": entry.subject,
                "result": entry.result,
                "confirmed_by": entry.confirmed_by,
                "date": entry.date.isoformat() if entry.date else None,
                "basis": entry.basis,
                "note": entry.note,
            }
            if number in self.used:
                applied.append({**row, "effects": self.used[number]})
            else:
                unmatched.append({**row, "effects": ["没有对上本次资料里的对象，未使用"]})
        return {"applied": applied, "unmatched": unmatched}


def _status_of(entries: list[tuple[int, Confirmation]], confirmed: _Confirmed, effect: str, default: str) -> tuple[str, str | None]:
    """The status a confirmation gives (the last one wins), and who gave it."""

    if not entries:
        return default, None
    number, entry = entries[-1]
    confirmed.note(number, effect)
    status = {"confirmed": "CONFIRMED", "rejected": "REJECTED", "corrected": "CONFIRMED"}[entry.result]
    return status, entry.confirmed_by


GAP_ORDER = (
    "calendar",
    "meter_scope",
    "unmetered_machine",
    "meter_topology",
    "tariff",
    "tariff_binding",
    "service",
    "quantity",
    "rated_power",
    "phase",
    "stop_times",
    "flat_meter",
    "maintenance",
    "environment",
)


def build_registry(
    site: SiteData,
    params: ParameterSet,
    source_register: list[dict[str, Any]],
    flat_meters: list[str],
) -> dict[str, Any]:
    """The static objects, links, inferences and gaps; batches and other many-row objects are in the tables."""

    manifest = site.manifest
    sources = source_ids(source_register)
    confirmed = _Confirmed(site)
    topology = site.topology() or {}
    places = {entry["meter_id"]: entry for entry in topology.get("meters", [])}
    origin = site.manifest_origin()
    batches = site.batches() or []
    machines_in_records = sorted({batch.machine for batch in batches if batch.machine})
    machine_meter = {machine: meter.id for meter in manifest.meters for machine in meter.production_machines}
    start, end = site.period()
    objects: dict[str, list[dict[str, Any]]] = {}
    links: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []

    def gap(kind: str, what: str, question: str, ask: str, affects: list[str], unlocks: str, objects_: list[str]) -> None:
        gaps.append(
            {
                "kind": kind,
                "what": what,
                "question": question,
                "ask": ask,
                "ask_label": ROLES[ask],
                "affects": affects,
                "unlocks": unlocks,
                "objects": objects_,
            }
        )

    objects["Site"] = [
        {
            "id": f"Site:{manifest.site.name}",
            "name": manifest.site.name,
            "timezone": value(manifest.site.timezone or "站点本地时间", basis="REGISTERED", status="CONFIRMED"),
            "period": {"start": _iso(start), "end": _iso(end)},
        }
    ]

    # Electricity accounts and tariff periods -----------------------------------
    tariff = site.tariff()
    accounts: list[dict[str, Any]] = []
    if isinstance(manifest.tariff, TariffFileSpec):
        accounts.append(
            {
                "id": f"ElectricityAccount:{manifest.tariff.plan}",
                "plan": value(manifest.tariff.plan, basis="REGISTERED", status="CONFIRMED", source_refs=[_ref(sources, manifest.tariff.file)]),
            }
        )
    elif manifest.tariff is not None:
        accounts.append({"id": "ElectricityAccount:TARIFF", "plan": value("manifest 里写的电价", basis="REGISTERED", status="CONFIRMED")})
    objects["ElectricityAccount"] = accounts
    periods = []
    if tariff is not None:
        for number, item in enumerate(tariff.tariffs, start=1):
            for period in item.periods:
                periods.append(
                    {
                        "id": f"TariffPeriod:{number}:{period.name}:{period.start}",
                        "name": period.name,
                        "clock": f"{period.start}-{period.end}",
                        "price": value(period.price, unit=f"{tariff.currency}/kWh", basis="REGISTERED", status="CONFIRMED"),
                        "valid_from": _iso(item.valid_from),
                        "valid_until": _iso(item.valid_until),
                    }
                )
    objects["TariffPeriod"] = periods
    if tariff is None:
        gap(
            "tariff",
            "电价（分时时段和单价）",
            "这段时间各表适用的分时电价时段和单价是什么？",
            "energy_finance",
            ["TOU"],
            "各表按电价时段的电量分布",
            [],
        )

    # Meters, assets and the links between them ------------------------------------
    meters = []
    assets: list[dict[str, Any]] = []
    for spec in manifest.meters:
        quality = site.quality(spec.id)
        source = _ref(sources, spec.file, sheet=spec.sheet, filter=f"{spec.filter_column}={spec.filter_value}" if spec.filter_column else None)
        meters.append(
            {
                "id": f"Meter:{spec.id}",
                "meter_id": spec.id,
                "name": spec.name,
                "semantics": value(spec.semantics, basis="REGISTERED", status="CANDIDATE" if origin else "CONFIRMED", source_refs=[source]),
                "unit": value(spec.unit, basis="REGISTERED", status="CANDIDATE" if origin else "CONFIRMED"),
                "interval_minutes": value(spec.interval_minutes, unit="min", basis="REGISTERED", status="CONFIRMED"),
                "coverage": {"first": quality.first, "last": quality.last, "records": quality.records},
                "group": spec.group,
            }
        )
        place = places.get(spec.id, {})
        if spec.production_machines:
            boundary_entries = confirmed.of("meter_asset", spec.id)
            status, by = _status_of(
                boundary_entries, confirmed, f"表 {spec.id} 的计量对象", "CANDIDATE" if origin else "PARTIAL"
            )
            boundary = next((entry.boundary for _n, entry in reversed(boundary_entries) if entry.boundary), None)
            for machine in spec.production_machines:
                links.append(
                    {
                        "type": "meters",
                        "from": f"Meter:{spec.id}",
                        "to": f"Asset:{machine}",
                        "basis": "REGISTERED",
                        "status": status,
                        "boundary": boundary or "未知（主机或含辅机没人确认）",
                        "confirmed_by": by,
                        "note": "manifest 里由模型起草" if origin else "manifest 里登记",
                    }
                )
            if status != "CONFIRMED":
                gap(
                    "meter_scope",
                    f"表 {spec.id} 的计量范围",
                    f"表 {spec.id}（{spec.name}）计的是机台 {'、'.join(spec.production_machines)} 的哪些设备（只有主机，还是含辅机）？有没有别的负荷接在上面？",
                    "metering",
                    [f"TASK.{machine}" for machine in spec.production_machines] + [f"XS.{spec.id}", "GRP"],
                    "批次单耗、批内低功率和批间功率按确认的计量范围解释",
                    [spec.id, *spec.production_machines],
                )
        else:
            asset_id = f"Asset:{spec.id}"
            assets.append(
                {
                    "id": asset_id,
                    "kind": "metered_object",
                    "name": value(place.get("equipment") or spec.name, basis="DERIVED", status="CANDIDATE", derivation="台账关联设备或表名"),
                    "category": value(place.get("category"), basis="REGISTERED", status="CANDIDATE", derivation="台账用能类别（按编号或名称对上的候选行）")
                    if place.get("category")
                    else None,
                    "tree_path": place.get("tree_path"),
                }
            )
            status, by = _status_of(confirmed.of("meter_asset", spec.id), confirmed, f"表 {spec.id} 的计量对象", "CANDIDATE")
            links.append(
                {
                    "type": "meters",
                    "from": f"Meter:{spec.id}",
                    "to": asset_id,
                    "basis": "DERIVED",
                    "status": status,
                    "boundary": "按表名，没人确认",
                    "confirmed_by": by,
                    "note": "没挂机台的表，对象按表名和台账候选登记",
                }
            )
            service = confirmed.of("service", spec.id)
            if service:
                status, by = _status_of(service, confirmed, f"表 {spec.id} 的服务对象", "CANDIDATE")
                entry = service[-1][1]
                served = machines_in_records if entry.serves == ["ALL"] else entry.serves
                objects.setdefault("UtilitySystem", []).append(
                    {"id": f"UtilitySystem:{spec.id}", "asset": asset_id, "serves": served, "status": status, "confirmed_by": by}
                )
                for machine in served:
                    links.append(
                        {"type": "serves", "from": asset_id, "to": f"Asset:{machine}", "basis": "USER_CONFIRMED", "status": status, "confirmed_by": by}
                    )
                if entry.minimum_kw is not None and entry.result != "rejected":
                    objects.setdefault("Constraint", []).append(
                        {
                            "id": f"Constraint:min-service:{spec.id}",
                            "type": "必要最低服务",
                            "applies_to": [asset_id],
                            "states": ["ALL_IDLE"],
                            "value": value(entry.minimum_kw, unit="kW", basis="USER_CONFIRMED", status="CONFIRMED"),
                            "confirmed_by": entry.confirmed_by,
                            "basis_note": entry.basis,
                        }
                    )
            else:
                gap(
                    "service",
                    f"表 {spec.id} 服务哪些对象",
                    f"表 {spec.id}（{spec.name}）给哪些机台或区域供能？全厂停产时必须保持的最低负荷大约多少？",
                    "equipment",
                    [f"DF.{spec.id}", f"DFS.{spec.id}", f"SL.{spec.id}"],
                    "需求跟随按确认的服务对象计算；停产时的负荷能和必要最低服务比",
                    [spec.id],
                )
    for machine in machines_in_records:
        commissioning = confirmed.of("commissioning", machine)
        status, by = _status_of(commissioning, confirmed, f"机台 {machine} 是否投用", "CONFIRMED")
        commissioned = commissioning[-1][1].commissioned if commissioning and commissioning[-1][1].result != "rejected" else True
        meter_id = machine_meter.get(machine)
        assets.append(
            {
                "id": f"Asset:{machine}",
                "kind": "production_machine",
                "name": machine,
                "commissioned": value(commissioned, basis="USER_CONFIRMED" if by else "DERIVED", status=status, derivation=None if by else "生产记录里有批次"),
                "meter": meter_id,
            }
        )
        if meter_id is None:
            gap(
                "unmetered_machine",
                f"机台 {machine} 没有对应的表",
                f"机台 {machine} 的用电计在哪块表里？还是没有单独计量？",
                "metering",
                ["PLANT", f"TASK.{machine}"],
                f"机台 {machine} 的批次电量和单耗",
                [machine],
            )
    objects["Meter"] = meters
    objects["Asset"] = assets
    objects.setdefault("UtilitySystem", [])
    objects.setdefault("Constraint", [])

    for account in accounts:
        bindings = confirmed.of("tariff_binding")
        for spec in manifest.meters:
            own = [item for item in bindings if item[1].subject in (spec.id, "ALL")]
            status, by = _status_of(own, confirmed, f"表 {spec.id} 的电价绑定", "CANDIDATE")
            links.append(
                {
                    "type": "in_account",
                    "from": f"Meter:{spec.id}",
                    "to": account["id"],
                    "basis": "USER_CONFIRMED" if by else "ASSUMED",
                    "status": status,
                    "confirmed_by": by,
                    "note": None if by else "manifest 只写了一个电价，按全部表都适用它计",
                }
            )
    if accounts and not confirmed.of("tariff_binding"):
        gap(
            "tariff_binding",
            "各表的电价方案和户号",
            "各表分别属于哪个户号、执行哪个电价方案？",
            "energy_finance",
            ["TOU", "PK"],
            "按户号分开的时段电量和需量",
            [],
        )

    topology_entries = confirmed.of("meter_topology")
    for number, entry in topology_entries:
        confirmed.note(number, f"表 {entry.subject} 的下级：{'、'.join(entry.children)}")
        for child in entry.children:
            links.append(
                {"type": "contains", "from": f"Meter:{entry.subject}", "to": f"Meter:{child}", "basis": "USER_CONFIRMED", "status": "CONFIRMED" if entry.result != "rejected" else "REJECTED", "confirmed_by": entry.confirmed_by}
            )
    if not topology_entries and len(manifest.meters) > 1:
        gap(
            "meter_topology",
            "表之间的上下级",
            "这些表之间有没有总表和分表的关系？哪块表包含哪几块？",
            "metering",
            ["PK", "PKC", "MX"],
            "全厂功率和需量不重复计；各表电量能不能相加",
            [],
        )

    # Products, process steps --------------------------------------------------------
    production = manifest.production
    products: dict[str, int] = {}
    for batch in batches:
        products[batch.product or "未写产品"] = products.get(batch.product or "未写产品", 0) + 1
    objects["Product"] = [
        {"id": f"Product:{name}", "name": name, "batches": count, "basis": "REGISTERED"} for name, count in sorted(products.items())
    ]
    if production is not None and production.phase_start_column:
        objects["ProcessStep"] = [
            {"id": "ProcessStep:prepare", "name": "准备段", "span": f"[{production.start_column}, {production.phase_start_column})", "basis": "REGISTERED"},
            {"id": "ProcessStep:process", "name": "加工段", "span": f"[{production.phase_start_column}, {production.end_column})", "basis": "REGISTERED"},
        ]
    elif production is not None:
        objects["ProcessStep"] = [
            {"id": "ProcessStep:batch", "name": "批次", "span": f"[{production.start_column}, {production.end_column})", "basis": "REGISTERED"}
        ]
        gap(
            "phase",
            "批次内准备（清机、预热）和正式加工的分界",
            "生产记录里有没有每批正式开始加工的时刻？",
            "production",
            ["TR", "XS"],
            "批次分成准备段和加工段，换线准备的电量能单独看",
            [],
        )
    else:
        objects["ProcessStep"] = []
    if production is not None and not production.quantity_basis:
        gap(
            "quantity",
            "批次数量的口径",
            "生产记录的数量列是投入量、成品量还是合格品量？",
            "production",
            ["TASK", "GRP"],
            "批次单耗和可比组",
            [],
        )
    if production is not None and production.stop_hours_columns:
        gap(
            "stop_times",
            "停机的起止时刻",
            "批次里登记的停机和换线，能不能给出每次的起止时刻（或班组记录）？",
            "production",
            ["STOP", "XS"],
            "批内低功率能和登记的停机对上",
            [],
        )
    if params.entry("band_reference_quantile").basis in {"ASSUMED", "CONVENTION"} and machine_meter:
        gap(
            "rated_power",
            "产线的额定功率或正常加工功率",
            "各条产线主机的额定功率、正常加工时的功率大约多少？",
            "process",
            ["XS", "PLANT", "DAY"],
            "功率档按设备参数划分，不再按数据分位数假设",
            sorted(machine_meter),
        )
    if not site.calendar() and not confirmed.of("calendar"):
        gap(
            "calendar",
            "实际停产和加班日历",
            "数据期内哪些天全厂停产、哪些天加班？",
            "production",
            ["DAY", "PLANT"],
            "开工日和休息日按确认的日历定，不再按数据推断",
            [],
        )
    for meter_id in flat_meters:
        gap(
            "flat_meter",
            f"表 {meter_id} 整期没有用电",
            f"表 {meter_id} 整个数据期读数不变或为 0：是没投用、停用，还是没接入采集？",
            "metering",
            [f"DQ.{meter_id}"],
            "判断这块表的数据能不能用",
            [meter_id],
        )
    gap(
        "maintenance",
        "检修、维护和改造的日期",
        "数据期内各设备有没有检修、维护或改造？分别在哪天？",
        "equipment",
        ["SL", "TRS"],
        "前后对比时能分开变化前后",
        [],
    )
    gap(
        "environment",
        "环境数据（室外温度、湿度）",
        "能不能提供数据期内逐时的室外温度和湿度（或厂内气象站记录）？",
        "data",
        ["DFS", "SL"],
        "受天气影响的负荷能按条件归一后再比",
        [],
    )
    objects["MaintenanceEvent"] = []
    objects["EnvironmentSeries"] = []

    # Facts and inferences ----------------------------------------------------------
    facts = []
    if manifest.site_facts is not None:
        for fact in site.facts() or []:
            facts.append(
                {
                    "id": f"Fact:{fact.id}",
                    "topic": fact.topic,
                    "answer": fact.answer,
                    "basis": "SOURCE_REPORTED",
                    "status": "CONFIRMED" if fact.confirmed else "CANDIDATE",
                    "source_refs": [_ref(sources, manifest.site_facts.file, row=fact.row)],
                }
            )
    objects["Fact"] = facts
    inferences = []
    for entry in manifest.assumptions:
        own = confirmed.of("assumption", entry.id)
        status, by = _status_of(own, confirmed, f"推断 {entry.id}", "CANDIDATE")
        inferences.append(
            {
                "id": entry.id,
                "topic": entry.topic,
                "inference": entry.inference,
                "basis_text": entry.basis,
                "origin": "manifest-drafted" if origin else "manifest",
                "status": status,
                "confirmed_by": by,
                "result_note": own[-1][1].note or own[-1][1].basis if own else None,
            }
        )
    parameter_ids = {item["id"] for item in params.as_list()}
    for number, entry in enumerate(confirmed.entries):
        if entry.kind == "assumption" and entry.subject in parameter_ids:
            confirmed.note(number, f"现场假设 {entry.subject}：{entry.result}")
        if entry.kind == "parameter" and entry.subject in {item["key"] for item in params.as_list()}:
            confirmed.note(number, f"参数 {entry.subject} 取 {entry.value}")
        if entry.kind == "calendar":
            first, last = day_range(entry.subject)
            if pd.Timestamp(last) >= pd.Timestamp(start).normalize() and pd.Timestamp(first) < pd.Timestamp(end):
                confirmed.note(number, f"{entry.subject} 记为 {entry.day_status}")
        if entry.kind == "peer_group":
            confirmed.note(number, f"可比组 {entry.subject}：{entry.result}")
    ranked = sorted(gaps, key=lambda item: (GAP_ORDER.index(item["kind"]), item["what"]))
    for number, item in enumerate(ranked, start=1):
        item["id"] = f"G{number:02d}"
    return {
        "objects": objects,
        "links": links,
        "inferences": inferences,
        "gaps": ranked,
        "confirmations": confirmed.record(),
    }


def peer_group_status(site: SiteData, key: str) -> tuple[str, str | None]:
    """A comparable group is a candidate until a person confirms or rejects it."""

    loaded = site.confirmations()
    entries = [entry for entry in (loaded.confirmations if loaded else []) if entry.kind == "peer_group" and entry.subject == key]
    if not entries:
        return "CANDIDATE", None
    entry = entries[-1]
    return ("REJECTED" if entry.result == "rejected" else "CONFIRMED"), entry.confirmed_by


def task_record(site: SiteData) -> dict[str, Any]:
    """A01 as it stands, with what nobody wrote listed as 待定."""

    task = site.task()
    scope = site.task_scope() or {}
    if task is None:
        return {
            "given": False,
            "scope": scope,
            "pending": ["企业诉求", "结果使用者", "要支持的决策", "分析深度", "约束和资源", "审核与停止条件"],
        }
    return {
        "given": True,
        **task.model_dump(mode="json", exclude_none=True),
        "scope": scope,
        "pending": task.pending(),
    }


def brief_task(site: SiteData) -> dict[str, Any] | None:
    """A01 for a finder: what the task is for, in its own words; None when nobody wrote it."""

    task = site.task()
    if task is None:
        return None
    data = task.model_dump(mode="json", exclude_none=True)
    keep = ("request", "users", "decisions", "depth", "constraints", "focus", "stop_conditions")
    return {key: data[key] for key in keep if data.get(key)}


__all__ = ["SCHEMA", "brief_task", "build_registry", "peer_group_status", "task_record", "value"]
