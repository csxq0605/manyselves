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

    calculations, lines, _lists = statistics(items, start=5)

    ng = next(c for c in calculations if c.quantity == "ng_count")
    assert (ng.id, ng.value, ng.formula) == ("K-0008", 2, "NG 对象：A房/2#、B房/3#")
    by_quantity = {c.quantity: c.value for c in calculations if c.unit}
    assert by_quantity["max_to_min_ratio"] == 3.9
    assert by_quantity["max_to_second_ratio"] == 3.6
    assert by_quantity["median"] == 3.1
    # A count is a bare number; the prose adds the measure word.
    counts = [(c.quantity, c.value) for c in calculations if c.unit == ""]
    assert ("record_count", 3) in counts and ("object_count", 3) in counts
    assert "K-" in lines[0] and "不写进正文" not in lines[0]
    assert "按数值从大到小 A房/2# 11.3A、B房/3# 3.1A、A房/1# 2.9A" in lines[1]


def test_celsius_gets_differences_and_a_derived_rise_is_ordered_too() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
        EvidenceMeasurement,
    )

    def point(number: int, subject: str, measured: float, rise: float) -> EvidenceItem:
        item = _item(number, subject, f"测点温度={measured:g}℃", measured, "℃", column="G")
        item.measurements = [
            EvidenceMeasurement(
                quantity="measured_temperature", value=measured, unit="℃", label="测点温度",
                source_cell=f"G{number}",
            ),
            EvidenceMeasurement(
                quantity="temperature_rise", value=rise, unit="K", label="相对环境温升",
                derived_from=["measured_temperature", "ambient_temperature"],
            ),
        ]
        return item

    items = [point(1, "A房/1D1", 36.1, 10.2), point(2, "A房/2D1", 31.3, 4.0), point(3, "B房/1#", 29.2, 3.9)]
    calculations, lines, _lists = statistics(items)

    celsius = [c for c in calculations if c.unit == "℃" or "测点温度" in c.subject]
    # A Celsius temperature has no meaningful ratio.
    assert not any(c.quantity in {"multiple_of_median", "max_to_min_ratio"} for c in celsius)
    assert any(c.quantity == "difference_to_median" and c.value == 4.8 for c in calculations)
    # The rise is a ratio quantity of its own: 10.2K is about 2.5 times its median 4.0K.
    rise = [c for c in calculations if c.quantity == "multiple_of_median" and c.unit == "倍"]
    assert [c.value for c in rise] == [2.5, 1.0, 1.0]
    assert any("相对环境温升" in line and "由同一记录算出" in line for line in lines)


def test_a_column_of_other_written_values_is_counted_per_value() -> None:
    items = [
        _item(1, "A房/1#", "安全连锁=无", column="F"),
        _item(2, "A房/2#", "安全连锁=无", column="F"),
        _item(3, "B房/1#", "安全连锁=有", column="F"),
    ]
    calculations, lines, _lists = statistics(items)

    counts = {c.formula: c.value for c in calculations if c.quantity == "value_count"}
    assert counts == {"记为“无”的对象：A房/1#、A房/2#": 2, "记为“有”的对象：B房/1#": 1}
    assert "记为“无”2 个" in lines[0]


