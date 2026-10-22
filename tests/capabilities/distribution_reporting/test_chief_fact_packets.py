"""Chapters 1 and 3 are fact-checked in small packets the program builds."""

from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import JudgmentRow
from manyselves.capabilities.distribution_reporting.runtime.chief_fact_packets import (
    apply_fact_findings,
    chief_fact_packets,
    chief_units,
)


def _row(row_id, obj, evidence, disposition="concern"):
    extra = (
        {"risk_level": "中", "hazard": "h", "hazard_conditions": ["c"],
         "action": {"measure": "m", "priority": "一般", "timeframe": "一月"}}
        if disposition == "concern" else {}
    )
    return JudgmentRow.model_validate(
        {"id": row_id, "submodule_id": "2.1.1", "object": obj, "checkpoint": "运行电流",
         "disposition": disposition, "evidence_status": "现场确认", "evidence_ids": [evidence],
         "basis": "b", **extra}
    )


CHAPTER1 = {
    "assessment_background": "本次评估覆盖两个配电房。",
    "overall_assessment": "A房 1# 柜电流偏高。\n其余正常。",
    "dimension_health": [{"module_id": "2.1", "status": "s", "key_indicators": "运行电流 13 个",
                          "risk_level": "中"}],
    "key_risks": [{"description": "d", "hazard": "h", "action": "a", "row_ids": ["J-1"]}],
    "phases": [],
    "regional_executive_summary": "B房 2# 柜正常。",
}
CHAPTER3 = {
    "risk_summary": ["A房 1# 柜电流偏高。"],
    "risk_details": [{"module_id": "2.1", "points": ["电流偏高。"]}],
    "cross_findings": [{"subject": "A房/1#", "conclusion": "c", "row_ids": ["J-1", "J-2"]}],
    "action_lead": "",
    "actions": [{"group_id": "G-01", "problem": "p", "hazard": "h", "solution": "s"}],
}
GROUPS = [{"group_id": "G-01", "row_ids": ["J-1"]}]
LEDGER = {"2.1": [_row("J-1", "A房/1#", "E-1"), _row("J-2", "B房/2#", "E-2")]}
SLOTS = {
    "V-0001": {"text": "262A", "subject": "A房/1#", "label": "运行电流", "value": 262.0, "unit": "A",
               "evidence_ids": ["E-1"]},
    "K-0001": {"text": "14个", "subject": "运行电流（低配 C列）", "label": "record_count", "value": 14.0,
               "unit": "个", "evidence_ids": ["E-1", "E-2"]},
    "S-0001": {"text": "A房/1#262A", "label": "运行电流（低配 C列）", "evidence_ids": ["E-1", "E-2"],
               "取值": "100A、262A"},
}


def test_every_paragraph_table_row_risk_and_group_is_a_unit() -> None:
    units = {unit["unit_id"] for unit in chief_units(CHAPTER1, CHAPTER3, GROUPS)}
    assert units == {
        "1.1-段1", "1.2-段1", "1.2-段2", "1.3-段1", "1.2-健康度-2.1", "1.2-重点风险-1",
        "3.1-汇总-1", "3.1-详述-2.1-1", "3.1-综合-1", "3.2-G-01",
    }


def test_a_unit_gets_its_rows_records_values_and_the_counts_of_its_columns() -> None:
    packets = chief_fact_packets(
        chapter1=CHAPTER1, chapter3=CHAPTER3, ledger=LEDGER, groups=GROUPS, slots=SLOTS,
        fact_texts={"E-1": "运行电流=262A", "E-2": "运行电流=100A"}, run_id="r",
    )
    units = {u["unit_id"]: u for p in packets for u in p["units"]}
    assert len(units) == 10  # every unit lands in one packet
    # A paragraph is held against the rows of the objects it names in full.
    assert units["1.2-段1"]["row_ids"] == ["J-1"]
    assert units["1.3-段1"]["row_ids"] == ["J-2"]
    # A structured unit against the rows it cites.
    assert units["3.1-综合-1"]["row_ids"] == ["J-1", "J-2"]
    # A dimension's health, citing no row, gets its dimension's column counts.
    assert "〔K-0001〕" in units["1.2-健康度-2.1"]["counts"]
    packet = next(p for p in packets if any(u["unit_id"] == "1.2-段1" for u in p["units"]))
    assert packet["facts"]["E-1"] == "运行电流=262A"
    assert "〔V-0001〕" in packet["values"]
    assert "〔S-0001:取值〕" in packet["counts"]


def test_packets_close_on_unit_count_or_size() -> None:
    packets = chief_fact_packets(
        chapter1=CHAPTER1, chapter3=CHAPTER3, ledger=LEDGER, groups=GROUPS, slots=SLOTS,
        fact_texts={}, run_id="r", max_units=3,
    )
    assert all(len(p["units"]) <= 3 for p in packets) and len(packets) == 4
    small = chief_fact_packets(
        chapter1=CHAPTER1, chapter3=CHAPTER3, ledger=LEDGER, groups=GROUPS, slots=SLOTS,
        fact_texts={}, run_id="r", max_chars=10,
    )
    assert len(small) == 10  # one unit each when nothing more fits


def test_findings_go_back_by_chapter_and_only_blocking_ones_send_the_chief_back() -> None:
    packets = [{"units": [{"unit_id": "1.2-重点风险-1", "where": "1.2 重点风险表"},
                          {"unit_id": "3.2-G-01", "where": "3.2 改善行动速查表"}]}]
    results = {"F-01": {"findings": {
        "1.2-重点风险-1": {"problem": "13 应为 14", "severity": "blocking", "suggestion": "改为 14"},
        "3.2-G-01": {"problem": "措辞可更准", "severity": "minor", "suggestion": "s"},
        "9.9-x": {"problem": "unknown unit", "severity": "blocking"},
    }}, "F-02": None}
    applied = apply_fact_findings(packets, results)
    assert applied["blocking1"] is True and applied["blocking3"] is False
    assert applied["check1"]["findings"]["1.2-重点风险-1"]["problem"].startswith("1.2 重点风险表：")
    assert set(applied["check3"]["findings"]) == {"3.2-G-01"}
