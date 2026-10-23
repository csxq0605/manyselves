"""Each role's material is assembled from the table's sites and objects."""

from __future__ import annotations

from typing import Any

from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import JudgmentRow
from manyselves.capabilities.distribution_reporting.runtime.context_packs import (
    PROJECT_LEVEL,
    Sites,
    brief_row,
    compact,
    linker_packs,
    object_batches,
    project_map,
    relation_batches,
    writing_groups,
)
from manyselves.capabilities.distribution_reporting.runtime.ledger_stage_tools import (
    merged_finding,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import EvidenceItem
from manyselves.kernel.workflow import gather_by_key


def _item(eid: str, module_id: str | None, subject: str, place: str | None, fact: str = "x=OK") -> EvidenceItem:
    return EvidenceItem(
        id=eid, subject=subject, place=place, fact=fact, module_id=module_id,
        source={"file_id": "t", "path": "Inputs/t.xlsx", "sheet": "s", "row": 1},
    )


def _row(rid: str, evidence: list[str], disposition: str = "concern", **extra: Any) -> JudgmentRow:
    module_id = rid.split("-")[1]
    values = {
        "id": rid, "submodule_id": f"{module_id}.1", "object": "对象", "checkpoint": "检查",
        "disposition": disposition, "evidence_status": "现场确认", "evidence_ids": evidence,
        "basis": "依据" * 100,
    }
    if disposition == "concern":
        values.update(
            risk_level="中", finding="温度偏高", hazard="可能过热",
            action={"measure": "复测", "priority": "一般", "timeframe": "一月内"},
        )
    if disposition == "undetermined":
        values.update(
            evidence_status="资料未提供",
            gap={"missing": "缺报告", "impact": "无法判断", "requested_input": "补报告"},
        )
    values.update(extra)
    return JudgmentRow.model_validate(values)


ITEMS = [
    _item("E-1", "2.2", "A房/1#柜", "A房", "温度=NG"),
    _item("E-2", "2.4", "A房/1#柜", "A房"),
    _item("E-3", "2.4", "B房/2#柜", "B房"),
    _item("E-4", "2.1", "评估总表 2.1", None),
    _item("E-5", None, "A房/1#柜", "A房"),
]
LEDGER = {
    "2.1": [_row("J-2.1-1", ["E-4"], "undetermined")],
    "2.2": [_row("J-2.2-1", ["E-1"])],
    "2.4": [_row("J-2.4-1", ["E-2"]), _row("J-2.4-2", ["E-3"], "no_concern")],
}


def _render(items) -> str:
    return "\n".join(f"{item.id} {item.fact}" for item in items)


def test_a_judge_sees_the_same_objects_elsewhere_and_the_project_level_records() -> None:
    sites = Sites(ITEMS)
    assert [i.id for i in sites.same_object_items("2.4")] == ["E-1"]
    assert [i.id for i in sites.project_items("2.4")] == ["E-4"]
    assert sites.site_of(LEDGER["2.4"][0]) == "A房"
    assert sites.site_of(LEDGER["2.1"][0]) == PROJECT_LEVEL


def test_the_map_places_records_and_flagged_rows_by_site_and_object() -> None:
    sites = Sites(ITEMS)
    facts = project_map(sites)
    assert "## A房" in facts and "- A房/1#柜｜记录 2.2:1(NG1) 2.4:1 待归属:1" in facts
    judged = project_map(sites, LEDGER)
    assert "  - J-2.2-1 2.2.1｜对象｜检查（关注，中）：温度偏高" in judged
    assert "- B房/2#柜｜记录 2.4:1｜另有 1 行判为无问题或不适用" in judged
    # A row on summary records only is listed on its own.
    assert "只引汇总表或项目级资料" in judged and "J-2.1-1" in judged


def test_the_linker_reads_every_flagged_row_or_splits_by_site() -> None:
    sites = Sites(ITEMS)
    (whole,) = linker_packs(LEDGER, sites, _render, budget_chars=100_000)
    assert [r["id"] for r in whole["rows"]] == ["J-2.1-1", "J-2.2-1", "J-2.4-1"]
    split = linker_packs(LEDGER, sites, _render, budget_chars=10)
    assert [p["scope"] for p in split] == ["配电房：A房", split[-1]["scope"]]
    assert [r["id"] for r in split[0]["rows"]] == ["J-2.2-1", "J-2.4-1"]
    system = split[-1]
    assert [r["id"] for r in system["rows"]] == ["J-2.1-1"]
    assert [line.split()[0] for line in system["other_rows"]] == ["J-2.2-1", "J-2.4-1"]


def test_objects_several_modules_judged_go_to_the_object_lens_with_all_records() -> None:
    (batch,) = object_batches(LEDGER, Sites(ITEMS), _render, budget_chars=100_000)
    assert batch["objects"] == [{"object": "A房/1#柜", "row_ids": ["J-2.2-1", "J-2.4-1"]}]
    assert batch["facts"].split("\n") == ["E-1 温度=NG", "E-2 x=OK", "E-5 x=OK"]


def test_relations_go_to_the_relation_lens_with_both_rows() -> None:
    ledger = {
        **LEDGER,
        "2.2": [_row("J-2.2-1", ["E-1"], relations=[
            {"row_id": "J-2.4-1", "relation": "负荷—发热", "reason": "同一柜"},
        ])],
    }
    (batch,) = relation_batches(ledger, Sites(ITEMS), _render, budget_chars=100_000)
    assert batch["relations"] == [
        {"for_row": "J-2.2-1", "row_id": "J-2.4-1", "relation": "负荷—发热", "reason": "同一柜"}
    ]
    assert {r["id"] for r in batch["rows"]} == {"J-2.2-1", "J-2.4-1"}


def test_rows_lose_their_empty_fields_and_the_chief_reads_them_briefly() -> None:
    assert compact({"a": "", "b": [], "c": {"d": None}, "e": [{"f": ""}], "g": 0}) == {
        "e": [{}], "g": 0,
    }
    brief = brief_row(LEDGER["2.4"][0])
    assert brief["basis"].endswith("…") and len(brief["basis"]) == 121
    assert "evidence_ids" not in brief and brief["finding"] == "温度偏高"


def test_sections_are_grouped_within_a_budget_in_order() -> None:
    sections = [{"id": str(n), "text": "字" * 40} for n in range(5)]
    groups = writing_groups("2.4", sections, budget_chars=130)
    assert [[s["id"] for s in g] for g in groups] == [["0", "1"], ["2", "3"], ["4"]]


def test_lens_findings_on_one_row_are_merged_and_labelled() -> None:
    gathered = gather_by_key(
        {
            "module": {"J-2.4-1": {"problem": "依据不足", "severity": "minor", "suggestion": "补"}},
            "object": {"J-2.4-1": {"problem": "铭牌不符", "severity": "blocking", "suggestion": "改"}},
        }
    )
    merged = merged_finding(gathered["J-2.4-1"])
    assert merged["severity"] == "blocking"
    assert merged["problem"] == "（模块切面）依据不足；（对象切面）铭牌不符"


def test_pending_relations_get_a_light_batch_of_their_own_and_no_story_line() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.ledger_stage_tools import (
        relation_lines,
    )

    ledger = {
        **LEDGER,
        "2.2": [_row("J-2.2-1", ["E-1"], relations=[
            {"row_id": "J-2.4-1", "relation": "负荷—发热", "reason": "同一柜"},
            {"row_id": "J-2.4-2", "relation": "温升—绝缘", "reason": "同房",
             "status": "待核实", "missing": "缺红外复测"},
        ])],
    }
    held, pending = relation_batches(ledger, Sites(ITEMS), _render, budget_chars=100_000)
    assert held["batch_id"].startswith("R-") and len(held["relations"]) == 1
    assert pending["batch_id"].startswith("P-") and pending["pending"] is True
    assert pending["relations"][0]["missing"] == "缺红外复测"
    assert "E-3" not in str(pending["facts"])
    (line,) = relation_lines(ledger)
    assert line.row_ids == ["J-2.2-1", "J-2.4-1"] and line.title == "负荷—发热"