def test_prose_values_are_slots_and_a_concern_object_is_named() -> None:
    rows = [
        JudgmentRow.model_validate(
            {"id": f"J-2.4-{n}", "submodule_id": "2.4.1", "object": obj, "checkpoint": "SPD",
             "disposition": "concern", "evidence_status": "现场确认", "evidence_ids": [f"E-{n}"], "basis": "NG",
             "risk_level": "中", "hazard": "h", "hazard_conditions": ["雷击浪涌"],
             "action": {"measure": "m", "priority": "一般", "timeframe": "一月"}}
        )
        for n, obj in ((1, "2#"), (2, "3#"))
    ]
    slots = {
        "V-0001": {"text": "11.3A", "subject": "A房/2#", "evidence_ids": ["E-1"]},
        "V-0002": {"text": "3.1A", "subject": "B房/3#", "evidence_ids": ["E-2"]},
        "K-0009": {"text": "9个", "label": "object_count", "evidence_ids": ["E-1", "E-2"]},
    }

    def check(text: str):
        return report_check_findings(
            sections={"2.4.1": text}, rows_by_submodule={"2.4.1": rows}, slots=slots,
        )

    assert check("2#、3# 等共〔K-0009〕SPD；2# 柜〔V-0001〕，约为 3# 柜〔V-0002〕的几倍。") == {}
    # A number the writer writes itself is free.
    assert check("2#、3# 等共 10 个 SPD。") == {}
    assert "B房/3# 旁写的〔V-0001〕" in check("B房 3# 柜剩余电流〔V-0001〕。")["2.4.1"]["problem"].replace(" 〔", "〔")
    minor = check("2# 柜〔V-0001〕。")["2.4.1"]
    assert minor["severity"] == "minor" and "J-2.4-2" in minor["problem"]


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
    assert problem_table([row]) == "另有 1 个对象资料不足，不能判定，见数据缺口表。"
    action = ChiefAction(priority="一般", problem="资质未提供", hazard="待定",
                         solution="提供资质证书", row_ids=["J-2.5-1"])
    table = action_table_markdown(
        "措施如下。", [action], {"J-2.5-1": "资料未提供"},
        {"J-2.5-1": ("运维管理", "运维单位资质", "资质")},
    )
    assert "依据：资料未提供" in table
    # The program fills 评估内容 from titles, without section numbers.
    assert "| 运维管理＞运维单位资质＞资质 |" in table


def test_action_content_merges_rows_of_one_section_and_keeps_one_shared_checkpoint() -> None:
    from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
        action_content,
    )

    places = {"J-1": ("A", "a", "温升"), "J-2": ("A", "a", "温升"), "J-3": ("A", "b", "门锁"),
              "J-4": ("A", "b", "照明")}
    assert action_content(["J-1", "J-2"], places) == "A＞a＞温升"
    assert action_content(["J-1", "J-3", "J-4"], places) == "A＞a＞温升；A＞b"


def test_gap_measure_is_written_once_and_chief_text_holds_no_table() -> None:
    from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
        chief_findings,
        problem_table,
    )

    row = JudgmentRow.model_validate(
        {"id": "J-2.4-1", "submodule_id": "2.4.1", "object": "18#柜", "checkpoint": "铭牌",
         "disposition": "undetermined", "evidence_status": "资料未提供", "evidence_ids": [],
         "basis": "b", "gap": {"missing": "铭牌", "impact": "i", "requested_input": "补充该柜铭牌参数"}}
    )
    table = problem_table([row])
    assert "补充该柜铭牌参数" not in table and "数据缺口表" in table
    findings = chief_findings(
        actions=[], health=[], risks=[], rows_by_module={},
        texts={"overall_assessment": "总体良好。\n\n| 维度 | 状态 |\n| --- | --- |\n| 环境 | 良 |"},
    )
    assert "画了表格" in findings["overall_assessment"]["problem"]
    assert chief_findings(
        actions=[], health=[], risks=[], rows_by_module={},
        texts={"overall_assessment": "总体良好，A｜B 两路供电。"},
    ) == {}


def test_only_text_before_generated_tables_may_not_hold_a_table() -> None:
    from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
        chief_findings,
    )

    table = "| 区域 | 结论 |\n| 1#楼 | 良 |"  # no separator row: still drawn as a table
    findings = chief_findings(
        actions=[], health=[], risks=[], rows_by_module={},
        texts={"overall_assessment": "总体良好。\n" + table, "regional_executive_summary": table},
    )
    assert set(findings) == {"overall_assessment"}


