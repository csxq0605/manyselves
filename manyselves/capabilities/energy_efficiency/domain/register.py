"""Write the candidate register: JSON, a spreadsheet to mark, and a page to read.

The spreadsheet has one row per item and an empty 选择 column (保留 / 不保留 /
需补资料) and 备注 column for the person.  Rows are sorted by the program's
full-period energy of the item, largest first; the sort is only for reading.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from .store import write_json

DECISION_LABELS = {"keep": "保留", "drop": "不保留", "needs_data": "需补资料"}
LABEL_DECISIONS = {label: decision for decision, label in DECISION_LABELS.items()}
ID_HEADER = "编号"
CODE_HEADER = "核对码"
CHOICE_HEADER = "选择"
NOTE_HEADER = "备注"
REGISTER_COLUMNS = [
    ID_HEADER,
    CODE_HEADER,
    "来源",
    "分区",
    "表",
    "寻找方式",
    "现象",
    "窗口电量kWh",
    "窗口平均kW",
    "窗口覆盖",
    "全期出现次数",
    "全期命中次数",
    "全期出现平均覆盖",
    "全期相关电量kWh",
    "占该表电量",
    "最大单次占比",
    "后/前半期",
    "边界敏感度",
    "重叠项",
    "模型标的相关项",
    "窗口电费",
    "全期回放电费",
    "未解决的检查问题",
    "数字提示",
    CHOICE_HEADER,
    NOTE_HEADER,
]


def sort_key(item: dict[str, Any]) -> tuple[float, float, str]:
    scalars = item["scalars"]
    replay = scalars.get("replay_item_kwh")
    window = scalars.get("window_energy_kwh")
    return (
        -(replay if replay is not None else -1.0),
        -(window if window is not None else -1.0),
        item["id"],
    )


def method_text(item: dict[str, Any]) -> str:
    method = item["finding_method"]
    params = json.dumps(method["params"], ensure_ascii=False) if method["params"] else ""
    head = f"{method['title']}（{method['method_id']}）"
    return f"{head} {params}".strip()


def _row(item: dict[str, Any], names: dict[str, str]) -> list[Any]:
    scalars = item["scalars"]
    overlap = sorted({entry["item_id"] for entry in item["computed"]["overlap"]})
    return [
        item["id"],
        item["check_code"],
        "模型发现" if item["source"] == "model" else "规则检出",
        item["partition_id"] or "",
        "、".join(f"{meter_id} {names.get(meter_id, '')}".strip() for meter_id in item["meter_ids"]),
        method_text(item),
        item["phenomenon"],
        scalars["window_energy_kwh"],
        scalars["window_avg_kw"],
        scalars["window_coverage"],
        scalars["replay_occurrences"],
        scalars["replay_hits"],
        scalars["replay_mean_coverage"],
        scalars["replay_item_kwh"],
        scalars["replay_share_of_meter"],
        scalars["top_share"],
        scalars["halves_ratio"],
        scalars["boundary_max_change"],
        "、".join(overlap),
        "、".join(item.get("related_ids", [])),
        scalars["window_cost"],
        scalars["replay_cost"],
        "；".join(item["check_issues"]),
        "；".join(item["computed"]["number_hints"]),
        "",
        "",
    ]


def write_workbook(path: Path, register: dict[str, Any]) -> None:
    names = {meter["meter_id"]: meter["name"] for meter in register["meters"]}
    book = Workbook()
    sheet = book.active
    sheet.title = "候选清单"
    sheet.append(REGISTER_COLUMNS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for item in sorted(register["items"], key=sort_key):
        sheet.append(_cells(_row(item, names)))
    choice_column = REGISTER_COLUMNS.index(CHOICE_HEADER) + 1
    letter = sheet.cell(row=1, column=choice_column).column_letter
    validation = DataValidation(
        type="list", formula1='"' + ",".join(DECISION_LABELS.values()) + '"', allow_blank=True
    )
    sheet.add_data_validation(validation)
    if sheet.max_row > 1:
        validation.add(f"{letter}2:{letter}{sheet.max_row}")
    highlight = PatternFill("solid", fgColor="FFF2CC")
    for row in range(1, sheet.max_row + 1):
        sheet.cell(row=row, column=choice_column).fill = highlight
        sheet.cell(row=row, column=choice_column + 1).fill = highlight
    sheet.freeze_panes = "C2"
    for column, width in zip("ABCDEFG", (8, 10, 10, 6, 18, 30, 50), strict=True):
        sheet.column_dimensions[column].width = width

    guide = book.create_sheet("说明")
    guide.append(["怎样标记"])
    guide.append([f"在“候选清单”的“{CHOICE_HEADER}”列填 保留 / 不保留 / 需补资料，可在“{NOTE_HEADER}”列写理由；不填的项按规则文件处理，没有规则就是未选。"])
    guide.append([f"标好后保存，用 energy-report 入口指定这个文件出报告。“{ID_HEADER}”和“{CODE_HEADER}”两列不要改：出报告时程序用它们确认标记对应的是这份清单里的同一项。"])
    guide.append([f"这份清单来自运行 {register['run_id']}。"])
    guide.append([])
    guide.append(["字段", "含义"])
    for name, meaning in register["scalar_fields"].items():
        guide.append([name, meaning])

    coverage = book.create_sheet("覆盖说明")
    coverage.append(["分区", "表", "方法", "结果", "说明", "对应项"])
    for record in register["partitions"]:
        meters = "、".join(record["meter_ids"])
        if record["outcome"] != "ok":
            coverage.append(_cells([record["partition_id"], meters, "", "分区失败", record.get("error", ""), ""]))
            continue
        for issue in record["issues"]:
            coverage.append(_cells([record["partition_id"], meters, "", "未解决的检查问题", issue, ""]))
        if record["notes"]:
            coverage.append(_cells([record["partition_id"], meters, "", "模型的分区说明", record["notes"], ""]))
        for entry in record["coverage"]:
            coverage.append(
                _cells(
                    [
                        record["partition_id"],
                        "、".join(entry["meter_ids"]),
                        entry["method_id"],
                        entry["outcome"],
                        entry["note"],
                        "、".join(entry["item_ids"]),
                    ]
                )
            )
    for outcome in register["prechecks"]:
        coverage.append(
            _cells(
                [
                    "预检",
                    outcome.get("meter_id", ""),
                    outcome["method_id"],
                    outcome["outcome"],
                    outcome.get("reason", ""),
                    outcome.get("item_id") or "",
                ]
            )
        )

    quality = book.create_sheet("数据质量")
    quality.append(["表", "名称", "全期电量kWh", "说明"])
    for meter in register["meters"]:
        quality.append(
            _cells([meter["meter_id"], meter["name"], meter["period_energy_kwh"], "；".join(meter["quality_notes"])])
        )
    _plain_text(book)
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)


def _cells(values: list[Any]) -> list[Any]:
    """Text a sheet can hold: control characters become spaces."""

    return [ILLEGAL_CHARACTERS_RE.sub(" ", value) if isinstance(value, str) else value for value in values]


def _plain_text(book: Workbook) -> None:
    """Keep text that starts with '=' as text, never as a formula."""

    for sheet in book.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    cell.data_type = "s"


def _cost_notes(item: dict[str, Any]) -> list[str]:
    notes = []
    for name, label in (("window", "时间窗"), ("replay", "全期回放")):
        for meter_id, entry in item["computed"]["per_meter"].items():
            cost = ((entry.get("cost") or {}).get(name)) or {}
            if cost.get("unpriced_records"):
                notes.append(
                    f"表 {meter_id} {label}里有 {cost['unpriced_records']} 条记录跨电价时段（{cost['unpriced_kwh']} kWh），"
                    f"不拆分计价，所以不给{label}电费"
                )
    return notes


def cost_note(item: dict[str, Any]) -> str | None:
    """Say which cost is empty, and why, when some records could not be priced whole."""

    notes = _cost_notes(item)
    return "；".join(notes) if notes else None


def partition_html(register: dict[str, Any]) -> str:
    parts = ["<h2>各分区查了什么</h2>"]
    for record in register["partitions"]:
        parts.append(f"<h3>{_e(record['partition_id'])} {_e(record.get('label', ''))}（{_e('、'.join(record['meter_ids']))}）</h3>")
        if record["outcome"] != "ok":
            parts.append(f"<p class='warn'>分区失败：{_e(record.get('error', ''))}</p>")
            continue
        rows = "".join(
            f"<tr><td>{_e(entry['method_id'])}</td><td>{_e(entry['outcome'])}</td><td>{_e(entry['note'])}</td>"
            f"<td>{_e('、'.join(entry['item_ids']))}</td></tr>"
            for entry in record["coverage"]
        )
        parts.append(f"<table class='figures'>{rows}</table>")
        if record["issues"]:
            parts.append(f"<p class='warn'><b>未解决的检查问题</b>：{_e('；'.join(record['issues']))}</p>")
        if record["notes"]:
            parts.append(f"<p><b>模型的分区说明</b>：{_e(record['notes'])}</p>")
    lines = precheck_lines(register)
    if lines:
        parts.append("<h3>人写的规则预检</h3><ul>")
        parts += [f"<li>{_e(line)}</li>" for line in lines]
        parts.append("</ul>")
    return "\n".join(parts)


PRECHECK_OUTCOMES = {
    "found": "检出",
    "none_found": "没有检出",
    "needs_input": "缺资料，没有跑",
    "not_applicable": "不适用，没有跑",
    "invalid": "规则写得不对，没有跑",
}


def precheck_lines(register: dict[str, Any]) -> list[str]:
    """One line per precheck and meter: what ran, what it found, and why one did not run."""

    lines = []
    for outcome in register["prechecks"]:
        head = f"预检第 {outcome['precheck']} 条 {outcome['method_id']}"
        if outcome.get("meter_id"):
            head += f"（表 {outcome['meter_id']}）"
        text = f"{head}：{PRECHECK_OUTCOMES.get(outcome['outcome'], outcome['outcome'])}"
        if outcome.get("item_id"):
            text += f"，列为 {outcome['item_id']}"
        if outcome.get("reason"):
            text += f"。{outcome['reason']}"
        lines.append(text)
    return lines


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def figures(item: dict[str, Any]) -> list[tuple[str, Any]]:
    """The program's numbers shown for an item, in the order the page and the report use."""

    scalars = item["scalars"]
    return [
        ("窗口电量 kWh", scalars["window_energy_kwh"]),
        ("窗口平均 kW", scalars["window_avg_kw"]),
        ("窗口覆盖", scalars["window_coverage"]),
        ("全期出现次数", scalars["replay_occurrences"]),
        ("全期命中次数", scalars["replay_hits"]),
        ("全期出现平均覆盖", scalars["replay_mean_coverage"]),
        ("全期相关电量 kWh", scalars["replay_item_kwh"]),
        ("占该表电量", scalars["replay_share_of_meter"]),
        ("最大单次占比", scalars["top_share"]),
        ("后/前半期", scalars["halves_ratio"]),
        ("边界敏感度", scalars["boundary_max_change"]),
        ("窗口电费", scalars["window_cost"]),
        ("全期回放电费", scalars["replay_cost"]),
    ]


