"""Cross-module views of the final judgment ledger, built by the program.

The joint dossiers group one object's judgments from every module.  From the
final ledger this module derives three views, without any model:

- ``object_chains``: objects that several modules judged and at least one
  flagged; the chief joins these into cross-module conclusions;
- ``see_also_by_row``: for every row on such an object, the other modules'
  rows on it, so a section can cite them ("见 2.x.x");
- ``conflict_summary``: what is still in conflict after revision (one fact
  judged differently by two modules);
- ``flagged_rows``: every flagged row (concern or undetermined) in one line,
  so an author can see what other modules found beyond its own objects;
- ``checked_relations`` and ``related_rows``: the cross-module references a
  row records (``related_row_ids``), kept only when they name an existing row
  of another module, and their content for the writer.

The program only offers candidates it can tell from the fixed structure (the
same fact or object) and checks that a cited row exists.  Whether two rows
bear on each other is the author's judgment, made with the knowledge base's
own method for cross-unit relations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..domain.judgment_ledger import JudgmentRow

ATTENTION = {"concern", "undetermined"}


def _row_index(ledger: Mapping[str, Sequence[JudgmentRow]]) -> dict[str, tuple[str, JudgmentRow]]:
    return {row.id: (module_id, row) for module_id, rows in ledger.items() for row in rows}


def _row_ids(dossier: Mapping[str, Any]) -> list[str]:
    return list(dict.fromkeys(j["row_id"] for j in dossier.get("judgments", []) if j.get("row_id")))


def _multi_module(dossier: Mapping[str, Any]) -> bool:
    return len({j["module_id"] for j in dossier.get("judgments", [])}) >= 2


def object_chains(
    dossiers: Sequence[Mapping[str, Any]], ledger: Mapping[str, Sequence[JudgmentRow]]
) -> list[dict[str, Any]]:
    """Objects judged by two or more modules with at least one flagged row."""

    index = _row_index(ledger)
    chains = []
    for dossier in dossiers:
        if not _multi_module(dossier):
            continue
        rows = [index[row_id] for row_id in _row_ids(dossier) if row_id in index]
        if not any(row.disposition in ATTENTION for _module, row in rows):
            continue
        chains.append(
            {
                "dossier_id": dossier["dossier_id"],
                "subject": dossier["subject"],
                "in_conflict": dossier.get("kind") == "conflict",
                "rows": [
                    {
                        "row_id": row.id,
                        "module_id": module_id,
                        "submodule_id": row.submodule_id,
                        "checkpoint": row.checkpoint,
                        "disposition": row.disposition,
                        "evidence_status": row.evidence_status,
                        **({"risk_level": row.risk_level} if row.risk_level else {}),
                    }
                    for module_id, row in rows
                ],
            }
        )
    return chains


def see_also_by_row(
    dossiers: Sequence[Mapping[str, Any]], ledger: Mapping[str, Sequence[JudgmentRow]]
) -> dict[str, list[dict[str, Any]]]:
    """Row id -> the other modules' rows on the same object."""

    index = _row_index(ledger)
    found: dict[str, dict[str, dict[str, Any]]] = {}
    for dossier in dossiers:
        if not _multi_module(dossier):
            continue
        rows = [index[row_id] for row_id in _row_ids(dossier) if row_id in index]
        for module_id, row in rows:
            others = found.setdefault(row.id, {})
            for other_module, other in rows:
                if other_module == module_id or other.id in others:
                    continue
                others[other.id] = {
                    "row_id": other.id,
                    "submodule_id": other.submodule_id,
                    "object": other.object,
                    "checkpoint": other.checkpoint,
                    "disposition": other.disposition,
                    "evidence_status": other.evidence_status,
                    "basis": other.basis,
                }
    return {row_id: list(others.values()) for row_id, others in found.items()}


def section_see_also(
    rows: Sequence[JudgmentRow], see_also: Mapping[str, Sequence[Mapping[str, Any]]]
) -> list[dict[str, Any]]:
    """The other modules' rows on this section's objects, each once."""

    entries: dict[str, dict[str, Any]] = {}
    for row in rows:
        for other in see_also.get(row.id, ()):
            entry = entries.setdefault(other["row_id"], {**other, "for_rows": []})
            entry["for_rows"].append(row.id)
    return list(entries.values())


_DISPOSITION_WORDS = {"concern": "关注", "undetermined": "不能判定"}


def flagged_rows(ledger: Mapping[str, Sequence[JudgmentRow]]) -> list[dict[str, str]]:
    """Every flagged row in one line: what it found and on which object."""

    found = []
    for module_id, rows in ledger.items():
        for row in rows:
            if row.disposition not in ATTENTION:
                continue
            finding = row.hazard if row.disposition == "concern" else (row.gap.missing if row.gap else "")
            level = f"，{row.risk_level}" if row.risk_level else ""
            found.append(
                {
                    "row_id": row.id,
                    "module_id": module_id,
                    "line": (
                        f"{row.submodule_id}｜{row.object}｜{row.checkpoint}"
                        f"（{_DISPOSITION_WORDS[row.disposition]}{level}）"
                        + (f"：{finding}" if finding else "")
                    ),
                }
            )
    return found


def checked_relations(
    ledger: Mapping[str, Sequence[JudgmentRow]],
) -> tuple[dict[str, list[JudgmentRow]], dict[str, list[str]]]:
    """Keep only related rows that exist and belong to another module.

    Returns the ledger with the other references removed, and what was removed.
    """

    owner = {row.id: module_id for module_id, rows in ledger.items() for row in rows}
    kept: dict[str, list[JudgmentRow]] = {}
    dropped: dict[str, list[str]] = {}
    for module_id, rows in ledger.items():
        kept[module_id] = []
        for row in rows:
            good = [r for r in row.related_row_ids if owner.get(r) not in (None, module_id)]
            if bad := [r for r in row.related_row_ids if r not in good]:
                dropped[row.id] = bad
                row = row.model_copy(update={"related_row_ids": good})
            kept[module_id].append(row)
    return kept, dropped


def related_rows(
    rows: Sequence[JudgmentRow], ledger: Mapping[str, Sequence[JudgmentRow]]
) -> list[dict[str, Any]]:
    """The other modules' rows that this section's rows say bear on their conclusions."""

    index = _row_index(ledger)
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        for row_id in row.related_row_ids:
            if row_id not in index:
                continue
            _module, other = index[row_id]
            entry = found.setdefault(
                row_id,
                {
                    "row_id": other.id,
                    "submodule_id": other.submodule_id,
                    "object": other.object,
                    "checkpoint": other.checkpoint,
                    "disposition": other.disposition,
                    "evidence_status": other.evidence_status,
                    **({"risk_level": other.risk_level} if other.risk_level else {}),
                    "basis": other.basis,
                    "for_rows": [],
                },
            )
            entry["for_rows"].append(row.id)
    return list(found.values())


def conflict_summary(dossier: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "dossier_id": dossier["dossier_id"],
        "subject": dossier["subject"],
        "reasons": list(dossier.get("reasons", [])),
        "conflicting_facts": list(dossier.get("conflicting_facts", [])),
        **(
            {"differing_quantities": dossier["differing_quantities"]}
            if dossier.get("differing_quantities")
            else {}
        ),
    }


__all__ = [
    "checked_relations",
    "flagged_rows",
    "conflict_summary",
    "object_chains",
    "related_rows",
    "section_see_also",
    "see_also_by_row",
]
