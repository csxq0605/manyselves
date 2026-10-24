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

from ..domain.judgment_ledger import CrossReview, JudgmentRow, StoryLine, StoryPlan
from .context_packs import (
    ATTENTION,
    PROJECT_LEVEL,
    Sites,
    brief_row,
    compact,
    compact_row,
    cross_batches,
    object_batches,
    project_map,
    row_line,
    writing_groups,
)
from .cross_module import relation_names, same_observation_rows
from .gap_entries import chief_gap_entries, gap_ids, gap_labels
from .observation_ledger import shared_labels
from .photo_reading import load_photo_readings, photo_reading_labels
from .research.fact_context import render_fact_packet
from .research.project_evidence import ProjectEvidenceIndex
from .review_packets import ReviewPackets

# Character budgets for one conversation's material (about two characters a
# token); the tasks' attention budgets are the hard limit.
# A reviewer's output grows with what it is given: this keeps one object
# review batch to a few minutes of writing.
OBJECT_BATCH_CHARS = 60_000
# The cross-module review's shared prefix (rows, records, map), about 220k
# tokens at most; beyond it the review is split by site.
CROSS_PREFIX_CHARS = 440_000
# Records an author gets with its relate candidates (same objects, proposals,
# then the same room's flagged rows while they fit).
RELATE_FACT_CHARS = 90_000
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

    def _renderer(self, run_id: str, *, for_writer: bool = False):
        reference = self._reference(run_id)
        shared = shared_labels(self.workspace, run_id)
        readings = photo_reading_labels(self.workspace, run_id, for_writer=for_writer)
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

    def _checked_proposals(
        self, run_id: str, ledger: Mapping[str, Sequence[JudgmentRow]], results: Mapping[str, Any]
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """The cross-module review's proposals that stand, offered to the authors of both rows.

        A proposal stands when both rows exist, belong to different modules,
        do not rest on one source cell and are not related already, and it is
        either a relation the knowledge base lists or states its mechanism and
        condition.  Each author gets it for its own row, with the other row in
        full; both decide for their own rows.
        """

        module_of = {row.id: m for m, rows in ledger.items() for row in rows}
        index = {row.id: row for rows in ledger.values() for row in rows}
        names = set(relation_names(self._relation_method(run_id)))
        observation_of, _owners = self._observation_map(run_id)
        same = {
            frozenset((a.id, b.id))
            for rows in same_observation_rows(ledger, observation_of)
            for a in rows for b in rows if a.id != b.id
        }
        related = {
            frozenset((row.id, relation.row_id))
            for rows in ledger.values() for row in rows for relation in row.relations
        }
        row_groups = self._groups_file(run_id).get("row_groups") or {}
        offers: dict[str, dict[str, list[dict[str, Any]]]] = {}
        kept: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        seen: set[tuple[frozenset[str], str]] = set()
        for batch_id, result in results.items():
            for proposal in CrossReview.model_validate(_plain(result) or {}).proposals:
                entry = {"batch_id": batch_id, **proposal.model_dump(mode="json")}
                a, b = proposal.row_id, proposal.other_row_id
                pair = frozenset((a, b))
                if a not in module_of or b not in module_of:
                    why = "所引的行不存在"
                elif module_of[a] == module_of[b]:
                    why = "两行属于同一个模块"
                elif pair in same:
                    why = "两行引用的是同一个表格单元格（同一观测），不是关联"
                elif pair in related:
                    why = "两行已经有关联"
                elif (pair, proposal.relation.strip()) in seen:
                    why = "重复的提议"
                elif (
                    names
                    and proposal.relation.strip() not in names
                    and not (proposal.mechanism.strip() and proposal.condition.strip())
                ):
                    why = "知识库没有收录这种关联，又没有写机理和成立条件"
                else:
                    seen.add((pair, proposal.relation.strip()))
                    kept.append(entry)
                    for own, other in ((a, b), (b, a)):
                        group_id = row_groups.get(own, module_of[own])
                        offers.setdefault(group_id, {}).setdefault(own, []).append(compact({
                            "row_id": other,
                            "relation": proposal.relation,
                            "reason": proposal.reason,
                            "mechanism": proposal.mechanism,
                            "condition": proposal.condition,
                            "other_row": compact_row(index[other]),
                        }))
                    continue
                dropped.append({**entry, "why": why})
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/cross-proposals.json", {"kept": kept, "dropped": dropped}
        )
        return offers

    def plan_relate(self, value: dict[str, Any]) -> dict[str, Any]:
        """One relate turn per judging group, right after it judged.

        Each author relates its rows to the other modules' rows on the same
        objects and the same room's flagged rows, with the records they cite;
        what it misses is the cross-module review's to propose.
        """

        context = value["context"]
        built = value["ledger"]
        run_id = context["run_id"]
        ledger = _ledger(built)
        module_of = {row.id: m for m, rows in ledger.items() for row in rows}
        index = {row.id: row for rows in ledger.values() for row in rows}
        names = set(relation_names(self._relation_method(run_id)))
        row_groups = self._groups_file(run_id).get("row_groups") or {}
        sites = self._sites(run_id)
        render = self._renderer(run_id)
        flagged = [row for rows in ledger.values() for row in rows if row.disposition in ATTENTION]
        items = []
        candidates: dict[str, dict[str, list[str]]] = {}
        for own_item in value.get("own_items") or []:
            group_id = own_item.get("group_id") or own_item["module_id"]
            module_id = own_item["module_id"]
            item = {**own_item, "relation_names": sorted(names) or own_item.get("relation_names", [])}
            mine = [
                row for row in ledger.get(module_id, [])
                if row_groups.get(row.id, group_id) == group_id
            ]
            related = list(dict.fromkeys(
                other["row_id"]
                for others in (own_item.get("related") or {}).values() for other in others
                if other.get("row_id") in index
            ))
            # Rows of other modules flagged at the same distribution room (the
            # tables' room column), so links between different objects there
            # can be checked against their records too.
            places = {p for row in mine for p in sites.row_places(row)} - {PROJECT_LEVEL}
            seen = set(related)
            same_room = [
                row.id for row in flagged
                if module_of[row.id] != module_id and row.id not in seen
                and places & set(sites.row_places(row))
            ]
            # Records of the candidates, nearest first, within the budget: the
            # same objects' always, the same room's while they fit.
            cited: list[Any] = []
            cited_ids: set[str] = set()
            kept_room: list[str] = []
            size = 0
            for row_id in [*related, *same_room]:
                new = [
                    sites.items[e] for e in index[row_id].evidence_ids
                    if e in sites.items and e not in cited_ids
                ]
                cost = len(render(new)) if new else 0
                if row_id in same_room:
                    if size + cost > RELATE_FACT_CHARS:
                        continue
                    kept_room.append(row_id)
                cited.extend(new)
                cited_ids.update(record.id for record in new)
                size += cost
            if kept_room:
                item["same_room"] = {row_id: brief_row(index[row_id]) for row_id in kept_room}
            item["facts"] = render(cited) or "（候选行没有引用记录）"
            candidates[group_id] = {"related": related, "same_room": kept_room}
            items.append(item)
        self.store.write_json(f"Work/runs/{run_id}/ledger/relate-candidates.json", candidates)
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
        method = self._relation_method(run_id) or "（知识库没有关联方法）"
        # The cross-module review: every slice opens with the same map, method,
        # rows and records, and differs only in what it is asked to decide.
        relation_map = project_map(sites, ledger, with_relations=True)
        cross_items = [
            {"run_id": run_id, "map": relation_map, "relation_method": method, **batch}
            for batch in cross_batches(ledger, sites, render, prefix_chars=CROSS_PREFIX_CHARS)
        ]
        # The object lens reads every module's rows on objects several judged,
        # with the final photo readings.
        object_items = [
            {"run_id": run_id, **batch}
            for batch in object_batches(ledger, sites, render, budget_chars=OBJECT_BATCH_CHARS)
        ]
        lenses = {
            "objects": [{"batch_id": b["batch_id"], "objects": b["objects"]} for b in object_items]
        }
        lenses["cross"] = [
            {"batch_id": b["batch_id"], "relations": b["relations"], "focus_rows": b["focus_rows"]}
            for b in cross_items
        ]
        self.store.write_json(f"Work/runs/{run_id}/ledger/review-lenses.json", lenses)
        return {
            "review_items": review_items,
            "object_items": object_items,
            "cross_items": cross_items,
        }

    def gather_reviews(self, value: dict[str, Any]) -> dict[str, Any]:
        """The three lenses' findings merged by row, one review per judging group to settle.

        The cross-module review's findings are the relation lens; its proposals
        that stand go with the findings to both rows' groups.

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
                "relation": findings(value.get("cross_reviews")),
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

        unassigned = set(self._reference(run_id).get("unassigned_evidence_ids") or ())

        def in_group(module_id: str, target: str, group_id: str) -> bool:
            # A finding on a record with no section goes to every group of
            # the module, like the record itself.
            if target in unassigned:
                return groups.get(group_id, {}).get("module_id", group_id) == module_id
            return group_of(module_id, target) == group_id

        def group_of(module_id: str, target: str) -> str:
            mine = [gid for gid, g in groups.items() if g.get("module_id") == module_id]
            if target in row_groups and row_groups[target] in mine:
                return row_groups[target]
            for gid in mine:
                if target in (groups[gid].get("evidence_ids") or ()):
                    return gid
            return next((gid for gid in mine if groups[gid].get("first")), mine[0] if mine else module_id)

        # Relations the cross-module review found missing go to both rows'
        # authors, settled with the findings in one revision.
        offers = self._checked_proposals(run_id, ledger, value.get("cross_reviews") or {})
        sites = self._sites(run_id)
        render = self._renderer(run_id)
        index = {row.id: row for rows in ledger.values() for row in rows}
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
                    if in_group(module_id, target, group_id)
                },
                "review": {"findings": {
                    target: finding
                    for target, finding in per_module.get(module_id, {}).items()
                    if in_group(module_id, target, group_id)
                }},
                **self._proposal_material(offers.get(group_id) or {}, own_rows, index, sites, render),
            })
        return {"settle_items": items}

    @staticmethod
    def _proposal_material(
        offered: Mapping[str, Sequence[Mapping[str, Any]]],
        own_rows: Sequence[JudgmentRow],
        index: Mapping[str, JudgmentRow],
        sites: Sites,
        render: Any,
    ) -> dict[str, Any]:
        """A group's proposals, with the records of the other rows it has not read."""

        if not offered:
            return {}
        own_ids = {row.id for row in own_rows}
        offered = {row_id: list(entries) for row_id, entries in offered.items() if row_id in own_ids}
        if not offered:
            return {}
        seen = {e for row in own_rows for e in row.evidence_ids}
        others = [index[o["row_id"]] for entries in offered.values() for o in entries if o["row_id"] in index]
        records = [
            sites.items[e]
            for e in dict.fromkeys(e for row in others for e in row.evidence_ids)
            if e in sites.items and e not in seen
        ]
        return {
            "proposed": offered,
            "proposal_facts": render(records) or "（另一端的行没有引用别的记录）",
        }

    # -- story and writing ----------------------------------------------

    def story_input(
        self,
        run_id: str,
        ledger: Mapping[str, Sequence[JudgmentRow]],
        *,
        chains: Sequence[Mapping[str, Any]],
        same_observation: Sequence[Mapping[str, Any]],
        gaps: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """What the story-line planner reads: the map, every flagged row briefly, what joins them.

        ``gaps`` are the settled ledger's gap entries: the map, the rows and
        everything built on them (the chief, its checkers, the joint review)
        say each gap once, as its entry does.
        """

        sites = self._sites(run_id)
        gap_of = gap_ids(gaps)
        return {
            "run_id": run_id,
            "map": project_map(sites, ledger, with_relations=True, gaps=gap_labels(gaps)),
            "rows": [
                brief_row(row, gap_id=gap_of.get(row.id)) for rows in ledger.values() for row in rows
                if row.disposition in ATTENTION
            ],
            "gap_entries": chief_gap_entries(gaps),
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
        render = self._renderer(run_id, for_writer=True)
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
                # Most shared first: every writer's numbers and length
                # reference, then the module's method, then the group's facts.
                pack = "\n\n".join(
                    block
                    for block in [
                        reference.get("numbers") or "",
                        length_reference,
                        "# 专家版方法（本模块，写正文看“写法”和“范例”）\n"
                        + (skills.get(module_id) or "（本模块没有专家版方法）"),
                        "# 本组小节所引的事实\n" + render(cited) if cited else "",
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
    "CROSS_PREFIX_CHARS",
    "LedgerStageTools",
    "merged_finding",
    "relation_lines",
]
