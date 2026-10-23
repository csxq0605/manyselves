"""Tools of the ledger flow that hand each role its own material.

``LedgerTools`` mixes these in.  They run between the model steps:

- ``plan_relate`` checks the cross-module linker's proposals and puts each
  one in front of the authors of both rows, next to the other modules'
  flagged rows and the rows on the same objects they already see;
- ``plan_review`` builds the three review lenses after the photos are read
  again: one packet per module (as before), batches of objects several
  modules judged, and batches of relations;
- ``gather_reviews`` merges the three lenses' findings by row and gives
  each module one review to settle;
- ``apply_story`` checks the story lines and plans the writing in groups of
  sections, each with the facts its rows cite and the lines through it.

What goes where follows the table's columns and the ledger's ids; sizes are
character budgets.  The model decides what relates, what is wrong and what
the story is; the program checks ids and structure.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from manyselves.kernel.workflow import gather_by_key

from ..domain.judgment_ledger import JudgmentRow, LinkProposals, StoryLine, StoryPlan
from .context_packs import (
    ATTENTION,
    Sites,
    brief_row,
    compact,
    compact_row,
    project_map,
    relation_batches,
    row_line,
    writing_groups,
)
from .cross_module import relation_names, same_observation_rows
from .observation_ledger import shared_labels
from .photo_reading import load_photo_readings, photo_reading_labels
from .research.fact_context import render_fact_packet
from .research.project_evidence import ProjectEvidenceIndex
from .review_packets import ReviewPackets

# Character budgets for one conversation's material (about two characters a
# token); the tasks' attention budgets are the hard limit.
LINK_PACK_CHARS = 160_000
# A reviewer's output grows with what it is given: these keep one review
# batch to a few minutes of writing.
OBJECT_BATCH_CHARS = 60_000
RELATION_BATCH_CHARS = 60_000
WRITING_GROUP_CHARS = 60_000

LENSES = {"module": "模块切面", "object": "对象切面", "relation": "关联切面"}
_SEVERITY = {"blocking": 2, "minor": 1}


def _rows(values: Sequence[Any]) -> list[JudgmentRow]:
    return [JudgmentRow.model_validate(value) for value in values]


def _ledger(value: Mapping[str, Any]) -> dict[str, list[JudgmentRow]]:
    return {module_id: _rows(rows) for module_id, rows in value["ledger"].items()}


def _plain(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def merged_finding(by_lens: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """One row's findings from several lenses as one finding, each part labelled."""

    parts = [(lens, finding) for lens, finding in by_lens.items() if isinstance(finding, Mapping)]
    if len(parts) == 1:
        return dict(parts[0][1])
    severity = max((str(f.get("severity", "minor")) for _l, f in parts), key=lambda s: _SEVERITY.get(s, 0))

    def joined(field: str) -> str:
        return "；".join(
            f"（{LENSES.get(lens, lens)}）{f[field]}" for lens, f in parts if f.get(field)
        )

    merged: dict[str, Any] = {
        "problem": joined("problem"),
        "severity": severity,
        "suggestion": joined("suggestion"),
    }
    grounds = {k: v for _l, f in parts for k, v in (f.get("grounds") or {}).items()} if all(
        isinstance(f.get("grounds", {}), Mapping) for _l, f in parts
    ) else None
    if grounds:
        merged["grounds"] = grounds
    return merged


