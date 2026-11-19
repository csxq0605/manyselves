"""The company model and the three lists, as sheets of the register workbook and parts of its page.

Laid out by the program from what the run holds: the task (A01), the
sources (A02), the objects and links (A03), the parameters in force (A04),
the confirmations applied (A05), the data gaps, and the three lists the
assembly step writes (statistics, leads to verify, monitoring candidates).
"""

from __future__ import annotations

import html
import json
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font

from .preparation import BASES, ROLES, STATUSES

TASK_LABELS = {
    "request": "企业诉求",
    "users": "结果使用者",
    "decisions": "要支持的决策",
    "meters": "对象",
    "period": "期间",
    "depth": "分析深度",
    "constraints": "约束和资源",
    "stop_conditions": "审核与停止条件",
    "focus": "关注点",
    "defined_by": "定义人",
}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _text(value: Any) -> Any:
    """A cell: numbers and text as they are, anything else as compact text."""

    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, dict) and "value" in value and "basis" in value:
        unit = f" {value['unit']}" if value.get("unit") else ""
        return f"{_text(value['value'])}{unit}（{BASES.get(value['basis'], value['basis'])}，{STATUSES.get(value['status'], value['status'])}）"
    if isinstance(value, list) and all(isinstance(entry, (str, int, float)) for entry in value):
        return "、".join(str(entry) for entry in value)
    return json.dumps(value, ensure_ascii=False)


def task_rows(model: dict[str, Any]) -> list[list[Any]]:
    task = model.get("task") or {}
    rows = [[TASK_LABELS.get(key, key), _text(value)] for key, value in task.items() if key in TASK_LABELS and value]
    for pending in task.get("pending", []):
        rows.append([pending, "待定（没人写，不阻塞；结论的适用范围按待定理解）"])
    return rows


SOURCE_HEAD = ["编号", "文件", "用途", "读了的工作表", "没读的工作表", "字节", "SHA-256"]


def source_rows(model: dict[str, Any]) -> list[list[Any]]:
    return [
        [
            entry["source_id"],
            entry["file"],
            _text(entry.get("roles", [])),
            _text(entry.get("sheets_read") or []),
            _text(entry.get("sheets_not_read") or []),
            entry.get("bytes"),
            entry.get("sha256"),
        ]
        for entry in model.get("sources", [])
    ]


OBJECT_HEAD = ["类型", "编号", "内容"]


