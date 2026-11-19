"""The workbook (台账) of one run: candidates, screening, opinions and the pages that say what was not seen.

One row per candidate: what the lens compared (meter, hours, reference
levels), what the program computed (excess under each level, share, overlap,
decomposition), the screening and its rule, the model's opinions (marked
unverified) and the columns a person fills in (取舍 wins over the
screening).  Beside it:

- 覆盖与剩余: per meter, energy no lens looked at, excess found by screening
  result, and the residual nobody explained;
- 剩余片: the largest unexplained pieces;
- 触发与处置: where the program sent the model and what it answered;
- 规格: every spec, what the program computed from it, and the YAML a person
  can paste into the preferences (P39 / P40) to make it a rule;
- 饱和: lenses in the order they add the most, independent discoveries, the
  residual rounds;
- 现场清单: every site question;
- the company model and the preferences as before.
"""

from __future__ import annotations

import hashlib
import html
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from .analysis import looks_of, probe_results
from .foundation import load_model
from .ledger import Ledger, system_of
from .lenses import LENSES
from .model_sheets import add_model_sheets, model_html
from .preparation import PREFERENCES_BOOK, ROLES, write_preferences_book
from .probes import RESULT_LABELS
from .screen import INVALID, PENDING, RULES, VALID
from .specs import (
    SPEC_KINDS,
    DriverSpec,
    GroupingSpec,
    ReferenceSpec,
    ResidualSpec,
    basis_text,
    driver_fit,
    parse_spec,
    reference_candidates,
    residual_mask,
    rule_snippet,
)
from .store import SiteData, read_json, write_json
from .submissions import JudgmentSubmission

SCHEMA = "manyselves.energy.register.2"
DECISION_LABELS = {"keep": "保留", "drop": "不保留", "needs_data": "需补资料"}
LABEL_DECISIONS = {label: decision for decision, label in DECISION_LABELS.items()}
SCREEN_DECISIONS = {VALID: "keep", INVALID: "drop", PENDING: "needs_data"}
LEDGER_SHEET = "台账"
ID_HEADER = "编号"
CODE_HEADER = "核对码"
CHOICE_HEADER = "取舍"
NOTE_HEADER = "理由"
LEVEL_HEADER = "选定参照"
FILL_HEADERS = (CHOICE_HEADER, NOTE_HEADER, LEVEL_HEADER, "核实人", "核实日期")
GROUP_COLORS = {"候选": "DDEBF7", "计算": "EDEDED", "筛选": "FCE4D6", "意见（模型，未核实）": "E2EFDA", "人填栏": "FFF2CC"}
OPINIONS = {"likely_loss": "像浪费", "likely_necessary": "像必要", "data_problem": "像数据问题", "unclear": "看不出"}
CAUSE_KINDS = {"loss": "浪费", "necessary": "必要", "data": "数据", "external": "外部驱动"}
CAUSE_STATUS = {"open": "未定", "kept": "保留", "dropped": "放弃"}
MEASURE_TYPES = {"management": "管理", "operation": "运行调整", "control": "控制改造", "repair": "检修", "retrofit": "改造"}
DISPOSITIONS = {
    "necessary_service": "必要服务",
    "possible_waste": "可能浪费",
    "comparable_group": "能比",
    "driver": "别的驱动",
    "needs_data": "要补资料",
    "not_a_problem": "看不出问题",
    "explained": "已解释（模型）",
    "new_reference": "新参照",
    "cannot_tell": "看不出",
}
PAGE_STYLE = """
body{font-family:system-ui,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:1040px;margin:0 auto;padding:16px;line-height:1.6;color:#1f2328;background:#fff}
h1{font-size:1.5rem}h2{font-size:1.2rem;margin-top:2rem}h3{font-size:1.05rem}
.item{border:1px solid #d0d7de;border-radius:8px;padding:12px 16px;margin:12px 0}
.check{border-collapse:collapse;margin:8px 0;font-size:.85rem;display:block;overflow-x:auto}.check td,.check th{border:1px solid #d0d7de;padding:4px 6px;vertical-align:top;text-align:left}
.warn{color:#9a6700}.note{color:#57606a;font-size:.85rem}
@media (prefers-color-scheme: dark){body{background:#0d1117;color:#e6edf3}.item,.check td,.check th{border-color:#30363d}.note{color:#8b949e}.warn{color:#d29922}}
"""