def test_the_edit_pass_finds_a_table_written_into_the_overview_text() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.ledger_tools import LedgerTools

    texts = {"1.1": "背景。", "1.2": "总体良好。\n| 维度 | 状态 |\n| --- | --- |", "1.3": "摘要。", "3.1": "风险。"}
    findings = LedgerTools._chief_text_findings({}, texts)
    assert set(findings) == {"1.2"} and findings["1.2"]["severity"] == "blocking"


def test_problem_table_merges_objects_with_the_same_finding() -> None:
    from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import problem_table

    def row(n: int, basis: str, disposition: str = "concern", photos: tuple[str, ...] = ()) -> JudgmentRow:
        value = {"id": f"J-2.4-{n}", "submodule_id": "2.4.2", "object": f"{n}#柜", "checkpoint": "接地",
                 "disposition": disposition, "evidence_status": "现场确认", "evidence_ids": [f"E-{n}"],
                 "basis": basis, "photo_ids": list(photos)}
        if disposition == "concern":
            value |= {"risk_level": "中", "hazard": "h",
                      "action": {"measure": "补接地线", "priority": "一般", "timeframe": "一月"}}
        else:
            value |= {"gap": {"missing": "接地照片", "impact": "i", "requested_input": "补拍照片"}}
        return JudgmentRow.model_validate(value)

    table = problem_table([
        row(1, "柜门未接地", photos=("P-1",)), row(2, "柜门未接地", photos=("P-2",)),
        row(3, "接地线截面偏小"), row(4, "x", "undetermined"), row(5, "y", "undetermined"),
    ])
    lines = [line for line in table.splitlines() if line.startswith("| ") and "---" not in line]
    assert len(lines) == 3  # header, the merged row, the different finding
    assert "| 1#柜、2#柜 | 接地 | 柜门未接地 | 中 | 补接地线 | P-1、P-2 |" in table
    assert "4#柜" not in table and "另有 2 个对象资料不足" in table


def test_a_row_writes_only_values_found_in_what_it_cites() -> None:
    def row(row_id: str, basis: str, **refs) -> dict:
        return {"id": row_id, "submodule_id": "2.4.1", "object": "1#", "checkpoint": "温升",
                "disposition": "concern", "evidence_status": "现场确认", "basis": basis,
                "risk_level": "中", "hazard": "h", "evidence_ids": ["E-1"],
                "rule_refs": ["KU-2.4-01"],
                "action": {"measure": "m", "priority": "一般", "timeframe": "一月"}, **refs}

    judgment = JudgmentSet.model_validate({"module_id": "2.4", "rows": [
        row("J-2.4-1", "测点 65.2℃，温差 12.3K，超过 40K 的限值的 3 倍以内"),
        row("J-2.4-2", "测点 65.2℃，温差 15K", calculation_ids=["K-1"]),
    ]})
    findings = ledger_findings(
        judgment, submodule_ids=["2.4.1"], module_evidence_ids=["E-1"],
        known_evidence_ids=["E-1"], known_photo_ids=[], known_calculation_ids=["K-1"],
        cited_values={"E-1": [(65.2, "℃")], "K-1": [(12.3, "K")], "KU-2.4-01": [(40, "K")]},
    )
    # 12.3K belongs to a calculation the first row does not cite; the second
    # row cites it but writes 15K.  Counts and ratios are not checked here.
    assert "12.3K" in findings["J-2.4-1"]["problem"]
    assert "40K" not in findings["J-2.4-1"]["problem"]
    assert "15K" in findings["J-2.4-2"]["problem"]
    assert findings["J-2.4-2"]["severity"] == "blocking"


