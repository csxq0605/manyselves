"""What the program hands each writer beside the rows, and what it checks in the prose."""

from __future__ import annotations

from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
    JudgmentRow,
    process_terms,
    report_check_findings,
)
from manyselves.capabilities.distribution_reporting.runtime.ledger_tools import (
    _section_brief,
    _section_state,
)
from manyselves.capabilities.distribution_reporting.runtime.rule_standards import (
    knowledge_units,
    section_standards,
    standards_coverage,
)

KNOWLEDGE = """# 评估方法论

### KU-2.1-01 负荷率评估
限值/阈值：未核实原标准条款。
相关标准：GB 50052-2009《供配电系统设计规范》
依据状态：相关标准仅为候选检索入口。

### KU-2.1-02 无功补偿
- **参考标准**：GB/T 12747
相关标准：无

## 附录
相关标准：不属于任何单元
"""


def _row(row_id: str, disposition: str, refs: list[str] | None = None) -> JudgmentRow:
    value = {
        "id": row_id, "submodule_id": "2.1.1", "object": "1#变压器", "checkpoint": "负荷率",
        "disposition": disposition, "evidence_status": "现场确认", "evidence_ids": ["E-1"],
        "basis": "b", "rule_refs": refs or [],
    }
    if disposition == "concern":
        value |= {"risk_level": "中", "hazard": "h",
                  "action": {"measure": "核查", "priority": "一般", "timeframe": "一月"}}
    if disposition == "undetermined":
        value |= {"gap": {"missing": "m", "impact": "i", "requested_input": "补资料"}}
    return JudgmentRow.model_validate(value)


def test_cited_knowledge_units_reach_the_writer_as_standard_names() -> None:
    units = knowledge_units([KNOWLEDGE])
    assert units["KU-2.1-01"] == {
        "title": "负荷率评估", "standards": "GB 50052-2009《供配电系统设计规范》"
    }
    assert units["KU-2.1-02"]["standards"] == "GB/T 12747"  # list and bold form; "无" ignored
    rows = [_row("J-2.1-1", "concern", ["KU-2.1-01", "KU-9.9-99"]), _row("J-2.1-2", "no_concern", ["KU-2.1-01"])]
    (entry,) = section_standards(rows, units)
    assert entry == {"rule": "负荷率评估", "standards": "GB 50052-2009《供配电系统设计规范》",
                     "for_rows": ["J-2.1-1", "J-2.1-2"]}
    coverage = standards_coverage({"2.1": rows}, units)
    assert coverage["units_with_standards"] == 1 and coverage["unresolved"] == ["KU-9.9-99"]


def test_the_brief_says_how_much_a_section_has_to_say_and_where_its_data_lives() -> None:
    assert _section_state([]) == "no_rows"
    assert _section_state([_row("J-2.1-8", "no_concern")]) == "no_concern"
    assert _section_state([_row("J-2.1-8", "undetermined"), _row("J-2.1-9", "undetermined")]) == "undetermined_only"
    assert _section_state([_row("J-2.1-8", "concern"), _row("J-2.1-9", "no_concern")]) == "mixed"
    rows = [_row("J-2.1-1", "concern", ["KU-2.1-01"])]
    brief = _section_brief(
        {"id": "2.1.1", "title": "t"}, rows, ledger={"2.1": rows},
        units=knowledge_units([KNOWLEDGE]), elsewhere={"J-2.1-1": ["2.2.2.1"], "J-9": ["x"]},
    )
    assert brief["section_state"] == "mixed"
    assert brief["rule_standards"][0]["rule"] == "负荷率评估"
    assert brief["data_elsewhere"] == {"J-2.1-1": ["2.2.2.1"]}


def test_internal_workflow_words_in_prose_are_found_by_the_program() -> None:
    assert process_terms("该冲突提交联合审查，参照KU-2.1-01，见D-003。") == ["联合审查", "KU-2.1-01", "D-003"]
    # Trade words with their own meaning stay.
    assert process_terms("另一模块对低压回路逐条实测（E-12、J-2.4-3）。") == ["另一模块", "E-12", "J-2.4-3"]
    assert process_terms("设备台账完整，故障件已返修，详见 2.4.3.1。") == []
    findings = report_check_findings(
        sections={"2.1.1": "两个模块结论不同，已提交联合审查。"},
        rows_by_submodule={}, known_values=[],
    )
    assert "联合审查" in findings["2.1.1"]["problem"]
    assert findings["2.1.1"]["severity"] == "blocking"
