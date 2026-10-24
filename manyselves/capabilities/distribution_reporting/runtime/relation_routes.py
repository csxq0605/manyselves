"""Relations that are not cross-module relations, sorted out by their structure.

A relation says one module's judgment adds to, explains, rules out or
changes another's.  Three shapes recorded as relations never do that, and the
program reads each from the two rows' dispositions, places and records alone,
never from the words:

- ``GAP_BOTH``: both rows could not be judged.  Two gaps side by side say
  nothing the gap entries do not; each entry names the other's rows.
- ``GAP_AFFECTS``: one row could not be judged and the pair is project level
  (no shared object or place in the records).  A missing document or record
  bearing on another row is said once, on the gap entry, as a row it affects.
- ``SAME_ROOM``: both rows hold a concern in the same distribution room and
  neither cites a reading.  Two findings that each stand on their own are
  listed together once, under their room.

Everything else stays a relation and goes to the authors' review.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..domain.judgment_ledger import JudgmentRow
from .context_packs import PROJECT_LEVEL, Sites

GAP_BOTH = "两端都不能判定"
GAP_AFFECTS = "一端不能判定且为项目级"
SAME_ROOM = "同一配电房各自成立"


def _read(row: JudgmentRow, sites: Sites) -> bool:
    return any(
        getattr(m, "value", None) is not None
        for e in row.evidence_ids if e in sites.items
        for m in sites.items[e].measurements or ()
    )


def _record_places(row: JudgmentRow, sites: Sites) -> set[str]:
    return set(sites.row_places(row)) - {PROJECT_LEVEL}


def _places(row: JudgmentRow, sites: Sites, known: Sequence[str]) -> set[str]:
    """The rooms of a row: its records' places, else a known place its object is named under."""

    found = _record_places(row, sites)
    if found:
        return found
    return {place for place in known if row.object.startswith(place)}


def known_places(sites: Sites) -> list[str]:
    # Longest first, so a room is not taken for a shorter name it starts with.
    return sorted((p for p in sites.objects() if p != PROJECT_LEVEL), key=len, reverse=True)


def route(a: JudgmentRow, b: JudgmentRow, sites: Sites, known: Sequence[str] = ()) -> str | None:
    """Where a relation between two rows belongs when it is not a relation; None when it is."""

    undetermined = [row.disposition == "undetermined" for row in (a, b)]
    if all(undetermined):
        return GAP_BOTH
    same_object = bool(set(sites.row_subjects(a)) & set(sites.row_subjects(b)))
    project_level = not same_object and not (_record_places(a, sites) and _record_places(b, sites))
    if any(undetermined) and project_level:
        return GAP_AFFECTS
    if (
        a.disposition == b.disposition == "concern"
        and _places(a, sites, known) & _places(b, sites, known)
        and not _read(a, sites)
        and not _read(b, sites)
    ):
        return SAME_ROOM
    return None


def routed_relations(
    ledger: Mapping[str, Sequence[JudgmentRow]], sites: Sites
) -> tuple[dict[str, list[JudgmentRow]], list[dict[str, Any]]]:
    """The ledger without the relations that are gaps or same-room listings, and those, once a pair."""

    index = {row.id: row for rows in ledger.values() for row in rows}
    known = known_places(sites)
    kept: dict[str, list[JudgmentRow]] = {}
    routes: dict[frozenset[str], dict[str, Any]] = {}
    for module_id, rows in ledger.items():
        kept[module_id] = []
        for row in rows:
            good = []
            for relation in row.relations:
                other = index.get(relation.row_id)
                where = route(row, other, sites, known) if other is not None else None
                if where is None:
                    good.append(relation)
                    continue
                pair = frozenset((row.id, other.id))
                entry = routes.setdefault(pair, {
                    "rows": sorted(pair),
                    "route": where,
                    "relation": relation.relation,
                    "reason": relation.reason,
                })
                if where == SAME_ROOM:
                    entry["places"] = sorted(_places(row, sites, known) & _places(other, sites, known))
            if len(good) != len(row.relations):
                row = row.model_copy(update={"relations": good})
            kept[module_id].append(row)
    return kept, sorted(routes.values(), key=lambda e: e["rows"])


def still_routed(
    routes: Sequence[Mapping[str, Any]],
    ledger: Mapping[str, Sequence[JudgmentRow]],
    sites: Sites,
) -> list[dict[str, Any]]:
    """Earlier routes that still hold for the revised rows (both rows kept, same shape)."""

    index = {row.id: row for rows in ledger.values() for row in rows}
    known = known_places(sites)
    found = []
    for entry in routes:
        a, b = (index.get(row_id) for row_id in entry["rows"])
        if a is None or b is None:
            continue
        where = route(a, b, sites, known)
        if where is None:
            continue
        found.append({
            **entry,
            "route": where,
            **({"places": sorted(_places(a, sites, known) & _places(b, sites, known))}
               if where == SAME_ROOM else {}),
        })
    return found


def gap_affects(
    routes: Sequence[Mapping[str, Any]], ledger: Mapping[str, Sequence[JudgmentRow]]
) -> dict[str, list[dict[str, str]]]:
    """Row id of a row with a gap -> the other rows its gap bears on (from the gap routes)."""

    index = {row.id: row for rows in ledger.values() for row in rows}
    affects: dict[str, list[dict[str, str]]] = {}
    for entry in routes:
        if entry["route"] not in (GAP_BOTH, GAP_AFFECTS):
            continue
        for mine, theirs in (entry["rows"], entry["rows"][::-1]):
            row, other = index.get(mine), index.get(theirs)
            if row is None or other is None or row.gap is None:
                continue
            listed = affects.setdefault(row.id, [])
            if all(x["row_id"] != other.id for x in listed):
                listed.append({
                    "row_id": other.id,
                    "submodule_id": other.submodule_id,
                    "object": other.object,
                    "checkpoint": other.checkpoint,
                })
    return affects


def same_room_issues(
    routes: Sequence[Mapping[str, Any]], ledger: Mapping[str, Sequence[JudgmentRow]]
) -> list[dict[str, Any]]:
    """Each room once, with the concerns of different modules that were only side by side there."""

    index = {row.id: row for rows in ledger.values() for row in rows}
    rooms: dict[str, list[str]] = {}
    for entry in routes:
        if entry["route"] != SAME_ROOM:
            continue
        for place in entry.get("places") or ():
            listed = rooms.setdefault(place, [])
            listed.extend(r for r in entry["rows"] if r not in listed and r in index)
    return [
        {
            "place": place,
            "rows": [
                {
                    "row_id": index[row_id].id,
                    "submodule_id": index[row_id].submodule_id,
                    "object": index[row_id].object,
                    "checkpoint": index[row_id].checkpoint,
                    "finding": index[row_id].finding or index[row_id].basis,
                }
                for row_id in sorted(row_ids, key=lambda r: (index[r].submodule_id, r))
            ],
        }
        for place, row_ids in rooms.items()
    ]


__all__ = [
    "GAP_AFFECTS",
    "GAP_BOTH",
    "SAME_ROOM",
    "gap_affects",
    "known_places",
    "route",
    "routed_relations",
    "same_room_issues",
    "still_routed",
]
