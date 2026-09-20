"""Single post-render validator for approved Markdown DOCX artifacts."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator

from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

from .markdown_content import markdown_content_expectations, semantic_content_text

LOGGER = logging.getLogger(__name__)
_UNRESOLVED_TOKEN = re.compile(r"\[\[(?:CLAIM:C-[^\]\s]+|CITE:\d+|PHOTO:[^\]]+)\]\]")


def validate_rendered_markdown_docx(
    output_path: Path,
    approved_markdown: str,
    *,
    expected_title: str | None = None,
    reject_unresolved_tokens: bool = False,
) -> list[str]:
    """Return post-render content warnings without blocking DOCX publication.

    Presentation-only syntax is normalized by the same Markdown model consumed
    by the renderer.  Matching advances through the DOCX corpus so reordered
    prose and collapsed duplicates are still observable.  A file that cannot be
    opened as a DOCX remains a hard error because there is no usable artifact to
    publish; content, title, and unresolved-token findings are warnings.
    """

    try:
        rendered = Document(output_path)
    except Exception as exc:
        raise ValueError("rendered file is not Word/WPS-openable") from exc

    visible_parts = list(_visible_parts(rendered))
    visible_text = "\n".join(visible_parts)
    semantic_visible = semantic_content_text(visible_text)
    expectations = markdown_content_expectations(approved_markdown)

    missing = []
    cursor = 0
    for expectation in expectations:
        position = semantic_visible.find(expectation.semantic_text, cursor)
        if position < 0:
            missing.append(expectation)
            continue
        cursor = position + len(expectation.semantic_text)

    validation_warnings: list[str] = []
    if missing:
        preview = ", ".join(
            f"line {item.line_number}: {item.text[:80]!r}" for item in missing[:8]
        )
        validation_warnings.append(
            f"rendered DOCX may have omitted approved Markdown content: {preview}"
        )
    if expected_title is not None and expected_title not in visible_text:
        validation_warnings.append("rendered DOCX is missing the approved title")
    unresolved = sorted(set(_UNRESOLVED_TOKEN.findall(visible_text)))
    if reject_unresolved_tokens and unresolved:
        preview = ", ".join(repr(item[:80]) for item in unresolved[:8])
        validation_warnings.append(
            f"rendered DOCX contains unresolved content tokens: {preview}"
        )

    for warning in validation_warnings:
        LOGGER.warning("DOCX post-render validation warning path=%s: %s", output_path, warning)
    return validation_warnings


def _visible_parts(document: DocumentObject) -> Iterator[str]:
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            paragraph = Paragraph(child, document)
            text = "".join(
                run.text
                for run in paragraph.runs
                if run.font.superscript is not True
            )
            if text.strip():
                yield text
        elif isinstance(child, CT_Tbl):
            table = Table(child, document)
            for row in table.rows:
                for cell in row.cells:
                    if cell.text.strip():
                        yield cell.text