def check_code(run_id: str, item_id: str) -> str:
    return hashlib.sha1(f"{run_id}:{item_id}".encode()).hexdigest()[:6]


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


# Texts ---------------------------------------------------------------------------


def levels_text(item: dict[str, Any]) -> str:
    return "；".join(
        f"{'【默认】' if level['label'] == item['default_level'] else ''}{level['label']}：{level['reference']} → {level['excess_kwh']:,.0f} kWh"
        for level in item["levels"]
    )


def range_text(item: dict[str, Any]) -> str:
    values = [level["excess_kwh"] for level in item["levels"]]
    if len(values) < 2:
        return ""
    return f"参照换档 {min(values):,.0f}–{max(values):,.0f} kWh"


def decomposition_text(item: dict[str, Any]) -> str:
    part = item.get("decomposition") or {}
    if not part.get("excess_kwh"):
        return ""
    texts = [f"{part['hours']:g} 小时，平均超出 {part['mean_excess_kw']} kW，{part['days']} 天（{part['first_day']} 至 {part['last_day']}）"]
    if part.get("plant_stop_share") is not None:
        texts.append(f"全厂停时占 {part['plant_stop_share']:.0%}")
    if part.get("top_days_share") is not None:
        texts.append(f"最多的两成天数占 {part['top_days_share']:.0%}")
    weekdays = part.get("weekday_share") or {}
    if weekdays:
        top = max(weekdays, key=lambda key: weekdays[key] or 0)
        texts.append(f"星期{top}最多（{weekdays[top]:.0%}）")
    clock = part.get("clock_share") or {}
    if clock:
        top = max(clock, key=lambda key: clock[key] or 0)
        texts.append(f"{top}最多（{clock[top]:.0%}）")
    if part.get("production_correlation") is not None:
        texts.append(f"和产线逐日电量秩相关 {part['production_correlation']}")
    change = part.get("change")
    if change:
        texts.append(f"变化最大的分界 {change['date']}：之前日均 {change['before_kwh_per_day']} kWh，之后 {change['after_kwh_per_day']} kWh")
    batches = part.get("batches")
    if batches:
        texts.append(
            f"高于参照的 {batches['above']} 批对其余 {batches['rest']} 批：运行功率 {batches['run_kw_above']} 对 {batches['run_kw_rest']} kW，"
            f"单位数量运行时长 {batches['hours_per_unit_above']} 对 {batches['hours_per_unit_rest']} h"
        )
    return "；".join(texts)


def overlap_text(item: dict[str, Any]) -> str:
    return "；".join(f"{entry['id']} {entry['kwh']:,.0f} kWh" for entry in item.get("overlaps", [])[:5])


def opinion_texts(item: dict[str, Any]) -> dict[str, str]:
    judgment = item.get("judgment")
    if not judgment:
        return {"opinion": "", "reference": "", "causes": "", "measures": "", "questions": "", "related": ""}
    tests = {entry["cause"]: entry for entry in item.get("tests", [])}
    causes = []
    for cause in judgment.get("causes", []):
        test = tests.get(cause["id"])
        tested = f"；检验 {test['type']}：{RESULT_LABELS[test['result']]}" + (f"（{test['why']}）" if test.get("why") else "") if test else ""
        causes.append(f"{cause['id']} [{CAUSE_KINDS.get(cause['kind'], cause['kind'])}·{CAUSE_STATUS.get(cause.get('status', 'open'))}] {cause['cause']}{tested}")
    measures = [
        f"[{MEASURE_TYPES.get(measure['type'], measure['type'])}] {measure['action']}（{ROLES.get(measure['owner_role'], measure['owner_role'])}"
        + (f"，依赖 {'、'.join(measure['depends_on'])}" if measure.get("depends_on") else "")
        + "）"
        for measure in judgment.get("measures", [])
    ]
    questions = [f"{question['question']}（问{ROLES.get(question['ask_role'], question['ask_role'])}）" for question in judgment.get("site_questions", [])]
    return {
        "opinion": OPINIONS.get(judgment.get("opinion"), "") + (f"；{judgment['note']}" if judgment.get("note") else ""),
        "reference": f"{judgment.get('reference_choice')}：{judgment.get('reference_reason')}",
        "causes": "；".join(causes),
        "measures": "；".join(measures),
        "questions": "；".join(questions),
        "related": "、".join(judgment.get("related", [])),
    }


