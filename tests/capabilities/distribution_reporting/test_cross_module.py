"""Cross-module views: conflicts apart from complements, routing by row, chains and see-also."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
    CrossFinding,
    JudgmentRow,
    cross_finding_findings,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_module import (
    checked_relations,
    object_chains,
    related_rows,
    section_see_also,
    see_also_by_row,
)
from manyselves.capabilities.distribution_reporting.runtime.joint_dossiers import (
    _conflicting_facts,
    _Dossier,
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
    assert _conflicting_facts(found) == []

    same_fact = _row("J-2.4-2", "2.4.1", "concern", evidence="E-1")
    found.judgments.append(_judgment("2.4", same_fact))
    (conflict,) = _conflicting_facts(found)
    assert conflict["evidence_id"] == "E-1"
    assert {j["row_id"] for j in conflict["judgments"]} == {"J-2.1-1", "J-2.4-2"}


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


def test_the_author_revises_when_other_modules_judged_its_objects(tmp_path: Path) -> None:
    tools = LedgerTools(tmp_path)
    quiet = tools.merge_findings({"item": {"module_id": "2.2", "program_findings": {}}, "review": {}})
    assert quiet["revise"] is False
    related = {"J-2.2-1": [{"row_id": "J-2.1-1", "basis": "负荷率 6.44%"}]}
    merged = tools.merge_findings(
        {"item": {"module_id": "2.2", "program_findings": {}, "related": related}, "review": {}}
    )
    assert merged["revise"] is True and merged["blocking"] is False
    assert merged["revision_input"]["related"] == related


def test_a_row_keeps_only_references_to_other_modules_on_its_object() -> None:
    dossier = {
        "dossier_id": "D-001", "subject": "1#变压器", "kind": "complement",
        "judgments": [_judgment(m, rows[0]) for m, rows in LEDGER.items()],
    }
    row = LEDGER["2.2"][0].model_copy(update={"related_row_ids": ["J-2.1-1", "J-2.2-1", "J-2.3-9"]})
    ledger = {**LEDGER, "2.2": [row]}
    kept, dropped = checked_relations(ledger, see_also_by_row([dossier], ledger))
    assert kept["2.2"][0].related_row_ids == ["J-2.1-1"]
    assert dropped == {"J-2.2-1": ["J-2.2-1", "J-2.3-9"]}
    (entry,) = related_rows(kept["2.2"], kept)
    assert entry["row_id"] == "J-2.1-1" and entry["submodule_id"] == "2.1.1"
    assert entry["basis"] == "J-2.1-1 的依据" and entry["for_rows"] == ["J-2.2-1"]


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
