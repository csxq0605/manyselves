"""Standard names behind the knowledge units a judgment row cites.

A judgment row records the knowledge units it applied as ``rule_refs``
(``KU-…`` ids).  The writer may not print ids, so without this the standard
behind a judgment was lost and the prose fell back to reciting the method.
The program reads every knowledge unit's title and its standards field from
the run's frozen knowledge sources and gives each section the standards its
rows cite.  The knowledge base itself marks these standards as candidates
whose clauses and currency are not verified; the writer is told so.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..domain.judgment_ledger import JudgmentRow

_UNIT = re.compile(r"(?m)^#{2,4}\s*(KU-[\w.\-]+)\s*(.*)$")
STANDARD_FIELDS = ("相关标准", "参考标准", "依据标准", "标准依据", "引用标准")
_FIELD = re.compile(
    r"(?m)^\s*(?:[-*]\s*)?(?:\*\*)?(" + "|".join(STANDARD_FIELDS) + r")(?:\*\*)?\s*[：:]\s*(.+?)\s*$"
)
_EMPTY = {"", "无", "暂无", "—", "-", "/"}
_UNIT_ID = re.compile(r"KU-\d[\d.]*-\d+")


def unit_ids(ref: str) -> list[str]:
    """The knowledge unit ids inside one rule reference, e.g. "R-001（KU-2.1-01）"."""

    return list(dict.fromkeys(_UNIT_ID.findall(ref)))


def knowledge_units(texts: Iterable[str]) -> dict[str, dict[str, str]]:
    """KU id -> its title and standards, from knowledge texts; the first definition wins."""

    units: dict[str, dict[str, str]] = {}
    for text in texts:
        heads = list(_UNIT.finditer(text))
        for index, head in enumerate(heads):
            end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
            body = text[head.end():end]
            # The unit ends at the next heading of any level.
            body = re.split(r"(?m)^#{1,4}\s", body, maxsplit=1)[0]
            standards = [
                match.group(2).strip()
                for match in _FIELD.finditer(body)
                if match.group(2).strip() not in _EMPTY
            ]
            units.setdefault(
                head.group(1),
                {"title": head.group(2).strip(), "standards": "；".join(dict.fromkeys(standards))},
            )
    return units


def section_standards(
    rows: Sequence[JudgmentRow], units: Mapping[str, Mapping[str, str]]
) -> list[dict[str, Any]]:
    """The standards this section's rows cite, each knowledge unit once, without its id."""

    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        for unit_id in (u for ref in row.rule_refs for u in unit_ids(ref)):
            unit = units.get(unit_id)
            if unit is None or not unit.get("standards"):
                continue
            entry = found.setdefault(
                unit_id,
                {"rule": unit.get("title", ""), "standards": unit["standards"], "for_rows": []},
            )
            entry["for_rows"].append(row.id)
    return list(found.values())


def standards_coverage(
    ledger: Mapping[str, Sequence[JudgmentRow]], units: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    """How many cited knowledge units resolved to a standard, for checking a run."""

    rows = [row for rows in ledger.values() for row in rows]
    cited = {u for row in rows for ref in row.rule_refs for u in unit_ids(ref)}
    resolved = sorted(u for u in cited if units.get(u, {}).get("standards"))
    without = [
        row.id for row in rows
        if not any(units.get(u, {}).get("standards") for ref in row.rule_refs for u in unit_ids(ref))
    ]
    return {
        "units_in_knowledge": len(units),
        "units_cited": len(cited),
        "units_with_standards": len(resolved),
        "unresolved": sorted(cited - set(resolved)),
        "rows": len(rows),
        "rows_without_standard": len(without),
    }


__all__ = ["STANDARD_FIELDS", "knowledge_units", "section_standards", "standards_coverage", "unit_ids"]