# Register ------------------------------------------------------------------------


def _items(site: SiteData, ledger: Ledger, judgments: dict[str, dict[str, Any]], tests: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    from .probes import decompose

    grid = ledger.grid
    items = []
    for candidate in ledger.candidates:
        screen = ledger.screening[candidate.id]
        values = [level["excess_kwh"] for level in candidate.levels]
        energy = grid.total(candidate.meter)
        overlaps = ledger.overlaps.get(candidate.id, [])
        part = decompose(grid, candidate, ledger.lens_run.batches)
        items.append(
            {
                "id": candidate.id,
                "check_code": check_code(site.run_id, candidate.id),
                "lens": candidate.lens,
                "lens_name": LENSES.get(candidate.lens, (candidate.lens, ""))[0],
                "meter": candidate.meter,
                "meter_ids": [candidate.meter],
                "name": grid.names.get(candidate.meter, ""),
                "system": system_of(grid, candidate.meter),
                "part": candidate.part,
                "title": candidate.title,
                "selection": candidate.selection,
                "source": candidate.source,
                "spec_id": candidate.spec_id,
                "levels": candidate.levels,
                "default_level": candidate.default,
                "conservative_level": candidate.conservative,
                "excess_kwh": round(candidate.excess, 1),
                "money": None if candidate.money is None else round(candidate.money, 1),
                "in_totals": candidate.in_union,
                "notes": candidate.notes,
                "preferences": candidate.preferences,
                "screen": screen,
                "overlaps": overlaps,
                "overlap_group": ledger.overlap_group.get(candidate.id),
                "decomposition": part,
                "details": candidate.details,
                "judgment": judgments.get(candidate.id),
                "tests": tests.get(candidate.id, []),
                "scalars": {
                    "excess_kwh": round(candidate.excess, 1),
                    "conservative_kwh": min(values) if values else None,
                    "liberal_kwh": max(values) if values else None,
                    "share_of_meter": round(candidate.excess / energy, 4) if energy > 0 else None,
                    "meter_kwh": round(energy, 1),
                    "hours": part.get("hours"),
                    "money": None if candidate.money is None else round(candidate.money, 1),
                    "overlap_kwh": max((entry["kwh"] for entry in overlaps), default=0.0),
                    "data_coverage": round(grid.coverage(candidate.meter), 4),
                },
            }
        )
    order = {VALID: 0, PENDING: 1, INVALID: 2}
    # Items outside the totals (cost, books against meters) come after the others of their screening result.
    items.sort(key=lambda item: (order[item["screen"]["result"]], not item["in_totals"], item["system"], -item["excess_kwh"], item["id"]))
    return items


def _specs(ledger: Ledger) -> list[dict[str, Any]]:
    grid = ledger.grid
    rows = []
    for record in ledger.specs:
        spec = parse_spec(record["spec"])
        computed: Any = None
        if isinstance(spec, ReferenceSpec):
            made, _problems = reference_candidates(grid, spec, record.get("source", "model"))
            computed = [{"meter": candidate.meter, "levels": candidate.levels} for candidate in made]
        elif isinstance(spec, ResidualSpec):
            masks, _problems = residual_mask(grid, spec)
            computed = [
                {
                    "meter": meter_id,
                    "energy_kwh": round(float(grid.energy_of(meter_id)[mask].sum()), 1),
                    "residual_kwh": round(float((ledger.residual[grid.row(meter_id)] + ledger.residual_rule[grid.row(meter_id)])[mask].sum()), 1),
                }
                for meter_id, mask in masks.items()
            ]
        elif isinstance(spec, DriverSpec):
            computed = driver_fit(grid, spec)
        elif isinstance(spec, GroupingSpec):
            computed = {"group": spec.group, "lines": spec.lines or "全部", "status": "待人确认（A05 peer_group）"}
        rows.append(
            {
                "origin": record["origin"],
                "source": record.get("source", "model"),
                "kind": spec.kind,
                "kind_name": SPEC_KINDS[spec.kind],
                "id": spec.id,
                "objects": getattr(spec, "objects", None) or getattr(spec, "lines", None) or [],
                "selection": spec.when.text() if getattr(spec, "when", None) else "",
                "accepted": record["accepted"],
                "problems": record.get("problems", []),
                "why": spec.why,
                "basis": basis_text(getattr(spec, "basis", [])),
                "computed": computed,
                "snippet": rule_snippet(spec) if spec.kind in ("reference", "residual") and record.get("source") != "rule" else "",
                "paste_into": {"reference": "P39 reference_rules", "residual": "P40 residual_rules"}.get(spec.kind, ""),
                "spec": record["spec"],
            }
        )
    return rows


def _questions(looks: dict[str, Any], rounds: list[dict[str, Any]], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for key, look in sorted(looks.items()):
        for question in (look.get("submission") or {}).get("site_questions", []):
            rows.append({"source": f"盲区 {look.get('system', key)}", **question})
    for entry in rounds:
        for question in ((entry.get("answers") or {}).get("submission") or {}).get("site_questions", []):
            rows.append({"source": f"补找第 {entry['round']} 轮", **question})
    for item in items:
        for question in (item.get("judgment") or {}).get("site_questions", []):
            rows.append({"source": f"判断 {item['id']}", **question})
    return rows


def build_register(site: SiteData, ledger: Ledger, judge_results: dict[str, Any], judge_errors: dict[str, Any], assumption_errors: dict[str, Any]) -> dict[str, Any]:
    looks = looks_of(site)
    judgments: dict[str, dict[str, Any]] = {}
    tests: dict[str, list[dict[str, Any]]] = {}
    judges = {}
    for key, result in sorted(judge_results.items()):
        try:
            submission = JudgmentSubmission.model_validate(result["submission"])
        except Exception as exc:  # a malformed settle is reported, not raised
            judges[key] = {"error": f"答复读不了：{exc}"[:300]}
            continue
        for judgment in submission.judgments:
            if judgment.candidate in ledger.by_id:
                judgments[judgment.candidate] = judgment.model_dump(mode="json")
        for entry in probe_results(site, ledger, submission):
            tests.setdefault(entry["candidate"], []).append(entry)
        judges[key] = {"notes": result.get("notes", []), "issues": (result.get("check") or {}).get("issues", []), "corrected": result.get("corrected", False)}
    for key, error in judge_errors.items():
        judges[key] = {"error": str(error)[:500]}
    items = _items(site, ledger, judgments, tests)
    meters = []
    for meter_id in ledger.grid.meters:
        quality = site.quality(meter_id)
        meters.append(
            {
                "meter_id": meter_id,
                "name": ledger.grid.names.get(meter_id, ""),
                "group": ledger.grid.groups.get(meter_id),
                "system": system_of(ledger.grid, meter_id),
                "energy_kwh": round(ledger.grid.total(meter_id), 1),
                "data_coverage": round(ledger.grid.coverage(meter_id), 4),
                "quality_notes": quality.notes(),
            }
        )
    answers = {}
    mechanisms = []
    for look in looks.values():
        submission = look.get("submission") or {}
        for answer in submission.get("triggers", []):
            answers[answer["trigger"]] = {**answer, "system": look.get("system")}
        for mechanism in submission.get("mechanisms", []):
            mechanisms.append({**mechanism, "system": look.get("system")})
    counts = {
        "items": len(items),
        "valid": sum(1 for item in items if item["screen"]["result"] == VALID),
        "pending": sum(1 for item in items if item["screen"]["result"] == PENDING),
        "invalid": sum(1 for item in items if item["screen"]["result"] == INVALID),
        "judged": sum(1 for item in items if item["judgment"]),
        "systems_looked": sum(1 for look in looks.values() if "submission" in look),
        "systems_failed": sum(1 for look in looks.values() if "error" in look),
        "judges_failed": sum(1 for entry in judges.values() if "error" in entry),
        "specs": len(ledger.specs),
        "specs_accepted": sum(1 for record in ledger.specs if record["accepted"]),
        "residual_rounds": len([entry for entry in ledger.rounds if entry.get("round")]),
        "assumptions_drafted": sum(1 for entry in site.assumptions() if entry.get("origin") == "drafted"),
    }
    period = site.period()
    return {
        "schema": SCHEMA,
        "run_id": site.run_id,
        "site": site.manifest.site.name,
        "period": {"start": period[0].isoformat(sep=" "), "end": period[1].isoformat(sep=" ")},
        "summary": ledger.summary(),
        "counts": counts,
        "meters": meters,
        "items": items,
        "coverage": ledger.coverage,
        "slices": ledger.slices,
        "triggers": [{**trigger, "answer": answers.get(trigger["id"])} for trigger in ledger.triggers],
        "mechanisms": mechanisms,
        "specs": _specs(ledger),
        "saturation": ledger.saturation,
        "rounds": ledger.rounds,
        "site_questions": _questions(looks, ledger.rounds, items),
        "looks": {key: {"system": look.get("system"), "error": look.get("error"), "notes": look.get("notes", []), "corrected": look.get("corrected", False)} for key, look in looks.items()},
        "judges": judges,
        "problems": ledger.problems,
        "drafting_failed": bool(assumption_errors),
        "lens_notes": ledger.lens_run.notes,
        "screen_rules": {rule: {"result": outcome, "text": text} for rule, (outcome, text) in RULES.items()},
        "lenses": {lens: {"name": name, "reference": text} for lens, (name, text) in LENSES.items()},
        "company_model": load_model(site),
        "assumptions": site.assumptions(),
        "caliber_facts": read_json(site.path("caliber.json"))["facts"] if site.path("caliber.json").is_file() else [],
        "manifest_origin": site.manifest_origin(),
    }


# Workbook --------------------------------------------------------------------------


def _cell(value: Any) -> Any:
    if isinstance(value, str):
        return ILLEGAL_CHARACTERS_RE.sub("", value)
    if isinstance(value, (list, dict)):
        return ILLEGAL_CHARACTERS_RE.sub("", str(value))
    return value


def ledger_columns() -> list[tuple[str, str, Any]]:
    return [
        ("候选", ID_HEADER, lambda item: item["id"]),
        ("候选", CODE_HEADER, lambda item: item["check_code"]),
        ("候选", "系统", lambda item: item["system"]),
        ("候选", "表", lambda item: item["meter"]),
        ("候选", "表名", lambda item: item["name"]),
        ("候选", "视角", lambda item: f"{item['lens']} {item['lens_name']}"),
        ("候选", "说明", lambda item: item["title"]),
        ("候选", "时段", lambda item: item["selection"]),
        ("计算", "默认参照", lambda item: item["default_level"]),
        ("计算", "默认参照下超出 kWh", lambda item: item["excess_kwh"]),
        ("计算", "最保守参照下 kWh", lambda item: item["scalars"]["conservative_kwh"]),
        ("计算", "占本表", lambda item: item["scalars"]["share_of_meter"]),
        ("计算", "各档参照与超出", levels_text),
        ("计算", "金额（上限）", lambda item: item["money"]),
        ("计算", "进合计", lambda item: "是" if item["in_totals"] else "否"),
        ("计算", "重叠组", lambda item: item["overlap_group"]),
        ("计算", "与谁重叠", overlap_text),
        ("计算", "拆分", decomposition_text),
        ("计算", "说明（程序）", lambda item: "；".join(item["notes"])),
        ("筛选", "筛选结果", lambda item: item["screen"]["result"]),
        ("筛选", "规则", lambda item: item["screen"]["rule"]),
        ("筛选", "理由", lambda item: item["screen"]["reason"]),
        ("意见（模型，未核实）", "意见", lambda item: opinion_texts(item)["opinion"]),
        ("意见（模型，未核实）", "参照意见", lambda item: opinion_texts(item)["reference"]),
        ("意见（模型，未核实）", "原因候选与检验", lambda item: opinion_texts(item)["causes"]),
        ("意见（模型，未核实）", "措施选项", lambda item: opinion_texts(item)["measures"]),
        ("意见（模型，未核实）", "现场问题", lambda item: opinion_texts(item)["questions"]),
        ("意见（模型，未核实）", "相关项", lambda item: opinion_texts(item)["related"]),
        *[("人填栏", header, lambda item: "") for header in FILL_HEADERS],
    ]


def _header(sheet: Any, columns: list[tuple[str, str, Any]]) -> None:
    sheet.append([group for group, _name, _value in columns])
    sheet.append([name for _group, name, _value in columns])
    start = 1
    for index in range(1, len(columns) + 1):
        group = columns[index - 1][0]
        if index == len(columns) or columns[index][0] != group:
            fill = PatternFill("solid", fgColor=GROUP_COLORS.get(group, "FFFFFF"))
            for column in range(start, index + 1):
                for row in (1, 2):
                    cell = sheet.cell(row=row, column=column)
                    cell.fill = fill
                    cell.font = Font(bold=True)
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
            if index > start:
                sheet.merge_cells(start_row=1, start_column=start, end_row=1, end_column=index)
            start = index + 1


def _table_sheet(book: Workbook, title: str, notes: list[str], head: list[str], rows: list[list[Any]]) -> None:
    sheet = book.create_sheet(title)
    for note in notes:
        sheet.append([note])
    sheet.append(head)
    for cell in sheet[sheet.max_row]:
        cell.font = Font(bold=True)
    for row in rows:
        sheet.append([_cell(value) for value in row])


def write_workbook(path: Path, register: dict[str, Any]) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = LEDGER_SHEET
    columns = ledger_columns()
    _header(sheet, columns)
    for item in register["items"]:
        sheet.append([_cell(render(item)) for _group, _name, render in columns])
    headers = [name for _group, name, _value in columns]
    validation = DataValidation(type="list", formula1='"' + ",".join(DECISION_LABELS.values()) + '"', allow_blank=True)
    sheet.add_data_validation(validation)
    letter = sheet.cell(row=2, column=headers.index(CHOICE_HEADER) + 1).column_letter
    if sheet.max_row > 2:
        validation.add(f"{letter}3:{letter}{sheet.max_row}")
    sheet.freeze_panes = "C3"
    _table_sheet(
        book,
        "覆盖与剩余",
        [
            "每块表的电：多少被视角看过（看过一次、两次以上、没看过），找到的超出按筛选结果分（同一格只算一次），"
            "以及没解释的超出（通用视角看到、没有哪个定位视角说得清的部分）。剩余里人确认的剩余规则（P40）算已解释；模型的解释只列出来，不算已解释。"
        ],
        ["表", "表名", "系统", "全期电量 kWh", "数据覆盖率", "看过的视角", "看过 kWh", "看过两次以上 kWh", "没看过 kWh", "有效 kWh", "待定 kWh", "无效 kWh", "没解释的剩余 kWh", "其中模型有解释 kWh", "人定规则已解释 kWh"],
        [
            [
                row["meter"], row["name"], row["system"], row["energy_kwh"], row["data_coverage"], "、".join(row["lenses"]),
                row["examined_kwh"], row["examined_twice_kwh"], row["not_examined_kwh"], row["valid_kwh"], row["pending_kwh"],
                row["invalid_kwh"], row["residual_open_kwh"], row["residual_model_kwh"], row["residual_rule_kwh"],
            ]
            for row in register["coverage"]
        ],
    )
    _table_sheet(
        book,
        "剩余片",
        ["没解释的超出里最大的几片（一块表的一周）：补找时模型看的就是这些。"],
        ["编号", "表", "表名", "周一", "kWh", "其中模型有解释", "夜间占比（22-6点）", "全厂停时占比", "逐日"],
        [
            [entry["id"], entry["meter"], entry["name"], entry["week"], entry["kwh"], entry["model_explained_kwh"], entry["night_share"], entry["stop_share"], "；".join(f"{day} {value}" for day, value in entry["days"])]
            for entry in register["slices"]
        ],
    )
    _table_sheet(
        book,
        "触发与处置",
        ["程序按偏好里的触发线（P31、P32、P21）把模型引到这些地方；处置是模型的意见。每个系统另外都做了一次领域先验和问卷检查（见下方机理）。"],
        ["编号", "触发", "系统", "表", "程序算的", "要模型做的", "处置", "对应规格", "理由"],
        [
            [
                trigger["id"], trigger["title"], trigger["system"], "、".join(trigger["meters"]), trigger["text"], trigger["asks"],
                DISPOSITIONS.get((trigger.get("answer") or {}).get("disposition"), "没有处置"),
                "、".join((trigger.get("answer") or {}).get("specs", [])), (trigger.get("answer") or {}).get("note", ""),
            ]
            for trigger in register["triggers"]
        ]
        + [["机理", mechanism["mechanism"], mechanism["system"], "、".join(mechanism.get("objects", [])), "", "", mechanism["outcome"], "、".join(mechanism.get("specs", []) + mechanism.get("covered_by", [])), mechanism["note"]] for mechanism in register["mechanisms"]],
    )
    _table_sheet(
        book,
        "规格",
        [
            "模型交的规格和人确认的规则；数都是程序按规格算的。确认一条参照或剩余解释，把“可抄进偏好”一栏的 YAML 贴进分析偏好的对应项（参照 → P39 reference_rules，剩余 → P40 residual_rules），下次运行就是确定性规则。"
        ],
        ["来源", "种类", "编号", "对象", "时段", "收下", "问题", "为什么", "依据", "程序算的", "可抄进偏好", "贴到"],
        [
            [
                spec["origin"], spec["kind_name"], spec["id"], "、".join(spec["objects"]), spec["selection"], "是" if spec["accepted"] else "否",
                "；".join(spec["problems"]), spec["why"], spec["basis"], str(spec["computed"])[:2000], spec["snippet"], spec["paste_into"],
            ]
            for spec in register["specs"]
        ],
    )
    saturation = register["saturation"]
    rows = [["按新增电量加入", entry["lens"], entry["name"], entry["added_kwh"], entry["total_kwh"], "", ""] for entry in saturation["order"]]
    rows += [["独立发现", entry["lens"], entry["name"], "", "", entry["slices"], f"{entry['independent_slices']} 片 / {entry['independent_kwh']} kWh"] for entry in saturation["discovery"]]
    rows += [["补找", f"第 {entry['round']} 轮", entry.get("reason", ""), entry.get("gain_kwh"), entry.get("residual_after_kwh"), "", "停" if entry.get("stopped") else "继续"] for entry in register["rounds"]]
    _table_sheet(
        book,
        "饱和",
        [f"视角按新增的超出贪心加入；独立发现是只有这一个视角在那片（{saturation['slice']}，超出不少于当天电量 × P41 {saturation['min_share']:g}）有发现。补找按 P33、P34 停。"],
        ["项", "视角", "名称", "新增 kWh", "累计 kWh", "有发现的片", "独立发现"],
        rows,
    )
    _table_sheet(
        book,
        "现场清单",
        ["模型提的现场问题（意见，未核实）。问清以后写进 confirmations、偏好或问卷，再跑。"],
        ["来源", "问题", "问谁", "对象", "问清后能定"],
        [[entry["source"], entry["question"], ROLES.get(entry["ask_role"], entry["ask_role"]), "、".join(entry.get("objects", [])), "、".join(entry.get("settles", []))] for entry in register["site_questions"]],
    )
    add_model_sheets(book, register)
    for name in book.sheetnames:
        for row in book[name].iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    cell.value = ILLEGAL_CHARACTERS_RE.sub("", cell.value)
    book.save(path)


def _table(head: list[str], rows: list[list[Any]]) -> str:
    cells = "".join(f"<th>{_e(cell)}</th>" for cell in head)
    body = "".join("<tr>" + "".join(f"<td>{_e(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table class='check'><tr>{cells}</tr>{body}</table>"


def coverage_html(register: dict[str, Any]) -> str:
    return "<h2>覆盖与剩余</h2>" + _table(
        ["表", "表名", "全期 kWh", "没看过 kWh", "有效 kWh", "待定 kWh", "无效 kWh", "没解释的剩余 kWh", "模型有解释"],
        [
            [row["meter"], row["name"], f"{row['energy_kwh']:,.0f}", f"{row['not_examined_kwh']:,.0f}", f"{row['valid_kwh']:,.0f}", f"{row['pending_kwh']:,.0f}", f"{row['invalid_kwh']:,.0f}", f"{row['residual_open_kwh']:,.0f}", f"{row['residual_model_kwh']:,.0f}"]
            for row in register["coverage"]
        ],
    )


def write_page(path: Path, register: dict[str, Any]) -> None:
    counts = register["counts"]
    summary = register["summary"]
    body = [
        f"<h1>{_e(register['site'])} 损耗项台账</h1>",
        f"<p>数据期 {_e(register['period']['start'])} 至 {_e(register['period']['end'])}；候选 {counts['items']} 项：有效 {counts['valid']}、待定 {counts['pending']}、无效 {counts['invalid']}；"
        f"找到的超出（同一格只算一次）{summary['found_kwh']:,.0f} kWh，没解释的剩余 {summary['residual_open_kwh']:,.0f} kWh。</p>",
        "<p class='note'>这是台账，不是结论。数都由程序按写明的参照算；有效、无效、待定按偏好里的规则定；模型的意见、原因和措施都未经核实。"
        "保留哪些由人在 register.xlsx 的“台账”页填“取舍”，人填的优先于筛选结果。</p>",
        coverage_html(register),
        "<h2>候选</h2>",
        _table(
            ["编号", "表", "视角", "默认参照下 kWh", "参照换档", "筛选", "意见（未核实）"],
            [
                [item["id"], f"{item['meter']} {item['name']}", item["lens_name"], f"{item['excess_kwh']:,.0f}", range_text(item), f"{item['screen']['result']}（{item['screen']['rule']}）", opinion_texts(item)["opinion"]]
                for item in register["items"]
                if item["excess_kwh"] > 0
            ],
        ),
        "<h2>触发与处置</h2>",
        _table(
            ["编号", "触发", "程序算的", "处置（模型）"],
            [[trigger["id"], trigger["title"], trigger["text"], DISPOSITIONS.get((trigger.get("answer") or {}).get("disposition"), "没有处置")] for trigger in register["triggers"]],
        ),
        model_html(register),
    ]
    page = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>损耗项台账</title><style>{PAGE_STYLE}</style></head><body>" + "\n".join(body) + "</body></html>"
    )
    path.write_text(page, encoding="utf-8")


def write_register(site: SiteData, data_dir: Path, register: dict[str, Any]) -> list[Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    paths = [data_dir / "register.json", data_dir / "register.xlsx", data_dir / "register.html"]
    write_workbook(paths[1], register)
    write_page(paths[2], register)
    book = write_preferences_book(site.parameter_set(), data_dir / PREFERENCES_BOOK)
    # Last: a report starts only from a register whose other files were written.
    write_json(paths[0], register)
    return [*paths, book]


__all__ = [
    "CHOICE_HEADER",
    "CODE_HEADER",
    "DECISION_LABELS",
    "ID_HEADER",
    "LABEL_DECISIONS",
    "LEDGER_SHEET",
    "LEVEL_HEADER",
    "NOTE_HEADER",
    "PAGE_STYLE",
    "SCREEN_DECISIONS",
    "build_register",
    "check_code",
    "coverage_html",
    "decomposition_text",
    "levels_text",
    "opinion_texts",
    "overlap_text",
    "range_text",
    "write_register",
]
