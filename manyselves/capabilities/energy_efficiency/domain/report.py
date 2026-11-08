"""Assemble the report from the items a person or a person's rules kept.

The report holds the kept items as they are in the register, the items that
need more data, and what was looked at.  It adds no totals: items may count
the same records, and the program does not know how meters nest.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from .items import CAUSE_KINDS
from .register import (
    PAGE_STYLE,
    cost_note,
    figures,
    item_html,
    method_text,
    overlap_text,
    partition_html,
    precheck_lines,
    relations_text,
    sort_key,
)
from .verification import checklist, checklist_html, checklist_markdown

_LABELS = {"keep": "保留", "drop": "不保留", "needs_data": "需补资料", "unselected": "未选"}


def _by(entry: dict[str, Any]) -> str:
    if entry["by"] == "person":
        return "人工"
    if entry["by"]:
        return f"规则 {entry['by'].removeprefix('rule ')}"
    return ""


def _md(value: Any) -> str:
    """Text from the model or the manifest kept on one Markdown line.

    Every such text is written after a label (``- 现象：``), never at the
    start of a line, so collapsing line breaks is enough to keep it from
    starting a heading or a list.
    """

    return re.sub(r"\s*[\r\n]+\s*", " ", "" if value is None else str(value)).strip()


def _decision_text(entry: dict[str, Any]) -> str:
    text = f"{_LABELS[entry['decision']]}（{_by(entry)}）"
    return text + (f"，理由：{entry['note']}" if entry["note"] else "")


def _md_item(item: dict[str, Any], names: dict[str, str], entry: dict[str, Any]) -> list[str]:
    meters = "、".join(f"{meter_id} {names.get(meter_id, '')}".strip() for meter_id in item["meter_ids"])
    lines = [f"### {item['id']} · {_md(meters)}", ""]
    lines.append(
        f"- 来源：{'模型发现' if item['source'] == 'model' else '规则检出'}；选择：{_md(_decision_text(entry))}"
    )
    lines.append(f"- 现象：{_md(item['phenomenon'])}")
    lines.append("- 时间窗：" + "；".join(f"{w['start']} 至 {w['end']}" for w in item["windows"]))
    lines.append(f"- 寻找方式：{_md(method_text(item))}。{_md(item['finding_method']['description'])}")
    if item["reasoning"]:
        lines.append(f"- 思考过程：{_md(item['reasoning'])}")
    if item.get("candidate_categories"):
        lines.append(
            "- 候选类别（模型提议，未核实）："
            + _md("；".join(f"{p['item_id']} {p['title']}：{p['why']}" for p in item["candidate_categories"]))
        )
    if item["computed"].get("category_notes"):
        lines.append(f"- 类别缺的资料：{_md('；'.join(item['computed']['category_notes']))}")
    if item["root_cause_candidates"]:
        lines.append("- 候选解释（均未核实）：")
        for cause in item["root_cause_candidates"]:
            kind = CAUSE_KINDS.get(cause.get("kind", "loss"), "")
            lines.append(
                f"  - [{kind}] {_md(cause['cause'])}（依据：{cause['basis']}，{_md(cause['basis_detail'])}；"
                f"核实方法：{_md(cause['how_to_check'])}）"
            )
    if item["missing_info"]:
        lines.append(f"- 还缺的资料：{_md('；'.join(item['missing_info']))}")
    if item["monitoring_suggestion"]:
        lines.append(f"- 监测建议：{_md(item['monitoring_suggestion'])}")
    shown = [f"{name} {value}" for name, value in figures(item) if value is not None]
    if shown:
        lines.append("- 程序计算：" + "；".join(shown))
    note = cost_note(item)
    if note:
        lines.append(f"- 电费说明：{note}")
    unknown = [meter_id for meter_id, entry in item["computed"]["per_meter"].items() if entry.get("unknown_meter")]
    if unknown:
        lines.append(f"- 表 {_md('、'.join(unknown))} 不在资料里，没有算数")
    if len(item["meter_ids"]) > 1:
        lines.append("- 多块表的数不合计（表之间可能有上下级关系），逐表数值见候选清单 JSON。")
    if item.get("related_ids"):
        lines.append(f"- 模型标的相关项：{_md(relations_text(item))}")
    if item["computed"].get("relation_notes"):
        lines.append(f"- 关系核对：{_md('；'.join(item['computed']['relation_notes']))}")
    if item["computed"]["overlap"]:
        lines.append(f"- 与其他项重叠：{overlap_text(item)}")
    if item["rule_notes"]:
        lines.append(f"- 模型对这条规则项的说明：{_md('；'.join(item['rule_notes']))}")
    if item["check_issues"]:
        lines.append(f"- 未解决的检查问题：{_md('；'.join(item['check_issues']))}")
    if item["computed"]["number_hints"]:
        lines.append(f"- 数字提示：{_md('；'.join(item['computed']['number_hints']))}")
    lines.append(f"- 证据：{_md('、'.join(item['evidence_ids']))}")
    lines.append("")
    return lines


def build_report(register: dict[str, Any], selection: dict[str, Any], sources: list[dict[str, Any]]) -> tuple[str, str]:
    names = {meter["meter_id"]: meter["name"] for meter in register["meters"]}
    decisions = {entry["item_id"]: entry for entry in selection["decisions"]}
    items = sorted(register["items"], key=sort_key)
    kept = [item for item in items if decisions[item["id"]]["decision"] == "keep"]
    needs = [item for item in items if decisions[item["id"]]["decision"] == "needs_data"]
    dropped = [item for item in items if decisions[item["id"]]["decision"] == "drop"]
    counts = selection["counts"]

    md = [
        f"# {_md(register['site'])} 候选损耗项报告（待审）",
        "",
        f"数据期：{register['period']['start']} 至 {register['period']['end']}。",
        f"候选清单共 {len(items)} 项：保留 {counts['keep']}、需补资料 {counts['needs_data']}、不保留 {counts['drop']}、未选 {counts['unselected']}。",
        "",
    ]
    for warning in selection.get("warnings", []):
        md.append(f"> 规则提示：{_md(warning)}")
    md += [
        "",
        "本报告只收录人或人定的规则保留的项。保留只表示值得核实，不表示损耗已成立；"
        "“涉及电量”是这段时间实际用的电，不是可避免的电量。所有数值由程序从数据算出；候选原因均未经现场核实。"
        "各项可能计入同一批记录，表之间也可能有上下级关系，所以不给合计。",
        "",
        "## 保留的候选损耗项",
        "",
    ]
    if not kept:
        md += ["（没有保留的项）", ""]
    for item in kept:
        md += _md_item(item, names, decisions[item["id"]])
    md += ["## 需补资料的项", ""]
    if not needs:
        md += ["（没有）", ""]
    for item in needs:
        entry = decisions[item["id"]]
        missing = "；".join(item["missing_info"]) or "未写"
        remark = f"；备注：{entry['note']}" if entry["note"] else ""
        md.append(f"- {item['id']}：{_md(item['phenomenon'])}（还缺：{_md(missing + remark)}）")
    md += ["", "## 不保留的项", ""]
    if not dropped:
        md += ["（没有）", ""]
    for item in dropped:
        entry = decisions[item["id"]]
        md.append(f"- {item['id']}（{_by(entry)}）" + (f"：{_md(entry['note'])}" if entry["note"] else ""))
    md += [""]
    lists = [checklist(item, decisions[item["id"]]) for item in [*kept, *needs]]
    md += checklist_markdown(lists)
    md += ["## 查了什么", ""]
    for record in register["partitions"]:
        if record["outcome"] != "ok":
            meters = _md("、".join(record["meter_ids"]))
            md.append(f"- 分区 {record['partition_id']}（{meters}）：失败，{_md(record.get('error', ''))}")
            continue
        md.append(f"- 分区 {record['partition_id']} {_md(record['label'])}（{_md('、'.join(record['meter_ids']))}）：")
        for entry in record["coverage"]:
            items_text = f"（对应 {'、'.join(entry['item_ids'])}）" if entry["item_ids"] else ""
            md.append(f"  - {entry['method_id']}：{entry['outcome']}，{_md(entry['note'])}{items_text}")
        if record["issues"]:
            md.append(f"  - 未解决的检查问题：{_md('；'.join(record['issues']))}")
        if record["notes"]:
            md.append(f"  - 模型的分区说明：{_md(record['notes'])}")
    for line in precheck_lines(register):
        md.append(f"- {_md(line)}")
    md += ["", "## 数据与来源", ""]
    for meter in register["meters"]:
        notes = "；".join(meter["quality_notes"]) or "无异常"
        md.append(f"- {meter['meter_id']} {_md(meter['name'])}：{_md(notes)}")
    for source in sources:
        md.append(f"- 文件 {_md(source['file'])}：sha256 {source['sha256'][:16]}…")
    markdown = "\n".join(md) + "\n"

    body = [
        f"<h1>{html.escape(register['site'])} 候选损耗项报告（待审）</h1>",
        f"<p>数据期 {html.escape(register['period']['start'])} 至 {html.escape(register['period']['end'])}；"
        f"保留 {counts['keep']}、需补资料 {counts['needs_data']}、不保留 {counts['drop']}、未选 {counts['unselected']}。</p>",
        "<p>本报告只收录人或人定的规则保留的项。保留只表示值得核实，不表示损耗已成立；“涉及电量”不是可避免的电量。"
        "所有数值由程序算出；候选原因均未经现场核实；不给合计。</p>",
        *[f"<p class='warn'>规则提示：{html.escape(warning)}</p>" for warning in selection.get("warnings", [])],
        "<h2>保留的候选损耗项</h2>",
    ]
    body += [item_html(item, names, _decision_text(decisions[item["id"]])) for item in kept] or [
        "<p>（没有保留的项）</p>"
    ]
    if needs:
        body.append("<h2>需补资料的项</h2><ul>")
        for item in needs:
            entry = decisions[item["id"]]
            missing = "；".join(item["missing_info"]) or "未写"
            remark = f"；备注：{entry['note']}" if entry["note"] else ""
            body.append(
                f"<li>{html.escape(item['id'])}：{html.escape(item['phenomenon'])}"
                f"（还缺：{html.escape(missing + remark)}）</li>"
            )
        body.append("</ul>")
    if dropped:
        body.append("<h2>不保留的项</h2><ul>")
        for item in dropped:
            entry = decisions[item["id"]]
            reason = f"：{entry['note']}" if entry["note"] else ""
            body.append(f"<li>{html.escape(item['id'])}（{html.escape(_by(entry))}）{html.escape(reason)}</li>")
        body.append("</ul>")
    body.append(checklist_html(lists))
    body.append(partition_html(register))
    body.append("<h2>数据与来源</h2><ul>")
    for meter in register["meters"]:
        notes = "；".join(meter["quality_notes"]) or "无异常"
        body.append(f"<li>{html.escape(meter['meter_id'])} {html.escape(meter['name'])}：{html.escape(notes)}</li>")
    for source in sources:
        body.append(f"<li>文件 {html.escape(source['file'])}：sha256 {html.escape(source['sha256'][:16])}…</li>")
    body.append("</ul>")
    page = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>候选损耗项报告</title><style>{PAGE_STYLE}</style></head><body>"
        + "\n".join(body)
        + "</body></html>"
    )
    return markdown, page


def write_report(directory: Path, markdown: str, page: str) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory / "report.md", directory / "report.html"]
    paths[0].write_text(markdown, encoding="utf-8")
    paths[1].write_text(page, encoding="utf-8")
    return paths


__all__ = ["build_report", "write_report"]
