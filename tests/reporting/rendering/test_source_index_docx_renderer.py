from pathlib import Path

import pytest
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.shared import Inches

from manyselves.core.reporting.rendering.source_index_docx_renderer import (
    SourceIndexDocxRenderer,
)

SOURCE_INDEX = """## 证据与来源索引

### 脚注对应关系

- [1] C-2.1-001：E-001

### 项目证据 E-*

- E-001：配电柜检查记录；Inputs/S4-4.xlsx；Sheet=低配评估详情；Cell=C4:E4

### 图片证据 P-*

- P-0001：主说明=1A2柜：连接点温升异常；主证据=E-001；关联证据=E-001（1A2柜：连接点温升异常）；原始图片键=ID_SOURCE；文件=Work/runs/report-test/assets/file-s44/P-0001.jpeg

### 本地参考 R-*

本报告未引用此类来源。

### 网络来源 W-*

本报告未引用此类来源。
"""


def test_source_index_docx_is_deterministic_openable_and_complete(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.docx"
    second = tmp_path / "second.docx"

    first_result = SourceIndexDocxRenderer.render(SOURCE_INDEX, first)
    second_result = SourceIndexDocxRenderer.render(SOURCE_INDEX, second)

    assert first.read_bytes() == second.read_bytes()
    assert first_result.output_sha256 == second_result.output_sha256
    document = Document(first)
    visible = [paragraph.text for paragraph in document.paragraphs]
    assert "证据与来源索引" in visible
    assert "脚注对应关系" in visible
    assert "[1] C-2.1-001：E-001" in visible
    assert any(text.startswith("E-001：配电柜检查记录") for text in visible)
    assert "图片证据 P-*" in visible
    assert any(
        text.startswith("P-0001：主说明=1A2柜：连接点温升异常")
        and "主证据=E-001" in text
        for text in visible
    )
    section = document.sections[0]
    assert section.orientation == WD_ORIENT.PORTRAIT
    assert section.page_width == Inches(8.5)
    assert section.left_margin == Inches(1)


def test_source_index_docx_rejects_blank_markdown(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot be blank"):
        SourceIndexDocxRenderer.render("\n", tmp_path / "blank.docx")
