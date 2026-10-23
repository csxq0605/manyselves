"""Review packets: each judgment row with exactly what it cites.

A reviewer checks whether a row follows from its facts and rules.  Instead of
the whole project context and the whole ledger, the program puts next to the
module's rows the facts, calculations, knowledge units and photo readings
they cite, the other modules' rows they are related to (with those rows'
facts), the module author's judging method and the program's own checks.
Everything is chosen by the row's fields, never by the content of a run.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..domain.judgment_ledger import JudgmentRow

from .observation_ledger import shared_labels
from .photo_reading import photo_reading_labels
from .research.fact_context import render_fact_packet
from .research.project_evidence import ProjectEvidenceIndex

_UNIT = re.compile(r"KU-\d[\d.]*-\d+")
# The input table records each checkpoint's state as OK or NG.
_STATE = re.compile(r"=\s*(OK|NG)(?![A-Za-z])")


def _cited_units(rows: Sequence[JudgmentRow]) -> list[str]:
    return list(dict.fromkeys(u for row in rows for ref in row.rule_refs for u in _UNIT.findall(ref)))


def program_checks(
    rows: Sequence[JudgmentRow],
    *,
    facts: Mapping[str, str],
    owner_rows: Mapping[str, Sequence[JudgmentRow]],
) -> list[dict[str, str]]:
    """Rows the program picked out for the reviewer to look at, with why.

    They are not findings: whether each one is wrong is the reviewer's call.
    - a row whose facts another section presents judges them differently
      from the rows of that section;
    - the recorded state is NG but the row finds no concern, or OK but the
      row finds one;
    - the material was not provided but the row finds no concern.
    """

    checks: list[dict[str, str]] = []
    for row in rows:
        for other in owner_rows.get(row.id, ()):
            if other.disposition != row.disposition:
                checks.append({
                    "row_id": row.id,
                    "check": (
                        f"同一份记录由 {other.submodule_id} 负责呈现，那里的 {other.id} 判为 "
                        f"{other.disposition}，本行判为 {row.disposition}；看本行是否应向它看齐，或理由是否写清"
                    ),
                })
        states = {state for e in row.evidence_ids for state in _STATE.findall(facts.get(e, ""))}
        if "NG" in states and row.disposition == "no_concern":
            checks.append({"row_id": row.id, "check": "所引记录有 NG，本行判为没有问题；看理由是否成立"})
        if states == {"OK"} and row.disposition == "concern":
            checks.append({"row_id": row.id, "check": "所引记录都是 OK，本行判为有问题；看依据是否另有事实"})
        if row.evidence_status == "资料未提供" and row.disposition == "no_concern":
            checks.append({
                "row_id": row.id,
                "check": "证据状态是资料未提供，却判为没有问题；缺资料不能作为没有问题的理由",
            })
    return checks


class ReviewPackets:
    """Build one module's review packet from the ledger and the run's references."""

    def __init__(
        self,
        tools: Any,
        run_id: str,
        ledger: Mapping[str, Sequence[JudgmentRow]],
        reference: Mapping[str, Any],
        elsewhere: Mapping[str, Sequence[str]],
    ) -> None:
        self.workspace = Path(tools.workspace)
        self.run_id = run_id
        self.ledger = ledger
        self.reference = reference
        self.elsewhere = elsewhere
        self.index = {
            row.id: (module_id, row) for module_id, rows in ledger.items() for row in rows
        }
        self.items = {item.id: item for item in ProjectEvidenceIndex(self.workspace, run_id).items()}
        self.shared = shared_labels(self.workspace, run_id)
        self.readings = photo_reading_labels(self.workspace, run_id)
        self.headers = {
            (path, sheet): columns
            for path, sheet, columns in reference.get("column_headers") or []
        }
        self.units = tools._unit_bodies(run_id)
        self.conditions = tools._judgment_conditions(run_id)

    def packet(self, module_id: str) -> dict[str, Any]:
        # Only the module's own rows and what they cite: what lies between modules
        # is the object and relation lenses'.
        rows = list(self.ledger.get(module_id, []))
        shown = rows
        evidence_ids = list(dict.fromkeys(e for row in shown for e in row.evidence_ids))
        items = [self.items[e] for e in evidence_ids if e in self.items]
        facts_text = render_fact_packet(
            items,
            module_id,
            shared=self.shared,
            photo_readings=self.readings,
            column_headers=self.headers,
        )
        lines = self.reference.get("calculation_lines") or {}
        calculations = [
            lines[c] for c in dict.fromkeys(c for row in shown for c in row.calculation_ids) if c in lines
        ]
        rules = "\n\n".join(
            f"### {unit} {self.units[unit][0]}\n{self.units[unit][1].strip()}"
            for unit in _cited_units(shown)
            if unit in self.units
        )
        if rules and self.conditions:
            rules += "\n\n## 判断条件（知识库）\n" + self.conditions
        owner_rows = self._owner_rows(rows)
        facts = {item.id: item.fact for item in items}
        return {
            "rows": [row.model_dump(mode="json", exclude_none=True) for row in rows],
            "calculations": calculations,
            "program_checks": program_checks(rows, facts=facts, owner_rows=owner_rows),
            "facts": facts_text,
            "rules": rules or "（本模块的行没有引用知识单元）",
            "method": (self.reference.get("author_skills") or {}).get(module_id, "")
            or "（本模块没有专家版判断方法）",
        }

    def _owner_rows(self, rows: Sequence[JudgmentRow]) -> dict[str, list[JudgmentRow]]:
        """Row id -> the rows of the presenting sections that cite the same facts."""

        found: dict[str, list[JudgmentRow]] = {}
        for row in rows:
            places = set(self.elsewhere.get(row.id, ()))
            if not places:
                continue
            cited = set(row.evidence_ids)
            found[row.id] = [
                other
                for _module_id, other in self.index.values()
                if other.submodule_id in places and cited & set(other.evidence_ids)
            ]
        return found


__all__ = ["ReviewPackets", "program_checks"]
