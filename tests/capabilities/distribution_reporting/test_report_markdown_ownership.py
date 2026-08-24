import ast
import json
import subprocess
import sys
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    SpecialTopicPlan,
)

MARKDOWN_MODULE = (
    "manyselves.capabilities.distribution_reporting.domain.report_markdown"
)


def test_report_markdown_imports_without_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    f"import {MARKDOWN_MODULE} as report_markdown",
                    "print(json.dumps({",
                    "    'module': report_markdown.__name__,",
                    "    'core_reporting': sorted(",
                    "        name for name in sys.modules",
                    "        if name.startswith('manyselves.core.reporting')",
                    "    ),",
                    "}))",
                )
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "module": MARKDOWN_MODULE,
        "core_reporting": [],
    }


def test_report_markdown_symbols_are_physically_capability_owned() -> None:
    report_markdown = import_module(MARKDOWN_MODULE)

    for symbol_name in (
        "CanonicalMarkdownTable",
        "CanonicalReportContent",
        "strip_leading_module_heading",
        "markdown_table",
        "compose_canonical_markdown",
    ):
        assert getattr(report_markdown, symbol_name).__module__ == MARKDOWN_MODULE

    source = Path(report_markdown.__file__).read_text(encoding="utf-8")
    imports = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert all(
        not any(
            name.name.startswith("manyselves.core.reporting")
            for name in node.names
        )
        if isinstance(node, ast.Import)
        else not (node.module or "").startswith("manyselves.core.reporting")
        for node in imports
    )
    assert find_spec("manyselves.core.reporting") is None


def test_canonical_markdown_exact_output_remains_stable() -> None:
    report_markdown = import_module(MARKDOWN_MODULE)
    module_narratives = {
        module_id: (
            f"## {module_id} 旧模块标题\n\n"
            f"### {module_id}.x 子节\n\n"
            f"{module_id} 正文"
        )
        for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5")
    }
    plan = SpecialTopicPlan(
        source_ref=Path("Inputs/topic.md"),
        source_sha256="0" * 64,
        sections=[
            {"section_id": "4.1", "title": "首个专项", "requirement": "要求一"},
            {"section_id": "4.2", "title": "第二专项", "requirement": "要求二"},
        ],
    )
    content = report_markdown.CanonicalReportContent(
        title="固定报告",
        assessment_background="## 1.1 旧标题\n背景正文",
        findings_overview="总览正文",
        regional_executive_summary="区域正文",
        module_narratives=module_narratives,
        risk_panorama="风险正文",
        dimension_risk_analysis="维度正文",
        data_gap_analysis="缺口正文",
        improvement_action_plan=(
            "行动说明\n\n动作表\n\n| 旧列 |\n| --- |\n| 旧值 |\n\n尾段"
        ),
        special_topic_plan=plan,
        special_topic_analysis=(
            "### 4.1 首个专项\n\n专项一\n\n### 4.2 第二专项\n\n专项二"
        ),
        tables=[
            report_markdown.CanonicalMarkdownTable(
                title="动作表",
                headers=["动作", "责任"],
                rows=[["整改|复核", "运维\n团队"]],
                source_ids=["E-1"],
            )
        ],
        trailing_markdown="附录尾注",
    )

    assert report_markdown.compose_canonical_markdown(content) == (
        "# 固定报告\n\n"
        "## 1. 配电评估概述\n\n"
        "### 1.1 评估背景\n\n"
        "背景正文\n\n"
        "### 1.2 健康度总览\n\n"
        "总览正文\n\n"
        "### 1.3 各区域执行摘要\n\n"
        "区域正文\n\n"
        "## 2. 评估内容描述\n\n"
        "### 2.1 配电系统架构问题\n\n"
        "#### 2.1.x 子节\n\n"
        "2.1 正文\n\n"
        "### 2.2 环境工况风险\n\n"
        "#### 2.2.x 子节\n\n"
        "2.2 正文\n\n"
        "### 2.3 针对故障的保护\n\n"
        "#### 2.3.x 子节\n\n"
        "2.3 正文\n\n"
        "### 2.4 配电设备/元件内在风险\n\n"
        "#### 2.4.x 子节\n\n"
        "2.4 正文\n\n"
        "### 2.5 运维管理与风险管控机制\n\n"
        "#### 2.5.x 子节\n\n"
        "2.5 正文\n\n"
        "## 3. 结论与建议\n\n"
        "### 3.1 风险/问题汇总与概览\n\n"
        "#### 3.1.1 风险全景图\n\n"
        "风险正文\n\n"
        "#### 3.1.2 各维度风险分析\n\n"
        "维度正文\n\n"
        "#### 3.1.3 数据缺口分析\n\n"
        "缺口正文\n\n"
        "### 3.2 改善行动速查表\n\n"
        "行动说明\n\n"
        "尾段\n\n"
        "## 4. 专项问题分析\n\n"
        "### 4.1 首个专项\n\n"
        "专项一\n\n"
        "### 4.2 第二专项\n\n"
        "专项二\n\n"
        "动作表（来源：E-1）\n\n"
        "| 动作 | 责任 |\n"
        "| --- | --- |\n"
        "| 整改\\|复核 | 运维 团队 |\n\n"
        "附录尾注"
    )