class LedgerStageTools:
    """Mixed into ``LedgerTools``; uses its workspace, store and references."""

    # -- material --------------------------------------------------------

    def _sites(self, run_id: str) -> Sites:
        return Sites(ProjectEvidenceIndex(self.workspace, run_id).items())

    def _renderer(self, run_id: str):
        reference = self._reference(run_id)
        shared = shared_labels(self.workspace, run_id)
        readings = photo_reading_labels(self.workspace, run_id)
        headers = {
            (path, sheet): columns
            for path, sheet, columns in reference.get("column_headers") or []
        }

        def render(items: Sequence[Any]) -> str:
            if not items:
                return ""
            return render_fact_packet(
                list(items), "相关", shared=shared, photo_readings=readings,
                column_headers=headers,
            )

        return render

    # -- relations -------------------------------------------------------

    def _groups_file(self, run_id: str) -> dict[str, Any]:
        """The judging groups and which group wrote each row (written by build-ledger)."""

        path = self.workspace / f"Work/runs/{run_id}/ledger/groups.json"
        if not path.is_file():
            return {"groups": {}, "row_groups": {}}
        return json.loads(path.read_text(encoding="utf-8"))

    def plan_relate(self, value: dict[str, Any]) -> dict[str, Any]:
        """One relate round per judging group: its own linking and the linker's proposals.

        A proposal stands when both rows exist, belong to different modules
        and do not rest on one source cell, and it is either a relation the
        knowledge base lists or states its mechanism and condition.  It goes to
        the authors of both rows, with the other row in full and the records it
        cites; each decides for its own rows, and links what else it sees, in
        the same turn.
        """

        context = value["context"]
        built = value["ledger"]
        run_id = context["run_id"]
        ledger = _ledger(built)
        module_of = {row.id: m for m, rows in ledger.items() for row in rows}
        index = {row.id: row for rows in ledger.values() for row in rows}
        names = set(relation_names(self._relation_method(run_id)))
        observation_of, _owners = self._observation_map(run_id)
        same = {
            frozenset((a.id, b.id))
            for rows in same_observation_rows(ledger, observation_of)
            for a in rows for b in rows if a.id != b.id
        }
        groups = self._groups_file(run_id)
        row_groups = groups.get("row_groups") or {}
        proposed: dict[str, dict[str, list[dict[str, Any]]]] = {}
        kept: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        for pack_id, result in (value.get("links") or {}).items():
            for proposal in LinkProposals.model_validate(_plain(result)).proposals:
                entry = {"pack_id": pack_id, **proposal.model_dump(mode="json")}
                a, b = proposal.row_id, proposal.other_row_id
                if a not in module_of or b not in module_of:
                    why = "所引的行不存在"
                elif module_of[a] == module_of[b]:
                    why = "两行属于同一个模块"
                elif frozenset((a, b)) in same:
                    why = "两行引用的是同一个表格单元格（同一观测），不是关联"
                elif (
                    names
                    and proposal.relation.strip() not in names
                    and not (proposal.mechanism.strip() and proposal.condition.strip())
                ):
                    why = "知识库没有收录这种关联，又没有写机理和成立条件"
                else:
                    kept.append(entry)
                    for own, other in ((a, b), (b, a)):
                        group_id = row_groups.get(own, module_of[own])
                        offer = compact({
                            "row_id": other,
                            "relation": proposal.relation,
                            "reason": proposal.reason,
                            "mechanism": proposal.mechanism,
                            "condition": proposal.condition,
                            "other_row": compact_row(index[other]),
                        })
                        rows = proposed.setdefault(group_id, {}).setdefault(own, [])
                        if all(o["row_id"] != other or o["relation"] != offer["relation"] for o in rows):
                            rows.append(offer)
                    continue
                dropped.append({**entry, "why": why})
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/link-proposals.json", {"kept": kept, "dropped": dropped}
        )
        sites = self._sites(run_id)
        render = self._renderer(run_id)
        items = []
        for own_item in value.get("own_items") or []:
            group_id = own_item.get("group_id") or own_item["module_id"]
            offers = proposed.get(group_id) or {}
            item = {**own_item, "relation_names": sorted(names) or own_item.get("relation_names", [])}
            if offers:
                others = [index[o["row_id"]] for entries in offers.values() for o in entries]
                cited = [
                    sites.items[e]
                    for e in dict.fromkeys(e for row in others for e in row.evidence_ids)
                    if e in sites.items
                ]
                item["proposed"] = offers
                item["facts"] = render(cited) or "（另一端的行没有引用记录）"
            else:
                item["facts"] = "（专员没有对本组的行提出关联）"
            items.append(item)
        return {"relate_items": items}

    # -- review lenses ---------------------------------------------------

    def plan_review(self, value: dict[str, Any]) -> dict[str, Any]:
        """The module and relation lenses, built from the related ledger and the final readings.

        The module lens reads one module's rows with what they cite, and
        nothing of the other modules: what lies between modules is the object
        lens's (run while the rows were related) and the relation lens's.
        """

        context = value["context"]
        related = value["ledger"]
        run_id = context["run_id"]
        ledger = _ledger(related)
        reference = self._reference(run_id)
        readings = load_photo_readings(self.workspace, run_id)
        elsewhere = self._data_elsewhere(run_id, ledger)
        packets = ReviewPackets(self, run_id, ledger, reference, elsewhere)
        sites = self._sites(run_id)
        render = self._renderer(run_id)
        review_items = [
            {
                "run_id": run_id,
                "module_id": module["module_id"],
                "title": module["title"],
                "program_findings": (related.get("program_findings") or {}).get(
                    module["module_id"], {}
                ),
                "photos": self._photos_to_check(ledger[module["module_id"]], reference, readings),
                **packets.packet(module["module_id"]),
            }
            for module in context["modules"]
        ]
        method = self._relation_method(run_id)
        relation_items = [
            {"run_id": run_id, "relation_method": method or "（知识库没有关联方法）", **batch}
            for batch in relation_batches(
                ledger, sites, render, budget_chars=RELATION_BATCH_CHARS
            )
        ]
        path = self.workspace / f"Work/runs/{run_id}/ledger/review-lenses.json"
        lenses = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        lenses["relations"] = [
            {"batch_id": b["batch_id"], "relations": b["relations"]} for b in relation_items
        ]
        self.store.write_json(f"Work/runs/{run_id}/ledger/review-lenses.json", lenses)
        return {"review_items": review_items, "relation_items": relation_items}

    def gather_reviews(self, value: dict[str, Any]) -> dict[str, Any]:
        """The three lenses' findings merged by row, one review per judging group to settle.

        A finding on a row goes to the group that wrote the row; one on a
        record (a fact no row covers) to the group judging that record's
        section, or the module's first group.
        """

        ledger = _ledger(value["ledger"])
        run_id = value["ledger"]["run_id"]
        module_of = {row.id: m for m, rows in ledger.items() for row in rows}

        def findings(results: Mapping[str, Any] | None) -> dict[str, Any]:
            found: dict[str, Any] = {}
            for result in (results or {}).values():
                for target, finding in (_plain(result) or {}).get("findings", {}).items():
                    if target in found:
                        found[target] = merged_finding({"a": found[target], "b": finding})
                    else:
                        found[target] = finding
            return found

        by_row = gather_by_key(
            {
                "module": findings(value.get("module_reviews")),
                "object": findings(value.get("object_reviews")),
                "relation": findings(value.get("relation_reviews")),
            }
        )
        per_module: dict[str, dict[str, Any]] = {}
        unplaced: dict[str, Any] = {}
        for target, by_lens in by_row.items():
            module_id = module_of.get(target)
            if module_id is None:
                unplaced[target] = by_lens
                continue
            per_module.setdefault(module_id, {})[target] = merged_finding(by_lens)
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/review-lens-findings.json",
            {"by_row": by_row, "unplaced": unplaced},
        )
        stored = self._groups_file(run_id)
        groups = stored.get("groups") or {
            module_id: {"module_id": module_id, "submodule_ids": [], "evidence_ids": [], "first": True}
            for module_id in ledger
        }
        row_groups = stored.get("row_groups") or {}
        program = value["ledger"].get("program_findings") or {}

        def group_of(module_id: str, target: str) -> str:
            mine = [gid for gid, g in groups.items() if g.get("module_id") == module_id]
            if target in row_groups and row_groups[target] in mine:
                return row_groups[target]
            for gid in mine:
                if target in (groups[gid].get("evidence_ids") or ()):
                    return gid
            return next((gid for gid in mine if groups[gid].get("first")), mine[0] if mine else module_id)

        items = []
        for group_id, group in groups.items():
            module_id = group.get("module_id", group_id)
            if module_id not in ledger:
                continue
            own_rows = [
                row for row in ledger[module_id]
                if group_of(module_id, row.id) == group_id
            ]
            items.append({
                "run_id": run_id,
                "module_id": module_id,
                "group_id": group_id,
                "submodule_ids": list(group.get("submodule_ids") or ()),
                "rows": [row.model_dump(mode="json", exclude_none=True) for row in own_rows],
                "program_findings": {
                    target: finding
                    for target, finding in (program.get(module_id) or {}).items()
                    if group_of(module_id, target) == group_id
                },
                "review": {"findings": {
                    target: finding
                    for target, finding in per_module.get(module_id, {}).items()
                    if group_of(module_id, target) == group_id
                }},
            })
        return {"settle_items": items}

    # -- story and writing ----------------------------------------------

    def story_input(
        self,
        run_id: str,
        ledger: Mapping[str, Sequence[JudgmentRow]],
        *,
        chains: Sequence[Mapping[str, Any]],
        same_observation: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """What the story-line planner reads: the map, every flagged row briefly, what joins them."""

        sites = self._sites(run_id)
        return {
            "run_id": run_id,
            "map": project_map(sites, ledger, with_relations=True),
            "rows": [
                brief_row(row) for rows in ledger.values() for row in rows
                if row.disposition in ATTENTION
            ],
            "object_chains": list(chains),
            "same_observation": list(same_observation),
        }

    def apply_story(self, value: dict[str, Any]) -> dict[str, Any]:
        """Story lines, and the writing and the chief's input planned with them.

        Without a planned story, the lines are the program's: rows joined by
        relations that hold, across at least two modules.  A pending relation
        stays with its sections in chapter 2 and joins no line.
        """

        final = dict(value["ledger"])
        run_id = final["run_id"]
        ledger = _ledger(final)
        index = {row.id: (m, row) for m, rows in ledger.items() for row in rows}
        lines: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        story = value.get("story")
        planned = (
            StoryPlan.model_validate(_plain(story)).lines if story else relation_lines(ledger)
        )
        for number, line in enumerate(planned, start=1):
            ids = [row_id for row_id in dict.fromkeys(line.row_ids) if row_id in index]
            modules = {index[row_id][0] for row_id in ids}
            if len(ids) < 2 or len(modules) < 2:
                dropped.append({**line.model_dump(mode="json"), "why": "链上不足两个模块的已有行"})
                continue
            sections = list(dict.fromkeys(index[row_id][1].submodule_id for row_id in ids))
            lines.append(
                {
                    "line_id": f"L{number}",
                    "title": line.title,
                    "conclusion": line.conclusion,
                    "row_ids": ids,
                    "sections": sections,
                    "section_notes": {
                        k: v for k, v in line.section_notes.items() if k in sections
                    },
                }
            )
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/story-lines.json", {"lines": lines, "dropped": dropped}
        )
        reference = self._reference(run_id)
        skills = reference.get("author_skills") or {}
        sites = self._sites(run_id)
        render = self._renderer(run_id)
        length_reference = reference.get("length_reference") or ""
        groups: list[dict[str, Any]] = []
        for item in final.get("writing_items") or []:
            module_id = item["module_id"]
            briefs = [
                {
                    **brief,
                    **(
                        {"story_lines": story_lines}
                        if (story_lines := [
                            compact({
                                "title": line["title"],
                                "conclusion": line["conclusion"],
                                "row_ids": line["row_ids"],
                                "other_sections": [
                                    s for s in line["sections"] if s != brief["submodule_id"]
                                ],
                                "note": line["section_notes"].get(brief["submodule_id"], ""),
                            })
                            for line in lines
                            if brief["submodule_id"] in line["sections"]
                        ])
                        else {}
                    ),
                }
                for brief in item["sections"]
            ]
            parts = writing_groups(module_id, briefs, budget_chars=WRITING_GROUP_CHARS)
            for number, sections in enumerate(parts, start=1):
                if not sections:
                    continue
                group_id = module_id if len(parts) == 1 else f"{module_id}#{number}"
                rows = [
                    index[row["id"]][1]
                    for section in sections
                    for row in section.get("rows") or []
                    if row["id"] in index
                ]
                related = [
                    index[entry["row_id"]][1]
                    for section in sections
                    for entry in section.get("related_rows") or []
                    if entry["row_id"] in index
                ]
                cited = [
                    sites.items[e]
                    for e in dict.fromkeys(e for row in [*rows, *related] for e in row.evidence_ids)
                    if e in sites.items
                ]
                pack = "\n\n".join(
                    block
                    for block in [
                        "# 本组小节所引的事实\n" + render(cited) if cited else "",
                        reference.get("numbers") or "",
                        length_reference,
                        "# 专家版方法（本模块，写正文看“写法”和“范例”）\n"
                        + (skills.get(module_id) or "（本模块没有专家版方法）"),
                    ]
                    if block
                )
                sections = [dict(section) for section in sections]
                sections[0]["pack"] = pack
                groups.append(
                    {
                        "group_id": group_id,
                        "module_id": module_id,
                        "title": item["title"],
                        "sections": sections,
                    }
                )
        chief_input = dict(final.get("chief_input") or {})
        chief_input["story_lines"] = [
            {key: line[key] for key in ("line_id", "title", "conclusion", "row_ids", "sections")}
            for line in lines
        ]
        return {**final, "writing_items": groups, "chief_input": chief_input}

    def _photos_to_check(self, rows, reference, readings):
        from .ledger_tools import photos_to_check

        return photos_to_check(rows, reference, readings)


