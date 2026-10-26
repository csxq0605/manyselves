"""Plan a revision of an existing report from its base version.

A revision runs under the run's own id as its next version.  What it redoes
is decided here, from the run's fixed structure only:

- facts: the current facts are compared with the base version's by id and
  text; a changed fact has a new id, so its old id counts as removed;
- modules: a module is reopened when it owns a changed or new fact, when one
  of its base rows cites a removed fact, or when a change asks for its
  judgment (``scope=judgment``);
- rows: in a reopened module the base rows that cite no changed fact and lie
  outside the sections whose judgment was asked for are kept as they were;
- wording: a ``scope=text`` change reaches only the writing of its section;
- chief: chapters 1 and 3 are revised from the base version's, only where
  rows changed or a change names them.

Everything else is left to the run's turn memo: a step whose input is the
same as in the base version is replayed, not redone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .models.reporting import EvidenceItem, ReportRevision

WHOLE_REPORT = "全文"
CHIEF_TARGETS = {"1", "3"}

MODULE_NOTE = (
    "这是修改已有报告，不是重写。previous_rows 是上一版本模块的判断行。"
    "keep_row_ids 里的行程序会按上一版原样保留，不用再提交；"
    "其余上一版的行要重新判断并提交：它们引用的事实有变化（removed_evidence_ids 是已不存在或已改动的旧事实编号，"
    "changed_facts 是新的或改动后的事实），或者修改要求点名要重新判断它所在的小节。"
    "新的事实要进入判断。行编号沿用上一版的，新行用新的编号，不与 keep_row_ids 重复。"
)
CHIEF_NOTE = (
    "这是修改已有报告，不是重写。previous_chapter3 和 previous_chapter1 是上一版本的第三章和第一章。"
    "只改动引用了 changed_row_ids 里任一行的条目，以及 instructions 点名的内容；其他条目照上一版原样抄写，"
    "不改措辞。综述类的段落只在它说到的内容变了时才改。"
)
SECTION_NOTE = "这是修改已有报告：按 revision_instruction 修改这一节，其余照判断行写。"


def base_dir(workspace: Path, run_id: str, version: int) -> Path:
    return Path(workspace) / "Work" / "runs" / run_id / "versions" / f"v{version}"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _module_of(section: str, sections: Mapping[str, Sequence[str]]) -> str | None:
    for module_id, ids in sections.items():
        if section == module_id or section in ids:
            return module_id
    return None


def plan_revision(
    revision: ReportRevision,
    *,
    base: Path,
    items: Sequence[EvidenceItem],
    sections: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Decide what a revision redoes; see the module docstring."""

    base_reference = _read_json(base / "ledger" / "reference-values.json", {})
    base_texts: dict[str, str] = base_reference.get("fact_texts") or {}
    current = {item.id: item for item in items}
    changed = sorted(
        evidence_id
        for evidence_id, item in current.items()
        if base_texts.get(evidence_id) != item.fact
    )
    removed = sorted(set(base_texts) - set(current))
    touched = set(changed) | set(removed)
    # The base version's rows as it delivered them, after review; versions
    # saved before the final ledger was kept only have the first draft.
    base_rows: dict[str, list[dict[str, Any]]] = _read_json(
        base / "ledger" / "judgments.json", None
    ) or _read_json(base / "ledger" / "judgments-r0.json", {})

    judgment_sections: dict[str, list[str]] = {}
    text_instructions: dict[str, list[str]] = {}
    chief_instructions: list[str] = []
    module_instructions: dict[str, list[dict[str, str]]] = {}
    for change in revision.changes:
        target = change.target.strip()
        if target == WHOLE_REPORT:
            targets = [s for ids in sections.values() for s in ids]
            chief_instructions.append(change.instruction)
        elif target in CHIEF_TARGETS:
            chief_instructions.append(f"第{target}章：{change.instruction}")
            continue
        elif target in sections:
            targets = list(sections[target])
        elif _module_of(target, sections) is not None:
            targets = [target]
        else:
            known = "、".join([*sections, *(s for ids in sections.values() for s in ids)])
            raise ValueError(f"修改目标 {target} 不在本报告的小节里；可用：1、3、{WHOLE_REPORT}、{known}")
        for section in targets:
            module_id = _module_of(section, sections)
            if module_id is None:
                continue
            if change.scope == "judgment":
                judgment_sections.setdefault(module_id, []).append(section)
                module_instructions.setdefault(module_id, []).append(
                    {"section": section, "instruction": change.instruction}
                )
            else:
                text_instructions.setdefault(section, []).append(change.instruction)

    affected: list[str] = []
    for module_id, ids in sections.items():
        owns_change = any(
            current[e].module_id == module_id or current[e].module_id is None
            for e in changed
        )
        cites_touched = any(
            touched & set(row.get("evidence_ids") or ()) for row in base_rows.get(module_id, [])
        )
        if owns_change or cites_touched or module_id in judgment_sections:
            affected.append(module_id)
        del ids

    keep_rows: dict[str, list[dict[str, Any]]] = {}
    for module_id in affected:
        reopened = set(judgment_sections.get(module_id, ()))
        keep_rows[module_id] = [
            row
            for row in base_rows.get(module_id, [])
            if not touched & set(row.get("evidence_ids") or ())
            and row.get("submodule_id") not in reopened
        ]
    return {
        "base_version": revision.base_version,
        "instruction": revision.instruction,
        "changed_evidence_ids": changed,
        "removed_evidence_ids": removed,
        "affected_modules": affected,
        "module_instructions": module_instructions,
        "base_rows": {m: base_rows.get(m, []) for m in affected},
        "keep_rows": keep_rows,
        "text_instructions": text_instructions,
        "chief_instructions": chief_instructions,
    }


