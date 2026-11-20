"""Assemble the report from the selection: what a person kept, what the screening found valid, what waits.

Every row belongs to a loss item someone proposed (the analyst, or a
person's rule).  The program writes every number from the register; how the
item was found, the thinking and the model's opinions are shown as opinions.
Excess amounts of different rows are not added: rows on one meter may share
cells (overlap) and meters may nest.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from .model_sheets import model_html, parameter_lines
from .preparation import ROLES
from .workbook import (
    DISPOSITIONS,
    PAGE_STYLE,
    coverage_html,
    decomposition_text,
    levels_text,
    opinion_texts,
    overlap_text,
    proposal_cause_text,
    range_text,
    set_aside_text,
    stability_text,
)

_LABELS = {"keep": "保留", "drop": "不保留", "needs_data": "需补资料/待定"}


def _by(entry: dict[str, Any]) -> str:
    if entry["by"] == "person":
        return "人工"
    if entry["by"].startswith("rule "):
        return f"规则 {entry['by'].removeprefix('rule ')}"
    return f"筛选 {entry['by'].removeprefix('screen ')}"


def _md(value: Any) -> str:
    """Text kept on one Markdown line (always written after a label)."""

    return re.sub(r"\s*[\r\n]+\s*", " ", "" if value is None else str(value)).strip()


def _decision_text(entry: dict[str, Any]) -> str:
    # A screening decision's reason is the screening's own, written once after it.
    note = entry["note"] if entry["note"] and not entry["by"].startswith("screen ") else ""
    return f"{_LABELS[entry['decision']]}（{_by(entry)}）" + (f"，理由：{note}" if note else "")


def _amount(item: dict[str, Any], entry: dict[str, Any]) -> str:
    level = entry.get("level") or item["default_level"]
    chosen = next((value for value in item["levels"] if value["label"] == level), None)
    amount = chosen["excess_kwh"] if chosen else item["excess_kwh"]
    spread = range_text(item)
    text = f"{amount:,.0f} kWh（参照：{level}{'，人选' if entry.get('level') else '，默认'}"
    return text + (f"；{spread}）" if spread else "）")


def _size(item: dict[str, Any]) -> str:
    """The item's amount in one phrase; an item outside the totals says so, with its money when it has one."""

    text = f"{item['excess_kwh']:,.0f} kWh"
    if item.get("in_totals", True):
        return text
    money = f"，金额上限 {item['money']:,.0f}" if item.get("money") is not None else ""
    return f"{text}{money}，不进电量合计"


