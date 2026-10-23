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
- ``checked_relations`` and ``related_rows``: the cross-module relations a
  row records (``relations``: row, relation, reason), kept only when they
  name an existing row of another module and either follow a relation the
  knowledge base's method lists or state their own mechanism and condition
  (marked as not in the knowledge base), and their content for the writer.
  A relation marked 待核实 (pending) is kept when it says which condition is
  still missing;
- ``same_observation_rows`` and ``same_observation_findings``: rows of
  different sections that rest on the same source cells (one observation
  routed to several sections).  They are one record seen from two angles:
  not a relation, counted once, and judged at one level, the owner's.
  ``shared_observation_groups`` takes rows sharing any routed observation,
  not only the same set, for grading and for the chief.

The program only offers candidates it can tell from the fixed structure (the
same fact or object) and checks that a cited row exists.  Whether two rows
bear on each other is the author's judgment, made with the knowledge base's
own method for cross-unit relations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..domain.judgment_ledger import JudgmentRow
from .joint_dossiers import object_level

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
        cited = {e for _module, row in rows for e in row.evidence_ids}
        chains.append(
            {
                "dossier_id": dossier["dossier_id"],
                "subject": dossier["subject"],
                # Same record row = one unit; same name only = maybe one unit,
                # maybe only entries of one place: the wording follows the level.
                "level": object_level(
                    fact for fact in dossier.get("facts") or () if fact["evidence_id"] in cited
                ),
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


def relation_names(method: str) -> list[str]:
    """The candidate relations a relation method lists: the first column of its tables."""

    names: list[str] = []
    table: list[list[str]] = []
    for line in [*method.splitlines(), ""]:
        if line.strip().startswith("|"):
            table.append([cell.strip() for cell in line.strip().strip("|").split("|")])
            continue
        # A table is a header row, a separator row and its data rows.
        for cells in table[2:]:
            if cells and cells[0] and cells[0] not in names:
                names.append(cells[0])
        table = []
    return names


def in_knowledge_base(relation: Any, names: Sequence[str]) -> bool:
    """Whether a relation is one the knowledge base's relation method lists."""

    return relation.relation.strip() in set(names)


def _observations(row: JudgmentRow, observation_of: Mapping[str, str]) -> frozenset[str]:
    """The source observations a row rests on (a fact not routed twice is its own)."""

    return frozenset(observation_of.get(e, e) for e in row.evidence_ids)


def same_observation_rows(
    ledger: Mapping[str, Sequence[JudgmentRow]],
    observation_of: Mapping[str, str],
) -> list[list[JudgmentRow]]:
    """Groups of rows in different sections resting on exactly the same observations.

    Only observations routed to several sections count: two rows citing the
    same plain fact are left to the relation step.
    """

    shared = set(observation_of.values())
    groups: dict[frozenset[str], list[JudgmentRow]] = {}
    for rows in ledger.values():
        for row in rows:
            key = _observations(row, observation_of)
            if key and key <= shared:
                groups.setdefault(key, []).append(row)
    return [
        rows for rows in groups.values()
        if len({row.submodule_id for row in rows}) > 1
    ]


def shared_observation_groups(
    ledger: Mapping[str, Sequence[JudgmentRow]],
    observation_of: Mapping[str, str],
) -> list[list[JudgmentRow]]:
    """For every routed observation, the rows of different sections that cite it.

    A row that cites one copy of a source cell rests on that cell, whatever
    else it also cites, so rows sharing any routed observation are one record
    seen from several sections.  Groups with the same rows are given once.
    """

    by_observation: dict[str, dict[str, JudgmentRow]] = {}
    for rows in ledger.values():
        for row in rows:
            for evidence_id in row.evidence_ids:
                if (observation := observation_of.get(evidence_id)) is not None:
                    by_observation.setdefault(observation, {})[row.id] = row
    groups: dict[frozenset[str], list[JudgmentRow]] = {}
    for rows in by_observation.values():
        if len({row.submodule_id for row in rows.values()}) > 1:
            groups.setdefault(frozenset(rows), list(rows.values()))
    return list(groups.values())


def same_observation_findings(
    ledger: Mapping[str, Sequence[JudgmentRow]],
    observation_of: Mapping[str, str],
    owner_sections: Mapping[str, str],
) -> dict[str, dict[str, dict[str, str]]]:
    """Module -> row -> finding: one observation judged at two levels.

    ``owner_sections`` maps an observation to the section that owns it (the
    knowledge base's first route, or the ownership rules).  A concern row of
    another section that cites the same observation, alone or among other
    facts, must take the owner's risk level and priority; it keeps its own
    angle in its text, and a reason it gives for differing is weighed in
    revision.
    """

    module_of = {row.id: m for m, rows in ledger.items() for row in rows}
    by_observation: dict[str, list[JudgmentRow]] = {}
    for rows in ledger.values():
        for row in rows:
            for observation in _observations(row, observation_of) & set(owner_sections):
                if row not in by_observation.setdefault(observation, []):
                    by_observation[observation].append(row)
    findings: dict[str, dict[str, dict[str, str]]] = {}
    for observation, rows in by_observation.items():
        owner_section = owner_sections.get(observation)
        owner_rows = [row for row in rows if row.submodule_id == owner_section]
        if not owner_rows or len({row.submodule_id for row in rows}) < 2:
            continue
        owner = owner_rows[0]
        if owner.disposition != "concern":
            continue
        level = (owner.risk_level, owner.action.priority if owner.action else None)
        for row in rows:
            if row.submodule_id == owner_section or row.disposition != "concern":
                continue
            mine = (row.risk_level, row.action.priority if row.action else None)
            if mine == level or row.id in findings.get(module_of[row.id], {}):
                continue
            findings.setdefault(module_of[row.id], {})[row.id] = {
                "problem": (
                    f"本行与 {owner.submodule_id} 的 {owner.id} 引用了同一个表格单元格（同一观测），"
                    f"那里定为风险{owner.risk_level}、{level[1]}，本行定为风险{row.risk_level}、{mine[1]}；"
                    "同一观测在报告里只能有一个当前定级"
                ),
                "severity": "blocking",
                "suggestion": (
                    f"risk_level 和优先级改成与 {owner.id} 一致，本行只写本节的视角；"
                    "本行另有事实使定级不同、或认为负责小节定级不对的，在 responses 写明理由和会改变定级的条件"
                ),
            }
    return findings


def checked_relations(
    ledger: Mapping[str, Sequence[JudgmentRow]],
    names: Sequence[str] = (),
    observation_of: Mapping[str, str] | None = None,
) -> tuple[dict[str, list[JudgmentRow]], dict[str, list[dict[str, str]]]]:
    """Keep only relations to an existing row of another module, with their grounds.

    ``names`` are the candidate relations of the knowledge base's method.  A
    relation under one of them rests on the knowledge base.  An author may
    also relate rows the method does not cover; such a relation is kept only
    when it states its mechanism and the condition it holds under, and it is
    marked as not in the knowledge base.  Two rows resting on the same
    observations (``observation_of``: routed copy -> observation) are one
    record, not a relation.  Returns the ledger with the other relations
    removed, and what was removed with why.
    """

    owner = {row.id: module_id for module_id, rows in ledger.items() for row in rows}
    same = {
        frozenset((a.id, b.id))
        for rows in same_observation_rows(ledger, observation_of or {})
        for a in rows for b in rows if a.id != b.id
    }
    listed = set(names)
    kept: dict[str, list[JudgmentRow]] = {}
    dropped: dict[str, list[dict[str, str]]] = {}
    for module_id, rows in ledger.items():
        kept[module_id] = []
        for row in rows:
            good = []
            for relation in row.relations:
                if owner.get(relation.row_id) is None:
                    why = "所引的行不存在"
                elif owner[relation.row_id] == module_id:
                    why = "所引的是本模块的行"
                elif frozenset((row.id, relation.row_id)) in same:
                    why = "两行引用的是同一个表格单元格（同一观测），是同一记录的两个视角，不是关联"
                elif (
                    listed
                    and relation.relation.strip() not in listed
                    and not (relation.mechanism.strip() and relation.condition.strip())
                ):
                    why = "知识库没有收录这种关联，又没有写机理和成立条件"
                elif relation.pending and not relation.missing.strip():
                    why = "标为待核实，却没有写缺哪项条件"
                else:
                    good.append(relation)
                    continue
                dropped.setdefault(row.id, []).append(
                    {"row_id": relation.row_id, "relation": relation.relation, "why": why}
                )
            if len(good) != len(row.relations):
                row = row.model_copy(update={"relations": good})
            kept[module_id].append(row)
    return kept, dropped


def related_rows(
    rows: Sequence[JudgmentRow],
    ledger: Mapping[str, Sequence[JudgmentRow]],
    names: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """The other modules' rows that this section's rows say bear on their conclusions."""

    index = _row_index(ledger)
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        for relation in row.relations:
            row_id = relation.row_id
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
            entry["for_rows"].append(
                {
                    "row_id": row.id,
                    "relation": relation.relation,
                    "reason": relation.reason,
                    **(
                        {"status": relation.status, "missing": relation.missing}
                        if relation.pending else {}
                    ),
                    **(
                        {}
                        if not names or in_knowledge_base(relation, names)
                        else {"in_knowledge_base": False}
                    ),
                }
            )
    return list(found.values())


def unlisted_relations(
    ledger: Mapping[str, Sequence[JudgmentRow]], names: Sequence[str]
) -> list[dict[str, Any]]:
    """Relations the authors drew that the knowledge base's method does not list.

    They are kept with the run as candidates for the knowledge base's
    maintenance, never written back automatically.
    """

    index = _row_index(ledger)
    found = []
    for module_id, rows in ledger.items():
        for row in rows:
            for relation in row.relations:
                if in_knowledge_base(relation, names) or relation.row_id not in index:
                    continue
                other_module, other = index[relation.row_id]
                found.append({
                    "relation": relation.relation,
                    "mechanism": relation.mechanism,
                    "condition": relation.condition,
                    "reason": relation.reason,
                    "from": {"row_id": row.id, "submodule_id": row.submodule_id,
                             "checkpoint": row.checkpoint, "rule_refs": list(row.rule_refs)},
                    "to": {"row_id": other.id, "submodule_id": other.submodule_id,
                           "checkpoint": other.checkpoint, "rule_refs": list(other.rule_refs)},
                })
    return found


# Lenses that read the dossiers of facts judged differently.
DOSSIER_LENSES = ("module", "object")


def unresolved_conflicts(
    dossiers: Sequence[Mapping[str, Any]],
    findings_by_row: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> list[Mapping[str, Any]]:
    """The conflicts still open after revision.

    The ones the table shows (a quantity read twice with two values, a photo
    against its record) always count.  Facts several modules still judge
    differently count only when a reviewer reading them raised a blocking
    finding on one of the rows that judge them.
    """

    def reviewed_blocking(row_id: str) -> bool:
        by_lens = findings_by_row.get(row_id) or {}
        return any(
            (by_lens.get(lens) or {}).get("severity") == "blocking" for lens in DOSSIER_LENSES
        )

    open_ = []
    for dossier in dossiers:
        if dossier["kind"] == "conflict":
            open_.append(dossier)
        elif dossier["kind"] == "judged_differently" and any(
            reviewed_blocking(judgment["row_id"])
            for fact in dossier.get("facts_judged_differently") or ()
            for judgment in fact["judgments"]
            if judgment.get("row_id")
        ):
            open_.append(dossier)
    return open_


def conflict_summary(dossier: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "dossier_id": dossier["dossier_id"],
        "subject": dossier["subject"],
        "reasons": list(dossier.get("reasons", [])),
        **(
            {"facts_judged_differently": dossier["facts_judged_differently"]}
            if dossier.get("facts_judged_differently")
            else {}
        ),
        **(
            {"differing_quantities": dossier["differing_quantities"]}
            if dossier.get("differing_quantities")
            else {}
        ),
    }


__all__ = [
    "checked_relations",
    "flagged_rows",
    "in_knowledge_base",
    "unlisted_relations",
    "relation_names",
    "conflict_summary",
    "unresolved_conflicts",
    "object_chains",
    "related_rows",
    "same_observation_findings",
    "same_observation_rows",
    "shared_observation_groups",
    "section_see_also",
    "see_also_by_row",
]
