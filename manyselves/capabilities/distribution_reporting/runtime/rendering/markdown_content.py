"""Shared Markdown structure used by the DOCX renderer and verifier."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

MarkdownBlockKind = Literal[
    "title",
    "heading",
    "paragraph",
    "bullet",
    "numbered_list",
    "table",
]
ContentKind = Literal["title", "heading", "paragraph", "list_item", "table_cell"]

_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_BULLET_PREFIX = re.compile(r"^[-*•➢]\s+")
_PRESENTATION_BULLET_PREFIX = re.compile(r"^[-*•·➢]\s*")
_NUMBERED_LIST_PREFIX = re.compile(r"^\d+\s*[.．、）)]\s+")
_HEADING_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)(?:[.．])?\s+")
_CLAIM_TOKEN = re.compile(r"\[\[CLAIM:C-[^\]\s]+\]\]")
_CITATION_TOKEN = re.compile(r"\[\[CITE:\d+\]\]")
_PHOTO_TOKEN = re.compile(r"\[\[PHOTO:[^\]]+\]\]")


@dataclass(frozen=True, slots=True)
class MarkdownBlock:
    kind: MarkdownBlockKind
    text: str
    line_number: int
    raw_lines: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContentExpectation:
    kind: ContentKind
    text: str
    semantic_text: str
    line_number: int


def strip_inline_markdown(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("**", "").replace("__", "").replace("`", "").strip()


def clean_markdown_heading(value: str) -> str:
    return re.sub(r"^#{1,6}\s*", "", value).strip()


def normalize_heading_title(value: str) -> str:
    text = strip_inline_markdown(clean_markdown_heading(value))
    text = re.sub(r"\s+", "", text)
    return re.sub(r"[。．.：:]+", "", text)


def is_report_title(value: str) -> bool:
    clean = normalize_heading_title(value)
    if clean in {"配电安全专家咨询报告", "配电安全评估报告", "配电系统安全评估报告"}:
        return True
    return bool(re.match(r"^#\s+.+报告\s*$", value.strip()))


def is_table_line(value: str) -> bool:
    return "|" in value and value.count("|") >= 2


def is_bullet_line(value: str) -> bool:
    return bool(_BULLET_PREFIX.match(value))


def is_numbered_list_line(value: str) -> bool:
    return bool(_NUMBERED_LIST_PREFIX.match(strip_inline_markdown(value)))


def clean_bullet_item_text(value: object) -> str:
    text = strip_inline_markdown(value)
    text = _PRESENTATION_BULLET_PREFIX.sub("", text, count=1).strip()
    return _NUMBERED_LIST_PREFIX.sub("", text, count=1).strip()


def clean_numbered_item_text(value: object) -> str:
    return _NUMBERED_LIST_PREFIX.sub("", strip_inline_markdown(value), count=1).strip()


def parse_markdown_blocks(markdown: str) -> list[MarkdownBlock]:
    """Parse the block distinctions that affect deterministic DOCX rendering."""

    blocks: list[MarkdownBlock] = []
    table_lines: list[str] = []
    table_start = 0

    def flush_table() -> None:
        nonlocal table_lines, table_start
        if not table_lines:
            return
        blocks.append(
            MarkdownBlock(
                kind="table",
                text="\n".join(table_lines),
                line_number=table_start,
                raw_lines=tuple(table_lines),
            )
        )
        table_lines = []
        table_start = 0

    for line_number, raw_line in enumerate(markdown.splitlines(), 1):
        line = raw_line.strip()
        if not line or line in {"---", "***", "___"}:
            continue
        if is_table_line(line):
            if not table_lines:
                table_start = line_number
            table_lines.append(line)
            continue

        flush_table()
        display = re.sub(r"^>\s*", "", line).strip()
        if is_report_title(display):
            kind: MarkdownBlockKind = "title"
        elif _HEADING.match(display):
            kind = "heading"
        elif is_bullet_line(display):
            kind = "bullet"
        elif is_numbered_list_line(display):
            kind = "numbered_list"
        else:
            kind = "paragraph"
        blocks.append(MarkdownBlock(kind=kind, text=display, line_number=line_number))

    flush_table()
    return blocks


def semantic_content_text(value: str) -> str:
    """Normalize presentation syntax while preserving approved prose."""

    text = strip_inline_markdown(value)
    text = re.sub(r"^#{1,6}\s*", "", text)
    text = re.sub(r"^>\s*", "", text)
    text = _PRESENTATION_BULLET_PREFIX.sub("", text, count=1)
    text = _NUMBERED_LIST_PREFIX.sub("", text, count=1)
    text = re.sub(r"^(\d+(?:\.\d+)+)\.\s+", r"\1 ", text)
    text = _CLAIM_TOKEN.sub("", text)
    text = _CITATION_TOKEN.sub("", text)
    text = _PHOTO_TOKEN.sub("", text)
    text = re.sub(r"【([^】]+)】[:：]?", r"\1：", text)
    text = re.sub(r"\s*([：:])\s*", r"\1", text)
    return re.sub(r"\s+", "", text).strip()


def markdown_content_expectations(markdown: str) -> list[ContentExpectation]:
    """Project Markdown blocks into ordered, presentation-free content nodes."""

    expectations: list[ContentExpectation] = []
    for block in parse_markdown_blocks(markdown):
        values: list[tuple[ContentKind, str]]
        if block.kind == "table":
            values = [
                ("table_cell", cell)
                for line in block.raw_lines
                if not _is_table_separator(line)
                for cell in _table_cells(line)
                if cell
            ]
        elif block.kind == "title":
            values = [("title", clean_markdown_heading(block.text))]
        elif block.kind == "heading":
            heading = strip_inline_markdown(clean_markdown_heading(block.text))
            numbered = _HEADING_NUMBER.match(heading)
            values = [("heading", numbered.group(1) if numbered else heading)]
        elif block.kind == "bullet":
            values = [("list_item", clean_bullet_item_text(block.text))]
        elif block.kind == "numbered_list":
            values = [("list_item", clean_numbered_item_text(block.text))]
        else:
            values = [("paragraph", block.text)]

        for kind, text in values:
            semantic = semantic_content_text(text)
            if semantic:
                expectations.append(
                    ContentExpectation(
                        kind=kind,
                        text=text,
                        semantic_text=semantic,
                        line_number=block.line_number,
                    )
                )
    return expectations


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_table_separator(line: str) -> bool:
    cells = _table_cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)
