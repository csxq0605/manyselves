"""Cross-module views: conflicts apart from complements, routing by row, chains and see-also."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
    CrossFinding,
    JudgmentRow,
    cross_finding_findings,
    drop_mirrored_relations,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_module import (
    checked_relations,
    object_chains,
    related_rows,
    section_see_also,
    see_also_by_row,
    unresolved_conflicts,
)
from manyselves.capabilities.distribution_reporting.runtime.joint_dossiers import (
    _Dossier,
    _facts_judged_differently,
)
from manyselves.capabilities.distribution_reporting.runtime.ledger_tools import LedgerTools


def _row(row_id: str, submodule: str, disposition: str, *, evidence: str, checkpoint: str = "c") -> JudgmentRow:
    value: dict[str, Any] = {
        "id": row_id, "submodule_id": submodule, "object": "1#变压器", "checkpoint": checkpoint,
        "disposition": disposition, "evidence_status": "现场确认", "evidence_ids": [evidence],
        "basis": f"{row_id} 的依据",
    }
    if disposition == "concern":
        value |= {"risk_level": "中", "hazard": "h",
                  "action": {"measure": "核查", "priority": "一般", "timeframe": "一月"}}
    if disposition == "undetermined":
        value |= {"gap": {"missing": "m", "impact": "i", "requested_input": "补资料"}}
    return JudgmentRow.model_validate(value)


def _judgment(module: str, row: JudgmentRow) -> dict[str, Any]:
    return {"module_id": module, "submodule_id": row.submodule_id, "row_id": row.id,
            "evidence_ids": list(row.evidence_ids), "disposition": row.disposition}


LEDGER = {
    "2.1": [_row("J-2.1-1", "2.1.1", "no_concern", evidence="E-1", checkpoint="负荷率")],
    "2.2": [_row("J-2.2-1", "2.2.2", "concern", evidence="E-2", checkpoint="噪声")],
    "2.4": [_row("J-2.4-1", "2.4.2", "concern", evidence="E-3", checkpoint="联锁")],
}


def test_only_one_fact_judged_differently_is_a_conflict() -> None:
    found = _Dossier("1#变压器")
    found.facts = {e: {"evidence_id": e} for e in ("E-1", "E-2", "E-3")}
    found.judgments = [_judgment(m, rows[0]) for m, rows in LEDGER.items()]
    # Three modules, three checkpoints, different conclusions: they complement each other.
    assert _facts_judged_differently(found) == []

    same_fact = _row("J-2.4-2", "2.4.1", "concern", evidence="E-1")
    found.judgments.append(_judgment("2.4", same_fact))
    (conflict,) = _facts_judged_differently(found)
    assert conflict["evidence_id"] == "E-1"
    assert {j["row_id"] for j in conflict["judgments"]} == {"J-2.1-1", "J-2.4-2"}


def test_facts_judged_differently_stay_open_only_on_a_reviewers_blocking_finding() -> None:
    differently = {
        "dossier_id": "D-001", "kind": "judged_differently",
        "facts_judged_differently": [{"evidence_id": "E-1", "judgments": [
            {"module_id": "2.1", "row_id": "J-2.1-1", "disposition": "no_concern"},
            {"module_id": "2.4", "row_id": "J-2.4-1", "disposition": "concern"},
        ]}],
    }
    shown = {"dossier_id": "D-002", "kind": "conflict"}
    assert unresolved_conflicts([differently, shown], {}) == [shown]
    # A relation-lens finding is not about the dossier.
    relation_only = {"J-2.4-1": {"relation": {"severity": "blocking"}}}
    assert unresolved_conflicts([differently], relation_only) == []
    flagged = {"J-2.4-1": {"module": {"severity": "blocking"}}}
    assert unresolved_conflicts([differently], flagged) == [differently]


def test_a_relation_judged_not_to_hold_at_both_ends_goes_from_both() -> None:
    def related(row_id: str, *others: str) -> JudgmentRow:
        row = _row(row_id, row_id.split("-")[1] + ".1", "concern", evidence="E-1")
        return JudgmentRow.model_validate({**row.model_dump(), "relations": [
            {"row_id": other, "relation": "r", "reason": "x"} for other in others
        ]})

    before = {
        "2.1": [related("J-2.1-1", "J-2.4-1"), related("J-2.1-2", "J-2.4-2")],
        "2.4": [related("J-2.4-1", "J-2.1-1"), related("J-2.4-2", "J-2.1-2")],
    }
    # Both 2.1 rows drop their relation; the reviewer found J-2.1-1 and
    # J-2.4-1 wanting, and J-2.1-2 alone (J-2.4-2 was not challenged).
    after = {
        "2.1": [related("J-2.1-1"), related("J-2.1-2")],
        "2.4": before["2.4"],
    }
    ledger, removed = drop_mirrored_relations(before, after, ["J-2.1-1", "J-2.4-1", "J-2.1-2"])
    assert removed == [{"row_id": "J-2.4-1", "other_row_id": "J-2.1-1", "relation": "r"}]
    assert ledger["2.4"][0].relations == []
    assert [r.row_id for r in ledger["2.4"][1].relations] == ["J-2.1-2"]
    # Only one end found wanting: the other end keeps its relation.
    ledger, removed = drop_mirrored_relations(before, after, ["J-2.1-1"])
    assert removed == [] and [r.row_id for r in ledger["2.4"][0].relations] == ["J-2.1-1"]


def test_chains_and_see_also_join_the_modules_on_one_object() -> None:
    dossier = {
        "dossier_id": "D-001", "subject": "1#变压器", "kind": "complement",
        "judgments": [_judgment(m, rows[0]) for m, rows in LEDGER.items()],
    }
    quiet = {
        "dossier_id": "D-002", "subject": "2#柜", "kind": "complement",
        "judgments": [_judgment("2.1", LEDGER["2.1"][0]), _judgment("2.2", LEDGER["2.1"][0])],
    }
    (chain,) = object_chains([dossier, quiet], LEDGER)  # D-002 has nothing flagged
    assert chain["subject"] == "1#变压器" and not chain["in_conflict"]
    assert [r["row_id"] for r in chain["rows"]] == ["J-2.1-1", "J-2.2-1", "J-2.4-1"]

    see_also = see_also_by_row([dossier], LEDGER)
    entries = section_see_also(LEDGER["2.2"], see_also)
    assert {e["row_id"] for e in entries} == {"J-2.1-1", "J-2.4-1"}
    assert all(e["for_rows"] == ["J-2.2-1"] for e in entries)
    assert next(e for e in entries if e["row_id"] == "J-2.1-1")["checkpoint"] == "负荷率"


def test_a_finding_on_another_modules_row_does_not_reach_this_author(tmp_path: Path) -> None:
    merged = LedgerTools(tmp_path).merge_findings(
        {
            "item": {"module_id": "2.2", "program_findings": {}},
            "review": {"findings": {
                "J-2.2-1": {"problem": "a", "severity": "blocking", "suggestion": "s"},
                "J-2.4-1": {"problem": "b", "severity": "blocking", "suggestion": "s"},
            }},
        }
    )
    assert set(merged["revision_input"]["findings"]) == {"J-2.2-1"}
    assert set(merged["foreign_findings"]) == {"J-2.4-1"}


def test_only_blocking_findings_send_the_author_back(tmp_path: Path) -> None:
    tools = LedgerTools(tmp_path)
    quiet = tools.merge_findings({"item": {"module_id": "2.2", "program_findings": {}}, "review": {}})
    assert quiet["revise"] is False
    minor = {"J-2.2-1": {"problem": "p", "severity": "minor", "suggestion": "s"}}
    merged = tools.merge_findings(
        {"item": {"module_id": "2.2", "program_findings": minor}, "review": {}}
    )
    assert merged["revise"] is False and merged["revision_input"]["minor"] == minor
    blocking = {"J-2.2-1": {"problem": "p", "severity": "blocking", "suggestion": "s"}}
    merged = tools.merge_findings(
        {"item": {"module_id": "2.2", "program_findings": {}}, "review": {"findings": blocking}}
    )
    assert merged["revise"] is True and set(merged["revision_input"]["findings"]) == {"J-2.2-1"}


def test_authors_see_what_other_modules_flagged_and_the_relation_method(tmp_path: Path) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.cross_module import flagged_rows

    flagged = flagged_rows(LEDGER)
    assert {entry["module_id"] for entry in flagged} <= set(LEDGER)
    tools = LedgerTools(tmp_path)
    tools.store.write_json(
        "Work/runs/r/ledger/relation-candidates.json", {"flagged": flagged, "method": "候选关系与核实条件"}
    )
    # What one author weighs in its relate step, before review.
    candidates = tools._relation_candidates("r", "2.2")
    others = [e for e in flagged if e["module_id"] != "2.2"]
    lines = candidates["flagged_elsewhere"]
    assert len(lines) == len(others) and not any(line.startswith("J-2.2-") for line in lines)
    assert candidates["relation_method"] == "候选关系与核实条件"


def test_a_row_keeps_only_listed_relations_to_existing_rows_of_other_modules() -> None:
    from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import RowRelation
    from manyselves.capabilities.distribution_reporting.runtime.cross_module import relation_names

    method = "说明。\n\n| 候选关系 | 要核实的条件 |\n| --- | --- |\n| 甲—乙 | 同期数据 |\n| 丙—丁 | 接线 |\n"
    names = relation_names(method)
    assert names == ["甲—乙", "丙—丁"]

    def rel(row_id: str, relation: str = "甲—乙") -> RowRelation:
        return RowRelation(row_id=row_id, relation=relation, reason="两边事实满足条件。")

    row = LEDGER["2.2"][0].model_copy(update={"relations": [
        rel("J-2.1-1"), rel("J-2.2-1"), rel("J-2.3-9"), rel("J-2.4-1", "同一房间"),
    ]})
    ledger = {**LEDGER, "2.2": [row]}
    kept, dropped = checked_relations(ledger, names)
    assert kept["2.2"][0].related_row_ids == ["J-2.1-1"]
    assert {d["row_id"]: d["why"] for d in dropped["J-2.2-1"]} == {
        "J-2.2-1": "所引的是本模块的行",
        "J-2.3-9": "所引的行不存在",
        "J-2.4-1": "知识库没有收录这种关联，又没有写机理和成立条件",
    }
    (entry,) = related_rows(kept["2.2"], kept)
    assert entry["row_id"] == "J-2.1-1" and entry["submodule_id"] == "2.1.1"
    assert entry["basis"] == "J-2.1-1 的依据"
    assert entry["for_rows"] == [{"row_id": "J-2.2-1", "relation": "甲—乙", "reason": "两边事实满足条件。"}]


def test_revisions_change_only_the_authors_own_rows(tmp_path: Path) -> None:
    (tmp_path / "Work/runs/r/ledger").mkdir(parents=True)
    context = {
        "run_id": "r",
        "modules": [
            {"module_id": m, "title": m, "submodules": [{"id": rows[0].submodule_id, "title": "t"}]}
            for m, rows in LEDGER.items()
        ],
        "module_titles": {m: m for m in LEDGER},
    }
    ledger = {"run_id": "r", "ledger": {m: [r.model_dump(mode="json") for r in rows] for m, rows in LEDGER.items()}}
    foreign = LEDGER["2.4"][0].model_dump(mode="json") | {"basis": "被 2.2 改写"}
    own = LEDGER["2.2"][0].model_dump(mode="json") | {"basis": "改好的依据"}
    final = LedgerTools(tmp_path).finalize_ledger(
        {
            "context": context,
            "ledger": ledger,
            "reviews": {"2.2": {"findings": {}, "revision": {"rows": [own, foreign],
                                                            "removed_row_ids": ["J-2.4-1"]}}},
        }
    )
    assert [r["id"] for r in final["ledger"]["2.2"]] == ["J-2.2-1"]
    assert final["ledger"]["2.2"][0]["basis"] == "改好的依据"
    assert final["ledger"]["2.4"][0]["basis"] == "J-2.4-1 的依据"
    assert final["chief_input"]["object_chains"] == []  # no evidence index here
    assert all(section["related_rows"] == [] for item in final["writing_items"] for section in item["sections"])


def test_cross_findings_cite_two_modules_and_appear_in_the_overview() -> None:
    good = CrossFinding(subject="1#变压器", conclusion="负荷低，可排除过载致噪。", row_ids=["J-2.1-1", "J-2.2-1"])
    assert cross_finding_findings([good], rows_by_module=LEDGER, overview="总体：负荷低，可排除过载致噪。") == {}

    one_module = CrossFinding(subject="1#变压器", conclusion="x。", row_ids=["J-2.2-1", "J-2.2-1"])
    missing = CrossFinding(subject="2#柜", conclusion="y。", row_ids=["J-2.1-1", "J-9.9-9"])
    problem = cross_finding_findings(
        [good, one_module, missing], rows_by_module=LEDGER, overview="x。"
    )["3.1"]["problem"]
    assert "只来自一个模块" in problem and "J-9.9-9" in problem and "没有原样写进" in problem