def test_prose_pointing_to_a_missing_table_or_repeating_data_shown_elsewhere_is_minor() -> None:
    from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
        layout_findings,
    )

    def row(row_id: str, disposition: str, section: str) -> JudgmentRow:
        value = {"id": row_id, "submodule_id": section, "object": "1#", "checkpoint": "负荷",
                 "disposition": disposition, "evidence_status": "现场确认", "basis": "b",
                 "evidence_ids": ["E-1"]}
        if disposition == "concern":
            value |= {"risk_level": "中", "hazard": "h",
                      "action": {"measure": "m", "priority": "一般", "timeframe": "一月"}}
        return JudgmentRow.model_validate(value)

    rows = {"2.1.1": [row("J-2.1-1", "no_concern", "2.1.1")],
            "2.4.4": [row("J-2.4-1", "concern", "2.4.4")]}
    slots = {"V-0001": {"text": "320A", "subject": "1#", "evidence_ids": ["E-1"]},
             "V-0002": {"text": "41.5%", "subject": "1#", "evidence_ids": ["E-1"]}}
    findings = layout_findings(
        sections={
            "2.1.1": "负荷情况见下表。1# 运行电流〔V-0001〕，负荷率〔V-0002〕（数据见 2.4.4）。",
            "2.4.4": "1# 运行电流〔V-0001〕，负荷率〔V-0002〕，问题见下表。",
        },
        rows_by_submodule=rows, elsewhere={"J-2.1-1": ["2.4.4"]}, slots=slots,
    )
    assert set(findings) == {"2.1.1"}
    problem = findings["2.1.1"]["problem"]
    assert "没有问题表" in problem and "V-0001" in problem and "V-0002" in problem
    assert findings["2.1.1"]["severity"] == "minor"
    one_value = layout_findings(
        sections={"2.1.1": "1# 负荷率〔V-0002〕（数据见 2.4.4），负荷正常。"},
        rows_by_submodule=rows, elsewhere={"J-2.1-1": ["2.4.4"]}, slots=slots,
    )
    assert one_value == {}


def _concern(row_id: str, **extra) -> dict:
    value = {"id": row_id, "submodule_id": "2.4.1", "object": "1#", "checkpoint": "温升",
             "disposition": "concern", "evidence_status": "现场确认", "basis": "温升偏高",
             "finding": "该柜温升偏高", "risk_level": "中", "hazard": "可能加速绝缘老化", "evidence_ids": ["E-1"],
             "hazard_conditions": ["温升持续偏高"],
             "rule_refs": ["KU-2.4-01"],
             "action": {"measure": "m", "priority": "一般", "timeframe": "一月"}}
    return value | extra


def test_a_hazard_names_its_sources_or_says_it_is_unverified() -> None:
    judgment = JudgmentSet.model_validate({"module_id": "2.4", "rows": [
        _concern("J-2.4-1"),
        _concern("J-2.4-2", hazard_sources=["KU-2.4-01"]),
        _concern("J-2.4-3", hazard="可能加速绝缘老化，待核实"),
        _concern("J-2.4-4", hazard_sources=["E-9"]),
        _concern("J-2.4-5", hazard_sources=["E-1"], hazard_conditions=[]),
    ]})
    findings = ledger_findings(
        judgment, submodule_ids=["2.4.1"], module_evidence_ids=["E-1"],
        known_evidence_ids=["E-1", "E-9"], known_photo_ids=[],
    )
    assert set(findings) == {"J-2.4-1", "J-2.4-4", "J-2.4-5"}
    assert "hazard_conditions" in findings["J-2.4-5"]["problem"]
    assert "hazard_sources" in findings["J-2.4-1"]["problem"]
    assert "E-9" in findings["J-2.4-4"]["problem"]


def test_a_concern_row_states_a_short_finding_for_the_problem_table() -> None:
    from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
        problem_table,
    )

    rows = [
        _concern("J-2.4-1", finding="", hazard_sources=["E-1"]),
        _concern("J-2.4-2", finding="长" * 90, hazard_sources=["E-1"]),
        # basis is reasoning for the reviewers: ids in it are not sent back.
        _concern("J-2.4-3", hazard_sources=["E-1"], basis="按KU-2.4-01判断温升偏高"),
    ]
    findings = ledger_findings(
        JudgmentSet.model_validate({"module_id": "2.4", "rows": rows}),
        submodule_ids=["2.4.1"], module_evidence_ids=["E-1"],
        known_evidence_ids=["E-1"], known_photo_ids=[],
    )
    assert set(findings) == {"J-2.4-1", "J-2.4-2"}
    table = problem_table([JudgmentRow.model_validate(rows[2])])
    assert "该柜温升偏高" in table and "KU-" not in table and "按KU" not in table
    # A row written before finding existed shows its basis without internal ids.
    old = JudgmentRow.model_validate(rows[2] | {"finding": ""})
    assert "KU-" not in problem_table([old]) and "温升偏高" in problem_table([old])


