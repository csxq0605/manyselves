"""The parts of the register around the items: data and caliber, coverage by category, ledgers.

What the task definition calls O1 (data and caliber notes), O3 (coverage of
the 17 categories) and O4 (the ledgers of missing inputs, data issues and
possible necessary services).  Everything here is laid out by the program
from what the run holds; nothing is judged.
"""

from __future__ import annotations

import html
from typing import Any

from openpyxl import Workbook

from .items import CATEGORY_OUTCOMES, INPUT_KINDS, KB_ITEMS
from .states import STATE_ORDER, state_label


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _table(head: list[str], rows: list[list[Any]]) -> str:
    cells = "".join(f"<th>{_e(cell)}</th>" for cell in head)
    body = "".join("<tr>" + "".join(f"<td>{_e(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table class='check'><tr>{cells}</tr>{body}</table>"


# -- O1 --------------------------------------------------------------------


def state_rows(register: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for meter in register["meters"]:
        for row in meter.get("states") or []:
            rows.append(
                [
                    meter["meter_id"],
                    meter["name"],
                    row["state"],
                    state_label(row["state"], bool(meter.get("production_machines"))),
                    row["hours"],
                    row["covered_hours"],
                    row["energy_kwh"],
                    row["share_of_energy"],
                    row["avg_kw"],
                ]
            )
    return rows


STATE_HEAD = ["表", "名称", "状态", "含义", "小时", "有记录小时", "电量kWh", "占本表电量", "平均kW"]


def origin_lines(register: dict[str, Any]) -> list[str]:
    origin = register.get("manifest_origin")
    if not origin:
        return []
    lines = [
        "这次数据的读法（manifest）没有人写，由模型按程序对文件的探测起草，程序试读通过"
        + ("（用的是改过一轮的稿）" if origin.get("kept") == "corrected" else "")
        + "；读法里推不出的地方列在下面的口径推断里，待现场确认。"
    ]
    if origin.get("issues"):
        lines.append("程序试读时仍觉得不对、模型没有改的地方：" + "；".join(origin["issues"]))
    return lines


def plant_lines(register: dict[str, Any]) -> list[str]:
    states = register.get("states")
    if not states:
        return [*origin_lines(register), "没有生产记录，分不出生产、批间和停产。"]
    lines = [*origin_lines(register), states["note"] + "。"]
    coverage = states.get("coverage")
    if coverage:
        lines.append(f"生产记录覆盖 {coverage['start']} 至 {coverage['end']}。")
    hours = states["plant"]["hours"]
    lines.append(
        "全厂："
        + "，".join(f"{state} {hours[state]} 小时" for state in STATE_ORDER if state in hours)
        + f"；停产共 {states['plant']['stopped_stretches']} 段，最长几段 {states['plant']['longest_stopped_hours']} 小时。"
    )
    if states.get("uncertain_batches") or states.get("unplaced_batches"):
        lines.append(
            f"有 {states['uncertain_batches']} 个批次缺开始或结束时间，它可能在运行的那段时间记为 unknown；"
            f"{states['unplaced_batches']} 个批次两头都没有时间，放不进状态轴。"
        )
    return lines


ASSUMPTION_HEAD = ["编号", "来源", "事项", "推断", "依据", "不成立时影响", "怎样确认", "依赖它的项", "程序核对", "现场确认结果"]
ORIGINS = {
    "manifest": "准备资料时写的",
    "manifest-drafted": "模型起草读法时写的",
    "drafted": "模型按程序量出的事实起草",
}


def assumption_rows(register: dict[str, Any]) -> list[list[Any]]:
    return [
        [
            entry["id"],
            ORIGINS.get(entry.get("origin", "manifest"), entry.get("origin", "")),
            entry["topic"],
            entry["inference"],
            entry["basis"],
            entry.get("affects", ""),
            entry.get("how_to_confirm", ""),
            "、".join(entry.get("item_ids", [])),
            "；".join(entry.get("check_notes", [])),
            "",
        ]
        for entry in register.get("assumptions", [])
    ]


FACT_HEAD = ["编号", "涉及的表", "事项", "程序量出的事实"]


def fact_rows(register: dict[str, Any]) -> list[list[Any]]:
    return [
        [fact["id"], "、".join(fact["meter_ids"]), fact["topic"], fact["text"]]
        for fact in register.get("caliber_facts", [])
    ]


TOPOLOGY_HEAD = ["表", "名称", "台账里对上的行（候选）", "用能类别", "关联设备", "工厂树里的位置（候选）", "说明"]


def topology_rows(register: dict[str, Any]) -> list[list[Any]]:
    topology = register.get("topology") or {}
    return [
        [
            row["meter_id"],
            row["name"],
            row.get("asset_match") or ("未对上" if "asset_matches" in row else "没有台账"),
            row.get("category") or "",
            row.get("equipment") or "",
            row.get("tree_path") or ("未对上" if "tree_nodes" in row else "没有工厂树"),
            "；".join(row.get("notes", [])),
        ]
        for row in topology.get("meters", [])
    ]


EXCLUDED_HEAD = ["表", "起", "止", "原因"]


def excluded_rows(register: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for meter in register["meters"]:
        for span in meter.get("excluded_spans") or []:
            rows.append([meter["meter_id"], span["start"], span["end"], span["reason"]])
    return rows


# -- O3 --------------------------------------------------------------------

CATEGORY_HEAD = ["分区", "类别", "名称", "结果", "表", "缺的资料", "说明", "对应项"]


def category_rows(register: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for record in register["partitions"]:
        if record["outcome"] != "ok":
            continue
        for entry in record.get("category_coverage", []):
            outcome = "有项提议" if entry["outcome"] == "found" else CATEGORY_OUTCOMES.get(entry["outcome"], entry["outcome"])
            rows.append(
                [
                    record["partition_id"],
                    entry["item_id"],
                    KB_ITEMS[entry["item_id"]][1],
                    outcome,
                    "、".join(entry["meter_ids"]),
                    "、".join(INPUT_KINDS.get(kind, kind) for kind in entry.get("missing_inputs", [])),
                    entry.get("note", ""),
                    "、".join(entry.get("item_ids", [])),
                ]
            )
    return rows


# -- O4 --------------------------------------------------------------------

MISSING_HEAD = ["缺的资料", "卡住的项", "卡住的类别（分区：表）"]


def missing_rows(register: dict[str, Any]) -> list[list[Any]]:
    """What is missing, and which items and categories wait on it."""

    items: dict[str, list[str]] = {}
    for item in register["items"]:
        for text in item.get("missing_info", []):
            items.setdefault(text.strip(), []).append(item["id"])
        for assumption in item.get("depends_on_assumptions", []):
            items.setdefault(f"现场确认口径推断 {assumption}", []).append(item["id"])
    categories: dict[str, list[str]] = {}
    for record in register["partitions"]:
        for entry in record.get("category_coverage", []) if record["outcome"] == "ok" else []:
            for kind in entry.get("missing_inputs", []):
                label = INPUT_KINDS.get(kind, kind)
                categories.setdefault(label, []).append(
                    f"{entry['item_id']}（{record['partition_id']}：{'、'.join(entry['meter_ids'])}）"
                )
    rows = [[text, "、".join(ids), ""] for text, ids in sorted(items.items())]
    rows += [[label, "", "；".join(blocked)] for label, blocked in sorted(categories.items())]
    return rows


SIDE_HEAD = ["项", "表", "现象", "为什么列在这里"]


def side_rows(register: dict[str, Any]) -> list[list[Any]]:
    """Items that point at a data issue or a possible necessary service: they are not losses to count."""

    rows = []
    for item in register["items"]:
        reasons = []
        categories = {proposal["item_id"] for proposal in item.get("candidate_categories", [])}
        if "MEASUREMENT_ISSUE" in categories:
            reasons.append("提议了 MEASUREMENT_ISSUE（计量或时间口径待核，不算节能）")
        if "NECESSARY" in categories:
            reasons.append("提议了 NECESSARY（可能是必要服务）")
        if item.get("possible_necessary_services"):
            reasons.append("可能的必要服务：" + "、".join(item["possible_necessary_services"]))
        if reasons:
            rows.append([item["id"], "、".join(item["meter_ids"]), item["phenomenon"], "；".join(reasons)])
    return rows


# -- output ----------------------------------------------------------------


def add_sheets(book: Workbook, register: dict[str, Any]) -> None:
    sheets = [
        ("状态轴", STATE_HEAD, state_rows(register), plant_lines(register)),
        ("口径推断", ASSUMPTION_HEAD, assumption_rows(register), ["推断都待现场确认；最后一列留给人填。"]),
        ("口径依据", FACT_HEAD, fact_rows(register), ["程序从数据量出的事实，推断引用的编号在这里。"]),
        ("表计对应", TOPOLOGY_HEAD, topology_rows(register), (register.get("topology") or {}).get("notes", [])),
        ("未纳入的数据", EXCLUDED_HEAD, excluded_rows(register), []),
        ("类别覆盖", CATEGORY_HEAD, category_rows(register), ["知识库 17 类在每个分区每块表上：有项提议，或看过没找到、不适用、缺资料。"]),
        ("补资料台账", MISSING_HEAD, missing_rows(register), ["缺的资料和它卡住的项、类别。"]),
        ("数据问题与必要服务", SIDE_HEAD, side_rows(register), ["这些项指向计量问题或可能的必要服务，人判断时不当作损耗计。"]),
    ]
    for title, head, rows, notes in sheets:
        sheet = book.create_sheet(title)
        for note in notes:
            sheet.append([note])
        sheet.append(head)
        for row in rows:
            sheet.append(row)


def data_html(register: dict[str, Any]) -> str:
    parts = ["<h2>数据与口径</h2>"]
    parts += [f"<p>{_e(line)}</p>" for line in plant_lines(register)]
    rows = state_rows(register)
    if rows:
        parts.append(_table(STATE_HEAD, rows))
    rows = assumption_rows(register)
    if rows:
        parts.append("<h3>数据口径推断（待现场确认）</h3>")
        parts.append(_table(ASSUMPTION_HEAD, rows))
    rows = fact_rows(register)
    if rows:
        parts.append("<details><summary>口径依据：程序从数据量出的事实</summary>")
        parts.append(_table(FACT_HEAD, rows))
        parts.append("</details>")
    rows = topology_rows(register)
    if rows:
        parts.append("<h3>表计和台账、工厂树的对应</h3>")
        parts += [f"<p>{_e(line)}</p>" for line in (register.get("topology") or {}).get("notes", [])]
        parts.append(_table(TOPOLOGY_HEAD, rows))
    rows = excluded_rows(register)
    if rows:
        parts.append("<h3>未纳入的数据</h3>")
        parts.append(_table(EXCLUDED_HEAD, rows))
    return "\n".join(parts)


def ledgers_html(register: dict[str, Any]) -> str:
    parts = []
    rows = category_rows(register)
    if rows:
        parts.append("<h2>17 类覆盖</h2>")
        parts.append(_table(CATEGORY_HEAD, rows))
    rows = missing_rows(register)
    if rows:
        parts.append("<h2>补资料台账</h2>")
        parts.append(_table(MISSING_HEAD, rows))
    rows = side_rows(register)
    if rows:
        parts.append("<h2>数据问题与可能的必要服务</h2>")
        parts.append(_table(SIDE_HEAD, rows))
    return "\n".join(parts)


def markdown_table(head: list[str], rows: list[list[Any]]) -> list[str]:
    def cell(value: Any) -> str:
        return " ".join(str("" if value is None else value).split()).replace("|", "／")

    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(cell(value) for value in row) + " |" for row in rows]
    return lines + [""]


def overview_markdown(register: dict[str, Any]) -> list[str]:
    lines = ["## 数据与口径", ""]
    lines += [*plant_lines(register), ""]
    for title, head, rows in (
        ("状态轴", STATE_HEAD, state_rows(register)),
        ("表计和台账、工厂树的对应", TOPOLOGY_HEAD, topology_rows(register)),
        ("未纳入的数据", EXCLUDED_HEAD, excluded_rows(register)),
        ("17 类覆盖", CATEGORY_HEAD, category_rows(register)),
        ("补资料台账", MISSING_HEAD, missing_rows(register)),
    ):
        if rows:
            lines += [f"### {title}", "", *markdown_table(head, rows)]
    return lines


__all__ = [
    "add_sheets",
    "assumption_rows",
    "category_rows",
    "fact_rows",
    "data_html",
    "ledgers_html",
    "missing_rows",
    "overview_markdown",
    "plant_lines",
    "side_rows",
    "state_rows",
]
