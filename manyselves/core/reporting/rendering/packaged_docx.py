"""Repository-packaged approved-Markdown to DOCX core."""

import io
import re
from pathlib import Path
from typing import Any

from docx import Document

DEFAULT_FILENAME = "配电安全专家咨询报告.docx"
_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    cells = _cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _append_markdown(document: Document, report_text: str) -> None:
    style_names = {style.name for style in document.styles}
    lines = report_text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            document.add_heading(heading.group(2).strip(), level=min(len(heading.group(1)), 4))
            index += 1
            continue
        if line.startswith("|") and index + 1 < len(lines) and _is_separator(lines[index + 1]):
            headers = _cells(line)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                row = _cells(lines[index])
                if len(row) == len(headers):
                    rows.append(row)
                index += 1
            table = document.add_table(rows=1, cols=len(headers))
            table.style = "Table Grid"
            for column, value in enumerate(headers):
                table.cell(0, column).text = value
            for row in rows:
                cells = table.add_row().cells
                for column, value in enumerate(row):
                    cells[column].text = value
            continue
        if line.startswith("- "):
            text = line[2:].strip()
            if "List Bullet" in style_names:
                document.add_paragraph(text, style="List Bullet")
            else:
                document.add_paragraph(f"• {text}")
        elif re.match(r"^\d+\.\s+", line):
            text = re.sub(r"^\d+\.\s+", "", line)
            if "List Number" in style_names:
                document.add_paragraph(text, style="List Number")
            else:
                document.add_paragraph(line)
        else:
            document.add_paragraph(line)
        index += 1


class PackagedDocxCore:
    """Render approved report prose without external runtime files."""

    def __init__(self, template_path: Path):
        self.template_path = Path(template_path)

    def render_approved_prose(
        self,
        report_text: str,
        *,
        filename: str | None = None,
        report_model: Any = None,
    ) -> tuple[str, bytes]:
        if report_model is not None:
            raise ValueError(
                "structured-model prose generation is disabled; pass approved report prose only"
            )
        if not self.template_path.is_file():
            raise FileNotFoundError(f"packaged DOCX template not found: {self.template_path}")
        document = Document(self.template_path)
        _append_markdown(document, report_text)
        buffer = io.BytesIO()
        document.save(buffer)
        return filename or DEFAULT_FILENAME, buffer.getvalue()