def overlap_text(item: dict[str, Any]) -> str:
    return "；".join(
        f"{entry['item_id']}（表 {entry['meter_id']}，{entry['shared_records']} 条记录"
        + (f"，{entry['shared_kwh']} kWh" if entry["shared_kwh"] is not None else "")
        + "）"
        for entry in item["computed"]["overlap"]
    )


def item_html(item: dict[str, Any], names: dict[str, str], decision: str | None = None) -> str:
    meters = "、".join(f"{meter_id} {names.get(meter_id, '')}".strip() for meter_id in item["meter_ids"])
    windows = "；".join(f"{window['start']} 至 {window['end']}" for window in item["windows"])
    parts = [
        f"<section class='item' id='{_e(item['id'])}'>",
        f"<h3>{_e(item['id'])} · {_e(meters)} <span class='source'>{'模型发现' if item['source'] == 'model' else '规则检出'}</span></h3>",
    ]
    if decision:
        parts.append(f"<p><b>选择</b>：{_e(decision)}</p>")
    parts += [
        f"<p><b>现象</b>：{_e(item['phenomenon'])}</p>",
        f"<p><b>时间窗</b>：{_e(windows)}</p>",
        f"<p><b>寻找方式</b>：{_e(method_text(item))}。{_e(item['finding_method']['description'])}</p>",
    ]
    if item["reasoning"]:
        parts.append(f"<p><b>思考过程</b>：{_e(item['reasoning'])}</p>")
    if item["root_cause_candidates"]:
        parts.append("<p><b>候选原因</b>（均未核实）：</p><ul>")
        for cause in item["root_cause_candidates"]:
            parts.append(
                f"<li>{_e(cause['cause'])}（依据：{_e(cause['basis'])}，{_e(cause['basis_detail'])}；"
                f"核实方法：{_e(cause['how_to_check'])}）</li>"
            )
        parts.append("</ul>")
    if item["missing_info"]:
        parts.append(f"<p><b>还缺的资料</b>：{_e('；'.join(item['missing_info']))}</p>")
    if item["monitoring_suggestion"]:
        parts.append(f"<p><b>监测建议</b>：{_e(item['monitoring_suggestion'])}</p>")
    cells = "".join(f"<tr><td>{_e(name)}</td><td>{_e(value)}</td></tr>" for name, value in figures(item) if value is not None)
    if cells:
        parts.append(f"<table class='figures'>{cells}</table>")
    note = cost_note(item)
    if note:
        parts.append(f"<p class='note'>{_e(note)}</p>")
    unknown = [meter_id for meter_id, entry in item["computed"]["per_meter"].items() if entry.get("unknown_meter")]
    if unknown:
        parts.append(f"<p class='warn'>表 {_e('、'.join(unknown))} 不在资料里，没有算数</p>")
    if len(item["meter_ids"]) > 1:
        parts.append("<p class='note'>多块表的数不合计（表之间可能有上下级关系），逐表数值见 JSON。</p>")
    if item.get("related_ids"):
        parts.append(f"<p><b>模型标的相关项</b>：{_e('、'.join(item['related_ids']))}</p>")
    if item["computed"]["overlap"]:
        parts.append(f"<p><b>与其他项重叠</b>：{_e(overlap_text(item))}</p>")
    if item["check_issues"]:
        parts.append(f"<p class='warn'><b>未解决的检查问题</b>：{_e('；'.join(item['check_issues']))}</p>")
    if item["computed"]["number_hints"]:
        parts.append(f"<p class='warn'><b>数字提示</b>：{_e('；'.join(item['computed']['number_hints']))}</p>")
    if item["rule_notes"]:
        parts.append(f"<p><b>模型对这条规则项的说明</b>：{_e('；'.join(item['rule_notes']))}</p>")
    parts.append(f"<p class='refs'>证据：{_e('、'.join(item['evidence_ids']))}</p>")
    parts.append("</section>")
    return "\n".join(parts)