def _item_lines(item: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    opinions = opinion_texts(item)
    proposal = item.get("proposal") or {}
    lines = [
        f"### {item['id']} · {_md(item['meter'])} {_md(item['name'])} · {_md(item['lens_name'])}",
        "",
        f"- 选择：{_md(_decision_text(entry))}；筛选：{item['screen']['result']}（{item['screen']['rule']} {item['screen']['reason']}）",
        f"- 损耗项：{_md(item.get('item'))} {_md(item.get('item_title'))}（{_md(item.get('origin'))}）",
        f"- 说明：{_md(item['title'])}",
        f"- 时段：{_md(item['selection'])}",
        f"- 超出：{_amount(item, entry)}"
        + (f"；占本表 {item['scalars']['share_of_meter']:.1%}" if item["scalars"].get("share_of_meter") is not None else "")
        + ("" if item.get("in_totals", True) else "；不进电量合计"),
        f"- 参照档：{_md(levels_text(item))}",
    ]
    if item.get("money") is not None:
        lines.append(f"- 金额（上限）：{item['money']:,.0f}")
    text = decomposition_text(item)
    if text:
        lines.append(f"- 拆分：{_md(text)}")
    text = stability_text(item)
    if text:
        lines.append(f"- 稳定性：{_md(text)}")
    for label, value in (("寻找方式", proposal.get("finding")), ("思考过程", proposal.get("reasoning")), ("提项时的原因与检验", proposal_cause_text(item)), ("还缺的资料", proposal.get("missing_info"))):
        if value:
            lines.append(f"- {label}（模型，未核实）：{_md(value)}")
    if item.get("set_aside"):
        lines.append(f"- 模型暂不立项的理由（意见，不进筛选）：{_md(set_aside_text(item))}")
    if item.get("overlaps"):
        lines.append(f"- 和其他项重叠：{_md(overlap_text(item))}（重叠部分不要相加）")
    if item.get("notes"):
        lines.append(f"- 程序说明：{_md('；'.join(item['notes']))}")
    for label, key in (("意见", "opinion"), ("参照意见", "reference"), ("原因候选与检验", "causes"), ("措施选项", "measures"), ("现场问题", "questions"), ("相关项", "related")):
        if opinions[key]:
            lines.append(f"- {label}（模型，未核实）：{_md(opinions[key])}")
    lines.append("")
    return lines


def build_report(register: dict[str, Any], selection: dict[str, Any], sources: list[dict[str, Any]]) -> tuple[str, str]:
    decisions = {entry["item_id"]: entry for entry in selection["decisions"]}
    items = [item for item in register["items"] if item["id"] in decisions]
    kept = [item for item in items if decisions[item["id"]]["decision"] == "keep"]
    waiting = [item for item in items if decisions[item["id"]]["decision"] == "needs_data" and item["excess_kwh"] > 0]
    dropped = [
        item
        for item in items
        if decisions[item["id"]]["decision"] == "drop" and not decisions[item["id"]]["by"].startswith("screen ")
    ]
    counts = selection["counts"]
    summary = register["summary"]
    md = [
        f"# {_md(register['site'])} 损耗项报告（待审）",
        "",
        f"数据期：{register['period']['start']} 至 {register['period']['end']}。",
        f"损耗项 {register['counts'].get('loss_items', 0)} 个（其中够大却没有项采用、程序自成一项的 {register['counts'].get('loss_items_unclaimed', 0)} 个），落在 {len(items)} 行：保留 {counts['keep']}、需补资料或待定 {counts['needs_data']}、不保留 {counts['drop']}。",
        f"程序差额 {summary.get('differences_kwh', 0):,.0f} kWh，损耗项的行共 {summary['found_kwh']:,.0f} kWh（同一格只算一次；按参照规格回算的行可以超出程序差额）；没解释的剩余 {summary['residual_open_kwh']:,.0f} kWh。",
        "",
    ]
    md += [f"> 提示：{_md(warning)}" for warning in selection.get("warnings", [])]
    md += [
        "",
        "损耗项由模型提出（或来自人定的参照规则）；够大的程序差额没有项采用的，程序让它自成一项（编号 D-），最多待定。保留的行来自人在台账上的取舍、人写的规则，或偏好规则下的筛选结果（有效）。"
        "所有数由程序在全期按写明的参照算；参照换一档会变多少，每项都列出。寻找方式、思考过程、原因、措施和现场问题是模型的意见，均未经现场核实。各项不相加。",
        "",
        "## 保留的损耗项",
        "",
    ]
    if not kept:
        md += ["（没有保留的项）", ""]
    for item in kept:
        md += _item_lines(item, decisions[item["id"]])
    md += ["## 需补资料或待定的项", ""]
    if not waiting:
        md += ["（没有）", ""]
    for item in waiting:
        entry = decisions[item["id"]]
        md.append(f"- {item['id']}（{_md(item['meter'])} {_md(item['lens_name'])}，{_size(item)}）：{_md(entry['note'])}（{_by(entry)}）")
    md += ["", "## 人或规则不保留的项", ""]
    if not dropped:
        md += ["（没有）", ""]
    for item in dropped:
        entry = decisions[item["id"]]
        md.append(f"- {item['id']}（{_by(entry)}）" + (f"：{_md(entry['note'])}" if entry["note"] else ""))
    md += ["", "## 覆盖与剩余", "", "| 表 | 全期 kWh | 没看过 kWh | 程序差额 kWh | 损耗项 kWh | 没有项采用 kWh | 有效 kWh | 待定 kWh | 没解释的剩余 kWh |", "|---|---|---|---|---|---|---|---|---|"]
    for row in register["coverage"]:
        md.append(
            f"| {_md(row['meter'])} {_md(row['name'])} | {row['energy_kwh']:,.0f} | {row['not_examined_kwh']:,.0f} | {row.get('differences_kwh', 0):,.0f} | {row.get('items_kwh', 0):,.0f} | {row.get('unclaimed_kwh', 0):,.0f} | {row['valid_kwh']:,.0f} | {row['pending_kwh']:,.0f} | {row['residual_open_kwh']:,.0f} |"
        )
    md += ["", "## 触发与处置", ""]
    for trigger in register["triggers"]:
        answer = trigger.get("answer") or {}
        md.append(f"- {trigger['id']} {_md(trigger['title'])}：{_md(trigger['text'])}；处置（模型）：{DISPOSITIONS.get(answer.get('disposition'), '没有处置')}" + (f"，{_md(answer.get('note'))}" if answer.get("note") else ""))
    if register["site_questions"]:
        md += ["", "## 现场清单（模型提的问题）", ""]
        for question in register["site_questions"]:
            md.append(f"- [{_md(question['source'])}] {_md(question['question'])}（问{ROLES.get(question['ask_role'], question['ask_role'])}）")
    lines = parameter_lines(register)
    if lines:
        md += ["", "## 现场假设和资料缺口", ""] + [f"- {_md(line)}" for line in lines]
    md += ["", "## 数据与来源", ""]
    for meter in register["meters"]:
        notes = "；".join(meter["quality_notes"]) or "无异常"
        md.append(f"- {meter['meter_id']} {_md(meter['name'])}：{_md(notes)}")
    for source in sources:
        md.append(f"- 文件 {_md(source['file'])}：sha256 {source['sha256'][:16]}…")
    markdown = "\n".join(md) + "\n"

    def item_html(item: dict[str, Any]) -> str:
        rows = "".join(f"<li>{html.escape(line[2:])}</li>" for line in _item_lines(item, decisions[item["id"]])[2:] if line.startswith("- "))
        return f"<div class='item'><h3>{html.escape(item['id'])} · {html.escape(item['meter'])} {html.escape(item['name'])} · {html.escape(item['lens_name'])}</h3><ul>{rows}</ul></div>"

    body = [
        f"<h1>{html.escape(register['site'])} 损耗项报告（待审）</h1>",
        f"<p>数据期 {html.escape(register['period']['start'])} 至 {html.escape(register['period']['end'])}；保留 {counts['keep']}、需补资料或待定 {counts['needs_data']}、不保留 {counts['drop']}。</p>",
        "<p class='note'>数由程序按写明的参照算，参照换档的范围每项都列出；原因、措施和现场问题是模型的意见，未经核实；各项不相加。</p>",
        *[f"<p class='warn'>提示：{html.escape(warning)}</p>" for warning in selection.get("warnings", [])],
        "<h2>保留的损耗项</h2>",
        *([item_html(item) for item in kept] or ["<p>（没有保留的项）</p>"]),
    ]
    if waiting:
        body.append("<h2>需补资料或待定的项</h2><ul>")
        body += [f"<li>{html.escape(item['id'])}（{html.escape(_size(item))}）：{html.escape(decisions[item['id']]['note'])}</li>" for item in waiting]
        body.append("</ul>")
    body.append(coverage_html(register))
    body.append(model_html(register))
    page = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>损耗项报告</title><style>{PAGE_STYLE}</style></head><body>" + "\n".join(body) + "</body></html>"
    )
    return markdown, page


def write_report(directory: Path, markdown: str, page: str) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory / "report.md", directory / "report.html"]
    paths[0].write_text(markdown, encoding="utf-8")
    paths[1].write_text(page, encoding="utf-8")
    return paths


__all__ = ["build_report", "write_report"]
