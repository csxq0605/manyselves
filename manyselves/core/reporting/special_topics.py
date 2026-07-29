"""Deterministic intake for the user-authored dynamic Chapter 4 plan."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .models import SpecialTopicPlan, SpecialTopicSectionRequirement

SPECIAL_TOPIC_FILENAME_MARKER = "专项问题分析"
MAX_SPECIAL_TOPIC_SOURCE_CHARS = 100_000


def _clean_title(value: str) -> tuple[str | None, str]:
    value = value.strip().strip("*_` ")
    match = re.match(r"^(4\.[1-9][0-9]*)[.、]?\s+(.+?)\s*$", value)
    if match is None:
        return None, value
    return match.group(1), match.group(2).strip()


def load_special_topic_plan(workspace: Path) -> SpecialTopicPlan | None:
    """Read the optional ``Inputs/*专项问题分析*.md`` file into a typed plan."""

    workspace = Path(workspace).resolve()
    inputs = workspace / "Inputs"
    candidates = sorted(
        path
        for path in inputs.rglob("*.md")
        if path.is_file() and SPECIAL_TOPIC_FILENAME_MARKER in path.stem
    ) if inputs.is_dir() else []
    if not candidates:
        return None
    if len(candidates) != 1:
        relative = [
            path.relative_to(workspace).as_posix()
            for path in candidates
        ]
        raise ValueError(
            "Inputs must contain exactly one standalone Markdown file whose filename "
            f"contains '{SPECIAL_TOPIC_FILENAME_MARKER}'; found={relative}"
        )

    source = candidates[0]
    raw = source.read_text(encoding="utf-8")
    if not raw.lstrip("\ufeff").strip():
        return None
    if len(raw) > MAX_SPECIAL_TOPIC_SOURCE_CHARS:
        raise ValueError(
            f"special-topic Markdown exceeds {MAX_SPECIAL_TOPIC_SOURCE_CHARS} characters"
        )
    headings = [
        (index, len(match.group(1)), match.group(2).strip())
        for index, line in enumerate(raw.splitlines())
        if (match := re.match(r"^(#{1,6})\s+(.+?)\s*$", line))
    ]
    if not headings:
        raise ValueError("special-topic Markdown must contain subsection headings")

    root = next(
        (
            item
            for item in headings
            if re.sub(r"^\d+(?:\.\d+)*\.?\s*", "", item[2]).strip()
            == SPECIAL_TOPIC_FILENAME_MARKER
        ),
        None,
    )
    if root is not None:
        root_line, root_level, _ = root
        section_level = root_level + 1
        section_headings = [
            item
            for item in headings
            if item[0] > root_line and item[1] == section_level
        ]
    else:
        section_level = min(level for _line, level, _title in headings)
        section_headings = [
            item for item in headings if item[1] == section_level
        ]
    if not section_headings:
        raise ValueError(
            "special-topic Markdown must place at least one subsection directly beneath "
            "its top-level title"
        )

    lines = raw.splitlines()
    sections: list[SpecialTopicSectionRequirement] = []
    for index, (line_number, _level, raw_title) in enumerate(section_headings, start=1):
        declared_id, title = _clean_title(raw_title)
        expected_id = f"4.{index}"
        if declared_id is not None and declared_id != expected_id:
            raise ValueError(
                "special-topic subsection numbering must be sequential in source order: "
                f"expected {expected_id}, found {declared_id}"
            )
        next_line = (
            section_headings[index][0]
            if index < len(section_headings)
            else len(lines)
        )
        requirement = "\n".join(lines[line_number + 1 : next_line]).strip()
        if not title:
            raise ValueError(f"special-topic subsection {expected_id} has no title")
        if not requirement:
            raise ValueError(
                f"special-topic subsection {expected_id} must include a brief requirement"
            )
        sections.append(
            SpecialTopicSectionRequirement(
                section_id=expected_id,
                title=title,
                requirement=requirement,
            )
        )

    relative = source.relative_to(workspace)
    return SpecialTopicPlan(
        source_ref=relative,
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        sections=sections,
    )
