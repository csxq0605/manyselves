"""Single-source Markdown assembly for final review and report delivery."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import REPORT_MODULE_IDS, SpecialTopicPlan
from .taxonomy import REPORT_TAXONOMY


@dataclass(frozen=True, slots=True)
class CanonicalMarkdownTable:
    """Renderer-neutral approved table."""

    title: str
    headers: list[str]
    rows: list[list[str]]
    source_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CanonicalReportContent:
    """Fixed report fields consumed by the canonical Markdown composer."""

    title: str
    assessment_background: str
    findings_overview: str
    regional_executive_summary: str
    module_narratives: dict[str, str]
    risk_panorama: str
    dimension_risk_analysis: str
    data_gap_analysis: str
    improvement_action_plan: str
    special_topic_plan: SpecialTopicPlan | None = None
    special_topic_analysis: str | None = None
    tables: list[CanonicalMarkdownTable] = field(default_factory=list)
    trailing_markdown: str = ""


def strip_leading_module_heading(narrative: str, module_id: str) -> str:
    """Remove the module heading and nest its retained subheadings one level."""

    lines = narrative.splitlines()
    module_heading = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(
                rf"^#{{1,6}}\s+{re.escape(module_id)}(?:\.(?!\d)|\s|$)",
                line.strip(),
            )
        ),
        None,
    )
    if module_heading is None:
        return narrative
    del lines[module_heading]
    while module_heading < len(lines) and not lines[module_heading].strip():
        del lines[module_heading]
    lines = [
        (
            "#" + line
            if index >= module_heading and re.match(r"^#{1,5}\s+", line)
            else line
        )
        for index, line in enumerate(lines)
    ]
    return "\n".join(lines)


def markdown_table(table: CanonicalMarkdownTable) -> str:
    """Render one Markdown table with stable escaping."""

    def cell(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(cell(value) for value in table.headers) + " |",
        "| " + " | ".join("---" for _ in table.headers) + " |",
    ]
    lines.extend(
        "| " + " | ".join(cell(value) for value in row) + " |"
        for row in table.rows
    )
    return "\n".join(lines)


def _plain_title_line(line: str) -> str:
    value = re.sub(r"^#{1,6}\s*", "", line.strip())
    return value.strip("*_` ")


def _is_table_row(line: str) -> bool:
    value = line.strip()
    return value.startswith("|") and value.endswith("|") and value.count("|") >= 2


def _is_separator_row(line: str) -> bool:
    if not _is_table_row(line):
        return False
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _is_matching_table_title(line: str, title: str) -> bool:
    plain = _plain_title_line(line)
    return (
        plain in {title, f"表：{title}", f"表:{title}"}
        or plain.startswith(f"{title}（来源：")
    )


def _remove_handwritten_table(body: str, table: CanonicalMarkdownTable) -> str:
    """Remove a Chief-authored duplicate when the structured table owns the title."""

    lines = body.splitlines()
    index = 0
    while index < len(lines):
        if not _is_matching_table_title(lines[index], table.title):
            index += 1
            continue
        header_index = index + 1
        while header_index < len(lines) and not lines[header_index].strip():
            header_index += 1
        if (
            header_index + 1 >= len(lines)
            or not _is_table_row(lines[header_index])
            or not _is_separator_row(lines[header_index + 1])
        ):
            index += 1
            continue
        end = header_index + 2
        while end < len(lines) and _is_table_row(lines[end]):
            end += 1
        while end < len(lines) and not lines[end].strip():
            end += 1
        del lines[index:end]
    return "\n".join(lines)


def _section_body(value: str, tables: list[CanonicalMarkdownTable]) -> str:
    """Keep section prose while runtime exclusively owns headings and tables."""

    for table in tables:
        value = _remove_handwritten_table(value, table)
    return "\n".join(
        line
        for line in value.splitlines()
        if not re.match(
            r"^#{1,6}\s+\d+(?:\.\d+)*\.?\s+",
            line.strip(),
        )
        and line.strip() not in {"---", "***", "___"}
    ).strip()


def _table_markdown(table: CanonicalMarkdownTable) -> str:
    source_note = "、".join(table.source_ids)
    title = f"{table.title}（来源：{source_note}）" if source_note else table.title
    return "\n\n".join((title, markdown_table(table)))


def compose_canonical_markdown(content: CanonicalReportContent) -> str:
    """Compose the one canonical fixed-section report used by audit and delivery."""

    lines = [
        f"# {content.title}",
        "",
        "## 1. 配电评估概述",
        "",
        "### 1.1 评估背景",
        "",
        _section_body(content.assessment_background, content.tables),
        "",
        "### 1.2 健康度总览",
        "",
        _section_body(content.findings_overview, content.tables),
        "",
        "### 1.3 各区域执行摘要",
        "",
        _section_body(content.regional_executive_summary, content.tables),
        "",
        "## 2. 评估内容描述",
    ]
    for module_id in REPORT_MODULE_IDS:
        definition = REPORT_TAXONOMY[module_id]
        lines.extend(
            [
                "",
                f"### {module_id} {definition.title}",
                "",
                strip_leading_module_heading(
                    content.module_narratives[module_id],
                    module_id,
                ).strip(),
            ]
        )
    lines.extend(
        [
            "",
            "## 3. 结论与建议",
            "",
            "### 3.1 风险/问题汇总与概览",
            "",
            "#### 3.1.1 风险全景图",
            "",
            _section_body(content.risk_panorama, content.tables),
            "",
            "#### 3.1.2 各维度风险分析",
            "",
            _section_body(content.dimension_risk_analysis, content.tables),
            "",
            "#### 3.1.3 数据缺口分析",
            "",
            _section_body(content.data_gap_analysis, content.tables),
            "",
            "### 3.2 改善行动速查表",
            "",
            _section_body(content.improvement_action_plan, content.tables),
            "",
        ]
    )
    if content.special_topic_plan is not None:
        lines.extend(
            [
                "## 4. 专项问题分析",
                "",
                (content.special_topic_analysis or "").strip(),
                "",
            ]
        )
    lines.extend(_table_markdown(table) for table in content.tables)
    if content.trailing_markdown.strip():
        lines.extend(["", content.trailing_markdown.strip()])
    return "\n".join(lines)
