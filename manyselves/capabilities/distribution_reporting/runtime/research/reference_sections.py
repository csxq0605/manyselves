"""Exact structural retrieval, never a summary or a claim of rule applicability."""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)(?:\s|[、．])")
_REFERENCE = re.compile(r"(?:第\s*(\d+(?:\.\d+)+)\s*条|(?:见|按|依据|参照)\s*(\d+(?:\.\d+)+))")
_CONTEXT = ("适用", "范围", "定义", "术语", "版本", "废止", "替代", "例外", "前言", "总则")


@dataclass(frozen=True)
class _Section:
    start: int
    body_end: int
    end: int
    title: str
    parent: int | None


def select_reference_sections(text: str, terms: tuple[str, ...]) -> str | None:
    """Return complete matching subtrees, governing context and explicit references.

    Only Markdown headings provide reliable boundaries here. Ancestor introductions
    and document preamble stay exact. The caller budgets the entire bundle; it must
    not trim it. Undetected implicit dependencies still require professional review.
    """
    lines = text.splitlines(keepends=True)
    headings: list[tuple[int, int, str]] = []
    fence: str | None = None
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else (marker if fence is None else fence)
            continue
        match = _HEADING.match(line.rstrip("\r\n")) if fence is None else None
        if match:
            headings.append((index, len(match[1]), match[2]))
    if len(headings) < 2:
        return None
    sections: list[_Section] = []
    stack: list[int] = []
    for index, (start, level, title) in enumerate(headings):
        while stack and headings[stack[-1]][1] >= level:
            stack.pop()
        end = next((pos for pos, depth, _ in headings[index + 1:] if depth <= level), len(lines))
        sections.append(_Section(start, headings[index + 1][0] if index + 1 < len(headings) else len(lines),
                                 end, title, stack[-1] if stack else None))
        stack.append(index)
    lowered = tuple(term.casefold() for term in terms if term)
    selected = {
        index for index, section in enumerate(sections)
        if any(term in "".join(lines[section.start:section.body_end]).casefold() for term in lowered)
    }
    if not selected:
        return None
    selected.update(index for index, section in enumerate(sections) if any(term in section.title for term in _CONTEXT))
    numbered: dict[str, list[int]] = {}
    for index, section in enumerate(sections):
        match = _NUMBER.match(section.title + " ")
        if match:
            numbered.setdefault(match[1], []).append(index)
    retained = set(range(headings[0][0]))
    unresolved: set[str] = set()
    pending = list(selected)
    visited: set[int] = set()
    while pending:
        index = pending.pop()
        if index in visited:
            continue
        visited.add(index)
        section = sections[index]
        added = set(range(section.start, section.end))
        parent = section.parent
        while parent is not None:
            ancestor = sections[parent]
            added.update(range(ancestor.start, ancestor.body_end))
            parent = ancestor.parent
        retained.update(added)
        for line_index in added:
            for match in _REFERENCE.finditer(lines[line_index]):
                number = match[1] or match[2]
                targets = numbered.get(number, [])
                if len(targets) == 1:
                    pending.extend(targets)
                else:
                    unresolved.add(number)
    if len(retained) == len(lines):
        return None  # Whole-source delivery already handles this without duplication.
    ranges: list[tuple[int, int]] = []
    for index in sorted(retained):
        if ranges and ranges[-1][1] == index:
            ranges[-1] = (ranges[-1][0], index + 1)
        else:
            ranges.append((index, index + 1))
    blocks = [
        "自动展开的完整章节及上级导语（不是全文）：保持原文，不证明适用性或版本有效性。"
        "仅按标题、词项和显式条号关联；隐含依赖、其他例外仍须核验。",
    ]
    if unresolved:
        blocks.append("未解析的条款引用：" + "、".join(sorted(unresolved)) + "；本次读不到被引用的条款，不能据此下确定结论。")
    for start, end in ranges:
        blocks.append(f"原文行 {start + 1}–{end}：\n" + "".join(lines[start:end]))
    return "\n\n".join(blocks)