def test_a_field_the_contract_does_not_have_is_dropped_not_sent_back() -> None:
    row = JudgmentRow.model_validate(_concern(
        "J-2.4-1", action={"measure": "m", "priority": "一般", "timeframe": "一月",
                           "decline_reason": "x", "priority_note": "y"},
    ))
    assert row.action is not None and row.action.measure == "m"
    assert "decline_reason" not in row.model_dump()["action"]


def test_a_row_on_summary_entries_only_is_set_to_the_source_authors_opinion() -> None:
    from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
        settle_evidence_status,
    )

    row = {"id": "J-2.5-1", "submodule_id": "2.5.1", "object": "台账", "checkpoint": "完整性",
           "disposition": "no_concern", "evidence_status": "现场确认", "basis": "评估表写完整",
           "evidence_ids": ["E-5"]}
    settled = settle_evidence_status([JudgmentRow.model_validate(row)], ["E-5"])
    assert settled[0].evidence_status == "来源作者意见"
    mixed = settle_evidence_status([JudgmentRow.model_validate(row | {"evidence_ids": ["E-5", "E-6"]})], ["E-5"])
    assert mixed[0].evidence_status == "现场确认"


def test_program_picks_rows_for_the_reviewer_without_judging_them() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.review_packets import (
        program_checks,
    )

    def row(row_id, disposition, status="现场确认", submodule="2.1.1"):
        value = {"id": row_id, "submodule_id": submodule, "object": "1#", "checkpoint": "c",
                 "disposition": disposition, "evidence_status": status, "basis": "b",
                 "evidence_ids": ["E-1"]}
        if disposition == "concern":
            value |= {"risk_level": "中", "hazard": "h，待核实",
                      "action": {"measure": "m", "priority": "一般", "timeframe": "一月"}}
        return JudgmentRow.model_validate(value)

    owner = row("J-2.4-1", "concern", submodule="2.4.4")
    checks = program_checks(
        [row("J-2.1-1", "no_concern"), row("J-2.1-2", "no_concern", "资料未提供")],
        facts={"E-1": "消防设施=NG"},
        owner_rows={"J-2.1-1": [owner]},
    )
    found = [(c["row_id"], c["check"][:6]) for c in checks]
    assert ("J-2.1-1", "同一份记录由") in found
    assert ("J-2.1-1", "所引记录有 ") in found
    assert ("J-2.1-2", "证据状态是资") in found


def test_a_recorded_relation_must_show_in_the_prose() -> None:
    from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
        layout_findings,
    )

    row = JudgmentRow.model_validate(_concern("J-2.4-1", hazard="h，待核实", relations=[
        {"row_id": "J-2.1-3", "relation": "负荷—发热", "reason": "负荷率低，可排除过载"}]))
    sections = {"2.4.1": "1# 温升偏高，可排除过载致热。"}
    found = layout_findings(sections=sections, rows_by_submodule={"2.4.1": [row]},
                            elsewhere={}, slots={}, row_sections={"J-2.1-3": "2.1.1"})
    assert "1#↔2.1.1" in found["2.4.1"]["problem"]
    sections = {"2.4.1": "1# 温升偏高，负荷率低（详见 2.1.1），可排除过载致热。"}
    assert layout_findings(sections=sections, rows_by_submodule={"2.4.1": [row]},
                           elsewhere={}, slots={}, row_sections={"J-2.1-3": "2.1.1"}) == {}