PAGE_STYLE = """
body{font-family:system-ui,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:960px;margin:0 auto;padding:16px;line-height:1.6;color:#1f2328;background:#fff}
h1{font-size:1.5rem}h2{font-size:1.2rem;margin-top:2rem}h3{font-size:1.05rem}
.item{border:1px solid #d0d7de;border-radius:8px;padding:12px 16px;margin:12px 0}
.source{font-size:.8rem;color:#57606a;font-weight:normal}
.figures td{padding:2px 12px 2px 0}.warn{color:#9a6700}.note,.refs{color:#57606a;font-size:.85rem}
@media (prefers-color-scheme: dark){body{background:#0d1117;color:#e6edf3}.item{border-color:#30363d}.source,.note,.refs{color:#8b949e}.warn{color:#d29922}}
"""


def write_page(path: Path, register: dict[str, Any]) -> None:
    names = {meter["meter_id"]: meter["name"] for meter in register["meters"]}
    counts = register["counts"]
    body = [
        f"<h1>{_e(register['site'])} 损耗项候选清单</h1>",
        f"<p>数据期 {_e(register['period']['start'])} 至 {_e(register['period']['end'])}；"
        f"模型发现 {counts['model_items']} 项，规则检出 {counts['rule_items']} 项；"
        f"分区 {counts['partitions']} 个，失败 {counts['partitions_failed']} 个。</p>",
        "<p>这是候选清单，不是结论：保留哪些由人在表格里标记或按人写的规则决定。所有数值由程序从数据算出；候选原因都未经核实。</p>",
        "<h2>候选项（按全期相关电量从大到小）</h2>",
    ]
    body += [item_html(item, names) for item in sorted(register["items"], key=sort_key)]
    body.append(partition_html(register))
    page = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>损耗项候选清单</title><style>{PAGE_STYLE}</style></head><body>"
        + "\n".join(body)
        + "</body></html>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")


def write_register(data_dir: Path, register: dict[str, Any]) -> list[Path]:
    paths = [data_dir / "register.json", data_dir / "register.xlsx", data_dir / "register.html"]
    write_workbook(paths[1], register)
    write_page(paths[2], register)
    # Last: a report can only start from a register whose other files were written.
    write_json(paths[0], register)
    return paths


__all__ = [
    "CHOICE_HEADER",
    "CODE_HEADER",
    "DECISION_LABELS",
    "ID_HEADER",
    "LABEL_DECISIONS",
    "NOTE_HEADER",
    "PAGE_STYLE",
    "cost_note",
    "figures",
    "item_html",
    "method_text",
    "overlap_text",
    "partition_html",
    "precheck_lines",
    "sort_key",
    "write_register",
]