def relation_lines(ledger: Mapping[str, Sequence[JudgmentRow]]) -> list[StoryLine]:
    """Rows joined by relations that hold, one line per connected set across modules."""

    module_of = {row.id: m for m, rows in ledger.items() for row in rows}
    index = {row.id: row for rows in ledger.values() for row in rows}
    parent: dict[str, str] = {}

    def find(row_id: str) -> str:
        while parent.setdefault(row_id, row_id) != row_id:
            parent[row_id] = parent[parent[row_id]]
            row_id = parent[row_id]
        return row_id

    names: dict[frozenset[str], list[str]] = {}
    for rows in ledger.values():
        for row in rows:
            for relation in row.relations:
                if relation.pending or relation.row_id not in index:
                    continue
                parent[find(row.id)] = find(relation.row_id)
                names.setdefault(frozenset((row.id, relation.row_id)), []).append(relation)
    sets: dict[str, list[str]] = {}
    for row_id in list(parent):
        sets.setdefault(find(row_id), []).append(row_id)
    lines = []
    for members in sets.values():
        ordered = sorted(members, key=lambda rid: (index[rid].submodule_id, rid))
        if len({module_of[rid] for rid in ordered}) < 2:
            continue
        relations = [
            relation
            for pair, found in names.items() if pair <= set(ordered)
            for relation in found
        ]
        titles = list(dict.fromkeys(r.relation for r in relations))
        lines.append(StoryLine(
            title="；".join(titles),
            conclusion="；".join(dict.fromkeys(r.reason for r in relations)),
            row_ids=ordered,
        ))
    return lines


__all__ = [
    "LENSES",
    "LINK_PACK_CHARS",
    "LedgerStageTools",
    "merged_finding",
    "relation_lines",
]
