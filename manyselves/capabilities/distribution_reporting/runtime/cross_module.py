"""Cross-module views of the final judgment ledger, built by the program.

The joint dossiers group one object's judgments from every module.  From the
final ledger this module derives three views, without any model:

- ``object_chains``: objects that several modules judged and at least one
  flagged; the chief joins these into cross-module conclusions;
- ``see_also_by_row``: for every row on such an object, the other modules'
  rows on it, so a section can cite them ("见 2.x.x");
- ``conflict_summary``: what is still in conflict after revision (one fact
  judged differently by two modules);
- ``structural_links``: rows of different modules joined by the fixed
  structure rather than one shared fact: a room and the equipment recorded in
  it (subjects are "room/equipment"), and subsections paired by mechanism in
  ``domain/mechanism-pairs.yaml`` within one room or across the whole site;
- ``checked_relations`` and ``related_rows``: the cross-module references a
  row records (``related_row_ids``), kept only when they name another
  module's row on the same object or a structural link, and their content
  for the writer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from ..domain.judgment_ledger import JudgmentRow
from .joint_dossiers import object_key

ATTENTION = {"concern", "undetermined"}
PAIRS_FILE = Path(__file__).resolve().parents[1] / "domain/mechanism-pairs.yaml"
SITE = "*"  # a row about the whole site, or whose facts name no room


@cache
def mechanism_pairs() -> tuple[tuple[str, str], ...]:
    data = yaml.safe_load(PAIRS_FILE.read_text(encoding="utf-8")) or {}
    return tuple((str(a), str(b)) for a, b in (pair["between"] for pair in data.get("pairs", [])))


def _within(submodule_id: str, section_id: str) -> bool:
    return submodule_id == section_id or submodule_id.startswith(section_id + ".")


def row_places(
    ledger: Mapping[str, Sequence[JudgmentRow]], evidence_subjects: Mapping[str, str]
) -> dict[str, tuple[set[str], set[str]]]:
    """Row id -> (rooms, room-level subjects) from the subjects of its facts.

    Subjects are written "room/equipment" by the mappers.  A room is a
    location that has its own room-level record; any other subject, and a row
    without a named subject, counts as the whole site.
    """

    keys = {object_key(subject) for subject in evidence_subjects.values()}
    rooms = {key for key in keys if "/" not in key}
    places: dict[str, tuple[set[str], set[str]]] = {}
    for rows in ledger.values():
        for row in rows:
            found: set[str] = set()
            whole: set[str] = set()
            for evidence_id in row.evidence_ids:
                subject = evidence_subjects.get(evidence_id)
                if subject is None:
                    continue
                key = object_key(subject)
                room = key.split("/", 1)[0]
                if room in rooms:
                    found.add(room)
                    if key == room:
                        whole.add(room)
                else:
                    found.add(SITE)
            places[row.id] = (found or {SITE}, whole)
    return places


def _entry(row: JudgmentRow, link: str) -> dict[str, Any]:
    return {
        "row_id": row.id,
        "submodule_id": row.submodule_id,
        "object": row.object,
        "checkpoint": row.checkpoint,
        "disposition": row.disposition,
        "evidence_status": row.evidence_status,
        "basis": row.basis,
        "link": link,
    }


def structural_links(
    ledger: Mapping[str, Sequence[JudgmentRow]],
    evidence_subjects: Mapping[str, str],
    pairs: Sequence[tuple[str, str]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Row id -> other modules' rows joined by room containment or a mechanism pair."""

    pairs = mechanism_pairs() if pairs is None else pairs
    places = row_places(ledger, evidence_subjects)
    rows = [(module_id, row) for module_id, module_rows in ledger.items() for row in module_rows]
    found: dict[str, dict[str, dict[str, Any]]] = {}

    def link(a: JudgmentRow, b: JudgmentRow, kind: str) -> None:
        found.setdefault(a.id, {}).setdefault(b.id, _entry(b, kind))
        found.setdefault(b.id, {}).setdefault(a.id, _entry(a, kind))

    for index, (module_a, a) in enumerate(rows):
        rooms_a, whole_a = places.get(a.id, ({SITE}, set()))
        for module_b, b in rows[index + 1:]:
            if module_a == module_b or not ({a.disposition, b.disposition} & ATTENTION):
                continue
            rooms_b, whole_b = places.get(b.id, ({SITE}, set()))
            # Problems of a room and of the equipment recorded in it add up;
            # a sound item in the room adds nothing to a room-level problem.
            both = {a.disposition, b.disposition} <= ATTENTION
            if both and ((whole_a & rooms_b and not whole_b >= rooms_b) or (
                whole_b & rooms_a and not whole_a >= rooms_a
            )):
                link(a, b, "room")
                continue
            paired = any(
                (_within(a.submodule_id, x) and _within(b.submodule_id, y))
                or (_within(a.submodule_id, y) and _within(b.submodule_id, x))
                for x, y in pairs
            )
            if paired and (rooms_a & rooms_b or SITE in rooms_a or SITE in rooms_b):
                link(a, b, "mechanism")
    return {row_id: list(others.values()) for row_id, others in found.items()}


def joined_see_also(
    *views: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Several row-id views merged; the first view that names a row pair wins."""

    merged: dict[str, dict[str, Mapping[str, Any]]] = {}
    for view in views:
        for row_id, others in view.items():
            bucket = merged.setdefault(row_id, {})
            for other in others:
                bucket.setdefault(other["row_id"], other)
    return {row_id: [dict(o) for o in others.values()] for row_id, others in merged.items()}


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
                others[other.id] = _entry(other, "same_object")
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


def checked_relations(
    ledger: Mapping[str, Sequence[JudgmentRow]],
    see_also: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, list[JudgmentRow]], dict[str, list[str]]]:
    """Keep only related rows that are another module's judgment on the same object.

    Returns the ledger with the other references removed, and what was removed.
    """

    kept: dict[str, list[JudgmentRow]] = {}
    dropped: dict[str, list[str]] = {}
    for module_id, rows in ledger.items():
        kept[module_id] = []
        for row in rows:
            allowed = {other["row_id"] for other in see_also.get(row.id, ())}
            good = [row_id for row_id in row.related_row_ids if row_id in allowed]
            if bad := [row_id for row_id in row.related_row_ids if row_id not in allowed]:
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
    "joined_see_also",
    "mechanism_pairs",
    "row_places",
    "structural_links",
    "conflict_summary",
    "object_chains",
    "related_rows",
    "section_see_also",
    "see_also_by_row",
]