def module_revision_brief(
    plan: Mapping[str, Any], module_id: str, changed_facts: str
) -> dict[str, Any] | None:
    """The part of one module's brief that tells its author what to redo."""

    if module_id not in plan.get("affected_modules", ()):
        return None
    keep = plan["keep_rows"].get(module_id, [])
    return {
        "note": MODULE_NOTE,
        "instruction": plan["instruction"],
        "section_instructions": plan["module_instructions"].get(module_id, []),
        "previous_rows": plan["base_rows"].get(module_id, []),
        "keep_row_ids": [row["id"] for row in keep],
        "removed_evidence_ids": [
            e
            for e in plan["removed_evidence_ids"]
            if any(
                e in (row.get("evidence_ids") or ())
                for row in plan["base_rows"].get(module_id, [])
            )
        ],
        "changed_facts": changed_facts,
    }


def splice_kept_rows(
    module_id: str, rows: Sequence[Mapping[str, Any]], plan: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """The kept base rows and the author's rows, in the base version's order.

    A resubmitted row takes its base row's place and new rows follow, so an
    unchanged ledger reads exactly as before and later steps can be reused.
    """

    if not plan or module_id not in plan.get("keep_rows", {}):
        return [dict(row) for row in rows]
    kept = {row["id"]: dict(row) for row in plan["keep_rows"][module_id]}
    submitted = {row.get("id"): dict(row) for row in rows if row.get("id") not in kept}
    ordered: list[dict[str, Any]] = []
    for base_row in plan.get("base_rows", {}).get(module_id, []):
        row_id = base_row.get("id")
        if row_id in kept:
            ordered.append(kept.pop(row_id))
        elif row_id in submitted:
            ordered.append(submitted.pop(row_id))
    return [*ordered, *kept.values(), *submitted.values()]


def base_chief_outputs(base: Path) -> dict[str, Any]:
    """Chapters 1 and 3 as the base version delivered them.

    Versions saved by this code keep them in ``ledger/chief-outputs.json``;
    older versions only have the chief's last accepted turns.
    """

    saved = _read_json(base / "ledger" / "chief-outputs.json", None)
    if isinstance(saved, dict):
        return {"chapter1": saved.get("chapter1"), "chapter3": saved.get("chapter3")}
    found: dict[str, Any] = {}
    for chapter, tasks in (
        ("chapter3", ("fix-3--revise-chief-actions", "revise-3--revise-chief-actions",
                      "chapter-3--chief-chapter-3")),
        ("chapter1", ("fix-1--revise-chief-overview", "revise-1--revise-chief-overview",
                      "chapter-1--chief-chapter-1")),
    ):
        for task in tasks:
            matches = sorted((base / "agent-results").glob(f"*/{task}.json"))
            record = _read_json(matches[0], {}) if matches else {}
            if record.get("status") == "completed":
                found[chapter] = record.get("result")
                break
    return found


def chief_revision(
    plan: Mapping[str, Any],
    *,
    base: Path,
    final_ledger: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any] | None:
    """What the chief redoes, or None when chapters 1 and 3 can stay as they were."""

    base_final = _read_json(base / "ledger" / "judgments.json", {})
    before = {row["id"]: row for rows in base_final.values() for row in rows}
    after = {row["id"]: row for rows in final_ledger.values() for row in rows}
    changed_rows = sorted(
        row_id for row_id in set(before) | set(after) if before.get(row_id) != after.get(row_id)
    )
    if not changed_rows and not plan.get("chief_instructions"):
        return None
    previous = base_chief_outputs(base)
    return {
        "note": CHIEF_NOTE,
        "instruction": plan["instruction"],
        "instructions": list(plan.get("chief_instructions") or ()),
        "changed_row_ids": changed_rows,
        "removed_row_ids": sorted(set(before) - set(after)),
        "previous_chapter3": previous.get("chapter3"),
        "previous_chapter1": previous.get("chapter1"),
    }


__all__ = [
    "CHIEF_NOTE",
    "MODULE_NOTE",
    "SECTION_NOTE",
    "WHOLE_REPORT",
    "base_chief_outputs",
    "base_dir",
    "chief_revision",
    "module_revision_brief",
    "plan_revision",
    "splice_kept_rows",
]
