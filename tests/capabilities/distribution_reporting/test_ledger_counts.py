"""Program counts, orderings and object-value pairings the report is checked against."""

from pathlib import Path

from manyselves.capabilities.distribution_reporting.domain.calculations import statistics
from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
    JudgmentRow,
    JudgmentSet,
    ledger_findings,
    report_check_findings,
)
from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    SourceLocation,
)


def _item(number: int, subject: str, fact: str, value=None, unit=None, column="C") -> EvidenceItem:
    return EvidenceItem(
        id=f"E-{number:04d}", subject=subject, fact=fact, value=value, unit=unit,
        module_id="2.4", submodule_id=next(iter(REPORT_TAXONOMY["2.4"].submodules)),
        source=SourceLocation(file_id="f", path=Path("Inputs/t.xlsx"), sheet="明细",
                              cell=f"{column}{number}", row=number, column=column),
    )


def test_status_counts_name_objects_and_numbers_are_ordered_with_ratios() -> None:
    items = [
        _item(1, "A房/1#", "SPD=OK", column="D"),
        _item(2, "A房/2#", "SPD=NG；指示窗变红", column="D"),
        _item(3, "B房/3#", "SPD=NG", column="D"),
        _item(4, "A房/1#", "剩余电流=2.9A", 2.9, "A"),
        _item(5, "A房/2#", "剩余电流=11.3A", 11.3, "A"),
        _item(6, "B房/3#", "剩余电流=3.1A", 3.1, "A"),
    ]

    calculations, lines = statistics(items, start=5)

    ng = next(c for c in calculations if c.quantity == "ng_count")
    assert (ng.id, ng.value, ng.formula) == ("K-0006", 2, "NG 对象：A房/2#、B房/3#")
    ratio = next(c for c in calculations if c.quantity == "max_to_min_ratio")
    assert ratio.value == 3.9
    assert "按数值从大到小 A房/2#=11.3A、B房/3#=3.1A、A房/1#=2.9A" in lines[1]


def test_counts_and_object_value_pairs_in_prose_must_match() -> None:
    rows = [
        JudgmentRow.model_validate(
            {"id": f"J-2.4-{n}", "submodule_id": "2.4.1", "object": obj, "checkpoint": "SPD",
             "disposition": "concern", "evidence_status": "现场确认", "evidence_ids": [f"E-{n}"], "basis": "NG",
             "risk_level": "中", "hazard": "h",
             "action": {"measure": "m", "priority": "一般", "timeframe": "一月"}}
        )
        for n, obj in ((1, "2#"), (2, "3#"))
    ]
    known = [(11.3, "A"), (3.1, "A"), (9, "个"), (3.6, "倍")]
    pairs = {"A房/2#": [(11.3, "A")], "B房/3#": [(3.1, "A")]}

    def check(text: str):
        return report_check_findings(
            sections={"2.4.1": text}, rows_by_submodule={"2.4.1": rows},
            known_values=known, subject_values=pairs,
        )

    assert check("2#、3# 等共 9 个 SPD，其中 2 处需更换；1个月内完成。") == {}
    assert "10个" in check("2#、3# 等共 10 个 SPD。")["2.4.1"]["problem"]
    assert "3# 旁写的 11.3A" in check("3# 柜剩余电流 11.3A。")["2.4.1"]["problem"]
    assert check("2# 柜 11.3A，约为 3# 柜 3.1A 的 3.6 倍。") == {}


def test_a_concern_row_covers_one_object() -> None:
    judgment = JudgmentSet.model_validate(
        {"module_id": "2.4", "rows": [
            {"id": "J-2.4-1", "submodule_id": "2.4.1", "object": "两台柜", "checkpoint": "SPD",
             "disposition": "undetermined", "evidence_status": "资料未提供", "evidence_ids": ["E-1", "E-2"], "basis": "b",
             "gap": {"missing": "m", "impact": "i", "requested_input": "n"}},
            {"id": "J-2.4-2", "submodule_id": "2.4.1", "object": "其余", "checkpoint": "SPD",
             "disposition": "no_concern", "evidence_status": "现场确认", "evidence_ids": ["E-3", "E-4"], "basis": "b"},
        ]}
    )
    findings = ledger_findings(
        judgment, submodule_ids=["2.4.1"], module_evidence_ids=["E-1", "E-2", "E-3", "E-4"],
        known_evidence_ids=["E-1", "E-2", "E-3", "E-4"], known_photo_ids=[],
        evidence_subjects={"E-1": "1#", "E-2": "2#", "E-3": "3#", "E-4": "4#"},
    )
    assert set(findings) == {"J-2.4-1"}
    assert "一行一个对象" in findings["J-2.4-1"]["problem"]


def test_evidence_status_reaches_the_problem_and_action_tables() -> None:
    from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
        ChiefAction,
        action_table_markdown,
        problem_table,
    )

    row = JudgmentRow.model_validate(
        {"id": "J-2.5-1", "submodule_id": "2.5.1", "object": "运维单位", "checkpoint": "资质",
         "disposition": "undetermined", "evidence_status": "资料未提供", "evidence_ids": [],
         "basis": "未取得资质文件",
         "gap": {"missing": "资质证书", "impact": "不能判断", "requested_input": "资质证书"}}
    )
    assert "（资料未提供）不能判定：资质证书" in problem_table([row])
    action = ChiefAction(priority="一般", content="补资料", problem="资质未提供", hazard="待定",
                         solution="提供资质证书", row_ids=["J-2.5-1"])
    assert "依据：资料未提供" in action_table_markdown("措施如下。", [action], {"J-2.5-1": "资料未提供"})