def object_rows(model: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for kind, entries in (model.get("objects") or {}).items():
        for entry in entries:
            rest = {key: value for key, value in entry.items() if key != "id"}
            rows.append([kind, entry["id"], "；".join(f"{key}：{_text(value)}" for key, value in rest.items() if value not in (None, [], {}))])
    return rows


LINK_HEAD = ["关系", "从", "到", "依据", "状态", "边界", "确认人", "说明"]


def link_rows(model: dict[str, Any]) -> list[list[Any]]:
    return [
        [
            link["type"],
            link["from"],
            link["to"],
            BASES.get(link.get("basis"), link.get("basis")),
            STATUSES.get(link.get("status"), link.get("status")),
            link.get("boundary"),
            link.get("confirmed_by"),
            link.get("note"),
        ]
        for link in model.get("links", [])
    ]


PARAMETER_HEAD = ["编号", "参数", "取值", "单位", "依据", "状态", "来源", "适用的表", "规则", "为什么", "换掉它要什么", "备选", "影响", "问谁"]


def parameter_rows(model: dict[str, Any]) -> list[list[Any]]:
    return [
        [
            entry["id"],
            entry["topic"],
            _text(entry["value"]),
            entry.get("unit"),
            BASES.get(entry["basis"], entry["basis"]),
            STATUSES.get(entry["status"], entry["status"]),
            entry.get("source"),
            _text(entry.get("meters") or []) or "全部",
            entry.get("rule"),
            entry.get("rationale"),
            entry.get("replaces_when"),
            _text(entry.get("alternatives") or []),
            _text(entry.get("affected") or []),
            ROLES.get(entry.get("ask", ""), entry.get("ask")),
        ]
        for entry in model.get("parameters", [])
    ]


GAP_HEAD = ["编号", "缺口", "要问的", "问谁", "影响", "补上以后", "对象"]


def gap_rows(model: dict[str, Any]) -> list[list[Any]]:
    return [
        [gap["id"], gap["what"], gap["question"], gap["ask_label"], _text(gap["affects"]), gap["unlocks"], _text(gap["objects"])]
        for gap in model.get("gaps", [])
    ]


CONFIRMATION_HEAD = ["类型", "对象", "结果", "确认人", "日期", "依据", "用到了哪里"]


def confirmation_rows(model: dict[str, Any]) -> list[list[Any]]:
    record = model.get("confirmations") or {}
    return [
        [
            entry.get("kind_label") or entry.get("kind"),
            entry.get("subject"),
            entry.get("result"),
            entry.get("confirmed_by"),
            entry.get("date"),
            entry.get("basis"),
            "；".join(entry.get("effects", [])),
        ]
        for entry in [*record.get("applied", []), *record.get("unmatched", [])]
    ]


INFERENCE_HEAD = ["编号", "关于", "推断", "依据", "来源", "状态", "确认人"]


def inference_rows(model: dict[str, Any]) -> list[list[Any]]:
    return [
        [entry["id"], entry["topic"], entry["inference"], entry.get("basis_text"), entry.get("origin"), STATUSES.get(entry["status"], entry["status"]), entry.get("confirmed_by")]
        for entry in model.get("inferences", [])
    ]


STATS_HEAD = ["句柄", "表", "说明", "键", "数值", "状态", "依赖的现场假设", "被哪些线索引用"]


def stats_rows(registers: dict[str, Any]) -> list[list[Any]]:
    return [
        [
            entry["id"],
            entry["table"],
            entry["what"],
            _text(entry["keys"]),
            "；".join(
                f"{name} {_text(value['value'])}{(' ' + value['unit']) if value.get('unit') and value['value'] is not None else ''}"
                for name, value in entry["values"].items()
            ),
            STATUSES.get(entry.get("status") or "", entry.get("status") or "按列"),
            _text(entry["assumption_ids"]),
            _text(entry["cited_by"]),
        ]
        for entry in registers.get("quantified_metrics", [])
    ]


LEAD_HEAD = ["编号", "来源", "对象", "时间窗", "寻找方式", "现象", "候选类别", "候选原因（含非损耗）", "缺的资料", "依赖的推断和现场假设", "依赖的分析偏好", "引用的底座值", "溯源", "状态"]


def lead_rows(registers: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for entry in registers.get("pending_verification", []):
        rows.append(
            [
                entry["id"],
                entry["source"],
                entry["object_text"],
                entry["time_text"],
                entry["finding"],
                entry["phenomenon"],
                _text(entry["categories"]),
                "\n".join(f"[{cause['kind']}] {cause['cause']}" for cause in entry["root_causes"]),
                _text(entry["missing_info"]),
                _text([*entry["depends_on_assumptions"], *entry["rests_on_parameters"]]),
                _text(entry.get("rests_on_preferences", [])),
                _text(entry["cited"]),
                entry.get("trace_text") or "",
                entry["status"],
            ]
        )
    return rows


MONITOR_HEAD = ["编号", "来源", "指标", "句柄", "现值", "单位", "算法", "对象", "相关线索", "状态"]


def monitor_rows(registers: dict[str, Any]) -> list[list[Any]]:
    return [
        [
            entry["id"],
            entry["origin"],
            entry["indicator"],
            entry.get("handle"),
            _text(entry.get("current_value")),
            entry.get("unit"),
            entry.get("method"),
            _text(entry.get("objects", [])),
            _text(entry["item_ids"]),
            STATUSES.get(entry["status"], entry["status"]),
        ]
        for entry in registers.get("monitoring_metrics", [])
    ]


def model_sheets(register: dict[str, Any]) -> list[tuple[str, list[str], list[list[Any]], list[str]]]:
    model = register.get("company_model")
    registers = register.get("registers")
    sheets: list[tuple[str, list[str], list[list[Any]], list[str]]] = []
    if registers:
        sheets += [
            ("统计结果", STATS_HEAD, stats_rows(registers), ["程序从数据算出的统计（统计参考，资格未核）：只描述数据，不说明是否损耗、是否必要，也不是基准。"]),
            ("监控指标候选", MONITOR_HEAD, monitor_rows(registers), ["可以按同一算法定期重算的指标；现值只是统计参考，不是目标值。用不用由人定。"]),
        ]
    if model:
        sheets += [
            ("任务定义", ["项", "内容"], task_rows(model), ["A01：这次任务的定义。"]),
            ("收件登记", SOURCE_HEAD, source_rows(model), ["A02：冻结在这次运行里的文件。"]),
            ("对象", OBJECT_HEAD, object_rows(model), ["A03：公司模型的对象；每个值带依据和状态。"]),
            ("关联", LINK_HEAD, link_rows(model), ["A03：计量、服务、归属关系；候选的没人确认。"]),
            ("参数", PARAMETER_HEAD, parameter_rows(model), ["A04：程序用到的参数。依据为“约定”的是分析偏好（怎样分析的设定，默认值和来历在默认偏好文件里，改法见“计算口径”页）；依据为“假设”的是现场假设，没人确认过，在现场核实清单里。"]),
            ("人工确认", CONFIRMATION_HEAD, confirmation_rows(model), ["A05：有权的人确认、否定或更正过的事项。"]),
            ("资料缺口", GAP_HEAD, gap_rows(model), ["程序推不出、要现场确认的事。"]),
        ]
        rows = inference_rows(model)
        if rows:
            sheets.append(("推断状态", INFERENCE_HEAD, rows, ["口径推断经确认后的状态。"]))
    return sheets


def add_model_sheets(book: Workbook, register: dict[str, Any]) -> None:
    for title, head, rows, notes in model_sheets(register):
        sheet = book.create_sheet(title)
        for note in notes:
            sheet.append([note])
        sheet.append(head)
        for cell in sheet[sheet.max_row]:
            cell.font = Font(bold=True)
        for row in rows:
            sheet.append(row)


def _table(head: list[str], rows: list[list[Any]]) -> str:
    cells = "".join(f"<th>{_e(cell)}</th>" for cell in head)
    body = "".join("<tr>" + "".join(f"<td>{_e(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table class='check'><tr>{cells}</tr>{body}</table>"


def review_flags(register: dict[str, Any], item: dict[str, Any]) -> list[str]:
    """Why a lead needs another look: an inference, a parameter or a cited value it rests on was rejected or does not read back."""

    model = register.get("company_model") or {}
    rejected_inferences = {entry["id"] for entry in model.get("inferences", []) if entry.get("status") == "REJECTED"}
    rejected_parameters = {entry["id"] for entry in model.get("parameters", []) if entry.get("status") == "REJECTED"}
    foundation = item.get("foundation") or {}
    flags = []
    hit = sorted(set(item.get("depends_on_assumptions", [])) & rejected_inferences)
    if hit:
        flags.append(f"所依据的推断 {'、'.join(hit)} 已被人否定")
    hit = sorted(set(foundation.get("rests_on_parameters", [])) & rejected_parameters)
    if hit:
        flags.append(f"所引底座值依赖的现场假设 {'、'.join(hit)} 已被人否定")
    if foundation.get("mismatched"):
        flags.append(f"引用的底座值 {'、'.join(foundation['mismatched'])} 和底座对不上")
    return flags


def parameter_lines(register: dict[str, Any]) -> list[str]:
    model = register.get("company_model") or {}
    lines = [
        f"{entry['id']} {entry['topic']} = {_text(entry['value'])}{entry.get('unit') or ''}（{entry['rule']}；{entry['replaces_when']}）"
        for entry in model.get("parameters", [])
        if entry["basis"] == "ASSUMED"
    ]
    lines += [f"{gap['id']} {gap['what']}：{gap['question']}（问{gap['ask_label']}）" for gap in model.get("gaps", [])]
    return lines


def model_html(register: dict[str, Any]) -> str:
    model = register.get("company_model")
    if not model:
        return ""
    parts = ["<h2>任务、参数和资料缺口</h2>"]
    rows = task_rows(model)
    if rows:
        parts.append("<h3>任务定义（A01）</h3>" + _table(["项", "内容"], rows))
    pairs = list(zip(parameter_rows(model), model.get("parameters", []), strict=True))
    assumed = [row for row, entry in pairs if entry["basis"] == "ASSUMED"]
    if assumed:
        parts.append("<h3>现场假设（A04，没人确认过）</h3>" + _table(PARAMETER_HEAD, assumed))
    preferences = [row for row, entry in pairs if entry.get("nature") == "preference"]
    if preferences:
        parts.append("<details><summary>分析偏好（A04，怎样分析的设定，不是工厂的事实）</summary>" + _table(PARAMETER_HEAD, preferences) + "</details>")
    rows = confirmation_rows(model)
    if rows:
        parts.append("<h3>人工确认（A05）</h3>" + _table(CONFIRMATION_HEAD, rows))
    rows = gap_rows(model)
    if rows:
        parts.append("<h3>资料缺口</h3>" + _table(GAP_HEAD, rows))
    parts.append("<details><summary>收件登记（A02）</summary>" + _table(SOURCE_HEAD, source_rows(model)) + "</details>")
    parts.append("<details><summary>对象和关联（A03）</summary>" + _table(OBJECT_HEAD, object_rows(model)) + _table(LINK_HEAD, link_rows(model)) + "</details>")
    return "\n".join(parts)


__all__ = ["add_model_sheets", "model_html", "model_sheets", "parameter_lines", "review_flags"]
