"""Shape of the 3.2 improvement-action table, following the expert report."""

from __future__ import annotations

import re

ACTION_TABLE_HEADERS = ("评估内容", "发现问题", "风险与危害", "解决方案", "备注")
ACTION_PRIORITY_GROUPS = ("重要，紧急", "重要，不紧急", "一般")
# Cells that must say something: the object and the action on it.
_REQUIRED_COLUMNS = (1, 3)

_GROUP_LABEL = re.compile(r"^\*\*(?P<label>[^*]+)\*\*\s*$")


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _group_of(label: str) -> str | None:
    head = re.split(r"\s*[·•｜|/]\s*", label.strip(), maxsplit=1)[0]
    return head if head in ACTION_PRIORITY_GROUPS else None


def action_table_problems(markdown: str) -> list[str]:
    """Return the ways a 3.2 body departs from the grouped five-column table.

    The body is a lead sentence followed by priority groups.  Each group is a
    bold label line (``**重要，紧急**``, optionally followed by a region such as
    ``**重要，紧急 · 芜湖区域**``) and directly below it one five-column table.
    """

    lines = markdown.splitlines()
    problems: list[str] = []
    tables = 0
    index = 0
    last_label: str | None = None
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        label = _GROUP_LABEL.match(line)
        if label:
            last_label = label.group("label")
            index += 1
            continue
        if not line.startswith("|"):
            last_label = None
            index += 1
            continue
        block: list[list[str]] = []
        while index < len(lines) and lines[index].strip().startswith("|"):
            block.append(_cells(lines[index]))
            index += 1
        tables += 1
        if last_label is None or _group_of(last_label) is None:
            problems.append(
                f"table {tables} is not directly under a priority label "
                f"(**{'** / **'.join(ACTION_PRIORITY_GROUPS)}**)"
            )
        last_label = None
        header = tuple(block[0])
        if header != ACTION_TABLE_HEADERS:
            problems.append(
                f"table {tables} header is {' | '.join(header)}; "
                f"expected {' | '.join(ACTION_TABLE_HEADERS)}"
            )
            continue
        rows = [row for row in block[1:] if not _is_separator(row)]
        if not rows:
            problems.append(f"table {tables} has no action rows")
        for number, row in enumerate(rows, 1):
            if len(row) != len(ACTION_TABLE_HEADERS):
                problems.append(f"table {tables} row {number} has {len(row)} cells, expected 5")
                continue
            for column in _REQUIRED_COLUMNS:
                if not row[column]:
                    problems.append(
                        f"table {tables} row {number} leaves {ACTION_TABLE_HEADERS[column]} empty"
                    )
    if tables == 0:
        problems.append("no action table found")
    return problems
