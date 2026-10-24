"""Section lengths of the expert report template, as a writing reference.

The template's heading outline is numbered by position (empty headings are
skipped), and each heading's Chinese characters are counted over its
paragraphs and tables up to the next heading. The numbers are a reference
for how much the expert version writes per section, never an output limit.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from manyselves.runtime.exemplar.outline import document_outline


@dataclass(frozen=True)
class TemplateSection:
    number: str
    title: str
    chars: int
    tables: int


def template_sections(path: Path) -> list[TemplateSection]:
    """Every non-empty heading with the characters and tables directly under it."""

    return [
        TemplateSection(section.number, section.title, section.chars, section.tables)
        for section in document_outline(path).sections
    ]


def _similar(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def _total(sections: list[TemplateSection], number: str) -> TemplateSection:
    members = [s for s in sections if s.number == number or s.number.startswith(number + ".")]
    head = next(s for s in sections if s.number == number)
    return TemplateSection(
        number, head.title, sum(s.chars for s in members), sum(s.tables for s in members)
    )


def matched_lengths(
    sections: list[TemplateSection], wanted: Iterable[tuple[str, str]]
) -> dict[str, TemplateSection]:
    """Map report section ids to template sections by number, checked by title.

    A number whose template title is unlike the wanted title falls back to
    the most similar title; nothing is reported without a reasonable match.
    """

    by_number = {s.number: s for s in sections}
    found: dict[str, TemplateSection] = {}
    for section_id, title in wanted:
        candidate = by_number.get(section_id)
        if candidate is None or _similar(candidate.title, title) < 0.5:
            best = max(sections, key=lambda s: _similar(s.title, title), default=None)
            candidate = best if best is not None and _similar(best.title, title) >= 0.6 else None
        if candidate is not None:
            found[section_id] = _total(sections, candidate.number)
    return found


def render_length_reference(
    lengths: Mapping[str, TemplateSection], titles: Mapping[str, str]
) -> str:
    lines = [
        "# 专家版各节篇幅参考",
        "专家版模板对应小节的中文字数和表格数，只作写作参考，不是上限：同类对象一句概括，"
        "缺资料收进数据缺口表，第一、三章只引用第二章结论。本项目对象更多时可以更长，但不重复论证。",
    ]
    for section_id, title in titles.items():
        if (section := lengths.get(section_id)) is not None:
            lines.append(
                f"{section_id} {title}：约 {section.chars} 字"
                + (f"，{section.tables} 张表" if section.tables else "")
            )
    return "\n".join(lines) + "\n"


__all__ = [
    "TemplateSection",
    "matched_lengths",
    "render_length_reference",
    "template_sections",
]
